"""Pets: dogs and cats found by Apple Vision, grouped by the search model's view of each, and named
in People like faces: naming, "Is this …?", "not this one", merging and undo, search and the Library's
filter. The test photos hold no animals, so Apple Vision's answers and the model's vectors are stood in."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import add_root, make_jpeg, run_all

DIM = 32


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def _e(i):
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1
    return v


class _Model:
    """Stands in for the search model: each crop's vector, in the order the crops are given."""

    def __init__(self, vecs):
        self.vecs = list(vecs)
        self.lock = threading.RLock()
        self.calls = 0

    def embed_images(self, images):
        self.calls += 1
        out, self.vecs = self.vecs[: len(images)], self.vecs[len(images):]
        return np.stack(out)


def _library(env, n=8):
    _, lib = env
    for i in range(n):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    return conn, [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]


def _stand_in(monkeypatch, found: dict, vecs: list, fail: set | None = None):
    """Apple Vision finds `found[shot index]` ([[kind, confidence, x, y, w, h], ...]) in each photo, by
    the thumbnail it's given; the model hands out `vecs` in order."""
    from joeylab import pets, search

    model = _Model(vecs)
    monkeypatch.setattr(pets, "available", lambda: True)
    monkeypatch.setattr(search.Model, "get", classmethod(lambda cls: model))
    seen = []

    def vision(path):
        sid = _sid_of[path]
        seen.append(sid)
        if fail and sid in fail:
            raise RuntimeError("no results")
        return {"found": found.get(sid, []), "humans": []}

    monkeypatch.setattr(pets, "vision_animals", vision)
    return model, seen


_sid_of: dict = {}


def _thumbs(conn):
    _sid_of.clear()
    _sid_of.update({r[1]: r[0] for r in conn.execute("SELECT id, thumb FROM shots")})


DOG = lambda: ["dog", 0.8, 0.2, 0.2, 0.5, 0.6]      # noqa: E731  (a big dog in the middle)


def _joey_world(env, monkeypatch):
    """Eight photos: Joey in 0-2, Joey looking different (another day's light) in 3-4, Rex in 5, nothing in 6-7."""
    conn, sids = _library(env)
    _thumbs(conn)
    rng = np.random.default_rng(0)
    joey, joey2, rex = _e(0), _unit(0.6 * _e(0) + 0.8 * _e(1)), _e(2)
    who = [joey, joey, joey, joey2, joey2, rex]
    vecs = [_unit(v + rng.normal(scale=0.02, size=DIM)) for v in who]
    found = {sids[i]: [DOG()] for i in range(6)}
    return conn, sids, _stand_in(monkeypatch, found, vecs)


def _settled(c):
    for _ in range(300):
        if not c.app.state.bg.running:
            return
        time.sleep(0.02)


