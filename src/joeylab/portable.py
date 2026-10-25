"""Getting your work in and out, without lock-in and without touching originals.

* Portable decisions: every rating, pick, label, album, person name, event name and folder name
  as one JSON file, keyed by each file's content fingerprint as well as its path, so it can be
  re-attached to a fresh catalog even after files moved.
* Lightroom Classic: ratings, picks and color labels read from a .lrcat (a copy of it: the
  catalog itself is never opened).
* A client proofing gallery: a self-contained folder of web pages and previews, and importing
  the client's favourites back as their own layer (never your picks).
"""

from __future__ import annotations

import html
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from . import config, db, library
from .export import check_dest

FORMAT = "joeylab-decisions/1"


# ---------------------------------------------------------------- portable decisions

def export_decisions(conn, project: str) -> dict:
    files = {}
    for r in conn.execute("SELECT id, shot_id, fp, size, path FROM files WHERE status IN ('present','missing')"):
        files.setdefault(r["shot_id"], []).append({"fp": r["fp"], "size": r["size"], "path": r["path"]})
    shots = []
    for r in conn.execute(
        "SELECT s.id, s.capture_str, s.camera, s.stem, d.rating, d.pick, d.label FROM shots s"
        " JOIN decisions d ON d.shot_id = s.id WHERE COALESCE(d.rating,0) != 0 OR COALESCE(d.pick,0) != 0 OR d.label IS NOT NULL"):
        shots.append({"files": files.get(r["id"], []), "capture": r["capture_str"], "camera": r["camera"], "stem": r["stem"],
                      "rating": r["rating"] or 0, "pick": r["pick"] or 0, "label": r["label"]})
    people = []
    for r in conn.execute(
        "SELECT pe.name, pe.kind, pe.hidden, f.shot_id, f.x, f.y, f.w, f.h FROM faces f JOIN people pe ON pe.id = f.person_id"
        " WHERE f.person_src = 'user' AND f.person_id > 0"):
        people.append({"name": r["name"], "kind": r["kind"], "hidden": r["hidden"], "files": files.get(r["shot_id"], [])[:1],
                       "box": [r["x"], r["y"], r["w"], r["h"]]})
    from . import aliases

    shown = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM people")}
    other_names = [{"name": shown[pid], "aliases": names} for pid, names in aliases.everyone(conn).items() if pid in shown]
    albums = []
    for a in conn.execute("SELECT * FROM albums WHERE kind IN ('manual', 'smart')"):
        items = [files.get(r[0], [])[:1] for r in conn.execute("SELECT shot_id FROM album_items WHERE album_id=?", (a["id"],))]
        albums.append({"name": a["name"], "kind": a["kind"], "query": a["query"], "description": a["description"],
                       "items": [i[0] for i in items if i]})
    events = []
    for e in conn.execute("SELECT id, name FROM events WHERE user_named = 1"):
        some = [files.get(r[0], [])[:1] for r in conn.execute(
            "SELECT id FROM shots WHERE event_id = ? ORDER BY eff_ts LIMIT 20", (e["id"],))]
        events.append({"name": e["name"], "files": [i[0] for i in some if i]})
    # (Not the frames tagged for the shot list: they're this catalog's photo ids.)
    settings = {r[0]: json.loads(r[1]) for r in conn.execute("SELECT key, value FROM settings WHERE key != 'shotlist_tags'")}
    favs = [{"viewer": r[0], "files": files.get(r[1], [])[:1]} for r in conn.execute(
        "SELECT viewer, shot_id FROM viewer_decisions WHERE fav = 1")]
    comments = [{"viewer": r[0], "text": r[1], "ts": r[2], "files": files.get(r[3], [])[:1]} for r in conn.execute(
        "SELECT viewer, text, ts, shot_id FROM comments")]
    offsets = [dict(r) for r in conn.execute("SELECT camera_key, offset_s, source FROM camera_offsets")]
    from . import keywords as kw_mod

    tagged: dict[int, list[list[str]]] = {}
    all_paths = kw_mod.paths(conn)
    for sid, kid in conn.execute("SELECT shot_id, keyword_id FROM shot_keywords"):
        tagged.setdefault(sid, []).append(all_paths.get(kid, []))
    kws = [{"files": files.get(sid, [])[:1], "keywords": paths} for sid, paths in tagged.items() if files.get(sid)]
    return {"format": FORMAT, "project": project, "exported": time.time(), "decisions": shots, "people": people,
            "other_names": other_names, "albums": albums, "events": events, "settings": settings, "favourites": favs,
            "comments": comments, "camera_offsets": offsets, "keywords": kws}


