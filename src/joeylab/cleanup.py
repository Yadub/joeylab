"""Moving files you've decided to remove (duplicate extras, rejects) and leftover empty folders to
the Trash, safely. JoeyLab itself never deletes anything: these run only from the command line,
on a list you give it, after showing what would go and asking you to type the count.

Every file is checked first, read-only:
  * it must be a file of this library, unchanged since JoeyLab last read it;
  * what stays behind is worked out: an identical copy (compared byte for byte), a re-saved copy of
    the same photo, the photo's other file (its RAW or JPEG), or nothing at all.
Files whose copy stays go by default; files that are the last copy go only when asked for
(--include-last-copies). Just before each move the file, and the copy that stays, are checked again.

Moves use macOS's own Trash (Finder's "Put Back" works). On an external drive the Trash lives on
that drive, so the space comes back when you empty the Trash. Each run writes a log to
~/.joeylab/trash-log. The catalog marks each file missing as it goes, as the next scan would, so
the app shows it gone at once; put back, it's found again by the next scan.
"""

from __future__ import annotations

import csv
import os
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

from . import config
from .hashing import full_hash

# Leftovers that don't make a folder "not empty": Finder, Windows and macOS metadata.
CLUTTER = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized", "Icon\r", "ehthumbs.db"}
# Volume-level folders the system owns: never listed, never entered.
SYSTEM_DIRS = {".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems", ".DocumentRevisions-V100",
               "System Volume Information", "$RECYCLE.BIN", ".Trash"}

IDENTICAL, RESAVED, TWIN, LAST, SKIP = "identical", "resaved", "twin", "last", "skip"
SIDECAR = "sidecar"                   # a RAW's own .xmp, moved with it (raws.py)
HAS_COPY = (IDENTICAL, RESAVED)
DESCRIBE = {
    IDENTICAL: "an identical copy stays on this drive (checked byte for byte)",
    RESAVED: "a re-saved copy of the same photo stays on this drive",
    TWIN: "no copy stays; the photo's other file (RAW or JPEG) does",
    LAST: "no copy stays on this drive: this is the last copy here",
    SKIP: "skipped, left alone",
    SIDECAR: "the RAW's own sidecar (.xmp), moved with it",
}


@dataclass
class Item:
    path: str
    category: str = SKIP
    size: int = 0
    mtime_ns: int = 0
    stays: str | None = None          # the copy (or twin) that stays
    stays_size: int | None = None
    shot_id: int | None = None
    ident: tuple | None = None        # (device, inode): the file itself, however its path is spelled
    why: str = ""
    result: str = ""
    trashed_to: str | None = None
    photo_gone: bool = False          # moved, and none of its photo's files is left: the photo is missing


def _is_clutter(name: str) -> bool:
    return name in CLUTTER or name.startswith("._")


def _clean(paths: list[str]) -> list[str]:
    out, seen = [], set()
    for p in paths:
        p = p.rstrip("\r\n").strip()
        if not p or p.startswith("#") or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


# ---------------------------------------------------------------- files

def plan(conn, paths: list[str], progress: Callable[[int, int], None] | None = None) -> list[Item]:
    """What would happen to each listed file, without changing anything."""
    from . import volumes

    paths = _clean(paths)
    home = os.path.realpath(config.HOME)
    drives = volumes.drive_keys(conn)
    rows = {}
    for p in paths:
        r = conn.execute("SELECT id, shot_id, size, mtime_ns, fp, status, root_id FROM files WHERE path=?", (p,)).fetchone()
        rows[p] = r
    listed = {r["id"] for r in rows.values() if r is not None}
    hashes: dict[str, str] = {}

    def h(path: str) -> str:
        if path not in hashes:
            hashes[path] = full_hash(path)
        return hashes[path]

    items = []
    for n, p in enumerate(paths, 1):
        if progress:
            progress(n, len(paths))
        it = Item(p)
        items.append(it)
        r = rows[p]
        if r is None:
            it.why = "not a file of this library"
            continue
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            it.why = "already gone"
            continue
        except OSError as e:
            it.why = f"can't be read ({e.strerror})"
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            it.why = "not a regular file"
            continue
        if os.path.realpath(p).startswith(home + os.sep):
            it.why = "inside JoeyLab's own folder"
            continue
        it.size, it.mtime_ns, it.shot_id, it.ident = st.st_size, st.st_mtime_ns, r["shot_id"], (st.st_dev, st.st_ino)
        if st.st_size != r["size"] or st.st_mtime_ns != r["mtime_ns"]:
            it.why = "changed since JoeyLab last read it (update the library and make the list again)"
            continue
        try:
            _what_stays(conn, it, r, listed, h, drives)
        except OSError as e:
            it.category, it.why = SKIP, f"can't be read ({e.strerror})"
    return items