def test_pets_are_found_grouped_and_named_once(env, monkeypatch):
    from joeylab import library, pets, query, search
    from joeylab.web.server import create_app

    conn, sids, (model, seen) = _joey_world(env, monkeypatch)
    before = conn.execute("SELECT COUNT(*) FROM search_vecs").fetchone()[0] if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='search_vecs'").fetchone() else 0
    assert pets.run(conn) == 8
    assert sorted(seen) == sids                                   # every photo looked at once
    assert conn.execute("SELECT COUNT(*) FROM animals").fetchone()[0] == 8
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE kind='pet' AND model=?", (pets.MODEL,)).fetchone()[0] == 6
    # Its own stamp: nothing is waiting any more, and the search index wasn't touched.
    assert pets.pending(conn) == []
    assert (conn.execute("SELECT COUNT(*) FROM search_vecs").fetchone()[0] if before else 0) == before
    assert search.INDEX_VERSION == "1"
    # Each pet has a People crop, like a face.
    fid = conn.execute("SELECT id FROM faces WHERE model=? LIMIT 1", (pets.MODEL,)).fetchone()[0]
    assert list((env[0] / "home" / "projects" / "t" / "previews" / "faces").rglob(f"{fid}_*.jpg"))

    out = pets.cluster(conn)
    assert out["pets"] == 6 and out["groups"] == 3
    groups = {}
    for sid, cl in conn.execute("SELECT shot_id, cluster FROM faces WHERE model=?", (pets.MODEL,)):
        groups.setdefault(cl, set()).add(sids.index(sid))
    assert sorted(map(sorted, groups.values())) == [[0, 1, 2], [3, 4], [5]]
    assert pets.cluster(conn)["groups"] is None                   # nothing changed: not grouped again

    c = TestClient(create_app())
    d = c.get("/api/p/t/people", params={"min_photos": 1}).json()
    assert d["pets_found"] == 6 and d["faces"] == 0 and d["groups"] == []
    assert [g["photos"] for g in d["pet_groups"]] == [3, 2, 1]
    joey_cl = d["pet_groups"][0]["cluster"]

    # Named once: a pet, listed with the pets, and nobody else is turned into one.
    r = c.post("/api/p/t/people/name", json={"cluster": joey_cl, "name": "Joey"}).json()
    _settled(c)
    joey = r["id"]
    assert conn.execute("SELECT kind FROM people WHERE id=?", (joey,)).fetchone()[0] == "pet"
    d = c.get("/api/p/t/people", params={"min_photos": 1}).json()
    assert [x["name"] for x in d["pets"]] == ["Joey"] and d["people"] == []
    assert d["pets"][0]["photos"] == 3 and d["pets"][0]["cover"]
    assert len(d["pet_groups"]) == 2

    # "Is this Joey?": his other group, not Rex.
    s = c.get("/api/p/t/people/suggestions").json()["suggestions"]
    assert [(x["kind"], x["name"]) for x in s] == [("pet", "Joey")]
    assert {sids.index(i) for i in library_shots(conn, s[0]["cluster"])} == {3, 4}
    assert s[0]["likely"] in ("possibly", "likely") and s[0]["person_faces"]
    r2 = c.post("/api/p/t/people/name", json={"cluster": s[0]["cluster"], "name": "Joey"}).json()
    assert r2["id"] == joey and len(r2["assigned"]) == 2
    _settled(c)

    # Found everywhere: the Library's filter, and a search for his name.
    flt = library.Filter(person_id=joey)
    ids = lambda: {r["id"] for r in library.shots(conn, flt)}       # noqa: E731
    assert {sids.index(x) for x in ids()} == {0, 1, 2, 3, 4}
    rest, parsed = query.natural("Joey at home", conn)
    assert parsed["people"] == [joey]

    # Not this one: stays out after grouping again; Undo of a naming takes the name off.
    one = conn.execute("SELECT id FROM faces WHERE person_id=? AND model=? ORDER BY id DESC LIMIT 1", (joey, pets.MODEL)).fetchone()[0]
    c.post(f"/api/p/t/faces/{one}/unassign")
    _settled(c)
    pets.cluster(conn, force=True)
    assert conn.execute("SELECT person_id FROM faces WHERE id=?", (one,)).fetchone()[0] == -1
    c.post(f"/api/p/t/people/{joey}/unname", json={"faces": [f for f in r2["assigned"] if f != one]})
    _settled(c)
    assert {sids.index(x) for x in ids()} == {0, 1, 2}

    # Merging pets, and Undo of the merge.
    rex_cl = conn.execute("SELECT cluster FROM faces WHERE shot_id=? AND model=?", (sids[5], pets.MODEL)).fetchone()[0]
    rex = c.post("/api/p/t/people/name", json={"cluster": rex_cl, "name": "Rex"}).json()["id"]
    _settled(c)
    m = c.post(f"/api/p/t/people/{joey}/merge", json={"other": rex}).json()
    assert m["merged"] and sids[5] in ids()
    c.post("/api/p/t/people/unmerge", json={"undo": m["undo"]})
    assert sids[5] not in ids()


def library_shots(conn, cluster):
    return [r[0] for r in conn.execute("SELECT shot_id FROM faces WHERE cluster=?", (cluster,))]


def test_a_pets_group_never_turns_someone_into_a_pet(env, monkeypatch):
    from joeylab import pets
    from joeylab.web.server import create_app

    conn, sids, _ = _joey_world(env, monkeypatch)
    pets.run(conn)
    pets.cluster(conn)
    dad = conn.execute("INSERT INTO people(name, kind) VALUES ('Dad', 'person')").lastrowid
    conn.commit()
    cl = conn.execute("SELECT cluster FROM faces WHERE shot_id=? AND model=?", (sids[5], pets.MODEL)).fetchone()[0]
    c = TestClient(create_app())
    assert c.post("/api/p/t/people/name", json={"cluster": cl, "name": "Dad"}).json()["id"] == dad
    assert conn.execute("SELECT kind FROM people WHERE id=?", (dad,)).fetchone()[0] == "person"
    # Hiding a pets' group: as other animals, a hidden pet.
    _settled(c)
    cl2 = conn.execute("SELECT cluster FROM faces WHERE shot_id=? AND model=?", (sids[3], pets.MODEL)).fetchone()[0]
    c.post(f"/api/p/t/clusters/{cl2}/ignore", json={})
    assert tuple(conn.execute("SELECT p.name, p.kind, p.hidden FROM people p JOIN faces f ON f.person_id = p.id WHERE f.cluster=?",
                              (cl2,)).fetchone()) == ("Other animals", "pet", 1)


