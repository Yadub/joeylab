"""The History page: every step (filterable by kind and date) with its photos, and the moves to the
Trash with Put Back, for the Mac's owner only."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import history, library
from ..deps import cap, conn_dep, require_owner


router = APIRouter()


def _day(v: str | None, end: bool = False) -> float | None:
    if not v:
        return None
    try:
        d = datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Dates are written 2026-10-16.")
    return d.timestamp() + (86400 if end else 0)


@router.get("/api/p/{p}/history/steps")
def api_steps(p: str, kind: str = "", since: str = "", until: str = "", offset: int = 0, limit: int = 50,
              conn=Depends(conn_dep)):
    """Steps newest first, with the first few photos each changed. kind: picks, stars, labels,
    keywords, dates or undo; since/until: days (YYYY-MM-DD, both included)."""
    if kind and kind not in history.KINDS:
        raise HTTPException(400, f"kind is one of {', '.join(history.KINDS)}.")
    d = history.steps(conn, kind or None, _day(since), _day(until, end=True), max(0, offset), cap(limit, 200))
    d["rows"] = library.rows_by_ids(conn, list(dict.fromkeys(i for s in d["steps"] for i in s["ids"])))
    return d


@router.get("/api/p/{p}/history/trash")
def api_trash_moves(p: str, request: Request):
    """Moves to the Trash from this library's Trash logs, and what of each is still in the Trash."""
    require_owner(request, "see what was moved to its Trash")
    return {"moves": history.trash_moves(p)}


@router.post("/api/p/{p}/history/putback")
def api_put_back(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Put files from one Trash log back where they were: {log, paths?} (all still in the Trash when
    no paths are given). Only what JoeyLab moved, only to where it was, never over anything."""
    require_owner(request, "put files back from its Trash")
    from ...cleanup import Busy

    paths = body.get("paths")
    if paths is not None and (not isinstance(paths, list) or not all(isinstance(x, str) for x in paths)):
        raise HTTPException(400, "paths is a list of the files' original places.")
    try:
        return history.put_back(conn, p, str(body.get("log") or ""), paths)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Busy as e:
        raise HTTPException(409, str(e))
