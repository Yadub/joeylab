"""People and pets: naming face groups fast (merge suggestions, a keyboard naming stream),
hiding strangers, coverage, who appears together, face-by-face comparison within a burst, and
forgetting face data. Pets (pets.py) are rows in faces of kind 'pet', grouped and named the same way
and listed apart."""

from __future__ import annotations

from collections import Counter
from itertools import combinations

import numpy as np
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ... import aliases, db, faces as faces_mod, identity, library, pets as pets_mod, scoring
from ..deps import conn_dep, is_owner, one, require_owner, require_writer, viewer_of

router = APIRouter()

UNNAMED = "(f.person_id IS NULL OR f.person_id <= 0)"     # -1: you said "not them" to a named person
# Faces by group that nobody's named, read from the faces_by_group index (+: not by person, which would
# read every face's row).
GROUPED_UNNAMED = "f.cluster IS NOT NULL AND (+f.person_id IS NULL OR +f.person_id <= 0)"
# The face's photo is in the library: photos that went are few, so this is a short list, and the faces
# are read from an index instead of being joined to their photos one by one. (< or >, not !=: either
# side of 'present' is a range of the status index; != would read all of it.)
PRESENT = "f.shot_id NOT IN (SELECT id FROM shots WHERE status < 'present' OR status > 'present')"
STRANGERS = "Strangers"
OTHER_ANIMALS = "Other animals"


def _cover(conn, where: str, args) -> int | None:
    r = conn.execute(f"SELECT f.id FROM faces f WHERE {PRESENT} AND {where}"
                     " ORDER BY COALESCE(f.closed,0), f.px * MIN(f.sharp, 400) DESC LIMIT 1", args).fetchone()
    return r[0] if r else None


def _samples(conn, clusters: list[int], k: int = 4) -> dict[int, list[int]]:
    """The k clearest unnamed faces of each group, for many groups in one query."""
    out: dict[int, list[int]] = {}
    for i in range(0, len(clusters), 500):
        chunk = clusters[i : i + 500]
        q = ",".join("?" * len(chunk))
        for c, fid in conn.execute(
                f"SELECT cluster, id FROM (SELECT f.cluster, f.id, ROW_NUMBER() OVER (PARTITION BY f.cluster"
                f" ORDER BY COALESCE(f.closed,0), f.px * MIN(f.sharp, 400) DESC) AS rn FROM faces f"
                f" WHERE f.cluster IN ({q}) AND {UNNAMED}) WHERE rn <= ?", (*chunk, k)):
            out.setdefault(c, []).append(fid)
    return out


