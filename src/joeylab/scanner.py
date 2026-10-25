"""Incremental, read-only inventory of photo folders.

Each scan reconciles the catalog with what is on disk:
  * new files are added,
  * files whose size/mtime changed are re-fingerprinted and re-analysed,
  * files that disappeared are matched against new paths, so a move or rename keeps its
    ratings and analysis: first without reading anything (the same inode, size and modified
    time on the same drive; or the same name, size and modified time in the folder the others
    went to, as when a folder is renamed), then by content fingerprint,
  * anything left over is marked missing (never forgotten automatically, so
    decisions survive a disk being unplugged or a folder being moved back),
  * roots that are not mounted/available are skipped rather than emptied, and so are
    folders that couldn't be read (a permission prompt, a flaky USB cable).

Files are only ever opened for reading. Fingerprints are computed before the catalog is
locked for writing, so the web app stays responsive while a card is imported.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import dating, db, exif
from .config import HOME, SETTLE_SECONDS, kind_for
from .hashing import quick_fp

PREVIEW_PREFERENCE = {"jpeg": 0, "other": 1, "raw": 2, "video": 3}
META_ORDER = {"raw": 0, "jpeg": 1, "other": 2, "video": 3}
PAIR_SECONDS = 2.0      # a RAW and a JPEG with one name belong together when shot within this
WRITE_BATCH = 500


@dataclass
class ScanReport:
    roots_scanned: list[str] = field(default_factory=list)
    roots_unavailable: list[str] = field(default_factory=list)
    roots_moved: list[dict] = field(default_factory=list)        # a drive came back under a new name
    roots_wrong_drive: list[str] = field(default_factory=list)   # another drive has the old name
    unreadable: list[str] = field(default_factory=list)
    seen: int = 0
    added: int = 0
    changed: int = 0
    moved: int = 0
    returned: int = 0
    missing: int = 0
    settling: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    shots_present: int = 0
    # Every tracked file and folder as found, for the "untouched" check (not stored in the summary).
    stats: dict = field(default_factory=dict, repr=False)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "stats"}
        d["settling"] = len(self.settling)
        d["errors"] = len(self.errors)
        d["unreadable"] = len(self.unreadable)
        return d


def add_root(conn, path: Path) -> tuple[int, str | None]:
    """Register a folder. Returns (root_id, note)."""
    raw = str(path).strip()
    if not raw or raw == "." or not (raw.startswith("/") or raw.startswith("~")):
        raise ValueError("Give the folder's full path, for example /Volumes/Photos or ~/Pictures/2024.")
    p = str(Path(raw).expanduser().resolve())
    if not os.path.isdir(p):
        raise NotADirectoryError(p)
    if p == str(HOME) or p.startswith(str(HOME) + os.sep):
        raise ValueError("refusing to scan JoeyLab's own data directory")
    from . import export, volumes

    for ex in db.excluded_folders(conn):
        if export.within(Path(p), ex):
            raise ValueError(f"{p} was moved to another library, so this one leaves it out. To have it here again, "
                             "include it again under Settings › Folders (or move it back from the other library).")
    # The same folder (or one inside it) can be spelled differently: macOS ignores case, and
    # accented names come in two encodings. Compared by the file system, it's caught either way.
    for r in conn.execute("SELECT id, path FROM roots"):
        if p == r["path"]:
            return r["id"], None
        if export.within(Path(p), r["path"]):
            try:
                if os.path.samefile(p, r["path"]):
                    return r["id"], None
            except OSError:
                pass
            return r["id"], f"already covered by root {r['path']}"

    vol = volumes.identify(p) or {}
    cur = conn.execute("INSERT INTO roots(path, added_at, volume_uuid, volume_name, rel_path) VALUES (?,?,?,?,?)",
                       (p, time.time(), vol.get("uuid"), vol.get("name"), vol.get("rel")))
    # A new root that contains existing roots supersedes them.
    for r in conn.execute("SELECT id, path FROM roots WHERE id != ?", (cur.lastrowid,)).fetchall():
        if export.within(Path(r["path"]), p):
            conn.execute("UPDATE files SET root_id=? WHERE root_id=?", (cur.lastrowid, r["id"]))
            conn.execute("DELETE FROM roots WHERE id=?", (r["id"],))
    conn.commit()
    return cur.lastrowid, None


def _stat_key(st: os.stat_result) -> tuple:
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


def _walk(root: str, root_id: int, now: float, report: ScanReport, on_disk: dict, settling: set,
          excluded: frozenset = frozenset()):
    home = str(HOME)

    def failed(err: OSError) -> None:
        report.unreadable.append(err.filename or root)
        report.errors.append(f"{err.filename}: couldn't read this folder ({err.strerror})")

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=failed):
        try:
            report.stats[dirpath] = _stat_key(os.lstat(dirpath))
        except OSError:
            pass
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and os.path.join(dirpath, d) != home
            and os.path.join(dirpath, d) not in excluded           # moved to another library (split.py)
        )
        for name in filenames:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1]
            kind = kind_for(ext)
            if kind is None:
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError as e:
                # Listed but not readable (a drive hiccup): treat the folder as unreadable this time,
                # so nothing in it is marked missing.
                report.errors.append(f"{path}: {e}")
                if dirpath not in report.unreadable:
                    report.unreadable.append(dirpath)
                continue
            if not os.path.isfile(path) or os.path.islink(path) or st.st_size == 0:
                continue
            report.stats[path] = _stat_key(st)
            if 0 <= now - st.st_mtime < SETTLE_SECONDS:     # a camera clock ahead isn't "still copying"
                settling.add(path)
                report.settling.append(path)
                continue
            on_disk[path] = (root_id, st.st_size, st.st_mtime_ns, ext.lower(), kind, st.st_dev, db.to_i64(st.st_ino))


def _under(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


SPOT_CHECKS = 2         # files read per pair of folders matched by name, to be sure


def _moved_without_reading(vanished: list, new_paths: list[str], on_disk: dict, root_dev: dict[int, int]) -> dict:
    """Vanished files found again at new paths, with (almost) nothing read: {new path: catalog row}.

    1. The same file: on the drive its library folder is on, the same inode, size and modified time.
       A move or rename on one drive (in Finder, or by another app) keeps all three.
    2. Files that left one folder for another together, as when a folder is renamed or moved, or on
       a drive that gives a moved file a new inode: the same name, size and modified time, in the
       folder most of its neighbours went to. A couple of files of each pair of folders are read to
       be sure; if their content differs, the pair is left to the content check.
    Anything ambiguous (two places as likely as each other: a file copied twice, say) is left to the
    content check too. Only vanished files are candidates, so a copy never takes over from a file
    that is still where it was."""
    found: dict[str, object] = {}
    # Only inode numbers that mean something: some drives make them up (exFAT under macOS's FSKit
    # numbers files down from 2^64, and may number them again), so those, and any number two files
    # share, are left to the next checks.
    by_ino: dict[tuple, list] = defaultdict(list)
    for r in vanished:
        dev = root_dev.get(r["root_id"])
        if r["ino"] is not None and r["ino"] > 0 and dev is not None:
            by_ino[(dev, r["ino"])].append(r)
    shared = Counter((on_disk[p][5], on_disk[p][6]) for p in new_paths)
    taken: set[int] = set()
    if by_ino:
        for p in new_paths:
            _, size, mtime_ns, _, _, dev, ino = on_disk[p]
            rows = by_ino.get((dev, ino), ())
            if len(rows) != 1 or shared[(dev, ino)] != 1:
                continue
            r = rows[0]
            if r["id"] not in taken and r["size"] == size and r["mtime_ns"] == mtime_ns:
                found[p] = r
                taken.add(r["id"])

    rest = [p for p in new_paths if p not in found]
    left = [r for r in vanished if r["id"] not in taken]
    if not rest or not left:
        return found
    by_key: dict[tuple, list[str]] = defaultdict(list)
    for p in rest:
        by_key[(os.path.basename(p), on_disk[p][1], on_disk[p][2])].append(p)
    cands, votes = [], Counter()
    for r in left:
        ps = by_key.get((os.path.basename(r["path"]), r["size"], r["mtime_ns"]))
        if ps:
            cands.append((r, ps))
            for p in ps:
                votes[(os.path.dirname(r["path"]), os.path.dirname(p))] += 1
    wanted: dict[str, list] = defaultdict(list)       # new path -> [(votes, row)]
    for r, ps in cands:
        src = os.path.dirname(r["path"])
        ranked = sorted(((votes[(src, os.path.dirname(p))], p) for p in ps), reverse=True)
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            continue
        wanted[ranked[0][1]].append((ranked[0][0], r))
    pairs: dict[tuple, list] = defaultdict(list)
    for p, rs in wanted.items():
        rs.sort(key=lambda x: -x[0])
        if len(rs) > 1 and rs[0][0] == rs[1][0]:
            continue
        pairs[(os.path.dirname(rs[0][1]["path"]), os.path.dirname(p))].append((p, rs[0][1]))
    for matches in pairs.values():
        matches.sort(key=lambda m: m[0])
        sample = matches if len(matches) <= SPOT_CHECKS else [matches[0], matches[-1]]
        try:
            same = all(quick_fp(p, r["size"]) == r["fp"] for p, r in sample)
        except OSError:
            same = False
        if same:
            found.update(matches)
    return found


def scan(conn, progress: Callable[[str, int, int], None] | None = None) -> ScanReport:
    report = ScanReport()
    now = time.time()
    scan_id = conn.execute("INSERT INTO scans(started) VALUES (?)", (now,)).lastrowid
    conn.commit()
    on_disk: dict[str, tuple] = {}
    settling: set[str] = set()

    from . import volumes

    drives = volumes.check_roots(conn)          # follow renamed drives; never scan a stranger's
    report.roots_moved = drives["moved"]
    report.roots_wrong_drive = drives["wrong_drive"]
    roots = conn.execute("SELECT id, path, ino FROM roots").fetchall()
    # Folders this library leaves out (they were moved to another library): never walked, and what
    # the catalog may still hold under them is neither "seen" nor "missing".
    excluded = frozenset(db.excluded_folders(conn))
    root_dev: dict[int, int] = {}          # the drive each scanned root is on now
    for r in roots:
        if r["path"] in excluded:
            continue
        if r["id"] in drives["hold"]:
            report.roots_unavailable.append(r["path"])
        elif os.path.isdir(r["path"]) and os.access(r["path"], os.R_OK | os.X_OK):
            if progress:
                progress(f"walking {r['path']}", 0, 0)
            before = len(report.unreadable)
            _walk(r["path"], r["id"], now, report, on_disk, settling, excluded)
            # Gone by the end of the walk (unplugged mid-scan)? Then nothing under it is "missing".
            if r["path"] in report.unreadable[before:] or not os.path.isdir(r["path"]):
                report.roots_unavailable.append(r["path"])
            else:
                report.roots_scanned.append(r["path"])
                try:
                    st = os.stat(r["path"])
                except OSError:
                    continue
                root_dev[r["id"]] = st.st_dev
                if r["ino"] != db.to_i64(st.st_ino):          # so a rename of the folder itself is followed
                    conn.execute("UPDATE roots SET ino=? WHERE id=?", (db.to_i64(st.st_ino), r["id"]))
                    conn.commit()
        else:
            report.roots_unavailable.append(r["path"])
    report.seen = len(on_disk)

    # ---- phase 1: work out every change, reading files but not writing the catalog
    db_files = {row["path"]: row for row in conn.execute(
        "SELECT id, path, root_id, size, mtime_ns, fp, status, ino FROM files")}
    updates: list[tuple] = []        # (sql, args)
    vanished = []
    for path, row in db_files.items():
        if path in settling:
            continue
        if path in on_disk:
            _, size, mtime_ns, _, _, _, ino = on_disk[path]
            if row["ino"] != ino:            # recorded for the first time, or the file was replaced
                updates.append(("UPDATE files SET ino=? WHERE id=?", (ino, row["id"])))
            if row["status"] == "missing":
                report.returned += 1
            if size != row["size"] or mtime_ns != row["mtime_ns"]:
                try:
                    fp = quick_fp(path, size)
                except OSError as e:
                    report.errors.append(f"{path}: {e}")
                    continue
                if fp != row["fp"]:
                    report.changed += 1
                    updates.append((
                        "UPDATE files SET size=?, mtime_ns=?, fp=?, full_hash=NULL, exif_done=0,"
                        " status='present', last_seen=? WHERE id=?", (size, mtime_ns, fp, scan_id, row["id"])))
                    continue
                # Same first and last bytes, but the file was written again: its full hash may be out of
                # date (duplicates are only ever decided on a fresh one).
                updates.append(("UPDATE files SET size=?, mtime_ns=?, full_hash=NULL, status='present', last_seen=? WHERE id=?",
                                (size, mtime_ns, scan_id, row["id"])))
            elif row["status"] == "missing":
                updates.append(("UPDATE files SET status='present', last_seen=? WHERE id=?", (scan_id, row["id"])))
        elif (_under(path, report.roots_scanned) and not _under(path, report.unreadable)
              and not _under(path, excluded)) or row["status"] == "missing":
            vanished.append(row)

    # Vanished files found again at new paths: moved or renamed. First without reading anything,
    # then by content fingerprint.
    new_paths = [p for p in on_disk if p not in db_files]
    found = _moved_without_reading(vanished, new_paths, on_disk, root_dev) if vanished and new_paths else {}
    taken = {r["id"] for r in found.values()}
    pool: dict[str, list] = defaultdict(list)
    for row in vanished:
        if row["id"] not in taken:
            pool[row["fp"]].append(row)
    inserts: list[tuple] = []
    for i, path in enumerate(new_paths):
        if progress and i % 25 == 0:
            progress("fingerprinting new files", i, len(new_paths))
        root_id, size, mtime_ns, ext, kind, _, ino = on_disk[path]
        old = found.get(path)
        if old is None:
            try:
                fp = quick_fp(path, size)
            except OSError as e:
                report.errors.append(f"{path}: {e}")
                continue
            if pool.get(fp):
                # Identical copies: the one with this file's name is the one that moved.
                name = os.path.basename(path)
                k = next((j for j, r in enumerate(pool[fp]) if os.path.basename(r["path"]) == name), 0)
                old = pool[fp].pop(k)
        if old is not None:
            report.moved += 1
            updates.append(("UPDATE files SET path=?, root_id=?, size=?, mtime_ns=?, ino=?, status='present', last_seen=?"
                            " WHERE id=?", (path, root_id, size, mtime_ns, ino, scan_id, old["id"])))
        else:
            report.added += 1
            inserts.append((root_id, path, kind, ext, size, mtime_ns, fp, scan_id, ino))
    for rows in pool.values():
        for row in rows:
            if row["status"] != "missing":
                report.missing += 1
                updates.append(("UPDATE files SET status='missing' WHERE id=?", (row["id"],)))

    # ---- phase 2: write, in short transactions
    for i in range(0, len(updates), WRITE_BATCH):
        for sql, args in updates[i : i + WRITE_BATCH]:
            conn.execute(sql, args)
        conn.commit()
    for i in range(0, len(inserts), WRITE_BATCH):
        for ins in inserts[i : i + WRITE_BATCH]:
            conn.execute(
                "INSERT INTO files(id, root_id, path, kind, ext, size, mtime_ns, fp, status, last_seen, ino)"
                " VALUES (?,?,?,?,?,?,?,?, 'present', ?, ?)", (db.new_id(conn, "files"), *ins))
        conn.commit()

    touched = _read_file_meta(conn, progress)
    touched |= _pair_shots(conn)
    _summarize_shots(conn, touched)

    report.shots_present = conn.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0]
    conn.execute(
        "UPDATE scans SET finished=?, summary=? WHERE id=?",
        (time.time(), json.dumps(report.as_dict()), scan_id),
    )
    conn.commit()
    return report


# ---------------------------------------------------------------- metadata per file

META_CHUNK = 2000             # files read and saved at a time: progress shows, and a stop loses at most this many


def _read_file_meta(conn, progress) -> set[int]:
    """Read EXIF (and any .xmp sidecar) for files not read yet, a chunk at a time: each chunk is saved
    before the next is read, so the count moves and nothing read is lost if the update stops. Returns
    the shots touched."""
    rows = conn.execute(
        "SELECT id, path, kind, shot_id FROM files WHERE status='present' AND exif_done=0").fetchall()
    if not rows or not exif.available():
        return set()
    touched: set[int] = set()
    for start in range(0, len(rows), META_CHUNK):
        if progress:
            progress(f"reading metadata for {len(rows):,} files", start, len(rows))
        chunk = rows[start : start + META_CHUNK]
        tags, done = exif.read_many([r["path"] for r in chunk if r["kind"] != "video"])
        vtags, vdone = exif.read_many([r["path"] for r in chunk if r["kind"] == "video"], batch=50, fast=False)
        tags.update(vtags)
        done |= vdone
        sidecar_of = {r["path"]: exif.sidecars_for(r["path"]) for r in chunk if r["kind"] != "video"}
        side_paths = sorted({s for v in sidecar_of.values() for s in v})
        side_tags, _ = exif.read_many(side_paths, tags=exif.SIDECAR_TAGS) if side_paths else ({}, set())
        for r in chunk:
            if r["path"] not in done:
                continue           # exiftool didn't run on this batch: try again next scan
            meta = exif.summarize(tags.get(r["path"], {}))
            sides = [exif.summarize_sidecar(side_tags[s]) for s in sidecar_of.get(r["path"], []) if s in side_tags]
            if sides:
                meta["sidecar"] = sides[0]
            if r["kind"] != "video" and exif.sniff_c2pa(r["path"]):
                meta["c2pa"] = True
            conn.execute("UPDATE files SET meta=?, f_capture=?, exif_done=1 WHERE id=?",
                         (json.dumps(meta), meta["capture_ts"], r["id"]))
            if r["shot_id"] is not None:
                touched.add(r["shot_id"])
        conn.commit()
    if progress:
        progress(f"reading metadata for {len(rows):,} files", len(rows), len(rows))
    return touched


# ---------------------------------------------------------------- shots

def _clusters_by_time(files: list) -> list[list]:
    """Files sharing a folder and name are one photo when taken together. Another camera's
    IMG_0001.JPG next to this one's IMG_0001.CR3 is a different photo."""
    timed = sorted((f for f in files if f["f_capture"] is not None), key=lambda f: f["f_capture"])
    clusters: list[list] = []
    for f in timed:
        if clusters and f["f_capture"] - clusters[-1][-1]["f_capture"] <= PAIR_SECONDS:
            clusters[-1].append(f)
        else:
            clusters.append([f])
    untimed = [f for f in files if f["f_capture"] is None]
    if not clusters:
        return [untimed]
    clusters[0].extend(untimed)
    return clusters


