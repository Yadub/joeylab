"""Libraries as a whole: their names, checking a folder before it becomes one, and renaming or
removing one.

All of this is JoeyLab's own data (the catalog, thumbnails, previews and logs in its home). The
photo folders a library tracks are never touched: renaming a library renames JoeyLab's records of
it, and removing one keeps a backup of its catalog and deletes the rest of JoeyLab's copy.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
import unicodedata
from pathlib import Path

from . import config, db, locks


def slug(name: str) -> str:
    """A library name from whatever was typed: "Smith Wedding 2024" is "smith-wedding-2024" and
    "Café trip!" is "cafe-trip". Empty when nothing usable is left."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9_.]+", "-", s)
    s = re.sub(r"([-_.])[-_.]+", r"\1", s).strip("-_.")
    return s[:64].rstrip("-_.")


def name_problem(name: str) -> str | None:
    """Why a slug can't be a new library's name, or None."""
    if not name:
        return "Give the library a name with at least one letter or digit."
    if name in db.RESERVED_NAMES:
        return f"“{name}” is a word JoeyLab's web addresses use; choose another name."
    if (config.project_dir(name) / "catalog.db").exists():
        return f"There's already a library called “{name}”."
    return None


# ---------------------------------------------------------------- checking a folder

def _drive_missing(p: str) -> str | None:
    parts = p.split("/")
    if len(parts) > 2 and parts[1] == "Volumes" and not os.path.isdir("/Volumes/" + parts[2]):
        return parts[2]
    return None


def check_folder(path: str, budget_s: float = 2.0, enough: int = 20000, library: str | None = None) -> dict:
    """Can this folder be a library's (or be added to `library`), and about how many photos are in it?

    Counts photos (a RAW and a JPEG of one shot count once) and videos in the folder and its
    subfolders, as the scanner would, stopping after `budget_s` seconds or `enough` files: a quick
    look, not a scan. Returns {ok, path, reason, empty, photos, videos, partial, unreadable, note}.
    `reason` says, in plain words, why it can't be used; `empty` marks the one case that may still
    go ahead when asked (a folder photos will be copied into later). A folder belongs to one library:
    one that another library includes (or holds a folder of) is refused."""
    out = {"ok": False, "path": None, "reason": None, "empty": False, "photos": 0, "videos": 0,
           "partial": False, "unreadable": 0, "note": None}
    raw = (path or "").strip()
    if not raw:
        out["reason"] = "Choose the folder your photos are in: type its path, or press Browse…"
        return out
    if not (raw.startswith("/") or raw.startswith("~")):
        out["reason"] = "Give the folder's full path, for example /Volumes/Photos or ~/Pictures/2024."
        return out
    p = str(Path(raw).expanduser().resolve())
    out["path"] = p
    if not os.path.exists(p):
        drive = _drive_missing(p)
        out["reason"] = (f"The drive “{drive}” isn't connected. Plug it in, then try again." if drive
                         else f"There's no folder at {p}. Check the spelling, or choose it with Browse…")
        return out
    if not os.path.isdir(p):
        out["reason"] = f"{p} is a file, not a folder. Choose the folder it's in."
        return out
    home = str(config.HOME.resolve())
    if p == home or p.startswith(home + os.sep):
        out["reason"] = f"That's JoeyLab's own folder ({home}). Choose the folder your photos are in."
        return out
    try:
        with os.scandir(p):
            pass
    except PermissionError:
        out["reason"] = (f"macOS doesn't let JoeyLab read {p}. Allow it in System Settings › Privacy & Security › "
                         "Files and Folders (or Full Disk Access), then try again.")
        return out
    except OSError as e:
        out["reason"] = f"JoeyLab couldn't read {p} ({e.strerror or e})."
        return out
    why = overlap(p, claims(), skip=(library,) if library else ())
    if why:
        out["reason"] = why
        return out

    shots: set[tuple[str, str]] = set()
    videos = files = 0
    stop = time.monotonic() + budget_s

    def failed(_err: OSError) -> None:
        out["unreadable"] += 1

    for dirpath, dirnames, filenames in os.walk(p, onerror=failed):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and os.path.join(dirpath, d) != home]
        for name in filenames:
            if name.startswith("."):
                continue
            stem, ext = os.path.splitext(name)
            kind = config.kind_for(ext)
            if kind == "video":
                videos += 1
            elif kind:
                shots.add((dirpath, stem.lower()))
            files += 1
        if files >= enough or time.monotonic() > stop:
            out["partial"] = True
            break
    out["photos"], out["videos"] = len(shots), videos
    if not shots and not videos:
        out["empty"] = True
        out["reason"] = (f"No photos in {p} or its subfolders"
                         + (f" (and {out['unreadable']:,} subfolders macOS didn't let JoeyLab read)" if out["unreadable"] else "")
                         + ". JoeyLab reads JPEG, HEIC, RAW and video files.")
        return out
    out["ok"] = True
    return out


