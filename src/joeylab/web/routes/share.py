"""Albums (in the catalog, smart or hand-picked), exports and imports, read-only share links,
other viewers' favourites and comments."""

from __future__ import annotations

import csv
import io
import json
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from ... import config, db, export, library, locks, portable
from ...library import Filter
from ..deps import conn_dep, filter_for, ids_of, is_owner, require_owner, require_writer, viewer_of

router = APIRouter()
XMP_LOCK_WAIT_S = 60          # the request waits only so long for an update of the library to finish

# The photos an event or trip link was made with. Events are found again on every update and may be
# regrouped; a link keeps showing what it showed when it was made (older links follow their event).
SHARE_PHOTOS = """CREATE TABLE IF NOT EXISTS share_photos (token TEXT NOT NULL, shot_id INTEGER NOT NULL,
                                                     PRIMARY KEY (token, shot_id))"""


# ------------------------------------------------------------ albums
@router.get("/api/p/{p}/albums")
def api_albums(p: str, light: int = 0, conn=Depends(conn_dep)):
    """Every album with its count and cover. light: just the hand-picked ones, most recently changed
    first, with their counts (for "Add to album")."""
    if light:
        return {"albums": [dict(r) for r in conn.execute(
            "SELECT a.id, a.name, a.kind, a.updated, (SELECT COUNT(*) FROM album_items i WHERE i.album_id = a.id) AS items"
            " FROM albums a WHERE a.kind = 'manual' ORDER BY a.updated DESC")]}
    out = []
    for a in conn.execute(
        "SELECT a.*, (SELECT COUNT(*) FROM album_items i WHERE i.album_id = a.id) AS items,"
        " (SELECT COUNT(*) FROM album_items i JOIN files f ON f.id = i.file_id WHERE i.album_id = a.id AND f.status='present') AS present,"
        " (SELECT i.shot_id FROM album_items i JOIN metrics m ON m.shot_id = i.shot_id WHERE i.album_id = a.id"
        "  ORDER BY m.quality DESC LIMIT 1) AS cover"
        " FROM albums a ORDER BY a.kind = 'selection', a.updated DESC"):
        d = dict(a)
        if d["kind"] == "smart" and d["query"]:
            try:
                flt = filter_for(conn, json.loads(d["query"]))
                w, args = flt.sql()
                d["items"] = d["present"] = conn.execute(f"SELECT COUNT(*) {library.LIGHT_FROM}{w}", args).fetchone()[0]
                top, _ = library.page(conn, flt, sort="quality", limit=1, with_total=False)
                d["cover"] = top[0]["id"] if top else None
            except (ValueError, HTTPException):
                pass
        out.append(d)
    return {"albums": out, "rows": library.rows_by_ids(conn, [a["cover"] for a in out if a.get("cover")])}