def _matcher(conn):
    by_fp, by_path = {}, {}
    for r in conn.execute("SELECT shot_id, fp, size, path FROM files"):
        by_fp[(r["fp"], r["size"])] = r["shot_id"]
        by_path[r["path"]] = (r["shot_id"], r["fp"])

    def find(files: list[dict]) -> int | None:
        # The same file at the same path first (with identical copies elsewhere, the decision belongs
        # to the copy it was made on), then the content wherever it lives now, then the path alone.
        for f in files:
            here = by_path.get(f.get("path"))
            if here and here[1] == f.get("fp"):
                return here[0]
        for f in files:
            sid = by_fp.get((f.get("fp"), f.get("size")))
            if sid:
                return sid
        for f in files:
            if f.get("path") in by_path:
                return by_path[f["path"]][0]
        return None
    return find


def import_decisions(conn, data: dict, overwrite: bool = False, viewer: str | None = None) -> dict:
    """Re-attach a portable export. Without overwrite, only photos with no decision yet change.
    One undoable step for the decisions."""
    if data.get("format") != FORMAT:
        raise ValueError("That isn't a JoeyLab decisions file.")
    find = _matcher(conn)
    items, unmatched = [], 0
    for d in data.get("decisions", []):
        sid = find(d.get("files", []))
        if not sid:
            unmatched += 1
            continue
        if not overwrite and conn.execute("SELECT 1 FROM decisions WHERE shot_id=? AND (COALESCE(rating,0)!=0 OR"
                                          " COALESCE(pick,0)!=0 OR label IS NOT NULL)", (sid,)).fetchone():
            continue
        items.append({"id": sid, "rating": d.get("rating", 0), "pick": d.get("pick", 0), "label": d.get("label") or ""})
    batch, changed = library.decide(conn, items, what="import decisions", source="import", viewer=viewer)
    from . import aliases

    named = 0
    for p in data.get("people", []):
        sid = find(p.get("files", []))
        if not sid:
            continue
        x, y, w, h = p["box"]
        face = conn.execute("SELECT id FROM faces WHERE shot_id=? AND ABS(x - ?) < ? AND ABS(y - ?) < ?",
                            (sid, x, max(w / 2, 0.01), y, max(w / 2, 0.01))).fetchone()
        if not face:
            continue
        pid = aliases.find(conn, p["name"])           # by their name or another name here
        if pid:
            pid = pid[0]
        else:
            pid = db.new_id(conn, "people")
            conn.execute("INSERT INTO people(id, name, kind, hidden) VALUES (?,?,?,?)",
                         (pid, p["name"], p.get("kind") or "person", p.get("hidden") or 0))
        conn.execute("UPDATE faces SET person_id=?, person_src='user' WHERE id=?", (pid, face[0]))
        named += 1
    for o in data.get("other_names", []):              # "Dad" is also "Papa", when Dad is here
        who = aliases.find(conn, o.get("name") or "")
        if who:
            aliases.add(conn, who[0], [str(x) for x in o.get("aliases") or []])
    albums = 0
    for a in data.get("albums", []):
        if conn.execute("SELECT 1 FROM albums WHERE name=? AND kind IN ('manual','smart')", (a["name"],)).fetchone():
            continue
        aid = db.new_id(conn, "albums")
        conn.execute("INSERT INTO albums(id, name, kind, query, description, created, updated) VALUES (?,?,?,?,?,?,?)",
                     (aid, a["name"], a.get("kind") or "manual", a.get("query"), a.get("description"), time.time(), time.time()))
        add_to_album(conn, aid, [s for s in (find([f]) for f in a.get("items", [])) if s])
        albums += 1
    events_named = 0
    for e in data.get("events", []):
        sids = [s for s in (find([f]) for f in e.get("files", [])) if s]
        if not sids:
            continue
        r = conn.execute(f"SELECT event_id, COUNT(*) FROM shots WHERE id IN ({','.join('?' * len(sids))}) AND event_id IS NOT NULL"
                         " GROUP BY event_id ORDER BY 2 DESC LIMIT 1", sids).fetchone()
        if r:
            conn.execute("UPDATE events SET name=?, user_named=1 WHERE id=?", (e["name"], r[0]))
            events_named += 1
    for f in data.get("favourites", []):
        sid = find(f.get("files", []))
        if sid:
            conn.execute("INSERT OR IGNORE INTO viewer_decisions(viewer, shot_id, fav, updated) VALUES (?,?,1,?)",
                         (f["viewer"], sid, time.time()))
    for k, v in (data.get("settings") or {}).items():
        if not conn.execute("SELECT 1 FROM settings WHERE key=?", (k,)).fetchone():
            conn.execute("INSERT INTO settings(key, value) VALUES (?,?)", (k, json.dumps(v)))
    conn.commit()
    from . import keywords as kw_mod

    tagged: dict[int, list[list[str]]] = {}
    for item in data.get("keywords", []):
        sid = find(item.get("files", []))
        if sid:
            tagged.setdefault(sid, []).extend(item.get("keywords") or [])
    kw = kw_mod.import_names(conn, tagged, "import", "keywords from an export") if tagged else {"photos": 0}
    return {"decisions": len(changed), "unmatched": unmatched, "people": named, "albums": albums,
            "events": events_named, "batch": batch, "keywords": kw["photos"]}


