"""Moving folders to another library, with everything JoeyLab knows about their photos.

A library's purpose decides how its photos are treated: a shoot is set up for culling, an archive for
tidying. A drive that holds both (wedding shoots among everyday photos) can be split: the wedding
folders become a library of their own, with its own purpose, rules, people, events and decisions.

Nothing is read again. Every catalog row about each photo with a file under the chosen folders (or,
for a missing photo, last seen there) goes with it: the photo and its files, its measurements, colours
and picture vectors, its faces with the people they're named as (and their other names), its
keywords, decisions, RAW choices, dates given by hand and time shifts with the history of all of
those, its places in albums, favourites and comments, and its backups on other drives (the tables
are listed in db.PER_PHOTO and db.PER_PHOTO_LINKED). Its thumbnails and previews go too (moved, never
made again). Stacks, duplicates, events, trips, scores and taste are then worked out again in both
libraries (`recompute`), as a normal background step.

It's catalog only: no photo file is read, written, moved or renamed. A stop at any point leaves the
library the photos came from as it was:

1. both catalogs are backed up first (`backups/<library>/before-move-<id>-<time>.db`, kept until
   removed by hand), while the write lock of the library they come from is held, so nothing changes
   there until the move is over;
2. the other library is written in one transaction and checked, rows per table, before it's
   committed; a new library is made in a folder of its own and only takes its name once complete;
3. only then are the rows removed from the library they came from, in one transaction (rows, never
   tables), and its scans leave the folders out from then on (the `excluded_folders` table);
4. last, the thumbnails and previews are renamed into the other library's folder; a stop there loses
   none of them (`finish` moves the rest);
5. each move is logged in `split-log/` and can be moved back: the same operation in reverse.

Ids. Photos, files, faces, stacks, events, albums, keywords, people and face groups have ids that are
never handed out twice in a library (`db.new_id`, high-water marks), so nothing left pointing at an
old id lands on a different photo. A moved row keeps its id where that's safe: always in a new
library, and in an existing one when the id was never used there, or is one that library gave away by
an earlier move (so a move back restores the ids too). Otherwise it gets a new one. Every library
remembers the ids it gave away (`LEDGER`), and its high-water marks keep them from being reused.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

from . import config, db, libraries, locks

PURPOSES = ("shoot", "archive")
LEDGER = "moved_out"            # settings: the ids this library gave away by moves, {table: [[first, last], ...]}
SIMPLE = db.PER_PHOTO          # one row (or a few) per photo, keyed by shot_id and nothing else
CACHED = ("thumbs", "thumbs_s", "previews", "previews/faces")
_SKIP = object()


class Refused(ValueError):
    """The move can't be done; the message says why, in plain words."""


def _log_dir() -> Path:
    return config.HOME / "split-log"


def _staging() -> Path:
    return config.HOME / "staging"


def _inside(p: str, folder: str) -> bool:
    return p == folder or p.startswith(folder.rstrip(os.sep) + os.sep)


def _range(folder: str) -> tuple[str, str]:
    """Paths strictly inside `folder` sort between these two (the byte after "/" is "0")."""
    f = folder.rstrip(os.sep)
    return f + "/", f + "0"


def _lit(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _plural(k: int, one: str, many: str | None = None) -> str:
    return f"{k:,} {one if k == 1 else many or one + 's'}"


def _bytes(b: int) -> str:
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if b >= size:
            return f"{b / size:.1f} {unit}".replace(".0 ", " ")
    return f"{b} bytes"


# ---------------------------------------------------------------- the ledger of ids given away
#
# {library: {table: {its id: our id}}}: which rows this library gave to another one, and the ids they
# have there. Kept in the settings as runs [its first id, our first id, how many], since ids move in
# long consecutive runs. A row coming back from that library takes its old id again.

def _runs(m: dict[int, int]) -> list[list[int]]:
    out: list[list[int]] = []
    for a in sorted(m):
        b = m[a]
        if out and a == out[-1][0] + out[-1][2] and b == out[-1][1] + out[-1][2]:
            out[-1][2] += 1
        else:
            out.append([a, b, 1])
    return out


def _ledger(conn) -> dict[str, dict[str, dict[int, int]]]:
    raw = db.get_setting(conn, LEDGER, {}) or {}
    out: dict[str, dict[str, dict[int, int]]] = {}
    for lib, tables in (raw.items() if isinstance(raw, dict) else ()):
        for t, runs in (tables.items() if isinstance(tables, dict) else ()):
            m = out.setdefault(lib, {}).setdefault(t, {})
            for a, b, n in runs:
                m.update(zip(range(int(a), int(a) + int(n)), range(int(b), int(b) + int(n))))
    return out


def _put(conn, key: str, value) -> None:
    """A setting written inside the current transaction (db.set_setting commits)."""
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))


def _save_ledger(conn, led: dict) -> None:
    _put(conn, LEDGER, {lib: {t: _runs(m) for t, m in sorted(ts.items()) if m}
                        for lib, ts in sorted(led.items()) if any(ts.values())})


def _hwm(conn, table: str) -> int:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (f"hwm:{table}",)).fetchone()
    top = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
    return max(int(r[0]) if r and r[0] is not None else 0, int(top or 0))


def _raise_hwm(conn, table: str, top: int) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                 " value = MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))", (f"hwm:{table}", int(top)))


def _assign(olds, present: set[int], top: int, back: dict[int, int], new: bool, merge: bool = False) -> dict[int, int]:
    """Where each moved id lands. In a new library, as it is. Otherwise: a row this library gave away
    (`back`: its id there -> its id here) takes its old id again; an id never used here (above the
    high-water mark `top`) is kept; anything else gets a new id above `top`. With merge, a row whose old
    id is still here joins it (a stack or face group split by the move, coming back)."""
    out: dict[int, int] = {}
    taken: set[int] = set()
    fresh: list[int] = []
    for o in olds:
        if new:
            out[o] = o
            continue
        w = back.get(o)
        if w is not None and ((w not in present and w not in taken) or (merge and w in present)):
            out[o] = w
            taken.add(w)
        elif w is None and o > top and o not in present and o not in taken:
            out[o] = o
            taken.add(o)
        else:
            fresh.append(o)
    nxt = max([top, *out.values()]) + 1
    for o in fresh:
        out[o] = nxt
        nxt += 1
    return out


# ---------------------------------------------------------------- the folders

def _known_folders(conn, roots: list[str]) -> set[str]:
    """The library's folders that hold photos, and every folder between them and its library folders."""
    out = set(roots)
    for (d,) in conn.execute("SELECT DISTINCT dir FROM shots"):
        root = next((r for r in roots if _inside(d, r)), None)
        p = d
        while root and p not in out and len(p) > len(root):
            out.add(p)
            p = os.path.dirname(p)
    return out


def _resolve(conn, src: str, raw: str, roots: list[str]) -> str:
    """A folder as typed (a full path, or one inside a library folder) as the catalog spells it."""
    text = (raw or "").strip()
    if not text:
        raise Refused("Choose a folder to move.")
    text = os.path.normpath(os.path.expanduser(text))
    tries = [text] if os.path.isabs(text) else [os.path.join(r, text) for r in roots]
    known = _known_folders(conn, roots)
    for t in tries:
        if t in known:
            return t
    for t in tries:
        hits = [k for k in known if libraries.same_path(k, t)]
        if len(hits) == 1:
            return hits[0]
    for t in tries:
        if os.path.isdir(t) and any(_inside(t, r) for r in roots):
            return t                         # a folder with no photos in it yet
    raise Refused(f"“{src}” has no folder {raw}. Choose one of its folders (Library › Folders lists them).")


def _excluded_rows(conn) -> list[dict]:
    for cols in ("folder, library, added, ino", "folder, library, added"):
        try:
            return [dict(r) for r in conn.execute(f"SELECT {cols} FROM excluded_folders ORDER BY folder")]
        except sqlite3.OperationalError:          # a catalog not upgraded yet
            continue
    return []


def _ino(folder: str) -> int | None:
    """A folder's inode, by which it's followed when renamed or moved in Finder (scanner._walk)."""
    try:
        return db.to_i64(os.lstat(folder).st_ino)
    except OSError:
        return None


def _unfinished(*names: str) -> dict | None:
    for op in _all_ops():
        if op.get("state") in ("copying", "copied", "removed") and (op["src"] in names or op["dst"] in names):
            return op
    return None