def _pair_shots(conn) -> set[int]:
    """Group present files into shots by (folder, filename stem, capture time). Only rows that
    actually change are written. Returns the shots created or changed."""
    decided = {r[0] for r in conn.execute("SELECT shot_id FROM decisions")}
    shots = {r["id"]: r for r in conn.execute("SELECT id, dir, stem, status, preview_file_id, media FROM shots")}
    by_key: dict[tuple[str, str], list] = defaultdict(list)
    for f in conn.execute("SELECT id, path, shot_id, kind, fp, f_capture FROM files WHERE status='present'"):
        d, name = os.path.split(f["path"])
        by_key[(d, os.path.splitext(name)[0].lower())].append(f)

    touched: set[int] = set()
    claimed: set[int] = set()
    merged_into: dict[int, int] = {}       # a shot whose files joined another one -> that one
    for (d, _), all_files in by_key.items():
        for files in _clusters_by_time(all_files) if len(all_files) > 1 else [all_files]:
            stem = os.path.splitext(os.path.basename(files[0]["path"]))[0]
            candidates = sorted({f["shot_id"] for f in files if f["shot_id"] is not None} - claimed)
            if candidates:
                with_dec = [s for s in candidates if s in decided]
                target = with_dec[0] if with_dec else candidates[0]
                for other in candidates:
                    if other != target:
                        merged_into[other] = target
                cur = shots.get(target)
                if not cur or (cur["dir"], cur["stem"], cur["status"]) != (d, stem, "present"):
                    conn.execute("UPDATE shots SET dir=?, stem=?, status='present' WHERE id=?", (d, stem, target))
                    touched.add(target)
            else:
                target = db.new_id(conn, "shots")
                conn.execute("INSERT INTO shots(id, dir, stem, status) VALUES (?, ?, ?, 'present')", (target, d, stem))
                touched.add(target)
                cur = None
            claimed.add(target)
            moving = [f["id"] for f in files if f["shot_id"] != target]
            if moving:
                conn.execute(f"UPDATE files SET shot_id=? WHERE id IN ({','.join('?' * len(moving))})", (target, *moving))
                touched.add(target)
            preview = min(files, key=lambda f: (PREVIEW_PREFERENCE[f["kind"]], f["path"]))
            media = "video" if all(f["kind"] == "video" for f in files) else "photo"
            if not cur or cur["preview_file_id"] != preview["id"] or cur["media"] != media:
                conn.execute("UPDATE shots SET preview_file_id=?, media=? WHERE id=?", (preview["id"], media, target))
                touched.add(target)

    # Missing files keep their last shot; shots without present files go missing.
    conn.execute(
        "UPDATE shots SET status='missing' WHERE status='present' AND id NOT IN"
        " (SELECT shot_id FROM files WHERE status='present' AND shot_id IS NOT NULL)"
    )
    # Shots emptied by merges (no files at all) are removed with what was computed about them.
    orphans = [r[0] for r in conn.execute(
        "SELECT id FROM shots WHERE id NOT IN (SELECT DISTINCT shot_id FROM files WHERE shot_id IS NOT NULL)")]
    if orphans:
        for o in orphans:
            if o in merged_into:          # its files joined another shot: what you made follows them
                dst = merged_into[o]
                while dst in merged_into and merged_into[dst] != o:
                    dst = merged_into[dst]
                _merge_shot_data(conn, o, dst)
        db.forget_computed(conn, orphans)
        q = ",".join("?" * len(orphans))
        conn.execute(f"DELETE FROM decisions WHERE shot_id IN ({q})", orphans)
        conn.execute(f"DELETE FROM shot_keywords WHERE shot_id IN ({q})", orphans)
        conn.execute(f"DELETE FROM shots WHERE id IN ({q})", orphans)
    conn.commit()
    return touched - set(orphans)


