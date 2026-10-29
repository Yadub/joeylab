"""Pets: the dogs and cats in your photos, grouped so that a pet named once is found everywhere.

* Apple Vision finds dogs and cats, each with a box, on every photo's 768 px thumbnail
  (VNRecognizeAnimalsRequest), and where it sees people (VNDetectHumanRectanglesRequest). Both are kept
  in the `animals` table with a stamp of their own (VERSION), so photos indexed for search before are
  only looked at for animals: their labels, text and search vectors stay as they are.
* Each dog or cat big enough to tell apart is a row in `faces` of kind 'pet' (model MODEL), whose
  vector is the search model's (SigLIP 2, already loaded for search) view of its crop. People groups,
  names, merges and undoes pets with the machinery it has for faces (faces.py), with bars of their own
  (LIMITS), and lists them apart from people.

Nothing here is an identity model: two white Labradors look alike to it. Groups are made strictly and
"Is this …?" asks about the rest, so a pet is named once and confirmed elsewhere.
"""

from __future__ import annotations

import io
import json
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Callable

import numpy as np

from . import config, db

VERSION = "1"
MODEL = "pet"            # faces.model of a pet: its vector is the search model's, of the animal's crop
KINDS = ("dog", "cat")   # what Apple Vision recognises
MIN_PX = 48              # an animal narrower than this on the thumbnail is kept as found, but not grouped
# Bars on pets' vectors as vectors() gives them (centred on the library's pets). Measured on the animals
# in a real library's cached previews: grouping at 0.5 kept groups 94% one kind of dog, by the search
# model's own description of each crop, where uncentred vectors put most dogs in one group at any bar
# that kept dogs and cats apart.
LIMITS = {"cluster": 0.50, "merge": 0.60, "suggest": 0.30, "unclear": 0.45, "track": 0.50}
CENTRE_SHRINK = 20       # with few pets, their mean is pulled towards nothing: a few can't say what all share
CROP_MARGIN = 1.1        # the search model sees the animal's box squared up, with a little around it


def available() -> bool:
    """Apple Vision to find animals, and the search model to tell them apart."""
    from . import search

    return search.vision_available() and search.available()


# ---------------------------------------------------------------- Apple Vision

def _box(o) -> list[float]:
    """An observation's box as [x, y, w, h] fractions from the top left (Vision's origin is the bottom left)."""
    b = o.boundingBox()
    x, y = max(0.0, float(b.origin.x)), max(0.0, 1.0 - float(b.origin.y) - float(b.size.height))
    w, h = min(1.0 - x, float(b.size.width)), min(1.0 - y, float(b.size.height))
    return [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]


def vision_animals(path: str) -> dict:
    """The dogs and cats in a photo, and where people are (Apple Vision): {"found": [[kind, confidence,
    x, y, w, h], ...], "humans": [[x, y, w, h], ...]}."""
    import Vision
    from Foundation import NSURL

    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(path), None)
    animals = Vision.VNRecognizeAnimalsRequest.alloc().init()
    humans = Vision.VNDetectHumanRectanglesRequest.alloc().init()
    res = handler.performRequests_error_([animals, humans], None)
    ok, err = res if isinstance(res, tuple) else (res, None)
    if not ok:                               # (no animals is an answer; Vision not running isn't)
        raise RuntimeError(f"Apple Vision didn't run on this photo ({err or 'no results'}); it will be tried again")
    found = []
    for o in animals.results() or []:
        labels = sorted(o.labels() or [], key=lambda lb: -float(lb.confidence()))
        if labels:
            found.append([str(labels[0].identifier()).lower(), round(float(o.confidence()), 3), *_box(o)])
    return {"found": found, "humans": [_box(o) for o in humans.results() or []]}


# ---------------------------------------------------------------- finding them

def pending(conn) -> list:
    """Photos not looked at for animals yet, or since they changed."""
    return conn.execute(
        "SELECT s.id, s.thumb, s.analyzed_fp FROM shots s LEFT JOIN animals a ON a.shot_id = s.id"
        " WHERE s.status='present' AND s.thumb IS NOT NULL AND s.analyzed_fp IS NOT NULL"
        " AND (a.src IS NULL OR a.src != s.analyzed_fp || ':' || ?) ORDER BY s.id", (VERSION,)).fetchall()