def check(src: str, folders: list[str], dst: str, purpose: str | None = None) -> dict:
    """Whether these folders of `src` can move to `dst` (a library, or the name of a new one), read-only.
    Returns what's needed to do it; raises Refused with the reason, in plain words."""
    if src not in config.list_projects():
        raise Refused(f"There's no library called “{src}”.")
    name = (dst or "").strip()
    if not name:
        raise Refused("Choose the library they go to, or give a new one a name.")
    new = name not in config.list_projects()
    if new:
        name = libraries.slug(name)
        why = libraries.name_problem(name)
        if why:
            raise Refused(why)
        if config.project_dir(name).exists():
            raise Refused(f"There's already a folder for a library called “{name}” in JoeyLab's home; choose another name.")
    if name == src:
        raise Refused(f"Those folders are in “{src}” already. Choose another library, or give a new one a name.")
    if purpose is not None and purpose not in PURPOSES:
        raise Refused("A library is a shoot (to cull) or an archive (to tidy).")
    if purpose is not None and not new:
        raise Refused(f"“{name}” is a library already: change what it's for in its own Settings.")
    for lib in (src,) if new else (src, name):
        if libraries._unfinished_move(lib):
            raise Refused(f"A move of files in “{lib}” stopped half way. Update the library once (JoeyLab finishes it), "
                          "then try again.")
    op = _unfinished(src, name)
    if op:
        raise Refused(f"An earlier move from “{op['src']}” to “{op['dst']}” stopped half way; finish it first "
                      f"(joeylab library split --finish {op['id']}).")

    cs = db.connect(src, create=False)
    try:
        roots = [r[0] for r in cs.execute("SELECT path FROM roots ORDER BY length(path) DESC")]
        src_excluded = _excluded_rows(cs)
        chosen: list[str] = []
        for raw in folders or []:
            f = _resolve(cs, src, raw, roots)
            gone = next((e for e in src_excluded if _inside(f, e["folder"])), None)
            if gone:
                to = f"the library “{gone['library']}”" if gone["library"] else "another library"
                raise Refused(f"{f} isn't in “{src}” any more: it was moved to {to}.")
            if not any(_inside(f, r) for r in roots):
                raise Refused(f"{f} isn't one of “{src}”'s folders.")
            chosen.append(f)
        if not chosen:
            raise Refused("Choose at least one folder to move.")
    finally:
        cs.close()
    # A folder inside another chosen one goes with it.
    chosen = sorted(set(chosen), key=len)
    folders_ = [f for i, f in enumerate(chosen) if not any(_inside(f, g) for g in chosen[:i])]

    everyone = libraries.claims()
    mine = everyone.get(src, {"roots": [], "excluded": []})
    theirs = everyone.get(name, {"roots": [], "excluded": []})
    back: dict[str, bool] = {}
    for f in folders_:
        left_out = [e for e in mine["excluded"] if _inside(e, f) and e != f]     # already other libraries'
        why = libraries.overlap(f, everyone, skip=(src, name), left_out=left_out)
        if why:
            raise Refused(why)
        cover = next((r for r in theirs["roots"] if _inside(f, r)), None)
        if cover:
            if f not in theirs["excluded"]:
                raise Refused(f"“{name}” already includes {f}" + ("" if cover == f else f" (it's inside {cover})") + ".")
            back[f] = True                   # a move back: it left that library before
        under = [e for e in theirs["excluded"] if _inside(e, f) and e != f]
        if under:
            raise Refused(f"“{name}” leaves {under[0]} out (it was moved elsewhere), so {f} can't go there whole. "
                          f"Move {under[0]} back first.")
        if cover is None and any(_inside(f, e) for e in theirs["excluded"]):
            raise Refused(f"{f} is inside a folder “{name}” leaves out. Move that whole folder back instead.")
        for r in theirs["roots"]:
            if r != f and _inside(r, f) and r not in mine["excluded"]:
                raise Refused(f"{f} holds {r}, which is in “{name}” as well as “{src}”. Remove one of them first.")
    return {"src": src, "dst": name, "new": new, "purpose": purpose, "folders": folders_, "back": back}


# ---------------------------------------------------------------- what moves

def _moved(conn, folders: list[str]) -> dict[str, list[int]]:
    """The photos under the folders, with every file of each, and their faces: a photo with a file
    there now, or a missing one that was last there (its folder). A photo whose files are elsewhere now
    stays, even if a missing file of it was once under them. Files of no photo go by where they are.
    Left in temporary tables x_shots, x_files and x_faces of this connection."""
    shots: set[int] = set()
    files: set[int] = set()
    for f in folders:
        lo, hi = _range(f)
        shots |= {r[0] for r in conn.execute(
            "SELECT DISTINCT shot_id FROM files WHERE status = 'present' AND shot_id IS NOT NULL AND path >= ? AND path < ?",
            (lo, hi))}
        shots |= {r[0] for r in conn.execute(
            "SELECT id FROM shots WHERE (dir = ? OR (dir >= ? AND dir < ?)) AND id NOT IN"
            " (SELECT shot_id FROM files WHERE status = 'present' AND shot_id IS NOT NULL)", (f, lo, hi))}
        files |= {r[0] for r in conn.execute("SELECT id FROM files WHERE shot_id IS NULL AND path >= ? AND path < ?", (lo, hi))}
    for t in ("x_shots", "x_files", "x_faces"):
        conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {t} (id INTEGER PRIMARY KEY)")
        conn.execute(f"DELETE FROM temp.{t}")
    conn.executemany("INSERT INTO temp.x_shots(id) VALUES (?)", [(i,) for i in shots])
    files |= {r[0] for r in conn.execute("SELECT id FROM files WHERE shot_id IN (SELECT id FROM temp.x_shots)")}
    conn.executemany("INSERT OR IGNORE INTO temp.x_files(id) VALUES (?)", [(i,) for i in files])
    conn.execute("INSERT INTO temp.x_faces(id) SELECT id FROM faces WHERE shot_id IN (SELECT id FROM temp.x_shots)")
    faces = [r[0] for r in conn.execute("SELECT id FROM temp.x_faces")]
    return {"shots": sorted(shots), "files": sorted(files), "faces": faces}


def _cached(pdir: Path, shots: set[int], faces: set[int], kinds=CACHED):
    """JoeyLab's own images of these photos and faces in a library's folder: (path, kind, id, the
    rest of the name after the id, whether it's a folder of zoom tiles)."""
    for kind in kinds:
        ids = faces if kind == "previews/faces" else shots
        base = pdir / kind
        if not ids or not base.is_dir():
            continue
        for sub in os.scandir(base):
            if not (len(sub.name) == 2 and sub.name.isdigit() and sub.is_dir(follow_symlinks=False)):
                continue
            for e in os.scandir(sub.path):
                head = e.name.split("_", 1)[0].split(".", 1)[0]
                if head.isdigit() and int(head) in ids:
                    yield e, kind, int(head), e.name[len(head):], e.is_dir(follow_symlinks=False)