def _merge_shot_data(conn, src: int, dst: int) -> None:
    """Two shots became one (a RAW joined its JPEG, say). Everything you made about the one going
    away moves to the one that stays: stars and picks, keywords, comments, favourites, album places,
    history and the names on its faces. When both had a decision, the newer one is kept and the other
    is written to the history, so Undo brings it back."""
    import json as _json

    cols = "rating, pick, label, updated_at"
    a = conn.execute(f"SELECT {cols} FROM decisions WHERE shot_id=?", (src,)).fetchone()
    b = conn.execute(f"SELECT {cols} FROM decisions WHERE shot_id=?", (dst,)).fetchone()
    if a and not b:
        conn.execute("UPDATE decisions SET shot_id=? WHERE shot_id=?", (dst, src))
    elif a and b:
        keep, drop = (a, b) if (a["updated_at"] or 0) > (b["updated_at"] or 0) else (b, a)
        vals = lambda r: {"rating": r["rating"], "pick": r["pick"], "label": r["label"]}
        if keep is a:
            conn.execute("UPDATE decisions SET rating=?, pick=?, label=?, updated_at=? WHERE shot_id=?",
                         (a["rating"], a["pick"], a["label"], a["updated_at"], dst))
        if vals(a) != vals(b):
            conn.execute("INSERT INTO decision_log(ts, batch, label, source, viewer, shot_id, before, after)"
                         " VALUES (?,?,?,?,?,?,?,?)",
                         (time.time(), f"m{src}-{dst}", "joined two files of one photo: kept the newer decision",
                          "merge", None, dst, _json.dumps(vals(drop)), _json.dumps(vals(keep))))
        conn.execute("DELETE FROM decisions WHERE shot_id=?", (src,))
    conn.execute("INSERT OR IGNORE INTO shot_keywords(shot_id, keyword_id, src) SELECT ?, keyword_id, src"
                 " FROM shot_keywords WHERE shot_id=?", (dst, src))
    conn.execute("DELETE FROM shot_keywords WHERE shot_id=?", (src,))
    conn.execute("UPDATE comments SET shot_id=? WHERE shot_id=?", (dst, src))
    for viewer, fav, updated in conn.execute("SELECT viewer, fav, updated FROM viewer_decisions WHERE shot_id=?", (src,)).fetchall():
        conn.execute("INSERT INTO viewer_decisions(viewer, shot_id, fav, updated) VALUES (?,?,?,?) ON CONFLICT(viewer, shot_id)"
                     " DO UPDATE SET fav=MAX(fav, excluded.fav)", (viewer, dst, fav, updated))
    conn.execute("DELETE FROM viewer_decisions WHERE shot_id=?", (src,))
    conn.execute("UPDATE album_items SET shot_id=? WHERE shot_id=?", (dst, src))
    conn.execute("UPDATE decision_log SET shot_id=? WHERE shot_id=?", (dst, src))
    from .smart import carry_shot_tags

    carry_shot_tags(conn, src, dst)
    # Names on faces: the faces are found again on the photo that stays; carry each name to the face
    # in the same place, or move the named faces across if that photo has none yet.
    named = conn.execute("SELECT id, x, y, w, person_id, person_src FROM faces WHERE shot_id=? AND person_src='user'",
                         (src,)).fetchall()
    theirs = conn.execute("SELECT id, x, y, w FROM faces WHERE shot_id=?", (dst,)).fetchall()
    if named and not theirs:
        conn.executemany("UPDATE faces SET shot_id=? WHERE id=?", [(dst, f["id"]) for f in named])
    else:
        for f in named:
            for g in theirs:
                if abs(f["x"] - g["x"]) < g["w"] / 2 and abs(f["y"] - g["y"]) < g["w"] / 2:
                    conn.execute("UPDATE faces SET person_id=?, person_src='user' WHERE id=?", (f["person_id"], g["id"]))
                    break


