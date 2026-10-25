"""Moving and renaming photos and folders from the app, for the Mac's owner.

JoeyLab never moves anything on its own. These run only when you ask (Library › Folders, or the
selection bar), after showing what would happen, and every one can be moved back.

* On one drive a move is a rename: instant, nothing copied. To another drive each file is copied,
  flushed to that drive and read back to compare (size and the whole content) before the original
  goes to the Mac's Trash, never deleted, so the space comes back only when you empty the Trash.
* Nothing is ever overwritten. A name that's taken is skipped or kept with " 2" (your choice), and
  the system itself refuses to replace anything (renamex_np with RENAME_EXCL).
* A photo moves whole: its RAW, JPEG, HEIC and Live Photo video, and the sidecars beside them
  (.xmp, .aae and the like), keeping their names together.
* Photos stay in the library: a move out of the library's folders, or onto or off a read-only
  drive, is refused, with the reason.
* The catalog follows as each photo moves (paths, your names for folders, the rules for which copy
  of a duplicate to keep, albums...), under the library's update lock, so the folder watcher never
  sees a move half done and the next scan finds nothing changed.
* Every operation is logged in move-log/ before anything moves (all it will do, then each step as
  it's done), so one stopped half way (a crash, a power cut, an unplugged drive) is finished or
  rolled back from its log by recover(), and any operation can be moved back (plan_back()).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Callable

from . import cleanup, config, db, locks, volumes

# Files beside a photo that belong to it: edits and metadata from other apps, camera thumbnails.
SIDECAR_EXTS = {".xmp", ".aae", ".thm", ".lrv", ".dop", ".pp3", ".on1"}
PARTIAL = ".joeylab-partial-"          # copies being made: hidden, so no scan ever sees one
CHUNK = 4 * 1024 * 1024
SPACE_MARGIN = 256 * 1024 * 1024         # left free on a drive after a copy (less on a small drive)
ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
TOKENS = ("date", "event", "place", "folder", "name", "n")
DEFAULT_PATTERN = "{date} {event} {n}"
DONE = ("moved", "copied", "put back", "made")


class Refused(ValueError):
    """The operation can't be done (the reason is for the person asking)."""


@dataclass
class Step:
    """One thing to move: a file, a folder (as a whole), or a folder to make or take away."""
    src: str
    dst: str
    kind: str = "file"               # file | folder | mkdir | rmdir
    unit: int = 0                    # the steps of one unit (a photo, a folder) go together or not at all
    shot_id: int | None = None
    file_id: int | None = None       # a catalogued file (None for a sidecar)
    size: int = 0
    mtime_ns: int = 0
    copy: bool = False               # to another drive: copied, checked, then the original goes to the Trash
    put_back: str | None = None      # moving back: the original, still in the Trash, comes back instead
    result: str = ""
    trashed_to: str | None = None    # where the original went (copies), or the folder taken away
    copied_size: int | None = None   # the copy as made, so moving back can tell it's unchanged
    copied_mtime_ns: int | None = None
    tree: list | None = None         # a folder copied to another drive: [[path inside, size], ...]


@dataclass
class Plan:
    op: str                          # move_photos | rename_photos | move_folder | rename_folder | new_folder | move_back
    project: str
    target: str = ""                 # the folder things go to, or a folder's new path
    source: str = ""                 # the folder moved or renamed
    steps: list[Step] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)   # [{name, steps}]: names taken, with the "keep both" steps
    skipped: list[dict] = field(default_factory=list)     # [{what, why}]
    refused: str | None = None
    photos: int = 0
    files: int = 0
    bytes: int = 0
    undoes: str | None = None
    undoes_label: str | None = None
    preview: list[dict] = field(default_factory=list)    # renames: [{id, from, to, taken}]

    def every(self) -> list[Step]:
        return self.steps + [s for c in self.conflicts for s in c["steps"]]

    def copying(self) -> bool:
        return any(s.copy for s in self.every())

    def how(self) -> str:
        steps = self.every()
        if any(s.put_back for s in steps):
            return "the originals come back from the Trash, and the copies go to the Trash"
        if not any(s.copy for s in steps):
            return "same drive, instant"
        if all(s.copy for s in steps):
            return "to another drive: each file is copied and checked, then the original goes to the Trash"
        return "partly to another drive: those files are copied and checked, then the originals go to the Trash"

    def what(self) -> str:
        """The plan in a sentence: "Move 24 photos (51 files, 1.2 GB) to /Volumes/Photo Drive/2026/Phuket · same drive, instant"."""
        if self.op == "move_photos":
            return f"Move {_n(self.photos, 'photo')} ({_n(self.files, 'file')}, {size_text(self.bytes)}) to {self.target} · {self.how()}"
        if self.op == "rename_photos":
            return f"Rename the files of {_n(self.photos, 'photo')} ({_n(self.files, 'file')}) · in their folders, instant"
        if self.op == "rename_folder":
            return (f"Rename “{os.path.basename(self.source)}” to “{os.path.basename(self.target)}” · "
                    f"{_n(self.photos, 'photo')} inside {'follows' if self.photos == 1 else 'follow'}, instant")
        if self.op == "move_folder":
            return (f"Move “{os.path.basename(self.source)}” ({_n(self.photos, 'photo')}, {size_text(self.bytes)}) into "
                    f"{os.path.dirname(self.target)} · {self.how()}")
        if self.op == "new_folder":
            return f"Make a new folder “{os.path.basename(self.target)}” in {os.path.dirname(self.target)}"
        return f"{self._back()} · {self.how()}"

    def done_label(self) -> str:
        """The same, done, for the history: "Moved 24 photos to Phuket"."""
        if self.op == "move_photos":
            return f"Moved {_n(self.photos, 'photo')} to {os.path.basename(self.target) or self.target}"
        if self.op == "rename_photos":
            return f"Renamed the files of {_n(self.photos, 'photo')}"
        if self.op == "rename_folder":
            return f"Renamed “{os.path.basename(self.source)}” to “{os.path.basename(self.target)}”"
        if self.op == "move_folder":
            return f"Moved “{os.path.basename(self.source)}” into {os.path.basename(os.path.dirname(self.target))}"
        if self.op == "new_folder":
            return f"Made the folder “{os.path.basename(self.target)}”"
        return self._back(done=True)

    def _back(self, done: bool = False) -> str:
        """Moving back a move back does it again: "Again: Moved 3 photos to Phuket"."""
        label = self.undoes_label or "an earlier move"
        for prefix in ("Moved back: ", "Again: "):
            if label.startswith(prefix):
                return ("Again: " if prefix == "Moved back: " else "Moved back: " if done else "Move back: ") + label[len(prefix):]
        return f"{'Moved back' if done else 'Move back'}: {label}"

    def as_dict(self) -> dict:
        why = Counter(s["why"] for s in self.skipped)
        return {"op": self.op, "what": self.what(), "how": self.how(), "target": self.target, "source": self.source,
                "refused": self.refused, "photos": self.photos, "files": self.files, "bytes": self.bytes,
                "same_drive": not self.copying(), "conflicts": len(self.conflicts),
                "conflict_examples": [{"name": c["name"], "keep_both": os.path.basename(c["steps"][0].dst)}
                                      for c in self.conflicts[:6] if c["steps"]],
                "skipped": [{"why": w, "n": k} for w, k in why.most_common()], "undoes": self.undoes,
                "preview": self.preview[:300]}


def _n(k: int, word: str) -> str:
    return f"{k:,} {word}{'' if k == 1 else 's'}"


def size_text(b: int) -> str:
    for unit, scale in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if b >= scale:
            return f"{b / scale:.1f} {unit}"
    return f"{b} B"


def _range(folder: str) -> tuple[str, str]:
    """Bounds for paths inside a folder, for an indexed range query (path >= lo AND path < hi)."""
    pre = folder.rstrip(os.sep) + os.sep
    return pre, pre[:-1] + chr(ord(os.sep) + 1)


# ---------------------------------------------------------------- where things can go

def roots(conn) -> list[str]:
    """The library's folders, deepest first."""
    return [r[0] for r in conn.execute("SELECT path FROM roots ORDER BY length(path) DESC")]


def root_of(path: str, rs: list[str]) -> str | None:
    """The library folder `path` is in (or is), or None: outside the library."""
    for r in rs:
        if path == r or path.startswith(r.rstrip(os.sep) + os.sep):
            return r
    return None


def same_drive(a: str, b: str) -> bool:
    """Whether `a` can go into the folder `b` by a rename (both on one drive). Tests replace this."""
    return os.lstat(a).st_dev == os.stat(b).st_dev


def _mount_point(path: str) -> str:
    """The folder a drive is mounted at, for any path on it."""
    p = os.path.realpath(path)
    dev = os.stat(p).st_dev
    while p != "/" and os.stat(os.path.dirname(p)).st_dev == dev:
        p = os.path.dirname(p)
    return p