def _square(img, box: list[float], scale: float) -> tuple[int, int, int, int]:
    """A square around a box (fractions), `scale` times its longer side, moved to stay inside the image."""
    W, H = img.size
    x, y, w, h = box
    side = min(max(w * W, h * H) * scale, W, H)
    cx, cy = (x + w / 2) * W, (y + h / 2) * H
    left, top = min(max(0.0, cx - side / 2), W - side), min(max(0.0, cy - side / 2), H - side)
    return int(left), int(top), int(left + side), int(top + side)


def run(conn, progress: Callable[[int, int], None] | None = None, batch: int = 16,
        errors: list | None = None) -> int:
    """Look for dogs and cats in the photos waiting for it (pending), a batch at a time: Apple Vision
    and the search model work first, then the batch is saved in one short write, so a stop keeps what
    was found and the next update carries on. A photo that couldn't be looked at is added to `errors`
    as (shot_id, reason) and stays waiting. Apple Vision not answering in VISION_TIMEOUT_S ends the
    pass for this update; the search model not finishing in GPU_TIMEOUT_S raises search.GpuStuck."""
    from PIL import Image

    from . import deadline, search

    errors = errors if errors is not None else []
    rows = pending(conn)
    if not rows:
        return 0
    model = None
    crop_dir = _crop_dir(conn)
    done = 0
    with deadline.Pool(2, "vision") as pool:    # (Vision fails under heavier load in a background service)
        for i in range(0, len(rows), batch):
            chunk = rows[i : i + batch]
            futs = {r["id"]: pool.submit(vision_animals, r["thumb"]) for r in chunk}
            seen: dict[int, dict] = {}
            stuck = False
            for r in chunk:
                try:
                    seen[r["id"]] = futs[r["id"]].result(timeout=0 if stuck else config.VISION_TIMEOUT_S)
                except FutureTimeout:
                    if not stuck:
                        errors.append((r["id"], f"{search.VISION_STUCK} in {config.VISION_TIMEOUT_S:.0f} s; "
                                                "the rest are looked at for pets in the next update"))
                    stuck = True
                except Exception as e:
                    errors.append((r["id"], f"Apple Vision failed: {type(e).__name__}: {e}"))
            images, crops, owners = {}, [], []
            for r in chunk:
                v = seen.get(r["id"])
                if not v or not v["found"]:
                    continue
                try:
                    with Image.open(r["thumb"]) as im:
                        img = images[r["id"]] = im.convert("RGB")
                except Exception as e:
                    errors.append((r["id"], f"thumbnail unreadable: {type(e).__name__}: {e}"))
                    del seen[r["id"]]
                    continue
                for k, a in enumerate(v["found"]):
                    if a[4] * img.width >= MIN_PX:
                        crops.append(img.crop(_square(img, a[2:6], CROP_MARGIN)))
                        owners.append((r["id"], k))
            vecs: dict = {}
            if crops:
                model = model or search.Model.get()
                try:
                    out = deadline.call(model.embed_images, crops, timeout=config.GPU_TIMEOUT_S,
                                        what="The search model").astype(np.float16)
                except deadline.TimedOut as e:
                    search.Model.drop_stuck()
                    raise search.GpuStuck(str(e)) from None
                vecs = {o: out[j] for j, o in enumerate(owners)}
            for r in chunk:
                if r["id"] in seen:
                    _save(conn, r, seen[r["id"]], images.get(r["id"]), vecs, crop_dir)
            conn.commit()
            done += len(seen)
            if progress:
                progress(min(i + len(chunk), len(rows)), len(rows))
            if stuck:
                break
    return done


def _crop_dir(conn) -> Path | None:
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    return Path(db_file).parent / "previews" / "faces" if db_file else None