def _cache_bytes(project: str, shots: list[int], faces: list[int], sample: int = 1500) -> int:
    """About how much of the source library's thumbnails and previews belong to these photos: the files
    are counted, and the sizes of an even sample of each kind stand for the rest (a library of tens of
    thousands of photos has hundreds of thousands of them, too many to look at each one while a plan is
    shown)."""
    by_kind: dict[str, list[str]] = {}
    for e, kind, _, _, is_dir in _cached(config.project_dir(project), set(shots), set(faces)):
        paths = by_kind.setdefault(kind, [])
        if is_dir:
            try:
                paths += [x.path for x in os.scandir(e.path) if x.is_file(follow_symlinks=False)]
            except OSError:
                continue
        else:
            paths.append(e.path)
    total = 0
    for paths in by_kind.values():
        step = max(1, len(paths) // sample)
        sizes = []
        for p in paths[::step]:
            try:
                sizes.append(os.stat(p).st_size)
            except OSError:
                continue
        if sizes:
            total += round(sum(sizes) / len(sizes) * len(paths))
    return total


_BYTES: dict[tuple, tuple[float, int, int]] = {}


def _bytes_for(src: str, folders: list[str], m: dict) -> int:
    """_cache_bytes, remembered for a couple of minutes: the app asks for the plan again as a new
    library's name is typed."""
    key = (src, tuple(folders))
    hit = _BYTES.get(key)
    if hit and time.time() - hit[0] < 120 and hit[1] == len(m["shots"]):
        return hit[2]
    n = _cache_bytes(src, m["shots"], m["faces"])
    _BYTES[key] = (time.time(), len(m["shots"]), n)
    return n


def plan(src: str, folders: list[str], dst: str, purpose: str | None = None) -> dict:
    """What moving these folders would do, read-only, with the sentence to show before it's confirmed."""
    chk = check(src, folders, dst, purpose)
    conn = db.connect(src, create=False)
    try:
        m = _moved(conn, chk["folders"])
        x = "SELECT id FROM temp.x_shots"
        one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
        photos = one(f"SELECT COUNT(*) FROM shots WHERE id IN ({x}) AND status='present'")
        missing = one(f"SELECT COUNT(*) FROM shots WHERE id IN ({x}) AND status!='present'")
        people = [r[0] for r in conn.execute(
            f"SELECT p.name FROM people p WHERE p.id IN (SELECT DISTINCT person_id FROM faces WHERE shot_id IN ({x})"
            " AND person_id > 0) ORDER BY p.name COLLATE NOCASE")]
        out = {
            **{k: chk[k] for k in ("src", "dst", "new", "purpose", "folders")},
            "photos": photos, "missing": missing, "files": len(m["files"]), "faces": len(m["faces"]),
            "people": len(people), "people_names": people[:12],
            "decisions": one(f"SELECT COUNT(*) FROM decisions WHERE shot_id IN ({x}) AND"
                             " (pick != 0 OR COALESCE(rating, 0) > 0 OR COALESCE(label, '') != '')"),
            "history": one(f"SELECT COUNT(*) FROM decision_log WHERE shot_id IN ({x})"),
            "albums": one(f"SELECT COUNT(DISTINCT album_id) FROM album_items WHERE shot_id IN ({x})"
                          " OR file_id IN (SELECT id FROM temp.x_files)"),
            "keywords": one(f"SELECT COUNT(DISTINCT keyword_id) FROM shot_keywords WHERE shot_id IN ({x})"),
            "previews_bytes": _bytes_for(src, chk["folders"], m),
        }
    finally:
        conn.close()
    out["what"] = describe(out)
    return out


def describe(p: dict) -> str:
    """"Move 52,340 photos (48 GB of previews already made, 290,000 faces, 3 people named) from … to a
    new shoot library “weddings”. Their decisions, names and history go with them. …"""
    extra = [f"about {_bytes(p['previews_bytes'])} of previews already made"] if p["previews_bytes"] else []
    if p["faces"]:
        extra.append(_plural(p["faces"], "face"))
    if p["people"]:
        extra.append(f"{_plural(p['people'], 'person', 'people')} named")
    what = _plural(p["photos"], "photo") + (f" ({', '.join(extra)})" if extra else "")
    if p["missing"]:
        what += f" and {_plural(p['missing'], 'missing photo')}"
    kind = f"{p['purpose']} " if p.get("purpose") in PURPOSES else ""
    to = f"a new {kind}library “{p['dst']}”" if p["new"] else f"the library “{p['dst']}”"
    return (f"Move {what} from {p['src']} to {to}. Their decisions, names and history go with them. "
            "Both libraries are then regrouped in the background.")


# ---------------------------------------------------------------- doing it

def _backup(db_path: Path, project: str, opid: str) -> Path:
    """A named copy of a catalog, kept until removed by hand (backup.prune leaves these alone)."""
    dest_dir = config.HOME / "backups" / project
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"before-move-{opid}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(db_path)
    try:
        out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        out.close()
    finally:
        src.close()
    config.private(dest)
    return dest


def _cols(conn, schema: str, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _has(conn, schema: str, table: str) -> bool:
    return conn.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _copy(ct, table: str, over: dict | None = None, join: str = "", where: str = "", verb: str = "INSERT",
          order: str = "") -> int:
    """INSERT INTO main.table SELECT … FROM the other catalog (s), column by column, some replaced."""
    if not _has(ct, "s", table):
        return 0
    over = over or {}
    have = set(_cols(ct, "s", table))
    cols = [c for c in _cols(ct, "main", table) if c in have and over.get(c) is not _SKIP]
    sel = ", ".join(over.get(c, f'x."{c}"') for c in cols)
    names = ", ".join(f'"{c}"' for c in cols)
    return ct.execute(f'{verb} INTO main."{table}" ({names}) SELECT {sel} FROM s."{table}" x {join} {where} {order}').rowcount


def _schema_from(ct) -> None:
    """Tables, indexes and triggers the other catalog has that this one lacks (made as they're first
    needed: picture vectors, search, dates given by hand…)."""
    virtual = [r[0] for r in ct.execute("SELECT name FROM s.sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL%'")]
    have = {r[0] for r in ct.execute("SELECT name FROM main.sqlite_master")}
    for kind in ("table", "index", "trigger"):
        for name, sql in ct.execute("SELECT name, sql FROM s.sqlite_master WHERE type=? AND sql IS NOT NULL", (kind,)).fetchall():
            if name in have or name.startswith("sqlite_") or any(name.startswith(v + "_") for v in virtual):
                continue
            ct.execute(sql)


def _map_table(ct, name: str, mapping: dict[int, int], merged: set[int] = frozenset()) -> None:
    ct.execute(f"CREATE TEMP TABLE IF NOT EXISTS {name} (old INTEGER PRIMARY KEY, new INTEGER NOT NULL, merged INTEGER NOT NULL DEFAULT 0)")
    ct.execute(f"DELETE FROM temp.{name}")
    ct.executemany(f"INSERT INTO temp.{name}(old, new, merged) VALUES (?,?,?)",
                   [(o, n, int(o in merged)) for o, n in mapping.items()])


def _move_keyed_settings(cs, ct, folders: list[str], m_shots: dict[int, int]) -> None:
    """Settings kept by folder or by photo follow them: your names for folders, "always keep copies
    under" rules, photos you merged into one event, and frames tagged for a shot list."""
    names = db.get_setting(cs, "folder_names", {}) or {}
    if isinstance(names, dict):
        go = {k: v for k, v in names.items() if any(_inside(k, f) for f in folders)}
        if go:
            theirs = db.get_setting(ct, "folder_names", {}) or {}
            _put(ct, "folder_names", {**theirs, **go})
            _put(cs, "folder_names", {k: v for k, v in names.items() if k not in go})
    rules = db.get_setting(cs, "keep_under", []) or []
    if isinstance(rules, list):
        inside = [r for r in rules if isinstance(r, str) and any(_inside(r, f) for f in folders)]
        above = [r for r in rules if isinstance(r, str) and any(_inside(f, r) for f in folders) and r not in inside]
        if inside or above:
            theirs = db.get_setting(ct, "keep_under", []) or []
            _put(ct, "keep_under", sorted(set(theirs) | set(inside) | set(above)))
            if inside:
                _put(cs, "keep_under", sorted(r for r in rules if r not in inside))
    joins = db.get_setting(cs, "event_joins", []) or []
    if isinstance(joins, list) and joins:
        both = [{**j, "a": m_shots[int(j["a"])], "b": m_shots[int(j["b"])]} for j in joins
                if isinstance(j, dict) and j.get("a") in m_shots and j.get("b") in m_shots]
        stay = [j for j in joins if not (isinstance(j, dict) and (j.get("a") in m_shots or j.get("b") in m_shots))]
        if both:
            _put(ct, "event_joins", (db.get_setting(ct, "event_joins", []) or []) + both)
        if len(stay) != len(joins):
            _put(cs, "event_joins", stay)
    for (folder,) in cs.execute("SELECT folder FROM keep_folders").fetchall():
        if any(_inside(folder, f) for f in folders):
            ct.execute("INSERT OR IGNORE INTO keep_folders(folder) VALUES (?)", (folder,))
            cs.execute("DELETE FROM keep_folders WHERE folder=?", (folder,))


def _shot_list(cs, ct, new: bool, m_shots: dict[int, int], m_people: dict[int, int]) -> None:
    """Shot-list items with frames tagged among the moved photos go with them (their tags too)."""
    from .smart import SHOT_TAGS, _save_tags

    raw = db.get_setting(cs, SHOT_TAGS) or {}
    tags = {int(k): [int(i) for i in v] for k, v in raw.items()} if isinstance(raw, dict) else {}
    touched = {k: [m_shots[i] for i in v if i in m_shots] for k, v in tags.items()}
    touched = {k: v for k, v in touched.items() if v}
    if not touched:
        return
    theirs_raw = db.get_setting(ct, SHOT_TAGS) or {}
    theirs = {int(k): [int(i) for i in v] for k, v in theirs_raw.items()} if isinstance(theirs_raw, dict) else {}
    for item_id, ids in touched.items():
        row = cs.execute("SELECT title, query, people, min_count, position FROM shotlist WHERE id=?", (item_id,)).fetchone()
        if not row:
            continue
        try:
            people = [m_people[int(p)] for p in json.loads(row["people"] or "[]") if int(p) in m_people]
        except (ValueError, TypeError):
            people = []
        hit = ct.execute("SELECT id FROM shotlist WHERE title=?", (row["title"],)).fetchone()
        if hit:
            tid = hit[0]
        else:
            cur = ct.execute("INSERT INTO shotlist(id, title, query, people, min_count, position) VALUES (?,?,?,?,?,?)",
                             (item_id if new else None, row["title"], row["query"], json.dumps(people), row["min_count"],
                              row["position"]))
            tid = cur.lastrowid
        theirs[tid] = list(dict.fromkeys(theirs.get(tid, []) + ids))
        tags[item_id] = [i for i in tags[item_id] if i not in m_shots]
        if not tags[item_id]:                # every frame it had went: the item goes too
            cs.execute("DELETE FROM shotlist WHERE id=?", (item_id,))
            del tags[item_id]
    _save_tags(ct, theirs, commit=False)
    _save_tags(cs, tags, commit=False)


def _people(cs, ct, new: bool, olds: list[int], back: dict[int, int]) -> tuple[dict[int, int], set[int]]:
    """Each moved person in the other library: the person already called that there (by any of their
    names), or a new one with their other names. Returns (old id -> new id, the ones made)."""
    from . import aliases

    top = _hwm(ct, "people")
    present = {r[0] for r in ct.execute("SELECT id FROM people")}
    out: dict[int, int] = {}
    made: set[int] = set()
    have = set(_cols(ct, "main", "people"))
    for pid in olds:
        row = cs.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not row:
            continue
        found = aliases.owner(ct, row["name"])
        if found:
            out[pid] = found[0]
        else:
            w = back.get(pid)
            if new:
                nid = pid
            elif w is not None and w not in present:
                nid = w
            elif w is None and pid > top and pid not in present:
                nid = pid
            else:
                nid = max(top, *present, *out.values(), 0) + 1
            cols = [c for c in row.keys() if c in have]
            vals = [nid if c == "id" else row[c] for c in cols]
            ct.execute(f"INSERT INTO people({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
            present.add(nid)
            out[pid] = nid
            made.add(nid)
        for (alias,) in cs.execute("SELECT name FROM person_aliases WHERE person_id=? ORDER BY rowid", (pid,)).fetchall():
            if aliases.owner(ct, alias) is None:
                ct.execute("INSERT INTO person_aliases(person_id, name) VALUES (?, ?)", (out[pid], alias))
    if out:
        _raise_hwm(ct, "people", max(out.values()))
    return out, made


def _keywords(cs, ct, new: bool, used: set[int]) -> dict[int, int]:
    """Each keyword the moved photos have (or had, in their history), with the keywords above it, in
    the other library: the same keyword there (by its place in the tree), or a new one."""
    rows = {r["id"]: dict(r) for r in cs.execute("SELECT * FROM keywords")}
    need: set[int] = set()
    for k in used:
        while k in rows and k not in need:
            need.add(k)
            k = rows[k]["parent_id"]
    depth = {}

    def d(k):
        if k not in depth:
            p = rows[k]["parent_id"]
            depth[k] = 0 if p not in rows else d(p) + 1
        return depth[k]

    top = _hwm(ct, "keywords")
    present = {r[0] for r in ct.execute("SELECT id FROM keywords")}
    have = set(_cols(ct, "main", "keywords"))
    out: dict[int, int] = {}
    for k in sorted(need, key=d):
        r = rows[k]
        parent = out.get(r["parent_id"]) if r["parent_id"] is not None else None
        hit = ct.execute("SELECT id FROM keywords WHERE name = ? AND parent_id IS ? ORDER BY COALESCE(deleted, 0), id LIMIT 1",
                         (r["name"], parent)).fetchone()
        if hit:
            out[k] = hit[0]
            continue
        if new or (k not in present and k > top):
            nid = k
        else:
            nid = max(top, *present, 0) + 1
        cols = [c for c in r if c in have]
        vals = [nid if c == "id" else parent if c == "parent_id" else r[c] for c in cols]
        ct.execute(f"INSERT INTO keywords({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        present.add(nid)
        out[k] = nid
    if out:
        _raise_hwm(ct, "keywords", max(out.values()))
    return out


def _albums(cs, ct, new: bool, olds: list[int], back: dict[int, int]) -> dict[int, int]:
    """Each album with moved photos in the other library: the same album there (a hand-picked one by
    its name, one found on a drive by its folder), or a new one."""
    top = _hwm(ct, "albums")
    present = {r[0] for r in ct.execute("SELECT id FROM albums")}
    have = set(_cols(ct, "main", "albums"))
    out: dict[int, int] = {}
    for aid in olds:
        a = cs.execute("SELECT * FROM albums WHERE id=?", (aid,)).fetchone()
        if not a:
            continue
        if a["folder"]:
            hit = ct.execute("SELECT id FROM albums WHERE folder=?", (a["folder"],)).fetchone()
        else:
            hit = ct.execute("SELECT id FROM albums WHERE kind=? AND name=? AND folder IS NULL ORDER BY id LIMIT 1",
                             (a["kind"], a["name"])).fetchone()
        if hit:
            out[aid] = hit[0]
            continue
        w = back.get(aid)
        if new:
            nid = aid
        elif w is not None and w not in present:
            nid = w
        elif w is None and aid > top and aid not in present:
            nid = aid
        else:
            nid = max(top, *present, 0) + 1
        cols = [c for c in a.keys() if c in have]
        ct.execute(f"INSERT INTO albums({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   [nid if c == "id" else a[c] for c in cols])
        present.add(nid)
        out[aid] = nid
    if out:
        _raise_hwm(ct, "albums", max(out.values()))
    return out


def _write_target(ct, cs, chk: dict, moved: dict, final_dir: Path, opid: str) -> dict:
    """Copy every row about the moved photos into the other library (inside ct's transaction, with the
    source's backup attached as `s`). Returns the id maps and counts."""
    new = chk["new"]
    xs = "SELECT id FROM temp.x_shots"
    ledger = _ledger(ct)
    back = ledger.get(chk["src"], {})              # rows this library gave to that one earlier: their old ids
    shots, files, faces = moved["shots"], moved["files"], moved["faces"]

    def present(table: str, col: str = "id") -> set[int]:
        return set() if new else {r[0] for r in ct.execute(f"SELECT {col} FROM main.{table} WHERE {col} IS NOT NULL")}

    m_shots = _assign(shots, present("shots"), 0 if new else _hwm(ct, "shots"), back.get("shots", {}), new)
    m_files = _assign(files, present("files"), 0 if new else _hwm(ct, "files"), back.get("files", {}), new)
    m_faces = _assign(faces, present("faces"), 0 if new else _hwm(ct, "faces"), back.get("faces", {}), new)
    _map_table(ct, "m_shots", m_shots)
    _map_table(ct, "m_files", m_files)
    _map_table(ct, "m_faces", m_faces)

    # Stacks, events (with their trips) and face groups: kept, joined (a move back) or new.
    groups = [r[0] for r in cs.execute(f"SELECT DISTINCT group_id FROM shots WHERE id IN ({xs}) AND group_id IS NOT NULL")]
    events = {r[0] for r in cs.execute(f"SELECT DISTINCT event_id FROM shots WHERE id IN ({xs}) AND event_id IS NOT NULL")}
    events |= {r[0] for r in cs.execute(f"SELECT DISTINCT parent_id FROM events WHERE id IN ({','.join(map(str, events)) or 'NULL'})"
                                        " AND parent_id IS NOT NULL")}
    clusters = [r[0] for r in cs.execute(f"SELECT DISTINCT cluster FROM faces WHERE shot_id IN ({xs}) AND cluster IS NOT NULL")]
    pg, pe = present("groups"), present("events")
    m_groups = _assign(groups, pg, 0 if new else _hwm(ct, "groups"), back.get("groups", {}), new, merge=True)
    m_events = _assign(sorted(events), pe, 0 if new else _hwm(ct, "events"), back.get("events", {}), new, merge=True)
    pc = present("faces", "cluster")
    top_c = 0 if new else max(int(db.get_setting(ct, "hwm_clusters", 0) or 0), max(pc, default=0))
    m_clusters = _assign(clusters, pc, top_c, back.get("clusters", {}), new, merge=True)
    _map_table(ct, "m_groups", m_groups, {o for o, n in m_groups.items() if n in pg})
    _map_table(ct, "m_events", m_events, {o for o, n in m_events.items() if n in pe})
    _map_table(ct, "m_clusters", m_clusters, {o for o, n in m_clusters.items() if n in pc})

    people_old = [r[0] for r in cs.execute(f"SELECT DISTINCT person_id FROM faces WHERE shot_id IN ({xs}) AND person_id > 0")]
    m_people, people_made = _people(cs, ct, new, people_old, back.get("people", {}))
    _map_table(ct, "m_people", m_people)
    used_kw = {r[0] for r in cs.execute(f"SELECT DISTINCT keyword_id FROM shot_keywords WHERE shot_id IN ({xs})")}
    log_rows = cs.execute(f"SELECT id, shot_id, before, after FROM decision_log WHERE shot_id IN ({xs}) ORDER BY id").fetchall()
    for r in log_rows:
        if r["before"] and '"keywords"' in r["before"]:
            for side in (r["before"], r["after"]):
                try:
                    used_kw.update(int(k) for k in (json.loads(side or "{}").get("keywords") or []))
                except (ValueError, TypeError, AttributeError):
                    pass
    m_kw = _keywords(cs, ct, new, used_kw)
    _map_table(ct, "m_keywords", m_kw)
    album_old = [r[0] for r in cs.execute(
        f"SELECT DISTINCT album_id FROM album_items WHERE shot_id IN ({xs}) OR file_id IN (SELECT id FROM temp.x_files)")]
    m_albums = _albums(cs, ct, new, album_old, back.get("albums", {}))
    _map_table(ct, "m_albums", m_albums)

    # The history: in a new library (or one with none of its own), as it was; in an existing one, steps
    # it gave away come back as they were and the rest go before its own (its Undo reaches its own
    # changes first).
    blog = back.get("decision_log", {})
    have_log = present("decision_log")
    m_log: dict[int, int] = {}
    for r in log_rows:
        w = r["id"] if new else blog.get(r["id"])
        if w is not None and w not in have_log:
            m_log[r["id"]] = w
    used = set(m_log.values())
    rest = []
    for r in log_rows:
        if r["id"] not in m_log:
            if not have_log and r["id"] not in used:
                m_log[r["id"]] = r["id"]
                used.add(r["id"])
            else:
                rest.append(r["id"])
    if rest:
        low = min([*have_log, *used, 1])
        for k, i in enumerate(rest):
            m_log[i] = low - len(rest) + k
    _map_table(ct, "m_log", m_log)

    # ---- rows
    tdir = final_dir / "thumbs"
    thumb = (f"CASE WHEN x.thumb IS NULL THEN NULL ELSE {_lit(tdir)} || '/' || printf('%02d', m.new % 100) || '/' || m.new"
             " || '.jpg' END")
    _copy(ct, "shots", {"id": "m.new", "preview_file_id": "mf.new", "group_id": "mg.new", "event_id": "me.new", "thumb": thumb},
          "JOIN temp.m_shots m ON m.old = x.id LEFT JOIN temp.m_files mf ON mf.old = x.preview_file_id"
          " LEFT JOIN temp.m_groups mg ON mg.old = x.group_id LEFT JOIN temp.m_events me ON me.old = x.event_id")
    _copy(ct, "files", {"id": "m.new", "shot_id": "ms.new", "root_id": "NULL"},
          "JOIN temp.m_files m ON m.old = x.id LEFT JOIN temp.m_shots ms ON ms.old = x.shot_id")
    for t in SIMPLE:
        _copy(ct, t, {"shot_id": "m.new"}, "JOIN temp.m_shots m ON m.old = x.shot_id")
    _copy(ct, "backups", {"file_id": "m.new", "shot_id": "ms.new"},
          "JOIN temp.m_files m ON m.old = x.file_id LEFT JOIN temp.m_shots ms ON ms.old = x.shot_id")
    _copy(ct, "comments", {"id": "x.id" if new else _SKIP, "shot_id": "m.new"}, "JOIN temp.m_shots m ON m.old = x.shot_id",
          order="ORDER BY x.id")
    _copy(ct, "faces", {"id": "m.new", "shot_id": "ms.new", "cluster": "mc.new",
                        "person_id": "CASE WHEN x.person_id > 0 THEN mp.new ELSE x.person_id END"},
          "JOIN temp.m_faces m ON m.old = x.id JOIN temp.m_shots ms ON ms.old = x.shot_id"
          " LEFT JOIN temp.m_clusters mc ON mc.old = x.cluster LEFT JOIN temp.m_people mp ON mp.old = x.person_id")
    _copy(ct, "shot_keywords", {"shot_id": "ms.new", "keyword_id": "mk.new"},
          "JOIN temp.m_shots ms ON ms.old = x.shot_id JOIN temp.m_keywords mk ON mk.old = x.keyword_id", verb="INSERT OR IGNORE")
    _copy(ct, "decision_log", {"id": "ml.new", "shot_id": "ms.new"},
          "JOIN temp.m_log ml ON ml.old = x.id JOIN temp.m_shots ms ON ms.old = x.shot_id", order="ORDER BY x.id")
    if any(o != n for o, n in m_kw.items()):
        for r in log_rows:
            if r["before"] and '"keywords"' in r["before"]:
                vals = []
                for side in (r["before"], r["after"]):
                    try:
                        d = json.loads(side or "{}")
                        d["keywords"] = sorted(m_kw.get(int(k), int(k)) for k in d.get("keywords") or [])
                    except (ValueError, TypeError, AttributeError):
                        d = None
                    vals.append(json.dumps(d) if d is not None else side)
                ct.execute("UPDATE main.decision_log SET before=?, after=? WHERE id=?", (*vals, m_log[r["id"]]))
    if _has(ct, "s", "search_fts") and _has(ct, "main", "search_fts"):
        ct.execute("INSERT INTO main.search_fts(rowid, labels, text) SELECT m.new, x.labels, x.text FROM s.search_fts x"
                   " JOIN temp.m_shots m ON m.old = x.rowid")
    _copy(ct, "groups", {"id": "m.new", "best_shot_id": "mb.new"},
          "JOIN temp.m_groups m ON m.old = x.id LEFT JOIN temp.m_shots mb ON mb.old = x.best_shot_id", "WHERE m.merged = 0")
    _copy(ct, "events", {"id": "m.new", "parent_id": "mp.new", "cover_shot_id": "mc.new"},
          "JOIN temp.m_events m ON m.old = x.id LEFT JOIN temp.m_events mp ON mp.old = x.parent_id"
          " LEFT JOIN temp.m_shots mc ON mc.old = x.cover_shot_id", "WHERE m.merged = 0")
    for eid, people in ct.execute("SELECT id, people FROM main.events WHERE id IN (SELECT new FROM temp.m_events WHERE merged = 0)"
                                  " AND people IS NOT NULL").fetchall():
        try:
            fixed = [[m_people[int(p)], k] for p, k in json.loads(people) if int(p) in m_people]
        except (ValueError, TypeError):
            fixed = []
        ct.execute("UPDATE main.events SET people=? WHERE id=?", (json.dumps(fixed), eid))
    _copy(ct, "places", where=f"WHERE x.id IN (SELECT DISTINCT place_id FROM s.shots WHERE id IN ({xs}) AND place_id IS NOT NULL)",
          verb="INSERT OR IGNORE")
    _copy(ct, "camera_offsets", verb="INSERT OR IGNORE")
    _copy(ct, "face_not_same", {"person_id": "mp.new", "cluster": "mc.new"},
          "JOIN temp.m_people mp ON mp.old = x.person_id JOIN temp.m_clusters mc ON mc.old = x.cluster", verb="INSERT OR IGNORE")
    _copy(ct, "face_groups", {"cluster": "mc.new"}, "JOIN temp.m_clusters mc ON mc.old = x.cluster", "WHERE mc.merged = 0",
          verb="INSERT OR IGNORE")
    # Album places, by hand: two photos of one name in one album keep both.
    for r in cs.execute(f"SELECT album_id, file_id, shot_id, name, source_path FROM album_items WHERE shot_id IN ({xs})"
                        " OR file_id IN (SELECT id FROM temp.x_files)").fetchall():
        if r["album_id"] not in m_albums:
            continue
        aid, name = m_albums[r["album_id"]], r["name"]
        fid = m_files.get(r["file_id"], r["file_id"])
        if ct.execute("SELECT 1 FROM album_items WHERE album_id=? AND name=? AND file_id=?", (aid, name, fid)).fetchone():
            continue
        stem, ext = os.path.splitext(name)
        k = 2
        while ct.execute("SELECT 1 FROM album_items WHERE album_id=? AND name=?", (aid, name)).fetchone():
            name, k = f"{stem} ({k}){ext}", k + 1
        ct.execute("INSERT INTO album_items(album_id, file_id, shot_id, name, source_path) VALUES (?,?,?,?,?)",
                   (aid, fid, m_shots.get(r["shot_id"]) if r["shot_id"] is not None else None, name, r["source_path"]))

    # ---- the folders: the library's own from now on
    folder_root = _target_roots(ct, cs, chk)
    for f, rid in folder_root.items():
        lo, hi = _range(f)
        ct.execute("UPDATE main.files SET root_id=? WHERE id IN (SELECT new FROM temp.m_files) AND (path = ? OR (path >= ? AND path < ?))",
                   (rid, f, lo, hi))
    src_left_out = _excluded_rows(cs)
    for f in chk["folders"]:
        ct.execute("DELETE FROM main.excluded_folders WHERE folder=?", (f,))
        for e in src_left_out:                  # parts of it that went to a third library stay out here too
            if _inside(e["folder"], f) and e["folder"] != f and e["library"] != chk["dst"]:
                ct.execute("INSERT OR IGNORE INTO main.excluded_folders(folder, library, added, ino) VALUES (?,?,?,?)",
                           (e["folder"], e["library"], e["added"], e.get("ino")))
    _move_keyed_settings(cs, ct, chk["folders"], m_shots)
    _shot_list(cs, ct, new, m_shots, m_people)

    # ---- settings and counters
    if new:
        for key in ("faces_model", "faces_enabled", "faces_full_at", "hwm_clusters", "faces_seen_upto"):
            v = db.get_setting(cs, key)
            if v is not None:
                _put(ct, key, v)
        if chk["purpose"] in PURPOSES:
            _put(ct, "purpose", chk["purpose"])
            if chk["purpose"] == "shoot":
                _put(ct, "grouping", config.SHOOT_GROUPING)  # a shoot stacks tightly, as when it's chosen in the app
        for k, v in cs.execute("SELECT key, value FROM meta WHERE key LIKE 'hwm:%'").fetchall():
            _raise_hwm(ct, k[4:], int(v or 0))
    for table, mp in (("shots", m_shots), ("files", m_files), ("faces", m_faces), ("groups", m_groups), ("events", m_events)):
        if mp:
            _raise_hwm(ct, table, max(mp.values()))
    if m_clusters:
        _put(ct, "hwm_clusters", max(int(db.get_setting(ct, "hwm_clusters", 0) or 0), max(m_clusters.values())))
    for key in ("search_gen", "emb_gen"):
        db.bump(ct, key)
    maps = {"shots": m_shots, "files": m_files, "faces": m_faces, "people": m_people, "albums": m_albums,
            "keywords": m_kw, "decision_log": m_log, "groups": m_groups, "events": m_events, "clusters": m_clusters}
    # Rows this library had given to that one are back: they're no longer owed.
    if back:
        for t, m in maps.items():
            for o in m:
                back.get(t, {}).pop(o, None)
        _save_ledger(ct, ledger)
    return {**maps, "people_made": people_made}


def _target_roots(ct, cs, chk: dict) -> dict[str, int]:
    """Each moved folder's library folder in the other library: the one that already includes it (a
    move back), or a new one, known by the same drive. Library folders inside it are folded into it."""
    out = {}
    src_roots = cs.execute("SELECT * FROM roots ORDER BY length(path) DESC").fetchall()
    for f in chk["folders"]:
        mine = ct.execute("SELECT id, path FROM main.roots").fetchall()
        cover = next((r for r in mine if _inside(f, r["path"])), None)
        if cover:
            out[f] = cover["id"]
            continue
        sr = next((r for r in src_roots if _inside(f, r["path"])), None)
        rel = None
        if sr is not None and sr["volume_uuid"]:
            rest = f[len(sr["path"]):].lstrip(os.sep)
            rel = os.path.join(sr["rel_path"] or "", rest) if rest else (sr["rel_path"] or "")
        rid = ct.execute("INSERT INTO main.roots(path, added_at, volume_uuid, volume_name, rel_path) VALUES (?,?,?,?,?)",
                         (f, time.time(), sr["volume_uuid"] if sr is not None else None,
                          sr["volume_name"] if sr is not None else None, rel)).lastrowid
        for r in mine:
            if r["path"] != f and _inside(r["path"], f):
                ct.execute("UPDATE main.files SET root_id=? WHERE root_id=?", (rid, r["id"]))
                ct.execute("DELETE FROM main.roots WHERE id=?", (r["id"],))
        out[f] = rid
    return out


# What to compare after the copy (rows about the moved photos there and here) and what's removed after.
_VERIFY = [("shots", "id", "m_shots"), ("files", "id", "m_files"), ("faces", "id", "m_faces"),
           *[(t, "shot_id", "m_shots") for t in SIMPLE], ("comments", "shot_id", "m_shots"),
           ("decision_log", "shot_id", "m_shots"), ("backups", "file_id", "m_files")]


def _verify(ct) -> dict[str, int]:
    """Rows per table about the moved photos, in the source's copy and in this library: they must match."""
    out = {}
    for table, col, mp in _VERIFY:
        if not _has(ct, "s", table):
            continue
        there = ct.execute(f'SELECT COUNT(*) FROM s."{table}" WHERE {col} IN (SELECT old FROM temp.{mp})').fetchone()[0]
        here = ct.execute(f'SELECT COUNT(*) FROM main."{table}" WHERE {col} IN (SELECT new FROM temp.{mp})').fetchone()[0]
        if there != here:
            raise RuntimeError(f"The copy of {table} didn't come out right ({there:,} rows to copy, {here:,} copied). "
                               "Nothing was changed.")
        out[table] = here
    pairs = [
        ("shot_keywords", "SELECT COUNT(DISTINCT ms.new || ':' || mk.new) FROM s.shot_keywords x JOIN temp.m_shots ms ON ms.old = x.shot_id"
                          " JOIN temp.m_keywords mk ON mk.old = x.keyword_id",
         "SELECT COUNT(*) FROM main.shot_keywords WHERE shot_id IN (SELECT new FROM temp.m_shots)"),
        ("album_items", "SELECT COUNT(*) FROM s.album_items WHERE (shot_id IN (SELECT old FROM temp.m_shots) OR file_id IN"
                        " (SELECT old FROM temp.m_files)) AND album_id IN (SELECT old FROM temp.m_albums)",
         "SELECT COUNT(*) FROM main.album_items WHERE file_id IN (SELECT new FROM temp.m_files) OR shot_id IN (SELECT new FROM temp.m_shots)"),
    ]
    if _has(ct, "s", "search_fts") and _has(ct, "main", "search_fts"):
        pairs.append(("search_fts", "SELECT COUNT(*) FROM s.search_fts WHERE rowid IN (SELECT old FROM temp.m_shots)",
                      "SELECT COUNT(*) FROM main.search_fts WHERE rowid IN (SELECT new FROM temp.m_shots)"))
    for table, a, b in pairs:
        there, here = ct.execute(a).fetchone()[0], ct.execute(b).fetchone()[0]
        if here < there:
            raise RuntimeError(f"The copy of {table} didn't come out right ({there:,} rows to copy, {here:,} copied). "
                               "Nothing was changed.")
        out[table] = here
    return out


def _missing_thumbs(src: str, thumbs: dict[int, str], m_shots: dict[int, int]) -> list[int]:
    """The moved photos (their ids there) whose thumbnail isn't in the source library's own folder,
    where every thumbnail is made: they're read again at the other library's next update."""
    have = {i for _, _, i, rest, _ in _cached(config.project_dir(src), set(m_shots), set(), kinds=("thumbs",)) if rest == ".jpg"}
    return [m_shots[o] for o, path in thumbs.items() if path and o not in have]


def _move_cached(src: str, dst: str, maps: dict) -> dict:
    """The moved photos' thumbnails, small thumbnails, previews, zoom tiles and face crops go from the
    source library's folder to the other's, under their ids there: renamed, so nothing is copied or
    made again. Only files inside the source library's own folder move. It can run again after a stop
    (what moved already isn't there to move). Returns {moved, failed: shot ids whose thumbnail didn't
    arrive}."""
    m_shots, m_faces = maps.get("shots", {}), maps.get("faces", {})
    src_dir, dst_dir = config.project_dir(src), str(config.project_dir(dst))
    made: set[str] = set()
    moved, failed = 0, []
    for e, kind, i, rest, is_dir in _cached(src_dir, set(m_shots), set(m_faces)):
        new = (m_faces if kind == "previews/faces" else m_shots)[i]
        folder = os.path.join(dst_dir, kind, f"{new % 100:02d}")
        if folder not in made:
            os.makedirs(folder, exist_ok=True)
            made.add(folder)
        dest = os.path.join(folder, f"{new}{rest}")
        try:
            try:
                os.rename(e.path, dest)
            except OSError:
                if is_dir and os.path.isdir(dest):      # tiles left by a move that stopped
                    shutil.rmtree(dest, ignore_errors=True)
                shutil.move(e.path, dest)               # (another disk: copied, then removed)
            moved += 1
        except OSError:
            if kind == "thumbs":
                failed.append(new)
    return {"moved": moved, "failed": failed}


def _thumbs_lost(dst: str, ids: list[int]) -> None:
    """Photos whose thumbnail couldn't be brought: read again at the library's next update."""
    if not ids:
        return
    conn = db.connect(dst, create=False)
    try:
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            conn.execute(f"UPDATE shots SET thumb=NULL, analyzed_fp=NULL WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        conn.commit()
    finally:
        conn.close()


def _remove_from_source(cs, chk: dict, moved: dict, maps: dict) -> dict:
    """Take the moved photos out of the library they came from (rows, never tables), inside cs's
    transaction, and leave their folders out of it from now on. It remembers which ids it gave away
    and what they are there (`maps`), so they come back as they were. Returns what went."""
    xs, xf, xc = "SELECT id FROM temp.x_shots", "SELECT id FROM temp.x_files", "SELECT id FROM temp.x_faces"
    tables = {r[0] for r in cs.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    whole_groups = [r[0] for r in cs.execute(
        f"SELECT DISTINCT group_id FROM shots WHERE id IN ({xs}) AND group_id IS NOT NULL"
        f" EXCEPT SELECT DISTINCT group_id FROM shots WHERE id NOT IN ({xs}) AND group_id IS NOT NULL")]
    whole_events = [r[0] for r in cs.execute(
        f"SELECT DISTINCT event_id FROM shots WHERE id IN ({xs}) AND event_id IS NOT NULL"
        f" EXCEPT SELECT DISTINCT event_id FROM shots WHERE id NOT IN ({xs}) AND event_id IS NOT NULL")]
    whole_clusters = [r[0] for r in cs.execute(
        f"SELECT DISTINCT cluster FROM faces WHERE id IN ({xc}) AND cluster IS NOT NULL"
        f" EXCEPT SELECT DISTINCT cluster FROM faces WHERE id NOT IN ({xc}) AND cluster IS NOT NULL")]
    listed = set()
    for (people,) in cs.execute("SELECT people FROM shotlist WHERE people IS NOT NULL"):
        try:
            listed |= {int(p) for p in json.loads(people)}
        except (ValueError, TypeError):
            pass
    whole_people = [r[0] for r in cs.execute(
        f"SELECT DISTINCT person_id FROM faces WHERE id IN ({xc}) AND person_id > 0"
        f" EXCEPT SELECT DISTINCT person_id FROM faces WHERE id NOT IN ({xc}) AND person_id > 0") if r[0] not in listed]
    whole_albums = [r[0] for r in cs.execute(
        f"SELECT DISTINCT a.id FROM albums a JOIN album_items i ON i.album_id = a.id WHERE a.kind = 'manual'"
        f" AND (i.shot_id IN ({xs}) OR i.file_id IN ({xf}))"
        f" EXCEPT SELECT DISTINCT album_id FROM album_items WHERE NOT (shot_id IN ({xs}) OR file_id IN ({xf}))")]

    removed: dict[str, int] = {}

    def gone(table: str, where: str) -> None:
        if table in tables:
            removed[table] = removed.get(table, 0) + cs.execute(f'DELETE FROM "{table}" WHERE {where}').rowcount

    for t in (*SIMPLE, "comments", "shot_keywords", "decision_log"):
        gone(t, f"shot_id IN ({xs})")
    gone("backups", f"file_id IN ({xf}) OR shot_id IN ({xs})")
    gone("album_items", f"file_id IN ({xf}) OR shot_id IN ({xs})")
    gone("dup_members", f"file_id IN ({xf}) OR shot_id IN ({xs})")
    gone("faces", f"shot_id IN ({xs})")
    if "search_fts" in tables:
        removed["search_fts"] = cs.execute(f"DELETE FROM search_fts WHERE rowid IN ({xs})").rowcount
    gone("files", f"id IN ({xf})")
    gone("shots", f"id IN ({xs})")

    def ids(v: list[int]) -> str:
        return ",".join(str(int(i)) for i in v) or "NULL"

    gone("groups", f"id IN ({ids(whole_groups)})")
    whole_trips = [r[0] for r in cs.execute(
        f"SELECT id FROM events WHERE kind = 'trip' AND id IN (SELECT parent_id FROM events WHERE id IN ({ids(whole_events)}))"
        f" AND id NOT IN (SELECT parent_id FROM events WHERE parent_id IS NOT NULL AND id NOT IN ({ids(whole_events)}))")]
    gone("events", f"id IN ({ids(whole_events + whole_trips)})")
    gone("face_groups", f"cluster IN ({ids(whole_clusters)})")
    gone("face_not_same", f"cluster IN ({ids(whole_clusters)})")
    gone("people", f"id IN ({ids(whole_people)})")          # their other names go with them (a trigger)
    gone("albums", f"id IN ({ids(whole_albums)})")

    # The folders: left out from now on (or no longer one of its folders at all).
    roots = {r["path"]: r["id"] for r in cs.execute("SELECT id, path FROM roots")}
    now = time.time()
    for f in chk["folders"]:
        cs.execute("DELETE FROM excluded_folders WHERE folder != ? AND substr(folder, 1, ?) = ?",
                   (f, len(f) + 1, f.rstrip(os.sep) + os.sep))
        if f in roots:
            cs.execute("DELETE FROM roots WHERE id=?", (roots[f],))
        else:
            cs.execute("INSERT INTO excluded_folders(folder, library, added, ino) VALUES (?,?,?,?) ON CONFLICT(folder)"
                       " DO UPDATE SET library=excluded.library, added=excluded.added, ino=excluded.ino",
                       (f, chk["dst"], now, _ino(f)))
    led = _ledger(cs)
    owed = led.setdefault(chk["dst"], {})
    for t in ("shots", "files", "faces", "decision_log", "groups", "events", "clusters", "people", "albums"):
        for o, n in (maps.get(t) or {}).items():
            owed.setdefault(t, {})[n] = o
    _save_ledger(cs, led)
    for key in ("search_gen", "emb_gen"):
        db.bump(cs, key)
    # Nothing about them may be left behind.
    for t, where in (("shots", f"id IN ({xs})"), ("files", f"id IN ({xf})"), ("faces", f"shot_id IN ({xs})"),
                     ("decision_log", f"shot_id IN ({xs})"), ("metrics", f"shot_id IN ({xs})")):
        if t in tables and cs.execute(f"SELECT COUNT(*) FROM {t} WHERE {where}").fetchone()[0]:
            raise RuntimeError(f"Some rows of {t} were left behind; nothing was changed.")
    return removed


# ---------------------------------------------------------------- the log of moves

def _save(op: dict) -> None:
    d = _log_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = d / f"{op['id']}.json"
    tmp = path.with_name(f".{op['id']}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(op, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _all_ops() -> list[dict]:
    d = _log_dir()
    out = []
    for f in d.glob("*.json") if d.is_dir() else []:
        try:
            op = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(op, dict) and op.get("id") and op.get("src") and op.get("dst"):
            out.append(op)
    return sorted(out, key=lambda o: o.get("ts", 0), reverse=True)


def load(opid: str) -> dict:
    for op in _all_ops():
        if op["id"] == opid:
            return op
    raise Refused("There's no move of folders between libraries with that id.")


def history(project: str | None = None) -> list[dict]:
    """Moves of folders between libraries, newest first (those of one library, given its name), each
    saying whether it can be moved back now."""
    libs = set(config.list_projects())
    out = []
    for op in _all_ops():
        if project and project not in (op["src"], op["dst"]):
            continue
        op = dict(op)
        op["can_move_back"] = (op.get("state") == "done" and not op.get("moved_back") and op["src"] in libs
                               and op["dst"] in libs)
        out.append(op)
    return out


def _each_catalog(fn) -> None:
    for name in config.list_projects():
        try:
            conn = db.connect(name, create=False)
        except Exception:
            continue
        try:
            fn(conn)
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
        finally:
            conn.close()


def rename_library(old: str, new: str) -> None:
    """A library was renamed: its moves, the folders other libraries left out for it and the ids they
    gave it follow."""
    for op in _all_ops():
        if old in (op["src"], op["dst"]):
            op["src"] = new if op["src"] == old else op["src"]
            op["dst"] = new if op["dst"] == old else op["dst"]
            _save(op)

    def fix(conn):
        conn.execute("UPDATE excluded_folders SET library=? WHERE library=?", (new, old))
        led = _ledger(conn)
        if old in led:
            led[new] = led.pop(old)
            _save_ledger(conn, led)

    _each_catalog(fix)


def forget_library(name: str) -> None:
    """A library was removed: ids other libraries gave it can never come back from it (a library made
    later under the same name is another library)."""
    def fix(conn):
        led = _ledger(conn)
        if name in led:
            del led[name]
            _save_ledger(conn, led)

    _each_catalog(fix)


# ---------------------------------------------------------------- the move

def run(src: str, folders: list[str], dst: str, purpose: str | None = None, *, back_of: str | None = None,
        lock_wait: float = 0.0, on_wait: Callable[[], None] | None = None,
        progress: Callable[[str], None] | None = None) -> dict:
    """Move the folders (and everything known about their photos) from `src` to `dst`: an existing
    library, or the name of a new one (with `purpose`). See the module's notes for how it stays safe.
    Returns what was done, with the backups and timings. Raises Refused (nothing done) with the reason."""
    say = progress or (lambda m: None)
    chk = check(src, folders, dst, purpose)
    dst, new = chk["dst"], chk["new"]
    opid = uuid.uuid4().hex[:10]
    t0 = time.time()
    times: dict[str, float] = {}

    def lap(key: str, since: float) -> float:
        now = time.time()
        times[key] = round(now - since, 2)
        return now

    if on_wait and (locks.is_busy(src) or (not new and locks.is_busy(dst))):
        on_wait()
    with locks.project_lock(src, wait=lock_wait), (nullcontext() if new else locks.project_lock(dst, wait=lock_wait)):
        chk = check(src, folders, dst, purpose)                  # again, now nothing else can change them
        op = {"id": opid, "ts": time.time(), "src": src, "dst": dst, "new": new, "purpose": chk["purpose"],
              "folders": chk["folders"], "state": "copying", "back_of": back_of}
        _save(op)
        stage = _staging() / opid
        final_dir = config.project_dir(dst)
        cs = db.connect(src, create=False)
        ct = None
        renamed = committing = False
        try:
            say("Finding what goes")
            cs.execute("BEGIN IMMEDIATE")                       # nothing changes the source until the move is over
            t = time.time()
            moved = _moved(cs, chk["folders"])
            thumbs = dict(cs.execute("SELECT id, thumb FROM shots WHERE id IN (SELECT id FROM temp.x_shots)").fetchall())
            file_shot = cs.execute("SELECT id, shot_id FROM files WHERE id IN (SELECT id FROM temp.x_files)").fetchall()
            t = lap("find", t)
            say("Backing up both catalogs")
            src_db = Path(cs.execute("PRAGMA database_list").fetchone()[2])
            op["backups"] = {src: str(_backup(src_db, src, opid))}
            if not new:
                op["backups"][dst] = str(_backup(final_dir / "catalog.db", dst, opid))
            _save(op)
            t = lap("backup", t)
            say(f"Copying {_plural(len(moved['shots']), 'photo')} to “{dst}”")
            if new:
                if stage.exists():
                    shutil.rmtree(stage)
                stage.mkdir(mode=0o700, parents=True)
                ct = db.connect_path(stage / "catalog.db", dst)
            else:
                ct = db.connect(dst, create=False)
            if ct.in_transaction:
                ct.commit()
            ct.execute("ATTACH DATABASE ? AS s", (op["backups"][src],))
            _schema_from(ct)                                     # only adds what it lacks
            ct.commit()
            ct.execute("BEGIN IMMEDIATE")
            # The backup is the source as it is now (its writes are held), so ids found there are the same.
            for tname in ("x_shots", "x_files", "x_faces"):
                ct.execute(f"CREATE TEMP TABLE IF NOT EXISTS {tname} (id INTEGER PRIMARY KEY)")
            ct.executemany("INSERT INTO temp.x_shots(id) VALUES (?)", [(i,) for i in moved["shots"]])
            ct.executemany("INSERT INTO temp.x_files(id) VALUES (?)", [(i,) for i in moved["files"]])
            ct.executemany("INSERT INTO temp.x_faces(id) VALUES (?)", [(i,) for i in moved["faces"]])
            maps = _write_target(ct, cs, chk, moved, final_dir, opid)
            t = lap("copy", t)
            say("Checking the copy")
            counts = _verify(ct)
            t = lap("verify", t)
            lost = _missing_thumbs(src, thumbs, maps["shots"])
            for i in range(0, len(lost), 500):              # read again at its next update
                chunk = lost[i : i + 500]
                ct.execute(f"UPDATE main.shots SET thumb=NULL, analyzed_fp=NULL WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            # What finishing needs, written down before the other library commits: a stop from here on may
            # leave it with its copy (`finish` then sees which).
            op["maps"] = {k: _runs(v) for k, v in maps.items() if isinstance(v, dict) and v}
            _save(op)
            committing = True
            ct.commit()
            ct.execute("DETACH DATABASE s")
            if new:
                if ct.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("The new library's catalog failed SQLite's check. Nothing was changed.")
                ct.close()
                ct = None
                if final_dir.exists():
                    raise Refused(f"A library called “{dst}” appeared meanwhile. Nothing was changed.")
                os.rename(stage, final_dir)
                renamed = True
            else:
                ct.close()
                ct = None
            op["state"] = "copied"
            _save(op)
            t = lap("commit", t)
            say(f"Removing them from “{src}”")
            removed = _remove_from_source(cs, chk, moved, maps)
            cs.commit()
            op["state"] = "removed"
            _save(op)
            t = lap("remove", t)
        except BaseException:
            if cs.in_transaction:
                cs.rollback()
            if ct is not None:
                try:
                    if ct.in_transaction:
                        ct.rollback()
                    ct.close()
                except sqlite3.Error:
                    pass
            if op["state"] == "copying":
                if committing and _landed(op):     # the other library has its copy: `finish` takes it from here
                    op["state"] = "copied"
                else:                              # nothing was committed anywhere: the source is as it was
                    if new and not renamed:
                        shutil.rmtree(stage, ignore_errors=True)
                    op["state"] = "failed"
                    op.pop("maps", None)
                _save(op)
            raise
        finally:
            cs.close()
        # The catalogs are done; now the images follow (renamed, so a stop here loses nothing: --finish
        # moves the rest).
        say("Moving thumbnails and previews")
        images = _move_cached(src, dst, maps)
        _thumbs_lost(dst, images["failed"])
        t = lap("images", t)
        try:
            from . import copies

            copies.move_rows(src, dst, {fid: (maps["files"][fid], maps["shots"].get(sid)) for fid, sid in file_shot})
        except Exception:                 # the index is derived: both libraries' next refresh puts it right
            pass
        t = lap("tidy", t)
    op.update(state="done", finished=time.time(), seconds=round(time.time() - t0, 2), times=times,
              counts={"photos": len(moved["shots"]), "files": len(moved["files"]), "faces": len(moved["faces"]),
                      "people": len(maps["people"]), "albums": len(maps["albums"]), "keywords": len(maps["keywords"]),
                      "history": len(maps["decision_log"]), "rows": counts, "removed": removed,
                      "images": {"moved": images["moved"], "lost": len(lost) + len(images["failed"])}},
              ids_kept=sum(1 for o, n in maps["shots"].items() if o == n))
    op.pop("maps", None)                       # the libraries' ledgers keep them now
    _save(op)
    return op


def move_back(opid: str, *, lock_wait: float = 0.0, on_wait=None, progress=None) -> dict:
    """Move the folders of an earlier move back where they came from (the same operation in reverse).
    A library the move made is removed once it's empty again (its catalog is kept in backups/removed)."""
    op = load(opid)
    if op.get("moved_back"):
        raise Refused("Those folders were moved back already.")
    if op.get("state") != "done":
        raise Refused("That move didn't finish, so there's nothing to move back.")
    res = run(op["dst"], op["folders"], op["src"], back_of=opid, lock_wait=lock_wait, on_wait=on_wait, progress=progress)
    op["moved_back"] = res["id"]
    _save(op)
    if op.get("new") and op["dst"] in config.list_projects():
        conn = db.connect(op["dst"], create=False)
        try:
            empty = not conn.execute("SELECT 1 FROM shots LIMIT 1").fetchone() and not conn.execute("SELECT 1 FROM roots LIMIT 1").fetchone()
        finally:
            conn.close()
        if empty:
            try:
                res["removed_library"] = libraries.remove(op["dst"], op["dst"])
                res["removed_library"]["name"] = op["dst"]
                _save({**res})
            except (locks.Busy, OSError, ValueError):
                pass                         # left, empty: it can be removed in its Settings
    return res


def include_again(project: str, folder: str) -> dict:
    """Stop leaving a folder out of this library, when no other library includes it: its photos are
    found at the next update (read again, since nothing about them is in this catalog)."""
    conn = db.connect(project, create=False)
    try:
        row = conn.execute("SELECT folder, library FROM excluded_folders WHERE folder=?", (folder,)).fetchone()
        if not row:
            raise Refused(f"“{project}” doesn't leave {folder} out.")
        h = libraries.holder(folder, libraries.claims(skip=(project,)))
        if h:
            raise Refused(f"{folder} is in the library “{h[0]}” now. Move it back from there instead "
                          f"(Settings › Library in “{h[0]}”), which brings its decisions, names and history with it.")
        conn.execute("DELETE FROM excluded_folders WHERE folder=?", (folder,))
        conn.commit()
    finally:
        conn.close()
    return {"folder": folder, "library": row["library"]}


def _landed(op: dict) -> bool:
    """Whether the other library has its copy of a move's photos: it includes every one of the folders
    now (before the move it included none of them)."""
    if op["dst"] not in config.list_projects():
        return False
    theirs = libraries.claims().get(op["dst"])
    return theirs is not None and all(
        any(_inside(f, r) for r in theirs["roots"]) and not any(_inside(f, e) for e in theirs["excluded"])
        for f in op["folders"])


# What you make about photos (beside their files and analysis), compared when a stopped move is
# finished: (table, the columns compared, which rows). Faces only by the names you gave them.
_YOURS = [*((t, None, "shot_id IN ({xs})") for t in db.MADE_PER_PHOTO),
          ("comments", None, "shot_id IN ({xs})"), ("shot_keywords", None, "shot_id IN ({xs})"),
          ("decision_log", None, "shot_id IN ({xs})"), ("album_items", None, "shot_id IN ({xs}) OR file_id IN ({xf})"),
          ("faces", ("id", "shot_id", "person_id"), "shot_id IN ({xs}) AND person_src = 'user'")]


def _changed_since(cs, backup: str, folders: list[str], moved: dict) -> set:
    """The moved photos (cs's x_ tables) whose decisions, keywords, dates, favourites, comments, album
    places, face names or history differ from the copy of the source taken when the move began (`b`).
    Empty when none did. Photos under the folders now that weren't then, or the other way round, count
    as changed too."""
    then = sqlite3.connect(backup)
    try:
        before = _moved(then, folders)
    finally:
        then.close()
    if before["shots"] != moved["shots"] or before["files"] != moved["files"]:
        return set(before["shots"]) ^ set(moved["shots"]) or {None}
    xs, xf = "SELECT id FROM temp.x_shots", "SELECT id FROM temp.x_files"
    out: set = set()
    for table, cols, where in _YOURS:
        where = where.format(xs=xs, xf=xf)
        here, there = _has(cs, "main", table), _has(cs, "b", table)
        if here and there:
            have = set(_cols(cs, "b", table))
            sel = ", ".join(f'"{c}"' for c in (cols or _cols(cs, "main", table)) if c in have)
            for a, b in (("main", "b"), ("b", "main")):
                out |= {r["shot_id"] for r in cs.execute(
                    f'SELECT {sel} FROM {a}."{table}" WHERE {where} EXCEPT SELECT {sel} FROM {b}."{table}" WHERE {where}')}
        elif here or there:
            out |= {r[0] for r in cs.execute(f'SELECT shot_id FROM {"main" if here else "b"}."{table}" WHERE {where}')}
    return out


def _changed_refusal(op: dict, changed: set) -> str:
    n = len(changed - {None})
    who = "one of those photos has" if n == 1 else f"{n:,} of those photos have" if n else "some of those photos have"
    if op.get("new"):
        out = (f"This move made “{op['dst']}”, so remove that library (its Settings › Library › Remove this "
               "library)")
    else:
        out = (f"Bring back the copy of its catalog taken just before the move (joeylab restore -p {op['dst']} "
               f"--from {op.get('backups', {}).get(op['dst'], '…')}); that also undoes anything done in “{op['dst']}” since")
    return (f"Since this move stopped, {who} changed in “{op['src']}” (decisions, keywords, dates, names or "
            f"history), and “{op['dst']}” has them as they were before. Finishing it now would lose those changes, "
            f"so nothing was removed. To keep them, take that copy out of “{op['dst']}” first. {out}. Then finish "
            "this move again: it closes with nothing moved, and the folders can be moved again.")


def _gone_already(cs, op: dict, moved: dict) -> bool:
    """The photos left the source already: the move stopped just after it committed that."""
    if moved["shots"] or moved["files"]:
        return False
    roots = {r[0] for r in cs.execute("SELECT path FROM roots")}
    left = {r["folder"]: r["library"] for r in _excluded_rows(cs)}
    return all(left.get(f) == op["dst"] or not any(_inside(f, r) for r in roots) for f in op["folders"])


def finish(opid: str) -> dict:
    """Finish a move that stopped. After the other library had its copy but before the photos left the
    one they came from, both have them: the copy there is complete and checked, so they're removed
    here, as the move would have, provided nothing about them changed here since the move began
    (compared with the copy of this catalog taken then): otherwise it's refused, since finishing would
    lose that work, and says how to keep it. When the other library doesn't have the copy (the move
    stopped before it was committed, or the copy was taken out of it since), the move closes with
    nothing moved. After the photos left, only some of their thumbnails and previews may have
    followed: the rest do now."""
    op = load(opid)
    if op.get("state") not in ("copying", "copied", "removed"):
        raise Refused("That move isn't waiting to be finished.")
    src, dst = op["src"], op["dst"]
    if op["state"] != "removed" and not _landed(op):
        if op.get("new"):
            shutil.rmtree(_staging() / op["id"], ignore_errors=True)
        op["state"] = "failed"              # the source has the photos as they are; nothing moved
        op.pop("maps", None)
        _save(op)
        return op
    if op["state"] == "copying":
        if "maps" not in op:
            raise Refused("That move's log doesn't say where its photos went, so it can't be finished. Nothing was changed.")
        op["state"] = "copied"              # it stopped just after the other library committed its copy
        _save(op)
    maps: dict[str, dict[int, int]] = {}
    for t, runs in (op.get("maps") or {}).items():
        maps[t] = {}
        for a, b, n in runs:
            maps[t].update(zip(range(a, a + n), range(b, b + n)))
    with locks.project_lock(src), locks.project_lock(dst):
        if op["state"] == "copied":
            backup = (op.get("backups") or {}).get(src)
            if not backup or not os.path.isfile(backup):
                raise Refused(f"The copy of “{src}” taken when the move began isn't there any more, so whether those "
                              "photos changed since can't be told. Nothing was removed.")
            cs = db.connect(src, create=False)
            try:
                cs.execute("ATTACH DATABASE ? AS b", (backup,))
                cs.execute("BEGIN IMMEDIATE")
                moved = _moved(cs, op["folders"])
                if not _gone_already(cs, op, moved):
                    ct = db.connect(dst, create=False)
                    try:
                        have = {r[0] for r in ct.execute("SELECT path FROM files")}
                    finally:
                        ct.close()
                    paths = [r[0] for r in cs.execute("SELECT path FROM files WHERE id IN (SELECT id FROM temp.x_files)")]
                    if any(p not in have for p in paths):
                        raise Refused(f"Some of those photos aren't in “{dst}”: “{src}” has changed since. Nothing was removed.")
                    changed = _changed_since(cs, backup, op["folders"], moved)
                    if changed:
                        raise Refused(_changed_refusal(op, changed))
                    _remove_from_source(cs, {"folders": op["folders"], "dst": dst}, moved, maps)
                cs.commit()
            except BaseException:
                if cs.in_transaction:
                    cs.rollback()
                raise
            finally:
                cs.close()
            op["state"] = "removed"
            _save(op)
        _thumbs_lost(dst, _move_cached(src, dst, maps)["failed"])
    op["state"] = "done"
    op["finished"] = time.time()
    op.pop("maps", None)
    _save(op)
    return op


# ---------------------------------------------------------------- afterwards

def recompute(conn, project: str, rep=None) -> dict:
    """After a move, in one library: duplicates, stacks, selections and scores; its rows in the index of
    backups; people, taste, places, events and trips; face groups' agreement; and its search cache
    rebuilt. Catalog only, as a normal update's grouping does. Returns seconds per step."""
    from . import faces, pipeline, search

    rep = rep or pipeline.Reporter()
    out: dict[str, float] = {}
    t = time.time()
    pipeline.organize(conn, rep, project)
    out["organise"] = round(time.time() - t, 2)
    t = time.time()
    if conn.execute("SELECT 1 FROM faces WHERE cluster IS NOT NULL LIMIT 1").fetchone():
        faces._cohesion(conn)
    out["faces"] = round(time.time() - t, 2)
    t = time.time()
    try:
        if conn.execute("SELECT 1 FROM search_vecs LIMIT 1").fetchone():
            search._matrix(conn)
    except (sqlite3.Error, OSError, ValueError, ImportError):
        pass
    out["search"] = round(time.time() - t, 2)
    return out
