"""Which photos have a backup: a small index of every library's files, shared by all of them.

Each library is its own catalog, so one library can't see what another holds. This index,
~/.joeylab/copies.db, keeps only what identifies a file: the library and photo it belongs to, its
drive (volume UUID and name) and its path on that drive, its size, quick fingerprint and (once
read) full content hash, and when it was last seen. Each library refreshes its own rows after every
update (`refresh`) and copies what concerns it into its catalog's `backups` table (`sync`), which
the Library's filters and counts read.

* A byte-identical file on another drive is a backup. The same size and quick fingerprint make a
  candidate ("probably backed up"); the full content hash, read in the background for candidates
  only and paced (`confirm`), makes it sure. Only checked backups count where it matters for
  deleting, and even then they never make a file go: they are information.
* A re-saved or exported version is a different file, so it is never a backup.
* A drive that isn't connected keeps its rows, with the day it was last seen. A file that has gone
  (to the Trash, deleted) leaves at its library's next update. Rows are kept by the catalog's file id,
  which follows moves and renames (in the app, or in Finder), so the index follows them too.
* Several libraries and the background service may write at once: WAL, short transactions, and one
  refresh per library at a time.

The index is only ever derived from the catalogs: if it's damaged it's set aside and built again.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from typing import Callable

from . import config, db, volumes

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS drives (
    key       TEXT PRIMARY KEY,     -- the volume UUID, or 'mac' for this Mac's own disk
    name      TEXT,
    mount     TEXT,                 -- where it was mounted when last seen
    last_seen REAL                  -- when a library last found it connected
);
CREATE TABLE IF NOT EXISTS libraries (
    name      TEXT PRIMARY KEY,
    refreshed REAL,
    files     INTEGER
);
CREATE TABLE IF NOT EXISTS copies (
    library   TEXT NOT NULL,
    file_id   INTEGER NOT NULL,     -- the catalog's file id: never reused, kept across moves and renames
    shot_id   INTEGER,              -- the photo it belongs to
    drive     TEXT NOT NULL,
    rel       TEXT NOT NULL,        -- its path inside the drive
    size      INTEGER NOT NULL,
    fp        TEXT NOT NULL,        -- the scan's quick fingerprint (size, first and last 256 KB)
    mtime_ns  INTEGER,              -- so a hash is never kept for a file written again
    full_hash TEXT,                 -- the whole content's hash, once read
    seen      REAL,                 -- last seen (to the day while its drive is connected)
    PRIMARY KEY (library, file_id)
);
CREATE INDEX IF NOT EXISTS copies_fp ON copies(fp);
"""
BATCH = 2000                 # rows per write transaction
SEEN_EVERY_S = 86400.0       # rows on a connected drive say when they were seen, to the day
GEN_KEY = "copies:gen"       # in a catalog's meta: the index generation its `backups` table was copied from
KNOWN_KEY = "copies:known"   # in a catalog's meta: the library is in the index


def path():
    return config.HOME / "copies.db"


# ---------------------------------------------------------------- the index file