def _inside(p: str, root: str) -> bool:
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def _fold(p: str) -> str:
    return unicodedata.normalize("NFC", p).casefold()


def same_path(a: str, b: str) -> bool:
    """One path spelled two ways a Mac drive treats alike (case, or the Unicode form of an accent)."""
    return _fold(a) == _fold(b)


def within(p: str, folder: str) -> bool:
    """`p` is `folder` or inside it, whatever the case or accents."""
    return _inside(_fold(p), _fold(folder))


def claims(skip=()) -> dict[str, dict]:
    """Every library's folders and the folders it leaves out (moved to another library), read without
    opening (or upgrading) the catalogs for writing: {library: {roots, excluded}}."""
    out = {}
    for name in config.list_projects():
        if name in skip:
            continue
        try:
            conn = sqlite3.connect(f"file:{config.project_dir(name) / 'catalog.db'}?mode=ro", uri=True)
            try:
                roots = [r[0] for r in conn.execute("SELECT path FROM roots")]
                try:
                    excluded = [r[0] for r in conn.execute("SELECT folder FROM excluded_folders")]
                except sqlite3.OperationalError:          # not upgraded yet
                    excluded = []
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        out[name] = {"roots": roots, "excluded": excluded}
    return out


def holder(path: str, everyone: dict[str, dict], skip=()) -> tuple[str, str] | None:
    """The library that includes `path` (one of its folders holds it and doesn't leave it out), and
    that folder: (library, folder), or None."""
    for name, c in everyone.items():
        if name in skip:
            continue
        for r in c["roots"]:
            if within(path, r) and not any(within(path, e) for e in c["excluded"]):
                return name, r
    return None


def overlap(path: str, everyone: dict[str, dict], skip=(), left_out=()) -> str | None:
    """Why `path` can't become one of a library's folders, or None: another library already includes
    it, or holds a folder inside it (unless that folder is one of `left_out`, folders the library will
    leave out). A folder belongs to one library."""
    h = holder(path, everyone, skip)
    if h:
        where = "" if same_path(h[1], path) else f" (it's inside {h[1]})"
        return (f"The library “{h[0]}” already includes {path}{where}. A folder can be in one library only: to give it a "
                f"library of its own, move it from “{h[0]}” (Library › Folders, ⋯, Move to another library), which keeps "
                "its decisions, names and history.")
    for name, c in everyone.items():
        if name in skip:
            continue
        for r in c["roots"]:
            if not same_path(r, path) and within(r, path) and not any(within(r, x) for x in left_out):
                return f"{path} holds {r}, which is in the library “{name}”. A folder can be in one library only."
    return None


def describe(check: dict) -> str:
    """"About 1,240 photos and 12 videos", for a check that found some."""
    parts = [f"{check['photos']:,} photo{'' if check['photos'] == 1 else 's'}"] if check["photos"] else []
    if check["videos"]:
        parts.append(f"{check['videos']:,} video{'' if check['videos'] == 1 else 's'}")
    return ("At least " if check["partial"] else "") + " and ".join(parts)


