"""Face grouping on big libraries: what you say about a face while the faces are being regrouped
stands; the regrouping chooses the same groups without sorting every group for every face; and
when nothing is new, nothing is read again."""

from __future__ import annotations

import time

import numpy as np
from fastapi.testclient import TestClient


def _photos(conn, n: int) -> list[int]:
    return [conn.execute("INSERT INTO shots(dir, stem) VALUES ('/lib', ?)", (f"IMG_{i}",)).lastrowid for i in range(n)]


def _face(conn, shot: int, vec: np.ndarray, x: float = .1, px: int = 120, sharp: float = 300.0, **cols) -> int:
    v = (vec / np.linalg.norm(vec)).astype(np.float16).tobytes()
    names = ["shot_id", "x", "y", "w", "h", "px", "area", "sharp", "closed", "vec", *cols]
    return conn.execute(f"INSERT INTO faces({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                        (shot, x, .1, .1, .1, px, .01, sharp, 0, v, *cols.values())).lastrowid


def _people(conn, rng, shots, who: dict[str, tuple], noise=0.03) -> dict[str, list[int]]:
    """Faces of a few people, {name: (from photo, up to photo, where across)}. Returns each one's face ids."""
    out = {}
    for name, (a, b, x) in who.items():
        base = rng.normal(size=512)
        out[name] = [_face(conn, s, base + rng.normal(scale=noise * np.linalg.norm(base) / 22, size=512), x=x)
                     for s in shots[a:b]]
    conn.commit()
    return out


def _vec(conn, fid: int) -> np.ndarray:
    return np.frombuffer(conn.execute("SELECT vec FROM faces WHERE id=?", (fid,)).fetchone()[0], dtype=np.float16).astype(np.float32)


def _who(conn) -> dict[int, tuple]:
    return {r[0]: (r[1], r[2], r[3]) for r in conn.execute("SELECT id, cluster, person_id, person_src FROM faces")}


def _while_regrouping(conn, monkeypatch, do):
    """Run do() between the regrouping's reading of every face and its writing of them (the minutes a
    complete regrouping takes on a big library). Each face is put in a group of its own first, so the
    regrouping moves all but the first face of each group, and writes them all again."""
    from joeylab import faces

    conn.execute("UPDATE faces SET cluster = 1000 + id")
    conn.commit()
    write = faces._write_groups

    def late(conn, updates, was):
        do()
        return write(conn, updates, was)
    monkeypatch.setattr(faces, "_write_groups", late)


# ---------------------------------------------------------------- what you say meanwhile stands

def test_names_given_while_faces_are_regrouped_stand(env, monkeypatch):
    from joeylab import db, faces, locks
    from joeylab.web.server import create_app

    conn = db.connect("t")
    shots = _photos(conn, 12)
    f = _people(conn, np.random.default_rng(1), shots, {"sam": (0, 6, .1), "meera": (6, 12, .1), "ali": (0, 6, .6)})
    sam = conn.execute("INSERT INTO people(name) VALUES ('Sam')").lastrowid
    conn.executemany("UPDATE faces SET person_id=?, person_src='user' WHERE id=?", [(sam, i) for i in f["sam"][:2]])
    conn.commit()
    faces.cluster(conn, full=True)
    assert {_who(conn)[i][1:] for i in f["sam"][2:]} == {(sam, "auto")}
    c = TestClient(create_app())
    said = {}

    def meanwhile():
        # Name one of Meera's faces (the others join her, being clearly her), say a face isn't Sam, and
        # take Sam's name off another.
        group = _who(conn)[f["meera"][0]][0]
        said["meera"] = c.post("/api/p/t/people/name", json={"cluster": group, "name": "Meera"}).json()["id"]
        assert c.post(f"/api/p/t/faces/{f['sam'][3]}/unassign").status_code == 200
        assert c.post(f"/api/p/t/people/{sam}/unname", json={"faces": [f["sam"][1]]}).json()["unnamed"] == 1

    _while_regrouping(conn, monkeypatch, meanwhile)
    with locks.project_lock("t"):           # as an update holds it: the regroupings these start wait
        faces.cluster(conn, full=True)
    now = _who(conn)
    assert now[f["meera"][0]][1:] == (said["meera"], "user")
    assert {now[i][1:] for i in f["meera"][1:]} == {(said["meera"], "auto")}
    assert now[f["sam"][3]][1:] == (-1, "user")
    assert now[f["sam"][1]][1:] == (None, None)
    assert now[f["sam"][0]][1:] == (sam, "user")
    assert {now[i][1:] for i in f["sam"][2:3] + f["sam"][4:]} == {(sam, "auto")}
    assert {now[i][1:] for i in f["ali"]} == {(None, None)}
    assert len({now[i][0] for i in f["meera"]}) == 1 and len({now[i][0] for i in f["ali"]}) == 1    # regrouped


def test_a_person_merged_or_forgotten_while_faces_are_regrouped_gets_no_faces(env, monkeypatch):
    from joeylab import db, faces, locks
    from joeylab.web.server import create_app

    conn = db.connect("t")
    shots = _photos(conn, 12)
    rng = np.random.default_rng(2)
    f = _people(conn, rng, shots, {"sam": (0, 6, .1), "meera": (6, 12, .1)})
    sam, meera, sam2 = (conn.execute("INSERT INTO people(name) VALUES (?)", (n,)).lastrowid for n in ("Sam", "Meera", "S"))
    conn.executemany("UPDATE faces SET person_id=?, person_src='user' WHERE id=?",
                     [(sam, f["sam"][0]), (sam, f["sam"][1]), (meera, f["meera"][0])])
    conn.commit()
    faces.cluster(conn, full=True)
    # New faces of each, not yet in a group: the regrouping gives them to Sam and to Meera.
    late = {n: _face(conn, shots[k], _vec(conn, f[n][2]) + rng.normal(scale=.002, size=512), x=.4)
            for n, k in (("sam", 7), ("meera", 1))}
    conn.commit()
    c = TestClient(create_app())

    def meanwhile():
        assert c.post(f"/api/p/t/people/{sam2}/merge", json={"other": sam}).json()["merged"]       # Sam is R
        assert c.delete(f"/api/p/t/people/{meera}").json()["ok"]                                    # forget Meera

    _while_regrouping(conn, monkeypatch, meanwhile)
    with locks.project_lock("t"):
        faces.cluster(conn, full=True)
    now = _who(conn)
    assert not [i for i, w in now.items() if w[1] in (sam, meera)]
    assert {now[i][1:] for i in f["sam"]} == {(sam2, "user"), (sam2, "auto")}
    assert now[late["sam"]][1:] == (None, None) and now[late["meera"]][1:] == (None, None)
    assert {now[i][1:] for i in f["meera"]} == {(None, None)}


# ---------------------------------------------------------------- the same groups, sorting less

def _best_by_sorting(sims, bar, k=8):
    """What the grouping did before: sort them all, take the first k down to the bar."""
    out = []
    for c in np.argsort(-sims)[:k]:
        if sims[c] < bar:
            break
        out.append(int(c))
    return np.array(out, dtype=np.int64)


def test_the_best_matches_are_those_sorting_them_all_gives():
    from joeylab import faces

    rng = np.random.default_rng(4)
    for n in (0, 1, 3, 8, 9, 50, 4000):
        for bar in (-2.0, 0.0, 0.5, 0.95, 2.0):
            sims = rng.uniform(-1, 1, n).astype(np.float32)
            assert faces._best(sims, bar).tolist() == _best_by_sorting(sims, bar).tolist(), (n, bar)


def _crowd(conn, rng, shots, people=14, noise=0.1, look=None):
    """Look-alikes side by side in every photo (a family at every event: many groups for a face to
    choose from, more than eight above the bar), some named, someone you said isn't one of them, and
    small faces. Returns how each one looks, for more photos of them."""
    if look is None:
        base = rng.normal(size=512)
        look = [base + rng.normal(scale=0.6 * np.linalg.norm(base) / 22, size=512) for _ in range(people)]
    conn.executemany("INSERT OR IGNORE INTO people(name) VALUES (?)", [(f"P{i}",) for i in range(3)])
    pid = [conn.execute("SELECT id FROM people WHERE name=?", (f"P{i}",)).fetchone()[0] for i in range(3)]
    for k, s in enumerate(shots):
        for i, v in enumerate(look):
            if rng.random() < 0.8:
                named = {"person_id": pid[i], "person_src": "user"} if i < 3 and k % 4 == 0 else {}
                if i == 5 and k % 7 == 3:
                    named = {"person_id": -1, "person_src": "user"}
                _face(conn, s, v + rng.normal(scale=noise * np.linalg.norm(v) / 22, size=512), x=i / len(look),
                      px=int(rng.integers(30, 200)), sharp=float(rng.choice([150.0, 600.0, rng.uniform(50, 900)])), **named)
    conn.commit()
    return look


def _state(conn):
    from joeylab import db

    return ([tuple(r) for r in conn.execute("SELECT id, cluster, person_id, person_src FROM faces ORDER BY id")],
            {k: db.get_setting(conn, k) for k in ("hwm_clusters", "faces_seen_upto", "faces_full_upto")})


def _put_back(conn, state):
    from joeylab import db

    conn.executemany("UPDATE faces SET cluster=?, person_id=?, person_src=? WHERE id=?",
                     [(c, p, s, i) for i, c, p, s in state[0]])
    for k, v in state[1].items():
        if v is None:
            conn.execute("DELETE FROM settings WHERE key=?", (k,))
        else:
            db.set_setting(conn, k, v)
    conn.commit()


def test_regrouping_chooses_the_same_groups_as_sorting_them_all(env, monkeypatch):
    from joeylab import db, faces

    conn = db.connect("t")
    rng = np.random.default_rng(6)
    shots = _photos(conn, 40)
    look = _crowd(conn, rng, shots[:34])
    many = []
    best = faces._best

    def counting(sims, bar, k=8):
        many.append(int((sims >= bar).sum()) > k)
        return best(sims, bar, k)

    def both(full):
        before = _state(conn)
        monkeypatch.setattr(faces, "_best", _best_by_sorting)
        a = faces.cluster(conn, full=full), _state(conn)
        _put_back(conn, before)
        monkeypatch.setattr(faces, "_best", counting)
        b = faces.cluster(conn, full=full), _state(conn)
        assert a == b
        return a[0]

    out = both(True)
    assert out["full"] and out["clusters"] > 14 and any(many)
    # New photos of them: placed in the groups there are, the same way.
    _crowd(conn, rng, shots[34:], look=look)
    many.clear()
    out = both(None)
    assert out["full"] is False and out["placed"] > 50 and any(many)


def test_how_well_each_group_agrees_is_worked_out_as_before(env):
    from joeylab import db, faces

    conn = db.connect("t")
    rng = np.random.default_rng(8)
    shots = _photos(conn, 30)
    _crowd(conn, rng, shots, people=6, noise=0.4)
    conn.execute("UPDATE shots SET status='missing' WHERE id=?", (shots[3],))
    _face(conn, shots[4], np.ones(128), model="opencv", cluster=1)                   # another model's face
    conn.commit()
    faces.cluster(conn, full=True)
    got = {r[0]: r[1:] for r in conn.execute("SELECT cluster, faces, cohesion, shared_photos, sharp FROM face_groups")}
    rows = conn.execute("SELECT f.cluster, f.shot_id, f.vec, f.sharp FROM faces f JOIN shots s ON s.id=f.shot_id WHERE"
                        " s.status='present' AND f.cluster IS NOT NULL AND f.model='insightface' ORDER BY f.cluster, f.id").fetchall()
    want = {}
    for c in {r[0] for r in rows}:
        g = [r for r in rows if r[0] == c]
        V = np.stack([np.frombuffer(r[2], dtype=np.float16).astype(np.float32) for r in g])
        V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
        cen = V.sum(0) / (np.linalg.norm(V.sum(0)) + 1e-9)
        shared = sum(1 for s in {r[1] for r in g} if sum(r[1] == s for r in g) > 1)
        want[c] = (len(g), float(np.median(V @ cen)), shared, float(np.median([r[3] or 0 for r in g])))
    assert got.keys() == want.keys() and len(got) > 3
    for c, (n, cohesion, shared, sharp) in want.items():
        assert got[c][0] == n and got[c][2] == shared and got[c][3] == sharp and abs(got[c][1] - cohesion) < 1e-5


# ---------------------------------------------------------------- nothing new: nothing read

def test_nothing_new_reads_no_vectors_and_the_daily_regrouping_waits_for_something_new(env, monkeypatch):
    from joeylab import db, faces

    conn = db.connect("t")
    shots = _photos(conn, 12)
    rng = np.random.default_rng(9)
    f = _people(conn, rng, shots, {"sam": (0, 6, .1), "meera": (6, 12, .1)})
    first = faces.cluster(conn)
    assert first["full"] and first["clusters"] == 2
    read = []
    vectors = faces.vectors
    monkeypatch.setattr(faces, "vectors", lambda conn: read.append(1) or vectors(conn))

    def last_full(days_ago):
        db.set_setting(conn, "faces_full_at", time.time() - days_ago * 86400)

    assert faces.cluster(conn) == {"faces": 12, "clusters": 2, "placed": 0} and not read
    last_full(2)                                         # a day on with nothing new: the same groups again, so no
    assert faces.cluster(conn)["placed"] == 0 and not read
    # A name given: the day's complete regrouping is due (it reads the vectors), and then isn't again.
    sam = conn.execute("INSERT INTO people(name) VALUES ('Sam')").lastrowid
    conn.execute("UPDATE faces SET person_id=?, person_src='user' WHERE id=?", (sam, f["sam"][0]))
    conn.commit()
    last_full(0.5)
    assert faces.cluster(conn)["placed"] == 0 and not read                         # not a day yet
    last_full(2)
    assert faces.cluster(conn)["full"] and read
    read.clear()
    last_full(2)
    assert faces.cluster(conn)["placed"] == 0 and not read
    # A new face is placed at once; a day on, it makes the complete regrouping due.
    new = _face(conn, shots[0], _vec(conn, f["sam"][0]), x=.5)
    conn.commit()
    last_full(0.5)
    read.clear()
    out = faces.cluster(conn)
    assert out["full"] is False and out["placed"] == 1 and read
    assert conn.execute("SELECT cluster FROM faces WHERE id=?", (new,)).fetchone()[0] is not None
    read.clear()
    assert faces.cluster(conn)["placed"] == 0 and not read
    last_full(2)
    assert faces.cluster(conn)["full"]
    # A face another model found, or one in a photo that's gone, isn't new.
    _face(conn, shots[1], np.ones(128), model="opencv")
    conn.execute("UPDATE shots SET status='missing' WHERE id=?", (shots[2],))
    _face(conn, shots[2], np.ones(512))
    conn.commit()
    read.clear()
    assert faces.cluster(conn)["placed"] == 0 and not read


def test_the_same_person_across_the_years_with_nobody_named_reads_nothing(env, monkeypatch):
    from joeylab import db, faces

    conn = db.connect("t")
    shots = _photos(conn, 6)
    f = _people(conn, np.random.default_rng(3), shots, {"sam": (0, 6, .1)})
    faces.cluster(conn)

    def boom(conn):
        raise AssertionError("read every face")

    monkeypatch.setattr(faces, "_units", boom)
    assert faces.across_years(conn) == []
    strangers = conn.execute("INSERT INTO people(name, hidden) VALUES ('Strangers', 1)").lastrowid    # only hidden people
    conn.execute("UPDATE faces SET person_id=?, person_src='user' WHERE id=?", (strangers, f["sam"][0]))
    conn.commit()
    assert faces.across_years(conn) == []
