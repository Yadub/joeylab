"""The History page: every step in the decision log (stars, picks, labels, keywords, dates, undos),
filterable, with the photos each changed; and the moves to the Trash, read from the Trash logs,
with Put Back for files still in the Trash.

Put Back is the one place the app moves a file without a typed count, so it only ever undoes a
move JoeyLab made: a file listed as moved in this library's own log, still the very file that went
(the same size, modified time and inode), from the Trash to the exact path it came from, inside one
of the library's folders, never over anything that is there now, and never during an update.
"""

from __future__ import annotations

import csv
import ctypes
import errno
import os
import re
import sys
import time
from pathlib import Path

from . import config

# Which steps a filter keeps: any of the step's rows changed this.
KINDS = {
    "picks": "json_extract(after, '$.pick') IS NOT json_extract(before, '$.pick')",
    "stars": "json_extract(after, '$.rating') IS NOT json_extract(before, '$.rating')",
    "labels": "COALESCE(json_extract(after, '$.label'), '') IS NOT COALESCE(json_extract(before, '$.label'), '')"
              " AND json_type(before, '$.label') IS NOT NULL",
    "keywords": "json_type(before, '$.keywords') IS NOT NULL",
    "dates": "json_type(before, '$.date') IS NOT NULL",
    "undo": "source = 'undo'",
}


def steps(conn, kind: str | None = None, since: float | None = None, until: float | None = None,
          offset: int = 0, limit: int = 50, sample: int = 8) -> dict:
    """One page of steps, newest first: {steps: [{batch, label, source, viewer, ts, n, picked,
    rejected, rated, undone, ids}], total}. `ids` is the first few photos each changed."""
    where, args = [], []
    if since is not None:
        where.append("ts >= ?")
        args.append(since)
    if until is not None:
        where.append("ts < ?")
        args.append(until)
    having = f" HAVING SUM({KINDS[kind]}) > 0" if kind in KINDS else ""
    w = (" WHERE " + " AND ".join(where)) if where else ""
    grouped = (f"SELECT batch, label, source, viewer, MIN(ts) AS ts, COUNT(DISTINCT shot_id) AS n, MIN(undone) AS undone,"
               " MAX(id) AS last,"
               " SUM(json_extract(after, '$.pick') = 1 AND json_extract(before, '$.pick') != 1) AS picked,"
               " SUM(json_extract(after, '$.pick') = -1 AND json_extract(before, '$.pick') != -1) AS rejected,"
               " SUM(json_extract(after, '$.rating') != json_extract(before, '$.rating')) AS rated"
               f" FROM decision_log{w} GROUP BY batch{having}")
    total = conn.execute(f"SELECT COUNT(*) FROM ({grouped})", args).fetchone()[0]
    rows = [dict(r) for r in conn.execute(f"{grouped} ORDER BY last DESC LIMIT ? OFFSET ?",
                                          [*args, max(1, min(int(limit), 200)), max(0, int(offset))])]
    by_batch = {r["batch"]: r for r in rows}
    for r in rows:
        r["ids"] = []
    if by_batch:
        q = ",".join("?" * len(by_batch))
        for b, sid in conn.execute(f"SELECT batch, shot_id FROM decision_log WHERE batch IN ({q}) ORDER BY id", list(by_batch)):
            ids = by_batch[b]["ids"]
            if len(ids) < sample and sid not in ids:
                ids.append(sid)
    from . import library

    redo = library._redo_target(conn)              # Redo works only straight after an Undo
    return {"steps": rows, "total": total, "redo": redo[0] if redo else None}


# ---------------------------------------------------------------- moves to the Trash

MOVED = "moved to the Trash"


def _logs_dir() -> Path:
    return config.HOME / "trash-log"


def _log_re(project: str):
    """A Trash log's name: <library>-files-<time>.tsv, with -2, -3... for a second run in the same second."""
    return re.compile(rf"^{re.escape(project)}-(files|folders)-(\d{{8}}-\d{{6}})(?:-(\d+))?\.tsv$")


