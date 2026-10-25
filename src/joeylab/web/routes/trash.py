"""Moving rejected photos and empty folders to the Trash from the app: the same checks as
`joeylab trash-list` / `empty-folders --trash`, for the Mac's owner only.

1. POST …/trash/plan starts a read-only check (in the background: it reads every file and its copy).
2. GET …/trash/{token} reports progress, then what stays behind for each file and how many would go.
3. POST …/trash/{token}/run with the count typed back moves them, each checked again just before.
Nothing moves without step 3, and only the files the plan found. Only the owner's own rejects go:
one rejected by an assistant (over MCP) or by someone else on the tailnet is listed and left alone.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import Counter

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import cleanup, copies, db, dupes as dupes_mod, library
from ..deps import conn_dep, filter_from, ids_of, require_owner, viewer_of
from .library import browse_filter

router = APIRouter()
EXPIRE_S = 3600
NOT_YOURS = {
    "assistant": "rejected by an assistant (open it in the Library and reject it yourself to include it)",
    "someone": "rejected by someone else (open it in the Library and reject it yourself to include it)",
}


def rejected_by_others(conn, ids: list[int], owner: str | None) -> dict[int, str]:
    """Of these rejected photos, the ones whose reject wasn't the owner's, with why: the latest step
    that rejected each came from an assistant over MCP, or from another tailnet user. (Undo and Redo
    only replay a step, so it's the step they replay that counts.) A reject with no history, from
    before JoeyLab kept one, is the owner's."""
    mine = {"", "owner"} | ({owner.lower()} if owner else set())
    out: dict[int, str] = {}
    for k in range(0, len(ids), 500):
        chunk = ids[k : k + 500]
        q = ",".join("?" * len(chunk))
        for sid, source, viewer in conn.execute(
                "SELECT shot_id, source, viewer FROM decision_log WHERE id IN (SELECT MAX(id) FROM decision_log"
                f" WHERE shot_id IN ({q}) AND source NOT IN ('undo', 'redo') AND json_extract(after, '$.pick') = -1"
                " AND COALESCE(json_extract(before, '$.pick'), 0) != -1 GROUP BY shot_id)", chunk):
            if source == "mcp":
                out[sid] = NOT_YOURS["assistant"]
            elif (viewer or "").lower() not in mine:
                out[sid] = NOT_YOURS["someone"]
    return out


class TrashJob:
    def __init__(self, project: str, what: str):
        self.token = uuid.uuid4().hex
        self.project, self.what = project, what
        self.state = "checking"              # checking | ready | moving | done | error
        self.done = self.total = 0
        self.items: list[cleanup.Item] = []
        self.folders: list[cleanup.EmptyFolder] = []
        self.roots: list[str] = []
        self.created = time.time()
        self.error: str | None = None
        self.result: dict | None = None
        self.scope: dict | None = None               # for duplicate extras: how many sets and photos
        self.in_folder: dict[str, int] = {}          # the library's files in each folder the plan touches
        self.notes: dict[str, dict] = {}             # each file's checked backup on another drive (copies.py)

    def note_backups(self, conn, paths: list[str]) -> None:
        """Look up each file's backups for the dialog: information only, never a reason to move a file."""
        try:
            self.notes = copies.notes_for_paths(conn, self.project, paths)
        except Exception:                            # the index can't be read: the dialog just doesn't say
            self.notes = {}

    def backups(self, items: list, last: set[str]) -> dict:
        """The dialog's backup lines: a summary, and per file a checked backup on another drive, or
        that it's the only copy anywhere, or that a possible copy hasn't been checked yet."""
        if not self.notes:
            return {"summary": None, "files": []}
        files = []
        for i in items[:300]:
            n = self.notes.get(i.path)
            if n is None:
                continue
            files.append({"path": i.path, "backup": n["backup"], "unchecked_on": n["unchecked_on"],
                          "only": i.path in last and not n["copies"]})
        return {"summary": copies.trash_summary({k: v for k, v in self.notes.items() if k in {i.path for i in items}}, last),
                "files": files}

    def by_folder(self) -> list[dict]:
        """For each folder, most first: the files that go if the copies go (copies), the ones that
        would go too with "also move the last copies" (last), and the library's files in it (total)."""
        rows: dict[str, dict] = {}
        for i in self.items:
            r = rows.setdefault(os.path.dirname(i.path), {"copies": 0, "last": 0, "skip": 0})
            r["copies" if i.category in cleanup.HAS_COPY else "last" if i.category in (cleanup.TWIN, cleanup.LAST) else "skip"] += 1
        return sorted(({"path": d, **r, "total": max(self.in_folder.get(d, 0), r["copies"] + r["last"])} for d, r in rows.items()),
                      key=lambda r: (-(r["copies"] + r["last"]), r["path"]))

    def as_dict(self) -> dict:
        d = {"token": self.token, "what": self.what, "state": self.state, "done": self.done, "total": self.total,
             "error": self.error, "result": self.result, "scope": self.scope}
        if self.what == "folders" and self.state != "checking":
            d["folders"] = [{"path": f.path, "leftovers": len(f.clutter), "inside": f.folders} for f in self.folders]
            d["go"] = {"copies": len(self.folders), "all": len(self.folders)}
        elif self.state != "checking":
            sm = cleanup.summary(self.items)
            d["summary"] = {c: {"n": n, "bytes": b, "label": cleanup.DESCRIBE[c]} for c, (n, b) in sm.items() if n}
            go_copies, go_all = cleanup.chosen(self.items, False), cleanup.chosen(self.items, True)
            d["go"] = {"copies": len(go_copies), "copies_bytes": sum(i.size for i in go_copies),
                       "all": len(go_all), "all_bytes": sum(i.size for i in go_all)}
            last = [i for i in self.items if i.category in (cleanup.TWIN, cleanup.LAST)]
            d["last_examples"] = [i.path for i in last[:8]]
            d["by_folder"] = self.by_folder()[:200]
            d["skipped"] = [{"why": w, "n": n} for w, n in Counter(i.why for i in self.items if i.category == cleanup.SKIP).most_common()]
            d["backups"] = self.backups([i for i in self.items if i.category != cleanup.SKIP], {i.path for i in last})
        return d


def reindex(conn, p: str) -> None:
    """Files just moved (to the Trash, or to another folder) leave or follow in the index of backups
    at once, so another library stops counting a file in the Trash as its backup."""
    try:
        copies.refresh(conn, p)
        copies.sync(conn, p)
    except Exception:
        pass                                         # the next update does it


def _jobs(request: Request) -> dict:
    jobs = request.app.state.trash_jobs
    for t in [t for t, j in jobs.items() if time.time() - j.created > EXPIRE_S and j.state != "moving"]:
        jobs.pop(t, None)
    return jobs


def _job(request: Request, p: str, token: str) -> TrashJob:
    j = _jobs(request).get(token)
    if not j or j.project != p:
        raise HTTPException(404, "That check has expired. Start again.")
    return j


@router.post("/api/p/{p}/trash/plan")
def api_trash_plan(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Check what would go: {what: "rejects", filter: {...}} (the rejected photos matching a Library
    filter, every file of each), {what: "rejects", ids: [...]} (just these photos, if rejected),
    {what: "rejects", set_ids: [...]} (the rejected extra copies of these duplicate sets: only their
    extra files, counted as rejecting them counted) or {what: "folders"} (empty folders). Read-only."""
    require_owner(request)
    what = body.get("what")
    if what not in ("rejects", "folders"):
        raise HTTPException(400, "Say what to move: rejects or folders.")
    job = TrashJob(p, what)
    if what == "rejects":
        set_ids, ids = ids_of(body.get("set_ids")), ids_of(body.get("ids"))
        if set_ids:
            ex = dupes_mod.extra_copies(conn, set_ids)["files"]
            mine = dupes_mod.rejected_of(conn, list(ex))           # only ever photos you rejected
            shots = sorted(mine)
            files_of = {sid: [path for path, _ in ex[sid]] for sid in shots}
            job.scope = {"set_ids": len(set(set_ids)), "photos": len(shots), "not_rejected": len(ex) - len(shots)}
        else:
            if ids:
                flt = library.Filter(ids=ids)
            else:
                q = body.get("filter") or {}
                flt = filter_from(q, viewer_of(request))
                if str(q.get("all")) not in ("1", "true"):
                    flt = browse_filter(conn, flt)
            flt.picks, flt.undecided, flt.rejects = False, False, True      # only ever photos you rejected
            shots = [s["id"] for s in library.shots(conn, flt)]
            files_of = {sid: [f["path"] for f in library.files_of(conn, sid)] for sid in shots}
        others = rejected_by_others(conn, shots, request.app.state.owner)
        paths = [path for sid in shots if sid not in others for path in files_of[sid]]
        held = [cleanup.Item(path, why=others[sid], shot_id=sid) for sid in shots if sid in others for path in files_of[sid]]
        job.total = len(paths)
    else:
        job.roots = cleanup.folder_roots(conn)[0]      # never a different drive under the library drive's name

    def work():
        c = db.connect(p, create=False)
        try:
            if what == "rejects":
                items = cleanup.plan(c, paths, progress=lambda n, total: setattr(job, "done", n)) + held
                job.in_folder = {d: dupes_mod.files_in_folder(c, d) for d in {os.path.dirname(i.path) for i in items}}
                job.note_backups(c, [i.path for i in items if i.category != cleanup.SKIP])
                job.items = items
            else:
                job.folders = cleanup.empty_folders(job.roots)
                job.total = job.done = len(job.folders)
            job.state = "ready"
        except Exception as e:                       # shown in the dialog
            job.state, job.error = "error", str(e)
        finally:
            c.close()

    _jobs(request)[job.token] = job
    threading.Thread(target=work, name=f"trash-plan:{p}", daemon=True).start()
    return job.as_dict()


@router.get("/api/p/{p}/trash/{token}")
def api_trash_status(p: str, token: str, request: Request):
    require_owner(request)
    return _job(request, p, token).as_dict()


@router.post("/api/p/{p}/trash/{token}/run")
def api_trash_run(p: str, token: str, request: Request, body: dict = Body(default_factory=dict)):
    """Move what the check found, once the count is typed back. include_last also moves files that
    are the last copy."""
    require_owner(request)
    job = _job(request, p, token)
    if job.what not in ("rejects", "folders"):            # RAWs are moved by their own step (routes/raws.py)
        raise HTTPException(404, "That check has expired. Start again.")
    if job.state != "ready":
        raise HTTPException(409, "The check isn't finished, or these were moved already.")
    if any(j.project == p and j.state == "moving" for j in _jobs(request).values()):
        raise HTTPException(409, "Something is already being moved to the Trash; wait for it to finish.")
    include_last = bool(body.get("include_last"))
    if job.what == "folders":
        n = len(job.folders)
    else:
        go = cleanup.chosen(job.items, include_last)
        n = len(go)
    try:
        typed = int(str(body.get("count", "")).replace(",", "").strip())
    except ValueError:
        typed = -1
    if not n or typed != n:
        raise HTTPException(400, f"Type {n} to confirm." if n else "There's nothing to move.")
    job.state, job.done, job.total = "moving", 0, n
    bg = request.app.state.bg

    def work():
        c = db.connect(p, create=False)

        def still_rejected(it) -> str | None:
            # Read again just before the move: un-rejected since the check (here, on a phone, by an
            # assistant) means it stays.
            r = c.execute("SELECT pick FROM decisions WHERE shot_id=?", (it.shot_id,)).fetchone()
            return None if r and r[0] == -1 else "no longer rejected"

        try:
            if job.what == "folders":
                log = cleanup.trash_folders(job.folders, job.roots, f"{p}-folders")
                moved = [f for f in job.folders if f.result == "moved to the Trash"]
                job.result = {"moved": len(moved), "log": log,
                              "left": [{"path": f.path, "why": f.result} for f in job.folders if f.result != "moved to the Trash"][:20]}
            else:
                log = cleanup.trash_files(job.items, go, f"{p}-files", still_wanted=still_rejected, conn=c)
                moved = [i for i in go if i.result == "moved to the Trash"]
                # The catalog already shows them gone (gone: the photos now missing), so no scan is
                # needed; stacks, suggestions and events are made again without them.
                job.result = {"moved": len(moved), "bytes": sum(i.size for i in moved), "log": log,
                              "emptied": len(cleanup.emptied_by(go)), "gone": sorted({i.shot_id for i in go if i.photo_gone}),
                              "left": [{"path": i.path, "why": i.result} for i in go if i.result != "moved to the Trash"][:20]}
                if moved:
                    bg.reorganise(p)
                    reindex(c, p)
            job.state = "done"
        except Exception as e:
            job.state, job.error = "error", str(e)
        finally:
            c.close()

    threading.Thread(target=work, name=f"trash-run:{p}", daemon=True).start()
    return job.as_dict()