# ---------------------------------------------------------------- albums in the catalog

def add_to_album(conn, album_id: int, shot_ids: list[int], added: list[int] | None = None) -> int:
    """Put photos in an album (each once). Returns how many were new to it; their ids go in `added`."""
    n = 0
    for sid in shot_ids:
        f = conn.execute("SELECT id, path FROM files WHERE id = (SELECT preview_file_id FROM shots WHERE id=?)", (sid,)).fetchone()
        if not f or conn.execute("SELECT 1 FROM album_items WHERE album_id=? AND shot_id=?", (album_id, sid)).fetchone():
            continue
        cur = conn.execute("INSERT OR IGNORE INTO album_items(album_id, file_id, shot_id, name, source_path) VALUES (?,?,?,?,?)",
                           (album_id, f["id"], sid, f"{sid}-{os.path.basename(f['path'])}", f["path"]))
        n += cur.rowcount
        if cur.rowcount and added is not None:
            added.append(sid)
    conn.execute("UPDATE albums SET updated=? WHERE id=?", (time.time(), album_id))
    conn.commit()
    return n


def remove_from_album(conn, album_id: int, shot_ids: list[int], removed: list[int] | None = None) -> int:
    """Take photos out of an album (the photos themselves stay in the library). Returns how many went."""
    n = 0
    for sid in shot_ids:
        k = conn.execute("DELETE FROM album_items WHERE album_id=? AND shot_id=?", (album_id, sid)).rowcount
        n += k
        if k and removed is not None:
            removed.append(sid)
    if n:
        conn.execute("UPDATE albums SET updated=? WHERE id=?", (time.time(), album_id))
    conn.commit()
    return n


# ---------------------------------------------------------------- Lightroom Classic

