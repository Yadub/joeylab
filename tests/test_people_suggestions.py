"""The "Is this …?" card: a No is remembered, and every answer can be undone."""

from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient

from conftest import add_root, make_jpeg, run_all


def _faces(conn, lib):
    rng = np.random.default_rng(3)
    base = rng.normal(size=512)
    near = lambda: (base + rng.normal(scale=0.15, size=512)).astype(np.float16).tobytes()
    sids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    pid = conn.execute("INSERT INTO people(name) VALUES ('Deepa')").lastrowid
    for n in sids[:3]:                                                   # named photos of her
        conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, area, sharp, closed, vec, cluster, person_id, person_src)"
                     " VALUES (?, .6, .3, .1, .15, 120, .02, 300, 0, ?, 1, ?, 'user')", (n, near(), pid))
    for n in sids[3:6]:                                                  # an unnamed group that looks like her
        conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, area, sharp, closed, vec, cluster)"
                     " VALUES (?, .2, .3, .1, .15, 120, .02, 300, 0, ?, 7)", (n, near()))
    conn.commit()
    return pid


def _settled(c):
    """Wait for the background regrouping a change starts (faces are regrouped off the request)."""
    import time

    for _ in range(200):
        if not c.app.state.bg.running:
            return
        time.sleep(0.02)


def test_no_is_remembered_and_answers_can_be_undone(env):
    from joeylab.web.server import create_app

    _, lib = env
    for i in range(6):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    pid = _faces(conn, lib)
    c = TestClient(create_app())
    s = c.get("/api/p/t/people/suggestions").json()["suggestions"]
    assert [(x["cluster"], x["name"]) for x in s] == [(7, "Deepa")]
    assert len(s[0]["sample"]) == 3 and s[0]["person_faces"]

    # No: remembered, so it isn't asked again; undoing the No brings it back.
    assert c.post(f"/api/p/t/people/{pid}/not-same", json={"cluster": 7}).status_code == 200
    assert c.get("/api/p/t/people/suggestions").json()["suggestions"] == []
    c.post(f"/api/p/t/people/{pid}/not-same", json={"cluster": 7, "undo": True})
    assert len(c.get("/api/p/t/people/suggestions").json()["suggestions"]) == 1

    # Yes, then Undo: the group's faces are unnamed again. (Naming regroups faces in the background,
    # which may fold the group into hers: the faces are followed by id, once that's done.)
    hers = [r[0] for r in conn.execute("SELECT id FROM faces WHERE cluster=1")]
    r = c.post("/api/p/t/people/name", json={"cluster": 7, "name": "Deepa"}).json()
    assert r["id"] == pid and len(r["assigned"]) == 3
    _settled(c)
    named = lambda ids, who="= ?": conn.execute(  # noqa: E731
        f"SELECT COUNT(*) FROM faces WHERE id IN ({','.join(map(str, ids))}) AND person_id {who}", (pid,) if "?" in who else ()).fetchone()[0]
    assert named(r["assigned"]) == 3
    u = c.post(f"/api/p/t/people/{pid}/unname", json={"faces": r["assigned"]}).json()
    assert u["unnamed"] == 3
    _settled(c)
    assert named(r["assigned"], "IS NOT NULL") == 0
    assert named(hers) == 3                                              # hers stay


# ---------------------------------------------------------------- big libraries: vectors kept, numbers the same

def _library(env):
    _, lib = env
    for i in range(6):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    _faces(conn, lib)
    return conn


def test_face_vectors_are_kept_and_follow_new_and_removed_faces(env):
    from joeylab import faces

    conn = _library(env)
    ids, vecs = faces.vectors(conn)
    assert len(ids) == 6 and vecs.dtype == np.float16 and list(ids) == sorted(ids)
    assert faces.vectors(conn)[1] is vecs                                  # kept: not read again
    sid = conn.execute("SELECT id FROM shots ORDER BY id").fetchone()[0]
    v = np.ones(512, dtype=np.float16) / np.float16(np.sqrt(512))
    conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, vec, cluster) VALUES (?, .1, .1, .1, .1, 99, ?, 9)", (sid, v.tobytes()))
    conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, vec, model) VALUES (?, .2, .1, .1, .1, 99, ?, 'opencv')",
                 (sid, np.ones(128, dtype=np.float16).tobytes()))                 # another model's: never mixed in
    gone = int(ids[0])
    conn.execute("DELETE FROM faces WHERE id=?", (gone,))
    conn.commit()
    ids2, vecs2 = faces.vectors(conn)
    assert len(ids2) == 6 and gone not in ids2 and np.array_equal(vecs2[-1], v)
    want = {r[0]: np.frombuffer(r[1], dtype=np.float16) for r in conn.execute(
        "SELECT id, vec FROM faces WHERE model='insightface' AND vec IS NOT NULL")}
    assert all(np.array_equal(vecs2[i], want[int(f)]) for i, f in enumerate(ids2))
    assert np.allclose(faces.inverse_norms(vecs2), 1 / np.linalg.norm(vecs2.astype(np.float32), axis=1), rtol=1e-5)


def test_centres_and_spans_are_what_one_by_one_gives():
    from joeylab import faces

    rng = np.random.default_rng(1)
    vecs = rng.normal(size=(500, 16)).astype(np.float16)
    keys, rows = rng.integers(0, 40, 300), rng.permutation(500)[:300]
    k, c = faces.unit_centres(keys, rows, vecs, chunk=7)
    for i, key in enumerate(k):
        s = vecs[rows[keys == key]].astype(np.float32).sum(axis=0)
        assert np.allclose(c[i], s / np.linalg.norm(s), atol=1e-5)
    # Each unit's span of years, all at once, exactly as numpy gives it one unit at a time.
    t = np.sort(rng.uniform(40, 56, 200))
    n = np.array([1, 2, 3, 7, 50, 137])
    starts = np.r_[0, np.cumsum(n)[:-1]]
    for p in (5, 95):
        got = faces._percentile(t, starts, n, p)
        assert all(got[i] == np.percentile(t[s : s + m], p) for i, (s, m) in enumerate(zip(starts, n)))


def test_lists_of_names_skip_the_groups(env):
    from joeylab.web.server import create_app

    _library(env)
    c = TestClient(create_app())
    full = c.get("/api/p/t/people", params={"min_photos": 1}).json()
    names = c.get("/api/p/t/people", params={"limit": 0}).json()
    assert [x["name"] for x in names["people"]] == ["Deepa"] and names["people"] == full["people"]
    assert names["groups"] == [] and [g["cluster"] for g in full["groups"]] == [7]
    assert full["people"][0]["photos"] == 3 and full["people"][0]["cover"]
