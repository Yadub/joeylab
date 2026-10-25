"""Two sets of face models: their vectors (512 and 128 numbers) are never compared with each other,
and switching finds every face again while keeping names, other names, "me", "not the same person"
answers and hidden people. The models are stood in for: the real ones aren't downloaded in tests."""

from __future__ import annotations

import os

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import add_root, make_jpeg, run_all

DIM = {"insightface": 512, "opencv": 128}


def _library(lib, n=6):
    for i in range(n):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    return conn, [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]


class Look:
    """Stand-in identities: one unit vector per person and model, each face a little off it."""

    def __init__(self, seed=4):
        self.rng = np.random.default_rng(seed)
        self.base: dict = {}

    def unit(self, v):
        return v / np.linalg.norm(v)

    def of(self, who, model, like=None, alike=0.5):
        if (who, model) not in self.base:
            v = self.unit(self.rng.normal(size=DIM[model]))
            if like:                     # someone who looks somewhat like `like` (below the grouping's bar)
                a = self.of(like, model)
                v = self.unit(alike * a + np.sqrt(1 - alike ** 2) * self.unit(v - (v @ a) * a))
            self.base[(who, model)] = v
        return self.base[(who, model)]

    def face(self, who, model, **kw):
        v = self.of(who, model, **kw) + self.rng.normal(scale=0.03 if model == "opencv" else 0.01, size=DIM[model])
        return self.unit(v).astype(np.float16).tobytes()


def _choose(model):
    from joeylab import faces

    faces.set_enabled(True, via="test", model=model)


def test_vectors_from_two_models_never_meet(env):
    """Halfway through a switch a library has faces from both: grouping, "Is this …?", the same person
    across the years, naming and a burst's faces each use only the chosen models' faces (mixing 512
    and 128 numbers would fail outright)."""
    from joeylab import faces
    from joeylab.web.server import create_app

    _, lib = env
    conn, sid = _library(lib)
    look = Look()
    alex = conn.execute("INSERT INTO people(name) VALUES ('Alex')").lastrowid
    for i, s in enumerate(sid):
        old = i < 3                                       # photos 0-2 still have the old models' faces
        model = "insightface" if old else "opencv"
        conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, area, sharp, closed, vec, model, cluster, person_id, person_src)"
                     " VALUES (?, .1, .2, .1, .15, 120, .02, 300, 0, ?, ?, ?, ?, 'user')",
                     (s, look.face("alex", model), model, 1, alex))
        conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, area, sharp, closed, vec, model, cluster)"
                     " VALUES (?, .6, .2, .1, .15, 120, .02, 300, 0, ?, ?, ?)",
                     (s, look.face("sam", model, like="alex", alike=0.5), model, 7 if old else 8))
    conn.commit()
    c = TestClient(create_app())
    for model in ("opencv", "insightface"):
        _choose(model)
        before = {r[0]: r[1] for r in conn.execute("SELECT id, cluster FROM faces WHERE model != ?", (model,))}
        out = faces.cluster(conn, full=True)
        assert out["faces"] == 6                          # the chosen models' faces only
        assert {r[0]: r[1] for r in conn.execute("SELECT id, cluster FROM faces WHERE model != ?", (model,))} == before
        mine = {r[0] for r in conn.execute("SELECT DISTINCT cluster FROM faces WHERE model = ?", (model,))}
        assert {r[0] for r in conn.execute("SELECT cluster FROM face_groups")} <= mine
        assert c.get("/api/p/t/people/suggestions").status_code == 200
        assert c.get("/api/p/t/people/across-years").status_code == 200
        unnamed = conn.execute("SELECT cluster FROM faces WHERE model = ? AND person_id IS NULL", (model,)).fetchone()[0]
        faces.name_cluster(conn, unnamed, alex)
        conn.execute("UPDATE faces SET person_id = NULL, person_src = NULL WHERE cluster = ? OR person_src = 'auto'", (unnamed,))
        conn.commit()
        burst = c.get("/api/p/t/compare-faces", params={"ids": ",".join(map(str, sid))}).json()
        frames = {int(k) for p in burst["people"] for k in p["faces"]}
        assert frames == set(sid[3:] if model == "opencv" else sid[:3])