def ratings_from_files(conn) -> tuple[list[dict], int]:
    """Stars (and rejects) set in camera or written into files by another app, as decisions for
    photos you haven't decided yet. A Lightroom (or other editor's) export carries the stars you gave
    the original, so they go on the original too, when the export's camera and capture time (to the
    hundredth of a second) point at exactly one photo. Returns (items, how many went to originals)."""
    from . import exif

    undecided = "NOT EXISTS (SELECT 1 FROM decisions d WHERE d.shot_id = s.id)"
    rated = conn.execute(
        f"SELECT s.id, s.cam_rating, s.body_serial, s.capture_ts FROM shots s WHERE s.status='present'"
        f" AND COALESCE(s.cam_rating, 0) != 0").fetchall()
    software = {}
    for sid, sw in conn.execute("SELECT shot_id, json_extract(meta, '$.software') FROM files WHERE meta IS NOT NULL"):
        if exif.is_editor(sw):
            software[sid] = sw
    items: dict[int, dict] = {}
    to_originals = 0
    for sid, r, body, ts in rated:
        it = {"rating": r if r > 0 else None, "pick": -1 if r == -1 else None}
        if conn.execute(f"SELECT 1 FROM shots s WHERE s.id=? AND {undecided}", (sid,)).fetchone():
            items[sid] = {"id": sid, **it}
        if sid in software and body and ts is not None:
            orig = [o for (o,) in conn.execute(
                "SELECT id FROM shots WHERE status='present' AND capture_ts > ? AND capture_ts < ? AND id != ? AND body_serial = ?",
                (ts - 0.05, ts + 0.05, sid, body)) if o not in software]     # a range, so the time index is used
            if len(orig) == 1 and orig[0] not in items and conn.execute(
                    f"SELECT 1 FROM shots s WHERE s.id=? AND COALESCE(s.cam_rating, 0) = 0 AND {undecided}", (orig[0],)).fetchone():
                items[orig[0]] = {"id": orig[0], **it}
                to_originals += 1
    return list(items.values()), to_originals


def import_lightroom(conn, lrcat: str, overwrite: bool = False, viewer: str | None = None) -> dict:
    """Ratings, picks/rejects and color labels from a Lightroom Classic catalog. A copy of the
    .lrcat is read (Lightroom's own file is never opened by SQLite), matched by full path, then by
    file name and capture time."""
    src = Path(lrcat).expanduser()
    if not src.is_file() or src.suffix.lower() != ".lrcat":
        raise ValueError("Choose a Lightroom catalog file (.lrcat).")
    tmpdir = Path(tempfile.mkdtemp(prefix="lrcat-", dir=str(config.HOME)))
    try:
        copy = tmpdir / "catalog.lrcat"
        shutil.copy2(src, copy)
        wal = Path(str(src) + "-wal")
        if wal.exists():                 # recent changes Lightroom hasn't merged yet (copied, not touched)
            shutil.copy2(wal, Path(str(copy) + "-wal"))
        lr = sqlite3.connect(copy)
        lr.row_factory = sqlite3.Row
        rows = lr.execute(
            "SELECT rf.absolutePath || fo.pathFromRoot || f.baseName || '.' || f.extension AS path, f.baseName AS stem,"
            " i.id_local AS image, i.rating, i.pick, i.colorLabels AS label, i.captureTime AS capture FROM Adobe_images i"
            " JOIN AgLibraryFile f ON f.id_local = i.rootFile JOIN AgLibraryFolder fo ON fo.id_local = f.folder"
            " JOIN AgLibraryRootFolder rf ON rf.id_local = fo.rootFolder").fetchall()
        try:                              # the keyword tree: names, parents, and which image has which
            kw_nodes = {r[0]: (r[1], r[2]) for r in lr.execute("SELECT id_local, name, parent FROM AgLibraryKeyword")}
            kw_links = lr.execute("SELECT image, tag FROM AgLibraryKeywordImage").fetchall()
        except sqlite3.DatabaseError:
            kw_nodes, kw_links = {}, []
        lr.close()
    except sqlite3.DatabaseError as e:
        raise ValueError(f"Couldn't read that Lightroom catalog ({e}).")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    by_path = {r[0]: r[1] for r in conn.execute("SELECT path, shot_id FROM files")}
    by_name = {}
    for sid, stem, cap in conn.execute("SELECT id, stem, capture_str FROM shots"):
        by_name[(stem.lower(), (cap or "")[:19])] = sid
    items, unmatched = [], 0
    shot_of_image: dict[int, int] = {}
    for r in rows:
        sid = by_path.get(r["path"])
        if not sid and r["capture"]:
            sid = by_name.get((r["stem"].lower(), r["capture"].replace("T", " ")[:19]))
        if not sid:
            unmatched += 1
            continue
        shot_of_image[r["image"]] = sid
        rating = int(r["rating"] or 0)
        pick = int(r["pick"] or 0)
        label = (r["label"] or "").strip().capitalize() or None
        if not (rating or pick or label):
            continue
        if not overwrite and conn.execute("SELECT 1 FROM decisions WHERE shot_id=? AND (COALESCE(rating,0)!=0 OR"
                                          " COALESCE(pick,0)!=0)", (sid,)).fetchone():
            continue
        items.append({"id": sid, "rating": rating, "pick": max(-1, min(1, pick)),
                      "label": label if label in library.LABELS else None})
    batch, changed = library.decide(conn, items, what="import from Lightroom", source="import", viewer=viewer)

    def kw_path(kid: int) -> list[str]:
        out, seen = [], set()
        while kid in kw_nodes and kid not in seen:      # Lightroom's root keyword has no name
            seen.add(kid)
            name, kid = kw_nodes[kid]
            if name:
                out.append(name)
        return out[::-1]
    from . import keywords

    tagged: dict[int, list[list[str]]] = {}
    for image, tag in kw_links:
        if image in shot_of_image and kw_path(tag):
            tagged.setdefault(shot_of_image[image], []).append(kw_path(tag))
    kw = keywords.import_names(conn, tagged, "lightroom", "keywords from Lightroom") if tagged else {"photos": 0, "new_keywords": 0}
    return {"imported": len(changed), "in_catalog": len(rows), "unmatched": unmatched, "batch": batch,
            "keywords": kw["photos"], "new_keywords": kw["new_keywords"]}