def drive_name(path: str) -> str:
    """“Photo Drive”, or this Mac's disk."""
    here = volumes.identify(path)
    if here:
        return f"“{here['name']}”"
    try:
        if os.stat(path).st_dev == os.stat(os.path.expanduser("~")).st_dev or os.stat(path).st_dev == os.stat("/").st_dev:
            return "this Mac's disk"
        mount = _mount_point(path)
    except OSError:
        return "this Mac's disk"
    return f"“{volumes.info(mount).get('name') or os.path.basename(mount)}”"


def drive_problem(path: str) -> str | None:
    """Why nothing on the drive holding `path` can be changed, or None."""
    try:
        if os.statvfs(path).f_flag & os.ST_RDONLY:
            return (f"The drive {drive_name(path)} is read-only (it's mounted that way, or locked), so nothing on it can be "
                    "moved or renamed. Eject it and plug it in again to change things on it.")
    except OSError as e:
        return f"{path} can't be reached ({e.strerror})."
    return None


def _writable(folder: str) -> str | None:
    if not os.access(folder, os.W_OK | os.X_OK):
        return f"macOS doesn't let JoeyLab change {folder} (its permissions)."
    return None


def _inside_home(path: str) -> bool:
    home = os.path.realpath(config.HOME)
    real = os.path.realpath(path)
    return real == home or real.startswith(home + os.sep)


def _folder_problem(folder: str, rs: list[str], left_out: list[str] = ()) -> str | None:
    """Why `folder` can't take part in a move (as where things go, or the folder moved), or None.
    `left_out`: folders the library leaves out (moved to another library)."""
    if not folder or not os.path.isabs(folder):
        return "Give the folder's full path."
    if not os.path.isdir(folder):
        return f"There's no folder at {folder} (is the drive plugged in?)."
    if _inside_home(folder):
        return "That's inside JoeyLab's own folder."
    if not root_of(folder, rs) or root_of(folder, list(left_out)):
        return (f"{folder} isn't one of this library's folders, so photos moved there would leave the library. "
                "Add it as a library folder first (Settings › Folders), then move them.")
    return drive_problem(folder) or _writable(folder)


def clean_name(name: str) -> str:
    """A file or folder name as typed, made safe: no slashes or colons (Finder shows a colon as a
    slash), no leading dot (it would be hidden), no spaces at either end."""
    n = unicodedata.normalize("NFC", str(name or ""))
    n = re.sub(r"[/:\x00-\x1f\x7f]", "-", n)
    n = re.sub(r"\s+", " ", n).strip().lstrip(".").strip()
    if not n:
        raise Refused("Give it a name.")
    if len(n.encode()) > 240:
        raise Refused("That name is too long.")
    return n


def _same_name(a: str, b: str) -> bool:
    """One name spelled two ways a Mac drive treats as the same (case, or the Unicode form of an accent)."""
    return unicodedata.normalize("NFC", a).casefold() == unicodedata.normalize("NFC", b).casefold()


def _actual_name(path: str) -> str:
    """`path` as the drive spells it: a drive may keep a typed name in another Unicode form (HFS+
    decomposes accents), and scans see the drive's spelling."""
    if path.isascii():
        return path
    parent, name = os.path.split(path)
    try:
        names = os.listdir(parent)
    except OSError:
        return path
    if name in names:
        return path
    want = unicodedata.normalize("NFC", name)
    return next((os.path.join(parent, n) for n in names if unicodedata.normalize("NFC", n) == want), path)


def _taken_in(conn, folder: str) -> set[str]:
    """Names the catalog has in a folder (present or missing), case-folded: a name JoeyLab
    remembers is never given to another file."""
    lo, hi = _range(folder)
    out = set()
    for (p,) in conn.execute("SELECT path FROM files WHERE path >= ? AND path < ?", (lo, hi)):
        rest = p[len(lo):]
        if os.sep not in rest:
            out.add(rest.casefold())
    return out


def _is_free(path: str, taken: set[str], claimed: set[str], mine: set[str] = frozenset()) -> bool:
    """Nothing has this name now: on disk, in the catalog, or earlier in this plan. `mine`: the
    paths of the files being renamed themselves (a change of case only is fine)."""
    key = path.casefold()
    if key in claimed:
        return False
    if key in mine:
        return True
    return os.path.basename(path).casefold() not in taken and not os.path.lexists(path)


# ---------------------------------------------------------------- the library's folders

def folders(conn) -> list[dict]:
    """The library's folders, to choose where things go: each library folder, every folder with
    photos and the folders between, with how many photos each holds (counting inside it)."""
    deep = roots(conn)
    names = db.get_setting(conn, "folder_names", {}) or {}
    out: dict[str, dict] = {}

    def node(p: str, root: str) -> dict:
        if p not in out:
            out[p] = {"path": p, "name": os.path.basename(p) or p, "label": names.get(p), "n": 0, "own": 0, "root": root}
        return out[p]

    for r in deep:
        nd = node(r, r)
        nd["available"] = os.path.isdir(r)
        nd["drive"] = drive_name(r) if nd["available"] else None
        nd["read_only"] = bool(nd["available"] and drive_problem(r))
    for d, k in conn.execute("SELECT dir, COUNT(*) FROM shots WHERE status='present' GROUP BY dir"):
        root = root_of(d, deep)
        if not root:
            continue
        node(d, root)["own"] = k
        p = d
        while True:
            node(p, root)["n"] += k
            if p == root or len(p) <= len(root):
                break
            p = os.path.dirname(p)
    return sorted(out.values(), key=lambda x: x["path"].lower())


def subfolders(conn, path: str) -> list[dict]:
    """The folders inside one of the library's folders as they are on disk now, empty ones too."""
    if not root_of(path, roots(conn)) or not os.path.isdir(path):
        raise Refused("That isn't one of this library's folders.")
    lo, hi = _range(path)
    counts: dict[str, int] = defaultdict(int)
    for d, k in conn.execute("SELECT dir, COUNT(*) FROM shots WHERE status='present' AND dir >= ? AND dir < ? GROUP BY dir", (lo, hi)):
        counts[os.path.join(path, d[len(lo):].split(os.sep, 1)[0])] += k
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
    except OSError as e:
        raise Refused(f"Couldn't read {path} ({e.strerror}).")
    out = []
    left_out = set(db.excluded_folders(conn))            # moved to another library
    for e in entries:
        if e.name.startswith(".") or e.name in cleanup.SYSTEM_DIRS or e.path in left_out:
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                out.append({"path": e.path, "name": e.name, "n": counts.get(e.path, 0)})
        except OSError:
            continue
    return out


# ---------------------------------------------------------------- what moves with a photo

def _index(cache: dict, folder: str) -> tuple[dict, dict, dict]:
    """A folder's names once, for finding sidecars: (sidecars by what they belong to, other files by
    stem, ._ files by the name they belong to), all case-folded."""
    if folder not in cache:
        side, other, double = defaultdict(list), defaultdict(list), {}
        try:
            names = os.listdir(folder)
        except OSError:
            names = []
        for n in names:
            if n.startswith("._"):
                double[n[2:].casefold()] = n
                continue
            base, ext = os.path.splitext(n)
            (side if ext.lower() in SIDECAR_EXTS else other)[base.casefold()].append(n)
        cache[folder] = (side, other, double)
    return cache[folder]


def companions(folder: str, names: list[str], cache: dict, same: bool = True) -> list[str]:
    """Sidecars of these files that sit beside them. IMG_1.CR3.xmp always goes with IMG_1.CR3;
    IMG_1.xmp and IMG_1.AAE go with the photo IMG_1 unless another file called IMG_1 stays behind.
    On one drive, the ._ files too (where a drive without extended attributes keeps a file's)."""
    side, other, double = _index(cache, folder)
    moving = {n.casefold() for n in names}
    out: list[str] = []
    for n in names:
        out += side.get(n.casefold(), [])                               # IMG_1.CR3.xmp
    for stem in dict.fromkeys(os.path.splitext(n)[0].casefold() for n in names):
        if not [x for x in other.get(stem, []) if x.casefold() not in moving]:
            out += side.get(stem, [])                                   # IMG_1.xmp
    out = [x for x in dict.fromkeys(out) if x.casefold() not in moving]
    if same:
        out += [double[x.casefold()] for x in names + out if x.casefold() in double]
    return out


def _renamed(name: str, old_stem: str, new_stem: str) -> str:
    """A file's name with its photo's stem replaced: IMG_1.CR3.xmp -> Phuket 001.CR3.xmp."""
    prefix = "._" if name.startswith("._") else ""
    body = name[len(prefix):]
    if old_stem != new_stem and body.casefold().startswith(old_stem.casefold()):
        return prefix + new_stem + body[len(old_stem):]
    return name


