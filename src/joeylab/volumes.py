"""Knowing a drive by its identity, not its name.

A folder on an external drive is remembered with the volume's UUID (exFAT and APFS keep it when the
drive is renamed) and the folder's path inside the drive. When the drive comes back under another
name ("/Volumes/Archive SSD" is now "/Volumes/Archive", or "/Volumes/Archive SSD 1" because two drives share a
name), the catalog's paths follow it: nothing on the drive changes. And when a *different* drive
turns up under the old name, its folders are not mistaken for yours: the library waits for the
real one.
"""

from __future__ import annotations

import os
import plistlib
import sqlite3
import subprocess
from pathlib import Path

VOLUMES = Path("/Volumes")
_cache: dict[tuple[str, int], dict] = {}


def _info(mount: str) -> dict:
    """diskutil's identity for a mounted volume (read-only)."""
    try:
        out = subprocess.run(["diskutil", "info", "-plist", mount], capture_output=True, timeout=15).stdout
        d = plistlib.loads(out) if out else {}
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        d = {}
    return {"uuid": d.get("VolumeUUID") or d.get("DiskUUID"), "name": d.get("VolumeName")}


def info(mount: str) -> dict:
    try:
        dev = os.stat(mount).st_dev
    except OSError:
        return {"uuid": None, "name": None}
    key = (mount, dev)
    if key not in _cache:
        _cache[key] = _info(mount)
    return _cache[key]


def split(path: str) -> tuple[str, str] | None:
    """("/Volumes/Archive SSD", "Photos/2024") for a path on an external drive; None otherwise."""
    base = str(VOLUMES).rstrip(os.sep) + os.sep
    if not path.startswith(base):
        return None
    rest = path[len(base):]
    name, _, rel = rest.partition(os.sep)
    return (base + name, rel) if name else None


def identify(path: str) -> dict | None:
    """{uuid, name, mount, rel} for a folder on an external drive that's mounted now."""
    sp = split(path)
    if not sp or not os.path.isdir(sp[0]):
        return None
    i = info(sp[0])
    if not i["uuid"]:
        return None
    return {"uuid": i["uuid"], "name": i["name"] or os.path.basename(sp[0]), "mount": sp[0], "rel": sp[1]}


# This Mac's own disk, as one drive whatever its volumes are called ("/" and the Data volume hold
# the same folders). A key, not a name: it never changes, so two libraries on the Mac's disk always
# agree that they're on one drive.
LOCAL = "mac"
LOCAL_NAME = "This Mac"


def mount_point(path: str) -> str | None:
    """The folder the drive holding `path` is mounted at (None if it isn't there)."""
    try:
        p = os.path.realpath(path)
        dev = os.stat(p).st_dev
    except OSError:
        return None
    while p != os.sep:
        parent = os.path.dirname(p)
        try:
            if os.stat(parent).st_dev != dev:
                break
        except OSError:
            break
        p = parent
    return p


def _local_dev(dev: int) -> bool:
    for p in (os.sep, "/System/Volumes/Data", os.path.expanduser("~")):
        try:
            if os.stat(p).st_dev == dev:
                return True
        except OSError:
            continue
    return False


def drive_of(path: str) -> dict | None:
    """{key, name, mount, rel} for the drive holding a folder that's there now: an external drive by
    its volume UUID (under /Volumes, or a disk image mounted anywhere), or this Mac's own disk (LOCAL).
    None when it can't be told for sure (not there, or a drive that gives no UUID)."""
    here = identify(path)
    if here:
        return {"key": here["uuid"], "name": here["name"], "mount": here["mount"], "rel": here["rel"]}
    if split(path):
        return None                     # under /Volumes, but the drive won't say who it is
    m = mount_point(path)
    if m is None:
        return None
    real = os.path.realpath(path)
    try:
        local = m == os.sep or _local_dev(os.stat(m).st_dev)
    except OSError:
        return None
    if local:
        return {"key": LOCAL, "name": LOCAL_NAME, "mount": os.sep, "rel": real.lstrip(os.sep)}
    i = info(m)
    if not i["uuid"]:
        return None
    return {"key": i["uuid"], "name": i["name"] or os.path.basename(m), "mount": m,
            "rel": os.path.relpath(real, m) if real != m else ""}