# ---------------------------------------------------------------- client proofing gallery

GALLERY_MARK = '<meta name="generator" content="JoeyLab gallery">'
GALLERY_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="generator" content="JoeyLab gallery">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
:root{{--bg:#F4F3EF;--ink:#22211E;--muted:#6E6B63;--acc:#4D6B66;--line:#E2E0D8}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1B1B19;--ink:#ECEAE3;--muted:#A09D94;--acc:#93B2AB;--line:#34332F}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,sans-serif}}
header{{padding:24px 16px 8px;max-width:1200px;margin:auto}} h1{{font:400 30px/1.1 Georgia,serif;margin:0 0 6px}}
.muted{{color:var(--muted)}} main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;padding:16px;max-width:1200px;margin:auto}}
figure{{margin:0;position:relative;cursor:zoom-in}} figure img{{width:100%;height:220px;object-fit:cover;border-radius:4px;display:block}}
button.heart{{position:absolute;right:8px;bottom:8px;border:0;border-radius:99px;padding:6px 10px;background:rgba(0,0,0,.55);color:#fff;font-size:15px;cursor:pointer}}
button.heart.on{{background:var(--acc)}} .bar{{position:sticky;bottom:0;background:var(--bg);border-top:1px solid var(--line);padding:12px 16px;display:flex;gap:12px;align-items:center;justify-content:center}}
.bar button{{border:1px solid var(--line);background:var(--acc);color:#fff;border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer}}
dialog{{border:0;padding:0;background:#000;max-width:96vw;max-height:96vh}} dialog img{{max-width:96vw;max-height:92vh;display:block}}
textarea{{width:100%;height:120px}}
</style></head><body>
<header><h1>{title}</h1><div class="muted">{count} photos · tap ♡ on the ones you love, then send your selection.</div></header>
<main id="g"></main>
<footer class="muted" style="text-align:center;padding:24px 0 32px;font-size:12px">Made with JoeyLab, by Yaduraj Bhageria</footer>
<div class="bar"><span id="n" class="muted">0 chosen</span><button id="send">Send my selection</button></div>
<dialog id="big"><img id="bigimg" alt=""></dialog>
<dialog id="out" style="background:var(--bg);color:var(--ink);padding:18px;max-width:520px">
<p>Copy this list and send it back (email or message):</p><textarea id="list" readonly></textarea>
<p><button onclick="navigator.clipboard.writeText(document.getElementById('list').value)">Copy</button>
<button onclick="document.getElementById('out').close()">Close</button></p></dialog>
<script>
const PHOTOS={photos};const KEY="fav-"+{key};let fav=new Set();try{{fav=new Set(JSON.parse(localStorage.getItem(KEY)||"[]"))}}catch(e){{}}
const g=document.getElementById("g"),n=document.getElementById("n");
function save(){{try{{localStorage.setItem(KEY,JSON.stringify([...fav]))}}catch(e){{}}n.textContent=fav.size+" chosen"}}
for(const p of PHOTOS){{const f=document.createElement("figure");const i=document.createElement("img");i.loading="lazy";i.src="photos/"+p.file;i.alt=p.name;
f.append(i);const b=document.createElement("button");b.className="heart"+(fav.has(p.name)?" on":"");b.textContent=fav.has(p.name)?"♥":"♡";
b.onclick=e=>{{e.stopPropagation();fav.has(p.name)?fav.delete(p.name):fav.add(p.name);b.className="heart"+(fav.has(p.name)?" on":"");b.textContent=fav.has(p.name)?"♥":"♡";save()}};
f.append(b);f.onclick=()=>{{document.getElementById("bigimg").src="photos/"+p.file;document.getElementById("big").showModal()}};g.append(f)}}
document.getElementById("big").onclick=e=>e.currentTarget.close();
document.getElementById("send").onclick=()=>{{document.getElementById("list").value=[...fav].join("\\n");document.getElementById("out").showModal()}};save();
</script></body></html>"""


def _script_json(value) -> str:
    """JSON that can sit inside <script>: a title or photo name can't close the tag ("</script>")
    or start a comment, whatever it says."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def export_gallery(conn, flt, dest: Path, title: str, render) -> dict:
    """A folder anyone can open in a browser (or you can upload anywhere): previews of the
    selected photos and a page to heart favourites. `render(row) -> Path` gives a 2048 px JPEG."""
    dest = check_dest(dest)
    page_file = dest / "index.html"
    if page_file.exists() and GALLERY_MARK not in page_file.read_text(encoding="utf-8", errors="ignore")[:4000]:
        raise ValueError(f"{page_file} already exists and isn't a JoeyLab gallery: choose an empty folder.")
    (dest / "photos").mkdir(parents=True, exist_ok=True)
    photos = []
    for s in library.shots(conn, flt):
        if s.get("media") == "video":
            continue
        try:
            src = render(s)
        except Exception:
            continue
        name = f"{s['stem']}"
        file = f"{s['id']}-{s['stem']}.jpg"
        shutil.copy2(src, dest / "photos" / file)
        photos.append({"name": name, "file": file})
    page = GALLERY_PAGE.format(title=html.escape(title), count=len(photos), photos=_script_json(photos),
                               key=_script_json(f"{title}-{int(time.time())}"))
    (dest / "index.html").write_text(page, encoding="utf-8")
    return {"dest": str(dest), "photos": len(photos)}


def import_favourites(conn, text: str, who: str) -> dict:
    """A list of photo names a client sent back becomes their favourites layer (viewer
    "client:<who>"), never your picks."""
    names = {n.strip().lower() for n in text.replace(",", "\n").splitlines() if n.strip()}
    viewer = f"client:{who.strip() or 'client'}"
    hit = 0
    for sid, stem in conn.execute("SELECT id, stem FROM shots WHERE status='present'"):
        if stem.lower() in names:
            conn.execute("INSERT INTO viewer_decisions(viewer, shot_id, fav, updated) VALUES (?,?,1,?)"
                         " ON CONFLICT(viewer, shot_id) DO UPDATE SET fav=1, updated=excluded.updated", (viewer, sid, time.time()))
            hit += 1
    conn.commit()
    return {"matched": hit, "names": len(names), "viewer": viewer}
