"""Search (field queries, plain language, image search with filters), similar photos, personal
concepts, taste, sets (balanced delivery, best of) and the shot list."""

from __future__ import annotations

import dataclasses
import json

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import identity, library, query as query_mod, scoring, smart, taste
from ..deps import FILTER_KEYS, cap, conn_dep, filter_from, ids_of, one, require_writer, viewer_of
from .library import browse_filter, hidden_by_browsing, settings_of

router = APIRouter()


def _search_mod(conn):
    from ... import search as srch

    return srch if srch.available() and srch.indexed_count(conn) else None


def _collapse(rows: list[dict]) -> list[dict]:
    """One result per burst, with how many similar frames it stands for."""
    seen: dict = {}
    out = []
    for r in rows:
        g = r.get("group_id")
        if g is not None and g in seen:
            seen[g]["similar_n"] = seen[g].get("similar_n", 1) + 1
            continue
        if g is not None:
            seen[g] = r
        out.append(r)
    return out


# How search results can be ordered: by how well they match (best first, or the filter's own order
# when there's nothing to look for in the pictures), or by date or quality.
SEARCH_SORTS = {"newest": "time_desc", "oldest": "time", "quality": "quality"}
RANKED_KEEP = 10000        # a picture search lists at most this many photos, closest first
_RANKED: dict[tuple, dict] = {}             # recent picture searches, ranked, for their later pages


def _pool(conn, key: tuple, fresh: bool, rank, unlikely: bool, sort: str) -> tuple[list[dict], dict]:
    """A picture search's results in the order asked for, and how many match at each level: worked
    out on its first page and kept for the pages after (and "select all"), so they neither shift
    nor search again."""
    if fresh or key not in _RANKED:
        _RANKED.pop(key, None)
        ranked = rank()[:RANKED_KEEP]
        _RANKED[key] = {"ranked": ranked, "pools": {},
                        "counts": {lv: sum(h["level"] == lv for h in ranked) for lv in ("strong", "possible", "unlikely")}}
        while len(_RANKED) > 4:
            _RANKED.pop(next(iter(_RANKED)))
    entry = _RANKED[key]
    if (unlikely, sort) not in entry["pools"]:
        hits = entry["ranked"] if unlikely else [h for h in entry["ranked"] if h.get("level") != "unlikely"]
        entry["pools"][(unlikely, sort)] = _resort(conn, hits, sort)
    return entry["pools"][(unlikely, sort)], entry["counts"]


