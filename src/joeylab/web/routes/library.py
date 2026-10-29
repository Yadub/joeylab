"""Projects, overview and home, browsing, decisions (with server-side undo and history),
the timeline, folders and per-library settings."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import __author__, __version__, config, copies, db, keywords as keywords_mod, library, locks, scanner, scoring, taste
from ...library import Filter
from ..deps import _int, big_json, cap, conn_dep, filter_for, with_search, ids_of, is_owner, is_writer, one, require_owner, require_writer, viewer_of

router = APIRouter()

PURPOSES = ("shoot", "archive")
DEFAULT_SETTINGS = {
    "purpose": None,              # shoot | archive; asked on first visit
    "delivery_target": None,      # how many photos a shoot delivers
    "hide_tiny": True,            # hide thumbnails and fragments (under 640 px) from browsing
    "hide_utility": False,        # hide screenshots, receipts and documents from browsing
    "hide_duplicates": None,      # hide extra copies of photos kept elsewhere; default: archives yes, shoots no
    "show_suggestions": None,     # badges on tiles; default: shoots yes, archives no
    "map_tiles": False,           # load map tiles from OpenStreetMap on the Places page
    "grouping": 3,                # how strictly frames are stacked: 1 bursts only ... 5 whole scenes
    "folder_names": {},           # your names for folders, shown instead of "SD Card 20260110"
}


SHOOT_GROUPING = config.SHOOT_GROUPING      # "Tight": bursts and near-identical frames, not a whole scene


def purpose_defaults(conn, purpose) -> bool:
    """A library set up as a shoot stacks tightly, unless a level was chosen: a stack is then a
    burst, and Keep only this and Accept work on bursts rather than whole scenes. True when that
    changed the level."""
    if purpose == "shoot" and db.get_setting(conn, "grouping", None) is None:
        db.set_setting(conn, "grouping", SHOOT_GROUPING)
        return True
    return False


def settings_of(conn) -> dict:
    s = dict(DEFAULT_SETTINGS)
    for k in DEFAULT_SETTINGS:
        v = db.get_setting(conn, k, None)
        if v is not None:
            s[k] = v
    if s["show_suggestions"] is None:
        s["show_suggestions"] = s["purpose"] != "archive"
    if s["hide_duplicates"] is None:
        s["hide_duplicates"] = s["purpose"] == "archive"
    return s


# ------------------------------------------------------------ meta / projects
@router.get("/api/meta")
def api_meta(request: Request):
    from ... import ai, exif, search

    jobs = request.app.state.jobs
    watcher = getattr(jobs, "watcher", None)
    owner = is_owner(request)
    # Folder names on this Mac (its home folder, where JoeyLab is installed) are the owner's alone.
    mine = {"home": str(Path.home()), "joeylab_bin": str(Path(sys.executable).parent / "joeylab")} if owner else {}
    # ai_loaded: a search won't wait for a model to load (picture search's, or CLIP's for a library without it).
    return {"ai": ai.available(), "ai_loaded": search.Model.loaded() or ai.Models.loaded(), "watch_minutes": jobs.watch_minutes,
            "watching": watcher.status() if watcher else None, **mine,
            "viewer": viewer_of(request), "writer": is_writer(request), "owner": owner,
            "can_trash": owner and sys.platform == "darwin",
            "version": __version__, "author": __author__, "exiftool": exif.available(), "build": _BUILD,
            **request.app.state.meta}


def _build_stamp() -> str:
    """This server's build: the version and the app's own files (their modification times), so a page
    left open can tell that JoeyLab was updated and offer to reload."""
    import hashlib

    static = Path(__file__).resolve().parents[1] / "static"
    h = hashlib.sha1(__version__.encode())
    for f in sorted(static.rglob("*")):
        if f.is_file():
            h.update(f"{f.relative_to(static)}:{f.stat().st_mtime_ns}\n".encode())
    return f"{__version__}+{h.hexdigest()[:10]}"


_BUILD = _build_stamp()           # once, at start-up


@router.get("/api/projects")
def api_projects(request: Request):
    jobs = request.app.state.jobs
    out = []
    for name in config.list_projects():
        try:
            conn = db.connect(name, create=False)
        except db.CatalogDamaged as e:
            out.append({"name": name, "error": str(e), "photos": 0, "picks": 0, "roots": []})
            continue
        try:
            last = conn.execute("SELECT MAX(finished) FROM scans").fetchone()[0]
            photos, picks, cover = _project_counts(name, conn)
            out.append({
                "name": name, "photos": photos, "picks": picks,
                "roots": [r[0] for r in conn.execute("SELECT path FROM roots")],
                "purpose": db.get_setting(conn, "purpose"),
                "last_scan": last,
                "cover": cover,
                "job": (j.as_dict() if (j := jobs.current(name)) else None),
            })
        finally:
            conn.close()
    return out


_PROJECT_COUNTS: dict[str, tuple] = {}     # library -> (its catalog and counts:gen, photos, picks, cover)


def _project_counts(name: str, conn) -> tuple:
    """A library's photos, picks and cover photo for the list of libraries, kept until its counts change
    (db.COUNTS_GEN, read before them): on 75,000 photos they took 45 ms on every load of the list (the
    cover 0.6 s the first time). A cover whose quality is scored again without a change to the counts
    stays until the next one."""
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    try:
        key = (path, os.stat(path).st_ino, db.counter(conn, db.COUNTS_GEN))
    except OSError:
        key = None
    hit = _PROJECT_COUNTS.get(name)
    if key is not None and hit and hit[0] == key:
        return hit[1:]
    cover = conn.execute(
        "SELECT s.id, s.analyzed_fp FROM shots s JOIN metrics m ON m.shot_id=s.id"
        " LEFT JOIN decisions d ON d.shot_id=s.id WHERE s.status='present' AND s.thumb IS NOT NULL"
        " ORDER BY COALESCE(d.rating,0) DESC, m.quality DESC LIMIT 1").fetchone()
    out = (one(conn, "SELECT COUNT(*) FROM shots WHERE status='present' AND (error IS NULL OR analyzed_fp IS NOT NULL)"),
           one(conn, "SELECT COUNT(*) FROM decisions d JOIN shots s ON s.id=d.shot_id WHERE s.status='present' AND d.pick=1"),
           {"id": cover[0], "v": library.version_tag(cover[1])} if cover else None)
    if key is not None:
        _PROJECT_COUNTS[name] = (key, *out)
    return out


@router.post("/api/projects")
def api_create_project(request: Request, body: dict = Body(default_factory=dict)):
    """A new library: {name (made into a slug), path, purpose, empty_ok}. The folder is checked
    first; one with no photos in it is refused unless empty_ok (photos will be copied in later)."""
    from ... import libraries

    require_owner(request, "add folders to a library")
    jobs = request.app.state.jobs
    path = (body.get("path") or "").strip()
    name = libraries.slug(body.get("name") or "") or libraries.slug(Path(path).name)
    why = libraries.name_problem(name)
    if why:
        raise HTTPException(409 if "already" in why else 400, why)
    check = libraries.check_folder(path)
    if not check["ok"] and not (check["empty"] and body.get("empty_ok")):
        raise HTTPException(400, check["reason"])
    pdir = config.project_dir(name)
    conn = db.connect(name, create=True)
    try:
        scanner.add_root(conn, Path(path).expanduser())
        if body.get("purpose") in PURPOSES:
            db.set_setting(conn, "purpose", body["purpose"])
            purpose_defaults(conn, body["purpose"])
    except (NotADirectoryError, ValueError) as e:
        conn.close()
        import shutil

        shutil.rmtree(pdir, ignore_errors=True)
        raise HTTPException(400, f"Can't use that folder: {e}")
    conn.close()
    job = jobs.start(name, quick=bool(body.get("quick")))
    if getattr(jobs, "watcher", None):
        jobs.watcher.request_refresh()
    return {"name": name, "job": job.as_dict(), "check": check}


@router.get("/api/fs/check")
def api_check_folder(request: Request, path: str = "", library: str = ""):
    """Whether a folder can be a library's (or be added to `library`), and about how many photos it
    holds (a quick look)."""
    from ... import libraries

    require_owner(request, "look at its folders")
    return libraries.check_folder(path, library=library or None)


@router.post("/api/projects/{p}/rename")
def api_rename_project(p: str, request: Request, body: dict = Body(default_factory=dict)):
    """{to}: a new name (made into a slug). JoeyLab's own records only; no photo folder changes."""
    from ... import libraries

    require_owner(request, "rename a library")
    jobs = request.app.state.jobs
    if p in jobs.running:
        raise HTTPException(409, "This library is being updated. Rename it when the update has finished.")
    try:
        new = libraries.rename(p, body.get("to") or "")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except (ValueError, OSError, locks.Busy) as e:
        raise HTTPException(409 if isinstance(e, locks.Busy) or "already" in str(e) else 400, str(e))
    with jobs.lock:
        for d in (jobs.last, jobs.versions):
            if p in d:
                d[new] = d.pop(p)
    request.app.state.overviews.forget(p)
    if getattr(jobs, "watcher", None):
        jobs.watcher.request_refresh()
    return {"name": new}


