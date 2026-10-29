"""RAW + JPEG pairs: the Tidy page's figures, the rules and your choices that decide which RAWs stay,
and moving the RAWs that aren't needed to the Trash, for the Mac's owner only (like every move to the
Trash, the same steps as `joeylab raw-list --trash`):

1. POST …/raws/trash/plan starts a read-only check (in the background: every JPEG that stays is
   read whole).
2. GET …/raws/trash/{token} reports progress, then each RAW with the JPEG that stays, by folder,
   and the ones left alone with why.
3. POST …/raws/trash/{token}/run with the count typed back moves them, each checked again just before.
"""

from __future__ import annotations

import csv
import io
import os
import threading

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import Response

from ... import cleanup, copies, db, library, locks, raws
from ..deps import conn_dep, filter_for, ids_of, require_owner, require_writer, viewer_of
from .library import browse_filter
from .trash import LOCK_WAIT_S, UPDATE_RUNNING, TrashJob, _job, _jobs, reindex

router = APIRouter()


class RawJob(TrashJob):
    """A check of the RAWs that would go, then their move: kept with the other Trash checks, so only
    one move to the Trash runs at a time from the app."""

    def __init__(self, project: str):
        super().__init__(project, "raws")
        self.items: list[raws.RawItem] = []

    def as_dict(self) -> dict:
        d = {"token": self.token, "what": self.what, "state": self.state, "done": self.done, "total": self.total,
             "error": self.error, "result": self.result}
        if self.state != "checking":
            go = raws.going(self.items)
            d["go"] = {"raws": len(go), "bytes": sum(i.size for i in go), "sidecars": sum(len(i.sidecars) for i in go),
                       "sidecar_bytes": sum(s.size for i in go for s in i.sidecars)}
            d["by_folder"] = raws.by_folder(self.items)[:200]
            d["skipped"] = [{"why": w, "n": n} for w, n in raws.skipped(self.items)]
            notes = self.notes or {}
            d["items"] = [{"raw": i.path, "stays": i.stays, "bytes": i.size, "sidecars": [os.path.basename(s.path) for s in i.sidecars],
                           "backup": (notes.get(i.path) or {}).get("backup"), "unchecked_on": (notes.get(i.path) or {}).get("unchecked_on"),
                           "only": i.path in notes and not notes[i.path]["copies"]}
                          for i in go[:300]]
            # The RAW is the file that goes: is it backed up on another drive, or the only copy anywhere?
            d["backups"] = copies.trash_summary({i.path: notes[i.path] for i in go if i.path in notes}, {i.path for i in go}) \
                if notes else None
        return d


def _shot_ids(conn, request: Request, body: dict) -> list[int] | None:
    """The photos a request names: {ids: [...]}, or {filter: {...}} (everything a Library filter
    matches, hidden photos too with all=1), or None for no limit."""
    ids = ids_of(body.get("ids"))
    if ids is not None:
        return ids
    q = body.get("filter")
    if q is None:
        return None
    flt = filter_for(conn, q, viewer_of(request))
    if str(q.get("all")) not in ("1", "true"):
        flt = browse_filter(conn, flt)
    return [s["id"] for s in library.shots(conn, flt)]


@router.get("/api/p/{p}/raws")
def api_raws(p: str, conn=Depends(conn_dep)):
    """The Tidy page: pairs, the space their RAWs take, what stays and why, what isn't needed, by
    year and by folder. Read-only."""
    return {**raws.summary(conn), "roots": [r[0] for r in conn.execute("SELECT path FROM roots")]}