def local_wall_clock(epoch: float) -> float:
    """A real (UTC) time expressed like camera times are stored: local wall clock as if UTC."""
    return epoch + time.localtime(epoch).tm_gmtoff


def _summarize_shots(conn, shot_ids: set[int]) -> None:
    """Shot-level metadata from its files: the RAW's capture details first, GPS from whichever
    file has it, ratings and edits from sidecars."""
    if not shot_ids:
        return
    offsets = {r[0]: r[1] for r in conn.execute("SELECT camera_key, offset_s FROM camera_offsets")}
    from .grouping import camera_key

    ids = sorted(shot_ids)
    for i in range(0, len(ids), 900):
        chunk = ids[i : i + 900]
        files_by_shot: dict[int, list] = defaultdict(list)
        for f in conn.execute(
            f"SELECT id, shot_id, kind, meta, mtime_ns FROM files WHERE status='present' AND shot_id IN ({','.join('?' * len(chunk))})",
            chunk):
            files_by_shot[f["shot_id"]].append(f)
        given = dating.manual(conn, chunk)
        shot_shift = dating.shifts(conn, list(files_by_shot))     # times you shifted by hand (dating.py)
        for sid, files in files_by_shot.items():
            files.sort(key=lambda f: META_ORDER[f["kind"]])
            metas = [json.loads(f["meta"]) for f in files if f["meta"]]
            info = next((m for m in metas if m.get("capture_ts") is not None), metas[0] if metas else None)
            vals: dict = {"id": sid}
            if info:
                vals.update({k: info.get(k) for k in ("capture_ts", "capture_str", "camera", "body_serial", "lens", "iso",
                                                         "aperture", "shutter", "focal", "width", "height", "orientation",
                                                         "tz_offset")})
                vals["orientation"] = vals["orientation"] or 1
                gps = next((m for m in metas if m.get("gps_lat") is not None), None)
                vals.update(gps_lat=gps["gps_lat"] if gps else None, gps_lon=gps["gps_lon"] if gps else None,
                            gps_alt=gps.get("gps_alt") if gps else None)
                sides = [m["sidecar"] for m in metas if m.get("sidecar")]
                rating = next((m["rating"] for m in metas if m.get("rating")), None)
                side_rating = next((s["rating"] for s in sides if s.get("rating") is not None), None)
                vals["cam_rating"] = side_rating if side_rating is not None else rating
                vals["xmp_label"] = next((s["label"] for s in sides if s.get("label")), None) or \
                    next((m["label"] for m in metas if m.get("label")), None)
                vals["edited"] = int(any(s.get("edited") for s in sides))
                vals["c2pa"] = "present" if any(m.get("c2pa") for m in metas) else None
                vals["duration"] = next((m["duration"] for m in metas if m.get("duration")), None)
            else:   # metadata not read yet (exiftool missing): keep what the catalog already knows
                old = conn.execute("SELECT capture_ts, camera, body_serial FROM shots WHERE id=?", (sid,)).fetchone()
                if old and old["capture_ts"] is not None:
                    vals.update(capture_ts=old["capture_ts"], camera=old["camera"], body_serial=old["body_serial"])
            if vals.get("capture_ts") is not None:
                off = offsets.get(camera_key(vals.get("camera"), vals.get("body_serial")), 0.0) + shot_shift.get(sid, 0.0)
                vals["eff_ts"], vals["time_src"] = vals["capture_ts"] + off, "exif"
            elif sid in given:                       # no date of its own, but you gave it one
                vals.update(capture_str=given[sid], capture_ts=dating.ts_of(given[sid]))
                vals["eff_ts"], vals["time_src"] = vals["capture_ts"], "manual"
            else:
                mt = min((f["mtime_ns"] for f in files), default=None)
                vals["eff_ts"] = local_wall_clock(mt / 1e9) if mt else None
                vals["time_src"] = "file"
            cols = [k for k in vals if k != "id"]
            conn.execute(f"UPDATE shots SET {', '.join(f'{c}=:{c}' for c in cols)} WHERE id=:id", vals)
        conn.commit()