@router.post("/api/projects/{p}/remove")
def api_remove_project(p: str, request: Request, body: dict = Body(default_factory=dict)):
    """{confirm: the library's name}. Keeps a backup of the catalog and deletes JoeyLab's copy of
    the library; its photo folders aren't touched."""
    from ... import libraries

    require_owner(request, "remove a library")
    jobs = request.app.state.jobs
    if p in jobs.running:
        raise HTTPException(409, "This library is being updated. Remove it when the update has finished.")
    try:
        out = libraries.remove(p, body.get("confirm") or "")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except (ValueError, OSError, locks.Busy) as e:
        raise HTTPException(409 if isinstance(e, locks.Busy) else 400, str(e))
    with jobs.lock:
        jobs.last.pop(p, None)
        jobs.versions.pop(p, None)
    request.app.state.overviews.forget(p)
    if getattr(jobs, "watcher", None):
        jobs.watcher.request_refresh()
    return out


# ------------------------------------------------------------ settings
@router.get("/api/p/{p}/settings")
def api_settings(p: str, conn=Depends(conn_dep)):
    return settings_of(conn)


@router.post("/api/p/{p}/settings")
def api_set_settings(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    for k, v in body.items():
        if k not in DEFAULT_SETTINGS:
            raise HTTPException(400, f"Unknown setting {k!r}.")
        if k == "purpose" and v not in PURPOSES:
            raise HTTPException(400, "purpose is shoot or archive.")
        if k == "delivery_target" and v is not None and not (isinstance(v, int) and 1 <= v <= 100000):
            raise HTTPException(400, "The delivery target is a number of photos.")
        if k == "grouping" and v not in config.GROUPING_LEVELS:
            raise HTTPException(400, "Stacking is a level from 1 (bursts only) to 5 (whole scenes).")
        db.set_setting(conn, k, v)
    if "grouping" in body or ("purpose" in body and purpose_defaults(conn, body["purpose"])):
        request.app.state.bg.regroup(p)      # restack on the new level, then re-score
    return settings_of(conn)


@router.get("/api/p/{p}/grouping")
def api_grouping(p: str, request: Request, conn=Depends(conn_dep)):
    """The current strictness, what each level would give (for the slider), and whether a regroup
    is still running."""
    from ... import grouping

    bg = request.app.state.bg
    return {"level": grouping.level_of(conn), "levels": grouping.preview(conn),
            "busy": bg.busy(f"regroup:{p}"), "error": bg.errors.get(f"regroup:{p}")}


# ------------------------------------------------------------ overview
def _bucket_start(ts: float, unit: str) -> float:
    d = datetime.fromtimestamp(ts, timezone.utc)
    if unit == "hour":
        d = d.replace(minute=0, second=0, microsecond=0)
    elif unit == "day":
        d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    elif unit == "month":
        d = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        d = d.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp()


def _ts_label(ts: float, unit: str) -> str:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return {"hour": d.strftime("%d %b %H:00"), "day": d.strftime("%d %b %Y"), "month": d.strftime("%b %Y"),
            "year": d.strftime("%Y")}[unit]


def browse_filter(conn, base: Filter | None = None) -> Filter:
    """What "everything" means when browsing: hides thumbnails/fragments (and utility shots) when
    the library is set to. Files that couldn't be read as photos at all are never shown or counted
    (Tidy › Health lists them)."""
    f = with_search(conn, base or Filter())
    f.readable = True
    s = settings_of(conn)
    hide = [x for x, on in (("tiny", s["hide_tiny"]), ("utility", s["hide_utility"]), ("duplicate", s["hide_duplicates"])) if on]
    if hide and not f.flag:
        f.no_flag = ",".join(filter(None, [f.no_flag, *hide]))
    return f


def without_extras(f: Filter) -> Filter:
    """Extra copies of photos kept elsewhere are never culled or counted as photos taken: they're
    left out of stacks, of what's left to cull and of Home's years (the Duplicates page has them).
    So are backups of a photo shown from another drive of the library: one photo, culled once."""
    if not f.flag:
        have = (f.no_flag or "").split(",")
        f.no_flag = ",".join(filter(None, [f.no_flag, *(x for x in ("duplicate", "backup-copy") if x not in have)]))
    return f


def duplicates_filter(conn, q, base: Filter) -> Filter | None:
    """The extra copies a Library filter (`base`, from filter_for) matches, for the Library's note on
    them; None when the filter is for flags already or shows everything."""
    if q.get("all") in ("1", "true") or q.get("flag"):
        return None
    f = browse_filter(conn, dataclasses.replace(base))
    f.no_flag = ",".join(x for x in (f.no_flag or "").split(",") if x and x != "duplicate") or None
    f.flag = "duplicate"
    return f


def duplicates_note(conn, q, viewer: str | None) -> dict | None:
    """For the Library's note: how many extra copies match this filter, and whether they're hidden."""
    f = duplicates_filter(conn, q, filter_for(conn, q, viewer))
    if f is None:
        return None
    w, a = f.sql()
    return {"hidden": bool(settings_of(conn)["hide_duplicates"]), "n": one(conn, f"SELECT COUNT(*) {library.LIGHT_FROM}{w}", a)}


# Library counts kept until counts:gen moves, for filters on nothing else but what it follows (decisions,
# flags and suggestions, whether a photo is present or could be read, photo or video): on a big library
# a filter's counts take tens of milliseconds even in one pass, and a decision doesn't change most of them.
_COUNTS: dict[tuple, tuple] = {}
_COUNTS_LOCK = threading.Lock()
_GEN_FIELDS = {"picks", "rejects", "undecided", "min_rating", "suggestion", "flag", "no_flag", "media", "readable",
               "include_missing"}


def _followed_by_gen(*filters: Filter | None) -> bool:
    plain = Filter()
    return all(f.name in _GEN_FIELDS or getattr(flt, f.name) == getattr(plain, f.name)
               for flt in filters if flt is not None for f in dataclasses.fields(flt))


def browse_counts(conn, shown: Filter, raw: Filter, dup: Filter | None, stack: bool = False,
                  base: Filter | None = None) -> tuple[int, int, int | None]:
    """The Library's counts in one pass over the photos (three counts used to read the library three
    times): how many photos (or stacks) it shows; how many more the filter matches than browsing shows
    (as hidden_by_browsing); and how many extra copies match (`dup`, from duplicates_filter: as
    duplicates_note). `base` is a filter every one of them narrows, so the pass can use its indexes."""
    agg = "COUNT(DISTINCT COALESCE(s.group_id, -s.id))" if stack else "COUNT(*)"
    hides = not (raw.no_flag == shown.no_flag and raw.readable == shown.readable)
    cols, args = [], []
    for f, how in ((shown, agg), (raw if hides else None, agg), (dup, "COUNT(*)")):
        if f is None:
            cols.append("NULL")
            continue
        w, a = f.sql()
        cols.append(f"{how} FILTER (WHERE {w.removeprefix(' WHERE ') or '1'})")
        args += a
    w, a = base.sql() if base is not None else ("", [])
    q, args = f"SELECT {', '.join(cols)} {library.LIGHT_FROM}{w}", [*args, *a]
    key = None
    if _followed_by_gen(shown, raw, dup, base):
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        try:                                    # (the counter before the counts; the file as it is now)
            key = (path, os.stat(path).st_ino, db.counter(conn, db.COUNTS_GEN), q, tuple(args))
        except OSError:
            key = None
        if (hit := _COUNTS.get(key)) is not None:
            return hit
    n, more, dups = conn.execute(q, args).fetchone()
    out = (n, max(0, more - n) if hides else 0, dups)
    if key is not None:
        with _COUNTS_LOCK:
            while len(_COUNTS) >= 64:
                _COUNTS.pop(next(iter(_COUNTS)))
            _COUNTS[key] = out
    return out


def hidden_by_browsing(conn, raw: Filter, shown: Filter, total: int | None, stack: bool = False) -> int:
    """How many more photos (or stacks) the filter matches than browsing shows: the thumbnail-sized
    and utility shots it hides, and files that couldn't be read. Counted only when something is
    hidden and the total is known."""
    if total is None or (raw.no_flag == shown.no_flag and raw.readable == shown.readable):
        return 0
    if stack:
        return max(0, library.count_stacks(conn, raw) - total)
    w, a = raw.sql()
    return max(0, one(conn, f"SELECT COUNT(*) {library.LIGHT_FROM}{w}", a) - total)


def _faces_of(conn, sid: int) -> list[dict]:
    """A photo's faces and pets, each with its group's size and whether that group is a clear one person
    (or pet), so the viewer can offer "name the whole group" only when it's safe to. A face found on an
    animal (pets.animal_faces) is left out: the animal is listed itself."""
    from ... import faces as faces_mod, pets

    out = []
    for r in conn.execute(
            "SELECT f.id, f.x, f.y, f.w, f.h, f.px, f.closed, f.smile, f.sharp, f.cluster, f.person_id, pe.name, pe.kind,"
            " f.model = ? AS pet, g.faces AS group_faces, g.cohesion, g.sharp AS group_sharp"
            " FROM faces f LEFT JOIN people pe ON pe.id = f.person_id LEFT JOIN face_groups g ON g.cluster = f.cluster"
            " WHERE f.shot_id=? AND NOT (COALESCE(f.kind, '') = 'pet' AND f.model != ?) ORDER BY f.x", (pets.MODEL, sid, pets.MODEL)):
        d = dict(r)
        coh, gs = d.pop("cohesion"), d.pop("group_sharp")
        d["pet"] = bool(d["pet"])
        d["group_clear"] = pets.clear_group(coh) if d["pet"] else faces_mod.clear_group(coh, gs)
        out.append(d)
    return out


@router.get("/api/p/{p}/overview")
def api_overview(p: str, request: Request, conn=Depends(conn_dep)):
    """Counts and summaries for the top bar, Home and the rail, kept between requests (web/overview.py):
    `fresh` is false while newer numbers are being counted, and `seq` changes when they arrive."""
    from ... import ai

    jobs, overviews = request.app.state.jobs, request.app.state.overviews
    body, fresh = overviews.get(p, conn)
    job = jobs.current(p)
    return {**body, "taste": taste.meta(), "ai": {"available": ai.available(), "embedded": body["embedded"]},
            "job": job.as_dict() if job else None, "version": jobs.version(p), "seq": overviews.seq(p), "fresh": fresh}


# The overview's numbers that pages confirm your actions with, counted again whenever the catalog's
# counts counter moves (overview_live); the rest come from the last full count (web/overview.py).
LIVE_KEYS = {"counts": ("photos", "videos", "files", "missing", "failed", "analysed", "undated", "with_gps", "cam_rated",
                        "groups", "multi_groups", "dup_sets", "dup_bytes", "picks", "rejects", "rated", "undecided",
                        "undecided_stacks", "suggest_pick", "suggest_reject", "suggest_check"),
             "top": ("ratings", "today")}


def overview_always(conn) -> dict:
    """The overview's numbers that cost nothing to read, read for every request: the settings, the
    library's folders, and how many people, events, trips and found selections it has."""
    return {"settings": settings_of(conn), "roots": [r[0] for r in conn.execute("SELECT path FROM roots")], "counts": {
        "people": one(conn, "SELECT COUNT(*) FROM people WHERE COALESCE(hidden,0)=0"),
        "events": one(conn, "SELECT COUNT(*) FROM events WHERE kind='event' AND hidden=0"),
        "trips": one(conn, "SELECT COUNT(*) FROM events WHERE kind='trip' AND hidden=0"),
        "selections": one(conn, "SELECT COUNT(*) FROM selections WHERE kind='selection'"),
        "mirrors": one(conn, "SELECT COUNT(*) FROM selections WHERE kind='mirror'"),
    }}


def overview_live(conn, today: float) -> dict:
    """The numbers pages confirm your actions with (LIVE_KEYS), in a few passes: photos and files,
    decisions, what's left to cull and the suggestions for it, stacks and duplicate sets. (scan: passes
    that read most photos read them in one go, not one by one from the status index: Filter.sql.)"""
    present, scan = "s.status='present'", "+s.status='present'"
    r = conn.execute(
        "SELECT SUM(s.error IS NULL OR s.analyzed_fp IS NOT NULL), SUM(s.media = 'video'),"     # unreadable files: "failed"
        " SUM(s.error IS NOT NULL AND s.analyzed_fp IS NULL), SUM(s.capture_ts IS NULL), SUM(s.gps_lat IS NOT NULL),"
        " SUM(CASE WHEN COALESCE(s.cam_rating, 0) != 0 THEN d.shot_id IS NULL ELSE 0 END)"   # stars set in the camera only
        f" FROM shots s LEFT JOIN decisions d ON d.shot_id = s.id WHERE {scan}").fetchone()
    counts = dict(zip(("photos", "videos", "failed", "undated", "with_gps", "cam_rated"), (x or 0 for x in r)))
    groups = conn.execute("SELECT COUNT(*), SUM(size > 1) FROM groups").fetchone()
    counts |= {
        "files": one(conn, "SELECT COUNT(*) FROM files WHERE status='present'"),
        "missing": one(conn, "SELECT COUNT(*) FROM shots WHERE status='missing'"),
        "analysed": one(conn, f"SELECT COUNT(*) FROM shots s JOIN metrics m ON m.shot_id=s.id WHERE {present}"),
        "groups": groups[0], "multi_groups": groups[1] or 0,
        # Only the kinds the Duplicates page lists by default (identical files and re-saved copies).
        "dup_sets": one(conn, "SELECT COUNT(*) FROM dup_sets WHERE kind IN ('exact','copy')"),
        "dup_bytes": one(conn, "SELECT COALESCE(SUM(f.size),0) FROM dup_members dm JOIN dup_sets ds ON ds.id=dm.set_id"
                               " JOIN files f ON f.id=dm.file_id WHERE dm.keep=0 AND ds.kind IN ('exact','copy') AND f.status='present'"),
        "picks": 0, "rejects": 0, "rated": 0,
    }
    ratings: dict[int, int] = {}
    for pick, rating, n in conn.execute(f"SELECT d.pick, COALESCE(d.rating, 0), COUNT(*) FROM decisions d JOIN shots s"
                                        f" ON s.id=d.shot_id WHERE {present} GROUP BY 1, 2"):
        counts["picks"] += n if pick == 1 else 0
        counts["rejects"] += n if pick == -1 else 0
        if rating > 0:
            counts["rated"] += n
            ratings[rating] = ratings.get(rating, 0) + n
    # What's left to cull and the suggestions among it, in one pass (the Cull badge counts its stacks).
    w, a = without_extras(browse_filter(conn, Filter(undecided=True))).sql(scan=True)
    u = conn.execute("SELECT COUNT(*), SUM(m.suggestion = 'pick'), SUM(m.suggestion = 'reject'), SUM(m.suggestion = 'check'),"
                     f" COUNT(DISTINCT COALESCE(s.group_id, -s.id)) {library.LIGHT_FROM}{w}", a).fetchone()
    counts |= dict(zip(("undecided", "suggest_pick", "suggest_reject", "suggest_check", "undecided_stacks"), (x or 0 for x in u)))
    return {"counts": counts, "ratings": dict(sorted(ratings.items())), "today": library.day_summary(conn, today)}


def overview_full(conn, p: str, today: float) -> dict:
    """Everything the overview says: one full count, which web/overview.py keeps."""
    present, scan = "s.status='present'", "+s.status='present'"
    counts = {
        "pairs": one(conn, f"SELECT COUNT(*) FROM shots s WHERE {present}"
                     " AND EXISTS (SELECT 1 FROM files f WHERE f.shot_id=s.id AND f.kind='raw' AND f.status='present')"
                     " AND EXISTS (SELECT 1 FROM files f WHERE f.shot_id=s.id AND f.kind='jpeg' AND f.status='present')"),
        "places": one(conn, f"SELECT COUNT(DISTINCT place_id) FROM shots s WHERE {scan} AND place_id IS NOT NULL"),
    }
    flags = {r[0]: r[1] for r in conn.execute(
        f"SELECT j.value, COUNT(*) FROM metrics m JOIN shots s ON s.id=m.shot_id, json_each(m.flags) j"
        f" WHERE {present} AND m.flags IS NOT NULL GROUP BY j.value ORDER BY 2 DESC")}
    cameras = [{"camera": r[0] or "Unknown camera", "serial": r[1], "n": r[2]} for r in conn.execute(
        f"SELECT camera, body_serial, COUNT(*) FROM shots s WHERE {scan} GROUP BY camera, body_serial ORDER BY 3 DESC")]
    folders = [{"dir": r[0], "n": r[1]} for r in conn.execute(
        f"SELECT dir, COUNT(*) FROM shots s WHERE {present} GROUP BY dir ORDER BY dir")]
    span = conn.execute(f"SELECT MIN(capture_str), MAX(capture_str), MIN(eff_ts), MAX(eff_ts) FROM shots s"
                        f" WHERE {scan} AND s.capture_ts IS NOT NULL").fetchone()
    timeline = None
    if span[2] is not None:
        secs = span[3] - span[2]
        unit = "hour" if secs <= 4 * 86400 else "day" if secs <= 180 * 86400 else "month"
        fmt = {"hour": "%Y-%m-%d %H", "day": "%Y-%m-%d", "month": "%Y-%m"}[unit]
        buckets: dict[float, dict] = {}
        for key, n, picks, first in conn.execute(        # counted in SQL, not row by row here
            f"SELECT strftime('{fmt}', s.eff_ts, 'unixepoch') AS k, COUNT(*), SUM(COALESCE(d.pick,0) = 1), MIN(s.eff_ts)"
            f" FROM shots s LEFT JOIN decisions d ON d.shot_id=s.id WHERE {scan} AND s.capture_ts IS NOT NULL"
            f" AND s.eff_ts IS NOT NULL GROUP BY k"):
            buckets[_bucket_start(first, unit)] = {"n": n, "picks": picks or 0}
        timeline = {"unit": unit, "buckets": [
            {"start": k, "label": _ts_label(k, unit), "key": datetime.fromtimestamp(k, timezone.utc).strftime(
                "%Y-%m-%d"), **v} for k, v in sorted(buckets.items())]}
    top = library.top(conn, browse_filter(conn), limit=12)
    emb = 0
    try:                                         # (both counted from an index: db.COUNT_INDEXES)
        emb = one(conn, "SELECT COUNT(*) FROM embeddings")
    except Exception:
        pass
    try:                                         # photos search can find (pages say when that's still to come)
        counts["indexed"] = one(conn, "SELECT COUNT(*) FROM search_vecs sv JOIN shots s ON s.id = sv.shot_id"
                                      " WHERE s.status='present'")
    except Exception:
        counts["indexed"] = 0
    counts["faces"] = one(conn, "SELECT COUNT(*) FROM faces")
    # Groups with a face still to name (+person_id: read by group from the faces_by_group index).
    counts["face_groups"] = one(conn, "SELECT COUNT(DISTINCT cluster) FROM faces WHERE cluster IS NOT NULL AND"
                                      " (+person_id IS NULL OR +person_id <= 0)")
    body = {"name": p, "counts": counts, "flags": flags, "cameras": cameras, "folders": folders,
            "span": {"from": span[0], "to": span[1]}, "timeline": timeline, "top": top, "embedded": emb,
            "backups": copies.counts(conn)}
    for part in (overview_live(conn, today), overview_always(conn)):
        body = {**body, **{k: v for k, v in part.items() if k != "counts"}, "counts": {**body["counts"], **part["counts"]}}
    return body


@router.get("/api/p/{p}/home")
def api_home(p: str, conn=Depends(conn_dep)):
    """The archive's front page: years with their best photos, recent events and trips, on this
    day, the people seen most, and library health at a glance."""
    from ... import events as events_mod

    flt = without_extras(browse_filter(conn))
    w, a = flt.sql()
    years = []
    for r in conn.execute(
        f"SELECT substr(s.capture_str, 1, 4) AS y, COUNT(*) AS n {library.LIGHT_FROM}{w} AND s.capture_ts IS NOT NULL"
        " GROUP BY y ORDER BY y DESC", a):
        # The year's photos by a capture-time range (indexed; a day either side covers any time zone),
        # then exactly by the year written on them.
        try:
            lo = datetime(int(r["y"]), 1, 1, tzinfo=timezone.utc).timestamp() - 86400
            hi = datetime(int(r["y"]) + 1, 1, 1, tzinfo=timezone.utc).timestamp() + 86400
        except (TypeError, ValueError):
            lo, hi = -1e12, 1e12
        # The best frame of each stack that isn't a thumbnail or an extra copy: when a stack's top frame
        # is one of those, the next one stands in, so a year is never left without photos.
        best: list[int] = []
        seen: set[int] = set()
        for sid, gid in conn.execute(
            f"SELECT s.id, COALESCE(s.group_id, -s.id) FROM shots s JOIN metrics m ON m.shot_id = s.id LEFT JOIN decisions d ON d.shot_id = s.id"
            f" WHERE s.status='present' AND s.capture_ts BETWEEN ? AND ? AND substr(s.capture_str, 1, 4) = ?"
            f" AND COALESCE(d.pick,0) != -1"
            f" AND COALESCE(m.flags,'') NOT LIKE '%\"tiny\"%' AND COALESCE(m.flags,'') NOT LIKE '%\"duplicate\"%'"
            f" ORDER BY COALESCE(d.rating,0) DESC, d.pick DESC, COALESCE(m.group_rank, 1) = 1 DESC, m.quality DESC LIMIT 400",
            (lo, hi, r["y"])):
            if gid not in seen:
                seen.add(gid)
                best.append(sid)
                if len(best) == 8:
                    break
        years.append({"year": r["y"], "n": r["n"], "best": best})
    evs = events_mod.listing(conn)
    recent = [e for e in evs if e["kind"] == "trip"][:6], [e for e in evs if e["kind"] == "event" and e["n"] >= 5][:12]
    people = [dict(r) for r in conn.execute(
        "SELECT p.id, p.name, COUNT(DISTINCT f.shot_id) AS photos, MIN(f.id) AS face FROM people p"
        " JOIN faces f ON f.person_id = p.id JOIN shots s ON s.id = f.shot_id WHERE s.status='present'"
        " AND COALESCE(p.hidden, 0) = 0 GROUP BY p.id ORDER BY photos DESC LIMIT 12")]
    otd = _on_this_day(conn)
    ids = [i for y in years for i in y["best"]] + [i for d in otd for i in d["ids"]]
    rows = library.rows_by_ids(conn, ids + [e["cover_shot_id"] for e in recent[0] + recent[1] if e["cover_shot_id"]])
    return {"years": years, "trips": recent[0], "events": recent[1], "people": people, "rows": rows,
            "on_this_day": otd}


def _on_this_day(conn, limit: int = 12) -> list[dict]:
    md = datetime.now().strftime("%m-%d")
    out = []
    for r in conn.execute(
        "SELECT substr(s.capture_str, 1, 4) AS y, s.id FROM shots s JOIN metrics m ON m.shot_id = s.id"
        " LEFT JOIN decisions d ON d.shot_id = s.id WHERE s.status='present' AND substr(s.capture_str, 6, 5) = ?"
        " AND substr(s.capture_str, 1, 4) < strftime('%Y', 'now') AND COALESCE(d.pick,0) != -1"
        " AND COALESCE(m.flags,'') NOT LIKE '%\"tiny\"%' AND COALESCE(m.flags,'') NOT LIKE '%\"duplicate\"%'"
        " AND COALESCE(m.group_rank,1) = 1"
        " ORDER BY COALESCE(d.rating,0) DESC, m.quality DESC", (md,)):
        out.append({"year": r[0], "id": r[1]})
    by_year: dict[str, list] = defaultdict(list)
    for o in out:
        if len(by_year[o["year"]]) < 4:
            by_year[o["year"]].append(o["id"])
    return [{"year": y, "ids": v} for y, v in sorted(by_year.items(), reverse=True)][:limit]


@router.get("/api/p/{p}/onthisday")
def api_on_this_day(p: str, conn=Depends(conn_dep)):
    days = _on_this_day(conn)
    return {"days": days, "rows": library.rows_by_ids(conn, [i for d in days for i in d["ids"]])}


@router.get("/api/p/{p}/timeline")
def api_timeline(p: str, request: Request, conn=Depends(conn_dep)):
    """Photo counts per month (or year) for the grid's scrubber and section headers. Filters apply."""
    flt = browse_filter(conn, filter_for(conn, request.query_params, viewer_of(request)))
    w, a = flt.sql()
    rows = conn.execute(
        f"SELECT substr(s.capture_str, 1, 7) AS ym, COUNT(*) AS n {library.LIGHT_FROM}{w} GROUP BY ym ORDER BY ym", a).fetchall()
    return {"months": [{"month": r["ym"] or "undated", "n": r["n"]} for r in rows]}


@router.get("/api/p/{p}/summary")
def api_summary(p: str, request: Request, conn=Depends(conn_dep)):
    """What the photos a Library filter shows are made of, for the Library's summary of anything shown
    (a search's matches, a folder, a person): how many, when (by year, or by month within one year), who,
    where, which events, cameras and folders, and how many are picked or rejected."""
    q = request.query_params
    flt = filter_for(conn, q, viewer_of(request))
    if q.get("all") not in ("1", "true"):
        flt = browse_filter(conn, flt)
    w, a = flt.sql()
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS summary_ids (id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM temp.summary_ids")
    conn.execute(f"INSERT INTO temp.summary_ids SELECT s.id {library.LIGHT_FROM}{w}", a)
    sel = "temp.summary_ids x JOIN shots s ON s.id = x.id"
    t = conn.execute(f"SELECT COUNT(*) AS n, MIN(s.capture_str) AS first, MAX(s.capture_str) AS last,"
                     f" SUM(d.pick = 1) AS picks, SUM(d.pick = -1) AS rejects, SUM(COALESCE(s.media, 'photo') = 'video') AS videos,"
                     f" SUM(s.capture_str IS NULL) AS undated FROM {sel} LEFT JOIN decisions d ON d.shot_id = s.id").fetchone()
    years = {(r[0] or "")[:4] for r in conn.execute(f"SELECT DISTINCT substr(s.capture_str, 1, 4) FROM {sel}") if r[0]}
    unit, width = ("month", 7) if len(years) <= 1 else ("year", 4)

    def top(sql: str, limit: int = 8) -> list[dict]:
        return [dict(r) for r in conn.execute(sql + f" ORDER BY n DESC LIMIT {limit}")]

    out = {
        "n": t["n"], "first": t["first"], "last": t["last"], "picks": t["picks"] or 0, "rejects": t["rejects"] or 0,
        "videos": t["videos"] or 0, "undated": t["undated"] or 0, "unit": unit,
        "when": [{"key": r[0], "n": r[1]} for r in conn.execute(
            f"SELECT substr(s.capture_str, 1, {width}) AS k, COUNT(*) FROM {sel} WHERE s.capture_str IS NOT NULL GROUP BY k ORDER BY k")],
        "people": top(f"SELECT pp.id, pp.name, COUNT(DISTINCT f.shot_id) AS n FROM temp.summary_ids x JOIN faces f ON f.shot_id = x.id"
                      f" JOIN people pp ON pp.id = f.person_id WHERE pp.name IS NOT NULL AND NOT COALESCE(pp.hidden, 0) GROUP BY pp.id", 10),
        "places": top(f"SELECT pl.id, pl.name, pl.country, COUNT(*) AS n FROM {sel} JOIN places pl ON pl.id = s.place_id GROUP BY pl.id"),
        "events": top(f"SELECT e.id, e.name, COUNT(*) AS n FROM {sel} JOIN events e ON e.id = s.event_id"
                      f" WHERE e.kind = 'event' AND NOT COALESCE(e.hidden, 0) GROUP BY e.id"),
        "cameras": top(f"SELECT s.camera AS name, COUNT(*) AS n FROM {sel} WHERE s.camera IS NOT NULL GROUP BY s.camera"),
        "folders": top(f"SELECT s.dir AS path, COUNT(*) AS n FROM {sel} GROUP BY s.dir"),
    }
    conn.execute("DELETE FROM temp.summary_ids")
    return out


# ------------------------------------------------------------ browsing
@router.get("/api/p/{p}/shots")
def api_shots(p: str, request: Request, conn=Depends(conn_dep)):
    q = request.query_params
    raw = filter_for(conn, q, viewer_of(request))
    if raw.backup and not q.get("offset"):
        copies.ensure(conn, p)                  # filtering by backups: from the latest the index knows
    base = dataclasses.replace(raw)
    flt = raw if q.get("all") in ("1", "true") else browse_filter(conn, dataclasses.replace(raw))
    offset = max(0, _int(q.get("offset"), 0) or 0)
    stack = q.get("stack") in ("1", "true")
    rows, _ = library.page(conn, flt, sort=q.get("sort", "time"), offset=offset,
                           limit=cap(_int(q.get("limit"), 200) or 200, 1000), stack=stack, with_total=False)
    # Duplicate extras have a note of their own; "N hidden" is the thumbnails and utility shots.
    if settings_of(conn)["hide_duplicates"] and flt is not raw:
        raw = without_extras(dataclasses.replace(raw))
    total, hidden, dups = None, 0, None
    if offset == 0 or q.get("total") == "1":
        dup = duplicates_filter(conn, q, base) if offset == 0 else None
        # The filter as asked holds every photo counted, unless it leaves out the extra copies the note counts.
        narrows = "duplicate" not in (base.no_flag or "").split(",")
        total, hidden, dups = browse_counts(conn, flt, raw, dup, stack, base if narrows else None)
    note = {"hidden": bool(settings_of(conn)["hide_duplicates"]), "n": dups} if dups is not None else None
    return {"rows": rows, "total": total, "hidden": hidden, "duplicates": note}


@router.get("/api/p/{p}/offset")
def api_offset(p: str, request: Request, month: str, sort: str = "time", conn=Depends(conn_dep)):
    """Where a month starts in the Library's time-sorted listing (with its filters), so the scrubber
    jumps there without loading every page before it."""
    q = request.query_params
    flt = filter_for(conn, q, viewer_of(request))
    if q.get("all") not in ("1", "true"):
        flt = browse_filter(conn, flt)
    if month != "undated" and not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(400, "A month is written 2024-05.")
    return {"offset": library.month_offset(conn, flt, month, sort)}


@router.post("/api/p/{p}/frames")
def api_frames(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """With bursts stacked, a selection of stacks stands for every matching frame in them: those
    frames' ids, for keywords, albums and favourites."""
    q = body.get("filter") or {}
    flt = filter_for(conn, q, viewer_of(request))
    if str(q.get("all")) not in ("1", "true"):
        flt = browse_filter(conn, flt)
    return {"ids": library.frames(conn, flt, ids_of(body.get("ids")) or [])}


@router.get("/api/p/{p}/rows")
def api_rows(p: str, ids: str, conn=Depends(conn_dep)):
    return {"rows": library.rows_by_ids(conn, ids_of(ids) or [])}


@router.get("/api/p/{p}/stacks")
def api_stacks(p: str, request: Request, conn=Depends(conn_dep)):
    flt = without_extras(browse_filter(conn, filter_for(conn, request.query_params, viewer_of(request))))
    prerender = getattr(request.app.state, "prerender", None)
    if prerender and is_writer(request):
        prerender.start(p, soon=True)          # culling: have the previews ready ahead of you
    return big_json({"stacks": library.stacks(conn, flt)})


def _backups_of(conn, p: str, sid: int) -> dict | None:
    """The viewer's "Backups": each file's copies on other drives, or that it's the only copy. None
    when the index can't be read (the panel then says nothing rather than something wrong)."""
    try:
        return copies.for_shot(conn, p, sid)
    except Exception:
        return None


@router.get("/api/p/{p}/shot/{sid}")
def api_shot(p: str, sid: int, request: Request, conn=Depends(conn_dep)):
    s = library.shot(conn, sid)
    if not s:
        raise HTTPException(404, "That photo isn't in the catalog any more.")
    m = conn.execute("SELECT * FROM metrics WHERE shot_id=?", (sid,)).fetchone()
    files = library.files_of(conn, sid, present_only=False)
    members = []
    if s["group_id"] is not None:
        members = [r[0] for r in conn.execute(
            "SELECT id FROM shots WHERE group_id=? AND status='present' ORDER BY eff_ts, id", (s["group_id"],))]
    sets = conn.execute(
        "SELECT ds.id, ds.kind FROM dup_members dm JOIN dup_sets ds ON ds.id=dm.set_id WHERE dm.shot_id=?", (sid,)).fetchall()
    dup = next((x for x in sets if x["kind"] in ("exact", "copy")), None)
    versions = [x for x in sets if x["kind"] in ("export", "pair")]
    dup_members = []
    if dup:
        dup_members = [dict(r) for r in conn.execute(
            "SELECT dm.shot_id, dm.keep, f.path FROM dup_members dm JOIN files f ON f.id=dm.file_id WHERE dm.set_id=?",
            (dup["id"],))]
    version_rows = []
    for v in versions:
        for r in conn.execute("SELECT dm.shot_id, f.path, s2.stem, s2.edited FROM dup_members dm JOIN files f ON f.id=dm.file_id"
                              " JOIN shots s2 ON s2.id = dm.shot_id WHERE dm.set_id=? AND dm.shot_id != ?", (v["id"], sid)):
            version_rows.append({"shot_id": r[0], "path": r[1], "stem": r[2], "kind": v["kind"], "edited": r[3]})
    if (s.get("orientation") or 1) in (5, 6, 7, 8):
        s["width"], s["height"] = s["height"], s["width"]
    place = conn.execute("SELECT * FROM places WHERE id=?", (s["place_id"],)).fetchone() if s.get("place_id") else None
    event = conn.execute("SELECT id, name, kind, parent_id FROM events WHERE id=?", (s["event_id"],)).fetchone() if s.get("event_id") else None
    trip = conn.execute("SELECT id, name FROM events WHERE id=?", (event["parent_id"],)).fetchone() if event and event["parent_id"] else None
    meta = {}
    for f in files:
        if f["meta"]:
            try:
                meta = json.loads(f["meta"])
                break
            except ValueError:
                pass
    sidecar = meta.get("sidecar") or {}
    offset = conn.execute("SELECT offset_s FROM camera_offsets WHERE camera_key=?",
                          (f"{s.get('camera') or 'unknown camera'}|{s.get('body_serial') or ''}",)).fetchone()
    viewer = viewer_of(request) or "owner"
    return {
        "row": library.rows_by_ids(conn, [sid])[0],
        "exif": {k: s.get(k) for k in ("capture_str", "camera", "body_serial", "lens", "iso", "aperture",
                                       "shutter", "focal", "width", "height", "tz_offset", "gps_lat", "gps_lon",
                                       "gps_alt", "time_src", "duration", "cam_rating", "xmp_label", "c2pa")},
        "software": meta.get("software"),
        "sidecar": sidecar,
        "clock_offset": offset[0] if offset else 0,
        "exposure": library.describe(s),
        "metrics": dict(m) if m else None,
        "why": scoring.explain(conn, sid),
        "files": [{"id": f["id"], "path": f["path"], "kind": f["kind"], "size": f["size"], "status": f["status"]}
                  for f in files],
        "group": {"id": s["group_id"], "members": members},
        "faces": _faces_of(conn, sid),
        "dup": {"id": dup["id"], "kind": dup["kind"], "members": dup_members} if dup else None,
        "versions": version_rows,
        "place": dict(place) if place else None, "place_src": s.get("place_src"),
        "event": dict(event) if event else None, "trip": dict(trip) if trip else None,
        "comments": [dict(r) for r in conn.execute("SELECT id, viewer, text, ts FROM comments WHERE shot_id=? ORDER BY ts", (sid,))],
        "keywords": keywords_mod.of_shot(conn, sid),
        "backups": _backups_of(conn, p, sid),
        "favs": [r[0] for r in conn.execute("SELECT viewer FROM viewer_decisions WHERE shot_id=? AND fav=1", (sid,))],
        "my_fav": bool(conn.execute("SELECT fav FROM viewer_decisions WHERE shot_id=? AND viewer=?", (sid, viewer)).fetchone()),
    }


# ------------------------------------------------------------ decisions
@router.post("/api/p/{p}/decide")
def api_decide(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    items = []
    for it in body.get("items", []):
        try:
            sid = int(it["id"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "Each change needs a photo id.")
        rating, pick, label = it.get("rating"), it.get("pick"), it.get("label")
        try:
            if rating is not None and not 0 <= int(rating) <= 5:
                raise HTTPException(400, "Ratings go from 0 to 5 stars.")
            if pick is not None and int(pick) not in (-1, 0, 1):
                raise HTTPException(400, "pick must be -1, 0 or 1.")
        except (TypeError, ValueError):
            raise HTTPException(400, "Ratings and picks are numbers.")
        if label not in (None, "") and label not in library.LABELS:
            raise HTTPException(400, f"Color labels are {', '.join(library.LABELS)}.")
        items.append({"id": sid, "rating": None if rating is None else int(rating),
                      "pick": None if pick is None else int(pick), "label": label})
    batch, changed = library.decide(conn, items, what=(body.get("label") or "change")[:60], source="web",
                                    viewer=viewer_of(request), batch=body.get("batch"))
    request.app.state.bg.decided(p, len(changed))
    return {"rows": library.rows_by_ids(conn, [i["id"] for i in items]), "batch": batch, "changed": len(changed)}


def _moved_in_time(request: Request, p: str, label: str | None) -> None:
    """Photos that moved on the timeline (a shift or a date undone or redone) restack and regroup."""
    if label and (label.removeprefix("undo ").startswith(("time shifted", "date"))):
        request.app.state.bg.reorganise(p)


@router.post("/api/p/{p}/undo")
def api_undo(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    res = library.undo(conn, viewer=None, batch=body.get("batch"))
    if not res:
        return {"undone": None, "rows": []}
    _moved_in_time(request, p, res["label"])
    return {"undone": res["label"], "batch": res["batch"], "skipped": res["skipped"],
            "rows": library.rows_by_ids(conn, res["ids"])}


@router.post("/api/p/{p}/redo")
def api_redo(p: str, request: Request, conn=Depends(conn_dep)):
    """Redo what you just undid (leaving anything changed since alone): only straight after Undo."""
    require_writer(request)
    res = library.redo(conn, viewer=None)
    if not res:
        return {"redone": None, "rows": []}
    _moved_in_time(request, p, res["label"])
    return {"redone": res["label"], "batch": res["batch"], "skipped": res["skipped"],
            "rows": library.rows_by_ids(conn, res["ids"])}


@router.get("/api/p/{p}/history")
def api_history(p: str, limit: int = 50, conn=Depends(conn_dep)):
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return {"steps": library.history(conn, cap(limit, 500)), "today": library.day_summary(conn, today),
            "week": library.day_summary(conn, today - 6 * 86400), "can_redo": library.can_redo(conn)}


@router.post("/api/p/{p}/date")
def api_set_date(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Give photos without a capture date one ("1998", "1998-06", "1998-06-14"), or take a date you
    gave away (date: null): {ids} or {filter}. In the catalog only, as one undoable step; photos whose
    files carry a date keep it."""
    require_writer(request)
    from ... import dating

    ids = ids_of(body.get("ids"))
    if ids is None and body.get("filter") is not None:
        ids = [r["id"] for r in library.shots(conn, browse_filter(conn, filter_for(conn, body["filter"], viewer_of(request))))]
    try:
        res = dating.set_dates(conn, ids or [], body.get("date"), viewer=viewer_of(request))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"n": len(res["changed"]), "kept": res["kept"], "batch": res["batch"],
            "rows": library.rows_by_ids(conn, res["changed"][:3000])}


@router.post("/api/p/{p}/date/shift")
def api_shift_time(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Move photos' times earlier or later ({ids} or {filter}, seconds): a camera whose clock was wrong,
    when the whole camera can't be corrected at once (two bodies of one model, say). In the catalog only,
    as one undoable step; stacks and events follow in the background."""
    require_writer(request)
    from ... import dating

    ids = ids_of(body.get("ids"))
    if ids is None and body.get("filter") is not None:
        ids = [r["id"] for r in library.shots(conn, browse_filter(conn, filter_for(conn, body["filter"], viewer_of(request))))]
    try:
        res = dating.shift_times(conn, ids or [], float(body.get("seconds") or 0), viewer=viewer_of(request))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e) if isinstance(e, ValueError) and str(e).startswith("Shift") else "Say how far to shift, in seconds.")
    if res["changed"]:
        request.app.state.bg.reorganise(p)
    return {"n": len(res["changed"]), "undated": res["undated"], "batch": res["batch"],
            "rows": library.rows_by_ids(conn, res["changed"][:3000])}


@router.post("/api/p/{p}/accept")
def api_accept(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Turn suggestions into picks/rejects for undecided photos matching the filter."""
    require_writer(request)
    flt = browse_filter(conn, filter_for(conn, body.get("filter") or {}, viewer_of(request)))
    flt.undecided = True
    only = body.get("only")
    items = []
    for r in library.shots(conn, flt):
        sug = r.get("suggestion")
        if sug in ("pick", "reject") and (not only or only == sug):
            items.append({"id": r["id"], "pick": 1 if sug == "pick" else -1})
    batch, changed = library.decide(conn, items, what=f"accept suggested {only or 'picks and rejects'}", source="accept",
                                    viewer=viewer_of(request))
    return {"n": len(changed), "batch": batch, "rows": library.rows_by_ids(conn, changed)}


@router.post("/api/p/{p}/import/camera-ratings")
def api_import_cam_ratings(p: str, request: Request, conn=Depends(conn_dep)):
    """Stars (and rejects) set in camera or written by another app become decisions, for photos you
    haven't decided yet. One undoable step."""
    require_writer(request)
    from ... import portable

    items, to_originals = portable.ratings_from_files(conn)
    batch, changed = library.decide(conn, items, what="ratings from camera and other apps", source="import",
                                    viewer=viewer_of(request))
    return {"n": len(changed), "batch": batch, "originals": to_originals}


# ------------------------------------------------------------ folders
@router.get("/api/p/{p}/folders")
def api_folder_tree(p: str, conn=Depends(conn_dep)):
    """Every folder with photos, as a tree: counts, how much is duplicated, your name for it."""
    names = db.get_setting(conn, "folder_names", {}) or {}
    counts = {r[0]: r[1] for r in conn.execute("SELECT dir, COUNT(*) FROM shots WHERE status='present' GROUP BY dir")}
    dup = {r[0]: r[1] for r in conn.execute(
        "SELECT s.dir, COUNT(DISTINCT s.id) FROM dup_members dm JOIN dup_sets ds ON ds.id=dm.set_id JOIN shots s ON s.id = dm.shot_id"
        " WHERE dm.keep = 0 AND ds.kind IN ('exact','copy') GROUP BY s.dir")}
    spans = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT dir, MIN(capture_str), MAX(capture_str) FROM shots WHERE status='present' GROUP BY dir")}
    roots = [r[0] for r in conn.execute("SELECT path FROM roots")]
    nodes: dict[str, dict] = {}

    def node(path: str) -> dict:
        if path not in nodes:
            nodes[path] = {"path": path, "name": os.path.basename(path) or path, "label": names.get(path),
                           "n": 0, "own": 0, "dups": 0, "children": []}
        return nodes[path]

    for d, n in counts.items():
        root = next((r for r in roots if d == r or d.startswith(r.rstrip("/") + "/")), None)
        cur = node(d)
        cur["own"] = n
        cur["dups"] = dup.get(d, 0)
        cur["from"], cur["to"] = spans.get(d, (None, None))
        path = d
        while root and path != root:
            parent = os.path.dirname(path)
            pn = node(parent)
            if cur not in pn["children"]:
                pn["children"].append(cur)
            cur, path = pn, parent

    def total(nd):
        nd["n"] = nd["own"] + sum(total(c) for c in nd["children"])
        nd["children"].sort(key=lambda c: c["name"].lower())
        return nd["n"]

    out = []
    for r in roots:
        if r in nodes:
            total(nodes[r])
            out.append(nodes[r])
    return {"roots": out}


@router.post("/api/p/{p}/folders/name")
def api_name_folder(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Your name for a folder, shown in JoeyLab only (the folder itself is never renamed)."""
    require_writer(request)
    path = body.get("path") or ""
    names = db.get_setting(conn, "folder_names", {}) or {}
    name = (body.get("name") or "").strip()
    if name:
        names[path] = name[:120]
    else:
        names.pop(path, None)
    db.set_setting(conn, "folder_names", names)
    return {"ok": True}


@router.get("/api/p/{p}/folders/suggest-names")
def api_suggest_folder_names(p: str, conn=Depends(conn_dep)):
    """Better names for generic folders ("SD CARD 20260930"), from their dates, events and places."""
    from ... import events as events_mod

    out = []
    for r in conn.execute(
        "SELECT s.dir, COUNT(*) AS n, MIN(s.capture_str) AS a, MAX(s.capture_str) AS b,"
        " (SELECT e.name FROM shots s2 JOIN events e ON e.id = s2.event_id WHERE s2.dir = s.dir AND e.kind = 'event'"
        "  GROUP BY e.id ORDER BY COUNT(*) DESC LIMIT 1) AS event,"
        " (SELECT p.name FROM shots s3 JOIN places p ON p.id = s3.place_id WHERE s3.dir = s.dir GROUP BY p.id ORDER BY COUNT(*) DESC LIMIT 1) AS place"
        " FROM shots s WHERE s.status='present' GROUP BY s.dir HAVING n >= 5"):
        if events_mod.folder_hint(r["dir"]):
            continue                       # already descriptive
        if not r["a"]:
            continue
        start = r["a"][:7]
        what = None
        if r["event"] and "·" in r["event"]:
            what = r["event"].split("·")[0].strip()
        what = what or r["place"]
        if what:
            out.append({"path": r["dir"], "n": r["n"], "suggestion": f"{start} {what}", "from": r["a"], "to": r["b"]})
    return {"suggestions": out}


# ------------------------------------------------------------ jobs and roots
@router.post("/api/p/{p}/jobs")
def api_start_job(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    if body.get("paths"):
        require_owner(request, "add folders to a library")
    jobs = request.app.state.jobs
    return jobs.start(p, paths=body.get("paths"), force=bool(body.get("force")), quick=bool(body.get("quick"))).as_dict()


@router.get("/api/p/{p}/jobs/current")
def api_job(p: str, request: Request):
    jobs = request.app.state.jobs
    j = jobs.current(p)
    bg = request.app.state.bg
    failed = {k.split(":", 1)[0]: v for k, v in bg.errors.items() if k.endswith(f":{p}")}
    overviews = request.app.state.overviews
    overviews.check(p)                    # the top bar's counts follow changes made by anything
    return {"job": j.as_dict() if j else None, "version": jobs.version(p), "background_errors": failed,
            "overview": overviews.seq(p)}


@router.get("/api/p/{p}/roots")
def api_roots(p: str, conn=Depends(conn_dep)):
    from ... import health, integrity

    seen = health.last_seen(conn)
    out = []
    for r in conn.execute("SELECT id, path FROM roots ORDER BY path"):
        out.append({"id": r["id"], "path": r["path"], "available": os.path.isdir(r["path"]),
                    "files": one(conn, "SELECT COUNT(*) FROM files WHERE root_id=? AND status='present'", (r["id"],)),
                    "missing": one(conn, "SELECT COUNT(*) FROM files WHERE root_id=? AND status='missing'", (r["id"],)),
                    "last_seen": seen.get(r["path"]),
                    "mount": integrity.mount_info(r["path"]) if os.path.isdir(r["path"]) else {}})
    return out


@router.post("/api/p/{p}/roots")
def api_add_root(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    from ... import libraries

    require_owner(request, "add folders to a library")
    jobs = request.app.state.jobs
    check = libraries.check_folder(body.get("path", ""), library=p)
    if not check["ok"] and not (check["empty"] and body.get("empty_ok")):
        raise HTTPException(400, check["reason"])
    try:
        _, note = scanner.add_root(conn, Path(body.get("path", "")).expanduser())
    except (NotADirectoryError, ValueError) as e:
        raise HTTPException(400, f"Can't use that folder: {e}")
    job = jobs.start(p) if body.get("scan", True) else None
    if getattr(jobs, "watcher", None):
        jobs.watcher.request_refresh()
    return {"note": note, "job": job.as_dict() if job else None}


@router.delete("/api/p/{p}/roots/{rid}")
def api_remove_root(p: str, rid: int, request: Request, conn=Depends(conn_dep)):
    require_owner(request, "remove folders from a library")
    from ... import locks

    with locks.project_lock(p, wait=120):        # never alongside an update
        scanner.remove_root(conn, rid)
    request.app.state.jobs.start(p)
    return {"ok": True}


@router.post("/api/p/{p}/prune")
def api_prune(p: str, request: Request, conn=Depends(conn_dep)):
    require_writer(request)
    from ... import locks

    with locks.project_lock(p, wait=120):
        return {"forgotten": scanner.prune_missing(conn, p)}


def _within(path: Path, bases: list[Path]) -> bool:
    return any(path == b or b in path.parents for b in bases)


@router.get("/api/fs")
def api_fs(request: Request, path: str | None = None):
    require_owner(request, "browse its folders")      # folder names on this Mac are the owner's alone
    bases = [Path.home().resolve(), Path("/Volumes")]
    target = Path(path).expanduser().resolve() if path else Path.home().resolve()
    if not _within(target, bases) or not target.is_dir():
        raise HTTPException(400, "Browse folders inside your home folder or on attached drives (/Volumes).")
    dirs, images = [], 0
    try:
        entries = sorted(os.scandir(target), key=lambda e: e.name.lower())
    except PermissionError:
        raise HTTPException(403, "macOS didn't allow reading that folder.")
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                dirs.append({"name": e.name, "path": e.path})
            elif config.kind_for(os.path.splitext(e.name)[1]):
                images += 1
        except OSError:
            continue
    parent = str(target.parent) if target.parent != target and _within(target.parent, bases) else None
    volumes = [str(v) for v in Path("/Volumes").iterdir()] if Path("/Volumes").is_dir() else []
    photos_lib = Path.home() / "Pictures" / "Photos Library.photoslibrary" / "originals"
    return {"path": str(target), "parent": parent, "dirs": dirs, "images": images,
            "places": [str(Path.home())] + [v for v in volumes if not os.path.islink(v)],
            "apple_photos": str(photos_lib) if photos_lib.is_dir() else None}


# ------------------------------------------------------------ everything matching a filter
def _bulk_keyword(conn, k, adding: bool = True) -> int:
    """A keyword given by id or, when adding, by a path typed by hand (made if new). A keyword
    removed since the page loaded comes back when added, as it does when its name is typed."""
    if isinstance(k, bool) or not isinstance(k, (int, str)):
        raise HTTPException(400, "A keyword is given by its id or its name.")
    if not str(k).isdigit():
        if not adding or not keywords_mod.split_path(str(k)):
            raise HTTPException(400, "A keyword is given by its id or its name.")
        return keywords_mod.ensure(conn, str(k))
    row = conn.execute("SELECT COALESCE(deleted, 0) FROM keywords WHERE id=?", (int(k),)).fetchone()
    if not row:
        raise HTTPException(400, f"There's no keyword {k} (removed for good?). Reload and try again.")
    if row[0] and adding:
        return keywords_mod.ensure(conn, keywords_mod.path_of(conn, int(k)))
    return int(k)


@router.post("/api/p/{p}/bulk")
def api_bulk(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Change every photo matching a Library filter (not just the ones loaded on screen), as one
    undoable step: {filter: {...}, rating?, pick?, label?, keywords_add?, keywords_remove?, album?}.
    Returns {n, batch, ids}."""
    require_writer(request)
    flt = browse_filter(conn, filter_for(conn, body.get("filter") or {}, viewer_of(request)))
    ids = [r["id"] for r in library.shots(conn, flt)]
    if body.get("count_only") or not ids:
        return {"n": len(ids), "batch": None, "ids": []}
    who = viewer_of(request)
    if any(k in body for k in ("rating", "pick", "label")):
        items = [{"id": i, "rating": body.get("rating"), "pick": body.get("pick"), "label": body.get("label")} for i in ids]
        what = body.get("what") or "change"
        batch, changed = library.decide(conn, items, what=f"{what} ({len(ids)} matching)"[:60], source="web", viewer=who)
        request.app.state.bg.decided(p, len(changed))
        return {"n": len(changed), "batch": batch, "ids": changed}
    if body.get("keywords_add") or body.get("keywords_remove"):
        add = [_bulk_keyword(conn, k) for k in body.get("keywords_add") or []]
        rem = [_bulk_keyword(conn, k, adding=False) for k in body.get("keywords_remove") or [] if str(k).isdigit()]
        names = [keywords_mod.path_of(conn, k)[-1] for k in add] + [f"−{keywords_mod.path_of(conn, k)[-1]}" for k in rem]
        batch, changed = keywords_mod.assign(conn, ids, add=add, remove=rem, what=f"keywords: {', '.join(names)}"[:60],
                                             source="web", viewer=who)
        return {"n": len(changed), "batch": batch, "ids": changed}
    if body.get("album"):
        from ... import portable

        n = portable.add_to_album(conn, int(body["album"]), ids)
        conn.commit()
        return {"n": n, "batch": None, "ids": ids}
    if "fav" in body:
        import time as _t

        fav = 1 if body.get("fav") else 0
        conn.executemany("INSERT INTO viewer_decisions(viewer, shot_id, fav, updated) VALUES (?,?,?,?)"
                         " ON CONFLICT(viewer, shot_id) DO UPDATE SET fav=excluded.fav, updated=excluded.updated",
                         [(who or "owner", i, fav, _t.time()) for i in ids])
        conn.commit()
        return {"n": len(ids), "batch": None, "ids": ids}
    raise HTTPException(400, "Say what to change: stars, pick, label, keywords, an album or favourite.")


_DATE_ONLY = re.compile(r"^[\d\s–\-,]*((jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\d\s–\-,]*)+$", re.I)


@router.get("/api/p/{p}/jump")
def api_jump(p: str, request: Request, conn=Depends(conn_dep)):
    """Names to jump to from ⌘K: people (by any of their names), keywords, albums, events and trips,
    folders and years."""
    from ... import aliases, identity

    names = db.get_setting(conn, "folder_names", {}) or {}
    others = aliases.everyone(conn)
    folders = [{"path": d, "label": names.get(d), "n": n} for d, n in conn.execute(
        "SELECT dir, COUNT(*) AS n FROM shots WHERE status='present' GROUP BY dir ORDER BY n DESC LIMIT 4000")]
    return {
        "me": identity.person_id(conn, viewer_of(request)),
        "people": [dict(r) | {"aliases": others.get(r["id"], [])} for r in conn.execute(
            "SELECT id, name, COALESCE(kind, 'person') AS kind FROM people WHERE COALESCE(hidden, 0)=0 ORDER BY name COLLATE NOCASE")],
        "keywords": [{"id": k["id"], "path": k["path"], "n": k["total"]} for k in keywords_mod.tree(conn)],
        "albums": [dict(r) for r in conn.execute("SELECT id, name, kind FROM albums ORDER BY name COLLATE NOCASE")],
        # Events still called only by their date add nothing over the years list.
        "events": [dict(r) for r in conn.execute(
            "SELECT id, COALESCE(name, auto_name) AS name, alt_name, folder, kind, n, strftime('%Y', start_ts, 'unixepoch') AS year"
            " FROM events WHERE hidden=0 AND COALESCE(name, auto_name) IS NOT NULL ORDER BY start_ts DESC")
            if not _DATE_ONLY.match(r["name"]) or r["alt_name"] or r["folder"]],
        "folders": folders,
        "years": [{"year": r[0], "n": r[1]} for r in conn.execute(
            "SELECT substr(capture_str, 1, 4) AS y, COUNT(*) FROM shots WHERE status='present' AND capture_str IS NOT NULL"
            " GROUP BY y ORDER BY y DESC")],
    }