@router.get("/api/p/{p}/people")
def api_people(p: str, request: Request, min_photos: int = 2, limit: int = 60, conn=Depends(conn_dep)):
    """The people named here (with their photos, picks and a cover) and the groups still to name.
    limit=0: the people only, for lists of names (the rail, the viewer, Settings)."""
    # Each person's photos, counted once each (their faces from the faces_by_person index).
    stats = {r["person_id"]: dict(r) for r in conn.execute(
        "SELECT x.person_id, COUNT(*) AS photos, SUM(COALESCE(d.pick, 0) = 1) AS picks,"
        " SUM(COALESCE(d.pick,0) != -1 AND x.open AND (m.quality >= 55 OR d.pick = 1 OR COALESCE(d.rating,0) >= 3)) AS good,"
        " MIN(s.capture_str) AS first, MAX(s.capture_str) AS last"
        " FROM (SELECT f.person_id, f.shot_id, MAX(COALESCE(f.closed,0) = 0) AS open FROM faces f WHERE f.person_id > 0"
        " GROUP BY f.person_id, f.shot_id) x JOIN shots s ON s.id = x.shot_id LEFT JOIN metrics m ON m.shot_id=s.id"
        " LEFT JOIN decisions d ON d.shot_id=s.id WHERE s.status='present' GROUP BY x.person_id")}
    people, hidden = [], []
    others = aliases.everyone(conn)
    for pe in conn.execute("SELECT id, name, COALESCE(kind, 'person') AS kind, COALESCE(hidden, 0) AS hidden FROM people"
                           " ORDER BY name COLLATE NOCASE"):
        st = stats.get(pe["id"], {"photos": 0, "picks": 0, "good": 0, "first": None, "last": None})
        item = {"id": pe["id"], "name": pe["name"], "aliases": others.get(pe["id"], []), "kind": pe["kind"],
                "photos": st["photos"], "picks": st["picks"] or 0, "good": st["good"] or 0, "first": st["first"],
                "last": st["last"], "cover": _cover(conn, "f.person_id=?", (pe["id"],))}
        (hidden if pe["hidden"] else people).append(item)
    groups, unclear_out, clear, small = [], [], [], 0
    pet_groups, pet_all, pet_small = [], [], 0
    if limit > 0:
        # Groups still to name, biggest first; "unclear faces" (a mix of blurred, turned and partial faces,
        # not one person) are set aside so they don't lead the list. One pass over the groups gives both
        # the ones shown and how many smaller ones there are. Pets' groups are listed on their own.
        unclear = faces_mod.unclear_groups(conn)
        of_pets = pets_mod.groups(conn)
        bar = max(1, min_photos)
        all_groups, small = [], 0
        for r in conn.execute(f"SELECT f.cluster, COUNT(DISTINCT f.shot_id) AS photos, COUNT(*) AS faces FROM faces f"
                              f" WHERE {GROUPED_UNNAMED} AND {PRESENT} GROUP BY f.cluster"):
            pet = r["cluster"] in of_pets
            if r["photos"] >= bar:
                (pet_all if pet else all_groups).append(dict(r))
            elif pet:
                pet_small += 1
            else:
                small += 1
        all_groups.sort(key=lambda g: -g["photos"])
        pet_all.sort(key=lambda g: -g["photos"])
        clear = [g for g in all_groups if g["cluster"] not in unclear]
        unclear_list = [g for g in all_groups if g["cluster"] in unclear]
        shown = clear[: max(1, min(int(limit), 5000))]
        pets_shown = pet_all[: max(1, min(int(limit), 5000))]
        samples = _samples(conn, [g["cluster"] for g in shown + unclear_list + pets_shown])
        groups = [{**g, "sample": samples.get(g["cluster"], [])} for g in shown]
        unclear_out = [{**g, "sample": samples.get(g["cluster"], [])} for g in unclear_list]
        pet_groups = [{**g, "sample": samples.get(g["cluster"], [])} for g in pets_shown]
    me = identity.person_id(conn, viewer_of(request))
    people.sort(key=lambda x: x["id"] != me)            # you first
    found = pets_mod.counts(conn)["pets"]
    animal_faces = one(conn, "SELECT COUNT(*) FROM faces WHERE kind = 'pet'") - found      # (faces_kind index)
    return {"me": me, "people": [x for x in people if x["kind"] != "pet"], "pets": [x for x in people if x["kind"] == "pet"],
            "hidden": hidden, "groups": groups, "groups_total": len(clear), "unclear": unclear_out, "smaller_groups": small,
            "pet_groups": pet_groups, "pet_groups_total": len(pet_all), "smaller_pet_groups": pet_small, "pets_found": found,
            "pets_available": pets_mod.available(), "animal_faces": animal_faces,
            "faces": one(conn, "SELECT COUNT(*) FROM faces") - found - animal_faces, "available": faces_mod.available(),
            "enabled": db.get_setting(conn, "faces_enabled", True) and faces_mod.enabled(),
            "recognition": faces_mod.enabled(), "model": faces_mod.current(), "switch": faces_mod.switch_progress(conn),
            "licence_url": faces_mod.MODELS[faces_mod.active()].links[0][1]}


def _person_named(conn, name: str, kind: str | None = None, hidden: bool = False, new_kind: str | None = None) -> int:
    """The person called this, by their name or one of their other names; someone new otherwise (a
    `new_kind` one: naming a pets' group makes a pet, but never turns someone you've named into one).
    The look-up and the new person are one write (until the caller commits): two presses of Save
    at once make one person, not two."""
    name = " ".join(name.split())
    if not name or len(name) > 80:
        raise HTTPException(400, "Give the person a name (up to 80 characters).")
    db.write_lock(conn)
    r = aliases.find(conn, name)
    if r:
        if kind:
            conn.execute("UPDATE people SET kind=? WHERE id=?", (kind, r[0]))
        return r[0]
    pid = db.new_id(conn, "people")              # never reuse a removed person's id
    conn.execute("INSERT INTO people(id, name, kind, hidden) VALUES (?, ?, ?, ?)",
                 (pid, name, kind or new_kind or "person", int(hidden)))
    return pid