@router.post("/api/p/{p}/raws/rules")
def api_raw_rules(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Turn the rules that keep RAWs on or off: {pick, stars, min_stars, fav, album, edited}."""
    require_writer(request)
    try:
        r = raws.set_rules(conn, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"rules": r}


@router.get("/api/p/{p}/raws/status")
def api_raw_status(p: str, ids: str = "", conn=Depends(conn_dep)):
    """What becomes of these photos' RAWs, and why."""
    st = raws.status(conn, ids_of(ids) or [])
    return {"status": {str(k): v for k, v in st.items()}}


@router.post("/api/p/{p}/raws/choose")
def api_raw_choose(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Keep RAW, RAW not needed, or back to the rules, for photos (ids) or everything a Library filter
    matches: {ids | filter, choice: keep | remove | ""}. One undoable step."""
    require_writer(request)
    choice = body.get("choice") or ""
    if choice not in (raws.KEEP, raws.REMOVE, ""):
        raise HTTPException(400, "The choice is keep, remove or empty (the rules decide).")
    ids = _shot_ids(conn, request, body)
    if ids is None:
        raise HTTPException(400, "Say which photos.")
    r = raws.choose(conn, ids, choice, source="web", viewer=viewer_of(request))
    st = raws.status(conn, r["changed"])
    return {**r, "status": {str(k): v for k, v in st.items()}, "rows": library.rows_by_ids(conn, r["changed"][:2000])}


@router.post("/api/p/{p}/raws/count")
def api_raw_count(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """What becomes of the RAWs of photos ({ids} or a Library {filter}), by state, with each state's
    photos: for acting on a whole event or selection."""
    ids = _shot_ids(conn, request, body)
    if ids is None:
        raise HTTPException(400, "Say which photos.")
    return raws.count(conn, ids)


@router.post("/api/p/{p}/raws/trash/plan")
def api_raw_plan(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Check what moving the RAWs you rejected would do (all of them, or those of {ids} or a {filter};
    with include_unkept, also those no rule keeps). Read-only."""
    require_owner(request)
    ids = _shot_ids(conn, request, body)
    job = RawJob(p)

    def work():
        c = db.connect(p, create=False)
        try:
            job.items = raws.plan(c, ids, progress=lambda n, total: (setattr(job, "done", n), setattr(job, "total", total)),
                                  include_unkept=bool(body.get("include_unkept")))
            job.note_backups(c, [i.path for i in raws.going(job.items)])
            job.state = "ready"
        except Exception as e:                       # shown in the dialog
            job.state, job.error = "error", str(e)
        finally:
            c.close()

    _jobs(request)[job.token] = job
    threading.Thread(target=work, name=f"raw-plan:{p}", daemon=True).start()
    return job.as_dict()


def _raw_job(request: Request, p: str, token: str) -> RawJob:
    job = _job(request, p, token)
    if not isinstance(job, RawJob):
        raise HTTPException(404, "That check has expired. Start again.")
    return job


@router.get("/api/p/{p}/raws/trash/{token}")
def api_raw_plan_status(p: str, token: str, request: Request):
    require_owner(request)
    return _raw_job(request, p, token).as_dict()


@router.get("/api/p/{p}/raws/trash/{token}/list.tsv")
def api_raw_plan_list(p: str, token: str, request: Request):
    """Every RAW the check found, with the JPEG that stays, its sidecars, or why it's left alone."""
    require_owner(request)
    job = _raw_job(request, p, token)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t")
    w.writerow(["RAW", "goes", "JPEG that stays", "bytes", "sidecars that go with it"])
    for i in job.items:
        goes = i.category == cleanup.TWIN
        w.writerow([i.path, "yes" if goes else f"no: {i.why}", i.stays or "", i.size, " ".join(s.path for s in i.sidecars)])
    return Response(buf.getvalue(), media_type="text/tab-separated-values",
                    headers={"Content-Disposition": f'attachment; filename="{p}-raws-to-move.tsv"'})


@router.post("/api/p/{p}/raws/trash/{token}/run")
def api_raw_run(p: str, token: str, request: Request, body: dict = Body(default_factory=dict)):
    """Move the RAWs the check found, once their count is typed back."""
    require_owner(request)
    job = _raw_job(request, p, token)
    if job.state != "ready":
        raise HTTPException(409, "The check isn't finished, or these were moved already.")
    if any(j.project == p and j.state == "moving" for j in _jobs(request).values()):
        raise HTTPException(409, "Something is already being moved to the Trash; wait for it to finish.")
    go = raws.going(job.items)
    try:
        typed = int(str(body.get("count", "")).replace(",", "").strip())
    except ValueError:
        typed = -1
    if not go or typed != len(go):
        raise HTTPException(400, f"Type {len(go)} to confirm." if go else "There's nothing to move.")
    job.state, job.done, job.total = "moving", 0, len(go)
    bg = request.app.state.bg

    def work():
        c = db.connect(p, create=False)
        try:
            with locks.project_lock(p, wait=LOCK_WAIT_S):        # never alongside an update (see routes/trash.py)
                log = raws.trash(c, job.items, f"{p}-files", progress=lambda n, total: setattr(job, "done", n))
            done, size = raws.moved(go)
            job.result = {"moved": len(done), "bytes": size, "sidecars": sum(s.result == cleanup.MOVED for i in done for s in i.sidecars),
                          "log": log, "ids": sorted({i.shot_id for i in done}),
                          "left": [{"path": i.path, "why": i.result} for i in go if i.result != cleanup.MOVED][:50]}
            if done:
                bg.rescore(p)                # "fixable in editing" needs a RAW
                reindex(c, p)
            job.state = "done"
        except locks.Busy:
            job.state, job.error = "error", UPDATE_RUNNING
        except Exception as e:                       # shown in the dialog (another move running, say)
            job.state, job.error = "error", str(e)
        finally:
            c.close()

    threading.Thread(target=work, name=f"raw-run:{p}", daemon=True).start()
    return job.as_dict()
