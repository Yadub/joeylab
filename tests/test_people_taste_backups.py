"""Taste learning, people (clustering, naming, coverage), backups and instant folder watching."""

from __future__ import annotations

import os
import sqlite3
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import add_root, make_jpeg, run_all


def _lib(env, n=30):
    tmp, lib = env
    for i in range(n):
        make_jpeg(lib / f"IMG_{i:04d}.JPG", seed=i, blur=6 if i % 2 else 0)
    add_root(lib)
    conn, _ = run_all()
    return conn


def test_migration_adds_columns_to_old_catalog(env):
    from joeylab import db

    conn = db.connect("old")          # a catalog as round 1 left it: without the new columns
    conn.execute("ALTER TABLE metrics DROP COLUMN taste")
    conn.execute("ALTER TABLE shots DROP COLUMN faces_fp")
    conn.execute("ALTER TABLE shots DROP COLUMN gps_lat")
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    conn = db.connect("old")
    assert "taste" in {r[1] for r in conn.execute("PRAGMA table_info(metrics)")}
    assert "faces_fp" in {r[1] for r in conn.execute("PRAGMA table_info(shots)")}
    assert "gps_lat" in {r[1] for r in conn.execute("PRAGMA table_info(shots)")}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_upgrade_backs_up_first_and_ids_are_never_reused(env):
    from joeylab import config, db, scanner

    tmp, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    conn = db.connect("t")                                   # upgrading again takes a backup first
    assert list((config.HOME / "backups" / "t").glob("pre-upgrade-v1-*.db"))
    top = conn.execute("SELECT MAX(id) FROM shots").fetchone()[0]
    os.remove(lib / "IMG_2.JPG")
    scanner.scan(conn)
    scanner.prune_missing(conn)
    make_jpeg(lib / "IMG_9.JPG", seed=9)
    scanner.scan(conn)
    assert conn.execute("SELECT MIN(id) FROM shots WHERE stem='IMG_9'").fetchone()[0] > top


def test_taste_learns_a_simple_preference(env):
    from joeylab import library, scoring, taste

    conn = _lib(env)
    rows = conn.execute("SELECT s.id, m.sharp_subject FROM shots s JOIN metrics m ON m.shot_id=s.id").fetchall()
    assert taste.train_all()["status"] == "learning"          # nothing decided yet
    # This photographer keeps sharp photos and rejects blurry ones.
    for r in rows:
        library.set_decision(conn, r["id"], pick=1 if r["sharp_subject"] > 200 else -1)
    meta = taste.train_all()
    assert meta["positives"] >= 12 and meta["negatives"] >= 12
    assert meta["auc"] > 0.8 and meta["trusted"]
    scoring.run(conn)
    t = dict(conn.execute("SELECT shot_id, taste FROM metrics").fetchall())
    sharp = [t[r["id"]] for r in rows if r["sharp_subject"] > 200]
    blurry = [t[r["id"]] for r in rows if r["sharp_subject"] <= 200]
    assert np.mean(sharp) > np.mean(blurry)


def _fake_faces(conn, shots, identities=2, per=6, seed=0):
    rng = np.random.default_rng(seed)
    bases = [v / np.linalg.norm(v) for v in rng.normal(size=(identities, 512))]
    k = 0
    for ident in range(identities):
        for j in range(per):
            v = bases[ident] + rng.normal(scale=0.03, size=512)
            v = (v / np.linalg.norm(v)).astype(np.float16)
            conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, area, sharp, closed, vec) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (shots[k % len(shots)], 0.1 * ident, 0.1, 0.1, 0.1, 120, 0.01, 300, 0, v.tobytes()))
            k += 1
    conn.commit()


