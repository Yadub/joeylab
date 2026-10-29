"""Places (offline reverse geocoding, a map), your homes and the trip distance, and events and trips."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import events as events_mod, geo, library
from ..deps import cap, conn_dep, require_owner, require_writer

router = APIRouter()


def _trips_busy(request: Request, p: str) -> bool:
    return request.app.state.bg.busy(f"regroup:trips:{p}")


def _keep_trips_current(request: Request, p: str, conn) -> bool:
    """Trips found by other homes or another distance than now (changed in another library, say)
    are found again in the background. Whether that's under way."""
    if not _trips_busy(request, p) and p not in request.app.state.jobs.running and events_mod.stale(conn):
        request.app.state.bg.retrip(p)
    return _trips_busy(request, p)


def _rule(request: Request, p: str, conn) -> dict:
    r = geo.rule(conn)
    return {**r, "map_tiles": geo.map_tiles(conn), "busy": _trips_busy(request, p), "available": geo.available()}


@router.get("/api/p/{p}/places")
def api_places(p: str, request: Request, conn=Depends(conn_dep)):
    busy = _keep_trips_current(request, p, conn)
    places = geo.summary(conn)
    countries: dict[str, dict] = {}
    for pl in places:
        c = countries.setdefault(pl["cc"] or "?", {"cc": pl["cc"], "country": pl["country"], "n": 0, "places": []})
        c["n"] += pl["n"]
        c["places"].append(pl)
    covers = {}
    for pl in places[:200]:
        r = conn.execute("SELECT s.id FROM shots s JOIN metrics m ON m.shot_id = s.id WHERE s.place_id=? AND s.status='present'"
                         " AND COALESCE(m.flags,'') NOT LIKE '%\"tiny\"%' ORDER BY m.quality DESC LIMIT 1", (pl["id"],)).fetchone()
        if r:
            covers[pl["id"]] = r[0]
    rows = library.rows_by_ids(conn, list(covers.values()))
    counts = conn.execute("SELECT SUM(gps_lat IS NOT NULL), SUM(place_src='inferred'), SUM(place_id IS NULL), COUNT(*),"
                          " SUM(place_src='folder'), SUM(place_src='manual') FROM shots WHERE status='present'").fetchone()
    return {"countries": sorted(countries.values(), key=lambda c: -c["n"]), "covers": covers, "rows": rows,
            "gps": counts[0] or 0, "inferred": counts[1] or 0, "unknown": counts[2] or 0, "total": counts[3] or 0,
            "folder": counts[4] or 0, "manual": counts[5] or 0, **_rule(request, p, conn), "busy": busy}


@router.get("/api/places/search")
def api_place_search(q: str = "", limit: int = 10):
    """Towns in the offline town list whose name starts so (for homes and an event's place)."""
    if not geo.available():
        return {"towns": [], "available": False}
    return {"towns": geo.Gazetteer.get().search(q[:80], cap(limit, 25)), "available": True}


@router.get("/api/p/{p}/places/prefs")
def api_place_prefs(p: str, request: Request, conn=Depends(conn_dep)):
    """Your homes (or JoeyLab's guess), the trip distance and the street map: for the whole Mac."""
    return _rule(request, p, conn)


@router.post("/api/p/{p}/places/prefs")
def api_set_place_prefs(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Change them: {homes: [town ids], km, map_tiles}. Changing homes or the distance finds this
    library's trips again straight away, in the background (other libraries' when next opened)."""
    require_owner(request)
    if "map_tiles" in body:
        if not isinstance(body["map_tiles"], bool):
            raise HTTPException(400, "map_tiles is true or false.")
        geo.set_map_tiles(body["map_tiles"])
    if "km" in body:
        km = body["km"]
        if isinstance(km, bool) or not isinstance(km, (int, float)) or not 1 <= km <= 20000:
            raise HTTPException(400, "The trip distance is a number of kilometres, from 1 to 20,000.")
        geo.set_trip_km(int(km))
    if "homes" in body:
        ids = body["homes"]
        if not isinstance(ids, list) or len(ids) > geo.MAX_HOMES or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise HTTPException(400, f"Homes are a list of up to {geo.MAX_HOMES} towns.")
        try:
            geo.set_homes(conn, ids)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if "km" in body or "homes" in body:
        request.app.state.bg.retrip(p)
    return _rule(request, p, conn)


@router.get("/api/p/{p}/places/points")
def api_points(p: str, conn=Depends(conn_dep)):
    return {"points": geo.points(conn)}


@router.post("/api/p/{p}/places/refresh")
def api_places_refresh(p: str, request: Request, conn=Depends(conn_dep)):
    """Download the town list if needed and name every photo's place now."""
    require_writer(request)
    from ... import locks

    log = []
    with locks.project_lock(p, wait=120):      # rebuilds events: never alongside an update
        res = geo.assign(conn, log=log.append)
        events_mod.run(conn)
    return {**res, "log": log}