def _present(path: str, not_ident: tuple | None = None) -> int | None:
    """The size of a regular file at this path, or None. With not_ident, a path that is really that
    same file (another spelling of it) counts as absent: a file is never its own copy."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or (not_ident and (st.st_dev, st.st_ino) == not_ident):
        return None
    return st.st_size


def _what_stays(conn, it: Item, r, listed: set[int], h, drives: dict | None = None) -> None:
    # 1. An identical copy outside this list, on the same drive: same quick fingerprint in the
    #    catalog, then the whole content of both read again now. A copy on another drive is a backup:
    #    it never makes a file go (the dialogs say it's there, as information).
    drives = drives or {}
    here = drives.get(r["root_id"])
    for cid, cpath, croot in conn.execute("SELECT id, path, root_id FROM files WHERE fp = ? AND id != ? AND status = 'present'",
                                          (r["fp"], r["id"])):
        if cid in listed or drives.get(croot) != here:
            continue
        size = _present(cpath, it.ident)
        if size == it.size and h(cpath) == h(it.path):
            it.category, it.stays, it.stays_size = IDENTICAL, cpath, size
            return
    # 2. A re-saved copy of the same photo (same picture, saved again at another quality).
    for (cpath,) in conn.execute(
            "SELECT f2.path FROM dup_members dm JOIN dup_sets ds ON ds.id = dm.set_id AND ds.kind = 'copy'"
            " JOIN dup_members d2 ON d2.set_id = dm.set_id AND d2.file_id != dm.file_id JOIN files f2 ON f2.id = d2.file_id"
            " WHERE dm.file_id = ? AND f2.status = 'present'", (r["id"],)):
        if conn.execute("SELECT id FROM files WHERE path=?", (cpath,)).fetchone()[0] in listed:
            continue
        size = _present(cpath, it.ident)
        if size:
            it.category, it.stays, it.stays_size = RESAVED, cpath, size
            return
    # 3. The photo's other file (its RAW for a JPEG, or the other way round).
    if r["shot_id"] is not None:
        for cid, cpath in conn.execute("SELECT id, path FROM files WHERE shot_id = ? AND id != ? AND status = 'present'",
                                       (r["shot_id"], r["id"])):
            if cid in listed:
                continue
            size = _present(cpath, it.ident)
            if size:
                it.category, it.stays, it.stays_size = TWIN, cpath, size
                return
    it.category = LAST


def summary(items: list[Item]) -> dict[str, tuple[int, int]]:
    out = {}
    for c in (IDENTICAL, RESAVED, TWIN, LAST, SKIP):
        sel = [i for i in items if i.category == c]
        out[c] = (len(sel), sum(i.size for i in sel))
    return out


def chosen(items: list[Item], include_last: bool) -> list[Item]:
    ok = HAS_COPY + ((TWIN, LAST) if include_last else ())
    return [i for i in items if i.category in ok]


def carry_over(conn, gone: str, stays: str, raise_to: dict | None = None) -> int:
    """A file went to the Trash and an identical (or re-saved) copy stays: what you made about the
    photo moves to the copy that stays. Album places point at it; keywords, favourites and names on
    faces are copied; comments move; stars and picks are collected in raise_to (only ever raised,
    and a reject is never carried). Nothing is taken off the moved photo, so Put Back loses nothing.
    Returns how many things were carried."""
    g = conn.execute("SELECT id, shot_id FROM files WHERE path=?", (gone,)).fetchone()
    s = conn.execute("SELECT id, shot_id FROM files WHERE path=?", (stays,)).fetchone()
    if not g or not s or g["shot_id"] is None or s["shot_id"] is None or g["shot_id"] == s["shot_id"]:
        return 0
    gs, ss = g["shot_id"], s["shot_id"]
    n = conn.execute("UPDATE album_items SET file_id=?, shot_id=? WHERE file_id=?", (s["id"], ss, g["id"])).rowcount
    # The rest belongs to the photo, so it moves only once none of the photo's files is left on disk.
    others = [r[0] for r in conn.execute("SELECT path FROM files WHERE shot_id=? AND id != ?", (gs, g["id"]))]
    if any(os.path.lexists(p) for p in others):
        return n
    n += conn.execute("UPDATE album_items SET shot_id=? WHERE shot_id=?", (ss, gs)).rowcount
    n += conn.execute("INSERT OR IGNORE INTO shot_keywords(shot_id, keyword_id, src) SELECT ?, keyword_id, src"
                      " FROM shot_keywords WHERE shot_id=?", (ss, gs)).rowcount
    n += conn.execute("UPDATE comments SET shot_id=? WHERE shot_id=?", (ss, gs)).rowcount
    for viewer, fav, updated in conn.execute("SELECT viewer, fav, updated FROM viewer_decisions WHERE shot_id=? AND fav=1",
                                             (gs,)).fetchall():
        n += conn.execute("INSERT INTO viewer_decisions(viewer, shot_id, fav, updated) VALUES (?,?,?,?) ON CONFLICT(viewer, shot_id)"
                          " DO UPDATE SET fav=MAX(fav, excluded.fav)", (viewer, ss, fav, updated)).rowcount
    theirs = conn.execute("SELECT id, x, y, w, h FROM faces WHERE shot_id=? AND COALESCE(person_src, '') != 'user'", (ss,)).fetchall()
    for f in conn.execute("SELECT x, y, w, h, person_id FROM faces WHERE shot_id=? AND person_src='user' AND person_id > 0", (gs,)):
        cx, cy = f["x"] + f["w"] / 2, f["y"] + f["h"] / 2
        near = min(theirs, key=lambda t: abs(t["x"] + t["w"] / 2 - cx) + abs(t["y"] + t["h"] / 2 - cy), default=None)
        if near is not None and abs(near["x"] + near["w"] / 2 - cx) + abs(near["y"] + near["h"] / 2 - cy) < max(f["w"], f["h"]):
            conn.execute("UPDATE faces SET person_id=?, person_src='user' WHERE id=?", (f["person_id"], near["id"]))
            n += 1
    from .smart import carry_shot_tags

    carry_shot_tags(conn, gs, ss, keep=True)          # frames tagged for the shot list
    if raise_to is not None:
        a = conn.execute("SELECT COALESCE(rating,0) r, COALESCE(pick,0) p, COALESCE(label,'') l FROM decisions WHERE shot_id=?", (gs,)).fetchone()
        b = conn.execute("SELECT COALESCE(rating,0) r, COALESCE(pick,0) p, COALESCE(label,'') l FROM decisions WHERE shot_id=?", (ss,)).fetchone()
        if a:
            br = b["r"] if b else 0
            want = dict(raise_to.get(ss, {}))
            if a["r"] > max(br, want.get("rating", 0)):
                want["rating"] = a["r"]
            if a["p"] == 1 and (not b or b["p"] != 1):
                want["pick"] = 1
            if a["l"] and not (b and b["l"]):
                want["label"] = a["l"]
            if want:
                raise_to[ss] = want
    return n


def carry_decisions(conn, raise_to: dict) -> int:
    """Apply the stars, picks and labels carry_over collected, as one step in the history."""
    if not raise_to:
        return 0
    from . import library

    _, changed = library.decide(conn, [{"id": sid, **v} for sid, v in raise_to.items()],
                                what="kept copies took over from copies moved to the Trash", source="web")
    return len(changed)


def trash_files(items: list[Item], go: list[Item], log_name: str, mover: Callable[[str], str | None] | None = None,
                still_wanted: Callable[[Item], str | None] | None = None, conn=None) -> str:
    """Move the confirmed files (`go`, a part of the planned `items`) to the Trash, each checked again
    just before (and, with still_wanted, asked whether it should still go: a reason means no). One
    run at a time, across the app and the command line. Every planned file goes in the log, moved or
    not. Returns the log's path."""
    mover = mover or move_to_trash
    chosen_ids = {id(i) for i in go}
    for it in items:
        if id(it) not in chosen_ids:
            it.result = f"left alone: {it.why}" if it.category == SKIP else "left alone: no copy stays (not asked to include last copies)"
    with trash_lock():
        _move_files(go, mover, still_wanted, on_moved=(lambda it: mark_moved(conn, it)) if conn is not None else None)
    if conn is not None:
        # The copies that stay take over albums, keywords, favourites, comments and stars.
        raise_to: dict = {}
        for it in go:
            if it.result == "moved to the Trash" and it.category in HAS_COPY and it.stays:
                carry_over(conn, it.path, it.stays, raise_to)
        conn.commit()
        carry_decisions(conn, raise_to)
        if any(it.result == "moved to the Trash" for it in go):
            from .dupes import choose_shown

            choose_shown(conn)          # a photo held on another drive too is shown from a copy that's still there
    return _log(items, log_name)