def _save(conn, r, v: dict, img, vecs: dict, crop_dir: Path | None) -> None:
    """One photo's animals: the `animals` row, and a pet (a row in faces) for each animal with a vector.
    A pet found again where one was before keeps the name you gave it (faces._carry)."""
    import cv2
    from PIL import Image

    from . import faces
    from .library import version_tag

    sid, fp = r["id"], r["analyzed_fp"]
    conn.execute("INSERT INTO animals(shot_id, src, found, humans) VALUES (?,?,?,?) ON CONFLICT(shot_id)"
                 " DO UPDATE SET src=excluded.src, found=excluded.found, humans=excluded.humans",
                 (sid, f"{fp}:{VERSION}", json.dumps(v["found"]), json.dumps(v["humans"])))
    old = conn.execute("SELECT id, x, y, w, h, cluster, person_id, person_src, model FROM faces WHERE shot_id=? AND model=?",
                       (sid, MODEL)).fetchall()
    new = [{"x": a[2], "y": a[3], "w": a[4], "h": a[5], "vec": vecs[(sid, k)]}
           for k, a in enumerate(v["found"]) if (sid, k) in vecs]
    conn.execute("DELETE FROM faces WHERE shot_id=? AND model=?", (sid, MODEL))
    for f, (_, pid, src) in zip(new, faces._carry(old, new, MODEL)):
        W, H = img.size
        crop = img.crop(_square(img, [f["x"], f["y"], f["w"], f["h"]], 1.0))
        grey = cv2.cvtColor(np.asarray(crop.resize((256, max(8, round(256 * crop.height / crop.width))))
                                       if crop.width > 256 else crop), cv2.COLOR_RGB2GRAY)
        fid = db.new_id(conn, "faces")
        conn.execute(                        # (quality -1: a pet's isn't judged as a face's is)
            "INSERT INTO faces(id, shot_id, x, y, w, h, px, area, sharp, vec, model, kind, person_id, person_src, quality)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,'pet',?,?,-1)",
            (fid, sid, f["x"], f["y"], f["w"], f["h"], round(f["w"] * W * faces.SOURCE_SIDE / max(W, H)), f["w"] * f["h"],
             float(cv2.Laplacian(grey, cv2.CV_64F).var()), f["vec"].tobytes(), MODEL, pid, src))
        if crop_dir:                         # People's 200 px crop, framed as a face's (web server's img_face)
            buf = io.BytesIO()
            img.crop(_square(img, [f["x"], f["y"], f["w"], f["h"]], 1.6)).resize((200, 200), Image.Resampling.LANCZOS) \
                .save(buf, "JPEG", quality=85)
            faces._write_crop(crop_dir, fid, fp, buf.getvalue())
    if crop_dir:
        for o in old:                        # the replaced pets' crops (JoeyLab's own cache)
            (crop_dir / f"{o['id'] % 100:02d}" / f"{o['id']}_{version_tag(fp)}.jpg").unlink(missing_ok=True)


# ---------------------------------------------------------------- grouping them

def vectors(conn) -> tuple[np.ndarray, np.ndarray]:
    """(ids, vectors) of every pet, ids ascending, float32: the search model's vectors of their crops,
    less what the library's pets have in common (their mean, pulled towards nothing when there are few),
    each made unit length again. As they come, two dogs look alike for being dogs on a lawn; centred,
    what tells them apart counts."""
    from . import faces

    ids, raw = faces.vectors(conn, MODEL)
    V = raw.astype(np.float32)
    if not len(ids):
        return ids, V
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
    V -= V.mean(0) * len(V) / (len(V) + CENTRE_SHRINK)
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
    return ids, V


def is_pet_group(conn, cluster: int) -> bool:
    """Whether a group is pets' (from the faces_kind index)."""
    return conn.execute("SELECT 1 FROM faces WHERE kind = 'pet' AND cluster = ? LIMIT 1", (cluster,)).fetchone() is not None


def groups(conn) -> set[int]:
    """Every pets' group (from the faces_kind index)."""
    return {r[0] for r in conn.execute("SELECT DISTINCT cluster FROM faces WHERE kind = 'pet' AND cluster IS NOT NULL")}