@router.get("/api/p/{p}/me")
def api_me(p: str, conn=Depends(conn_dep)):
    """Who you are in this library: the person you've said is you."""
    return {"person_id": identity.person_id(conn, None), "name": identity.name_of(None)}


@router.post("/api/p/{p}/me")
def api_set_me(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """{person_id: id} says that person is you; {person_id: null} clears it."""
    require_owner(request)
    pid = body.get("person_id")
    if pid is None:
        identity.set_name(None, None)
        return {"person_id": None, "name": None}
    r = conn.execute("SELECT id, name FROM people WHERE id=?", (int(pid),)).fetchone()
    if not r:
        raise HTTPException(404, "No such person.")
    identity.set_name(None, r["name"])
    return {"person_id": r["id"], "name": r["name"]}


@router.post("/api/p/{p}/people/name")
def api_name_group(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Name an unnamed group (or a set of faces). An existing name merges into that person. Other
    groups that are clearly the same person join at once; the full re-grouping follows in the
    background."""
    require_writer(request)
    kind = body.get("kind") if body.get("kind") in ("person", "pet") else None
    pet = False
    if body.get("cluster") is not None:
        pet = pets_mod.is_pet_group(conn, int(body["cluster"]))
        faces_mod.vectors(conn, pets_mod.MODEL if pet else None)   # read before the write lock, if they aren't kept already
    elif body.get("faces"):                                          # pets named one by one (from the viewer)
        ids = [int(x) for x in body["faces"]][:900]
        pet = conn.execute(f"SELECT COUNT(*) FROM faces WHERE model = ? AND id IN ({','.join('?' * len(ids))})",
                           (pets_mod.MODEL, *ids)).fetchone()[0] == len(ids)
    pid = _person_named(conn, body.get("name", ""), kind, new_kind="pet" if pet else None)
    aliases.add(conn, pid, [str(x) for x in body.get("aliases") or []])      # (bringing back someone forgotten)
    before = {r[0] for r in conn.execute("SELECT id FROM faces WHERE person_id=?", (pid,))}
    joined = 0
    if body.get("cluster") is not None:
        joined = faces_mod.name_cluster(conn, int(body["cluster"]), pid)
    for fid in body.get("faces") or []:
        conn.execute("UPDATE faces SET person_id=?, person_src='user' WHERE id=?", (pid, int(fid)))
    conn.commit()
    request.app.state.bg.recluster(p)
    # The faces this named, so the app can offer Undo.
    assigned = [r[0] for r in conn.execute("SELECT id FROM faces WHERE person_id=?", (pid,)) if r[0] not in before]
    name = conn.execute("SELECT name FROM people WHERE id=?", (pid,)).fetchone()[0]    # "Papa" names Dad
    return {"id": pid, "name": name, "joined": joined, "assigned": assigned}


@router.post("/api/p/{p}/people/{pid}/unname")
def api_unname(p: str, pid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Undo a naming: these faces go back to being unnamed (those still carrying that name)."""
    require_writer(request)
    ids = [int(x) for x in body.get("faces") or []]
    n = 0
    for i in range(0, len(ids), 900):
        chunk = ids[i : i + 900]
        n += conn.execute(f"UPDATE faces SET person_id=NULL, person_src=NULL WHERE person_id=? AND id IN ({','.join('?' * len(chunk))})",
                          (pid, *chunk)).rowcount
    conn.commit()
    request.app.state.bg.recluster(p)
    return {"unnamed": n}


@router.post("/api/p/{p}/clusters/{cluster}/ignore")
def api_ignore_group(p: str, cluster: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Strangers, or not a face at all: the group leaves the naming list (and coverage) for good."""
    require_writer(request)
    pet = pets_mod.is_pet_group(conn, cluster)
    pid = _person_named(conn, body.get("as") or (OTHER_ANIMALS if pet else STRANGERS), hidden=True,
                        new_kind="pet" if pet else None)
    conn.execute("UPDATE people SET hidden=1 WHERE id=?", (pid,))
    conn.execute(f"UPDATE faces SET person_id=?, person_src='user' WHERE cluster=? AND {UNNAMED.replace('f.', '')}",
                 (pid, cluster))
    conn.commit()
    return {"ok": True}


def _clash(e: aliases.Clash) -> JSONResponse:
    return JSONResponse({"error": str(e), "clash": e.info()}, status_code=409)


def _merged(p: str, request: Request, conn, undo: dict) -> dict:
    conn.commit()
    request.app.state.bg.recluster(p)
    dst = conn.execute("SELECT name FROM people WHERE id=?", (undo["into"],)).fetchone()[0]
    return {"id": undo["into"], "merged": True, "name": dst, "aliases": aliases.of(conn, undo["into"]), "undo": undo}


@router.post("/api/p/{p}/people/{pid}/rename")
def api_rename(p: str, pid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """{name, keep_old}: a new name, keeping the old one as another name if asked. A name someone else
    already has (as their name or another name) means they're one person: this one merges into them."""
    require_writer(request)
    db.write_lock(conn)
    name = " ".join((body.get("name") or "").split())
    was = conn.execute("SELECT name FROM people WHERE id=?", (pid,)).fetchone()
    if not was:
        raise HTTPException(404, "No such person.")
    if not name:
        raise HTTPException(400, "Give the person a name.")
    other = aliases.owner(conn, name)
    if other and other[0] != pid:
        return _merged(p, request, conn, aliases.merge(conn, pid, other[0]))
    others = aliases.of(conn, pid) + ([was[0]] if body.get("keep_old") else [])
    try:
        before = aliases.set_names(conn, pid, name, others)
    except aliases.Clash as e:
        conn.rollback()
        return _clash(e)
    conn.commit()
    return {"id": pid, "name": name, "aliases": aliases.of(conn, pid), "before": before}


@router.post("/api/p/{p}/people/{pid}/names")
def api_names(p: str, pid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """{name, aliases}: a person's name and other names in one go, as the Edit dialog has them (and
    Undo puts back). A name someone else has is refused, saying who has it; with {merge: true}, a
    main name someone else has merges this person into them."""
    require_writer(request)
    db.write_lock(conn)
    now = conn.execute("SELECT name FROM people WHERE id=?", (pid,)).fetchone()
    if not now:
        raise HTTPException(404, "No such person.")
    name = " ".join(str(body.get("name") or "").split())
    others = [str(x) for x in body.get("aliases") or []]
    try:
        other = aliases.owner(conn, name)
        if other and other[0] != pid:
            if not body.get("merge"):
                raise aliases.Clash(name, *other)
            # Their other names come along (those the person they join has already are theirs anyway).
            others = [a for a in others if (aliases.owner(conn, a) or (pid,))[0] != other[0]]
            before = aliases.set_names(conn, pid, now[0], others)
            undo = aliases.merge(conn, pid, other[0])
            undo["person"]["aliases"] = before["aliases"]          # Undo brings them back as they were
            return _merged(p, request, conn, undo)
        before = aliases.set_names(conn, pid, name, others)
    except aliases.Clash as e:
        conn.rollback()
        return _clash(e)
    conn.commit()
    return {"id": pid, "name": conn.execute("SELECT name FROM people WHERE id=?", (pid,)).fetchone()[0],
            "aliases": aliases.of(conn, pid), "before": before}


@router.post("/api/p/{p}/people/{pid}/merge")
def api_merge(p: str, pid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """{other}: they're the same person. The other one's faces join this one, and their name and
    other names become this one's other names. Returns what Undo needs."""
    require_writer(request)
    try:
        other = int(body.get("other"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Say who to merge into this person.")
    db.write_lock(conn)
    return _merged(p, request, conn, aliases.merge(conn, other, pid))


@router.post("/api/p/{p}/people/unmerge")
def api_unmerge(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """Undo a merge, from what the merge returned."""
    require_writer(request)
    db.write_lock(conn)
    try:
        pid = aliases.unmerge(conn, body.get("undo") or {})
    except aliases.Clash as e:
        conn.rollback()
        return _clash(e)
    except ValueError:
        conn.rollback()
        raise
    conn.commit()
    request.app.state.bg.recluster(p)
    return {"id": pid}


@router.post("/api/p/{p}/people/{pid}/kind")
def api_person_kind(p: str, pid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    kind = body.get("kind")
    if kind not in ("person", "pet"):
        raise HTTPException(400, "kind is person or pet.")
    conn.execute("UPDATE people SET kind=?, hidden=COALESCE(?, hidden) WHERE id=?",
                 (kind, None if body.get("hidden") is None else int(bool(body["hidden"])), pid))
    conn.commit()
    return {"ok": True}


@router.post("/api/p/{p}/people/{pid}/hidden")
def api_person_hidden(p: str, pid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    conn.execute("UPDATE people SET hidden=? WHERE id=?", (int(bool(body.get("hidden", True))), pid))
    conn.commit()
    return {"ok": True}


@router.delete("/api/p/{p}/people/{pid}")
def api_forget_person(p: str, pid: int, request: Request, conn=Depends(conn_dep)):
    """Take a name off every face. Returns the faces and name, so the app can offer Undo (naming
    those faces again)."""
    require_writer(request)
    db.write_lock(conn)                                # the faces Undo names again are the faces taken off
    person = conn.execute("SELECT name, kind FROM people WHERE id=?", (pid,)).fetchone()
    if not person:
        raise HTTPException(404, "No such person.")
    others = aliases.of(conn, pid)                     # (they go with the person)
    faces = [r[0] for r in conn.execute("SELECT id FROM faces WHERE person_id=? AND person_src='user'", (pid,))]
    conn.execute("UPDATE faces SET person_id=NULL, person_src=NULL WHERE person_id=?", (pid,))
    conn.execute("DELETE FROM people WHERE id=?", (pid,))
    conn.execute("DELETE FROM face_not_same WHERE person_id=?", (pid,))
    conn.commit()
    request.app.state.bg.recluster(p)
    return {"ok": True, "name": person["name"], "kind": person["kind"], "aliases": others, "faces": faces}


@router.get("/api/p/{p}/people/{pid}/faces")
def api_person_faces(p: str, pid: int, conn=Depends(conn_dep)):
    return {"faces": [dict(r) for r in conn.execute(
        "SELECT f.id, f.shot_id, f.person_src, f.closed FROM faces f JOIN shots s ON s.id=f.shot_id"
        " WHERE s.status='present' AND f.person_id=? ORDER BY f.person_src='user', f.px DESC", (pid,))]}


@router.post("/api/p/{p}/faces/{fid}/unassign")
def api_unassign(p: str, fid: int, request: Request, conn=Depends(conn_dep)):
    """"Not this person": the face stays out of that person, even after re-grouping."""
    require_writer(request)
    conn.execute("UPDATE faces SET person_id=-1, person_src='user' WHERE id=?", (fid,))
    conn.commit()
    request.app.state.bg.recluster(p)
    return {"ok": True}


@router.get("/api/p/{p}/people/animal-faces")
def api_animal_faces(p: str, limit: int = 200, conn=Depends(conn_dep)):
    """Faces the face models found on a dog or cat where nobody is, set aside from people's (pets.py),
    newest first, so any that is a person's can be put back."""
    faces, total = pets_mod.taken_for_people(conn, max(1, min(int(limit), 2000)))
    return {"faces": faces, "total": total}


@router.post("/api/p/{p}/faces/kind")
def api_face_kind(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """{faces, kind}: "person" puts faces set aside as an animal's back among people's, for good; "pet"
    sets them aside again (Undo). Faces you named are left as they are."""
    require_writer(request)
    if body.get("kind") not in ("person", "pet"):
        raise HTTPException(400, "kind is person or pet.")
    try:
        ids = [int(x) for x in body.get("faces") or []]
    except (TypeError, ValueError):
        raise HTTPException(400, "Say which faces.")
    n = pets_mod.set_kind(conn, ids, body["kind"])
    request.app.state.bg.recluster(p)
    return {"changed": n}


def _centres(conn, pet: bool = False) -> tuple[dict[int, np.ndarray], dict[int, np.ndarray]]:
    """The unit centre of each named person's faces (those you named) and of each unnamed group's, in
    photos in the library, from the faces the chosen models found (other models' vectors can't be
    compared with them), or with `pet`, from pets' own vectors. The vectors are kept in memory
    (faces_mod.vectors); who each face is, is read fresh."""
    ids, vecs = pets_mod.vectors(conn) if pet else faces_mod.vectors(conn)
    a = faces_mod.assignments(conn)
    at = faces_mod.rows_of(ids, a["id"])
    out = []
    for keys, mask in ((a["person"], (a["person"] > 0) & a["user"]),
                       (a["cluster"], (a["person"] <= 0) & (a["cluster"] != faces_mod.NO_GROUP))):
        mask &= at >= 0
        k, c = faces_mod.unit_centres(keys[mask], at[mask], vecs)
        out.append({int(x): c[i] for i, x in enumerate(k)})
    return out[0], out[1]


def _suggest(conn, names: dict[int, str], pet: bool, limit: int) -> list[dict]:
    """"Is this …?" for people's groups, or for pets' (each compared only with its own kind of vectors)."""
    people, groups = _centres(conn, pet)
    pids = [k for k in people if k in names]
    if not pids:
        return []
    unclear = set() if pet else faces_mod.unclear_groups(conn)
    cids = [c for c in groups if c not in unclear]
    if not cids:
        return []
    sims = np.stack([groups[c] for c in cids]) @ np.stack([people[k] for k in pids]).T
    # A group you've said isn't someone is never suggested as them again.
    col, row = {k: j for j, k in enumerate(pids)}, {c: i for i, c in enumerate(cids)}
    for pid, cl in conn.execute("SELECT person_id, cluster FROM face_not_same"):
        if pid in col and cl in row:
            sims[row[cl], col[pid]] = -1.0
    best = sims.argmax(axis=1)
    score = sims[np.arange(len(cids)), best]
    sizes = {r[0]: r[1] for r in conn.execute(
        f"SELECT f.cluster, COUNT(DISTINCT f.shot_id) FROM faces f WHERE {GROUPED_UNNAMED} GROUP BY f.cluster")}
    lim = faces_mod.limits(pets_mod.MODEL if pet else None)
    bar = lim["suggest"]
    ranked = sorted((i for i in range(len(cids)) if score[i] >= bar), key=lambda i: (-score[i], -sizes.get(cids[i], 0)))
    top = ranked[: max(1, min(int(limit), 200))]
    samples = _samples(conn, [cids[i] for i in top], 8)
    theirs: dict[int, list[int]] = {}
    out = []
    for i in top:
        pid = pids[int(best[i])]
        if pid not in theirs:          # the named person's (or pet's) clearest faces, to compare with
            theirs[pid] = [r[0] for r in conn.execute(
                f"SELECT f.id FROM faces f WHERE {PRESENT} AND f.person_id=?" + (" AND f.model = ?" if pet else "")
                + " ORDER BY COALESCE(f.closed,0), f.px * MIN(f.sharp, 400) DESC LIMIT 4",
                (pid, pets_mod.MODEL) if pet else (pid,))]
        sim = float(score[i])
        out.append({"cluster": cids[i], "person": pid, "name": names[pid], "similarity": round(sim, 3),
                    "kind": "pet" if pet else "person",
                    # How sure, in the bars of these vectors: as close as a merge needs, half way there, or less.
                    "likely": "very" if sim >= lim["merge"] else "likely" if sim >= (bar + lim["merge"]) / 2 else "possibly",
                    "photos": sizes.get(cids[i], 0), "sample": samples.get(cids[i], []),
                    "person_faces": theirs[pid], "person_cover": (theirs[pid] or [None])[0]})
    return out


@router.get("/api/p/{p}/people/suggestions")
def api_name_suggestions(p: str, limit: int = 40, conn=Depends(conn_dep)):
    """"Is this Alex?": unnamed groups whose faces look like someone you've named, closest first.
    Includes the less certain matches a full re-grouping wouldn't merge on its own (children
    growing up, glasses, beards). Pets' groups that look like a pet you've named come first (there
    are few). Ranked first; faces and covers are fetched only for those shown."""
    if not conn.execute("SELECT 1 FROM faces WHERE person_id > 0 AND person_src = 'user' LIMIT 1").fetchone():
        return {"suggestions": []}                       # nobody named yet: nothing to compare with
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM people WHERE COALESCE(hidden,0)=0")}
    return {"suggestions": _suggest(conn, names, True, limit) + _suggest(conn, names, False, limit)}


@router.get("/api/p/{p}/people/across-years")
def api_across_years(p: str, limit: int = 20, conn=Depends(conn_dep)):
    """"Is this Maya, younger?": unnamed groups from before (or after) the years someone is named in,
    never in a photo with them, whose faces nearest in time look alike. Children split into one group
    per age; this joins them up."""
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM people WHERE COALESCE(hidden,0)=0")}
    out = []
    for x in faces_mod.across_years(conn, limit):
        if x["person"] in names:
            out.append({**x, "name": names[x["person"]]})
    return {"suggestions": out}


@router.post("/api/p/{p}/people/{pid}/not-same")
def api_not_same(p: str, pid: int, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    """That group isn't this person at another age: don't suggest it again."""
    require_writer(request)
    try:
        cluster = int(body.get("cluster"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Say which face group isn't this person.")
    if body.get("undo"):
        conn.execute("DELETE FROM face_not_same WHERE person_id=? AND cluster=?", (pid, cluster))
        conn.commit()
    else:
        faces_mod.not_same(conn, pid, cluster)
    return {"ok": True}


@router.get("/api/p/{p}/people/together")
def api_together(p: str, conn=Depends(conn_dep)):
    """Who appears with whom: pairs of named people and how many photos they share."""
    by_shot: dict[int, set] = {}
    for sid, pid in conn.execute(
        "SELECT f.shot_id, f.person_id FROM faces f JOIN shots s ON s.id=f.shot_id JOIN people pe ON pe.id = f.person_id"
        " WHERE s.status='present' AND f.person_id > 0 AND COALESCE(pe.hidden,0)=0"):
        by_shot.setdefault(sid, set()).add(pid)
    pairs: Counter = Counter()
    for ps in by_shot.values():
        for a, b in combinations(sorted(ps), 2):
            pairs[(a, b)] += 1
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM people")}
    return {"pairs": [{"a": a, "b": b, "a_name": names.get(a), "b_name": names.get(b), "photos": n}
                      for (a, b), n in pairs.most_common(60)]}


@router.delete("/api/p/{p}/faces")
def api_forget_faces(p: str, request: Request, confirm: str = "", conn=Depends(conn_dep)):
    """Forget every face, name and face crop in this library, and stop looking for faces here.
    Face vectors are biometric data; this removes them from the catalog completely. Pets go with
    their names too, and are found again (unnamed) at the next update."""
    require_writer(request)
    if confirm != p:
        raise HTTPException(400, "Type the library's name to confirm.")
    conn.execute("DELETE FROM faces")
    conn.execute("DELETE FROM people")
    conn.execute("DELETE FROM animals")
    conn.execute("UPDATE shots SET faces_fp=NULL")
    db.set_setting(conn, "faces_enabled", False)
    conn.commit()
    from ... import config
    import shutil

    shutil.rmtree(config.project_dir(p) / "previews" / "faces", ignore_errors=True)
    return {"ok": True}


@router.post("/api/p/{p}/faces/enable")
def api_enable_faces(p: str, request: Request, body: dict = Body(default_factory=dict), conn=Depends(conn_dep)):
    require_writer(request)
    if body.get("enabled", True) and not faces_mod.enabled():
        raise HTTPException(409, "Face recognition is off on this Mac. Turn it on in Settings › People & privacy, "
                                 "where you choose its models and see their licence.")
    db.set_setting(conn, "faces_enabled", bool(body.get("enabled", True)))
    return {"enabled": db.get_setting(conn, "faces_enabled", True)}


@router.get("/api/faces")
def api_faces_status():
    """Face recognition for this whole Mac: off until the owner turns it on, choosing one of the sets
    of models (each with what it's good at, its licence and its download) and agreeing to its licence."""
    key = faces_mod.current()
    return {"enabled": faces_mod.enabled(), "model": key, "installed": faces_mod.available(),
            "downloaded": bool(key) and faces_mod.downloaded(key), "earlier": faces_mod.kept_from_before(),
            "licence_url": faces_mod.MODELS[key].links[0][1] if key else None,
            "switch_note": faces_mod.SWITCH_NOTE,
            "choices": [{"key": m.key, "label": m.label, "title": m.title, "use": m.use, "good": m.good, "licence": m.licence,
                         "links": [{"label": label, "url": url} for label, url in m.links], "credit": m.credit,
                         "download_mb": m.download_mb, "kept_mb": m.kept_mb, "downloaded": faces_mod.downloaded(m.key),
                         "installed": faces_mod.available(m.key)} for m in faces_mod.MODELS.values()]}


@router.post("/api/faces")
def api_faces_switch(request: Request, body: dict = Body(default_factory=dict)):
    """{enabled, model, accept_licence}: turn face recognition on with a set of models (or switch to
    the other set: every library's faces are found again at its next update), or off."""
    if not is_owner(request):
        raise HTTPException(403, "Only the owner of this Mac can turn face recognition on or off.")
    on = bool(body.get("enabled"))
    key = body.get("model") or faces_mod.current()
    if on and key not in faces_mod.MODELS:
        raise HTTPException(400, "Choose the face models first.")
    if on and body.get("accept_licence") is not True:
        raise HTTPException(400, f"Agree to the face models' licence to turn face recognition on. {faces_mod.MODELS[key].licence}")
    faces_mod.set_enabled(on, via="web", model=key if on else None)
    return api_faces_status()


# ------------------------------------------------------------ faces across a burst
@router.get("/api/p/{p}/compare-faces")
def api_compare_faces(p: str, ids: str, conn=Depends(conn_dep)):
    """For frames of one burst: each person's face in every frame, side by side, with eyes, smile,
    focus and how well it's caught (Apple Vision's face capture quality), so you can choose the frame
    where everyone looks good. Each frame's `look` weighs the last three, capture quality half of it:
    it only chooses between frames with as many eyes open, and a turned head never decides alone."""
    from ..deps import ids_of

    shot_ids = ids_of(ids) or []
    if not shot_ids or len(shot_ids) > 40:
        raise HTTPException(400, "Compare 1 to 40 frames.")
    q = ",".join("?" * len(shot_ids))
    rows = conn.execute(f"SELECT f.id, f.shot_id, f.x, f.y, f.w, f.px, f.closed, f.smile, f.sharp, f.quality, f.vec, f.person_id,"
                        f" pe.name FROM faces f LEFT JOIN people pe ON pe.id = f.person_id WHERE f.shot_id IN ({q})"
                        " AND f.vec IS NOT NULL AND f.model = ? AND COALESCE(f.kind, '') != 'pet' ORDER BY f.px DESC",
                        (*shot_ids, faces_mod.active())).fetchall()
    if not rows:
        return {"people": [], "frames": shot_ids}
    # Track each person across frames: a face joins the track whose face it most resembles.
    tracks: list[dict] = []
    bar = faces_mod.limits()["track"]
    for r in rows:
        v = np.frombuffer(r["vec"], dtype=np.float16).astype(np.float32)
        best, sim = None, 0.0
        for t in tracks:
            if r["shot_id"] in t["faces"]:
                continue
            s = float(t["v"] @ v / (np.linalg.norm(t["v"]) + 1e-9))
            if s > sim:
                best, sim = t, s
        if best is not None and sim >= bar:
            best["faces"][r["shot_id"]] = r
            best["v"] = best["v"] + v
        else:
            tracks.append({"v": v.copy(), "faces": {r["shot_id"]: r}})
    tracks = [t for t in tracks if len(t["faces"]) >= max(1, len(shot_ids) // 3)]
    tracks.sort(key=lambda t: -max(f["px"] for f in t["faces"].values()))
    out = []
    for t in tracks[:12]:
        first = next(iter(t["faces"].values()))
        out.append({"name": next((f["name"] for f in t["faces"].values() if f["name"]), None),
                    "faces": {str(sid): {"id": f["id"], "closed": f["closed"], "smile": f["smile"],
                                         "sharp": round(f["sharp"] or 0, 1), "px": f["px"],
                                         "caught": None if scoring.caught(f["quality"]) is None else round(scoring.caught(f["quality"]), 2)}
                              for sid, f in t["faces"].items()},
                    "x": first["x"]})
    # Per frame: how many tracked people have open eyes, and how good their faces look.
    def look(f) -> float | None:
        parts = [(w, v) for w, v in ((0.5, scoring.caught(f["quality"])), (0.25, f["smile"]),
                                     (0.25, scoring._ramp(f["sharp"], 1.3, 2.3))) if v is not None]
        return sum(w * v for w, v in parts) / sum(w for w, _ in parts) if parts else None

    score = {}
    for sid in shot_ids:
        fs = [t["faces"].get(sid) for t in tracks[:12]]
        seen = [f for f in fs if f is not None]
        looks = [x for x in (look(f) for f in seen) if x is not None]
        score[str(sid)] = {"open": sum(1 for f in seen if not f["closed"]), "closed": sum(1 for f in seen if f["closed"]),
                           "missing": sum(1 for f in fs if f is None),
                           "look": round(sum(looks) / len(looks), 3) if looks else None}
    return {"people": out, "frames": shot_ids, "score": score, "rows": library.rows_by_ids(conn, shot_ids)}