@router.post("/api/p/{p}/albums")
def api_create_album(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """A JoeyLab album: hand-picked (ids, or everything matching a filter now) or smart (a saved
    filter that stays up to date). Nothing is written outside the catalog."""
    require_writer(request)
    name = (body.get("name") or "").strip()
    if not name or len(name) > 120:
        raise HTTPException(400, "Give the album a name.")
    smart = body.get("smart")
    now = time.time()
    if smart:
        aid = db.new_id(conn, "albums")
        conn.execute("INSERT INTO albums(id, name, kind, query, description, created, updated) VALUES (?,?,?,?,?,?,?)",
                     (aid, name, "smart", json.dumps(smart), body.get("description"), now, now))
        conn.commit()
        return {"id": aid, "added": 0}
    aid = db.new_id(conn, "albums")              # a share link to a deleted album must not open a new one
    conn.execute("INSERT INTO albums(id, name, kind, description, created, updated) VALUES (?,?,?,?,?,?)",
                 (aid, name, "manual", body.get("description"), now, now))
    conn.commit()
    ids = ids_of(body.get("ids"))
    if ids is None and body.get("filter") is not None:
        from .library import browse_filter

        # What the Library shows for this filter (and what "Save all N" counted): without the photos browsing
        # hides (thumbnail-sized ones, extra copies, screenshots) unless the filter asks for everything.
        flt = filter_for(conn, body["filter"], viewer_of(request))
        if str(body["filter"].get("all")) not in ("1", "true"):
            flt = browse_filter(conn, flt)
        ids = [r["id"] for r in library.shots(conn, flt)]
    added = portable.add_to_album(conn, aid, ids or [])
    return {"id": aid, "added": added}


@router.post("/api/p/{p}/albums/{aid}/items")
def api_album_items(p: str, aid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Add photos to a hand-picked album or take them out: {add: ids, remove: ids}, or everything a
    Library filter matches with {filter: {...}, op: "add" | "remove"}. Returns the counts and the
    ids that changed (so the change can be taken back)."""
    require_writer(request)
    from .library import browse_filter

    a = conn.execute("SELECT kind, name FROM albums WHERE id=?", (aid,)).fetchone()
    if not a:
        raise HTTPException(404, "No such album.")
    if a["kind"] == "smart":
        raise HTTPException(400, "A smart album follows its filter; edit the filter instead.")
    add, remove = ids_of(body.get("add")) or [], ids_of(body.get("remove")) or []
    if body.get("filter") is not None:
        if body.get("op") not in ("add", "remove"):
            raise HTTPException(400, "Say whether to add the matching photos or remove them (op).")
        q = body["filter"]
        flt = filter_for(conn, q, viewer_of(request))
        if str(q.get("all")) not in ("1", "true"):
            flt = browse_filter(conn, flt)
        ids = [r["id"] for r in library.shots(conn, flt)]
        add, remove = (add + ids, remove) if body["op"] == "add" else (add, remove + ids)
    added_ids: list[int] = []
    removed_ids: list[int] = []
    added = portable.add_to_album(conn, aid, add, added_ids)
    removed = portable.remove_from_album(conn, aid, remove, removed_ids)
    return {"added": added, "removed": removed, "added_ids": added_ids, "removed_ids": removed_ids, "album": a["name"]}


@router.post("/api/p/{p}/albums/{aid}")
def api_album_edit(p: str, aid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    if body.get("name"):
        conn.execute("UPDATE albums SET name=?, updated=? WHERE id=?", (body["name"].strip()[:120], time.time(), aid))
    if "description" in body:
        conn.execute("UPDATE albums SET description=? WHERE id=?", (body["description"], aid))
    if body.get("smart") is not None:
        conn.execute("UPDATE albums SET query=? WHERE id=? AND kind='smart'", (json.dumps(body["smart"]), aid))
    conn.commit()
    return {"ok": True}


@router.delete("/api/p/{p}/albums/{aid}")
def api_album_delete(p: str, aid: int, request: Request, conn=Depends(conn_dep)):
    """Remove an album from JoeyLab (its photos stay where they are; selection albums come back
    while their folder exists)."""
    require_writer(request)
    conn.execute("DELETE FROM album_items WHERE album_id=?", (aid,))
    conn.execute("DELETE FROM albums WHERE id=?", (aid,))
    conn.execute("DELETE FROM shares WHERE kind='album' AND ref=?", (str(aid),))     # its links stop working
    conn.commit()
    return {"ok": True}


@router.get("/api/p/{p}/albums/{aid}/filter")
def api_album_filter(p: str, aid: int, conn=Depends(conn_dep)):
    a = conn.execute("SELECT kind, query FROM albums WHERE id=?", (aid,)).fetchone()
    if not a:
        raise HTTPException(404, "No such album.")
    return json.loads(a["query"]) if a["kind"] == "smart" and a["query"] else {"album": aid}


# ------------------------------------------------------------ exports (never into tracked folders)
def _export_filter(conn, body, request: Request) -> Filter:
    flt = filter_for(conn, body.get("filter") or {}, viewer_of(request))
    if body.get("ids"):
        flt.ids = ids_of(body["ids"])
    return flt


@router.post("/api/p/{p}/export/album")
def api_album_folder(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_owner(request, "make album folders on it")
    dest = Path(body.get("dest") or "").expanduser()
    if not str(dest).strip() or not dest.is_absolute():
        raise HTTPException(400, "Give the album a full folder path, for example ~/Desktop/Wedding-Best.")
    which = body.get("files", "preferred")
    if which not in export.FILE_CHOICES:
        raise HTTPException(400, f"files must be one of {', '.join(export.FILE_CHOICES)}")
    try:
        r = export.album(conn, _export_filter(conn, body, request), dest, which=which, copy=bool(body.get("copy")),
                         clean=bool(body.get("clean")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"created": r.created, "skipped": r.skipped, "removed": r.removed, "dest": r.dest}


@router.get("/api/p/{p}/export/list")
def api_list(p: str, request: Request, conn=Depends(conn_dep)):
    q = dict(request.query_params)
    paths = export.paths(conn, _export_filter(conn, {"filter": q, "ids": q.get("ids")}, request), which=q.get("files", "all"))
    return Response("\n".join(paths) + ("\n" if paths else ""), media_type="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="{p}-paths.txt"'})


@router.get("/api/p/{p}/export/csv")
def api_csv(p: str, request: Request, conn=Depends(conn_dep)):
    q = dict(request.query_params)
    rows = library.shots(conn, _export_filter(conn, {"filter": q, "ids": q.get("ids")}, request))
    buf = io.StringIO()
    csv.writer(buf).writerows(export.csv_rows(conn, rows))
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{p}.csv"'})


@router.post("/api/p/{p}/export/xmp")
def api_xmp(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_owner(request, "write sidecar files next to its photos")
    if body.get("confirm") is not True:
        raise HTTPException(400, "Confirm that .xmp files may be written next to your photos.")
    style = body.get("style", "stem")
    if style not in ("stem", "full"):
        raise HTTPException(400, "style must be stem or full")
    include = tuple(body.get("include") or ("rating", "label"))
    try:
        # The library's update lock: a scan reads ratings from sidecars, so never one half written.
        with locks.project_lock(p, wait=XMP_LOCK_WAIT_S):
            r = export.write_sidecars(conn, _export_filter(conn, body, request), style=style,
                                      update_existing=bool(body.get("update_existing")), include=include, project=p)
    except locks.Busy:
        raise HTTPException(409, "An update of this library is running, so no sidecars were written. "
                                 "Try again when it finishes.")
    return {"written": r.written, "updated": r.updated, "skipped_existing": r.skipped_existing,
            "kept_theirs": r.kept_theirs, "errors": r.errors[:20], "error_count": len(r.errors), "backup_dir": r.backup_dir}


@router.post("/api/p/{p}/export/xmp/restore")
def api_xmp_restore(p: str, request: Request, body: dict = Body(default_factory=dict)):
    """Put back the sidecars an XMP update changed, from its backup in ~/.joeylab."""
    require_owner(request, "write sidecar files next to its photos")
    d = Path(body.get("backup_dir") or "")
    base = config.HOME / "xmp-backups"
    if not d.is_dir() or base not in d.resolve().parents:
        raise HTTPException(400, "That isn't one of JoeyLab's sidecar backups.")
    return {"restored": export.restore_sidecars(d)}


@router.get("/api/p/{p}/export/decisions.json")
def api_export_decisions(p: str, conn=Depends(conn_dep)):
    data = portable.export_decisions(conn, p)
    return Response(json.dumps(data, indent=1), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{p}-decisions-{time.strftime("%Y%m%d")}.json"'})


@router.post("/api/p/{p}/import/decisions")
def api_import_decisions(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    try:
        return portable.import_decisions(conn, body.get("data") or {}, overwrite=bool(body.get("overwrite")),
                                         viewer=viewer_of(request))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/p/{p}/import/lightroom")
def api_import_lightroom(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_owner(request, "import from a file on it")
    try:
        return portable.import_lightroom(conn, body.get("path") or "", overwrite=bool(body.get("overwrite")),
                                         viewer=viewer_of(request))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/p/{p}/export/gallery")
def api_gallery(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_owner(request, "make gallery folders on it")
    images = request.app.state.images

    def render(row):
        r = images["row"](conn, row["id"])
        return images["render"](p, r, "", 2048)

    try:
        return portable.export_gallery(conn, _export_filter(conn, body, request), Path(body.get("dest") or "").expanduser(),
                                       body.get("title") or p, render)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/p/{p}/import/favourites")
def api_import_favs(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    return portable.import_favourites(conn, body.get("text") or "", body.get("who") or "client")


# ------------------------------------------------------------ other viewers: favourites and comments
@router.post("/api/p/{p}/fav")
def api_fav(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Your own favourites, separate from the owner's picks (anyone on the tailnet may)."""
    who = viewer_of(request) or "owner"
    fav = 1 if body.get("fav", True) else 0
    for sid in ids_of(body.get("ids")) or []:
        conn.execute("INSERT INTO viewer_decisions(viewer, shot_id, fav, updated) VALUES (?,?,?,?)"
                     " ON CONFLICT(viewer, shot_id) DO UPDATE SET fav=excluded.fav, updated=excluded.updated",
                     (who, sid, fav, time.time()))
    conn.commit()
    return {"viewer": who, "fav": fav}


@router.get("/api/p/{p}/viewers")
def api_viewers(p: str, request: Request, conn=Depends(conn_dep)):
    return {"me": viewer_of(request) or "owner", "viewers": [dict(r) for r in conn.execute(
        "SELECT viewer, COUNT(*) AS favs, MAX(updated) AS last FROM viewer_decisions WHERE fav=1 GROUP BY viewer ORDER BY favs DESC")]}


@router.post("/api/p/{p}/comments")
def api_comment(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    text = (body.get("text") or "").strip()
    if not text or len(text) > 2000:
        raise HTTPException(400, "Write a comment (up to 2,000 characters).")
    try:
        sid = int(body.get("shot_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Which photo?")
    who = viewer_of(request) or ("owner" if is_owner(request) else "guest")     # a tagged device isn't the owner
    cid = conn.execute("INSERT INTO comments(shot_id, viewer, text, ts) VALUES (?,?,?,?)",
                       (sid, who, text, time.time())).lastrowid
    conn.commit()
    return {"id": cid}


@router.delete("/api/p/{p}/comments/{cid}")
def api_delete_comment(p: str, cid: int, request: Request, conn=Depends(conn_dep)):
    """Your own comments; the owner may remove anyone's. A device with no identity (a tagged one, a
    direct connection) owns none."""
    who = viewer_of(request)
    row = conn.execute("SELECT viewer FROM comments WHERE id=?", (cid,)).fetchone()
    if row and not is_owner(request) and not (who and row[0] == who):
        raise HTTPException(403, "You can remove only your own comments.")
    conn.execute("DELETE FROM comments WHERE id=?", (cid,))
    conn.commit()
    return {"ok": True}


@router.get("/api/p/{p}/comments")
def api_comments(p: str, conn=Depends(conn_dep)):
    return {"comments": [dict(r) for r in conn.execute(
        "SELECT c.id, c.shot_id, c.viewer, c.text, c.ts FROM comments c ORDER BY c.ts DESC LIMIT 200")]}


# ------------------------------------------------------------ read-only share links
@router.get("/api/p/{p}/shares")
def api_shares(p: str, request: Request, conn=Depends(conn_dep)):
    require_writer(request)               # live links: anyone who sees them can open them
    return {"shares": [dict(r) for r in conn.execute("SELECT * FROM shares WHERE revoked=0 ORDER BY created DESC")]}


@router.post("/api/p/{p}/shares")
def api_make_share(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """A link that shows one album or event, and nothing else, to anyone on your tailnet."""
    require_writer(request)
    kind = body.get("kind")
    if kind not in ("album", "event"):
        raise HTTPException(400, "Share an album or an event.")
    try:
        ref = str(int(body.get("ref")))
    except (TypeError, ValueError):
        raise HTTPException(400, "Say which album or event to share.")
    token = secrets.token_urlsafe(18)
    days = body.get("days")
    label = (body.get("label") or "")[:120]
    if kind == "event":
        # The link keeps these photos, and the name, whatever later updates do to the event.
        e = conn.execute("SELECT name, auto_name FROM events WHERE id=?", (int(ref),)).fetchone()
        if not e:
            raise HTTPException(404, "That event isn't there any more (events are re-detected on each update).")
        ids = _live_ids(conn, kind, ref)
        if not ids:
            raise HTTPException(400, "There's nothing in it to share: rejects, thumbnails and duplicates never are.")
        label = label or (e["name"] or e["auto_name"] or "")[:120]
        conn.execute(SHARE_PHOTOS)
        conn.executemany("INSERT OR IGNORE INTO share_photos(token, shot_id) VALUES (?,?)", [(token, i) for i in ids])
    conn.execute("INSERT INTO shares(token, kind, ref, label, created, expires) VALUES (?,?,?,?,?,?)",
                 (token, kind, ref, label, time.time(),
                  time.time() + float(days) * 86400 if days else None))
    conn.commit()
    return {"token": token, "path": f"/s/{token}"}


@router.delete("/api/p/{p}/shares/{token}")
def api_revoke_share(p: str, token: str, request: Request, conn=Depends(conn_dep)):
    require_writer(request)
    conn.execute("UPDATE shares SET revoked=1 WHERE token=?", (token,))
    if _has_snapshots(conn):
        conn.execute("DELETE FROM share_photos WHERE token=?", (token,))
    conn.commit()
    return {"ok": True}


def _resolve(token: str):
    """(project, connection, share row) for a live share token, or 404."""
    for name in config.list_projects():
        try:
            c = db.connect(name, create=False)
        except Exception:
            continue
        r = c.execute("SELECT * FROM shares WHERE token=? AND revoked=0", (token,)).fetchone()
        if r and (not r["expires"] or r["expires"] > time.time()):
            return name, c, r
        c.close()
    raise HTTPException(404, "This link has expired or was turned off.")


def _has_snapshots(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='share_photos'").fetchone() is not None


def _share_ids(conn, share) -> list[int]:
    if share["kind"] == "event" and _has_snapshots(conn):
        kept = [r[0] for r in conn.execute("SELECT shot_id FROM share_photos WHERE token=?", (share["token"],))]
        if kept:
            # The photos it was made with, still here and not rejected since.
            return [r["id"] for r in library.shots(conn, Filter(ids=kept)) if (r.get("pick") or 0) != -1]
    return _live_ids(conn, share["kind"], share["ref"])


def _live_ids(conn, kind: str, ref: str) -> list[int]:
    if kind == "album":
        a = conn.execute("SELECT kind, query FROM albums WHERE id=?", (int(ref),)).fetchone()
        if not a:
            return []
        flt = filter_for(conn, json.loads(a["query"])) if a["kind"] == "smart" and a["query"] else Filter(album_id=int(ref))
    else:
        e = conn.execute("SELECT kind FROM events WHERE id=?", (int(ref),)).fetchone()
        if not e:
            return []
        # A shared event or trip shows what you'd show: not your rejects, thumbnails or duplicates.
        flt = Filter(trip_id=int(ref)) if e["kind"] == "trip" else Filter(event_id=int(ref))
        flt.no_flag = "tiny,duplicate"
        return [r["id"] for r in library.shots(conn, flt) if (r.get("pick") or 0) != -1]
    return [r["id"] for r in library.shots(conn, flt)]


@router.get("/api/share/{token}")
def api_share(token: str, request: Request):
    p, conn, share = _resolve(token)
    try:
        ids = _share_ids(conn, share)
        rows = library.rows_by_ids(conn, ids)
        title = share["label"]
        if not title:
            if share["kind"] == "album":
                r = conn.execute("SELECT name FROM albums WHERE id=?", (int(share["ref"]),)).fetchone()
            else:
                r = conn.execute("SELECT name FROM events WHERE id=?", (int(share["ref"]),)).fetchone()
            title = r[0] if r else "Photos"
        viewer = f"guest:{viewer_of(request) or token[:6]}"
        favs = {r[0] for r in conn.execute("SELECT shot_id FROM viewer_decisions WHERE viewer=? AND fav=1", (viewer,))}
        light = [{"id": r["id"], "ar": r["ar"], "v": r["v"], "capture_str": r["capture_str"],
                  "media": r.get("media"), "fav": r["id"] in favs} for r in rows]
        return {"title": title, "rows": light}
    finally:
        conn.close()


@router.post("/api/share/{token}/fav")
def api_share_fav(token: str, request: Request, body: dict = Body(default_factory=dict)):
    p, conn, share = _resolve(token)
    try:
        allowed = set(_share_ids(conn, share))
        viewer = f"guest:{viewer_of(request) or token[:6]}"
        sid = int(body.get("id"))
        if sid not in allowed:
            raise HTTPException(404, "Not in this share.")
        conn.execute("INSERT INTO viewer_decisions(viewer, shot_id, fav, updated) VALUES (?,?,?,?)"
                     " ON CONFLICT(viewer, shot_id) DO UPDATE SET fav=excluded.fav, updated=excluded.updated",
                     (viewer, sid, 1 if body.get("fav", True) else 0, time.time()))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.get("/img/share/{token}/{size}/{sid}")
def img_share(token: str, size: str, sid: int, request: Request):
    p, conn, share = _resolve(token)
    try:
        if sid not in set(_share_ids(conn, share)):
            raise HTTPException(404, "Not in this share.")
        images = request.app.state.images
        if size == "v":
            path = images["render"](p, images["row"](conn, sid), "", 2048)
        elif size == "m":
            path = images["medium"](conn, sid)        # 768 px, for sharp tiles on a Retina screen
        else:
            path = images["small"](p, conn, sid)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
    finally:
        conn.close()
