"""Naming a face group pulls in other groups that are clearly the same person, but never one that is
in a photo with the named group or with the person already: two faces in one photo are two people."""

from __future__ import annotations

import numpy as np


def _catalog(conn, groups: dict[int, list[int]], person: dict[int, int] | None = None):
    """Faces that all look alike: {cluster: [photo numbers]}, plus {photo: person} already named."""
    rng = np.random.default_rng(5)
    base = rng.normal(size=512)
    base /= np.linalg.norm(base)
    photos = sorted({n for ns in groups.values() for n in ns} | set(person or {}))
    sid = {n: conn.execute("INSERT INTO shots(dir, stem) VALUES ('/lib', ?)", (f"IMG_{n}",)).lastrowid for n in photos}

    def vec():
        v = base + rng.normal(scale=0.01, size=512)
        return (v / np.linalg.norm(v)).astype(np.float16).tobytes()

    for cid, ns in groups.items():
        for n in ns:
            conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, area, sharp, closed, vec, cluster)"
                         " VALUES (?, .2, .3, .1, .15, 120, .02, 300, 0, ?, ?)", (sid[n], vec(), cid))
    for n, pid in (person or {}).items():
        conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, area, sharp, closed, vec, cluster, person_id, person_src)"
                     " VALUES (?, .6, .3, .1, .15, 120, .02, 300, 0, ?, 50, ?, 'user')", (sid[n], vec(), pid))
    conn.commit()


def _named(conn, pid):
    return {r[0] for r in conn.execute("SELECT DISTINCT cluster FROM faces WHERE person_id=?", (pid,))}


def _most_per_photo(conn, pid):
    return conn.execute("SELECT MAX(k) FROM (SELECT COUNT(*) k FROM faces WHERE person_id=? GROUP BY shot_id)",
                        (pid,)).fetchone()[0]


def test_a_look_alike_in_the_same_photos_is_not_named_too(env):
    from joeylab import db, faces

    conn = db.connect("t")
    # Twins: groups 1 and 2 are side by side in photos 1-4. Group 3 is twin 1 again, on her own.
    _catalog(conn, {1: [1, 2, 3, 4], 2: [1, 2, 3, 4], 3: [5, 6]})
    maya = conn.execute("INSERT INTO people(name) VALUES ('Maya')").lastrowid
    joined = faces.name_cluster(conn, 1, maya)
    assert _named(conn, maya) == {1, 3} and joined == 1
    assert _most_per_photo(conn, maya) == 1


def test_a_group_in_a_photo_with_the_person_already_is_left_alone(env):
    from joeylab import db, faces

    conn = db.connect("t")
    conn.execute("INSERT INTO people(id, name) VALUES (7, 'Maya')")
    # Maya is already named in photos 7 and 8 (group 50). Group 4 is someone beside her in photo 7.
    _catalog(conn, {1: [1, 2], 4: [7, 9]}, person={7: 7, 8: 7})
    assert faces.name_cluster(conn, 1, 7) == 0
    assert _named(conn, 7) == {1, 50} and _most_per_photo(conn, 7) == 1


def test_two_groups_pulled_in_are_never_in_one_photo(env):
    from joeylab import db, faces

    conn = db.connect("t")
    # Groups 5 and 6 are both alike enough to join, but share photo 3: only one of them can be her.
    _catalog(conn, {1: [1, 2], 5: [3, 4], 6: [3, 5]})
    maya = conn.execute("INSERT INTO people(name) VALUES ('Maya')").lastrowid
    assert faces.name_cluster(conn, 1, maya) == 1
    named = _named(conn, maya)
    assert 1 in named and len(named & {5, 6}) == 1
    assert _most_per_photo(conn, maya) == 1


def test_a_group_already_hers_still_counts_as_hers(env):
    from joeylab import db, faces

    conn = db.connect("t")
    # Group 8 was put with Maya by the grouping earlier; naming group 1 doesn't treat it as a stranger.
    _catalog(conn, {1: [1, 2], 8: [3, 4]})
    maya = conn.execute("INSERT INTO people(name) VALUES ('Maya')").lastrowid
    conn.execute("UPDATE faces SET person_id=?, person_src='auto' WHERE cluster=8", (maya,))
    conn.commit()
    faces.name_cluster(conn, 1, maya)
    assert _named(conn, maya) == {1, 8}


def test_a_detected_face_gets_its_crop_and_vector(tmp_path, monkeypatch):
    """analyze_one must turn a detection into a face (it once failed on every face: `io` wasn't imported)."""
    import numpy as np
    from PIL import Image

    from joeylab import faces, imaging

    img = Image.new("RGB", (800, 600), (120, 110, 100))
    monkeypatch.setattr(imaging, "load_preview", lambda *a, **k: img)
    box = np.array([[300.0, 200.0, 500.0, 420.0]])
    kps = np.zeros((1, 5, 2))
    monkeypatch.setattr(faces, "detect", lambda rgb: (box, kps, np.array([0.9])))
    monkeypatch.setattr(faces, "eyes_and_smile", lambda rgb, b: (0, 0.2))
    monkeypatch.setattr(faces, "embed", lambda rgb, k: np.ones(512, dtype=np.float32))
    out = faces.analyze_one({"shot_id": 1, "stamp": "s", "path": str(tmp_path / "x.jpg"), "kind": "jpeg", "orientation": 1})
    assert out["error"] is None, out.get("trace")
    assert len(out["faces"]) == 1 and out["faces"][0]["crop"][:2] == b"\xff\xd8"