def root_drives(conn) -> dict[int, dict | None]:
    """Each library folder's drive: {root id: {key, name, base}} where base is the folder's path
    inside the drive, or None when its drive can't be told now (an unplugged disk image, say). A
    folder on an external drive is known by the UUID the catalog keeps for it, plugged in or not;
    one on a disk mounted elsewhere, by the drive it was on when last seen (REMEMBERED)."""
    from .db import get_setting

    out: dict[int, dict | None] = {}
    remembered = None
    for r in conn.execute("SELECT id, path, volume_uuid, volume_name, rel_path FROM roots").fetchall():
        if r["volume_uuid"]:
            sp = split(r["path"])
            base = r["rel_path"] if r["rel_path"] is not None else (sp[1] if sp else "")
            out[r["id"]] = {"key": r["volume_uuid"], "name": r["volume_name"] or (os.path.basename(sp[0]) if sp else r["path"]),
                            "base": base}
            continue
        d = drive_of(r["path"]) if os.path.isdir(r["path"]) else None
        if d:
            out[r["id"]] = {"key": d["key"], "name": d["name"], "base": d["rel"]}
            continue
        if remembered is None:
            try:
                remembered = get_setting(conn, REMEMBERED, {}) or {}
            except sqlite3.Error:
                remembered = {}
        m = remembered.get(str(r["id"])) if isinstance(remembered, dict) else None
        out[r["id"]] = {"key": m["key"], "name": m["name"], "base": m["base"]} \
            if isinstance(m, dict) and m.get("path") == r["path"] and m.get("key") else None
    return out


REMEMBERED = "root_drives"      # per library: {root id: {key, name, base, path}}, as last seen connected


def remember_drives(conn, drives: dict[int, dict | None], connected: dict[int, bool]) -> None:
    """Keep which drive each connected library folder is on, so it's still known while unplugged
    (a disk mounted outside /Volumes can't be asked who it is when it isn't there)."""
    from .db import get_setting, set_setting

    paths = {r[0]: r[1] for r in conn.execute("SELECT id, path FROM roots")}
    old = get_setting(conn, REMEMBERED, {}) or {}
    new = {k: v for k, v in old.items() if isinstance(v, dict) and k.isdigit() and int(k) in paths}
    for rid, d in drives.items():
        if d and connected.get(rid) and rid in paths:
            new[str(rid)] = {"key": d["key"], "name": d["name"], "base": d["base"], "path": paths[rid]}
    if new != old:
        set_setting(conn, REMEMBERED, new)


def drive_keys(conn) -> dict[int, str]:
    """{root id: drive key}, for telling copies on one drive from copies on another. A folder whose
    drive can't be told now counts as on this Mac's disk, as everything did before drives were told
    apart, or (under /Volumes) as on the drive of that name."""
    out = {}
    for rid, d in root_drives(conn).items():
        if d:
            out[rid] = d["key"]
        else:
            path = conn.execute("SELECT path FROM roots WHERE id=?", (rid,)).fetchone()[0]
            sp = split(path)
            out[rid] = f"name:{os.path.basename(sp[0])}" if sp else LOCAL
    return out


def rel_on_drive(path: str, root_path: str, base: str) -> str:
    """A file's path inside its drive, from its library folder's path and that folder's place on the drive."""
    inside = path[len(root_path.rstrip(os.sep)) + 1:] if path.startswith(root_path.rstrip(os.sep) + os.sep) else os.path.basename(path)
    return os.path.join(base, inside) if base else inside


def mounted() -> dict[str, str]:
    """{volume uuid: mount point} for every drive under /Volumes."""
    out: dict[str, str] = {}
    try:
        names = sorted(os.listdir(VOLUMES))
    except OSError:
        return out
    for n in names:
        m = str(VOLUMES / n)
        if os.path.islink(m) or not os.path.isdir(m):       # "Macintosh HD" is a link to /
            continue
        u = info(m)["uuid"]
        if u:
            out.setdefault(u, m)
    return out


# Catalog columns holding paths to originals, rewritten when a drive's or a folder's name changes.
PATH_COLUMNS = (("files", "path"), ("shots", "dir"), ("keep_folders", "folder"), ("selections", "folder"),
                ("selections", "parent"), ("albums", "folder"), ("albums", "parent"), ("album_items", "source_path"),
                ("excluded_folders", "folder"))


def _rewrite(conn, table: str, col: str, old: str, new: str) -> int:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return 0
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in have:
        return 0
    pre = old.rstrip(os.sep) + os.sep
    return conn.execute(f"UPDATE {table} SET {col} = ? || substr({col}, ?) WHERE {col} = ? OR substr({col}, 1, ?) = ?",
                        (new, len(old) + 1, old, len(pre), pre)).rowcount