def test_the_pets_pass_keeps_what_it_did_and_tries_again(env, monkeypatch):
    """A photo Vision fails on waits for the next update; a stop part way keeps the batches saved; a
    photo whose thumbnail changes is looked at again and keeps its pet's name."""
    from joeylab import pets, search

    conn, sids = _library(env, n=6)
    _thumbs(conn)
    vecs = [_e(0)] * 20
    found = {sid: [DOG()] for sid in sids}
    model, seen = _stand_in(monkeypatch, found, vecs, fail={sids[1]})
    errors = []
    assert pets.run(conn, batch=2, errors=errors) == 5
    assert [sid for sid, _ in errors] == [sids[1]]
    assert [r["id"] for r in pets.pending(conn)] == [sids[1]]
    # The search model stops answering on the next batch: what was saved stays.
    _stand_in(monkeypatch, found, vecs)
    conn.execute("DELETE FROM animals")
    conn.commit()
    calls = []

    def stuck(images):
        calls.append(1)
        if len(calls) > 1:
            raise search.GpuStuck("The search model didn't finish in 1 s")
        return np.stack([_e(0)] * len(images))
    monkeypatch.setattr(search.Model, "get", classmethod(lambda cls: type("M", (), {"embed_images": staticmethod(stuck)})()))
    with pytest.raises(search.GpuStuck):
        pets.run(conn, batch=2)
    assert conn.execute("SELECT COUNT(*) FROM animals").fetchone()[0] == 2
    assert len(pets.pending(conn)) == 4

    # A named pet whose photo's thumbnail changed is found again in the same place, with its name.
    _stand_in(monkeypatch, found, [_e(0)] * 20)
    pets.run(conn)
    fid, sid = conn.execute("SELECT id, shot_id FROM faces WHERE model=? ORDER BY id LIMIT 1", (pets.MODEL,)).fetchone()
    pid = conn.execute("INSERT INTO people(name, kind) VALUES ('Joey', 'pet')").lastrowid
    conn.execute("UPDATE faces SET person_id=?, person_src='user' WHERE id=?", (pid, fid))
    conn.execute("UPDATE shots SET analyzed_fp = analyzed_fp || 'x' WHERE id=?", (sid,))
    conn.commit()
    assert [r["id"] for r in pets.pending(conn)] == [sid]
    pets.run(conn)
    new = conn.execute("SELECT id, person_id, person_src FROM faces WHERE shot_id=? AND model=?", (sid, pets.MODEL)).fetchone()
    assert new[0] != fid and tuple(new[1:]) == (pid, "user")


def test_finding_faces_again_keeps_the_photos_pets(env, monkeypatch):
    from joeylab import faces, pets

    conn, sids = _library(env, n=2)
    sid = sids[0]
    conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, vec, model, kind) VALUES (?, .2, .2, .5, .5, 300, ?, ?, 'pet')",
                 (sid, _e(0).astype(np.float16).tobytes(), pets.MODEL))
    conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, vec) VALUES (?, .6, .1, .1, .1, 80, ?)",
                 (sid, np.ones(512, dtype=np.float16).tobytes()))
    conn.commit()
    monkeypatch.setattr(faces, "_init_worker", lambda quiet=True, key="insightface": None)
    monkeypatch.setattr(faces, "available", lambda key=None: True)
    monkeypatch.setattr(faces, "analyze_one", lambda t: {"shot_id": t["shot_id"], "stamp": t["stamp"], "faces": [], "error": None})
    faces.run(conn, force=True)
    kinds = [r[0] for r in conn.execute("SELECT model FROM faces WHERE shot_id=?", (sid,))]
    assert kinds == [pets.MODEL]                                  # the person's face went; the pet stayed


def test_the_viewer_lists_a_photos_pets(env, monkeypatch):
    from joeylab import pets
    from joeylab.web.server import create_app

    conn, sids, _ = _joey_world(env, monkeypatch)
    pets.run(conn)
    pets.cluster(conn)
    c = TestClient(create_app())
    d = c.get(f"/api/p/t/shot/{sids[0]}").json()
    assert [(f["pet"], f["group_faces"]) for f in d["faces"]] == [(True, 3)]
    assert d["faces"][0]["group_clear"]


# ---------------------------------------------------------------- dogs' faces taken for people's

def _face(conn, sid, x, y, vec, cluster=None, person=None, src=None, closed=0, w=0.1):
    from joeylab import db

    fid = db.new_id(conn, "faces")
    conn.execute("INSERT INTO faces(id, shot_id, x, y, w, h, px, area, sharp, closed, smile, vec, cluster, person_id, person_src)"
                 " VALUES (?,?,?,?,?,?,120,?,300,?,0.2,?,?,?,?)",
                 (fid, sid, x, y, w, w, w * w, closed, vec.astype(np.float16).tobytes(), cluster, person, src))
    return fid