def test_clustering_naming_merging_and_unassign(env):
    from joeylab import faces
    from joeylab.web.server import create_app

    conn = _lib(env, n=12)
    shots = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    _fake_faces(conn, shots)
    res = faces.cluster(conn)
    assert (res["faces"], res["clusters"]) == (12, 2)
    c = TestClient(create_app())
    groups = c.get("/api/p/t/people").json()["groups"]
    assert len(groups) == 2 and all(g["photos"] == 6 for g in groups)

    pid = c.post("/api/p/t/people/name", json={"cluster": groups[0]["cluster"], "name": "Maya"}).json()["id"]
    people = c.get("/api/p/t/people").json()
    assert people["people"][0]["name"] == "Maya" and people["people"][0]["photos"] == 6
    assert len(people["groups"]) == 1
    assert c.get("/api/p/t/shots", params={"person": pid}).json()["total"] == 6
    # the person survives a re-cluster
    faces.cluster(conn)
    assert c.get("/api/p/t/shots", params={"person": pid}).json()["total"] == 6
    # "not them" removes a face permanently
    fid = c.get(f"/api/p/t/people/{pid}/faces").json()["faces"][0]["id"]
    c.post(f"/api/p/t/faces/{fid}/unassign", json={})
    faces.cluster(conn)
    assert len(c.get(f"/api/p/t/people/{pid}/faces").json()["faces"]) == 5
    # naming the other group with the same name merges; renaming to an existing name merges too
    c.post("/api/p/t/people/name", json={"cluster": people["groups"][0]["cluster"], "name": "Sam"})
    sam = [x for x in c.get("/api/p/t/people").json()["people"] if x["name"] == "Sam"][0]
    r = c.post(f"/api/p/t/people/{sam['id']}/rename", json={"name": "maya"}).json()
    assert r["merged"]
    assert len(c.get("/api/p/t/people").json()["people"]) == 1