def moved_path(path: str, old: str, new: str) -> str | None:
    """Where `path` is once the folder `old` is called `new` (None if it isn't inside it)."""
    if path == old:
        return new
    pre = old.rstrip(os.sep) + os.sep
    return new.rstrip(os.sep) + os.sep + path[len(pre):] if path.startswith(pre) else None


def rewrite_paths(conn, old: str, new: str) -> dict:
    """Point everything the catalog keeps by path at a folder's new path: files and photos, the
    folders and rules for which copy of a duplicate to keep, selections and albums, your names for
    folders, and smart albums filtered on the folder. Catalog only; the caller commits."""
    import json as _json

    counts = {f"{t}.{c}": _rewrite(conn, t, c, old, new) for t, c in PATH_COLUMNS}
    for key in ("folder_names", "keep_under"):          # {folder: your name} and ["always keep copies under" folders]
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        try:
            val = _json.loads(row[0]) if row and row[0] else None
        except ValueError:
            continue
        if isinstance(val, dict) and any(moved_path(k, old, new) for k in val):
            val = {(moved_path(k, old, new) or k): v for k, v in val.items()}
        elif isinstance(val, list) and any(isinstance(k, str) and moved_path(k, old, new) for k in val):
            val = sorted({(moved_path(k, old, new) or k) if isinstance(k, str) else k for k in val})
        else:
            continue
        conn.execute("UPDATE settings SET value=? WHERE key=?", (_json.dumps(val), key))
        counts[key] = 1
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='albums'").fetchone():
        for aid, q in conn.execute("SELECT id, query FROM albums WHERE kind='smart' AND query IS NOT NULL").fetchall():
            try:
                f = _json.loads(q)
            except ValueError:
                continue
            if not (isinstance(f, dict) and isinstance(f.get("folder"), str) and f["folder"]):
                continue
            folders = f["folder"].split("\n")           # one folder, or several, one per line
            moved = [moved_path(x, old, new) for x in folders]
            if any(moved):
                to = "\n".join(m or x for m, x in zip(moved, folders))
                conn.execute("UPDATE albums SET query=? WHERE id=?", (_json.dumps({**f, "folder": to}), aid))
                counts["smart_albums"] = counts.get("smart_albums", 0) + 1
    return counts


def rewrite_left_out(conn, old: str, new: str) -> int:
    """A folder the library leaves out (moved to another library) was renamed or moved in Finder: it's
    left out under its new path (and so is anything left out inside it). Catalog only; the caller commits."""
    return _rewrite(conn, "excluded_folders", "folder", old, new)


def stale_links(conn, old: str) -> int:
    """Links in album folders JoeyLab made that point inside `old` (they break when it moves)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='album_links'").fetchone():
        return 0
    pre = old.rstrip(os.sep) + os.sep
    return conn.execute("SELECT COUNT(*) FROM album_links WHERE target = ? OR substr(target, 1, ?) = ?",
                        (old, len(pre), pre)).fetchone()[0]


def relocate(conn, root_id: int, old: str, new: str, commit: bool = True) -> dict:
    """Point the catalog at a library folder's new path (the drive or the folder renamed). Catalog only."""
    counts = rewrite_paths(conn, old, new)
    sp = split(new)
    conn.execute("UPDATE roots SET path=?, rel_path=CASE WHEN volume_uuid IS NULL THEN rel_path ELSE ? END WHERE id=?",
                 (new, sp[1] if sp else None, root_id))
    stale = stale_links(conn, old)
    if commit:
        conn.commit()
    return {"from": old, "to": new, "paths": counts.get("files.path", 0), "stale_album_links": stale}


