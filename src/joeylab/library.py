"""Shared queries: filtered shot listings and decision updates."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field


def like_escape(s: str) -> str:
    """Make a user string literal inside LIKE (folder names often contain _ )."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Photos by whether their files have a byte-identical copy on another drive (the catalog's `backups`
# table, kept from the index every library shares). A photo is backed up when all its files are.
_NO_COPY = "NOT EXISTS (SELECT 1 FROM backups b WHERE b.file_id = fb.id{})"
_FILES = "SELECT 1 FROM files fb WHERE fb.shot_id = s.id AND fb.status = 'present' AND "
BACKUP_SQL = {
    "elsewhere": f"NOT EXISTS ({_FILES}{_NO_COPY.format('')})",
    "checked": f"NOT EXISTS ({_FILES}{_NO_COPY.format(' AND b.checked = 1')})",
    "only": f"EXISTS ({_FILES}{_NO_COPY.format('')})",
}


@dataclass
class Filter:
    picks: bool = False
    rejects: bool = False
    undecided: bool = False
    min_rating: int | None = None
    suggestion: str | None = None      # pick | reject | check
    flag: str | None = None            # comma-separated: any of them
    no_flag: str | None = None         # comma-separated: none of them (e.g. hide "tiny")
    min_quality: float | None = None
    max_quality: float | None = None
    camera: str | None = None          # comma-separated: any of these cameras
    lens: str | None = None
    min_iso: float | None = None
    max_iso: float | None = None
    min_focal: float | None = None
    max_focal: float | None = None
    max_aperture: float | None = None  # f-number at most this (wide open)
    path_contains: str | None = None
    date_from: str | None = None       # YYYY-MM-DD
    date_to: str | None = None
    dir_prefix: str | None = None      # a folder and everything below it (several: one per line)
    person_id: int | None = None       # photos containing this named person
    people_all: list[int] | None = None  # photos with all of these people together
    cluster: int | None = None         # photos containing a face from this (unnamed) group
    album_id: int | None = None        # photos in this album (selections point at the originals)
    event_id: int | None = None
    trip_id: int | None = None
    place_id: int | None = None
    country: str | None = None         # ISO country code
    has_gps: bool | None = None
    media: str | None = None           # photo | video
    label: str | None = None           # color label
    edited: bool | None = None
    undated: bool | None = None
    favs_of: str | None = None         # a viewer's favourites
    c2pa: bool | None = None
    keyword: int | list[int] | None = None   # tagged with this keyword (or all of these), or anything under it
    not_keyword: list[int] | None = None     # tagged with none of these (nor anything under them)
    untagged: bool | None = None       # no keywords at all
    no_album: bool | None = None       # in no album (hand-picked or found on a drive; smart albums are filters)
    file_kind: str | None = None       # raw (RAW only) | jpeg (JPEG only) | pair (RAW + JPEG) | anyraw (has a RAW)
    raw: str | None = None             # what becomes of its RAW (raws.py): kept | removable | notneeded | ...
    backup: str | None = None          # elsewhere: every file has a copy on another drive | checked: ...compared byte
                                       # for byte | only: a file is on no other drive JoeyLab knows (copies.py)
    month: int | None = None           # taken in this month of any year (1-12)
    step: str | None = None            # the photos one step in the history changed
    ids: list[int] | None = field(default=None)
    include_missing: bool = False
    readable: bool = False             # leave out files that couldn't be read as photos at all

    def sql(self, scan: bool = False) -> tuple[str, list]:
        """The WHERE clause and its arguments. scan: the query reads most of the library anyway, so the
        photos are read in one pass rather than looked up one by one from the status index (nearly every
        photo is present, which the planner's sampled statistics don't always know)."""
        w, a = [], []
        if not self.include_missing:
            w.append("+s.status='present'" if scan else "s.status='present'")
        if self.readable:
            w.append("(s.error IS NULL OR s.analyzed_fp IS NOT NULL)")
        if self.picks:
            w.append("d.pick=1")
        if self.rejects:
            w.append("d.pick=-1")
        if self.undecided:
            w.append("(d.shot_id IS NULL OR (COALESCE(d.pick,0)=0 AND COALESCE(d.rating,0)=0))")
        if self.min_rating is not None:
            w.append("COALESCE(d.rating,0) >= ?")
            a.append(self.min_rating)
        if self.suggestion:
            w.append("m.suggestion = ?")
            a.append(self.suggestion)
        if self.flag:
            flags = [f for f in self.flag.split(",") if f]
            w.append("(" + " OR ".join("m.flags LIKE ?" for _ in flags) + ")")
            a += [f'%"{f}"%' for f in flags]
        if self.no_flag:
            for f in [f for f in self.no_flag.split(",") if f]:
                w.append("COALESCE(m.flags, '') NOT LIKE ?")
                a.append(f'%"{f}"%')
        if self.min_quality is not None:
            w.append("m.quality >= ?")
            a.append(self.min_quality)
        if self.max_quality is not None:
            w.append("m.quality <= ?")
            a.append(self.max_quality)
        cams = [c.strip() for c in (self.camera or "").split(",") if c.strip()]
        if cams:
            w.append("(" + " OR ".join("s.camera LIKE ? ESCAPE '\\' OR s.body_serial LIKE ? ESCAPE '\\'" for _ in cams) + ")")
            for c in cams:
                a += [f"%{like_escape(c)}%"] * 2
        if self.lens:
            w.append("s.lens LIKE ? ESCAPE '\\'")
            a.append(f"%{like_escape(self.lens)}%")
        for col, lo, hi in (("s.iso", self.min_iso, self.max_iso), ("s.focal", self.min_focal, self.max_focal)):
            if lo is not None:
                w.append(f"{col} >= ?")
                a.append(lo)
            if hi is not None:
                w.append(f"{col} <= ?")
                a.append(hi)
        if self.max_aperture is not None:
            w.append("s.aperture <= ?")
            a.append(self.max_aperture)
        if self.path_contains:
            w.append("s.dir LIKE ? ESCAPE '\\'")
            a.append(f"%{like_escape(self.path_contains)}%")
        if self.date_from:
            w.append("s.capture_str >= ?")
            a.append(self.date_from)
        if self.date_to:
            w.append("s.capture_str <= ?")
            a.append(self.date_to + " 99")
        if self.dir_prefix:                 # one folder, or several, one per line (a smart album of folders)
            dirs = [d for d in self.dir_prefix.split("\n") if d] or [self.dir_prefix]
            w.append("(" + " OR ".join("(s.dir = ? OR s.dir LIKE ? ESCAPE '\\')" for _ in dirs) + ")")
            for d in dirs:
                a += [d, like_escape(d.rstrip("/")) + "/%"]
        # One person's photos are found from their faces (indexed), not by checking every photo.
        if self.person_id is not None:
            w.append("s.id IN (SELECT shot_id FROM faces WHERE person_id = ?)")
            a.append(self.person_id)
        for pid in self.people_all or []:
            w.append("s.id IN (SELECT shot_id FROM faces WHERE person_id = ?)")
            a.append(int(pid))
        if self.cluster is not None:
            w.append("s.id IN (SELECT shot_id FROM faces WHERE cluster = ?)")
            a.append(self.cluster)
        if self.album_id is not None:
            w.append("s.id IN (SELECT shot_id FROM album_items WHERE album_id = ?)")
            a.append(self.album_id)
        if self.event_id is not None:
            w.append("s.event_id = ?")
            a.append(self.event_id)
        if self.trip_id is not None:
            w.append("s.event_id IN (SELECT id FROM events WHERE parent_id = ?)")
            a.append(self.trip_id)
        if self.keyword is not None or self.not_keyword:
            from .keywords import SUBTREE

            for kid in [self.keyword] if isinstance(self.keyword, int) else self.keyword or []:
                w.append(f"s.id IN (SELECT sk.shot_id FROM shot_keywords sk WHERE sk.keyword_id IN ({SUBTREE}))")
                a.append(int(kid))
            for kid in self.not_keyword or []:
                w.append(f"s.id NOT IN (SELECT sk.shot_id FROM shot_keywords sk WHERE sk.keyword_id IN ({SUBTREE}))")
                a.append(int(kid))
        if self.untagged:
            w.append("NOT EXISTS (SELECT 1 FROM shot_keywords sk WHERE sk.shot_id = s.id)")
        if self.no_album:
            w.append("NOT EXISTS (SELECT 1 FROM album_items ai WHERE ai.shot_id = s.id)")
        if self.file_kind in ("raw", "jpeg", "pair", "anyraw"):
            has = "EXISTS (SELECT 1 FROM files fk WHERE fk.shot_id = s.id AND fk.status='present' AND fk.kind = '{}')"
            raw, jpeg = has.format("raw"), has.format("jpeg")
            w.append({"raw": f"{raw} AND NOT {jpeg}", "jpeg": f"{jpeg} AND NOT {raw}", "pair": f"{raw} AND {jpeg}",
                      "anyraw": raw}[self.file_kind])
        if self.raw:
            from .raws import filter_sql

            w.append(filter_sql(self.raw) or "0")
        if self.backup:
            w.append(BACKUP_SQL.get(self.backup, "0"))
        if self.month:
            w.append("substr(s.capture_str, 6, 2) = ? AND COALESCE(s.time_src, 'exif') != 'file'")
            a.append(f"{int(self.month):02d}")
        if self.step:
            w.append("s.id IN (SELECT shot_id FROM decision_log WHERE batch = ?)")
            a.append(self.step)
        if self.place_id is not None:
            w.append("s.place_id = ?")
            a.append(self.place_id)
        if self.country:
            w.append("s.place_id IN (SELECT id FROM places WHERE cc = ?)")
            a.append(self.country.upper())
        if self.has_gps is not None:
            w.append("s.gps_lat IS NOT NULL" if self.has_gps else "s.gps_lat IS NULL")
        if self.media:
            w.append("COALESCE(s.media, 'photo') = ?")
            a.append(self.media)
        if self.label:
            w.append("d.label = ? COLLATE NOCASE")
            a.append(self.label)
        if self.edited is not None:
            w.append("COALESCE(s.edited, 0) = ?")
            a.append(int(self.edited))
        if self.undated is not None:
            w.append("s.capture_ts IS NULL" if self.undated else "s.capture_ts IS NOT NULL")
        if self.favs_of:
            # Several viewers, comma-separated: photos all of them loved.
            for v in [x for x in self.favs_of.split(",") if x]:
                w.append("EXISTS (SELECT 1 FROM viewer_decisions vd WHERE vd.shot_id = s.id AND vd.viewer = ? AND vd.fav = 1)")
                a.append(v)
        if self.c2pa is not None:
            w.append("s.c2pa IS NOT NULL" if self.c2pa else "s.c2pa IS NULL")
        if self.ids is not None:
            w.append(f"s.id IN ({','.join(str(int(i)) for i in self.ids) or 'NULL'})")
        return (" WHERE " + " AND ".join(w)) if w else "", a