def _stamp(conn) -> list:
    """What pets' groups are made from: how many pets there are, the newest, and what you've said about
    them (names, "not this one"), as numbers that change when any of it does."""
    return list(conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(CASE WHEN person_src = 'user' THEN id * (COALESCE(person_id, 0) + 2)"
        " END), 0) FROM faces WHERE kind = 'pet' AND model = ?", (MODEL,)).fetchone())


def cluster(conn, force: bool = False) -> dict:
    """Group the pets, afresh whenever pets were found or you said something about one (there are few of
    them, so this takes moments), with faces' grouping and pets' own bars: two animals in one photo are
    never one pet, and named pets anchor their groups. Group ids are stable, as for faces."""
    from . import faces

    stamp = _stamp(conn)
    if not force and db.get_setting(conn, "pets_grouped") == stamp:
        return {"pets": stamp[0], "groups": None}
    ids, vecs = vectors(conn)
    rows = conn.execute("SELECT f.id, f.shot_id, f.px, f.sharp, f.person_id, f.person_src, f.cluster FROM faces f"
                        f" WHERE f.kind = 'pet' AND f.model = ? AND {faces.PRESENT} ORDER BY f.id", (MODEL,)).fetchall()
    at = faces.rows_of(ids, np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows)))
    keep = np.flatnonzero(at >= 0)
    rows, at = [rows[i] for i in keep], at[keep]
    made = faces._cluster_full(conn, rows, at, vecs, LIMITS)["clusters"] if rows else 0
    _cohesion(conn, ids, vecs)
    db.set_setting(conn, "pets_grouped", stamp)
    return {"pets": len(rows), "groups": made}


def _cohesion(conn, ids: np.ndarray, vecs: np.ndarray) -> None:
    """How well each pets' group agrees, beside faces' groups in face_groups (faces._cohesion keeps these)."""
    from . import faces

    rows = conn.execute(f"SELECT f.id, f.cluster, f.shot_id, COALESCE(f.sharp, 0) FROM faces f WHERE f.kind = 'pet'"
                        f" AND f.model = ? AND f.cluster IS NOT NULL AND {faces.PRESENT} ORDER BY f.cluster, f.id",
                        (MODEL,)).fetchall()
    by: dict[int, list] = {}
    for r in rows:
        by.setdefault(r[1], []).append(r)
    out = []
    for cl, rs in by.items():
        at = faces.rows_of(ids, np.array([r[0] for r in rs], dtype=np.int64))
        V = vecs[at[at >= 0]]
        if not len(V):
            continue
        cen = V.sum(0)
        cen /= np.linalg.norm(cen) + 1e-9
        _, per_shot = np.unique([r[2] for r in rs], return_counts=True)
        out.append((cl, len(rs), float(np.median(V @ cen)), int((per_shot > 1).sum()), float(np.median([r[3] for r in rs]))))
    conn.execute("DELETE FROM face_groups WHERE cluster NOT IN (SELECT DISTINCT cluster FROM faces WHERE cluster IS NOT NULL)")
    conn.executemany("INSERT OR REPLACE INTO face_groups(cluster, faces, cohesion, shared_photos, sharp) VALUES (?,?,?,?,?)", out)
    conn.commit()


def clear_group(cohesion: float | None) -> bool:
    """Whether a pets' group agrees well enough to be one pet (the viewer offers to name all of it)."""
    return cohesion is not None and cohesion >= LIMITS["unclear"]


def counts(conn) -> dict:
    """How many pets were found (from the faces_kind index)."""
    return {"pets": conn.execute("SELECT COUNT(*) FROM faces WHERE kind = 'pet' AND model = ?", (MODEL,)).fetchone()[0]}


# ---------------------------------------------------------------- dogs' faces taken for people's

INSIDE = 0.6             # a face is in an animal's box when this share of it is
NOBODY = 0.3             # ...and Apple Vision sees nobody there: no person's box holds this share of it