def _int(v) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def _read(path: Path, what: str) -> list[dict]:
    """The moves a log lists: {path, now_at, bytes, inode, mtime_ns}. Logs from before inode and
    modified time were recorded have None for them."""
    out = []
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            if row.get("result") != MOVED or not row.get("now at"):
                continue
            path_key = "file" if what == "files" else "folder"
            size = (_int(row.get("bytes") or 0)) if what == "files" else None
            out.append({"path": row.get(path_key) or "", "now_at": row["now at"], "bytes": size,
                        "inode": _int(row.get("inode")), "mtime_ns": _int(row.get("modified ns"))})
    return out


def in_a_trash(path: str) -> bool:
    """Inside this account's Trash, or a drive's own Trash folder for this account."""
    real = os.path.realpath(path)
    home_trash = os.path.realpath(Path.home() / ".Trash")
    if real.startswith(home_trash + os.sep):
        return True
    return bool(re.match(rf"^/Volumes/[^/]+/\.Trashes/{os.getuid()}/", real))


def _state(item: dict) -> str:
    if os.path.lexists(item["now_at"]):
        return "in_trash"
    if os.path.lexists(item["path"]):
        return "back"
    return "gone"


def trash_moves(project: str, limit: int = 60) -> list[dict]:
    """This library's moves to the Trash, newest first, each with what's still in the Trash."""
    pat = _log_re(project)
    d = _logs_dir()
    logs = sorted((f for f in d.iterdir() if pat.match(f.name)),
                  key=lambda f: (pat.match(f.name).group(2), int(pat.match(f.name).group(3) or 1)),
                  reverse=True) if d.is_dir() else []
    out = []
    for f in logs[:limit]:
        what, stamp, _ = pat.match(f.name).groups()
        try:
            items = _read(f, what)
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
        if not items:
            continue
        for it in items:
            it["state"] = _state(it)
        out.append({"log": f.name, "what": what, "ts": time.mktime(time.strptime(stamp, "%Y%m%d-%H%M%S")),
                    "moved": len(items), "bytes": sum(it["bytes"] or 0 for it in items),
                    "in_trash": sum(it["state"] == "in_trash" for it in items), "items": items})
    return out


