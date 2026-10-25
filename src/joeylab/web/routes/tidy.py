"""Tidying, all read-only on your folders: duplicates, selections, library health, backup
coverage, camera clocks, the "untouched" check, catalog backups, and plans you run yourself."""

from __future__ import annotations

import csv
import io
import os

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import Response

from ... import backup, clocksync, copies, dupes as dupes_mod, health, library, selections
from ..deps import cap, conn_dep, ids_of, require_owner, require_writer, viewer_of

router = APIRouter()


def _kinds(v: str | None) -> tuple:
    allowed = ("exact", "copy", "export", "pair")
    return tuple(k for k in (v or "exact,copy").split(",") if k in allowed)


@router.get("/api/p/{p}/dupes/groups")
def api_dupe_groups(p: str, kinds: str | None = None, conn=Depends(conn_dep)):
    roots = [r[0] for r in conn.execute("SELECT path FROM roots")]
    counts = dict(conn.execute("SELECT kind, COUNT(*) FROM dup_sets GROUP BY kind").fetchall())
    # Copies on another drive are backups, not duplicates: how many photos have one, for the page's note.
    backups = copies.counts(conn) if copies.ensure(conn, p) else None
    return {**dupes_mod.folder_groups(conn, _kinds(kinds)), "roots": roots, "counts": counts,
            "removable": list(dupes_mod.REMOVABLE), "backups": backups}


# ------------------------------------------------------------ backups (copies on other drives)
@router.get("/api/p/{p}/copies")
def api_copies(p: str, request: Request, conn=Depends(conn_dep)):
    """Tidy › Backups: per drive and folder, the photos with a byte-identical copy on another drive
    (and where), the ones only on this drive, the space each takes, and the drives holding them."""
    out = copies.report(conn, p)
    jobs = request.app.state.jobs
    out["checking"] = getattr(jobs, "copies_state", dict)()
    return out


@router.post("/api/p/{p}/copies/check")
def api_copies_check(p: str, request: Request, conn=Depends(conn_dep)):
    """Refresh this library's files in the index now, and start checking the probable backups byte
    for byte in the background (read-only, paced)."""
    require_writer(request)
    copies.refresh(conn, p)
    copies.sync(conn, p)
    kick = getattr(request.app.state.jobs, "copies_kick", None)
    if kick:
        kick()
    return {"candidates": copies.candidates()}