def _keep_both(names: list[str], stem: str, folder: str, taken: set, claimed: set) -> list[str] | None:
    """The first "IMG_1 2", "IMG_1 3"... under which every one of these names is free."""
    for k in range(2, 1000):
        new = [_renamed(n, stem, f"{stem} {k}") if _renamed(n, stem, f"{stem} {k}") != n else
               _with_suffix(n, k) for n in names]
        if all(_is_free(os.path.join(folder, n), taken, claimed) for n in new):
            return new
    return None


def _with_suffix(name: str, k: int) -> str:
    prefix = "._" if name.startswith("._") else ""
    base, ext = os.path.splitext(name[len(prefix):])
    return f"{prefix}{base} {k}{ext}"


def _by_id(conn, sql: str, ids: list[int]) -> list:
    out = []
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        out += conn.execute(sql.format(q=",".join("?" * len(chunk))), chunk).fetchall()
    return out


def _source_ok(path: str) -> str | None:
    """Why this file can't be moved now, or None."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "not on disk (missing)"
    except OSError as e:
        return f"can't be read ({e.strerror})"
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if st.st_flags & (stat.UF_IMMUTABLE | stat.SF_IMMUTABLE):
        return "locked in Finder"
    return None


def _photo_steps(conn, plan: Plan, ids: list[int], new_stems: dict[int, str] | None, target: str | None) -> None:
    """Steps for whole photos: moved into `target` keeping their names, or renamed where they are to
    `new_stems`. Photos whose names are taken go to plan.conflicts, with "keep both" names."""
    rs = roots(conn)
    by_shot: dict[int, list] = defaultdict(list)
    for r in _by_id(conn, "SELECT id, shot_id, path FROM files WHERE status='present' AND shot_id IN ({q}) ORDER BY kind, path", ids):
        by_shot[r["shot_id"]].append(r)
    stems = {r["id"]: r["stem"] for r in _by_id(conn, "SELECT id, stem FROM shots WHERE id IN ({q})", ids)}
    cache: dict = {}
    taken_in: dict[str, set] = {}
    problems: dict[str, str | None] = {}
    claimed: set[str] = set()
    unit = 0
    for sid in ids:
        files, what = by_shot.get(sid), stems.get(sid, f"photo {sid}")
        if not files:
            plan.skipped.append({"what": what, "why": "not on disk (missing)"})
            continue
        src_dir = os.path.dirname(files[0]["path"])
        if any(os.path.dirname(f["path"]) != src_dir for f in files):
            plan.skipped.append({"what": what, "why": "its files are in different folders"})
            continue
        dest = target or src_dir
        if target and os.path.realpath(src_dir) == os.path.realpath(target):
            plan.skipped.append({"what": what, "why": "already in that folder"})
            continue
        if src_dir not in problems:
            problems[src_dir] = (drive_problem(src_dir) or _writable(src_dir)) if root_of(src_dir, rs) else \
                "not in one of this library's folders"
        why = problems[src_dir] or next((w for w in (_source_ok(f["path"]) for f in files) if w), None)
        if why:
            plan.skipped.append({"what": what, "why": why})
            continue
        old_stem = os.path.splitext(os.path.basename(files[0]["path"]))[0]
        new_stem = new_stems.get(sid, old_stem) if new_stems is not None else old_stem
        if new_stems is not None and new_stem == old_stem:
            plan.skipped.append({"what": what, "why": "already named so"})
            continue
        copy = bool(target) and not same_drive(files[0]["path"], target)
        names = [os.path.basename(f["path"]) for f in files]
        all_names = names + companions(src_dir, names, cache, same=not copy)
        if dest not in taken_in:
            taken_in[dest] = _taken_in(conn, dest)
        taken, mine = taken_in[dest], set()
        if new_stems is not None:                  # renamed where they are: their own names don't count
            mine = {os.path.join(src_dir, n).casefold() for n in all_names}
            taken = taken - {n.casefold() for n in all_names}
        dst_names = [_renamed(n, old_stem, new_stem) for n in all_names]
        ids_of = {os.path.basename(f["path"]): f["id"] for f in files}

        def steps_for(out_names: list[str]) -> list[Step]:
            out = []
            for n, d in zip(all_names, out_names):
                src = os.path.join(src_dir, n)
                try:
                    st = os.lstat(src)
                except OSError:
                    continue
                out.append(Step(src=src, dst=os.path.join(dest, d), unit=unit, shot_id=sid, file_id=ids_of.get(n),
                                size=st.st_size, mtime_ns=st.st_mtime_ns, copy=copy))
            return out

        if all(_is_free(os.path.join(dest, d), taken, claimed, mine) for d in dst_names):
            plan.steps += steps_for(dst_names)
            chosen, was_taken = dst_names, False
        else:
            alt = _keep_both(dst_names, new_stem, dest, taken, claimed)
            if not alt:
                plan.skipped.append({"what": what, "why": "every name like it is taken"})
                continue
            plan.conflicts.append({"name": dst_names[0], "steps": steps_for(alt)})
            chosen, was_taken = alt, True
        claimed.update(os.path.join(dest, d).casefold() for d in chosen)
        if new_stems is not None:
            plan.preview.append({"id": sid, "from": names[0], "to": chosen[0], "taken": was_taken})
        unit += 1
    every = plan.every()
    plan.photos = len({s.unit for s in every})
    plan.files = len(every)
    plan.bytes = sum(s.size for s in every)
    if target and plan.copying():
        _space_check(plan, target, sum(s.size for s in every if s.copy))


def _space_check(plan: Plan, target: str, need: int) -> None:
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return
    if need + min(SPACE_MARGIN, usage.total // 50) > usage.free:
        plan.refused = (f"There isn't room on {drive_name(target)}: this needs {size_text(need)}, and "
                        f"{size_text(usage.free)} is free (JoeyLab leaves a little spare).")


# ---------------------------------------------------------------- plans

def plan_move_photos(conn, project: str, ids: list[int], target: str) -> Plan:
    """Move whole photos into one of the library's folders, keeping their names."""
    target = os.path.normpath(target) if target else ""
    plan = Plan("move_photos", project, target=target)
    plan.refused = _folder_problem(target, roots(conn), db.excluded_folders(conn))
    if not plan.refused:
        _photo_steps(conn, plan, list(dict.fromkeys(ids)), None, target)
        if not plan.every() and not plan.refused:
            plan.refused = "Nothing to move: " + (plan.skipped[0]["why"] if plan.skipped else "no photos chosen") + "."
    return plan


def rename_stems(conn, ids: list[int], pattern: str, start: int = 1) -> dict[int, str]:
    """New names from a pattern: {date} (when taken, 2026-01-24), {event}, {place}, {folder}, {name}
    (the name it has now) and {n} (a counter in time order: 001, 002...). Empty parts close up."""
    rows = _by_id(conn, "SELECT s.id, s.stem, s.dir, s.capture_str, s.eff_ts, COALESCE(e.name, e.auto_name) AS event,"
                        " p.name AS place FROM shots s LEFT JOIN events e ON e.id = s.event_id"
                        " LEFT JOIN places p ON p.id = s.place_id WHERE s.id IN ({q})", ids)
    rows.sort(key=lambda r: (r["eff_ts"] is None, r["eff_ts"] or 0, r["dir"], r["stem"]))
    width = max(3, len(str(start + len(rows) - 1)))
    names = {}
    for k, r in enumerate(rows):
        if r["capture_str"]:
            date = r["capture_str"][:10].replace(":", "-")
        elif r["eff_ts"] is not None:
            date = datetime.fromtimestamp(r["eff_ts"], timezone.utc).strftime("%Y-%m-%d")
        else:
            date = ""
        vals = {"date": date, "event": (r["event"] or "").split("·")[0].strip(), "place": r["place"] or "",
                "folder": os.path.basename(r["dir"]), "name": r["stem"], "n": str(start + k).zfill(width)}
        out = re.sub(r"\{(\w+)\}", lambda m: vals.get(m.group(1), ""), pattern or DEFAULT_PATTERN)
        out = re.sub(r"\s+", " ", out).strip(" -_·")
        try:
            names[r["id"]] = clean_name(out)
        except Refused:
            names[r["id"]] = r["stem"]
    return names


def plan_rename_photos(conn, project: str, ids: list[int], pattern: str, start: int = 1) -> Plan:
    """Rename whole photos' files where they are, from a pattern, keeping each photo's files together."""
    pattern = pattern or DEFAULT_PATTERN
    plan = Plan("rename_photos", project, target=pattern)
    bad = [t for t in re.findall(r"\{(\w+)\}", pattern) if t not in TOKENS]
    if bad:
        plan.refused = f"JoeyLab doesn't know {{{bad[0]}}}. Use {', '.join('{' + t + '}' for t in TOKENS)}."
        return plan
    ids = list(dict.fromkeys(ids))
    _photo_steps(conn, plan, ids, rename_stems(conn, ids, pattern, start), None)
    if not plan.every():
        plan.refused = "Nothing to rename: " + (plan.skipped[0]["why"] if plan.skipped else "no photos chosen") + "."
    return plan