def _rename_excl(src: str, dst: str) -> None:
    """Move src to dst only if nothing is at dst (checked by the system in the same step)."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.renamex_np(os.fsencode(src), os.fsencode(dst), 0x00000004) != 0:      # RENAME_EXCL
            err = ctypes.get_errno()
            if err not in (errno.ENOTSUP, errno.EINVAL):
                raise OSError(err, os.strerror(err), dst)
            # A drive that can't check in the same step (exFAT under macOS's FSKit driver, like the
            # external SSD): check just before a plain rename instead.
            if os.path.lexists(dst):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)
            os.rename(src, dst)
        return
    if os.path.lexists(dst):
        raise FileExistsError(dst)
    os.rename(src, dst)


def _within(path: str, roots: list[str]) -> bool:
    real = os.path.realpath(os.path.dirname(path))
    return any(real == r or real.startswith(r + os.sep) for r in roots)


def put_back(conn, project: str, log: str, paths: list[str] | None = None, wait: float = 0.0) -> dict:
    """Move files (or folders) a Trash log lists back to where they were: only the ones still in the
    Trash and still the very ones that went there, only into this library's folders, never over
    something that is there now. A file put back is shown again at once (the next update agrees).
    Holds the library's update lock (waiting up to `wait` seconds for an update to finish), so a scan
    never sees it half done. Returns {moved, results: [{path, result}], log}."""
    from . import locks
    from .cleanup import trash_lock

    m = _log_re(project).match(log or "")
    if not m or not (_logs_dir() / log).is_file():
        raise ValueError("That isn't one of this library's Trash logs.")
    what = m.group(1)
    items = _read(_logs_dir() / log, what)
    if paths is not None:
        wanted = set(paths)
        items = [it for it in items if it["path"] in wanted]
        if not items:
            raise ValueError("Those aren't in that Trash log.")
    roots = [os.path.realpath(r[0]) for r in conn.execute("SELECT path FROM roots")]
    results, moved = [], 0
    try:
        lock = locks.project_lock(project, wait=wait)
        lock.__enter__()
    except locks.Busy:
        raise locks.Busy("An update of this library is running, so nothing was put back. Try again when it finishes.")
    try:
        scan = conn.execute("SELECT MAX(id) FROM scans").fetchone()[0]
        with trash_lock(wait=10):
            for it in items:
                src, dst = it["now_at"], it["path"]
                if not os.path.lexists(src):
                    why = "not in the Trash any more"
                elif not in_a_trash(src):
                    why = "left alone: it isn't in a Trash folder"
                elif not os.path.isabs(dst) or not _within(dst, roots):
                    why = "left alone: that place isn't inside this library's folders"
                elif not _same_as_moved(conn, src, it, what):
                    why = ("left alone: what's in the Trash under that name isn't the file moved from there, "
                           "or it has changed since")
                elif not os.path.isdir(os.path.dirname(dst)):
                    why = "left alone: the folder it came from isn't there any more"
                elif os.path.lexists(dst):
                    why = "left alone: something else is at that place now"
                else:
                    try:
                        _rename_excl(src, dst)
                        why = "put back"
                    except OSError as e:
                        why = f"left alone: {e.strerror or e}"
                if why == "put back":
                    moved += 1
                    if what == "files":
                        _present(conn, dst, scan)
                results.append({"path": dst, "result": why})
        conn.commit()
    finally:
        lock.__exit__(None, None, None)
    _log_put_back(project, results)
    return {"moved": moved, "results": results, "log": log}


def _same_as_moved(conn, src: str, it: dict, what: str) -> bool:
    """Whether what's in the Trash at `src` is the file (or folder) the log says went there, and
    unchanged: never another of the same name moved to the Trash later (all of one camera's RAWs can
    have one size). Logs record its size, modified time and inode; inode numbers are compared only
    where the drive keeps real ones, and device numbers never (they change when a drive is plugged in
    again). Older logs record only the size, so the catalog's record of the file at its old place
    (kept, marked missing) stands in for the rest."""
    from . import db
    from .cleanup import real_ino

    try:
        st = os.lstat(src)
    except OSError:
        return False
    if what == "folders":
        return not real_ino(it["inode"]) or st.st_ino == it["inode"]
    if it["bytes"] is not None and st.st_size != it["bytes"]:
        return False
    if it["mtime_ns"] is not None:
        return st.st_mtime_ns == it["mtime_ns"] and (not real_ino(it["inode"]) or st.st_ino == it["inode"])
    r = conn.execute("SELECT size, mtime_ns, ino FROM files WHERE path=?", (it["path"],)).fetchone()
    if r is None:
        return True                   # nothing in the catalog to compare with, and nothing of it to bring back
    if (st.st_size, st.st_mtime_ns) != (r["size"], r["mtime_ns"]):
        return False
    return not (r["ino"] is not None and r["ino"] > 0) or db.to_i64(st.st_ino) == r["ino"]


def _present(conn, path: str, scan: int | None) -> None:
    """A file put back is in the library again, as the next scan will find it."""
    conn.execute("UPDATE files SET status='present', last_seen=COALESCE(?, last_seen) WHERE path=?", (scan, path))
    conn.execute("UPDATE shots SET status='present' WHERE id IN (SELECT shot_id FROM files WHERE path=? AND shot_id IS NOT NULL)",
                 (path,))


def _log_put_back(project: str, results: list[dict]) -> None:
    d = _logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{project}-putback-{time.strftime('%Y%m%d-%H%M%S')}.tsv"
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))      # where your files were: yours alone
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t")
        if fh.tell() == 0:
            w.writerow(["file", "result"])
        for r in results:
            w.writerow([r["path"], r["result"]])