def _relocate_all(conn, moves: list[tuple[int, str, str]]) -> list[dict]:
    """Apply several moves at once. Two drives with one name can come back swapped (each folder's new
    path is the other's old one), so every folder first moves to a temporary path, then to its new
    one, in one transaction."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for rid, old, _ in moves:
            relocate(conn, rid, old, f"/.joeylab-moving-{rid}", commit=False)
        out = []
        for rid, old, new in moves:
            res = relocate(conn, rid, f"/.joeylab-moving-{rid}", new, commit=False)
            out.append({**res, "from": old})
        conn.commit()
        return out
    except Exception:
        conn.rollback()
        raise


def _renamed_in_place(conn) -> list[tuple[int, str, str]]:
    """Library folders renamed where they are (in Finder, say): the folder isn't at its path any more,
    but a folder beside it is the same folder (the same inode, recorded by the scan) and holds the
    library's files. Returns [(root id, old path, new path)]."""
    from .db import to_i64

    out = []
    for r in conn.execute("SELECT id, path, ino, volume_uuid FROM roots WHERE ino IS NOT NULL").fetchall():
        parent = os.path.dirname(r["path"])
        if os.path.lexists(r["path"]) or not os.path.isdir(parent):
            continue
        # On an external drive, only that drive: another one under its name is never taken for it.
        if r["volume_uuid"] and (identify(parent) or {}).get("uuid") != r["volume_uuid"]:
            continue
        try:
            same = [e.path for e in os.scandir(parent) if e.is_dir(follow_symlinks=False)
                    and to_i64(os.lstat(e.path).st_ino) == r["ino"]]
        except OSError:
            continue
        if len(same) == 1 and _holds_files_of(conn, r["id"], r["path"], same[0]):
            out.append((r["id"], r["path"], same[0]))
    return out


def _holds_files_of(conn, root_id: int, old: str, new: str, sample: int = 5) -> bool:
    """Some of the library's files under `old` are in `new` now, by the same name and size (so an
    unrelated folder that happens to have the old folder's inode number isn't taken for it)."""
    rows = conn.execute("SELECT path, size FROM files WHERE root_id=? AND status='present' ORDER BY id LIMIT ?",
                        (root_id, sample)).fetchall()
    for p, size in rows:
        to = moved_path(p, old, new)
        try:
            if to and os.lstat(to).st_size == size:
                return True
        except OSError:
            continue
    return False


def check_roots(conn) -> dict:
    """Run at the start of every scan. Remembers the drive of each external folder, follows a
    renamed drive, and holds back folders whose drive name now belongs to a different drive.
    Returns {"moved": [...], "wrong_drive": [root paths], "hold": {root ids not to scan}}."""
    out = {"moved": [], "wrong_drive": [], "hold": set()}
    renamed = _renamed_in_place(conn)
    if renamed:
        try:
            out["moved"] = [{**m, "kind": "folder"} for m in _relocate_all(conn, renamed)]
        except sqlite3.IntegrityError:          # the new name is tracked separately already: leave it to you
            conn.rollback()
    rows = conn.execute("SELECT id, path, volume_uuid, volume_name, rel_path FROM roots").fetchall()
    if not any(split(r["path"]) for r in rows):
        return out
    where = None
    moves: list[tuple[int, str, str, str]] = []
    for r in rows:
        sp = split(r["path"])
        if not sp:
            continue
        here = identify(r["path"]) if os.path.isdir(r["path"]) else None
        if here and not r["volume_uuid"]:
            conn.execute("UPDATE roots SET volume_uuid=?, volume_name=?, rel_path=? WHERE id=?",
                         (here["uuid"], here["name"], here["rel"], r["id"]))
            continue
        if not r["volume_uuid"]:
            continue
        if here and here["uuid"] == r["volume_uuid"]:
            if here["name"] != r["volume_name"]:
                conn.execute("UPDATE roots SET volume_name=? WHERE id=?", (here["name"], r["id"]))
            continue
        # The folder isn't where it was, or a different drive answers to the old name: look for ours.
        where = mounted() if where is None else where
        mount = where.get(r["volume_uuid"])
        if mount:
            rel = r["rel_path"] if r["rel_path"] is not None else sp[1]
            new = os.path.join(mount, rel) if rel else mount
            if os.path.isdir(new) and new != r["path"]:
                moves.append((r["id"], r["path"], new, info(mount)["name"] or os.path.basename(mount)))
                continue
        if here:          # the old name is mounted, but it's another drive
            out["wrong_drive"].append(r["path"])
            out["hold"].add(r["id"])
    conn.commit()
    if moves:
        try:
            out["moved"] += _relocate_all(conn, [(rid, old, new) for rid, old, new, _ in moves])
            for rid, _, _, name in moves:
                conn.execute("UPDATE roots SET volume_name=? WHERE id=?", (name, rid))
            conn.commit()
        except sqlite3.IntegrityError:      # a new path is tracked separately already: wait for you to sort it out
            for rid, old, _, _ in moves:
                out["wrong_drive"].append(old)
                out["hold"].add(rid)
    return out