@router.get("/api/p/{p}/events")
def api_events(p: str, request: Request, kind: str | None = None, year: int | None = None, hidden: int = 0,
               conn=Depends(conn_dep)):
    """Events and trips (with hidden=1, the ones you hid), how many are hidden, and what decides trips."""
    if year is not None and not 1826 <= year <= 2200:
        raise HTTPException(400, "That isn't a year photos were taken in.")
    busy = _keep_trips_current(request, p, conn)
    evs = events_mod.listing(conn, kind, year, hidden=bool(hidden))
    covers = [e["cover_shot_id"] for e in evs if e["cover_shot_id"]]
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM people")}
    for e in evs:
        e["people"] = [[names.get(pid), k] for pid, k in e["people"] if names.get(pid)]
    years: dict[int, int] = defaultdict(int)
    for e in events_mod.listing(conn, "event"):
        if e["start_ts"] is not None:
            years[datetime.fromtimestamp(e["start_ts"], timezone.utc).year] += 1
    n_hidden = conn.execute("SELECT COUNT(*) FROM events WHERE hidden = 1" + (" AND kind = ?" if kind else ""),
                            (kind,) if kind else ()).fetchone()[0]
    return {"events": evs, "rows": library.rows_by_ids(conn, covers), "years": sorted(years.items(), reverse=True),
            "hidden": n_hidden, "rule": geo.rule(conn), "busy": busy}


@router.get("/api/p/{p}/events/{eid}")
def api_event(p: str, eid: int, conn=Depends(conn_dep)):
    e = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    if not e:
        raise HTTPException(404, "That event isn't there any more (events are re-detected on each update).")
    d = dict(e)
    d["place"] = json.loads(d["place"]) if d.get("place") else None
    children = [dict(r) for r in conn.execute("SELECT id, name, auto_name, user_named, kind, start_ts, end_ts, n, cover_shot_id, hidden,"
                                              " place FROM events WHERE parent_id=? ORDER BY start_ts", (eid,))]
    for c in children:
        c["place"] = json.loads(c["place"]) if c.get("place") else None
    flt = library.Filter(event_id=eid) if d["kind"] == "event" else None
    if d["kind"] == "trip":
        ids = [r[0] for r in conn.execute("SELECT s.id FROM shots s JOIN events e ON e.id = s.event_id WHERE e.parent_id=?"
                                          " AND s.status='present'", (eid,))]
        flt = library.Filter(ids=ids)
    best, _ = library.page(conn, flt, sort="quality", limit=12, stack=True)
    return {"event": d, "children": children, "best": best,
            "rows": library.rows_by_ids(conn, [c["cover_shot_id"] for c in children if c["cover_shot_id"]])}


@router.post("/api/p/{p}/events/{eid}/name")
def api_event_name(p: str, eid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    events_mod.rename(conn, eid, body.get("name"))
    return {"ok": True}


@router.post("/api/p/{p}/events/{eid}/hidden")
def api_event_hidden(p: str, eid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    conn.execute("UPDATE events SET hidden=? WHERE id=?", (int(bool(body.get("hidden", True))), eid))
    conn.commit()
    return {"ok": True}


@router.get("/api/p/{p}/events/{eid}/neighbours")
def api_event_neighbours(p: str, eid: int, conn=Depends(conn_dep)):
    """The events (or trips) either side of this one in time, to merge it with."""
    return {"events": events_mod.neighbours(conn, eid)}


@router.post("/api/p/{p}/events/{eid}/merge")
def api_event_merge(p: str, eid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Merge another event (or trip) into this one: {other}. Any between the two join too. This one's
    name stays, and the merge holds through every update."""
    require_writer(request)
    from ... import locks

    try:
        other = int(body.get("other"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Say which event to merge with.")
    try:
        with locks.project_lock(p, wait=120):      # events are rebuilt by updates: never alongside one
            return events_mod.merge(conn, eid, other)
    except locks.Busy:
        raise HTTPException(409, "The library is updating. Merge them once it has finished.")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/p/{p}/events/unmerge")
def api_event_unmerge(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Undo a merge, from what the merge returned ({undo}): the events come back apart, with their
    names, and events and trips are found again in the background."""
    require_writer(request)
    from ... import locks

    undo = body.get("undo")
    if not isinstance(undo, dict):
        raise HTTPException(400, "Say which merge to undo.")
    try:
        with locks.project_lock(p, wait=120):
            r = events_mod.unmerge(conn, undo)
    except locks.Busy:
        raise HTTPException(409, "The library is updating. Undo the merge once it has finished.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    request.app.state.bg.retrip(p)
    return {**r, "busy": True}


@router.post("/api/p/{p}/events/{eid}/place")
def api_event_place(p: str, eid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Your place for an event or a trip: {place_id} (a town), or null to take yours away. Set on its
    photos without GPS; trips are found again straight away, in the background."""
    require_writer(request)
    pid = body.get("place_id")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)):
        raise HTTPException(400, "Choose a town from the list.")
    try:
        r = events_mod.set_place(conn, eid, pid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    request.app.state.bg.retrip(p)
    return {**r, "busy": True}