def test_a_switch_finds_every_face_again_and_keeps_what_you_said(env, monkeypatch):
    from joeylab import aliases, db, faces, identity
    from joeylab.web.server import create_app

    _, lib = env
    conn, sid = _library(lib)
    look = Look()
    _choose("insightface")
    pid = {}
    for name, hidden in (("Alex", 0), ("Sam", 0), ("Strangers", 1)):
        pid[name] = conn.execute("INSERT INTO people(name, hidden) VALUES (?, ?)", (name, hidden)).lastrowid
    aliases.add(conn, pid["Sam"], ["Sammy"])
    identity.set_name(None, "Alex")
    # Each face: (photo, x, who it is, cluster, person, how named). Photo 1's Sam is one the new
    # models won't find; Noor (group 30) looks a little like Alex, and you said she isn't.
    before = [(0, .1, "alex", 10, pid["Alex"], "user"), (1, .1, "alex", 10, pid["Alex"], "user"),
              (2, .1, "alex", 10, pid["Alex"], "user"), (0, .6, "sam", 20, pid["Sam"], "user"),
              (1, .6, "sam", 20, pid["Sam"], "user"), (3, .7, "sam", 20, pid["Sam"], "auto"),
              (2, .6, "stranger", 40, pid["Strangers"], "user"), (3, .4, "noor", 30, None, None),
              (4, .4, "noor", 30, None, None), (5, .4, "noor", 30, None, None), (5, .1, "alex", 50, -1, "user")]
    for n, x, who, cl, p, src in before:
        conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, area, sharp, closed, vec, model, cluster, person_id, person_src)"
                     " VALUES (?, ?, .2, .1, .15, 120, .02, 300, 0, ?, 'insightface', ?, ?, ?)",
                     (sid[n], x, look.face(who, "insightface", **({"like": "alex"} if who == "noor" else {})), cl, p, src))
    conn.execute("INSERT INTO face_not_same(person_id, cluster, ts) VALUES (?, 30, 0)", (pid["Alex"],))
    for s in sid:
        conn.execute("UPDATE shots SET faces_fp = analyzed_fp || ':' || ? WHERE id = ?", (faces.FACES_VERSION, s))
    db.set_setting(conn, "faces_model", "insightface")
    conn.commit()
    assert not faces.pending(conn) and faces.switch_progress(conn) is None

    # Switch. Every photo's faces are due again, and progress says how far the switch is.
    _choose("opencv")
    assert len(faces.pending(conn)) == len(sid)
    assert faces.switch_progress(conn) == {"from": "insightface", "to": "opencv", "done": 0, "total": len(sid)}

    # The stand-in models find the same faces a little bigger and off-centre (another detector's
    # boxes), all but photo 1's Sam.
    found = {}
    for n, x, who, *_ in before:
        if (n, x) != (1, .6):
            found.setdefault(sid[n], []).append((x, who))

    def analyze_one(task):
        out = [{"x": x - .01, "y": .19, "w": .12, "h": .17, "px": 140, "area": .02, "sharp": 300.0, "closed": 0, "smile": .2,
                "vec": look.face(who, "opencv", **({"like": "alex"} if who == "noor" else {})), "crop": None}
               for x, who in found.get(task["shot_id"], [])]
        return {"shot_id": task["shot_id"], "stamp": task["stamp"], "faces": out, "error": None}

    monkeypatch.setattr(faces, "analyze_one", analyze_one)
    monkeypatch.setattr(faces, "_init_worker", lambda quiet=True, key="insightface": None)
    monkeypatch.setattr(faces, "SMALL_BATCH", 100)
    done, errors = faces.run(conn)
    assert done == len(sid) and not errors and not faces.pending(conn)
    faces.cluster(conn)

    rows = conn.execute("SELECT * FROM faces").fetchall()
    assert len(rows) == len(before) - 1 and {r["model"] for r in rows} == {"opencv"}
    assert {len(r["vec"]) for r in rows} == {128 * 2}
    at = {(sid.index(r["shot_id"]), round(r["x"] + .01, 2)): r for r in rows}
    # Your names stay on the faces in the same place (photo 1's Sam wasn't found again).
    for n, x, _, _, p, src in before:
        if src == "user" and (n, x) != (1, .6):
            assert (at[(n, x)]["person_id"], at[(n, x)]["person_src"]) == (p, "user"), (n, x)
    assert at[(5, .1)]["person_id"] == -1                                     # "not this person" too
    # People, other names, hidden people and "me" are all still there.
    assert {r["name"]: r["hidden"] for r in conn.execute("SELECT name, hidden FROM people")} == \
        {"Alex": 0, "Sam": 0, "Strangers": 1}
    assert aliases.of(conn, pid["Sam"]) == ["Sammy"] and identity.person_id(conn, None) == pid["Alex"]
    # Groups keep their ids, so "not the same person" still points at Noor's group: never suggested as Alex.
    assert {at[(n, .4)]["cluster"] for n in (3, 4, 5)} == {30}
    assert conn.execute("SELECT COUNT(*) FROM face_not_same WHERE person_id = ? AND cluster = 30", (pid["Alex"],)).fetchone()[0]
    c = TestClient(create_app())
    assert not [s for s in c.get("/api/p/t/people/suggestions").json()["suggestions"] if s["cluster"] == 30]
    conn.execute("DELETE FROM face_not_same")
    conn.commit()
    assert [s["name"] for s in c.get("/api/p/t/people/suggestions").json()["suggestions"] if s["cluster"] == 30] == ["Alex"]
    # The switch is finished: grouped with the new models, nothing left to do.
    assert db.get_setting(conn, "faces_model") == "opencv" and faces.switch_progress(conn) is None
    people = c.get("/api/p/t/people").json()
    assert people["model"] == "opencv" and people["switch"] is None
    assert [x["name"] for x in people["people"]][0] == "Alex"                 # you first
    assert [x["name"] for x in people["hidden"]] == ["Strangers"]