def repair_from_logs(conn, project: str) -> dict:
    """For moves made before copies took over automatically: read this library's Trash logs and let
    every copy that stays take over from the file moved. Safe to run more than once."""
    import glob

    raise_to: dict = {}
    files = carried = 0
    for log in sorted(glob.glob(str(config.HOME / "trash-log" / f"{project}-files-*.tsv"))):
        with open(log, encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                if row.get("result") == "moved to the Trash" and row.get("copy that stays") and \
                        not row.get("what stays", "").startswith("no copy stays"):
                    files += 1
                    carried += carry_over(conn, row["file"], row["copy that stays"], raise_to)
    conn.commit()
    return {"files": files, "carried": carried, "decisions": carry_decisions(conn, raise_to)}


def mark_moved(conn, it: Item) -> None:
    """A file has just gone to the Trash: the catalog says so at once, as the next scan would. The
    file is missing, and so is its photo once none of its files is left on disk (several files of one
    photo can go in one run); the file leaves its duplicate sets, and a set left with one file goes."""
    from .scanner import PREVIEW_PREFERENCE

    r = conn.execute("SELECT id, shot_id FROM files WHERE path=?", (it.path,)).fetchone()
    if not r:
        return
    conn.execute("UPDATE files SET status='missing' WHERE id=?", (r["id"],))
    shot, sets = r["shot_id"], {x[0] for x in conn.execute("SELECT set_id FROM dup_members WHERE file_id=?", (r["id"],))}
    conn.execute("DELETE FROM dup_members WHERE file_id=?", (r["id"],))
    if shot is not None:
        left = [f for f in conn.execute("SELECT id, path, kind FROM files WHERE shot_id=? AND status='present'", (shot,))
                if os.path.lexists(f["path"])]
        if not left:
            conn.execute("UPDATE shots SET status='missing' WHERE id=?", (shot,))
            sets |= {x[0] for x in conn.execute("SELECT set_id FROM dup_members WHERE shot_id=?", (shot,))}
            conn.execute("DELETE FROM dup_members WHERE shot_id=?", (shot,))
            it.photo_gone = True
        else:      # shown from its other file now, as the next scan would choose
            other = min(left, key=lambda f: (PREVIEW_PREFERENCE.get(f["kind"], 9), f["path"]))
            conn.execute("UPDATE shots SET preview_file_id=? WHERE id=? AND preview_file_id=?", (other["id"], shot, r["id"]))
    for sid in sets:
        rest = conn.execute("SELECT rowid, keep FROM dup_members WHERE set_id=? ORDER BY rowid", (sid,)).fetchall()
        if len(rest) < 2:
            conn.execute("DELETE FROM dup_members WHERE set_id=?", (sid,))
            conn.execute("DELETE FROM dup_sets WHERE id=?", (sid,))
        elif not any(x["keep"] for x in rest):
            conn.execute("UPDATE dup_members SET keep=1 WHERE rowid=?", (rest[0]["rowid"],))
    conn.commit()


def _move_files(go: list[Item], mover, still_wanted, on_moved: Callable[[Item], None] | None = None) -> None:
    for it in go:
        if still_wanted:
            reason = still_wanted(it)
            if reason:
                it.result = f"left alone: {reason}"
                continue
        try:
            st = os.lstat(it.path)
        except OSError:
            it.result = "left alone: gone already"
            continue
        if st.st_size != it.size or st.st_mtime_ns != it.mtime_ns:
            it.result = "left alone: changed since it was checked"
            continue
        if it.category in HAS_COPY + (TWIN,) and _present(it.stays, (st.st_dev, st.st_ino)) != it.stays_size:
            it.result = "left alone: the copy that should stay has changed or gone"
            continue
        try:
            it.trashed_to = mover(it.path)
            it.result = "moved to the Trash"
        except OSError as e:
            it.result = f"left alone: {e}"
            continue
        if on_moved:
            on_moved(it)


# ---------------------------------------------------------------- empty folders

@dataclass
class EmptyFolder:
    path: str
    clutter: list[str] = field(default_factory=list)   # leftovers inside it (and its empty subfolders)
    folders: int = 0                                    # empty subfolders inside it
    result: str = ""
    trashed_to: str | None = None
    drive: str | None = None                            # the external drive it was found on (volume uuid)


def folder_roots(conn) -> tuple[list[str], list[str]]:
    """(the library folders to look in for empty folders, the ones held back). As before a scan,
    a renamed drive is followed, and a folder whose drive name now belongs to a different drive is
    held back: that drive's folders aren't the library's."""
    from . import volumes

    hold = volumes.check_roots(conn)["hold"]
    rows = conn.execute("SELECT id, path FROM roots ORDER BY path").fetchall()
    return [r[1] for r in rows if r[0] not in hold], [r[1] for r in rows if r[0] in hold]


def _drive_of(path: str) -> str | None:
    from . import volumes

    here = volumes.identify(path)
    return here["uuid"] if here else None


def _emptiness(path: str) -> tuple[bool, list[str], int]:
    """(empty?, clutter files inside, empty subfolders inside) for one folder, looking all the way down."""
    clutter, folders = [], 0
    try:
        entries = list(os.scandir(path))
    except OSError:
        return False, [], 0            # can't tell: treat as not empty
    for e in entries:
        try:
            if e.is_symlink():
                return False, [], 0
            if e.is_dir(follow_symlinks=False):
                if e.name.startswith(".") or e.name in SYSTEM_DIRS:
                    return False, [], 0      # hidden folders count as content
                empty, c, f = _emptiness(e.path)
                if not empty:
                    return False, [], 0
                clutter += c
                folders += 1 + f
            elif _is_clutter(e.name):
                clutter.append(e.path)
            else:
                return False, [], 0
        except OSError:
            return False, [], 0
    return True, clutter, folders


def empty_folders(roots: list[str]) -> list[EmptyFolder]:
    """The top-most folders under these roots holding nothing but clutter (or nothing at all). The
    roots themselves are never listed, nor anything the system keeps on a drive. Read-only. Take
    the roots from folder_roots(), which leaves out folders on a different drive of the same name."""
    out: list[EmptyFolder] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        drive = _drive_of(root)
        for dirpath, dirnames, _ in os.walk(root, topdown=True, followlinks=False):
            keep = []
            for d in sorted(dirnames):
                full = os.path.join(dirpath, d)
                if d in SYSTEM_DIRS or d.startswith(".") or os.path.islink(full):
                    continue
                empty, clutter, folders = _emptiness(full)
                if empty:
                    out.append(EmptyFolder(full, clutter, folders, drive=drive))    # don't look inside: it goes whole
                else:
                    keep.append(d)
            dirnames[:] = keep
    return out


def trash_folders(folders: list[EmptyFolder], roots: list[str], log_name: str,
                  mover: Callable[[str], str | None] | None = None) -> str:
    mover = mover or move_to_trash
    roots_real = {os.path.realpath(r) for r in roots}
    with trash_lock():
        _move_folders(folders, roots_real, mover)
    path = _log_path(log_name)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["folder", "leftover files", "empty subfolders", "result", "now at"])
        for f in folders:
            w.writerow([f.path, len(f.clutter), f.folders, f.result, f.trashed_to or ""])
    return str(path)


