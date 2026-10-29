"""How well each face is caught (Apple Vision's face capture quality): judged once per face, in a pass of
its own, and used in scoring's faces part and Cull's face compare without ever deciding alone. The test
photos hold no faces, so Apple Vision's answers are stood in."""

from __future__ import annotations

import threading

import numpy as np
from fastapi.testclient import TestClient

from conftest import add_root, make_jpeg, run_all


def _library(env, n=3):
    _, lib = env
    for i in range(n):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    return conn, [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]


def _face(conn, sid, x, area, closed=0, quality=None, vec=None, model="insightface", kind=None):
    from joeylab import db

    fid = db.new_id(conn, "faces")
    w = area ** 0.5
    conn.execute("INSERT INTO faces(id, shot_id, x, y, w, h, px, area, sharp, closed, smile, vec, model, kind, quality)"
                 " VALUES (?,?,?,0.2,?,?,120,?,300,?,0.3,?,?,?,?)",
                 (fid, sid, x, w, w, area, closed, (vec if vec is not None else np.ones(512)).astype(np.float16).tobytes(),
                  model, kind, quality))
    return fid


def test_each_face_is_judged_once_from_its_crop(env, monkeypatch):
    from joeylab import config, faces, pets

    conn, sids = _library(env)
    big = _face(conn, sids[0], .1, .04)
    small = _face(conn, sids[0], .6, .01)
    tiny = _face(conn, sids[1], .3, .001)
    pet = _face(conn, sids[1], .5, .2, vec=np.ones(768), model=pets.MODEL, kind="pet", quality=-1)
    conn.commit()
    # The big face has a crop on disk (as the faces step writes them); the others are judged on the thumbnail.
    from joeylab.library import version_tag

    fp = conn.execute("SELECT analyzed_fp FROM shots WHERE id=?", (sids[0],)).fetchone()[0]
    crop = config.project_dir("t") / "previews" / "faces" / f"{big % 100:02d}" / f"{big}_{version_tag(fp)}.jpg"
    crop.parent.mkdir(parents=True, exist_ok=True)
    make_jpeg(crop, seed=9, size=(200, 200))
    asked = []

    def vision(items):
        asked.extend(items)
        return [0.55 if "/previews/faces/" in path else 0.15 for path, _ in items]

    monkeypatch.setattr(faces, "vision_quality", vision)
    assert {r["id"] for r in faces.quality_pending(conn)} == {big, small, tiny}         # never a pet
    assert faces.measure_quality(conn) == 3
    q = dict(conn.execute("SELECT id, quality FROM faces"))
    assert (q[big], q[small], q[tiny], q[pet]) == (0.55, 0.15, 0.15, -1)
    by_path = {p.rsplit("/", 1)[-1]: box for p, box in asked}
    x, y, w, h = by_path[crop.name]                     # the face's box in its crop: centred, 1/1.6 of its longer side
    assert abs(x + w / 2 - 0.5) < 1e-6 and abs(max(w, h) - 1 / 1.6) < 1e-6
    # The photo's quality: its main faces', the bigger counting for more (the tiny one isn't a main face).
    fq = dict(conn.execute("SELECT shot_id, face_quality FROM metrics"))
    assert abs(fq[sids[0]] - (0.55 * .04 + 0.15 * .01) / .05) < 1e-6 and fq[sids[1]] is None
    assert faces.quality_pending(conn) == [] and faces.measure_quality(conn) == 0       # judged once


def test_apple_vision_not_answering_leaves_the_rest_for_the_next_update(env, monkeypatch):
    from joeylab import config, faces

    conn, sids = _library(env)
    for sid in sids:
        _face(conn, sid, .1, .04)
    conn.commit()
    monkeypatch.setattr(config, "VISION_TIMEOUT_S", 0.3)
    release = threading.Event()
    calls = []

    def vision(items):
        calls.append(1)
        if len(calls) == 2:
            release.wait(10)
        return [0.4] * len(items)

    monkeypatch.setattr(faces, "vision_quality", vision)
    errors = []
    try:
        n = faces.measure_quality(conn, batch=1, errors=errors)
    finally:
        release.set()
    assert n == 1 and len(errors) == 1
    assert len(faces.quality_pending(conn)) == 2


def test_face_quality_counts_in_the_score_but_never_alone():
    from joeylab import scoring

    base = {"sharp_subject": 800, "face_sharp": 120, "main_faces": 2, "face_area": 0.05, "brightness": 120,
            "clip_high": 0, "clip_low": 0, "eyes_closed": 0, "smile": 0.3, "aesthetic": 5.4}
    plain = scoring.components(base)["faces"]
    well = scoring.components({**base, "face_quality": 0.55})["faces"]
    turned = scoring.components({**base, "face_quality": 0.15})["faces"]
    assert turned < plain < well or turned < well
    blink_well = scoring.components({**base, "eyes_closed": 1, "face_quality": 0.55})["faces"]
    assert blink_well < turned                            # closed eyes weigh more than a turned head
    assert well - turned <= scoring.CAUGHT_SHARE + 1e-9
    assert scoring.caught(-1) is None and scoring.caught(None) is None


def test_culls_face_compare_weighs_face_quality_after_eyes(env):
    from joeylab.web.server import create_app

    conn, sids = _library(env)
    v = np.random.default_rng(0).normal(size=512)
    _face(conn, sids[0], .1, .04, quality=0.2, vec=v)
    _face(conn, sids[1], .1, .04, quality=0.5, vec=v)
    _face(conn, sids[2], .1, .04, quality=0.5, closed=1, vec=v)
    conn.commit()
    d = TestClient(create_app()).get("/api/p/t/compare-faces", params={"ids": ",".join(map(str, sids))}).json()
    s = d["score"]
    assert s[str(sids[1])]["look"] > s[str(sids[0])]["look"]
    assert s[str(sids[2])]["closed"] == 1
    assert abs(d["people"][0]["faces"][str(sids[1])]["caught"] - 0.875) < 0.01