def _count_under(conn, folder: str) -> tuple[int, int, int]:
    """(photos, files, bytes) the catalog has in a folder and everything inside it."""
    lo, hi = _range(folder)
    photos = conn.execute("SELECT COUNT(*) FROM shots WHERE status='present' AND (dir = ? OR (dir >= ? AND dir < ?))",
                          (folder, lo, hi)).fetchone()[0]
    files, size = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files WHERE status='present'"
                               " AND path >= ? AND path < ?", (lo, hi)).fetchone()
    return photos, files, size


def _remembered_under(conn, folder: str) -> bool:
    lo, hi = _range(folder)
    return bool(conn.execute("SELECT 1 FROM files WHERE path >= ? AND path < ? LIMIT 1", (lo, hi)).fetchone())


def _folder_source(path: str, rs: list[str]) -> str | None:
    if os.path.isdir(path) and os.path.ismount(path):
        return "That's a whole drive. Rename it in Finder; JoeyLab follows a renamed drive by itself."
    return _folder_problem(path, rs) or (_writable(os.path.dirname(path)) if os.path.isdir(path) else None)


def plan_rename_folder(conn, project: str, path: str, name: str) -> Plan:
    """Rename a folder (one of the library's folders, or one inside them) where it is."""
    path = os.path.normpath(path) if path else ""
    plan = Plan("rename_folder", project, source=path)
    try:
        name = clean_name(name)
    except Refused as e:
        plan.refused = str(e)
        return plan
    plan.refused = _folder_source(path, roots(conn))
    if plan.refused:
        return plan
    dst = os.path.join(os.path.dirname(path), name)
    plan.target = dst
    if dst == path:
        plan.refused = "That's its name already."
        return plan
    same = _same_name(os.path.basename(dst), os.path.basename(path)) and os.path.lexists(dst) and os.path.samefile(dst, path)
    if os.path.lexists(dst) and not same:
        plan.refused = f"There's already something called “{name}” in {os.path.dirname(path)}. Choose another name."
        return plan
    if not same and _remembered_under(conn, dst):
        plan.refused = (f"JoeyLab remembers photos in a folder called “{name}” here that are missing now. Choose another "
                        "name, or forget the missing photos first (Tidy › Health).")
        return plan
    plan.photos, plan.files, plan.bytes = _count_under(conn, path)
    plan.steps = [Step(src=path, dst=dst, kind="folder")]
    return plan


def plan_new_folder(conn, project: str, parent: str, name: str) -> Plan:
    """Make a new, empty folder inside one of the library's folders."""
    parent = os.path.normpath(parent) if parent else ""
    plan = Plan("new_folder", project)
    try:
        name = clean_name(name)
    except Refused as e:
        plan.refused = str(e)
        return plan
    plan.refused = _folder_problem(parent, roots(conn), db.excluded_folders(conn))
    if plan.refused:
        return plan
    plan.target = dst = os.path.join(parent, name)
    if os.path.lexists(dst):
        plan.refused = f"There's already something called “{name}” in {parent}."
        return plan
    plan.steps = [Step(src=dst, dst=dst, kind="mkdir")]
    return plan


def _tree(folder: str) -> list[list]:
    """Every file in a folder on disk, [[path inside, size], ...] (._ files and .DS_Store left out:
    they travel as extended attributes, or Finder makes them again)."""
    out = []
    for dirpath, _, filenames in os.walk(folder):
        for f in filenames:
            if f.startswith("._") or f == ".DS_Store":
                continue
            p = os.path.join(dirpath, f)
            try:
                out.append([os.path.relpath(p, folder), os.lstat(p).st_size])
            except OSError:
                continue
    return sorted(out)


def plan_move_folder(conn, project: str, path: str, into: str) -> Plan:
    """Move a folder inside the library's folders into another of them, with everything in it."""
    path = os.path.normpath(path) if path else ""
    into = os.path.normpath(into) if into else ""
    plan = Plan("move_folder", project, source=path)
    rs = roots(conn)
    if path in rs:
        plan.refused = ("That's one of the library's own folders: it can be renamed where it is, but not moved. "
                        "Move the folders inside it instead.")
        return plan
    plan.refused = _folder_source(path, rs) or _folder_problem(into, rs, db.excluded_folders(conn))
    if plan.refused:
        return plan
    real_p, real_i = os.path.realpath(path), os.path.realpath(into)
    if real_i == real_p or real_i.startswith(real_p + os.sep):
        plan.refused = "A folder can't go inside itself."
        return plan
    if real_i == os.path.realpath(os.path.dirname(path)):
        plan.refused = "It's in that folder already."
        return plan
    name, copy = os.path.basename(path), not same_drive(path, into)
    plan.photos = _count_under(conn, path)[0]
    tree = _tree(path)
    plan.files, plan.bytes = len(tree), sum(t[1] for t in tree)
    dst = plan.target = os.path.join(into, name)
    taken = _taken_in(conn, into)
    if _is_free(dst, taken, set()) and not _remembered_under(conn, dst):
        plan.steps = [Step(src=path, dst=dst, kind="folder", copy=copy, size=plan.bytes)]
    else:
        alt = next((a for a in (os.path.join(into, f"{name} {k}") for k in range(2, 1000))
                    if _is_free(a, taken, set()) and not _remembered_under(conn, a)), None)
        if not alt:
            plan.refused = f"Every name like “{name}” is taken in {into}."
            return plan
        plan.conflicts.append({"name": name, "steps": [Step(src=path, dst=alt, kind="folder", copy=copy, size=plan.bytes)]})
    if copy:
        _space_check(plan, into, plan.bytes)
    return plan


# ---------------------------------------------------------------- the log

def log_dir():
    return config.HOME / "move-log"