def test_a_dogs_face_leaves_people_and_can_be_put_back(env, monkeypatch):
    import json

    from joeylab import faces, pets
    from joeylab.web.server import create_app

    conn, sids = _library(env, n=3)
    a, b, c3 = sids
    rng = np.random.default_rng(1)
    person, dog = rng.normal(size=512), rng.normal(size=512)
    near = lambda base: base + rng.normal(scale=0.1, size=512)       # noqa: E731
    # Photo a: a dog's face (eyes "closed") in the dog's box, a person beside it, and a face you named in the box.
    dogface = _face(conn, a, .30, .30, near(dog), cluster=5, closed=1)
    beside = _face(conn, a, .70, .20, near(person), cluster=6)
    named = _face(conn, a, .45, .45, near(dog), cluster=5, person=conn.execute(
        "INSERT INTO people(name, kind) VALUES ('Joey', 'pet')").lastrowid, src="user")
    # Photo b: a face in a dog's box, but someone is there (a child hugging the dog): left alone.
    hugged = _face(conn, b, .30, .30, near(person), cluster=6)
    _face(conn, c3, .05, .80, near(person), cluster=6)
    conn.execute("UPDATE metrics SET faces=3, main_faces=3, eyes_closed=1 WHERE shot_id=?", (a,))
    for sid, humans in ((a, [[.65, .10, .30, .80]]), (b, [[.25, .25, .30, .50]])):
        conn.execute("INSERT INTO animals(shot_id, src, found, humans) VALUES (?, 'x', ?, ?)",
                     (sid, json.dumps([["dog", .8, .20, .20, .40, .40]]), json.dumps(humans)))
    conn.commit()

    assert pets.animal_faces(conn) == 1
    row = conn.execute("SELECT kind, cluster, person_id FROM faces WHERE id=?", (dogface,)).fetchone()
    assert tuple(row) == ("pet", None, None)
    assert [r[0] for r in conn.execute("SELECT kind FROM faces WHERE id IN (?, ?, ?)", (beside, named, hugged))] == [None] * 3
    m = conn.execute("SELECT faces, eyes_closed FROM metrics WHERE shot_id=?", (a,)).fetchone()
    assert tuple(m) == (2, 0)                                      # a dog's shut eyes aren't a blink
    assert pets.animal_faces(conn) == 0                            # (nothing more to set aside)
    faces.cluster(conn, full=True)
    assert conn.execute("SELECT cluster FROM faces WHERE id=?", (dogface,)).fetchone()[0] is None

    cl = TestClient(create_app())
    d = cl.get("/api/p/t/people").json()
    assert d["animal_faces"] == 1 and d["faces"] == 4
    assert [f["id"] for f in cl.get("/api/p/t/people/animal-faces").json()["faces"]] == [dogface]
    assert dogface not in [f["id"] for f in cl.get(f"/api/p/t/shot/{a}").json()["faces"]]

    # Put back: a person's face for good (never set aside again), in the group it fits.
    assert cl.post("/api/p/t/faces/kind", json={"faces": [dogface, named], "kind": "person"}).json()["changed"] == 1
    _settled(cl)
    assert conn.execute("SELECT kind FROM faces WHERE id=?", (dogface,)).fetchone()[0] == "person"
    assert conn.execute("SELECT cluster FROM faces WHERE id=?", (dogface,)).fetchone()[0] is not None
    assert conn.execute("SELECT eyes_closed FROM metrics WHERE shot_id=?", (a,)).fetchone()[0] == 1
    assert pets.animal_faces(conn) == 0
    # Undo of putting it back.
    cl.post("/api/p/t/faces/kind", json={"faces": [dogface], "kind": "pet"})
    _settled(cl)
    assert tuple(conn.execute("SELECT kind, cluster FROM faces WHERE id=?", (dogface,)).fetchone()) == ("pet", None)


def test_a_face_put_back_keeps_your_word_when_found_again(env, monkeypatch):
    from joeylab import faces

    conn, sids = _library(env, n=2)
    sid = sids[0]
    fid = _face(conn, sid, .30, .30, np.ones(512))
    conn.execute("UPDATE faces SET kind='person' WHERE id=?", (fid,))
    conn.commit()
    again = {"x": .31, "y": .30, "w": .1, "h": .1, "px": 120, "area": .01, "sharp": 300, "closed": 0, "smile": .2,
             "vec": np.ones(512, dtype=np.float16).tobytes(), "crop": None}
    monkeypatch.setattr(faces, "_init_worker", lambda quiet=True, key="insightface": None)
    monkeypatch.setattr(faces, "analyze_one", lambda t: {"shot_id": t["shot_id"], "stamp": t["stamp"],
                                                         "faces": [dict(again)] if t["shot_id"] == sid else [], "error": None})
    faces.run(conn, force=True)
    new = conn.execute("SELECT id, kind FROM faces WHERE shot_id=?", (sid,)).fetchone()
    assert new[0] != fid and new[1] == "person"