# ---------------------------------------------------------------- renaming and removing

def _own_logs(name: str) -> list[Path]:
    """The trash, put-back and move logs written for this library (by their names)."""
    trash = re.compile(rf"^{re.escape(name)}-(files|folders|putback)-\d{{8}}-\d{{6}}(?:-\d+)?\.tsv$")
    moved = re.compile(rf"^{re.escape(name)}-\d{{8}}-\d{{6}}-[0-9a-f]{{6}}(\.open)?\.jsonl$")
    out = []
    for d, pat in ((config.HOME / "trash-log", trash), (config.HOME / "move-log", moved)):
        if d.is_dir():
            out += [f for f in d.iterdir() if pat.match(f.name)]
    return out


def _unfinished_move(name: str) -> bool:
    return any(f.name.endswith(".open.jsonl") for f in _own_logs(name))


def _check_existing(name: str) -> Path:
    pdir = config.project_dir(name)
    if not (pdir / "catalog.db").exists():
        raise FileNotFoundError(f"There's no library called “{name}”.")
    if pdir.is_symlink():
        raise ValueError(f"{pdir} is a link; JoeyLab only renames or removes its own folders.")
    if _unfinished_move(name):
        raise ValueError(f"A move in “{name}” stopped half way. Update the library once (JoeyLab finishes the "
                         "move), then try again.")
    return pdir


def rename(old: str, new: str) -> str:
    """Rename a library (its name becomes a slug). Only JoeyLab's own records move: the catalog
    folder, its backups and its logs. Returns the new name."""
    new = slug(new)
    pdir = _check_existing(old)
    if new == old:
        return new
    target = config.project_dir(new)
    same = target.exists() and os.path.samefile(target, pdir)       # only the case changes ("Trip" to "trip")
    why = None if same else name_problem(new)
    if why:
        raise ValueError(why)
    with locks.project_lock(old):           # never during an update (raises locks.Busy)
        os.rename(pdir, target)
        for base in (config.HOME / "backups", config.HOME / "xmp-backups"):
            if (base / old).is_dir() and not (base / new).exists():
                os.rename(base / old, base / new)
        for f in _own_logs(old):
            os.rename(f, f.with_name(new + f.name[len(old):]))
    from . import copies, split

    copies.rename_library(old, new)          # its files in the index every library shares
    split.rename_library(old, new)           # its moves to and from other libraries, and what they left out
    return new


def remove(name: str, confirm: str) -> dict:
    """Remove a library from JoeyLab: its catalog is backed up first, then JoeyLab's own copy
    (catalog, thumbnails, previews) is deleted. Its photo folders aren't touched. `confirm` must be
    the library's name, typed. Returns {kept: the backup folder, catalog: the catalog backup}."""
    pdir = _check_existing(name)
    if (confirm or "").strip() != name:
        raise ValueError(f"Type the library's name, “{name}”, to remove it.")
    with locks.project_lock(name):          # never during an update (raises locks.Busy)
        kept = config.HOME / "backups" / "removed" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
        kept.mkdir(mode=0o700, parents=True)
        catalog = kept / "catalog.db"
        src = sqlite3.connect(pdir / "catalog.db")
        try:
            out = sqlite3.connect(catalog)
            with out:
                src.backup(out)
            out.close()
        finally:
            src.close()
        config.private(catalog)
        if (config.HOME / "backups" / name).is_dir():
            os.rename(config.HOME / "backups" / name, kept / "backups")
        logs = _own_logs(name)
        if logs:
            (kept / "logs").mkdir()
            for f in logs:
                os.rename(f, kept / "logs" / f.name)
        shutil.rmtree(pdir)
    from . import copies, split

    copies.forget_library(name)              # its files no longer count as anyone's backups
    split.forget_library(name)               # nothing comes back from it
    return {"kept": str(kept), "catalog": str(catalog)}