class Log:
    """One operation's log, a line of JSON each: what it will do (written, and flushed to disk, before
    anything moves), then each step as it's done, and how it ended. It's …open.jsonl until it has
    finished or been recovered."""

    def __init__(self, path, header: dict, steps: list[Step], events: list[dict]):
        self.path, self.header, self.steps, self.events = path, header, steps, events

    @property
    def id(self) -> str:
        return self.header["id"]

    @property
    def open(self) -> bool:
        return self.path.name.endswith(".open.jsonl")

    @classmethod
    def start(cls, plan: Plan, steps: list[Step], photos: int) -> "Log":
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        oid = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        path = d / f"{plan.project}-{oid}.open.jsonl"
        header = {"v": 1, "id": oid, "project": plan.project, "op": plan.op, "what": plan.what(), "label": plan.done_label(),
                  "target": plan.target, "source": plan.source, "same_drive": not any(s.copy for s in steps),
                  "photos": photos, "files": len(steps), "bytes": sum(s.size for s in steps), "created": time.time(),
                  "undoes": plan.undoes}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)     # where your files are: yours alone
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(header) + "\n")
            for i, s in enumerate(steps):
                d_ = {k: v for k, v in asdict(s).items() if v is not None and v != "" and v is not False}
                fh.write(json.dumps({"step": i, **d_}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return cls(path, header, steps, [])

    def note(self, **event) -> None:
        event.setdefault("ts", time.time())
        self.events.append(event)
        self._apply(event)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def finish(self, **summary) -> None:
        self.note(finished=time.time(), **summary)
        if self.open:
            closed = self.path.with_name(self.path.name.replace(".open.jsonl", ".jsonl"))
            os.replace(self.path, closed)
            self.path = closed

    @classmethod
    def read(cls, path) -> "Log":
        header, steps, events = None, [], []
        names = {f.name for f in fields(Step)}
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue                     # a line cut short by a crash
                if header is None:
                    header = d
                elif "step" in d:
                    steps.append(Step(**{k: v for k, v in d.items() if k in names}))
                else:
                    events.append(d)
        if header is None:
            raise ValueError(f"{path} is empty")
        log = cls(path, header, steps, events)
        for e in events:                         # what was done, onto the steps
            log._apply(e)
        return log

    def _apply(self, e: dict) -> None:
        i = e.get("i")
        if not isinstance(i, int) or not 0 <= i < len(self.steps):
            return
        s = self.steps[i]
        for k in ("trashed_to", "copied_size", "copied_mtime_ns", "tree", "dst"):
            if e.get(k) is not None:
                setattr(s, k, e[k])
        if e.get("done"):
            s.result = e["done"]
        elif e.get("left") and s.result not in DONE:
            s.result = "left alone: " + e["left"]

    @classmethod
    def find(cls, project: str, oid: str) -> "Log":
        if not ID_RE.match(oid or ""):
            raise Refused("That isn't one of this library's moves.")
        for name in (f"{project}-{oid}.jsonl", f"{project}-{oid}.open.jsonl"):
            p = log_dir() / name
            if p.is_file():
                return cls.read(p)
        raise Refused("That isn't one of this library's moves.")

    def partial(self, i: int) -> str | None:
        return next((e["partial"] for e in self.events if e.get("i") == i and e.get("partial")), None)

    @classmethod
    def peek(cls, path) -> "Log":
        """The header and the last lines only (how it ended, whether it was moved back): enough for
        the list, without reading every step of a big move."""
        with open(path, "rb") as fh:
            header = json.loads(fh.readline())
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            fh.seek(max(0, end - 65536))
            tail = fh.read().decode("utf-8", "replace").splitlines()[1:]
        events = []
        for line in tail:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and "step" not in d and "v" not in d:
                events.append(d)
        return cls(path, header, [], events)

    @property
    def moved_back_by(self) -> str | None:
        return next((e["moved_back_by"] for e in reversed(self.events) if e.get("moved_back_by")), None)

    @property
    def summary(self) -> dict:
        return next((e for e in reversed(self.events) if e.get("finished")), {})


def history(project: str, limit: int = 100) -> list[dict]:
    """This library's moves and renames, newest first: what each did, and whether it can be moved back."""
    d = log_dir()
    if not d.is_dir():
        return []
    pat = re.compile(rf"^{re.escape(project)}-(\d{{8}}-\d{{6}}-[0-9a-f]{{6}})(\.open)?\.jsonl$")
    logs = []
    for f in d.iterdir():
        if pat.match(f.name):
            try:
                logs.append(Log.peek(f))
            except (OSError, ValueError):
                continue
    logs.sort(key=lambda g: g.header.get("created") or 0, reverse=True)
    out = []
    for log in logs[:limit]:
        f, h, s = log.path, log.header, log.summary
        state = "stopped" if log.open else "moved back" if log.moved_back_by else "done" if s.get("files") else "nothing moved"
        out.append({"id": log.id, "ts": h.get("created"), "op": h.get("op"), "label": h.get("label"), "what": h.get("what"),
                    "target": h.get("target"), "source": h.get("source"), "same_drive": h.get("same_drive"),
                    "photos": s.get("photos", h.get("photos")), "files": s.get("files", 0), "bytes": h.get("bytes"),
                    "left": s.get("left", 0), "recovered": bool(s.get("recovered")), "undoes": h.get("undoes"),
                    "moved_back_by": log.moved_back_by, "state": state, "can_move_back": state == "done", "log": f.name})
    return out


# ---------------------------------------------------------------- doing it

def rename_excl(src: str, dst: str) -> None:
    """Rename src to dst only if nothing is at dst, checked by the system in the same step. A name
    changed only in case (phuket -> Phuket, on a drive that ignores case) is the same file: a plain rename."""
    from .history import _rename_excl

    if src != dst and _same_name(os.path.basename(src), os.path.basename(dst)) and \
            os.path.dirname(src) == os.path.dirname(dst) and os.path.lexists(dst) and os.path.samefile(src, dst):
        os.rename(src, dst)
        return
    try:
        _rename_excl(src, dst)
    except OSError as e:
        if e.errno not in (errno.ENOTSUP, errno.EINVAL) or os.path.lexists(dst):
            raise
        os.rename(src, dst)          # a drive that can't check in the same step: checked just above


def _full_sync(fd: int) -> None:
    """Flush to the drive itself, not just its cache (F_FULLFSYNC on macOS)."""
    import fcntl

    try:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    except (AttributeError, OSError):
        os.fsync(fd)


def _sync_dir(folder: str) -> None:
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _read_back(path: str) -> str:
    """The whole content of a file read again from the drive (not from memory, where the system allows)."""
    import fcntl

    h = hashlib.blake2b(digest_size=20)
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
        except (AttributeError, OSError):
            pass
        while buf := os.read(fd, CHUNK):
            h.update(buf)
    finally:
        os.close(fd)
    return h.hexdigest()


def copy_checked(src: str, dst: str) -> str:
    """Copy src to a new file dst: the content, its extended attributes (Finder tags and the like),
    permissions and modified time; flushed to the drive, then read back and compared with what was
    read from src. Returns the content hash (as hashing.full_hash makes it). Raises OSError if
    anything differs, and dst (JoeyLab's own unfinished copy) is removed."""
    before = os.lstat(src)
    h = hashlib.blake2b(digest_size=20)
    fin = os.open(src, os.O_RDONLY)
    try:
        fout = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        os.close(fin)
        raise
    try:
        try:
            while buf := os.read(fin, CHUNK):
                h.update(buf)
                view = memoryview(buf)
                while view:
                    view = view[os.write(fout, view):]
            if sys.platform == "darwin":
                try:
                    import posix

                    posix._fcopyfile(fin, fout, 1 << 2)       # COPYFILE_XATTR
                except (AttributeError, OSError):
                    pass
            os.fchmod(fout, stat.S_IMODE(before.st_mode))
            _full_sync(fout)
        finally:
            os.close(fout)
            os.close(fin)
        os.utime(dst, ns=(before.st_atime_ns, before.st_mtime_ns))
        _sync_dir(os.path.dirname(dst))
        after = os.lstat(src)
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise OSError(errno.EAGAIN, "it changed while it was being copied")
        if os.lstat(dst).st_size != before.st_size or _read_back(dst) != h.hexdigest():
            raise OSError(errno.EIO, "the copy didn't match the original when read back")
        return h.hexdigest()
    except BaseException:
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise


@contextmanager
def _lock(project: str, wait: float, on_wait: Callable[[], None] | None):
    """The library's update lock, which scans take too: the folder watcher's scan waits for the move."""
    try:
        cm = locks.project_lock(project)
        cm.__enter__()
    except locks.Busy:
        if not wait:
            raise
        if on_wait:
            on_wait()
        cm = locks.project_lock(project, wait=wait)
        cm.__enter__()
    try:
        yield
    finally:
        cm.__exit__(None, None, None)


def run(conn, plan: Plan, keep_both: bool = False, progress: Callable[[int, int], None] | None = None,
        wait: float = 0.0, on_wait: Callable[[], None] | None = None) -> dict:
    """Do what the plan says: names that are taken are skipped, or kept with " 2" with keep_both.
    Each unit is checked again just before it moves. Returns what happened."""
    if plan.refused:
        raise Refused(plan.refused)
    steps = plan.steps + ([s for c in plan.conflicts for s in c["steps"]] if keep_both else [])
    skipped = list(plan.skipped) + ([] if keep_both else
                                    [{"what": c["name"], "why": "something of that name is there already"} for c in plan.conflicts])
    steps.sort(key=lambda s: s.unit)
    if not steps:
        raise Refused("There's nothing to do: " + (skipped[0]["why"] if skipped else "nothing chosen") + ".")
    if plan.op in ("move_photos", "rename_photos"):
        plan.photos, plan.files, plan.bytes = len({s.unit for s in steps}), len(steps), sum(s.size for s in steps)
    with _lock(plan.project, wait, on_wait):
        recover(conn, plan.project)
        log = Log.start(plan, steps, plan.photos)
        try:
            res = _execute(conn, plan, log, steps, progress)
        except BaseException:
            recover(conn, plan.project)            # finish or roll back what was under way, from the log
            raise
        res["skipped"] = skipped + res["skipped"]
        log.finish(photos=res["photos"], files=res["files"], left=len(res["skipped"]))
        if plan.undoes:
            try:
                Log.find(plan.project, plan.undoes).note(moved_back_by=log.id)
            except (Refused, OSError):
                pass
    res.update(id=log.id, label=plan.done_label(), what=plan.what(), log=str(log.path))
    return res


def _units(steps: list[Step]) -> list[list[tuple[int, Step]]]:
    by: dict[int, list] = defaultdict(list)
    for i, s in enumerate(steps):
        by[s.unit].append((i, s))
    return [by[u] for u in sorted(by)]


def _execute(conn, plan: Plan, log: Log, steps: list[Step], progress) -> dict:
    res = {"photos": 0, "files": 0, "bytes": 0, "skipped": [], "trash_failed": [], "folder": None, "stale_links": 0,
           "ids": []}
    units = _units(steps)
    rs = roots(conn)
    for k, unit in enumerate(units):
        if progress:
            progress(k, len(units))
        first = unit[0][1]
        if plan.op == "move_back":                 # the folder it came from may have gone since: made again
            for _, s in unit:
                parent = os.path.dirname(s.dst)
                if s.kind != "rmdir" and not os.path.isdir(parent) and root_of(parent, rs):
                    os.makedirs(parent, exist_ok=True)
        why = _recheck(conn, unit)
        if not why:
            try:
                if first.kind == "mkdir":
                    _make(unit, log)
                elif first.kind == "rmdir":
                    _unmake(unit, log)
                elif first.kind == "folder":
                    _folder(conn, unit[0], log, res)
                elif first.put_back:
                    _put_back(conn, unit, log)
                elif first.copy:
                    _copy_unit(conn, unit, log, res)
                else:
                    _rename_unit(conn, unit, log)
            except OSError as e:
                why = e.strerror or str(e)
        if why:
            for i, s in unit:
                if s.result not in DONE:
                    log.note(i=i, left=why)
            res["skipped"].append({"what": os.path.basename(first.src), "why": why})
            continue
        done = [s for _, s in unit if s.result in DONE]
        if done:
            res["photos"] += 1
            res["files"] += len(done)
            res["bytes"] += sum(s.size for s in done)
            res["ids"] += sorted({s.shot_id for s in done if s.shot_id is not None})
    if progress:
        progress(len(units), len(units))
    if plan.op in ("rename_folder", "move_folder") and steps[0].result in DONE:
        res["photos"] = plan.photos
        res["folder"] = steps[0].dst
        res["stale_links"] = volumes.stale_links(conn, steps[0].src)
    if plan.op == "move_back" and steps and steps[0].kind == "folder" and steps[0].result in DONE:
        res["folder"], res["photos"] = steps[0].dst, plan.photos
    _after(conn)
    return res


def _recheck(conn, unit: list[tuple[int, Step]]) -> str | None:
    """Why this unit can't go now, checked again just before it moves."""
    for _, s in unit:
        if s.kind == "rmdir":
            continue
        if s.kind == "mkdir":
            if os.path.lexists(s.dst):
                return "something of that name is there already"
            if not os.path.isdir(os.path.dirname(s.dst)):
                return "the folder it goes in isn't there any more"
            continue
        if s.put_back and not os.path.lexists(s.put_back):
            return "the original isn't in the Trash any more"
        if s.kind == "folder":
            if not os.path.isdir(s.src):
                return "the folder isn't there any more"
        else:
            try:
                st = os.lstat(s.src)
            except OSError:
                if s.file_id is None:
                    continue                      # a sidecar that went with its file, or is gone
                return "not there any more"
            if (st.st_size, st.st_mtime_ns) != (s.size, s.mtime_ns):
                return "changed since the plan was made"
            if s.file_id is not None:
                r = conn.execute("SELECT path FROM files WHERE id=?", (s.file_id,)).fetchone()
                if not r or r[0] != s.src:
                    return "moved since the plan was made"
        if os.path.lexists(s.dst) and not (os.path.lexists(s.src) and os.path.samefile(s.src, s.dst)):
            return "something of that name is there now"
        if not os.path.isdir(os.path.dirname(s.dst)):
            return "the folder it goes to isn't there any more"
    return None


def _follow_file(conn, s: Step, digest: str | None = None, copied: bool = False) -> None:
    """The catalog follows one file to its new place."""
    if s.file_id is None:
        return
    st = os.lstat(s.dst)
    r = conn.execute("SELECT size, mtime_ns FROM files WHERE id=?", (s.file_id,)).fetchone()
    if copied and r and (r["size"], r["mtime_ns"]) == (s.size, s.mtime_ns):
        # A copy on another drive: its own inode, and its time as that drive keeps it (some keep times
        # less finely); the same content, whose full hash is known now.
        conn.execute("UPDATE files SET path=?, ino=?, size=?, mtime_ns=?, full_hash=COALESCE(?, full_hash) WHERE id=?",
                     (s.dst, db.to_i64(st.st_ino), st.st_size, st.st_mtime_ns, digest, s.file_id))
    else:
        # A rename, or a file changed since the last scan: only its place follows (the next scan reads it again).
        conn.execute("UPDATE files SET path=?, ino=? WHERE id=?", (s.dst, db.to_i64(st.st_ino), s.file_id))
    conn.execute("UPDATE album_items SET source_path=? WHERE source_path=?", (s.dst, s.src))
    new_root = root_of(s.dst, roots(conn))
    if new_root and new_root != root_of(s.src, roots(conn)):
        conn.execute("UPDATE files SET root_id=(SELECT id FROM roots WHERE path=?) WHERE id=?", (new_root, s.file_id))


def _follow_shot(conn, unit: list[tuple[int, Step]]) -> None:
    main = next((s for _, s in unit if s.file_id is not None and s.result in DONE), None)
    if main and main.shot_id is not None:
        conn.execute("UPDATE shots SET dir=?, stem=? WHERE id=?",
                     (os.path.dirname(main.dst), os.path.splitext(os.path.basename(main.dst))[0], main.shot_id))


def _begin(conn) -> None:
    conn.commit()
    db.write_lock(conn)


def _rename_unit(conn, unit: list[tuple[int, Step]], log: Log) -> None:
    """One photo on one drive: each of its files renamed, never over anything, with the catalog
    following in the same transaction. If one of its files can't move, the ones moved go back."""
    _begin(conn)
    done: list[Step] = []
    try:
        for i, s in unit:
            if s.file_id is None and not os.path.lexists(s.src):
                if os.path.lexists(s.dst):        # a ._ file the system moved along with its file
                    s.result = "moved"
                    log.note(i=i, done="moved")
                continue
            rename_excl(s.src, s.dst)
            s.result = "moved"
            done.append(s)
            dst = _actual_name(s.dst)
            if dst != s.dst:
                s.dst = dst
                log.note(i=i, done="moved", dst=dst)
            else:
                log.note(i=i, done="moved")
            _follow_file(conn, s)
        _follow_shot(conn, unit)
        conn.commit()
    except OSError:
        stuck = False
        for s in reversed(done):
            try:
                rename_excl(s.dst, s.src)
                s.result = ""
            except OSError:
                stuck = True
        conn.commit() if stuck else conn.rollback()       # what couldn't go back keeps the catalog with it
        raise


def _copy_unit(conn, unit: list[tuple[int, Step]], log: Log, res: dict) -> None:
    """One photo to another drive: each file copied and checked first (under a hidden name); only
    when all are good do they take their names, the catalog follow, and the originals go to the Trash."""
    made, digests = [], {}
    try:
        for i, s in unit:
            if s.file_id is None and not os.path.lexists(s.src):
                continue
            partial = os.path.join(os.path.dirname(s.dst), f"{PARTIAL}{log.id}-{i}")
            log.note(i=i, partial=partial)
            digests[i] = copy_checked(s.src, partial)
            made.append((i, s, partial))
        _begin(conn)
        placed = []
        try:
            for i, s, partial in made:
                rename_excl(partial, s.dst)
                placed.append((s, partial))
                st = os.lstat(s.dst)
                s.copied_size, s.copied_mtime_ns = st.st_size, st.st_mtime_ns
                _follow_file(conn, s, digest=digests[i], copied=True)
                s.result = "copied"
            _follow_shot(conn, unit)
            conn.commit()
        except OSError:
            conn.rollback()
            for s, partial in placed:
                try:
                    rename_excl(s.dst, partial)
                except OSError:
                    pass
                s.result = ""
            raise
        for i, s, _ in made:
            log.note(i=i, done="copied", copied_size=s.copied_size, copied_mtime_ns=s.copied_mtime_ns)
    except OSError:
        for _, s, partial in made:
            if s.result != "copied" and os.path.lexists(partial):
                os.unlink(partial)            # JoeyLab's own unfinished copy; the original was never touched
        raise
    with cleanup.trash_lock(wait=120):
        for i, s, _ in made:
            try:
                s.trashed_to = cleanup.move_to_trash(s.src) or ""
                log.note(i=i, done="copied", trashed_to=s.trashed_to)
            except OSError as e:
                res["trash_failed"].append({"path": s.src, "why": str(e)})
                log.note(i=i, trash_failed=str(e))


def _put_back(conn, unit: list[tuple[int, Step]], log: Log) -> None:
    """Moving back from another drive while the originals are still in the Trash: each original goes
    back to its place (on its own drive: a rename), the catalog follows, then the copy goes to the Trash."""
    for _, s in unit:
        if s.copied_size is not None and os.path.lexists(s.src):
            st = os.lstat(s.src)
            if (st.st_size, st.st_mtime_ns) != (s.copied_size, s.copied_mtime_ns):
                raise OSError(errno.EBUSY, "the copy has changed since it was moved; move it back in Finder")
    _begin(conn)
    back: list[Step] = []
    try:
        for _, s in unit:
            rename_excl(s.put_back, s.dst)
            back.append(s)
            _follow_back(conn, s)
        conn.commit()
    except OSError:
        conn.rollback()
        for s in back:
            try:
                rename_excl(s.dst, s.put_back)
            except OSError:
                pass
        raise
    for _, s in unit:
        s.result = "put back"
    _follow_shot(conn, unit)
    conn.commit()
    with cleanup.trash_lock(wait=120):
        for i, s in unit:
            trashed = ""
            if os.path.lexists(s.src):
                try:
                    trashed = cleanup.move_to_trash(s.src) or ""
                except OSError as e:
                    log.note(i=i, trash_failed=str(e))
            log.note(i=i, done="put back", trashed_to=trashed)


def _follow_back(conn, s: Step) -> None:
    """The catalog follows an original put back from the Trash (it's the very file it was)."""
    if s.kind == "folder":
        _follow_folder(conn, s.src, s.dst)
        lo, hi = _range(s.dst)
        for fid, p in conn.execute("SELECT id, path FROM files WHERE path >= ? AND path < ?", (lo, hi)).fetchall():
            try:
                st = os.lstat(p)
            except OSError:
                continue
            conn.execute("UPDATE files SET ino=?, size=?, mtime_ns=? WHERE id=?", (db.to_i64(st.st_ino), st.st_size, st.st_mtime_ns, fid))
    elif s.file_id is not None:
        st = os.lstat(s.dst)
        conn.execute("UPDATE files SET path=?, ino=?, size=?, mtime_ns=? WHERE id=?",
                     (s.dst, db.to_i64(st.st_ino), st.st_size, st.st_mtime_ns, s.file_id))
        conn.execute("UPDATE album_items SET source_path=? WHERE source_path=?", (s.dst, s.src))


def _make(unit: list[tuple[int, Step]], log: Log) -> None:
    for i, s in unit:
        os.mkdir(s.dst)
        s.dst = _actual_name(s.dst)
        s.result = "made"
        log.note(i=i, done="made", dst=s.dst)


def _unmake(unit: list[tuple[int, Step]], log: Log) -> None:
    """Moving back a folder JoeyLab made: it goes to the Trash, if it's still empty."""
    for i, s in unit:
        if not os.path.isdir(s.src):
            s.result = "gone"
            log.note(i=i, left="it isn't there any more")
            continue
        empty, _, _ = cleanup._emptiness(s.src)
        if not empty:
            raise OSError(errno.ENOTEMPTY, "it isn't empty any more")
        with cleanup.trash_lock(wait=120):
            s.trashed_to = cleanup.move_to_trash(s.src) or ""
        s.result = "moved"
        log.note(i=i, done="moved", trashed_to=s.trashed_to)


def _follow_folder(conn, src: str, dst: str) -> None:
    """The catalog follows a folder and everything in it (a library folder itself, too)."""
    root = conn.execute("SELECT id FROM roots WHERE path=?", (src,)).fetchone()
    if root:
        volumes.relocate(conn, root[0], src, dst, commit=False)
        return
    volumes.rewrite_paths(conn, src, dst)
    new_root = root_of(dst, roots(conn))
    if new_root and new_root != root_of(src, roots(conn)):
        lo, hi = _range(dst)
        conn.execute("UPDATE files SET root_id=(SELECT id FROM roots WHERE path=?) WHERE path >= ? AND path < ?", (new_root, lo, hi))


def _folder(conn, item: tuple[int, Step], log: Log, res: dict) -> None:
    i, s = item
    if s.put_back:
        if s.tree is not None and _tree(s.src) != s.tree:
            raise OSError(errno.EBUSY, "files in it have changed since it was moved; move it back in Finder")
        _put_back(conn, [item], log)
        return
    if s.copy:
        _folder_copy(conn, i, s, log, res)
        return
    _begin(conn)
    try:
        rename_excl(s.src, s.dst)
    except OSError:
        conn.rollback()
        raise
    s.dst = _actual_name(s.dst)
    _follow_folder(conn, s.src, s.dst)
    conn.commit()
    s.result = "moved"
    log.note(i=i, done="moved", dst=s.dst)


def _copy_tree(src: str, stage: str) -> dict[str, str]:
    """Copy a folder into `stage` (a new, hidden folder), every file checked. Returns each file's
    content hash by its path inside."""
    os.mkdir(stage)
    digests = {}
    for dirpath, dirnames, filenames in os.walk(src):
        rel_dir = os.path.relpath(dirpath, src)
        out_dir = stage if rel_dir == "." else os.path.join(stage, rel_dir)
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                os.symlink(os.readlink(os.path.join(dirpath, d)), os.path.join(out_dir, d))
            else:
                os.mkdir(os.path.join(out_dir, d))
        for f in filenames:
            if f.startswith("._"):
                continue                  # carried as the file's extended attributes
            a, b = os.path.join(dirpath, f), os.path.join(out_dir, f)
            if os.path.islink(a):
                os.symlink(os.readlink(a), b)
                continue
            digests[os.path.normpath(os.path.join(rel_dir, f))] = copy_checked(a, b)
        _sync_dir(out_dir)
    return digests


def _folder_copy(conn, i: int, s: Step, log: Log, res: dict) -> None:
    """A folder to another drive: copied whole into a hidden folder beside where it goes, every file
    checked; then it takes its name, the catalog follows, and the original folder goes to the Trash."""
    stage = os.path.join(os.path.dirname(s.dst), f"{PARTIAL}{log.id}-{i}")
    log.note(i=i, partial=stage)
    before = _tree(s.src)
    try:
        digests = _copy_tree(s.src, stage)
        if _tree(s.src) != before:
            raise OSError(errno.EAGAIN, "files in it changed while it was being copied")
        _begin(conn)
        try:
            rename_excl(stage, s.dst)
        except OSError:
            conn.rollback()
            raise
    except OSError:
        shutil.rmtree(stage, ignore_errors=True)       # JoeyLab's own unfinished copy; the original was never touched
        raise
    olds = {p: (size, mt) for p, size, mt in conn.execute(
        "SELECT path, size, mtime_ns FROM files WHERE path >= ? AND path < ?", _range(s.src))}
    _follow_folder(conn, s.src, s.dst)
    for p, (size, mt) in olds.items():
        rel = os.path.relpath(p, s.src)
        new = os.path.join(s.dst, rel)
        try:
            st, was = os.lstat(new), os.lstat(p)
        except OSError:
            continue
        if (was.st_size, was.st_mtime_ns) == (size, mt):       # unchanged since the last scan: the copy is it
            conn.execute("UPDATE files SET ino=?, size=?, mtime_ns=?, full_hash=COALESCE(?, full_hash) WHERE path=?",
                         (db.to_i64(st.st_ino), st.st_size, st.st_mtime_ns, digests.get(rel), new))
    conn.commit()
    s.tree = _tree(s.dst)
    s.result = "copied"
    log.note(i=i, done="copied", tree=s.tree)
    with cleanup.trash_lock(wait=120):
        try:
            s.trashed_to = cleanup.move_to_trash(s.src) or ""
            log.note(i=i, done="copied", trashed_to=s.trashed_to)
        except OSError as e:
            res["trash_failed"].append({"path": s.src, "why": str(e)})
            log.note(i=i, trash_failed=str(e))


def _after(conn) -> None:
    """Photos are paired again, as a scan would, once their files have moved."""
    from . import scanner

    touched = scanner._pair_shots(conn)
    scanner._summarize_shots(conn, touched)


# ---------------------------------------------------------------- moving back

def plan_back(conn, project: str, oid: str) -> Plan:
    """Put back what an earlier operation moved, every step in reverse. Files moved to another drive
    come back from the Trash while the originals are still there (and the copies go to the Trash);
    otherwise they're copied back. A photo goes back whole or not at all."""
    log = Log.find(project, oid)
    plan = Plan("move_back", project, undoes=oid, undoes_label=log.header.get("label"))
    if log.open:
        plan.refused = "That one stopped half way and hasn't been finished yet: update the library first."
        return plan
    if log.moved_back_by:
        plan.refused = "That was moved back already."
        return plan
    done = [s for s in log.steps if s.result in DONE]
    if not done:
        plan.refused = "Nothing of that was moved, so there's nothing to move back."
        return plan
    plan.source, plan.target = log.header.get("target") or "", log.header.get("source") or ""
    rs = roots(conn)
    units: dict[int, list[Step]] = defaultdict(list)
    for s in done:
        units[s.unit].append(s)
    for u in sorted(units, reverse=True):
        back, why = [], None
        # Where the photo is now: a later rename of the folder it went to takes it along.
        now_in = None
        main = next((s for s in units[u] if s.file_id is not None and s.kind == "file"), None)
        if main:
            r = conn.execute("SELECT path FROM files WHERE id=?", (main.file_id,)).fetchone()
            if r and r[0] != main.dst:
                if os.path.basename(r[0]) != os.path.basename(main.dst):
                    plan.skipped.append({"what": os.path.basename(main.src), "why": "renamed again since: move that back first"})
                    continue
                now_in = os.path.dirname(r[0])
        for s in units[u]:
            if s.kind == "mkdir":
                back.append(Step(src=s.dst, dst=s.dst, kind="rmdir", unit=u))
                continue
            if s.kind == "rmdir":
                why = "a folder taken away can be put back from the Trash in Finder"
                break
            here = os.path.join(now_in, os.path.basename(s.dst)) if now_in else s.dst     # where it is now
            there = s.src                                                                  # where it goes back to
            try:
                st = os.lstat(here)
            except OSError:
                if s.file_id is None and s.kind == "file":
                    continue                       # a sidecar gone since (or a ._ file that went with its file)
                why = "not where it was moved to any more"
                break
            if s.file_id is not None:
                r = conn.execute("SELECT path FROM files WHERE id=?", (s.file_id,)).fetchone()
                if r and r[0] != here:
                    why = "moved again since: move that back first"
                    break
            if os.path.lexists(there) and not os.path.samefile(there, here):
                why = "something else has its old name now"
                break
            if not root_of(os.path.dirname(there), rs):
                why = "the folder it came from isn't in the library any more"
                break
            b = Step(src=here, dst=there, kind=s.kind, unit=u, shot_id=s.shot_id, file_id=s.file_id, size=st.st_size,
                     mtime_ns=st.st_mtime_ns, copied_size=s.copied_size, copied_mtime_ns=s.copied_mtime_ns, tree=s.tree)
            if s.result == "copied" and s.trashed_to and os.path.lexists(s.trashed_to):
                b.put_back = s.trashed_to
            elif s.result in ("copied", "put back"):
                parent = os.path.dirname(there)
                b.copy = not (os.path.isdir(parent) and same_drive(here, parent))
            back.append(b)
        if why:
            first = units[u][0]
            plan.skipped.append({"what": os.path.basename(first.src), "why": why})
            continue
        plan.steps += back
    folder = any(s.kind == "folder" for s in plan.steps)
    plan.photos = (log.header.get("photos") or 0) if folder else len({s.unit for s in plan.steps})
    plan.files = len(plan.steps)
    plan.bytes = sum(s.size for s in plan.steps)
    if not plan.steps:
        plan.refused = "None of it can be moved back: " + (plan.skipped[0]["why"] if plan.skipped else "nothing left") + "."
    return plan


# ---------------------------------------------------------------- after a crash

def recover(conn, project: str) -> list[dict]:
    """Bring every operation that stopped half way to a consistent end, from its log and what's on
    disk now; each move, and each update of the library, calls this first. A file that reached its
    new place stays there and the catalog follows it; one that didn't stays where it was. A copy to
    another drive that was put in place is checked again and finished (its original goes to the
    Trash); one cut short is removed (JoeyLab's own unfinished copy: its original was never
    touched). Returns what was done, per operation."""
    d = log_dir()
    if not d.is_dir():
        return []
    out = []
    for path in sorted(d.glob(f"{project}-*.open.jsonl")):
        try:
            log = Log.read(path)
        except (OSError, ValueError, TypeError, KeyError):
            continue
        out.append(_recover(conn, log))
    if out:
        _after(conn)
    return out


def _settled(s: Step) -> bool:
    return s.result in ("moved", "made", "put back") or s.result.startswith("left alone") or \
        (s.result == "copied" and s.trashed_to is not None)


def _arrived(s: Step) -> bool:
    """A rename on one drive happened: the new name is there and the old one isn't (for a change of
    case only, the folder lists the new spelling)."""
    if os.path.dirname(s.src) == os.path.dirname(s.dst) and _same_name(os.path.basename(s.src), os.path.basename(s.dst)):
        try:
            return os.path.basename(s.dst) in os.listdir(os.path.dirname(s.dst))
        except OSError:
            return False
    return os.path.lexists(s.dst) and not os.path.lexists(s.src)


def _remove_copy(path: str) -> None:
    """JoeyLab's own copy of something whose original is still in place."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.unlink(path)


def _trash_original(log: Log, i: int, s: Step) -> None:
    trashed = None
    try:
        with cleanup.trash_lock(wait=120):
            trashed = cleanup.move_to_trash(s.src) or ""
    except OSError as e:
        log.note(i=i, trash_failed=str(e))
    st = os.lstat(s.dst)
    log.note(i=i, done="copied", trashed_to=trashed, copied_size=st.st_size, copied_mtime_ns=st.st_mtime_ns)


def _recover(conn, log: Log) -> dict:
    """One stopped operation, a unit (a photo, a folder) at a time, so a photo's files end up
    together: in their new place if any of them got there, or else where they were."""
    finished = removed = 0
    for unit in _units(log.steps):
        for i, s in unit:                         # copies left unfinished under their hidden names
            partial = log.partial(i)
            if partial and os.path.lexists(partial):
                _remove_copy(partial)
                removed += 1
        first = unit[0][1]
        if not (first.copy or first.put_back or first.kind in ("mkdir", "rmdir")):
            # Renames on one drive: once any file of the photo got to its new name, the rest follow it,
            # and the catalog is brought up to date with each (also those logged as done: the process
            # may have stopped before the catalog's transaction was saved).
            if any(_arrived(s) for _, s in unit):
                for i, s in unit:
                    if not _arrived(s) and os.path.lexists(s.src) and not os.path.lexists(s.dst):
                        try:
                            rename_excl(s.src, s.dst)
                        except OSError:
                            pass
            for i, s in unit:
                if _arrived(s):
                    _follow_recovered(conn, s)
                    if s.result != "moved":
                        log.note(i=i, done="moved")
                        finished += 1
                elif s.result not in DONE:
                    log.note(i=i, left="it hadn't moved yet")
            continue
        pending = [(i, s) for i, s in unit if not _settled(s)]
        if not pending:
            continue
        if first.kind in ("mkdir", "rmdir"):
            for i, s in pending:
                if s.kind == "mkdir" and os.path.isdir(s.dst):
                    log.note(i=i, done="made")
                elif s.kind == "rmdir" and not os.path.lexists(s.src):
                    log.note(i=i, done="moved")
            continue
        if first.put_back:
            # Originals coming back from the Trash: the rest come back too (each a rename on its own
            # drive), then the copies go to the Trash.
            back = [(i, s) for i, s in pending if os.path.lexists(s.dst) and not os.path.lexists(s.put_back)]
            if back:
                for i, s in pending:
                    if (i, s) not in back and os.path.lexists(s.put_back) and not os.path.lexists(s.dst):
                        try:
                            rename_excl(s.put_back, s.dst)
                            back.append((i, s))
                        except OSError:
                            pass
                for i, s in back:
                    _follow_back(conn, s)
                    if os.path.lexists(s.src):
                        try:
                            with cleanup.trash_lock(wait=120):
                                cleanup.move_to_trash(s.src)
                        except OSError as e:
                            log.note(i=i, trash_failed=str(e))
                    log.note(i=i, done="put back")
                    finished += 1
            for i, s in pending:
                if (i, s) not in back:
                    log.note(i=i, left="it hadn't moved back yet")
            continue
        if first.copy:
            # Copies to another drive: finished if every file got there whole, else JoeyLab's copies
            # go and the originals stay (they were never touched).
            wanted = [(i, s) for i, s in pending if os.path.lexists(s.src) or os.path.lexists(s.dst)]
            placed = [(i, s) for i, s in wanted if os.path.lexists(s.dst) and
                      (not os.path.lexists(s.src) or _same_content(s.src, s.dst))]
            if placed and len(placed) == len(wanted):
                for i, s in placed:
                    _follow_recovered(conn, s, copied=True)
                    if os.path.lexists(s.src):
                        _trash_original(log, i, s)
                    else:
                        log.note(i=i, done="copied")
                    finished += 1
                continue
            for i, s in pending:
                if (i, s) in placed and os.path.lexists(s.src):
                    _remove_copy(s.dst)
                    removed += 1
                    log.note(i=i, left="it stopped before every file was copied; left where it was")
                elif (i, s) in placed:            # its original went to the Trash already: the copy is it now
                    _follow_recovered(conn, s, copied=True)
                    log.note(i=i, done="copied", trashed_to="")
                    finished += 1
                else:
                    log.note(i=i, left="it hadn't moved yet")
            continue
    conn.commit()
    log.finish(recovered=True, photos=len({s.unit for s in log.steps if s.result in DONE}),
               files=len([s for s in log.steps if s.result in DONE]), removed=removed)
    return {"id": log.id, "label": log.header.get("label"), "finished": finished, "removed": removed}


def _same_content(a: str, b: str) -> bool:
    from .hashing import full_hash

    try:
        if os.path.isdir(a):
            return os.path.isdir(b) and _tree(a) == _tree(b)
        return os.lstat(a).st_size == os.lstat(b).st_size and full_hash(a) == full_hash(b)
    except OSError:
        return False


def _follow_recovered(conn, s: Step, copied: bool = False) -> None:
    if s.kind == "folder":
        if _remembered_under(conn, s.src) or conn.execute("SELECT 1 FROM roots WHERE path=?", (s.src,)).fetchone():
            _follow_folder(conn, s.src, s.dst)
        return
    if s.file_id is None:
        return
    r = conn.execute("SELECT path FROM files WHERE id=?", (s.file_id,)).fetchone()
    if not r or r[0] == s.dst:
        return
    if conn.execute("SELECT 1 FROM files WHERE path=? AND id != ?", (s.dst, s.file_id)).fetchone():
        return                                    # a scan found it there first and keeps it
    _follow_file(conn, s, copied=copied)
    if s.shot_id is not None:
        conn.execute("UPDATE shots SET dir=? WHERE id=?", (os.path.dirname(s.dst), s.shot_id))
