"""Moving folders to another library, for the Mac's owner (`split.py` does the work and keeps the log).

1. POST …/split/plan {folders, to, purpose} works out what would move, read-only, with the sentence
   shown before it's confirmed.
2. POST …/split/run (the same body) does it in the background; GET /api/split/{token} follows it.
   Both libraries are then regrouped in the background.
3. GET …/split lists the folders the library leaves out and the moves to and from it; POST
   …/split/include {folder} stops leaving one out (when no other library has it), and POST
   /api/split/{id}/back moves an earlier move back.
"""

from __future__ import annotations

import threading
import time
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import locks, split
from ..deps import conn_dep, require_owner

router = APIRouter()
EXPIRE_S = 3600


class SplitJob:
    def __init__(self, src: str, dst: str):
        self.token = uuid.uuid4().hex
        self.src, self.dst = src, dst
        self.state = "moving"                # waiting | moving | done | error
        self.stage = "Starting"
        self.created = time.time()
        self.result: dict | None = None
        self.error: str | None = None

    def as_dict(self, bg=None) -> dict:
        return {"token": self.token, "src": self.src, "dst": self.dst, "state": self.state, "stage": self.stage,
                "result": self.result, "error": self.error,
                "regrouping": [x for x in (self.src, self.dst) if bg is not None and bg.busy(f"regroup:move:{x}")]}


def _jobs(request: Request) -> dict:
    jobs = getattr(request.app.state, "split_jobs", None)
    if jobs is None:
        jobs = request.app.state.split_jobs = {}
    for t in [t for t, j in jobs.items() if time.time() - j.created > EXPIRE_S and j.state in ("done", "error")]:
        jobs.pop(t, None)
    return jobs


def _folders(body: dict) -> list[str]:
    f = body.get("folders")
    if isinstance(f, str):
        f = [f]
    if not isinstance(f, list) or not all(isinstance(x, str) for x in f):
        raise HTTPException(400, "Say which folders: a list of their paths.")
    return f


def _after(request: Request, op: dict) -> None:
    """Both libraries regrouped in the background (stacks, duplicates, events, scores, search), and every
    page told the catalogs changed."""
    bg, jobs = request.app.state.bg, request.app.state.jobs
    for lib in (op["src"], op["dst"]):
        if lib in split.config.list_projects():
            bg._go(f"regroup:move:{lib}", lib, lambda conn, lib=lib: split.recompute(conn, lib), wait=1800)
            jobs._bump(lib)
    if getattr(jobs, "watcher", None):
        jobs.watcher.request_refresh()


def _start(request: Request, job: SplitJob, work) -> None:
    def go():
        try:
            job.result = work(lambda: setattr(job, "state", "waiting"), lambda m: (setattr(job, "stage", m), setattr(job, "state", "moving")))
            job.state, job.stage = "done", "Moved"
            _after(request, job.result)
        except (split.Refused, locks.Busy) as e:
            job.state, job.error = "error", str(e)
        except Exception as e:              # shown in the dialog
            job.state, job.error = "error", f"{e}"

    threading.Thread(target=go, name=f"split:{job.src}", daemon=True).start()


@router.post("/api/p/{p}/split/plan")
def api_split_plan(p: str, request: Request, body: dict = Body(default_factory=dict)):
    require_owner(request, "move folders to another library")
    try:
        return split.plan(p, _folders(body), str(body.get("to") or ""), body.get("purpose") or None)
    except split.Refused as e:
        raise HTTPException(400, str(e))


@router.post("/api/p/{p}/split/run")
def api_split_run(p: str, request: Request, body: dict = Body(default_factory=dict)):
    require_owner(request, "move folders to another library")
    folders, to, purpose = _folders(body), str(body.get("to") or ""), body.get("purpose") or None
    try:
        chk = split.check(p, folders, to, purpose)
    except split.Refused as e:
        raise HTTPException(400, str(e))
    job = SplitJob(p, chk["dst"])
    _jobs(request)[job.token] = job
    _start(request, job, lambda on_wait, say: split.run(p, folders, to, purpose, lock_wait=1800, on_wait=on_wait, progress=say))
    return job.as_dict()


@router.get("/api/split/{token}")
def api_split_status(token: str, request: Request):
    require_owner(request, "move folders to another library")
    job = _jobs(request).get(token)
    if not job:
        raise HTTPException(404, "That move has expired from memory; its libraries show how it ended.")
    return job.as_dict(request.app.state.bg)


@router.post("/api/split/{oid}/back")
def api_split_back(oid: str, request: Request):
    require_owner(request, "move folders to another library")
    try:
        op = split.load(oid)
    except split.Refused as e:
        raise HTTPException(404, str(e))
    if op.get("moved_back") or op.get("state") != "done":
        raise HTTPException(400, "Those folders were moved back already." if op.get("moved_back")
                            else "That move didn't finish, so there's nothing to move back.")
    job = SplitJob(op["dst"], op["src"])
    _jobs(request)[job.token] = job
    _start(request, job, lambda on_wait, say: split.move_back(oid, lock_wait=1800, on_wait=on_wait, progress=say))
    return job.as_dict()


@router.get("/api/p/{p}/split")
def api_split_info(p: str, request: Request, conn=Depends(conn_dep)):
    """The folders this library leaves out (each with the library it went to, if that's still there),
    and the moves of folders to and from it, newest first."""
    require_owner(request, "see its folders")
    libs = set(split.config.list_projects())
    left_out = [{**r, "there": bool(r["library"] and r["library"] in libs)} for r in split._excluded_rows(conn)]
    return {"excluded": left_out, "moves": split.history(p)[:50]}


@router.post("/api/p/{p}/split/include")
def api_split_include(p: str, request: Request, body: dict = Body(default_factory=dict)):
    require_owner(request, "change a library's folders")
    try:
        with locks.project_lock(p, wait=30):
            out = split.include_again(p, str(body.get("folder") or ""))
    except split.Refused as e:
        raise HTTPException(400, str(e))
    except locks.Busy as e:
        raise HTTPException(409, str(e))
    request.app.state.jobs.start(p)            # its photos are read at this update
    if getattr(request.app.state.jobs, "watcher", None):
        request.app.state.jobs.watcher.request_refresh()
    return out


@router.post("/api/p/{p}/split/finish")
def api_split_finish(p: str, request: Request, body: dict = Body(default_factory=dict)):
    """Finish a move that stopped half way (its id), from its log."""
    require_owner(request, "move folders to another library")
    try:
        op = split.finish(str(body.get("id") or ""))
    except (split.Refused, locks.Busy) as e:
        raise HTTPException(400, str(e))
    _after(request, op)
    return op