def apply_offsets(conn) -> int:
    """Recompute the shared timeline after a camera clock correction changed."""
    from .grouping import camera_key

    offsets = {r[0]: r[1] for r in conn.execute("SELECT camera_key, offset_s FROM camera_offsets")}
    shifted = dating.shifts(conn)                  # a photo's own shift stays on top of its camera's correction
    rows = conn.execute("SELECT id, capture_ts, camera, body_serial FROM shots WHERE capture_ts IS NOT NULL").fetchall()
    conn.executemany("UPDATE shots SET eff_ts=? WHERE id=?",
                     [(r["capture_ts"] + offsets.get(camera_key(r["camera"], r["body_serial"]), 0.0) + shifted.get(r["id"], 0.0), r["id"])
                      for r in rows])
    conn.commit()
    return len(rows)


def remove_root(conn, root_id: int) -> None:
    """Stop tracking a folder. Its photos become missing (decisions kept until prune)."""
    conn.execute("UPDATE files SET status='missing' WHERE root_id=?", (root_id,))
    conn.execute("DELETE FROM roots WHERE id=?", (root_id,))
    conn.commit()
    _pair_shots(conn)


def prune_missing(conn, project: str | None = None) -> int:
    """Forget photos missing from disk, with their ratings. Catalog only; touches no files."""
    ids = [r[0] for r in conn.execute("SELECT id FROM shots WHERE status='missing'")]
    db.forget_computed(conn, ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        q = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM decisions WHERE shot_id IN ({q})", chunk)
        conn.execute(f"DELETE FROM shot_keywords WHERE shot_id IN ({q})", chunk)
        conn.execute(f"DELETE FROM shots WHERE id IN ({q})", chunk)
    conn.execute("DELETE FROM files WHERE status='missing'")
    conn.commit()
    if project:
        _forget_cached_images(project, ids)
    return len(ids)


def _forget_cached_images(project: str, ids: list[int]) -> None:
    """JoeyLab's own thumbnails and previews of forgotten photos (inside ~/.joeylab only)."""
    from .config import project_dir

    base = project_dir(project)
    for sid in ids:
        sub = f"{sid % 100:02d}"
        (base / "thumbs" / sub / f"{sid}.jpg").unlink(missing_ok=True)
        for d in ("thumbs_s", "previews"):
            folder = base / d / sub
            if folder.is_dir():
                for f in folder.glob(f"{sid}_*.jpg"):
                    f.unlink(missing_ok=True)