@router.post("/api/p/{p}/dupes/keep")
def api_dupe_keep(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    n = dupes_mod.prefer_folder(conn, body.get("folder", ""), ids_of(body.get("set_ids")) or [])
    request.app.state.bg.reorganise(p)    # "duplicate" flags, stacks and events follow the kept copies
    return {"changed": n}


@router.post("/api/p/{p}/dupes/rules")
def api_dupe_rules(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """"Always keep copies under this folder" (and everything in it), for every set: {folder, on=true}.
    Off removes the rule. Every set's kept copy is chosen again at once."""
    require_writer(request)
    folder = (body.get("folder") or "").strip()
    roots = [r[0] for r in conn.execute("SELECT path FROM roots")]
    if not folder.startswith("/") or not any(dupes_mod._under(folder, r) or dupes_mod._under(r, folder) for r in roots):
        raise HTTPException(400, "Choose a folder of this library.")
    n = dupes_mod.set_keep_rule(conn, folder, on=body.get("on", True) is not False)
    if n:
        request.app.state.bg.reorganise(p)
    return {"rules": dupes_mod.keep_rules(conn), "changed": n}


@router.post("/api/p/{p}/dupes/combine")
def api_dupe_combine(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Give each kept copy the best stars/pick any of its copies has (one undoable step)."""
    require_writer(request)
    batch, changed = dupes_mod.combine_decisions(conn, ids_of(body.get("set_ids")) or [], viewer=viewer_of(request))
    return {"batch": batch, "changed": len(changed), "rows": library.rows_by_ids(conn, changed)}


@router.post("/api/p/{p}/dupes/reject-extras")
def api_dupe_reject_extras(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Reject every extra copy in these sets (a whole folder group, or every group shown), after giving
    the kept copies the best stars and picks. {set_ids, combine=true, dry=false}. One undoable step."""
    require_writer(request)
    set_ids = ids_of(body.get("set_ids")) or []
    if not set_ids:
        raise HTTPException(400, "Say which duplicate sets.")
    r = dupes_mod.reject_extras(conn, set_ids, combine=body.get("combine", True) is not False,
                                viewer=viewer_of(request), dry=bool(body.get("dry")))
    if r["rejected"]:
        request.app.state.bg.decided(p, r["rejected"])
    return r


@router.get("/api/p/{p}/dupes/extras")
def api_dupe_extras(p: str, ids: str = "", conn=Depends(conn_dep)):
    """Paths of the extra (non-kept) files in these sets, as a text file. Only identical files and
    re-saved copies are ever listed: edited versions and RAW+JPEG halves never are."""
    paths = dupes_mod.extras(conn, ids_of(ids) or [])
    return Response("\n".join(paths) + ("\n" if paths else ""), media_type="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="{p}-duplicate-extras.txt"'})


@router.post("/api/p/{p}/dupes/verify")
def api_dupe_verify(p: str, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Check a list of extras right before deleting them: each must still have its kept copy."""
    paths = body.get("paths")
    if isinstance(paths, str):
        paths = paths.splitlines()
    res = health.verify_extras(conn, paths or [])
    return {"results": res, "ok": sum(r["ok"] for r in res), "problems": [r for r in res if not r["ok"]]}


@router.get("/api/p/{p}/dupes")
def api_dupes(p: str, ids: str | None = None, kinds: str | None = None, limit: int = 60, offset: int = 0,
              conn=Depends(conn_dep)):
    sets = []
    if ids:
        q = f"SELECT id, kind FROM dup_sets WHERE id IN ({','.join(str(int(i)) for i in ids_of(ids))}) ORDER BY id"
        args: tuple = ()
    else:
        ks = _kinds(kinds)
        q = f"SELECT id, kind FROM dup_sets WHERE kind IN ({','.join('?' * len(ks)) or 'NULL'}) ORDER BY kind, id LIMIT ? OFFSET ?"
        args = (*ks, cap(limit, 500), max(0, int(offset)))
    for ds in conn.execute(q, args).fetchall():
        members = [dict(r) for r in conn.execute(
            "SELECT dm.shot_id, dm.file_id, dm.keep, f.path, f.size FROM dup_members dm"
            " JOIN files f ON f.id=dm.file_id WHERE dm.set_id=? ORDER BY dm.keep DESC, f.path", (ds["id"],))]
        rows = {r["id"]: r for r in library.rows_by_ids(conn, [m["shot_id"] for m in members])}
        for m in members:
            m["row"] = rows.get(m["shot_id"])
        sets.append({"id": ds["id"], "kind": ds["kind"], "removable": ds["kind"] in dupes_mod.REMOVABLE, "members": members})
    return {"sets": sets}


@router.get("/api/p/{p}/selections")
def api_selections(p: str, conn=Depends(conn_dep)):
    roots = [r[0] for r in conn.execute("SELECT path FROM roots")]
    rows = [dict(r) | {"album_items": conn.execute("SELECT COUNT(*) FROM album_items WHERE album_id=?",
                                                   (r["album_id"],)).fetchone()[0] if r["album_id"] else 0}
            for r in conn.execute("SELECT * FROM selections ORDER BY identical_bytes DESC")]
    return {"selections": [r for r in rows if r["kind"] == "selection"],
            "mirrors": [r for r in rows if r["kind"] == "mirror"], "roots": roots}


@router.get("/api/p/{p}/selections/{sid}/plan.csv")
def api_selection_plan(p: str, sid: int, conn=Depends(conn_dep)):
    try:
        text, name = selections.link_plan_csv(conn, sid)
    except KeyError:
        raise HTTPException(404, "That selection isn't found on your drives any more. Update the library to refresh the list.")
    safe = "".join(ch if ch.isalnum() or ch in " -_." else "_" for ch in name)
    return Response(text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="link plan - {safe}.csv"'})


@router.post("/api/p/{p}/albums/{aid}/linkfolder")
def api_link_folder(p: str, aid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_owner(request, "make link folders on it")
    try:
        return selections.make_link_folder(conn, aid, body.get("dest") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------ health
@router.get("/api/p/{p}/health")
def api_health(p: str, deep: int = 0, conn=Depends(conn_dep)):
    return health.report(conn, p, deep=bool(deep))


@router.get("/api/p/{p}/health/empty-folders")
def api_empty_folders(p: str, conn=Depends(conn_dep)):
    """Folders in this library's folders holding nothing but .DS_Store-type leftovers. Read-only:
    moving them to the Trash is `joeylab empty-folders --trash`, which asks first."""
    from ... import cleanup

    roots, held = cleanup.folder_roots(conn)        # never a different drive under the library drive's name
    found = cleanup.empty_folders(roots)
    return {"folders": [{"path": f.path, "root": next((r for r in roots if f.path.startswith(r.rstrip("/") + "/")), None),
                         "leftovers": len(f.clutter), "inside": f.folders} for f in found],
            "unavailable": [r for r in roots if not os.path.isdir(r)] + held}


@router.get("/api/p/{p}/health/coverage")
def api_coverage(p: str, conn=Depends(conn_dep)):
    return health.backup_coverage(conn, p)


@router.get("/api/p/{p}/health/sidecars")
def api_orphans(p: str, conn=Depends(conn_dep)):
    return {"orphans": health.orphan_sidecars(conn)}


@router.get("/api/p/{p}/health/reorganise.csv")
def api_reorganise(p: str, conn=Depends(conn_dep)):
    """A proposed tidy layout, as a CSV for you to review (and apply yourself if you like).
    JoeyLab never moves a file."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["current_path", "suggested_folder", "file_name"])
    for r in health.reorganise_plan(conn):
        w.writerow([r["from"], r["to_folder"], r["name"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{p}-reorganise-plan.csv"'})


def _known(p: str) -> None:
    from ... import config

    if p not in config.list_projects():
        raise HTTPException(404, f"There's no library called {p!r}.")


@router.get("/api/p/{p}/backups")
def api_backups(p: str):
    _known(p)
    return {"backups": backup.list_backups(p)}


@router.post("/api/p/{p}/backups")
def api_backup_now(p: str, request: Request):
    require_writer(request)
    _known(p)
    path = backup.backup(p, force=True)
    return {"file": str(path) if path else None}


# ------------------------------------------------------------ camera clocks
@router.get("/api/p/{p}/clock")
def api_clock(p: str, suggest: int = 1, conn=Depends(conn_dep)):
    return {"cameras": clocksync.cameras(conn), "suggestions": clocksync.suggest(conn, p) if suggest else []}


@router.post("/api/p/{p}/clock")
def api_set_clock(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Correct one camera's clock on JoeyLab's timeline (EXIF is never rewritten)."""
    require_writer(request)
    key = body.get("camera") or ""
    try:
        off = float(body.get("offset_s", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "The offset is a number of seconds.")
    if abs(off) > 2 * 365 * 86400:
        raise HTTPException(400, "That's more than two years; check the number.")
    n = clocksync.set_offset(conn, key, off)
    request.app.state.bg.reorganise(p)   # regroup events on the corrected timeline (queued if a regroup is running)
    return {"updated": n}
