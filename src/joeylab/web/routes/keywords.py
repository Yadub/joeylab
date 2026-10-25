"""The keyword tree: list it, tag photos (undoable), rename, move, merge and remove keywords, and
bring in keywords other apps wrote into .xmp sidecars."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import keywords
from ..deps import conn_dep, ids_of, require_writer, viewer_of

router = APIRouter()


@router.get("/api/p/{p}/keywords")
def api_keywords(p: str, conn=Depends(conn_dep)):
    return {"tree": keywords.tree(conn)}


@router.post("/api/p/{p}/keywords")
def api_new_keyword(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Make a keyword from a path like "Activities › Climbing" (missing parents are made too)."""
    require_writer(request)
    try:
        kid = keywords.ensure(conn, body.get("path") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": kid, "path": " › ".join(keywords.path_of(conn, kid))}


def _resolve(conn, values) -> list[int]:
    """Keyword ids, or paths typed by hand (made if new)."""
    out = []
    for v in values or []:
        if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
            if conn.execute("SELECT 1 FROM keywords WHERE id=?", (int(v),)).fetchone():
                out.append(int(v))
        elif isinstance(v, str) and keywords.split_path(v):
            out.append(keywords.ensure(conn, v))
    return out


@router.post("/api/p/{p}/keywords/assign")
def api_assign(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Add and/or remove keywords on photos: one step in the history, undoable like a rating."""
    require_writer(request)
    ids = ids_of(body.get("ids")) or []
    if not ids:
        raise HTTPException(400, "Choose some photos first.")
    try:
        add = _resolve(conn, body.get("add"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    remove = [int(v) for v in body.get("remove") or [] if str(v).isdigit()]
    names = [keywords.path_of(conn, k)[-1] for k in add] + [f"−{keywords.path_of(conn, k)[-1]}" for k in remove
                                                               if keywords.path_of(conn, k)]
    batch, changed = keywords.assign(conn, ids, add=add, remove=remove, what=f"keywords: {', '.join(names)}"[:60],
                                     source="web", viewer=viewer_of(request))
    return {"batch": batch, "n": len(changed), "added": add, "removed": remove}


@router.post("/api/p/{p}/keywords/{kid}")
def api_edit_keyword(p: str, kid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """{name} renames, {parent_id} moves (null: to the top), {merge_into} folds it into another."""
    require_writer(request)
    try:
        if body.get("merge_into"):
            return keywords.merge(conn, kid, int(body["merge_into"]), viewer=viewer_of(request))
        if "name" in body:
            keywords.rename(conn, kid, body["name"])
        if "parent_id" in body:
            keywords.move(conn, kid, int(body["parent_id"]) if body["parent_id"] not in (None, "") else None)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.delete("/api/p/{p}/keywords/{kid}")
def api_delete_keyword(p: str, kid: int, request: Request, conn=Depends(conn_dep)):
    """Take a keyword off every photo and out of the tree (its sub-keywords move up a level)."""
    require_writer(request)
    return keywords.delete(conn, kid, viewer=viewer_of(request))


@router.post("/api/p/{p}/import/keywords")
def api_import_keywords(p: str, request: Request, conn=Depends(conn_dep)):
    """Keywords that Lightroom, Capture One or another app wrote into .xmp sidecars."""
    require_writer(request)
    return keywords.from_sidecars(conn)


@router.get("/api/p/{p}/keywords/of")
def api_keywords_of(p: str, ids: str = "", conn=Depends(conn_dep)):
    """Keywords on a set of photos, with how many of them carry each (for the tagging dialog)."""
    sel = ids_of(ids) or []
    if not sel:
        return {"keywords": []}
    q = ",".join("?" * len(sel))
    rows = conn.execute(f"SELECT keyword_id, COUNT(*) FROM shot_keywords WHERE shot_id IN ({q}) GROUP BY keyword_id", sel).fetchall()
    all_paths = keywords.paths(conn)
    return {"keywords": [{"id": k, "path": " › ".join(all_paths.get(k, [])), "n": n} for k, n in rows],
            "photos": len(sel)}