SHOT_SELECT = """
SELECT s.*, m.quality, m.flags, m.suggestion, m.group_rank, m.sharp_subject, m.face_sharp,
       m.brightness, m.clip_high, m.main_faces, m.eyes_closed, m.smile, m.aesthetic,
       d.rating, COALESCE(d.pick, 0) AS pick, d.label,
       g.size AS group_size, f.path AS preview_path, f.kind AS preview_kind
FROM shots s
LEFT JOIN metrics m ON m.shot_id = s.id
LEFT JOIN decisions d ON d.shot_id = s.id
LEFT JOIN groups g ON g.id = s.group_id
LEFT JOIN files f ON f.id = s.preview_file_id
"""

SORTS = {
    "time": "s.eff_ts IS NULL, s.eff_ts, s.dir, s.stem",
    "quality": "m.quality DESC",
    "sharpness": "m.sharp_subject DESC",
    "path": "s.dir, s.stem",
    "rating": "COALESCE(d.rating,0) DESC, m.quality DESC",
}


def shots(conn, flt: Filter | None = None, sort: str = "time", limit: int | None = None) -> list[dict]:
    where, args = (flt or Filter()).sql()
    q = SHOT_SELECT + where + f" ORDER BY {SORTS.get(sort, SORTS['time'])}"
    if limit:
        q += f" LIMIT {int(limit)}"
    return [_row(r) for r in conn.execute(q, args)]


