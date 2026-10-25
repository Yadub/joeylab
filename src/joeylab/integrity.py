"""Checking the promise: after an update, every tracked file and folder is exactly as it was.

The scan records each file's and folder's size, modified time, change time (any metadata change,
including extended attributes) and inode. After the update the same paths are checked again.
JoeyLab never writes there, so any difference means another app (or you) changed something
during the update, and it is reported with the paths rather than hidden.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

FIELDS = ("size", "modified", "metadata changed", "replaced")


def compare(before: dict[str, tuple]) -> dict:
    changed, gone = [], []
    for path, old in before.items():
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            gone.append(path)
            continue
        except OSError:
            continue
        now = (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
        if now != old:
            what = [FIELDS[i] for i in range(4) if now[i] != old[i]]
            changed.append({"path": path, "what": what})
    return {"checked": len(before), "changed": changed, "gone": gone}


def record(conn, result: dict) -> dict:
    ok = not result["changed"] and not result["gone"]
    details = {"changed": result["changed"][:200], "gone": result["gone"][:200]}
    conn.execute("INSERT INTO integrity(ts, checked, changed, details, ok) VALUES (?,?,?,?,?)",
                 (time.time(), result["checked"], len(result["changed"]) + len(result["gone"]), json.dumps(details), int(ok)))
    conn.execute("DELETE FROM integrity WHERE id NOT IN (SELECT id FROM integrity ORDER BY id DESC LIMIT 200)")
    conn.commit()
    return {"ok": ok, **result}


def latest(conn) -> dict | None:
    r = conn.execute("SELECT * FROM integrity ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    d = dict(r)
    d["details"] = json.loads(d["details"] or "{}")
    d["runs_ok"] = conn.execute("SELECT COUNT(*) FROM integrity WHERE ok=1").fetchone()[0]
    d["runs"] = conn.execute("SELECT COUNT(*) FROM integrity").fetchone()[0]
    return d


def mount_info(path: str) -> dict:
    """How the drive holding a folder is mounted: read-only and noatime mean the OS itself
    guarantees reading never changes anything (not even access times)."""
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    best = None
    real = os.path.realpath(path)
    mounts = {}
    for line in out.splitlines():
        if " on " not in line or " (" not in line:
            continue
        dev, rest = line.split(" on ", 1)
        mnt, _, opts = rest.rpartition(" (")
        mounts[mnt] = (dev, [o.strip() for o in opts.rstrip(")").split(",")])
        if real == mnt or real.startswith(mnt.rstrip("/") + "/"):
            if best is None or len(mnt) > len(best[1]):
                best = (dev, mnt)
    if not best:
        return {}
    dev, mnt = best
    data = "/System/Volumes/Data"
    if mnt == "/" and real != "/" and data in mounts and os.path.exists(data + real):
        mnt, dev = data, mounts[data][0]      # /Users and the like live on the Data volume, not the sealed "/"
    opts = mounts[mnt][1]
    fs = opts[0] if opts else ""
    try:                         # the OS's own answer for this folder's volume
        read_only = bool(os.statvfs(real).f_flag & os.ST_RDONLY)
    except OSError:
        read_only = "read-only" in opts or "rdonly" in opts
    return {"device": dev, "mount": mnt, "fs": fs, "read_only": read_only,
            "noatime": "noatime" in opts, "external": mnt.startswith("/Volumes/"),
            "how_to_read_only": (f"diskutil unmount \"{mnt}\" && diskutil mount readOnly {dev}"
                                 if mnt.startswith("/Volumes/") else None)}