def _open(p) -> sqlite3.Connection:
    c = sqlite3.connect(p, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        if c.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                db._script(c, SCHEMA)
                c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            c.commit()
        c.execute("SELECT 1 FROM copies LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        c.close()
        raise
    return c


def connect(create: bool = True) -> sqlite3.Connection | None:
    """The shared index (None when there isn't one yet and create is off). A damaged index is set
    aside and started again: every library adds its rows back at its next refresh."""
    p = path()
    if not p.exists():
        if not create:
            return None
        config.secure_home()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.close(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            pass
    try:
        return _open(p)
    except sqlite3.DatabaseError:
        if not create:
            return None
        os.replace(p, p.with_name(f"copies.db.damaged-{time.strftime('%Y%m%d-%H%M%S')}"))
        for side in ("-wal", "-shm"):
            try:
                os.unlink(str(p) + side)
            except OSError:
                pass
        os.close(os.open(p, os.O_WRONLY | os.O_CREAT, 0o600))
        return _open(p)


def generation(ix) -> int:
    """A number that changes with every write to the index, so a catalog knows its copy is stale."""
    r = ix.execute("SELECT value FROM meta WHERE key='gen'").fetchone()
    return int(r[0]) if r else 0


def _bump(ix) -> None:
    ix.execute("INSERT INTO meta(key, value) VALUES ('gen', '1') ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")


def _write(ix, work: list[tuple[str, list]]) -> int:
    """Apply [(sql, rows)] in short transactions of BATCH rows each, the generation bumped in each."""
    n = 0
    for sql, rows in work:
        for i in range(0, len(rows), BATCH):
            chunk = rows[i : i + BATCH]
            ix.execute("BEGIN IMMEDIATE")
            try:
                ix.executemany(sql, chunk)
                _bump(ix)
                ix.commit()
            except Exception:
                ix.rollback()
                raise
            n += len(chunk)
    return n


@contextmanager
def _one_refresh(project: str, wait: float = 120.0):
    """One refresh of a library's rows at a time (an update and the background service could both
    start one)."""
    import fcntl

    from .locks import Busy

    lock = config.project_dir(project) / "copies.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.time() + wait
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise Busy(f"The backup index of {project!r} is being refreshed already.")
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- refreshing a library's rows

def _roots(conn) -> tuple[dict, dict, dict, dict]:
    """(root paths, their drives, which are connected now, where each connected one is mounted)."""
    roots = {r["id"]: r["path"] for r in conn.execute("SELECT id, path FROM roots")}
    drives = volumes.root_drives(conn)
    connected, mounts = {}, {}
    for rid, p in roots.items():
        d = drives.get(rid)
        here = volumes.drive_of(p) if d and os.path.isdir(p) else None
        connected[rid] = bool(here and here["key"] == d["key"])
        if connected[rid]:
            mounts[rid] = here["mount"]
    return roots, drives, connected, mounts


def refresh(conn, project: str) -> dict:
    """Bring this library's rows in the index up to date with its catalog: new files added, moved or
    changed ones updated, gone ones removed. Only what changed is written. Files on a drive that
    can't be told now (an unplugged disk image) keep the rows they had."""
    from .health import last_seen

    t0 = time.time()
    with _one_refresh(project):
        roots, drives, connected, mounts = _roots(conn)
        volumes.remember_drives(conn, drives, connected)
        seen_at = last_seen(conn)
        now = time.time()
        want: dict[int, tuple] = {}
        unknown: set[int] = set()
        for f in conn.execute("SELECT id, shot_id, root_id, path, size, fp, mtime_ns, full_hash FROM files WHERE status='present'"):
            d = drives.get(f["root_id"])
            if d is None or f["root_id"] not in roots:
                unknown.add(f["id"])
                continue
            seen = now if connected.get(f["root_id"]) else seen_at.get(roots[f["root_id"]])
            want[f["id"]] = (f["shot_id"], d["key"], volumes.rel_on_drive(f["path"], roots[f["root_id"]], d["base"]),
                             f["size"], f["fp"], f["mtime_ns"], f["full_hash"], seen, connected.get(f["root_id"], False))
        ix = connect()
        try:
            have = {r["file_id"]: r for r in ix.execute(
                "SELECT file_id, shot_id, drive, rel, size, fp, mtime_ns, full_hash, seen FROM copies WHERE library=?", (project,))}
            inserts, updates, seens = [], [], []
            for fid, (shot, key, rel, size, fp, mt, fh, seen, live) in want.items():
                old = have.get(fid)
                if old is None:
                    inserts.append((project, fid, shot, key, rel, size, fp, mt, fh, seen))
                    continue
                same_file = (old["size"], old["fp"], old["mtime_ns"]) == (size, fp, mt)
                h = fh or (old["full_hash"] if same_file else None)
                if (old["shot_id"], old["drive"], old["rel"], old["size"], old["fp"], old["mtime_ns"], old["full_hash"]) != \
                        (shot, key, rel, size, fp, mt, h):
                    updates.append((shot, key, rel, size, fp, mt, h, seen if live else None, project, fid))
                elif live and (old["seen"] or 0) < now - SEEN_EVERY_S:
                    seens.append((now, project, fid))
            gone = [(project, fid) for fid in have if fid not in want and fid not in unknown]
            _write(ix, [
                (("INSERT INTO copies(library, file_id, shot_id, drive, rel, size, fp, mtime_ns, full_hash, seen)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)"), inserts),
                (("UPDATE copies SET shot_id=?, drive=?, rel=?, size=?, fp=?, mtime_ns=?, full_hash=?, seen=COALESCE(?, seen)"
                  " WHERE library=? AND file_id=?"), updates),
                ("UPDATE copies SET seen=? WHERE library=? AND file_id=?", seens),
                ("DELETE FROM copies WHERE library=? AND file_id=?", gone),
            ])
            ix.execute("BEGIN IMMEDIATE")
            try:
                for rid, d in drives.items():
                    if d is None:
                        continue
                    if connected.get(rid):
                        ix.execute("INSERT INTO drives(key, name, mount, last_seen) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE"
                                   " SET name=excluded.name, mount=excluded.mount, last_seen=MAX(COALESCE(last_seen, 0), excluded.last_seen)",
                                   (d["key"], d["name"], mounts[rid], now))
                    else:
                        ix.execute("INSERT INTO drives(key, name, mount, last_seen) VALUES (?,?,NULL,?) ON CONFLICT(key) DO UPDATE"
                                   " SET last_seen=MAX(COALESCE(last_seen, 0), COALESCE(excluded.last_seen, 0))",
                                   (d["key"], d["name"], seen_at.get(roots[rid])))
                ix.execute("INSERT INTO libraries(name, refreshed, files) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE"
                           " SET refreshed=excluded.refreshed, files=excluded.files", (project, now, len(want) + len(unknown)))
                ix.commit()
            except Exception:
                ix.rollback()
                raise
            _prune_libraries(ix)
        finally:
            ix.close()
    return {"files": len(want), "added": len(inserts), "changed": len(updates), "removed": len(gone),
            "seconds": round(time.time() - t0, 3)}


def _prune_libraries(ix) -> int:
    """Rows of libraries that aren't there any more (a catalog removed by hand) mustn't count as backups."""
    present = set(config.list_projects())
    stale = [r[0] for r in ix.execute("SELECT DISTINCT library FROM copies") if r[0] not in present]
    stale += [r[0] for r in ix.execute("SELECT name FROM libraries") if r[0] not in present and r[0] not in stale]
    for name in stale:
        _forget(ix, name)
    return len(stale)


def _forget(ix, name: str) -> None:
    ix.execute("BEGIN IMMEDIATE")
    try:
        ix.execute("DELETE FROM copies WHERE library=?", (name,))
        ix.execute("DELETE FROM libraries WHERE name=?", (name,))
        _bump(ix)
        ix.commit()
    except Exception:
        ix.rollback()
        raise


def rename_library(old: str, new: str) -> None:
    """A library was renamed: its rows follow it."""
    ix = connect(create=False)
    if ix is None:
        return
    try:
        ix.execute("BEGIN IMMEDIATE")
        try:
            ix.execute("DELETE FROM copies WHERE library=?", (new,))
            ix.execute("UPDATE copies SET library=? WHERE library=?", (new, old))
            ix.execute("DELETE FROM libraries WHERE name=?", (new,))
            ix.execute("UPDATE libraries SET name=? WHERE name=?", (new, old))
            _bump(ix)
            ix.commit()
        except Exception:
            ix.rollback()
            raise
    finally:
        ix.close()


def move_rows(src: str, dst: str, files: dict[int, tuple[int, int | None]]) -> int:
    """Files moved to another library with their folders (split.py): their rows follow them, with
    their new ids ({old file id: (new file id, new photo id)}), keeping what's known of their content,
    so nothing is read whole again. Returns how many rows moved."""
    ix = connect(create=False)
    if ix is None or not files:
        return 0
    try:
        news = [(dst, n) for n, _ in files.values()]
        rows = [(dst, n, s, src, o) for o, (n, s) in files.items()]
        _write(ix, [("DELETE FROM copies WHERE library=? AND file_id=?", news),      # (none, unless left from before)
                    ("UPDATE copies SET library=?, file_id=?, shot_id=? WHERE library=? AND file_id=?", rows)])
        return len(rows)
    finally:
        ix.close()


def forget_library(name: str) -> None:
    """A library was removed: its files no longer back anything up."""
    ix = connect(create=False)
    if ix is None:
        return
    try:
        _forget(ix, name)
    finally:
        ix.close()


# ---------------------------------------------------------------- what concerns one library

_PAIRS = ("SELECT a.file_id, a.shot_id, a.full_hash AS ah, b.drive, b.full_hash AS bh FROM copies a"
          " JOIN copies b ON b.fp = a.fp AND b.size = a.size AND b.drive != a.drive WHERE a.library = ?")


def sync(conn, project: str, force: bool = False) -> bool:
    """Copy this library's backups from the index into its catalog (the `backups` table), when the
    index has changed since the last copy. Cheap when nothing changed. Returns whether it copied."""
    ix = connect(create=False)
    if ix is None:
        return False
    where = conn.execute("PRAGMA database_list").fetchone()[2]
    try:
        gen = generation(ix)
        if not force and gen and (_SYNCED.get(where) == gen or db.counter(conn, GEN_KEY) == gen):
            return False
        known = ix.execute("SELECT 1 FROM libraries WHERE name=?", (project,)).fetchone() is not None
        found: dict[int, list] = {}
        for r in ix.execute(_PAIRS, (project,)):
            if r["ah"] and r["bh"] and r["ah"] != r["bh"]:
                continue                       # the same start and end, but not the same file
            v = found.setdefault(r["file_id"], [r["shot_id"], 0, set()])
            v[1] |= bool(r["ah"] and r["bh"])
            v[2].add(r["drive"])
    finally:
        ix.close()
    rows = {fid: (shot, int(checked), ",".join(sorted(ds))) for fid, (shot, checked, ds) in found.items()}
    old = {r[0]: (r[1], r[2], r[3]) for r in conn.execute("SELECT file_id, shot_id, checked, drives FROM backups")}
    gone = [(fid,) for fid in old if fid not in rows]
    put = [(fid, *v) for fid, v in rows.items() if old.get(fid) != v]
    if not force and not gone and not put and known == bool(db.counter(conn, KNOWN_KEY)) and db.counter(conn, GEN_KEY):
        _SYNCED[where] = gen            # another library changed: nothing here did, so the catalog isn't written
        return False
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany("DELETE FROM backups WHERE file_id=?", gone)
        conn.executemany("INSERT INTO backups(file_id, shot_id, checked, drives) VALUES (?,?,?,?) ON CONFLICT(file_id)"
                         " DO UPDATE SET shot_id=excluded.shot_id, checked=excluded.checked, drives=excluded.drives", put)
        for key, value in ((GEN_KEY, gen), (KNOWN_KEY, int(known))):
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, str(value)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    _SYNCED[where] = gen
    return True


_SYNCED: dict[str, int] = {}     # catalog path -> the index generation it was last found up to date with


def known(conn) -> bool:
    """Whether this library is in the index (its catalog's copy says so)."""
    return bool(db.counter(conn, KNOWN_KEY))


def ensure(conn, project: str) -> bool:
    """sync(), never failing the caller: a page that shows backups uses whatever the catalog has."""
    try:
        sync(conn, project)
    except (sqlite3.Error, OSError):
        return False
    return known(conn)


def drive_states(ix=None) -> dict[str, dict]:
    """Every drive the index knows: {key: {name, mount, last_seen, connected}}. Connected means
    mounted now as itself (by its UUID), wherever that is."""
    own = ix is None
    ix = ix or connect(create=False)
    if ix is None:
        return {volumes.LOCAL: {"name": volumes.LOCAL_NAME, "mount": os.sep, "last_seen": time.time(), "connected": True}}
    try:
        rows = ix.execute("SELECT key, name, mount, last_seen FROM drives").fetchall()
    finally:
        if own:
            ix.close()
    out, mounted = {}, None
    for r in rows:
        d = {"name": r["name"], "mount": r["mount"], "last_seen": r["last_seen"], "connected": False}
        if r["key"] == volumes.LOCAL:
            d.update(connected=True, mount=os.sep, name=volumes.LOCAL_NAME)
        elif r["mount"] and os.path.isdir(r["mount"]) and volumes.info(r["mount"]).get("uuid") == r["key"]:
            d["connected"] = True
        else:
            mounted = volumes.mounted() if mounted is None else mounted
            if r["key"] in mounted:
                d.update(connected=True, mount=mounted[r["key"]])
        out[r["key"]] = d
    return out


def copies_of(project: str, file_ids: list[int]) -> dict[int, list[dict]]:
    """For these files of a library, every other file the index knows with the same content:
    {file id: [{library, shot_id, file_id, drive, drive_name, rel, checked, same_drive, connected,
    last_seen}]}, checked copies first. `checked`: the full content hashes agree; otherwise only the
    size and quick fingerprint do (probably the same). Files whose hashes differ aren't copies."""
    ix = connect(create=False)
    if ix is None or not file_ids:
        if ix is not None:
            ix.close()
        return {}
    out: dict[int, list[dict]] = defaultdict(list)
    try:
        ids = [int(x) for x in file_ids]
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            for r in ix.execute(
                    "SELECT a.file_id AS mine, a.drive AS here, a.full_hash AS ah, b.library, b.file_id, b.shot_id, b.drive,"
                    " b.rel, b.full_hash AS bh, b.seen FROM copies a JOIN copies b ON b.fp = a.fp AND b.size = a.size"
                    " AND NOT (b.library = a.library AND b.file_id = a.file_id)"
                    f" WHERE a.library = ? AND a.file_id IN ({','.join('?' * len(chunk))})", (project, *chunk)):
                if r["ah"] and r["bh"] and r["ah"] != r["bh"]:
                    continue
                out[r["mine"]].append({"library": r["library"], "shot_id": r["shot_id"], "file_id": r["file_id"],
                                       "drive": r["drive"], "rel": r["rel"], "checked": bool(r["ah"] and r["bh"]),
                                       "same_drive": r["drive"] == r["here"], "seen": r["seen"]})
        states = drive_states(ix)
    finally:
        ix.close()
    for cs in out.values():
        for c in cs:
            st = states.get(c["drive"], {})
            c.update(drive_name=st.get("name") or c["drive"], connected=bool(st.get("connected")),
                     last_seen=st.get("last_seen") or c["seen"])
        cs.sort(key=lambda c: (c["same_drive"], not c["checked"], not c["connected"], c["drive_name"], c["rel"]))
    return dict(out)


def for_shot(conn, project: str, shot_id: int) -> dict:
    """The viewer's and MCP's view of one photo: for each of its present files, its backups on other
    drives (checked or probable), and whether it's the only copy anywhere JoeyLab knows."""
    files = conn.execute("SELECT id, path, kind, size FROM files WHERE shot_id=? AND status='present' ORDER BY kind",
                         (shot_id,)).fetchall()
    ensure(conn, project)
    found = copies_of(project, [f["id"] for f in files])
    out = []
    for f in files:
        cs = found.get(f["id"], [])
        out.append({"file_id": f["id"], "name": os.path.basename(f["path"]), "kind": f["kind"], "size": f["size"],
                    "backups": [c for c in cs if not c["same_drive"]],
                    "same_drive": [c for c in cs if c["same_drive"]]})
    backed = [x for x in out if x["backups"]]
    return {"known": known(conn), "files": out,
            "all_backed_up": bool(out) and len(backed) == len(out),
            "all_checked": bool(out) and all(any(c["checked"] for c in x["backups"]) for x in out),
            "only_here": [x["name"] for x in out if not x["backups"]]}


def notes_for_paths(conn, project: str, paths: list[str]) -> dict[str, dict]:
    """For the Trash dialogs: each file's checked backup on another drive, if any. Only backups whose
    full content hash agrees are named (a probable one is counted as not checked yet); `copies` counts
    every other copy the index knows anywhere, on this drive or another, checked or not."""
    ids: dict[str, int] = {}
    for i in range(0, len(paths), 500):
        chunk = paths[i : i + 500]
        for fid, p in conn.execute(f"SELECT id, path FROM files WHERE path IN ({','.join('?' * len(chunk))})", chunk):
            ids[p] = fid
    ensure(conn, project)
    found = copies_of(project, list(ids.values()))
    out = {}
    for p in paths:
        cs = found.get(ids.get(p), [])
        checked = [c for c in cs if c["checked"] and not c["same_drive"]]
        unchecked = [c for c in cs if not c["checked"] and not c["same_drive"]]
        b = checked[0] if checked else None
        out[p] = {"backup": {k: b[k] for k in ("library", "shot_id", "drive_name", "rel", "connected", "last_seen")} if b else None,
                  "backups": len(checked), "unchecked": len(unchecked),
                  "unchecked_on": unchecked[0]["drive_name"] if unchecked else None, "copies": len(cs)}
    return out


def trash_summary(notes: dict[str, dict], last: set[str]) -> dict:
    """How many of the files a Trash dialog lists have a checked backup, how many are the only copy
    anywhere (no copy stays in the library and the index knows no other), and how many have a copy
    elsewhere that hasn't been checked yet."""
    backed = sum(1 for n in notes.values() if n["backup"])
    only = sum(1 for p, n in notes.items() if p in last and not n["copies"])
    unchecked = sum(1 for n in notes.values() if not n["backup"] and n["unchecked"])
    return {"backed_up": backed, "only_copy": only, "unchecked": unchecked, "files": len(notes)}


# ---------------------------------------------------------------- the Backups page

def report(conn, project: str, folders: int = 400) -> dict:
    """Per drive and per folder of this library: how many photos have a backup on another drive and
    where, how many are only on this drive (and so at risk), the space each takes, and the drives
    holding the backups, connected or not. A photo counts as backed up when every one of its files
    has a copy on another drive; as checked when every one of those has been compared byte for byte."""
    is_known = ensure(conn, project)
    roots, drives, connected, _ = _roots(conn)
    states = drive_states()
    seen_at = {}
    from .health import last_seen

    seen_at = last_seen(conn)
    per_drive: dict[str, dict] = {}
    per_folder: dict[tuple, dict] = {}

    def blank() -> dict:
        return {"photos": 0, "bytes": 0, "backed": 0, "backed_bytes": 0, "checked": 0, "only": 0, "only_bytes": 0,
                "where": Counter()}

    for r in conn.execute(
            "SELECT s.id, s.dir, MIN(f.root_id) AS root_id, COUNT(f.id) AS files, SUM(f.size) AS bytes,"
            " SUM(b.file_id IS NOT NULL) AS backed, SUM(COALESCE(b.checked, 0)) AS checked, GROUP_CONCAT(b.drives) AS ds"
            " FROM shots s JOIN files f ON f.shot_id = s.id AND f.status = 'present' LEFT JOIN backups b ON b.file_id = f.id"
            " WHERE s.status = 'present' GROUP BY s.id"):
        d = drives.get(r["root_id"])
        key = d["key"] if d else f"root:{r['root_id']}"
        if key not in per_drive:
            per_drive[key] = blank() | {"key": key, "name": d["name"] if d else os.path.basename(roots.get(r["root_id"], "") or "?"),
                                        "folders_in": set()}
        targets = [per_drive[key], per_folder.setdefault((key, r["dir"]), blank() | {"path": r["dir"], "drive": key})]
        size = r["bytes"] or 0
        whole = r["backed"] == r["files"]
        where = set((r["ds"] or "").replace(",,", ",").split(",")) - {""} if whole else set()
        for t in targets:
            t["photos"] += 1
            t["bytes"] += size
            if whole:
                t["backed"] += 1
                t["backed_bytes"] += size
                t["checked"] += r["checked"] == r["files"]
                t["where"].update(where)
            else:
                t["only"] += 1
                t["only_bytes"] += size
    for rid, p in roots.items():               # a library folder's drive is listed even with no photos (yet)
        d = drives.get(rid)
        key = d["key"] if d else f"root:{rid}"
        per_drive.setdefault(key, blank() | {"key": key, "name": d["name"] if d else os.path.basename(p), "folders_in": set()})
        per_drive[key]["folders_in"].add(p)
        if connected.get(rid):
            per_drive[key]["connected"] = True
        per_drive[key]["last_seen"] = max(per_drive[key].get("last_seen") or 0, seen_at.get(p) or 0) or None

    def name_of(k: str) -> str:
        return (states.get(k) or {}).get("name") or k

    def where_list(c: Counter) -> list[dict]:
        return [{"drive": k, "name": name_of(k), "photos": v, "connected": bool((states.get(k) or {}).get("connected")),
                 "last_seen": (states.get(k) or {}).get("last_seen")} for k, v in c.most_common()]

    out_drives = []
    for key, t in sorted(per_drive.items(), key=lambda kv: (-kv[1]["only_bytes"], kv[1]["name"])):
        fs = sorted((f for (k, _), f in per_folder.items() if k == key), key=lambda f: (-f["only_bytes"], -f["only"], f["path"]))
        out_drives.append({
            "key": key, "name": t["name"], "connected": bool(t.get("connected")), "last_seen": t.get("last_seen"),
            "local": key == volumes.LOCAL, "roots": sorted(t["folders_in"]),
            **{k: t[k] for k in ("photos", "bytes", "backed", "backed_bytes", "checked", "only", "only_bytes")},
            "where": where_list(t["where"]),
            "folders": [{**{k: f[k] for k in ("path", "photos", "bytes", "backed", "backed_bytes", "checked", "only", "only_bytes")},
                         "where": where_list(f["where"])[:3]} for f in fs[:folders]],
            "more_folders": max(0, len(fs) - folders),
        })
    holders = Counter()
    for t in per_drive.values():
        holders.update(t["where"])
    mine = {d["key"] for d in out_drives}
    libs = _libraries_by_drive()
    return {
        "known": is_known,
        "photos": sum(d["photos"] for d in out_drives), "bytes": sum(d["bytes"] for d in out_drives),
        "backed": sum(d["backed"] for d in out_drives), "backed_bytes": sum(d["backed_bytes"] for d in out_drives),
        "checked": sum(d["checked"] for d in out_drives),
        "only": sum(d["only"] for d in out_drives), "only_bytes": sum(d["only_bytes"] for d in out_drives),
        "unchecked_files": conn.execute("SELECT COUNT(*) FROM backups b JOIN files f ON f.id = b.file_id"
                                        " WHERE f.status='present' AND b.checked = 0").fetchone()[0],
        "drives": out_drives,
        "holders": [{**w, "libraries": libs.get(w["drive"], []), "this_library": w["drive"] in mine} for w in where_list(holders)],
        "libraries": sorted({lib for ls in libs.values() for lib in ls}),
        "refreshed": _refreshed(project),
    }


def _libraries_by_drive() -> dict[str, list[str]]:
    ix = connect(create=False)
    if ix is None:
        return {}
    try:
        out: dict[str, list[str]] = defaultdict(list)
        for drive, lib in ix.execute("SELECT DISTINCT drive, library FROM copies ORDER BY library"):
            out[drive].append(lib)
        return dict(out)
    finally:
        ix.close()


def _refreshed(project: str) -> float | None:
    ix = connect(create=False)
    if ix is None:
        return None
    try:
        r = ix.execute("SELECT refreshed FROM libraries WHERE name=?", (project,)).fetchone()
        return r[0] if r else None
    finally:
        ix.close()


def counts(conn) -> dict:
    """Photos with a backup on another drive and photos only on this drive (the Library's rail): the
    Library's backup filters (library.BACKUP_SQL) counted in one pass, from each photo's present files
    and how many of them have a copy elsewhere (+status: the photos in id order, grouped as they come)."""
    r = conn.execute(
        "SELECT SUM(nb = nf), SUM(nc = nf), SUM(nb < nf) FROM (SELECT COUNT(fb.id) AS nf, COUNT(b.file_id) AS nb,"
        " COUNT(CASE WHEN b.checked = 1 THEN 1 END) AS nc FROM shots s"
        " LEFT JOIN files fb ON fb.shot_id = s.id AND fb.status = 'present' LEFT JOIN backups b ON b.file_id = fb.id"
        " WHERE +s.status = 'present' AND (s.error IS NULL OR s.analyzed_fp IS NOT NULL) GROUP BY s.id)").fetchone()
    return {"known": known(conn), "elsewhere": r[0] or 0, "checked": r[1] or 0, "only": r[2] or 0}


# ---------------------------------------------------------------- checking candidates byte for byte

def _hash(path: str, rate: float | None, stop: Callable[[], bool] | None) -> str | None:
    """The full content hash (the same as hashing.full_hash), read at no more than `rate` bytes a
    second. None if asked to stop half way."""
    h = hashlib.blake2b(digest_size=20)
    t0, n = time.monotonic(), 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
            n += len(block)
            if rate:
                ahead = n / rate - (time.monotonic() - t0)
                if ahead > 0:
                    time.sleep(min(ahead, 1.0))
            if stop and stop():
                return None
    return h.hexdigest()


def candidates(ix=None) -> int:
    """Files that may be backups (or backed up) but haven't been read whole yet."""
    own = ix is None
    ix = ix or connect(create=False)
    if ix is None:
        return 0
    try:
        return ix.execute("SELECT COUNT(*) FROM copies a WHERE a.full_hash IS NULL AND EXISTS (SELECT 1 FROM copies b"
                          " WHERE b.fp = a.fp AND b.size = a.size AND b.drive != a.drive)").fetchone()[0]
    finally:
        if own:
            ix.close()


def confirm(seconds: float = 60.0, rate_mb: float | None = 40.0, stop: Callable[[], bool] | None = None,
            progress: Callable[[int, int], None] | None = None) -> dict:
    """Read candidates whole, read-only, to confirm (or rule out) that they're the same file as their
    copy on another drive. Only candidates are read: files with the same size and fingerprint as a
    file on another drive and no full hash yet. Those whose other copy is already hashed go first
    (one read settles them), then the smallest. Paced (at most rate_mb MB a second), stopped after
    `seconds` or when stop() says so, and only files on connected drives that are exactly as indexed
    (same size and modified time) are read. Hashes are written in small transactions."""
    ix = connect(create=False)
    if ix is None:
        return {"read": 0, "bytes": 0, "left": 0}
    end = time.time() + seconds
    rate = rate_mb * 1e6 if rate_mb else None
    try:
        states = drive_states(ix)
        todo = ix.execute(
            "SELECT a.library, a.file_id, a.drive, a.rel, a.size, a.fp, a.mtime_ns, MAX(b.full_hash IS NOT NULL) AS settles"
            " FROM copies a JOIN copies b ON b.fp = a.fp AND b.size = a.size AND b.drive != a.drive"
            " WHERE a.full_hash IS NULL GROUP BY a.library, a.file_id ORDER BY settles DESC, a.size, a.library, a.file_id").fetchall()
        done, read, pending, last_flush = 0, 0, [], time.time()

        def flush():
            nonlocal pending, last_flush
            if pending:
                _write(ix, [("UPDATE copies SET full_hash=? WHERE library=? AND file_id=? AND fp=? AND size=? AND full_hash IS NULL",
                             pending)])
            pending, last_flush = [], time.time()

        for i, r in enumerate(todo):
            if time.time() > end or (stop and stop()):
                break
            if progress:
                progress(i, len(todo))
            st = states.get(r["drive"]) or {}
            if not st.get("connected") or not st.get("mount"):
                continue
            p = os.path.join(st["mount"], r["rel"])
            try:
                s = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(s.st_mode) or s.st_size != r["size"] or (r["mtime_ns"] is not None and s.st_mtime_ns != r["mtime_ns"]):
                continue                        # not the file indexed: its library's next refresh sorts it out
            try:
                h = _hash(p, rate, (lambda: time.time() > end + 600 or bool(stop and stop())))
            except OSError:
                continue
            if h is None:
                break
            pending.append((h, r["library"], r["file_id"], r["fp"], r["size"]))
            done += 1
            read += r["size"]
            if len(pending) >= 25 or time.time() - last_flush > 2:
                flush()
        flush()
        return {"read": done, "bytes": read, "left": candidates(ix)}
    finally:
        ix.close()
