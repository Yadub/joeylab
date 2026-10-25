"""Moving and renaming photos and folders from the app, for the Mac's owner only (`moves.py` does
the work and keeps the log).

1. POST …/moves/plan works out what would happen, read-only: {op: "move_photos", ids | filter, into},
   {op: "rename_photos", ids | filter, pattern, start}, {op: "move_folder", path, into},
   {op: "rename_folder", path, name} or {op: "new_folder", parent, name}. With preview: true it's
   only shown (a rename pattern as it's typed); otherwise it's kept for step 2 under a token.
2. POST …/moves/{token}/run {keep_both} does it, each unit checked again just before it moves, in
   the background (it waits for an update of the library to finish first); GET …/moves/{token}
   follows it.
3. GET …/moves lists every move and rename, newest first; POST …/moves/{id}/back moves one back
   ({run: true} starts at once). The History page lists these too.

GET …/moves/folders is the library's folder tree for choosing where to move (with path=…, the
folders inside one folder as they are on disk, empty ones too).
"""

from __future__ import annotations

import threading
import time
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import db, dupes, library, locks, moves
from ..deps import conn_dep, filter_from, ids_of, require_owner, viewer_of
from .library import browse_filter
from .trash import reindex

router = APIRouter()
EXPIRE_S = 3600


class MoveJob:
    def __init__(self, project: str, plan: moves.Plan):
        self.token = uuid.uuid4().hex
        self.project, self.plan = project, plan
        self.state = "ready"                 # ready | waiting | moving | done | error
        self.done = self.total = 0
        self.created = time.time()
        self.result: dict | None = None
        self.error: str | None = None

    def as_dict(self) -> dict:
        return {"token": self.token, "state": self.state, "done": self.done, "total": self.total,
                "plan": self.plan.as_dict(), "result": self.result, "error": self.error}


def _jobs(request: Request) -> dict:
    jobs = request.app.state.move_jobs
    for t in [t for t, j in jobs.items() if time.time() - j.created > EXPIRE_S and j.state in ("ready", "done", "error")]:
        jobs.pop(t, None)
    return jobs


def _ids(conn, request: Request, body: dict) -> list[int]:
    """The photos chosen: ids, or everything matching a Library filter (as the selection bar sends)."""
    ids = ids_of(body.get("ids"))
    if ids:
        return ids
    if body.get("filter") is not None:
        flt = browse_filter(conn, filter_from(body.get("filter") or {}, viewer_of(request)))
        return [r["id"] for r in library.shots(conn, flt)]
    raise HTTPException(400, "Say which photos: ids, or a filter.")


def _start(request: Request, p: str, job: MoveJob, keep_both: bool) -> None:
    bg = request.app.state.bg

    def work():
        conn = db.connect(p, create=False)
        try:
            job.state = "moving"
            job.result = moves.run(conn, job.plan, keep_both=keep_both, wait=1800,
                                   on_wait=lambda: setattr(job, "state", "waiting"),
                                   progress=lambda k, n: (setattr(job, "done", k), setattr(job, "total", n),
                                                          setattr(job, "state", "moving")))
            if job.result.get("photos"):
                dupes.rekeep(conn)             # which copy of a duplicate is kept goes by its folder
                bg.reorganise(p)               # stacks, scores and events (named after folders) follow
            reindex(conn, p)                   # the index of backups follows the files to their new place
            job.state = "done"
        except (moves.Refused, locks.Busy) as e:
            job.state, job.error = "error", str(e)
        except Exception as e:             # shown in the dialog
            job.state, job.error = "error", f"{e}"
        finally:
            conn.close()

    job.state = "moving"
    threading.Thread(target=work, name=f"move:{p}", daemon=True).start()


@router.post("/api/p/{p}/moves/plan")
def api_move_plan(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_owner(request, "move or rename files")
    op = body.get("op")
    try:
        if op == "move_photos":
            plan = moves.plan_move_photos(conn, p, _ids(conn, request, body), str(body.get("into") or ""))
        elif op == "rename_photos":
            try:
                start = max(0, int(body.get("start") or 1))
            except (TypeError, ValueError):
                raise HTTPException(400, "The counter starts at a number.")
            plan = moves.plan_rename_photos(conn, p, _ids(conn, request, body), str(body.get("pattern") or ""), start)
        elif op == "move_folder":
            plan = moves.plan_move_folder(conn, p, str(body.get("path") or ""), str(body.get("into") or ""))
        elif op == "rename_folder":
            plan = moves.plan_rename_folder(conn, p, str(body.get("path") or ""), str(body.get("name") or ""))
        elif op == "new_folder":
            plan = moves.plan_new_folder(conn, p, str(body.get("parent") or ""), str(body.get("name") or ""))
        else:
            raise HTTPException(400, "op is one of move_photos, rename_photos, move_folder, rename_folder, new_folder.")
    except moves.Refused as e:
        raise HTTPException(400, str(e))
    if body.get("preview"):
        return {"token": None, "state": "preview", "plan": plan.as_dict()}
    job = MoveJob(p, plan)
    _jobs(request)[job.token] = job
    return job.as_dict()


@router.post("/api/p/{p}/moves/{token}/run")
def api_move_run(p: str, token: str, request: Request, body: dict = Body(default_factory=dict)):
    require_owner(request, "move or rename files")
    job = _jobs(request).get(token)
    if not job or job.project != p:
        raise HTTPException(404, "That plan has expired. Start again.")
    if job.plan.refused:
        raise HTTPException(400, job.plan.refused)
    if job.state != "ready":
        raise HTTPException(409, "That's under way or done already.")
    _start(request, p, job, bool(body.get("keep_both")))
    return job.as_dict()


@router.get("/api/p/{p}/moves/folders")
def api_move_folders(p: str, request: Request, path: str = "", conn=Depends(conn_dep)):
    """The library's folders (with path=…, the folders inside one, as they are on disk)."""
    require_owner(request, "move or rename files")
    try:
        return {"folders": moves.subfolders(conn, path) if path else moves.folders(conn)}
    except moves.Refused as e:
        raise HTTPException(400, str(e))


@router.get("/api/p/{p}/moves")
def api_moves(p: str, request: Request, limit: int = 100):
    """Every move and rename in this library, newest first, from the move logs."""
    require_owner(request, "see what was moved")
    return {"moves": moves.history(p, max(1, min(int(limit), 500)))}


@router.get("/api/p/{p}/moves/{token}")
def api_move_status(p: str, token: str, request: Request):
    require_owner(request, "move or rename files")
    job = _jobs(request).get(token)
    if not job or job.project != p:
        raise HTTPException(404, "That plan has expired. Start again.")
    return job.as_dict()


@router.post("/api/p/{p}/moves/{oid}/back")
def api_move_back(p: str, oid: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Move back one earlier move or rename: its plan, or ({run: true}) done at once."""
    require_owner(request, "move or rename files")
    try:
        plan = moves.plan_back(conn, p, oid)
    except moves.Refused as e:
        raise HTTPException(400, str(e))
    job = MoveJob(p, plan)
    _jobs(request)[job.token] = job
    if body.get("run"):
        if plan.refused:
            raise HTTPException(400, plan.refused)
        _start(request, p, job, False)
    return job.as_dict()


@router.post("/api/p/{p}/moves/recover")
def api_move_recover(p: str, request: Request, conn=Depends(conn_dep)):
    """Finish (or roll back) a move that stopped half way, from its log."""
    require_owner(request, "move or rename files")
    try:
        with locks.project_lock(p, wait=30):
            return {"recovered": moves.recover(conn, p)}
    except locks.Busy as e:
        raise HTTPException(409, str(e))
