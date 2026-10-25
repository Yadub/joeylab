"""Dates you give photos that have none (old scans, files that lost their capture date), and times
you shift (a camera whose clock was wrong, when a whole camera can't be corrected at once): kept in
the catalog only, never written into the files. Each is a step in the history, undone like a rating;
an update keeps a date unless the file turns out to carry one of its own, and always keeps a shift."""

from __future__ import annotations

import calendar
import json
import re
import time
import uuid
from datetime import datetime

TABLE = "CREATE TABLE IF NOT EXISTS manual_dates (shot_id INTEGER PRIMARY KEY, capture_str TEXT NOT NULL, set_at REAL)"
# A photo's own clock shift, on top of its camera's correction (camera_offsets): eff_ts = capture_ts +
# camera offset + shift.
SHIFTS = "CREATE TABLE IF NOT EXISTS shot_offsets (shot_id INTEGER PRIMARY KEY, offset_s REAL NOT NULL, set_at REAL)"
_WHEN = re.compile(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?)?)?$")


def ensure(conn) -> None:
    conn.execute(TABLE)
    conn.execute(SHIFTS)


def parse(text: str) -> str:
    """ "1998", "1998-06", "1998-06-14" or "1998-06-14 09:30" as a capture time ("1998-06-01 12:00:00":
    a year or month alone means its first day, at noon). ValueError for anything else."""
    m = _WHEN.match((text or "").strip())
    if not m:
        raise ValueError("Write the date as a year (1998), a month (1998-06) or a day (1998-06-14).")
    y, mo, d, hh, mm = (int(x) if x else None for x in m.groups())
    try:
        when = datetime(y, mo or 1, d or 1, 12 if hh is None else hh, mm or 0)
    except ValueError:
        raise ValueError(f"There's no such date as {text.strip()}.")
    if not 1826 <= y <= datetime.now().year + 1:
        raise ValueError("That year is outside what a photo could be from.")
    return when.strftime("%Y-%m-%d %H:%M:%S")


def manual(conn, shot_ids: list[int]) -> dict[int, str]:
    """The dates given by hand to these photos."""
    ensure(conn)
    out: dict[int, str] = {}
    for k in range(0, len(shot_ids), 500):
        chunk = [int(x) for x in shot_ids[k : k + 500]]
        out.update(conn.execute(f"SELECT shot_id, capture_str FROM manual_dates WHERE shot_id IN ({','.join('?' * len(chunk))})",
                                chunk).fetchall())
    return out


def ts_of(capture_str: str) -> float:
    """Capture times are kept as the wall clock read as UTC, like a camera's."""
    return float(calendar.timegm(time.strptime(capture_str, "%Y-%m-%d %H:%M:%S")))


def apply(conn, sid: int, capture_str: str | None) -> None:
    """Give a photo this date, or (None) take a given date away so it's undated again."""
    from .scanner import local_wall_clock

    ensure(conn)
    if capture_str:
        conn.execute("INSERT INTO manual_dates(shot_id, capture_str, set_at) VALUES (?,?,?) ON CONFLICT(shot_id)"
                     " DO UPDATE SET capture_str=excluded.capture_str, set_at=excluded.set_at", (sid, capture_str, time.time()))
        ts = ts_of(capture_str)
        conn.execute("UPDATE shots SET capture_ts=?, capture_str=?, eff_ts=?, time_src='manual' WHERE id=?",
                     (ts, capture_str, ts, sid))
        return
    conn.execute("DELETE FROM manual_dates WHERE shot_id=?", (sid,))
    mt = conn.execute("SELECT MIN(mtime_ns) FROM files WHERE shot_id=? AND status='present'", (sid,)).fetchone()[0]
    conn.execute("UPDATE shots SET capture_ts=NULL, capture_str=NULL, eff_ts=?, time_src='file' WHERE id=?",
                 (local_wall_clock(mt / 1e9) if mt else None, sid))