def _group_ids(conn, ids: list[int]) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    for k in range(0, len(ids), 500):
        chunk = ids[k : k + 500]
        out.update(conn.execute(f"SELECT id, group_id FROM shots WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall())
    return out


def _resort(conn, hits: list[dict], sort: str) -> list[dict]:
    """Matches in date or quality order instead of best match first."""
    if sort not in SEARCH_SORTS:
        return hits
    key = {}
    ids = [h["id"] for h in hits]
    for k in range(0, len(ids), 500):
        chunk = ids[k : k + 500]
        for sid, ts, desc, q in conn.execute(
                f"SELECT s.id, s.sort_ts, s.sort_desc, m.quality FROM shots s LEFT JOIN metrics m ON m.shot_id = s.id"
                f" WHERE s.id IN ({','.join('?' * len(chunk))})", chunk):
            key[sid] = {"oldest": ts or 0, "newest": desc or 0, "quality": -(q or 0)}[sort]
    return sorted(hits, key=lambda h: key.get(h["id"], 0))


@router.get("/api/p/{p}/search")
def api_search(p: str, request: Request, q: str = "", limit: int = 200, offset: int = 0, collapse: int = 1,
               sort: str = "best", unlikely: int = 0, ids_only: int = 0, conn=Depends(conn_dep)):
    """Field queries and plain language become filters; what's left is matched against what the
    photos show. Filters from the query string (e.g. the Library's) also apply. Paged like the
    Library (offset, limit); ids_only gives every match's id (for "select all" and albums)."""
    parsed = query_mod.parse(q, conn, me=identity.person_id(conn, viewer_of(request)))
    params = {**dict(request.query_params), **{k: str(v) for k, v in parsed["filter"].items()}}
    raw = filter_from(params, viewer_of(request))
    flt = browse_filter(conn, dataclasses.replace(raw))
    filtered = any(k for k in parsed["filter"]) or any(k in request.query_params for k in ("status", "person", "event", "place", "album"))
    text = parsed["text"]
    offset, limit = max(0, offset), cap(limit, 1000)
    meta = {"chips": parsed["chips"], "text": text, "filter": parsed["filter"]}
    if not text:
        order = SEARCH_SORTS.get(sort) or parsed["sort"] or "quality"
        if ids_only:
            w, a = flt.sql()
            return {"ids": [r[0] for r in conn.execute(f"SELECT s.id {library.LIGHT_FROM}{w}", a)]}
        rows, total = library.page(conn, flt, sort=order, offset=offset, limit=limit, with_total=offset == 0)
        return {"engine": "filter", "rows": rows, "total": total, "hidden": hidden_by_browsing(conn, raw, flt, total), **meta}
    restrict = None
    if filtered:
        w, a = flt.sql()
        restrict = {r[0] for r in conn.execute(f"SELECT s.id {library.LIGHT_FROM}{w}", a)}
    srch = _search_mod(conn)
    key = (conn.execute("PRAGMA database_list").fetchone()[2], text, bool(collapse),
           tuple(sorted((k, v) for k, v in params.items() if k in FILTER_KEYS or k == "all")))
    if srch:
        def rank():
            hits = srch.search(conn, text, 2 * RANKED_KEEP, restrict=restrict)          # (folding bursts leaves fewer)
            groups = _group_ids(conn, [h["id"] for h in hits])
            return _collapse([h | {"group_id": groups.get(h["id"])} for h in hits]) if collapse else hits

        pool, counts = _pool(conn, key, offset == 0 and not ids_only, rank, bool(unlikely), sort)
        if ids_only:
            return {"ids": [h["id"] for h in pool]}
        hits = pool[offset : offset + limit]
        rows = {r["id"]: r for r in library.rows_by_ids(conn, [h["id"] for h in hits])}
        out = [rows[h["id"]] | {"score": h["score"], "prob": h["prob"], "box": h["box"], "level": h["level"],
                                "why": h["label"], "why_conf": h["label_conf"], "why_text": h["text"], "all_parts": h["all_parts"],
                                **({"similar_n": h["similar_n"]} if h.get("similar_n") else {})}
               for h in hits if h["id"] in rows]
        return {"engine": "siglip2", "indexed": srch.indexed_count(conn), "photos": one(conn, "SELECT COUNT(*) FROM shots WHERE status='present'"),
                "total": len(pool), "counts": counts, "rows": out, "parts": srch.concepts(text), **meta}
    from ... import ai

    if not ai.available():
        raise HTTPException(409, "Search needs the AI layer. Install it with: uv sync --extra ai")

    def rank_clip():
        hits = [{"id": i, "score": round(s, 4)} for i, s in ai.search(conn, text, 5000) if restrict is None or i in restrict]
        groups = _group_ids(conn, [h["id"] for h in hits])
        return _collapse([h | {"group_id": groups.get(h["id"])} for h in hits]) if collapse else hits

    pool, _ = _pool(conn, key + ("clip",), offset == 0 and not ids_only, rank_clip, True, sort)
    if ids_only:
        return {"ids": [h["id"] for h in pool]}
    hits = pool[offset : offset + limit]
    rows = {r["id"]: r for r in library.rows_by_ids(conn, [h["id"] for h in hits])}
    out = [rows[h["id"]] | {"score": h["score"], **({"similar_n": h["similar_n"]} if h.get("similar_n") else {})}
           for h in hits if h["id"] in rows]
    return {"engine": "clip", "rows": out, "total": len(pool), **meta}


@router.get("/api/p/{p}/query/parse")
def api_parse(p: str, q: str, request: Request, conn=Depends(conn_dep)):
    """How JoeyLab reads a search: the filters it found and what's left for the picture match."""
    return query_mod.parse(q, conn, me=identity.person_id(conn, viewer_of(request)))


@router.get("/api/p/{p}/similar/{sid}")
def api_similar(p: str, sid: int, limit: int = 60, conn=Depends(conn_dep)):
    srch = _search_mod(conn)
    if srch:
        hits = srch.similar(conn, sid, cap(limit, 500))
    else:
        from ... import ai

        if not ai.available():
            raise HTTPException(409, "Finding similar photos needs the AI layer: uv sync --extra ai")
        hits = ai.similar(conn, sid, cap(limit, 500))
    rows = {r["id"]: r for r in library.rows_by_ids(conn, [h[0] for h in hits])}
    return {"rows": [rows[i] | {"score": round(s, 4)} for i, s in hits if i in rows]}


# ------------------------------------------------------------ personal concepts
@router.get("/api/p/{p}/concepts")
def api_concepts(p: str, conn=Depends(conn_dep)):
    return {"concepts": [{"id": r["id"], "name": r["name"], "examples": len(json.loads(r["positives"] or "[]")),
                          "cover": (json.loads(r["positives"] or "[]") or [None])[0]}
                         for r in conn.execute("SELECT * FROM concepts ORDER BY created DESC")]}


@router.post("/api/p/{p}/concepts")
def api_make_concept(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    try:
        cid = smart.train_concept(conn, body.get("name") or "My concept", ids_of(body.get("ids")) or [],
                                  ids_of(body.get("not_ids")) or [])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": cid}


@router.get("/api/p/{p}/concepts/{cid}")
def api_concept_matches(p: str, cid: int, conn=Depends(conn_dep)):
    hits = smart.concept_matches(conn, cid)
    rows = {r["id"]: r for r in library.rows_by_ids(conn, [h["id"] for h in hits])}
    c = conn.execute("SELECT name FROM concepts WHERE id=?", (cid,)).fetchone()
    return {"name": c[0] if c else None, "rows": [rows[h["id"]] | {"score": h["score"], "level": h["level"]}
                                                  for h in hits if h["id"] in rows]}


@router.delete("/api/p/{p}/concepts/{cid}")
def api_delete_concept(p: str, cid: int, request: Request, conn=Depends(conn_dep)):
    require_writer(request)
    conn.execute("DELETE FROM concepts WHERE id=?", (cid,))
    conn.commit()
    return {"ok": True}


# ------------------------------------------------------------ taste and sets
@router.post("/api/p/{p}/taste/train")
def api_taste_train(p: str, request: Request, conn=Depends(conn_dep)):
    require_writer(request)
    from ... import locks

    with locks.project_lock(p, wait=120):      # re-scoring rewrites every photo: never alongside an update
        meta_ = taste.train_all()
        scoring.run(conn)
    return meta_


@router.get("/api/p/{p}/taste")
def api_taste(p: str):
    return taste.meta()


@router.get("/api/p/{p}/taste/questions")
def api_taste_questions(p: str, n: int = 10, conn=Depends(conn_dep)):
    ids = smart.taste_questions(conn, min(n, 30))
    return {"rows": library.rows_by_ids(conn, ids)}


@router.post("/api/p/{p}/balance")
def api_balance(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    try:
        target = int(body.get("target") or settings_of(conn)["delivery_target"] or 0)
    except (TypeError, ValueError):
        target = 0
    if not 1 <= target <= 5000:
        raise HTTPException(400, "How many photos do you deliver? Set a target first.")
    res = smart.balance(conn, target, filter_from(body.get("filter") or {}, viewer_of(request)))
    res["rows"] = library.rows_by_ids(conn, res["ids"])
    return res


@router.get("/api/p/{p}/bestof")
def api_best_of(p: str, year: int | None = None, per: str = "month", n: int = 2, event: int | None = None,
                conn=Depends(conn_dep)):
    if per not in ("week", "month", "day"):
        raise HTTPException(400, "per is week, month or day.")
    res = smart.best_of(conn, year, per, max(1, min(n, 12)), event)
    res["rows"] = library.rows_by_ids(conn, res["ids"])
    return res


# ------------------------------------------------------------ shot list
@router.get("/api/p/{p}/shotlist")
def api_shotlist(p: str, conn=Depends(conn_dep)):
    items = smart.shotlist_status(conn)
    ids = {i for it in items for i in it["covered"][:4] + it["candidates"][:4] + it["tagged"][:4]}
    return {"items": items, "rows": library.rows_by_ids(conn, list(ids))}


@router.post("/api/p/{p}/shotlist/{iid}/tag")
def api_shotlist_tag(p: str, iid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Say photos cover a shot-list item ({ids, on: true}) or don't (on: false)."""
    require_writer(request)
    try:
        ids = [int(x) for x in body.get("ids") or []]
    except (TypeError, ValueError):
        raise HTTPException(400, "Photos are given by their ids.")
    if not ids:
        raise HTTPException(400, "Which photos cover it?")
    try:
        tagged = smart.tag_shots(conn, iid, ids, on=body.get("on", True) is not False)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"tagged": tagged}


@router.post("/api/p/{p}/shotlist")
def api_shotlist_add(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "What's the shot? (e.g. Rings, First dance)")
    people = [int(x) for x in body.get("people") or []]
    if body.get("id"):
        conn.execute("UPDATE shotlist SET title=?, query=?, people=?, min_count=? WHERE id=?",
                     (title, body.get("query") or title, json.dumps(people), int(body.get("min_count") or 1), int(body["id"])))
    else:
        conn.execute("INSERT INTO shotlist(title, query, people, min_count) VALUES (?,?,?,?)",
                     (title, body.get("query") if body.get("query") is not None else title, json.dumps(people),
                      int(body.get("min_count") or 1)))
    conn.commit()
    return {"ok": True}


@router.post("/api/p/{p}/shotlist/preset")
def api_shotlist_preset(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """A starting list for a kind of shoot (wedding, birthday, holiday, portrait session)."""
    require_writer(request)
    presets = {
        "wedding": ["Rings", "Getting ready", "First look", "Exchange of vows", "The kiss", "Family formals", "Couple portraits",
                    "Cake cutting", "First dance", "Speeches", "Details: flowers and decor", "Guests dancing"],
        "birthday": ["Birthday person close-up", "Cake with candles", "Blowing out candles", "Opening presents", "Group photo",
                     "Decorations"],
        "holiday": ["Arrival", "Landscape", "Food", "Everyone together", "Sunset", "Local street life"],
        "portrait": ["Head and shoulders", "Full length", "Laughing", "Profile", "Black and white"],
    }
    items = presets.get(body.get("kind") or "")
    if not items:
        raise HTTPException(400, f"Presets: {', '.join(presets)}")
    for i, t in enumerate(items):
        conn.execute("INSERT INTO shotlist(title, query, people, min_count, position) VALUES (?,?,?,?,?)",
                     (t, t.lower(), "[]", 1, i))
    conn.commit()
    return {"added": len(items)}


@router.delete("/api/p/{p}/shotlist/{iid}")
def api_shotlist_delete(p: str, iid: int, request: Request, conn=Depends(conn_dep)):
    require_writer(request)
    conn.execute("DELETE FROM shotlist WHERE id=?", (iid,))
    smart.untag_item(conn, iid)
    conn.commit()
    return {"ok": True}