def _move_folders(folders: list[EmptyFolder], roots_real: set[str], mover) -> None:
    for f in folders:
        real = os.path.realpath(f.path)
        if real in roots_real or not any(real.startswith(r + os.sep) for r in roots_real):
            f.result = "left alone: not inside a library folder"
            continue
        if _drive_of(f.path) != f.drive:                 # another drive under the same name since the check
            f.result = "left alone: a different drive is there now"
            continue
        empty, clutter, _ = _emptiness(f.path)           # again, right before
        if not empty:
            f.result = "left alone: no longer empty"
            continue
        try:
            f.trashed_to = mover(f.path)
            f.result = "moved to the Trash"
        except OSError as e:
            f.result = f"left alone: {e}"


# ---------------------------------------------------------------- the Trash

class Busy(RuntimeError):
    """Something else (the app or the command line) is moving files to the Trash right now."""


@contextmanager
def trash_lock(wait: float = 0.0):
    """One move to the Trash at a time on this Mac. Two at once could each count the other's file
    as "the copy that stays" and move both copies."""
    import fcntl

    path = config.HOME / "trash.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.time() + wait
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise Busy("Something else is moving files to the Trash right now. Try again when it finishes.")
                time.sleep(0.3)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def move_to_trash(path: str) -> str | None:
    """Move to the macOS Trash (the drive's own Trash on an external drive), the way Finder does, so
    "Put Back" works. Returns where it went."""
    if sys.platform != "darwin":
        raise OSError("moving to the Trash works on macOS only")
    try:
        from Foundation import NSURL, NSFileManager
    except ImportError:
        return _trash_command(path)
    ok, where, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(path), None, None)
    if not ok:
        raise OSError(str(err.localizedDescription()) if err else "the Trash refused it")
    return str(where.path()) if where else None


def _trash_command(path: str) -> str | None:
    import subprocess

    if not os.path.exists("/usr/bin/trash"):
        raise OSError("no way to reach the Trash on this Mac")
    r = subprocess.run(["/usr/bin/trash", "-s", path], capture_output=True, text=True)
    if r.returncode:
        raise OSError(r.stderr.strip() or "the Trash refused it")
    return None


def _log_path(name: str):
    d = config.HOME / "trash-log"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.tsv"
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))      # what was moved, and from where: yours alone
    return path


def _log(items: list[Item], name: str) -> str:
    path = _log_path(name)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["file", "what stays", "copy that stays", "bytes", "result", "now at"])
        for it in items:
            w.writerow([it.path, DESCRIBE[it.category], it.stays or "", it.size, it.result or it.why, it.trashed_to or ""])
    return str(path)


def emptied_by(items: list[Item]) -> list[str]:
    """Folders that the files just moved have left empty (for a hint to tidy them too)."""
    out = []
    for d in sorted({os.path.dirname(i.path) for i in items if i.result == "moved to the Trash"}):
        empty, _, _ = _emptiness(d)
        if empty:
            out.append(d)
    return out
