"""Several cameras on one timeline: find how far each body's clock is off.

Two cameras at the same wedding photograph the same moments. For every pair of bodies whose
photos overlap in time, near-identical frames (by their AI embeddings) are paired up and the
time differences collected; a clear, consistent difference is the clock offset. Offsets are
stored in the catalog only (EXIF is never rewritten) and applied to the shared timeline.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import scanner
from .grouping import camera_key

MIN_PHOTOS = 20
MATCH_COS = 0.90         # same moment from two cameras (different lens, angle) looks alike, not identical
WINDOW = 26 * 3600       # wide enough for a time zone left at home or an AM/PM mix-up
BIN = 120.0              # seconds; "the same moment" spans a minute or two across two shooters
MIN_PAIRS = 6


def _vectors(conn) -> dict[int, np.ndarray]:
    from . import embeddings

    return embeddings.whole_frame(conn)


_SUGGESTED: dict[str, tuple] = {}       # catalog -> (what it was worked out from, the suggestions)
_WORKING: set[str] = set()
_LOCK = threading.Lock()
FILE = "clocks.json"     # the last suggestions, beside the catalog: after a restart Home doesn't wait seconds for them


def _plain(stamp) -> list:
    """A stamp as it reads back from the file (lists, not tuples or rows), so the two compare."""
    return json.loads(json.dumps(stamp, default=list))


def _kept(db_file: str) -> tuple | None:
    try:
        d = json.loads((Path(db_file).parent / FILE).read_text(encoding="utf-8"))
        if d.get("format") == 1 and isinstance(d.get("suggestions"), list):
            return d["stamp"], d["suggestions"]
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass
    return None


def _keep(db_file: str, stamp: list, out: list) -> None:
    path = Path(db_file).parent / FILE
    tmp = path.with_name(f".{FILE}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump({"format": 1, "stamp": stamp, "suggestions": out}, f, separators=(",", ":"))
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        tmp.unlink(missing_ok=True)


def _inputs(conn) -> tuple:
    """Changes when anything suggest() reads does: the photos' vectors, which photos there are, their
    capture times and cameras, and the offsets already set (milliseconds, against seconds of matching;
    +status: one pass over the photos, not each looked up from the status index)."""
    from . import embeddings

    return (embeddings._stamp(conn), tuple(conn.execute(
        "SELECT COUNT(*), TOTAL(capture_ts), TOTAL(LENGTH(COALESCE(camera, '')) * 7 + LENGTH(COALESCE(body_serial, '')))"
        " FROM shots WHERE +status='present' AND +capture_ts IS NOT NULL AND COALESCE(media,'photo')='photo'").fetchone()),
        tuple(conn.execute("SELECT camera_key, offset_s FROM camera_offsets ORDER BY camera_key").fetchall()))


def suggest(conn, project: str | None = None) -> list[dict]:
    """Clock offsets that look likely (see _suggest), kept until what they're worked out from changes:
    Home and Tidy ask for them on every visit, and matching a big library's cameras takes seconds. With
    `project`, ones out of date are returned at once and worked out again in the background."""
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    stamp = _plain(_inputs(conn))
    hit = _SUGGESTED.get(db_file)
    if hit is None and project and db_file and (hit := _kept(db_file)):
        _SUGGESTED[db_file] = hit
    if hit and hit[0] == stamp:
        return [dict(x) for x in hit[1]]
    if hit and project:
        with _LOCK:
            start = db_file not in _WORKING
            _WORKING.add(db_file)
        if start:
            threading.Thread(target=_again, args=(project, db_file), name=f"clocks-{project}", daemon=True).start()
        return [dict(x) for x in hit[1]]
    out = _suggest(conn)
    _SUGGESTED[db_file] = (stamp, out)
    if project and db_file:
        _keep(db_file, stamp, out)
    return [dict(x) for x in out]


def _again(project: str, db_file: str) -> None:
    from . import db

    try:
        conn = db.connect(project, create=False)
        try:
            if conn.execute("PRAGMA database_list").fetchone()[2] == db_file:
                stamp = _plain(_inputs(conn))
                _SUGGESTED[db_file] = (stamp, out := _suggest(conn))
                _keep(db_file, stamp, out)
        finally:
            conn.close()
    except Exception:
        pass                                # the next visit tries again
    finally:
        with _LOCK:
            _WORKING.discard(db_file)


def _suggest(conn) -> list[dict]:
    vecs = _vectors(conn)
    if not vecs:
        return []
    bodies: dict[str, list] = defaultdict(list)
    for r in conn.execute("SELECT id, capture_ts, camera, body_serial FROM shots WHERE status='present'"
                          " AND capture_ts IS NOT NULL AND COALESCE(media,'photo')='photo'"):
        if r["id"] in vecs:
            bodies[camera_key(r["camera"], r["body_serial"])].append((r["capture_ts"], r["id"]))
    bodies = {k: sorted(v) for k, v in bodies.items() if len(v) >= MIN_PHOTOS}
    current = {r[0]: r[1] for r in conn.execute("SELECT camera_key, offset_s FROM camera_offsets")}
    keys = sorted(bodies, key=lambda k: -len(bodies[k]))
    out = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            ta = np.array([t for t, _ in bodies[a]])
            tb = np.array([t for t, _ in bodies[b]])
            if ta.min() > tb.max() + WINDOW or tb.min() > ta.max() + WINDOW:
                continue
            VB = np.stack([vecs[sid] for _, sid in bodies[b]])
            dts = []
            # Each photo of a against b's photos taken within a day of it; its best match only.
            for s in range(0, len(ta), 256):
                chunk = ta[s:s + 256]
                lo = int(np.searchsorted(tb, chunk[0] - WINDOW))
                hi = int(np.searchsorted(tb, chunk[-1] + WINDOW, side="right"))
                if hi - lo < 1:
                    continue
                VA = np.stack([vecs[sid] for _, sid in bodies[a][s:s + 256]])
                # The whole chunk at once: each photo's best match among b's photos within the window.
                S = np.where(np.abs(tb[None, lo:hi] - chunk[:, None]) <= WINDOW, VA @ VB[lo:hi].T, -1.0)
                c_ = S.argmax(axis=1)
                hit = S[np.arange(len(chunk)), c_] >= MATCH_COS
                dts.extend((tb[lo + c_[hit]] - chunk[hit]).tolist())
            if len(dts) < MIN_PAIRS:
                continue
            dts = np.array(dts)
            bins = np.round(dts / BIN)
            vals, counts = np.unique(bins, return_counts=True)
            # Smooth over neighbouring bins: one moment's matches straddle a boundary.
            smooth = np.array([counts[np.abs(vals - v) <= 1].sum() for v in vals])
            k = int(np.argmax(smooth))
            near = dts[np.abs(bins - vals[k]) <= 1]
            share = len(near) / len(dts)
            if len(near) < MIN_PAIRS or share < 0.25:
                continue
            offset = -float(np.median(near))       # add this to b's clock to line it up with a's
            if abs(offset) < 60:
                continue
            base = current.get(a, 0.0)
            if abs(offset + base - current.get(b, 0.0)) < 2 * BIN:
                continue                        # already applied
            out.append({"camera": b, "relative_to": a, "offset_s": round(offset + base, 1), "pairs": int(len(near)),
                        "share": round(share, 2), "current": current.get(b, 0.0)})
    return out


def set_offset(conn, key: str, offset_s: float, source: str = "user") -> int:
    if abs(offset_s) < 0.5:
        conn.execute("DELETE FROM camera_offsets WHERE camera_key=?", (key,))
    else:
        conn.execute("INSERT INTO camera_offsets(camera_key, offset_s, source, updated) VALUES (?,?,?,?)"
                     " ON CONFLICT(camera_key) DO UPDATE SET offset_s=excluded.offset_s, source=excluded.source,"
                     " updated=excluded.updated", (key, float(offset_s), source, time.time()))
    conn.commit()
    return scanner.apply_offsets(conn)


def cameras(conn) -> list[dict]:
    offsets = {r[0]: r[1] for r in conn.execute("SELECT camera_key, offset_s FROM camera_offsets")}
    out = []
    for r in conn.execute("SELECT camera, body_serial, COUNT(*) AS n, MIN(capture_str) AS a, MAX(capture_str) AS b"   # (one pass)
                          " FROM shots WHERE +status='present' AND +capture_ts IS NOT NULL GROUP BY camera, body_serial ORDER BY n DESC"):
        key = camera_key(r["camera"], r["body_serial"])
        out.append({"key": key, "camera": r["camera"] or "Unknown camera", "serial": r["body_serial"], "n": r["n"],
                    "from": r["a"], "to": r["b"], "offset_s": offsets.get(key, 0.0)})
    return out