def shot(conn, shot_id: int) -> dict | None:
    r = conn.execute(SHOT_SELECT + " WHERE s.id=?", (shot_id,)).fetchone()
    return _row(r) if r else None


def _row(r) -> dict:
    d = dict(r)
    d["flags"] = json.loads(d["flags"]) if d.get("flags") else []
    return d


def files_of(conn, shot_id: int, present_only: bool = True) -> list[dict]:
    q = "SELECT * FROM files WHERE shot_id=?" + (" AND status='present'" if present_only else "")
    return [dict(r) for r in conn.execute(q + " ORDER BY kind", (shot_id,))]


LABELS = ("Red", "Yellow", "Green", "Blue", "Purple")   # Lightroom's color labels, keys 6-9 + 0


def _apply(conn, shot_id: int, rating, pick, label, now: float) -> tuple[dict, dict] | None:
    cur = conn.execute("SELECT rating, pick, label FROM decisions WHERE shot_id=?", (shot_id,)).fetchone()
    before = {"rating": cur["rating"] or 0, "pick": cur["pick"] or 0, "label": cur["label"] or ""} if cur else \
        {"rating": 0, "pick": 0, "label": ""}
    after = {"rating": before["rating"] if rating is None else int(rating),
             "pick": before["pick"] if pick is None else int(pick),
             "label": before["label"] if label is None else (label or "")}
    if after == before:
        return None
    conn.execute(
        "INSERT INTO decisions(shot_id, rating, pick, label, updated_at) VALUES (?,?,?,?,?)"
        " ON CONFLICT(shot_id) DO UPDATE SET rating=excluded.rating, pick=excluded.pick,"
        " label=excluded.label, updated_at=excluded.updated_at",
        (shot_id, after["rating"], after["pick"], after["label"] or None, now),
    )
    return before, after