def test_face_crop_and_shot_faces(env):
    from joeylab import faces
    from joeylab.web.server import create_app

    conn = _lib(env, n=4)
    shots = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    _fake_faces(conn, shots, identities=1, per=2)
    faces.cluster(conn)
    c = TestClient(create_app())
    fid = conn.execute("SELECT id FROM faces LIMIT 1").fetchone()[0]
    r = c.get(f"/img/t/f/{fid}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    d = c.get(f"/api/p/t/shot/{shots[0]}").json()
    assert d["faces"] and "person_id" in d["faces"][0]


def test_backup(env):
    from joeylab import backup, library

    conn = _lib(env, n=3)
    library.set_decision(conn, 1, rating=5)
    p = backup.backup("t")
    assert p and p.exists()
    assert backup.backup("t") is None                       # once per day
    b = sqlite3.connect(p)
    assert b.execute("SELECT rating FROM decisions WHERE shot_id=1").fetchone()[0] == 5


def test_folder_watcher_triggers_update(env, monkeypatch):
    from joeylab.web import watch

    tmp, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    run_all()
    monkeypatch.setattr(watch, "QUIET_SECONDS", 1)

    class Jobs:
        running: dict = {}
        started: list = []

        def start(self, project, **kw):
            self.started.append((project, kw))

    jobs = Jobs()
    w = watch.FolderWatcher(jobs)
    w.start()
    try:
        time.sleep(1.0)
        make_jpeg(lib / "NEW.JPG", seed=9)
        for _ in range(100):
            if jobs.started:
                break
            time.sleep(0.1)
        assert jobs.started and jobs.started[0][0] == "t" and jobs.started[0][1]["only_if_changed"]
    finally:
        w.stop()


TEST_MODELS = os.environ.get("JOEYLAB_TEST_MODELS")     # a folder holding scrfd_500m.onnx and arcface_mbf.onnx


@pytest.mark.skipif(not (TEST_MODELS and os.path.isfile(os.path.join(TEST_MODELS, "scrfd_500m.onnx"))),
                    reason="set JOEYLAB_TEST_MODELS to a folder with the face models to run this")
def test_face_detector_runs(env, monkeypatch):
    from pathlib import Path

    from joeylab import faces

    models = Path(TEST_MODELS)
    monkeypatch.setattr(faces, "DET_PATH", models / "scrfd_500m.onnx")
    monkeypatch.setattr(faces, "REC_PATH", models / "arcface_mbf.onnx")
    monkeypatch.setattr(faces, "FACE_MODEL", models / "face_landmarker.task")
    saved = os.dup(2)                 # the worker initialiser silences stderr; restore it after
    try:
        faces._init_worker()
    finally:
        os.dup2(saved, 2)
    try:
        boxes, kps, _ = faces.detect(np.full((600, 900, 3), 200, dtype=np.uint8))
        assert boxes.shape == (0, 4) and kps.shape == (0, 5, 2)   # a blank image has no faces
    finally:
        if faces._lm is not None:      # MediaPipe crashes if left for interpreter shutdown
            faces._lm.close()
            faces._lm = None


def test_search_query_variants_and_label_hits(env):
    import json

    from joeylab import db, search

    assert "a photo of a dog" in search.query_variants("dogs")
    assert search.query_variants("a red umbrella")[0] == "a red umbrella"
    conn = db.connect("t")
    search.ensure_schema(conn)
    conn.execute("INSERT INTO vision(shot_id, src, labels, text) VALUES (1, 'x', ?, ?)",
                 (json.dumps([["dog", 0.91], ["german_shepherd", 0.8], ["door", 0.2]]), "HAPPY BIRTHDAY"))
    conn.execute("INSERT INTO search_fts(rowid, labels, text) VALUES (1, 'dog german shepherd door', 'HAPPY BIRTHDAY')")
    hits = search._keyword_hits(conn, "dogs")
    assert hits[1]["label"] == "dog" and hits[1]["label_conf"] == 0.91
    assert search._keyword_hits(conn, "doors")[1]["label"] == "door"
    assert search._keyword_hits(conn, "happy birthday")[1]["text"] is True
    assert search._keyword_hits(conn, "cats") == {}


def test_duplicate_folder_groups_and_keep_choice(env):
    import shutil

    from joeylab import dupes

    tmp, lib = env
    for i in range(3):
        make_jpeg(lib / "card" / f"DSCF{i}.JPG", seed=i)
    (lib / "picks").mkdir()
    for i in range(2):                                   # a selection: 2 of the card's 3 photos
        shutil.copy2(lib / "card" / f"DSCF{i}.JPG", lib / "picks" / f"DSCF{i}.JPG")
    add_root(lib)
    conn, _ = run_all()
    g = dupes.folder_groups(conn)["groups"]
    assert len(g) == 1 and g[0]["sets"] == 2 and g[0]["extra_files"] == 2
    pct = {f["path"].rsplit("/", 1)[1]: f["pct"] for f in g[0]["folders"]}
    assert pct == {"card": 67, "picks": 100}
    picks = str(lib / "picks")
    dupes.prefer_folder(conn, picks, g[0]["set_ids"])
    kept = {r[0] for r in conn.execute("SELECT f.path FROM dup_members dm JOIN files f ON f.id=dm.file_id WHERE dm.keep=1")}
    assert all(p.startswith(picks) for p in kept)
    run_all()                                            # the choice survives a full recompute
    kept = {r[0] for r in conn.execute("SELECT f.path FROM dup_members dm JOIN files f ON f.id=dm.file_id WHERE dm.keep=1")}
    assert kept and all(p.startswith(picks) for p in kept)


def test_selection_folders_albums_and_link_folder(env, tmp_path):
    import shutil

    import pytest as _pytest

    from joeylab import selections

    tmp, lib = env
    for i in range(8):
        make_jpeg(lib / "SD Card" / f"DSCF{i:04d}.JPG", seed=i)
    (lib / "Wedding").mkdir()
    for i in range(6):                                   # a selection: 6 of the card's 8
        shutil.copy2(lib / "SD Card" / f"DSCF{i:04d}.JPG", lib / "Wedding" / f"DSCF{i:04d}.JPG")
    shutil.copytree(lib / "SD Card", lib / "Backup of card")   # a whole folder copied twice
    add_root(lib)
    conn, _ = run_all()
    rows = {(os.path.basename(r["folder"]), r["kind"]) for r in conn.execute("SELECT folder, kind FROM selections")}
    assert ("Wedding", "selection") in rows
    assert any(k == "mirror" for _, k in rows)
    sel = conn.execute("SELECT * FROM selections WHERE folder LIKE '%Wedding'").fetchone()
    assert sel["matched"] == 6 and sel["identical"] == 6 and sel["album_id"]
    album = sel["album_id"]
    originals = {r[0] for r in conn.execute(
        "SELECT f.path FROM album_items i JOIN files f ON f.id = i.file_id WHERE i.album_id = ?", (album,))}
    assert len(originals) == 6 and not any("/Wedding/" in p for p in originals)   # points at originals
    plan, _ = selections.link_plan_csv(conn, sel["id"])
    assert len(plan.strip().splitlines()) == 7 and "True" in plan

    # link folder on the Mac's own disk: names from the selection, targets are the originals
    dest = tmp_path / "Links" / "Wedding"
    r = selections.make_link_folder(conn, album, str(dest))
    assert r["made"] == 6 and all(os.path.islink(dest / n) for n in os.listdir(dest))
    assert all("/Wedding/" not in os.readlink(dest / n) for n in os.listdir(dest))
    with _pytest.raises(ValueError):
        selections.make_link_folder(conn, album, "/Volumes/Somewhere/x")    # never on external drives

    # deleting the copies keeps the album (and the Library can still show it)
    shutil.rmtree(lib / "Wedding")
    conn, _ = run_all()
    assert conn.execute("SELECT COUNT(*) FROM album_items WHERE album_id = ?", (album,)).fetchone()[0] == 6
    from joeylab import library

    assert library.page(conn, library.Filter(album_id=album))[1] == 6