def set_dates(conn, shot_ids: list[int], text: str | None, source: str = "web", viewer: str | None = None) -> dict:
    """Date undated photos (or change or clear a date given by hand), as one undoable step. Photos
    whose files carry a capture date are left as they are. Returns {batch, changed, kept}."""
    when = parse(text) if text else None
    ensure(conn)
    ids = list(dict.fromkeys(int(i) for i in shot_ids))
    src: dict[int, str | None] = {}
    for k in range(0, len(ids), 500):
        chunk = ids[k : k + 500]
        src.update(conn.execute(f"SELECT id, time_src FROM shots WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall())
    given = manual(conn, ids)
    batch, now, changed, kept = uuid.uuid4().hex[:12], time.time(), [], 0
    what = f"date: {when[:10]}" if when else "date taken away"
    for sid in ids:
        if sid not in src:
            continue
        if src[sid] not in ("file", "manual") and sid not in given:
            kept += 1                       # its own capture date wins
            continue
        before = given.get(sid)
        if before == when:
            continue
        apply(conn, sid, when)
        conn.execute("INSERT INTO decision_log(ts, batch, label, source, viewer, shot_id, before, after) VALUES (?,?,?,?,?,?,?,?)",
                     (now, batch, what, source, viewer, sid, json.dumps({"date": before}), json.dumps({"date": when})))
        changed.append(sid)
    conn.commit()
    return {"batch": batch if changed else None, "changed": changed, "kept": kept}


def shifts(conn, shot_ids: list[int] | None = None) -> dict[int, float]:
    """Each photo's own clock shift in seconds (all of them, or these photos'), where it has one."""
    ensure(conn)
    if shot_ids is None:
        return dict(conn.execute("SELECT shot_id, offset_s FROM shot_offsets").fetchall())
    out: dict[int, float] = {}
    for k in range(0, len(shot_ids), 500):
        chunk = [int(x) for x in shot_ids[k : k + 500]]
        out.update(conn.execute(f"SELECT shot_id, offset_s FROM shot_offsets WHERE shot_id IN ({','.join('?' * len(chunk))})",
                                chunk).fetchall())
    return out


def apply_shift(conn, sid: int, offset_s: float) -> None:
    """Give a photo with a capture date of its own this clock shift (0: none), and move it on the
    timeline: its camera time, plus its camera's correction, plus the shift."""
    from .grouping import camera_key

    ensure(conn)
    if abs(offset_s) < 0.5:
        conn.execute("DELETE FROM shot_offsets WHERE shot_id=?", (sid,))
        offset_s = 0.0
    else:
        conn.execute("INSERT INTO shot_offsets(shot_id, offset_s, set_at) VALUES (?,?,?) ON CONFLICT(shot_id)"
                     " DO UPDATE SET offset_s=excluded.offset_s, set_at=excluded.set_at", (sid, float(offset_s), time.time()))
    r = conn.execute("SELECT capture_ts, camera, body_serial FROM shots WHERE id=?", (sid,)).fetchone()
    if r is None or r[0] is None:
        return
    cam = conn.execute("SELECT offset_s FROM camera_offsets WHERE camera_key=?", (camera_key(r[1], r[2]),)).fetchone()
    conn.execute("UPDATE shots SET eff_ts=? WHERE id=?", (r[0] + (cam[0] if cam else 0.0) + offset_s, sid))


def shift_times(conn, shot_ids: list[int], seconds: float, source: str = "web", viewer: str | None = None) -> dict:
    """Move these photos' times by seconds (later when positive), as one undoable step: a photo with a
    capture date of its own gets a clock shift (kept by every update, added to its camera's
    correction); one dated by hand has that date moved; one with no date at all is left alone.
    Returns {batch, changed, undated}."""
    seconds = float(seconds)
    if not seconds or abs(seconds) > 2 * 365 * 86400:
        raise ValueError("Shift by up to two years, earlier or later.")
    ensure(conn)
    ids = list(dict.fromkeys(int(i) for i in shot_ids))
    src: dict[int, str | None] = {}
    for k in range(0, len(ids), 500):
        chunk = ids[k : k + 500]
        src.update(conn.execute(f"SELECT id, time_src FROM shots WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall())
    given, shifted = manual(conn, ids), shifts(conn, ids)
    batch, now, changed, undated = uuid.uuid4().hex[:12], time.time(), [], 0
    hours = seconds / 3600
    what = f"time shifted {'+' if seconds > 0 else '−'}{abs(hours):g} h" if abs(seconds) % 360 == 0 else \
        f"time shifted {'+' if seconds > 0 else '−'}{abs(seconds) / 60:g} min"
    for sid in ids:
        kind = src.get(sid)
        if kind == "manual" and sid in given:
            before = given[sid]
            after = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts_of(before) + seconds))
            apply(conn, sid, after)
            b, a = {"date": before}, {"date": after}
        elif kind == "exif":
            before_s = shifted.get(sid, 0.0)
            apply_shift(conn, sid, before_s + seconds)
            b, a = {"shift": before_s}, {"shift": before_s + seconds}
        else:
            if sid in src:
                undated += 1                    # nothing to move: give it a date first
            continue
        conn.execute("INSERT INTO decision_log(ts, batch, label, source, viewer, shot_id, before, after) VALUES (?,?,?,?,?,?,?,?)",
                     (now, batch, what, source, viewer, sid, json.dumps(b), json.dumps(a)))
        changed.append(sid)
    conn.commit()
    return {"batch": batch if changed else None, "changed": changed, "undated": undated}