def _share(face, box) -> float:
    """How much of a face (x, y, w, h fractions) lies inside a box."""
    fx, fy, fw, fh = face
    bx, by, bw, bh = box
    ix = max(0.0, min(fx + fw, bx + bw) - max(fx, bx))
    iy = max(0.0, min(fy + fh, by + bh) - max(fy, by))
    return ix * iy / (fw * fh) if fw and fh else 0.0


def animal_faces(conn) -> int:
    """Faces the face models found that are an animal's: inside a dog's or cat's box, where Apple Vision
    sees nobody. Each is marked kind 'pet', leaves its group and People's groups of people, and stops
    counting as a face in its photo's scores. Conservative: a face you named (or said isn't someone) is
    never touched, nor one you put back (kind 'person'). Looks only at photos with an animal, so it's
    quick enough to run at every update (faces found again since are checked too). Returns how many."""
    from . import faces

    marked, shots = [], set()
    for sid, found, humans in conn.execute(
            "SELECT a.shot_id, a.found, a.humans FROM animals a JOIN shots s ON s.id = a.shot_id"
            " WHERE a.found IS NOT NULL AND a.found != '[]' AND s.status = 'present'").fetchall():
        boxes = [a[2:6] for a in json.loads(found)]
        people = json.loads(humans or "[]")
        for f in conn.execute("SELECT id, x, y, w, h FROM faces WHERE shot_id=? AND model != ? AND kind IS NULL"
                              " AND COALESCE(person_src, '') != 'user'", (sid, MODEL)):
            box = (f["x"], f["y"], f["w"], f["h"])
            if any(_share(box, b) >= INSIDE for b in boxes) and not any(_share(box, h) >= NOBODY for h in people):
                marked.append(f["id"])
                shots.add(sid)
    if not marked:
        return 0
    for i in range(0, len(marked), 900):
        chunk = marked[i : i + 900]
        conn.execute("UPDATE faces SET kind='pet', cluster=NULL, person_id=NULL, person_src=NULL WHERE kind IS NULL"
                     f" AND COALESCE(person_src, '') != 'user' AND id IN ({','.join('?' * len(chunk))})", chunk)
    faces.refresh_metrics(conn, shots)
    conn.commit()
    return len(marked)


def set_kind(conn, face_ids, kind: str) -> int:
    """Your word on faces the face models found: 'person' puts faces marked as an animal's back among
    people's, for good (never marked again), each into the group it fits; 'pet' says they're an animal's
    (Undo of that). Faces you named are left as they are, as are pets themselves. Returns how many changed."""
    from . import faces

    if kind not in ("person", "pet"):
        raise ValueError("kind is person or pet")
    ids = [int(x) for x in face_ids][:5000]
    changed, shots = [], set()
    for i in range(0, len(ids), 900):
        chunk = ids[i : i + 900]
        q = ",".join("?" * len(chunk))
        rows = conn.execute(f"SELECT id, shot_id FROM faces WHERE id IN ({q}) AND model != ? AND COALESCE(kind, '') != ?"
                            " AND COALESCE(person_src, '') != 'user'", (*chunk, MODEL, kind)).fetchall()
        changed += [r[0] for r in rows]
        shots |= {r[1] for r in rows}
    for i in range(0, len(changed), 900):
        chunk = changed[i : i + 900]
        extra = ", cluster=NULL, person_id=NULL, person_src=NULL" if kind == "pet" else ""
        conn.execute(f"UPDATE faces SET kind=?{extra} WHERE id IN ({','.join('?' * len(chunk))})", (kind, *chunk))
    faces.refresh_metrics(conn, shots)
    conn.commit()
    if kind == "person" and changed:
        faces.place(conn, changed)
    return len(changed)


def taken_for_people(conn, limit: int = 200) -> tuple[list[dict], int]:
    """Faces marked as an animal's (from the faces_kind index), newest first: ({id, shot_id}, how many)."""
    from . import faces

    rows = conn.execute(f"SELECT f.id, f.shot_id FROM faces f WHERE f.kind = 'pet' AND f.model != ? AND {faces.PRESENT}"
                        " ORDER BY f.id DESC", (MODEL,)).fetchall()
    return [{"id": r[0], "shot_id": r[1]} for r in rows[:limit]], len(rows)