def test_a_face_found_again_with_the_same_models_keeps_only_your_naming(env):
    """Re-detecting with the same models (a photo edited, say) is as before: a name you gave stays
    with the face in the same place; the grouping places the faces afresh."""
    from joeylab import faces

    old = [{"id": 1, "x": .1, "y": .2, "w": .1, "h": .15, "cluster": 5, "person_id": 3, "person_src": "user", "model": "insightface"},
           {"id": 2, "x": .5, "y": .2, "w": .1, "h": .15, "cluster": 6, "person_id": 4, "person_src": "auto", "model": "insightface"},
           {"id": 3, "x": .52, "y": .25, "w": .1, "h": .15, "cluster": 7, "person_id": -1, "person_src": "user", "model": "insightface"}]
    new = [{"x": .51, "y": .24, "w": .1, "h": .15}, {"x": .11, "y": .21, "w": .1, "h": .15}, {"x": .8, "y": .2, "w": .1, "h": .15}]
    assert faces._carry(old, new, "insightface") == [(None, -1, "user"), (None, 3, "user"), (None, None, None)]
    # With other models: the group and the grouping's person come along as well.
    assert faces._carry(old, new, "opencv") == [(7, -1, "user"), (5, 3, "user"), (None, None, None)]
    assert faces._carry(old, [{"x": .49, "y": .2, "w": .1, "h": .15}], "opencv") == [(6, 4, "auto")]


TEST_MODELS = os.environ.get("JOEYLAB_TEST_MODELS")     # a folder holding the OpenCV face models too


@pytest.mark.skipif(not (TEST_MODELS and os.path.isfile(os.path.join(TEST_MODELS, "face_recognition_sface_2021dec.onnx"))),
                    reason="set JOEYLAB_TEST_MODELS to a folder with the face models to run this")
def test_the_opencv_models_run(env, monkeypatch):
    from pathlib import Path

    from joeylab import faces

    models = Path(TEST_MODELS)
    monkeypatch.setattr(faces, "YUNET_PATH", models / "face_detection_yunet_2023mar.onnx")
    monkeypatch.setattr(faces, "SFACE_PATH", models / "face_recognition_sface_2021dec.onnx")
    monkeypatch.setattr(faces, "FACE_MODEL", models / "face_landmarker.task")
    saved = os.dup(2)                 # the worker initialiser silences stderr; restore it after
    try:
        faces._init_worker(key="opencv")
    finally:
        os.dup2(saved, 2)
    try:
        boxes, kps, _ = faces.detect(np.full((1200, 1800, 3), 200, dtype=np.uint8))
        assert boxes.shape == (0, 4) and kps.shape == (0, 5, 2)   # a blank image has no faces
        rng = np.random.default_rng(0)
        v = faces.embed(rng.integers(0, 255, (300, 300, 3), dtype=np.uint8),
                        faces.ARC_TEMPLATE + np.array([90, 90], dtype=np.float32))
        assert v.shape == (128,) and abs(float(np.linalg.norm(v)) - 1) < 1e-3
    finally:
        faces._backend = "insightface"
        if faces._lm is not None:      # MediaPipe crashes if left for interpreter shutdown
            faces._lm.close()
            faces._lm = None