def check_decision(rating=None, pick=None, label=None) -> tuple:
    """Values a decision may take (shared by the web app, the CLI and assistants over MCP).
    Returns them normalised; raises ValueError for anything else."""
    if rating is not None:
        if isinstance(rating, bool) or not isinstance(rating, (int, float)) or int(rating) != rating or not 0 <= rating <= 5:
            raise ValueError("Stars are a whole number from 0 to 5.")
        rating = int(rating)
    if pick is not None:
        if isinstance(pick, bool) or pick not in (-1, 0, 1):
            raise ValueError("pick is 1 (pick), -1 (reject) or 0 (neither).")
        pick = int(pick)
    if label is not None and label != "":
        match = [x for x in LABELS if x.lower() == str(label).lower()]
        if not match:
            raise ValueError(f"A label is one of {', '.join(LABELS)}.")
        label = match[0]
    return rating, pick, label


def decide(conn, items: list[dict], what: str = "change", source: str = "web", viewer: str | None = None,
           batch: str | None = None) -> tuple[str, list[int]]:
    """Apply decisions [{id, rating?, pick?, label?}] as one undoable step, logged. Values are checked
    first (ValueError); photos that don't exist are skipped. Returns (batch id, ids that changed)."""
    import uuid

    checked = []
    for it in items:
        rating, pick, label = check_decision(it.get("rating"), it.get("pick"), it.get("label"))
        checked.append((int(it["id"]), rating, pick, label))
    known = set()
    ids = [c[0] for c in checked]
    for k in range(0, len(ids), 500):
        chunk = ids[k : k + 500]
        known |= {r[0] for r in conn.execute(f"SELECT id FROM shots WHERE id IN ({','.join('?' * len(chunk))})", chunk)}
    batch = batch or uuid.uuid4().hex[:12]
    now = time.time()
    changed = []
    for sid, rating, pick, label in checked:
        if sid not in known:
            continue
        res = _apply(conn, sid, rating, pick, label, now)
        if res is None:
            continue
        before, after = res
        conn.execute("INSERT INTO decision_log(ts, batch, label, source, viewer, shot_id, before, after)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (now, batch, what, source, viewer, sid, json.dumps(before), json.dumps(after)))
        changed.append(sid)
    conn.commit()
    return batch, changed


def set_decision(conn, shot_id: int, rating: int | None = None, pick: int | None = None,
                 label: str | None = None, source: str = "cli", what: str = "change") -> None:
    decide(conn, [{"id": shot_id, "rating": rating, "pick": pick, "label": label}], what=what, source=source)


FIELDS = ("rating", "pick", "label")


def _current(conn, sid: int) -> dict:
    r = conn.execute("SELECT rating, pick, label FROM decisions WHERE shot_id=?", (sid,)).fetchone()
    return {"rating": (r["rating"] or 0) if r else 0, "pick": (r["pick"] or 0) if r else 0, "label": (r["label"] or "") if r else ""}


def _replay(conn, rows, back: bool, label: str, viewer: str | None, batch: str,
            source: str = "undo") -> tuple[list[int], int]:
    """Move a step's photos back to their "before" (undo) or forward to their "after", but only
    where nothing has changed them since: a later star or pick on the same photo is left alone and
    counted as skipped. Logged as a new step from `source` (undo or redo). Returns (photos changed,
    photos skipped)."""
    from . import keywords

    items, ids, skipped = [], [], 0
    now = time.time()
    for r in rows:
        b, a = json.loads(r["before"]), json.loads(r["after"])
        frm, to = (a, b) if back else (b, a)
        sid = r["shot_id"]
        if "date" in b:                       # a date given by hand (dating.py)
            from . import dating

            cur = dating.manual(conn, [sid]).get(sid)
            if cur != frm["date"]:
                skipped += 1                  # dated again since: that one stays
                continue
            dating.apply(conn, sid, to["date"])
            conn.execute("INSERT INTO decision_log(ts, batch, label, source, viewer, shot_id, before, after) VALUES (?,?,?,?,?,?,?,?)",
                         (now, batch, label, "undo", viewer, sid, json.dumps({"date": cur}), json.dumps({"date": to["date"]})))
            ids.append(sid)
            continue
        if "shift" in b:                      # a photo's own clock shift (dating.py)
            from . import dating

            cur = dating.shifts(conn, [sid]).get(sid, 0.0)
            if abs(cur - float(frm["shift"] or 0)) > 0.5:
                skipped += 1                  # shifted again since: that one stays
                continue
            dating.apply_shift(conn, sid, float(to["shift"] or 0))
            conn.execute("INSERT INTO decision_log(ts, batch, label, source, viewer, shot_id, before, after) VALUES (?,?,?,?,?,?,?,?)",
                         (now, batch, label, "undo", viewer, sid, json.dumps({"shift": cur}), json.dumps({"shift": to["shift"]})))
            ids.append(sid)
            continue
        if "raw" in b:                        # Keep RAW / RAW not needed (raws.py)
            from . import raws

            cur = raws.choices(conn, [sid]).get(sid, "")
            if cur != frm["raw"]:
                skipped += 1                  # chosen again since: that choice stays
                continue
            raws.put_choice(conn, sid, to["raw"])
            conn.execute("INSERT INTO decision_log(ts, batch, label, source, viewer, shot_id, before, after) VALUES (?,?,?,?,?,?,?,?)",
                         (now, batch, label, source, viewer, sid, json.dumps({"raw": cur}), json.dumps({"raw": to["raw"]})))
            ids.append(sid)
            continue
        if "keywords" in b:
            cur = set(x[0] for x in conn.execute("SELECT keyword_id FROM shot_keywords WHERE shot_id=?", (sid,)))
            add, drop = set(to["keywords"]) - set(frm["keywords"]), set(frm["keywords"]) - set(to["keywords"])
            new = sorted((cur - drop) | add)
            if new != sorted(cur):
                keywords.restore(conn, sid, new)
                conn.execute("INSERT INTO decision_log(ts, batch, label, source, viewer, shot_id, before, after) VALUES (?,?,?,?,?,?,?,?)",
                             (now, batch, label, source, viewer, sid, json.dumps({"keywords": sorted(cur)}), json.dumps({"keywords": new})))
                ids.append(sid)
            continue
        cur = _current(conn, sid)
        item, clash = {"id": sid}, False
        for f in FIELDS:
            if frm.get(f, "" if f == "label" else 0) == to.get(f, "" if f == "label" else 0):
                continue                      # this step didn't touch it
            if cur[f] == frm.get(f, "" if f == "label" else 0):
                item[f] = to.get(f, "" if f == "label" else 0)
            else:
                clash = True                  # changed again since: yours wins
        skipped += clash
        if len(item) > 1:
            items.append(item)
    if items:
        _, ch = decide(conn, items, what=label, source=source, viewer=viewer, batch=batch)
        ids += ch
    return ids, skipped


def undo(conn, viewer: str | None = None, batch: str | None = None, source: str | None = None) -> dict | None:
    """Reverse the latest step (or a given one) that hasn't been undone, without touching anything
    changed since on the same photos. A redo is a step of its own, so Undo straight after Redo
    reverses the redo. The undo is logged too, so the history shows it and Redo can reverse it.
    Returns {batch, label, ids, skipped} or None when there's nothing to undo. `source` limits it to
    changes from one place (the MCP server undoes only its own)."""
    if batch is None:
        q = "SELECT batch FROM decision_log WHERE undone=0 AND source != 'undo'"
        args: list = []
        if viewer:
            q += " AND COALESCE(viewer, '') = ?"
            args.append(viewer)
        if source:
            q += " AND source = ?"
            args.append(source)
        row = conn.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()
        if not row:
            return None
        batch = row[0]
    rows = conn.execute("SELECT id, shot_id, before, after, label FROM decision_log WHERE batch=? AND undone=0 ORDER BY id DESC",
                        (batch,)).fetchall()
    if not rows:
        return None
    import uuid

    ub = uuid.uuid4().hex[:12]            # the undo is one step in the history too
    ids, skipped = _replay(conn, rows, True, f"undo {rows[0]['label']}", viewer, ub)
    conn.execute("UPDATE decision_log SET undoes=? WHERE batch=?", (batch, ub))
    conn.execute("UPDATE decision_log SET undone=1 WHERE batch=?", (batch,))
    conn.commit()
    return {"batch": batch, "label": rows[0]["label"], "ids": [r["shot_id"] for r in rows], "skipped": skipped}


def _redo_target(conn, viewer: str | None = None):
    """The undo that Redo would reverse: the latest one not reversed yet, made after the latest
    decision. Any new decision (from anywhere) ends what can be redone, as in any editor."""
    who, args = "", []
    if viewer:
        who, args = " AND COALESCE(viewer, '') = ?", [viewer]
    last = conn.execute(f"SELECT id FROM decision_log WHERE source NOT IN ('undo', 'redo'){who} ORDER BY id DESC LIMIT 1",
                        args).fetchone()
    return conn.execute(f"SELECT batch, undoes FROM decision_log WHERE source='undo' AND undone=0 AND undoes IS NOT NULL"
                        f" AND id > ?{who} ORDER BY id DESC LIMIT 1", [last[0] if last else 0, *args]).fetchone()


def can_redo(conn, viewer: str | None = None) -> bool:
    return _redo_target(conn, viewer) is not None


def redo(conn, viewer: str | None = None) -> dict | None:
    """Reverse the latest Undo, straight after it (again leaving anything changed since alone). The
    redo is logged as a step of its own, which Undo reverses."""
    row = _redo_target(conn, viewer)
    if not row:
        return None
    ub = row[0]
    rows = conn.execute("SELECT id, shot_id, before, after, label FROM decision_log WHERE batch=? ORDER BY id DESC",
                        (ub,)).fetchall()
    if not rows:
        return None
    import uuid

    label = rows[0]["label"]
    label = label[5:] if label.startswith("undo ") else label
    rb = uuid.uuid4().hex[:12]
    ids, skipped = _replay(conn, rows, True, label, viewer, rb, source="redo")
    conn.execute("UPDATE decision_log SET undoes=? WHERE batch=?", (ub, rb))      # the redo reverses that undo...
    conn.execute("UPDATE decision_log SET undone=1 WHERE batch=?", (ub,))         # ...which is spent
    conn.commit()
    return {"batch": rb, "label": label, "ids": [r["shot_id"] for r in rows], "skipped": skipped}


def history(conn, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    """Recent changes, one row per step: what, when, how many photos, and whether it was undone."""
    rows = conn.execute(
        "SELECT batch, label, source, viewer, MIN(ts) AS ts, COUNT(*) AS n, MIN(undone) AS undone,"
        " SUM(json_extract(after, '$.pick') = 1 AND json_extract(before, '$.pick') != 1) AS picked,"
        " SUM(json_extract(after, '$.pick') = -1 AND json_extract(before, '$.pick') != -1) AS rejected,"
        " SUM(json_extract(after, '$.rating') != json_extract(before, '$.rating')) AS rated"
        " FROM decision_log GROUP BY batch ORDER BY MAX(id) DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def day_summary(conn, since: float) -> dict:
    """"Today you picked 84 and rejected 210": net effect of changes since a time."""
    r = conn.execute(
        "SELECT SUM(json_extract(after, '$.pick') = 1 AND json_extract(before, '$.pick') != 1),"
        " SUM(json_extract(after, '$.pick') = -1 AND json_extract(before, '$.pick') != -1),"
        " SUM(json_extract(after, '$.rating') > 0 AND json_extract(after, '$.rating') != json_extract(before, '$.rating')),"
        " COUNT(DISTINCT shot_id) FROM decision_log WHERE ts >= ? AND source != 'undo' AND undone = 0"
        " AND json_extract(before, '$.keywords') IS NULL", (since,)).fetchone()
    return {"picked": r[0] or 0, "rejected": r[1] or 0, "rated": r[2] or 0, "photos": r[3] or 0}


def describe(s: dict) -> str:
    bits = []
    if s.get("aperture"):
        bits.append(f"f/{s['aperture']:g}")
    if s.get("shutter"):
        sh = s["shutter"]
        bits.append(f"1/{round(1 / sh)}s" if sh < 1 else f"{sh:g}s")
    if s.get("iso"):
        bits.append(f"ISO{int(s['iso'])}")
    if s.get("focal"):
        bits.append(f"{s['focal']:g}mm")
    return " ".join(bits)


# ---------------------------------------------------------------- light rows for the web grid

LIGHT_COLS = """s.id, s.stem, s.dir, s.capture_str, s.capture_ts, s.eff_ts, s.time_src, s.camera, s.width, s.height,
       s.orientation, s.analyzed_fp, s.group_id, s.status, s.media, s.duration, s.place_id, s.event_id, s.cam_rating,
       s.edited, s.c2pa, s.gps_lat IS NOT NULL AS has_gps,
       m.quality, m.flags, m.suggestion, m.group_rank, m.sharp_subject,
       m.aesthetic, m.taste, g.size AS group_size, d.rating, COALESCE(d.pick, 0) AS pick, d.label,
       s.sort_ts, s.sort_desc"""
LIGHT_JOINS = """LEFT JOIN metrics m ON m.shot_id = s.id
LEFT JOIN decisions d ON d.shot_id = s.id
LEFT JOIN groups g ON g.id = s.group_id"""
LIGHT_FROM = "FROM shots s\n" + LIGHT_JOINS

# Photos without a capture date (dated only by when the file was saved) always sort last.
UNDATED_LAST = "COALESCE(time_src, 'exif') = 'file', eff_ts IS NULL"
OUTER_SORTS = {
    "time": "sort_ts, dir, stem",               # = UNDATED_LAST, eff_ts, dir, stem (db._SORT_KEY), indexed
    "time_desc": "sort_desc, dir, stem",          # = UNDATED_LAST, eff_ts DESC, dir, stem
    "quality": "quality IS NULL, quality DESC, eff_ts",
    "aesthetic": "aesthetic IS NULL, aesthetic DESC, eff_ts",
    "sharpness": "sharp_subject IS NULL, sharp_subject DESC",
    "taste": "taste IS NULL, taste DESC, quality DESC",
    "rating": "COALESCE(rating, 0) DESC, quality DESC",
    "path": "dir, stem",
    "added": "id DESC",                           # ids are never reused: the newest in the catalog first
    "size": "bytes IS NULL, bytes DESC, sort_ts",
    "camera": "camera IS NULL, camera COLLATE NOCASE, sort_ts, dir, stem",
}
# Columns only one sort needs, added to the rows only for it.
SORT_COLS = {"size": "(SELECT SUM(fz.size) FROM files fz WHERE fz.shot_id = s.id AND fz.status='present') AS bytes"}


def version_tag(fp: str | None) -> str:
    return hashlib.blake2b((fp or "").encode(), digest_size=5).hexdigest() if fp else "0"


def light(r) -> dict:
    d = dict(r)
    d["flags"] = json.loads(d["flags"]) if d.get("flags") else []
    w, h = d.pop("width"), d.pop("height")
    if w and h:
        if (d.get("orientation") or 1) in (5, 6, 7, 8):
            w, h = h, w
        d["ar"] = round(w / h, 4)
    else:
        d["ar"] = 1.5
    d["v"] = version_tag(d.pop("analyzed_fp"))
    for k in ("orientation", "sort_ts", "sort_desc"):
        d.pop(k, None)
    return d


def top(conn, flt: Filter, limit: int = 12) -> list[dict]:
    """The best photos by quality, one per burst or scene (its best frame): a top-N over one pass,
    without numbering every row of the library first."""
    where, args = flt.sql()
    where = (where + " AND " if where else " WHERE ") + "COALESCE(m.group_rank, 1) = 1"
    q = (f"SELECT * FROM (SELECT {LIGHT_COLS}, COALESCE(g.size, 1) AS stack_n {LIGHT_FROM}{where})"
         f" ORDER BY {OUTER_SORTS['quality']} LIMIT ?")
    return [light(r) for r in conn.execute(q, [*args, int(limit)])]


def page(conn, flt: Filter, sort: str = "time", offset: int = 0, limit: int = 200,
         stack: bool = False, with_total: bool = True) -> tuple[list[dict], int | None]:
    """One page of light rows, plus the total (counted only when asked: later pages of an
    infinite grid already know it). With stack, each group shows only its best matching frame
    and carries stack_n (how many of its frames match)."""
    offset, limit = max(0, int(offset)), max(1, min(int(limit), 5000))
    where, args = flt.sql()
    extra = f", {SORT_COLS[sort]}" if sort in SORT_COLS else ""
    if stack:
        # Number the frames of each stack over just (id, stack, rank), then fetch the full row for
        # each stack's first frame only.
        part = "COALESCE(s.group_id, -s.id)"
        narrow = (f"SELECT s.id AS sid, ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY COALESCE(m.group_rank, 999), s.id) AS rn,"
                  f" COUNT(*) OVER (PARTITION BY {part}) AS stack_n {LIGHT_FROM}{where}")
        base = (f"SELECT {LIGHT_COLS}, t.stack_n{extra} FROM ({narrow}) t JOIN shots s ON s.id = t.sid"
                f" {LIGHT_JOINS} WHERE t.rn = 1")
        total = count_stacks(conn, flt) if with_total else None
    else:
        base = f"SELECT {LIGHT_COLS}, 1 AS stack_n{extra} {LIGHT_FROM}{where}"
        total = conn.execute(f"SELECT COUNT(*) {LIGHT_FROM}{where}", args).fetchone()[0] if with_total else None
    q = f"SELECT * FROM ({base}) ORDER BY {OUTER_SORTS.get(sort, OUTER_SORTS['time'])} LIMIT ? OFFSET ?"
    rows = [light(r) for r in conn.execute(q, [*args, int(limit), int(offset)])]
    for r in rows:
        r.pop("rn", None)
    return rows, total


def _and(where: str, cond: str) -> str:
    return f"{where} AND ({cond})" if where else f" WHERE ({cond})"


def month_offset(conn, flt: Filter, month: str, sort: str = "time") -> int | None:
    """Where a month ("2024-05", or "undated") starts in the time-sorted listing, so the scrubber
    can load from there instead of every page before it. None when nothing of that month matches."""
    where, args = flt.sql()
    col = "s.sort_desc" if sort == "time_desc" else "s.sort_ts"
    if month == "undated":
        cond, margs = "s.capture_str IS NULL OR COALESCE(s.time_src, 'exif') = 'file'", []
    else:
        cond, margs = "substr(s.capture_str, 1, 7) = ? AND COALESCE(s.time_src, 'exif') != 'file'", [month]
    first = conn.execute(f"SELECT MIN({col}) {LIGHT_FROM}{_and(where, cond)}", [*args, *margs]).fetchone()[0]
    if first is None:
        return None
    return conn.execute(f"SELECT COUNT(*) {LIGHT_FROM}{_and(where, f'{col} < ?')}", [*args, first]).fetchone()[0]


def frames(conn, flt: Filter, ids: list[int]) -> list[int]:
    """Every frame matching the filter in the stacks these photos stand for (a photo that isn't in a
    stack stands for itself): what a change to a selection of stacks reaches."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    out = dict.fromkeys(ids)
    gids: set[int] = set()
    for k in range(0, len(ids), 500):
        chunk = ids[k : k + 500]
        gids |= {r[0] for r in conn.execute(
            f"SELECT DISTINCT group_id FROM shots WHERE group_id IS NOT NULL AND id IN ({','.join('?' * len(chunk))})", chunk)}
    where, args = flt.sql()
    gl = sorted(gids)
    for k in range(0, len(gl), 500):
        among = ",".join(str(int(g)) for g in gl[k : k + 500])
        for (sid,) in conn.execute(f"SELECT s.id {LIGHT_FROM}{_and(where, f's.group_id IN ({among})')} ORDER BY s.sort_ts", args):
            out.setdefault(sid)
    return list(out)


def count_stacks(conn, flt: Filter) -> int:
    """How many stacks (bursts/scenes, singles count as one) have a frame matching the filter."""
    where, args = flt.sql()
    return conn.execute(f"SELECT COUNT(DISTINCT COALESCE(s.group_id, -s.id)) {LIGHT_FROM}{where}", args).fetchone()[0]


def rows_by_ids(conn, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    out = {}
    for i in range(0, len(ids), 500):
        chunk = [int(x) for x in ids[i : i + 500]]
        q = f"SELECT {LIGHT_COLS}, 1 AS stack_n {LIGHT_FROM} WHERE s.id IN ({','.join('?' * len(chunk))})"
        for r in conn.execute(q, chunk):
            out[r["id"]] = light(r)
    return [out[i] for i in ids if i in out]


def _start(ts, src) -> tuple:
    """Culling order: undated photos (no capture date) come after everything dated."""
    return (src == "file" or ts is None, ts or 0)


def stack_numbers(conn) -> dict:
    """Every stack in the library, numbered in culling order: ("g", group id) or ("s", photo id) ->
    1, 2, 3... A stack keeps its number whatever is decided or filtered, until the library is
    regrouped."""
    return _stack_numbers(conn.execute("SELECT id, group_id, eff_ts, time_src FROM shots WHERE status='present'"
                                       " ORDER BY eff_ts, id").fetchall())


def _stack_numbers(rows) -> dict:
    """stack_numbers() from (id, group id, eff_ts, time_src) rows in (eff_ts, id) order."""
    first: dict = {}
    for r in rows:
        key = ("g", r[1]) if r[1] is not None else ("s", r[0])
        if key not in first:
            first[key] = (_start(r[2], r[3]), r[0])
    return {k: i for i, k in enumerate(sorted(first, key=first.get), 1)}


def stacks(conn, flt: Filter) -> list[dict]:
    """Culling order: every group with at least one matching frame, whole group included,
    ordered by when the group started. Singles are stacks of one. Each stack carries its number
    in the library (`no`), how many of its frames are undecided (`und`), how long it spans in
    seconds, and where a new burst starts in it (`breaks`: frame indexes, a gap of over
    BURST_GAP_S)."""
    from .config import BURST_GAP_S

    where, args = flt.sql()
    matching = conn.execute(f"SELECT DISTINCT s.id, s.group_id {LIGHT_FROM}{where}", args).fetchall()
    gids = {r[1] for r in matching if r[1] is not None}
    singles = {r[0] for r in matching if r[1] is None}
    # Every photo in the library once, in culling order: the stacks' frames and their numbers both come
    # from it (rather than a query per kind with thousands of ids written into it).
    rows = conn.execute("SELECT s.id, s.group_id, s.eff_ts, s.time_src, COALESCE(d.pick, 0) = 0 AND COALESCE(d.rating, 0) = 0"
                        " FROM shots s LEFT JOIN decisions d ON d.shot_id = s.id WHERE s.status='present'"
                        " ORDER BY s.eff_ts, s.id").fetchall()
    numbers = _stack_numbers(rows)
    members: dict[int, list] = {}
    out = []
    for r in rows:
        if r[1] is not None:
            if r[1] in gids:
                members.setdefault(r[1], []).append(r)
        elif r[0] in singles:
            out.append({"group": None, "ids": [r[0]], "start": _start(r[2], r[3]), "und": r[4], "span": 0, "breaks": []})
    for gid, ms in members.items():
        ts = [m[2] for m in ms]
        dated = None not in ts
        out.append({"group": gid, "ids": [m[0] for m in ms], "start": _start(ms[0][2], ms[0][3]),
                    "und": sum(m[4] for m in ms), "span": round(ts[-1] - ts[0], 2) if dated else 0,
                    "breaks": [i for i in range(1, len(ts)) if ts[i] - ts[i - 1] > BURST_GAP_S] if dated else []})
    out.sort(key=lambda s: (s["start"], s["ids"][0]))
    for s in out:
        s["start"] = s["start"][1]
        s["no"] = numbers.get(("g", s["group"]) if s["group"] is not None else ("s", s["ids"][0]))
    return out
