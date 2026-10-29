"""Library › Explore: the library on one surface, arranged by what's in the photos, colour, light,
time, quality or camera (explore.py). The Library's filters choose which photos show; positions
come from the whole library, so they stay put."""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, Depends, HTTPException, Request

from ... import colours, explore
from ..deps import big_json, conn_dep, filter_for, viewer_of
from .library import browse_filter

router = APIRouter()


@router.get("/api/p/{p}/explore")
def api_explore(p: str, request: Request, conn=Depends(conn_dep)):
    """The photos the Library's filters show (as its grid would), the arrangements that can be made
    here (True, or why not), and how far measuring the photos' colours is."""
    q = request.query_params
    raw = filter_for(conn, q, viewer_of(request))
    flt = raw if q.get("all") in ("1", "true") else browse_filter(conn, dataclasses.replace(raw))
    return big_json({"shown": explore.shown_ids(conn, flt), "available": explore.available(conn), "measuring": colours.backfill(p)})


@router.get("/api/p/{p}/explore/photo/{sid}")
def api_explore_photo(p: str, sid: int, conn=Depends(conn_dep)):
    """What a photo's places are made from, for the peek: its palette, brightness, sharpness..."""
    return explore.facts(conn, sid)


@router.get("/api/p/{p}/explore/{by}")
def api_explore_layout(p: str, by: str, conn=Depends(conn_dep)):
    """Every analysed photo's cell in one arrangement, with what to draw to explain it, or how far
    making it is."""
    try:
        return big_json(explore.layout(conn, p, by))
    except ValueError as e:
        raise HTTPException(400, str(e))
