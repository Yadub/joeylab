"""Regressions: one test per bug fixed after a review of the whole app (files, drives, undo, events,
keywords, sidecars, access, search and taste)."""

from __future__ import annotations

import json
import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import add_root, jpeg_at, make_jpeg, run_all


# ---------------------------------------------------------------- F1 merges keep your work

def test_two_files_of_one_photo_joining_keep_everything(env):
    from joeylab import keywords, library, scanner

    _, lib = env
    make_jpeg(lib / "a" / "IMG_1.JPG", seed=1)
    (lib / "b").mkdir()
    Image.open(lib / "a" / "IMG_1.JPG").save(lib / "b" / "IMG_1.TIF")
    old = time.time() - 60
    os.utime(lib / "b" / "IMG_1.TIF", (old, old))
    add_root(lib)
    conn, _ = run_all()
    a, b = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY dir")]
    library.decide(conn, [{"id": a, "rating": 2}], what="2★")
    time.sleep(0.01)
    library.decide(conn, [{"id": b, "rating": 5, "pick": 1}], what="5★ pick")
    keywords.assign(conn, [b], add=[keywords.ensure(conn, "Trips > Lisbon")], what="keywords: Lisbon")
    conn.execute("INSERT INTO comments(shot_id, viewer, text, ts) VALUES (?, 'mum', 'print this', 0)", (b,))
    conn.execute("INSERT INTO viewer_decisions(viewer, shot_id, fav, updated) VALUES ('mum', ?, 1, 0)", (b,))
    conn.commit()

    shutil.move(str(lib / "b" / "IMG_1.TIF"), str(lib / "a" / "IMG_1.TIF"))     # the TIF joins its JPEG
    scanner.scan(conn)
    shots = [r[0] for r in conn.execute("SELECT id FROM shots WHERE status='present'")]
    assert len(shots) == 1
    s = shots[0]
    assert conn.execute("SELECT rating, pick FROM decisions WHERE shot_id=?", (s,)).fetchone()[:] == (5, 1)   # the newer one
    assert [k["path"] for k in keywords.of_shot(conn, s)] == ["Trips › Lisbon"]
    assert conn.execute("SELECT COUNT(*) FROM comments WHERE shot_id=?", (s,)).fetchone()[0] == 1
    assert conn.execute("SELECT fav FROM viewer_decisions WHERE shot_id=?", (s,)).fetchone()[0] == 1
    # The decision that lost is in the history, and Undo brings it back.
    step = conn.execute("SELECT batch FROM decision_log WHERE source='merge'").fetchone()[0]
    library.undo(conn, batch=step)
    assert conn.execute("SELECT rating, pick FROM decisions WHERE shot_id=?", (s,)).fetchone()[:] == (2, 0)


# ---------------------------------------------------------------- F2 undo leaves later changes alone

def test_undo_keeps_what_you_changed_since_and_redo_replays(env):
    from joeylab import keywords, library

    _, lib = env
    for i in range(2):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    a, b = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    library.decide(conn, [{"id": a, "pick": -1}, {"id": b, "label": "Red"}], what="assistant", source="mcp")
    library.decide(conn, [{"id": a, "rating": 5, "pick": 1}, {"id": b, "rating": 3}], what="you", source="web")
    res = library.undo(conn, source="mcp")
    assert res["skipped"] == 1                                    # your pick on a wins
    assert conn.execute("SELECT rating, pick FROM decisions WHERE shot_id=?", (a,)).fetchone()[:] == (5, 1)
    assert conn.execute("SELECT rating, label FROM decisions WHERE shot_id=?", (b,)).fetchone()[:] == (3, None)
    assert library.redo(conn)["label"] == "assistant"
    assert conn.execute("SELECT label FROM decisions WHERE shot_id=?", (b,)).fetchone()[0] == "Red"
    assert conn.execute("SELECT pick FROM decisions WHERE shot_id=?", (a,)).fetchone()[0] == 1     # still yours
    # Keyword steps undo by what they added, not by overwriting.
    beach, sun = keywords.ensure(conn, "Beach"), keywords.ensure(conn, "Sunset")
    keywords.assign(conn, [a], add=[beach], what="keywords: Beach")
    keywords.assign(conn, [a], add=[sun], what="keywords: Sunset")
    library.undo(conn, batch=conn.execute("SELECT batch FROM decision_log WHERE label='keywords: Beach'").fetchone()[0])
    assert [k["name"] for k in keywords.of_shot(conn, a)] == ["Sunset"]
    with pytest.raises(ValueError):
        library.decide(conn, [{"id": a, "rating": 99}])
    with pytest.raises(ValueError):
        library.decide(conn, [{"id": a, "label": "Magenta"}])
    assert library.decide(conn, [{"id": 999999, "rating": 3}])[1] == []          # unknown photos are skipped


# ---------------------------------------------------------------- F3 ids are never handed out twice

def test_keywords_people_and_albums_never_reuse_ids(env):
    from joeylab import keywords
    from joeylab.web.server import create_app

    _, lib = env
    make_jpeg(lib / "IMG_0.JPG", seed=0)
    add_root(lib)
    conn, _ = run_all()
    k1 = keywords.ensure(conn, "Beach")
    keywords.delete(conn, k1)
    assert keywords.ensure(conn, "Funeral") != k1
    c = TestClient(create_app())
    sid = conn.execute("SELECT id FROM shots").fetchone()[0]
    a1 = c.post("/api/p/t/albums", json={"name": "Shared", "ids": [sid]}).json()["id"]
    tok = c.post("/api/p/t/shares", json={"kind": "album", "ref": a1}).json()["token"]
    c.delete(f"/api/p/t/albums/{a1}")
    a2 = c.post("/api/p/t/albums", json={"name": "Private", "ids": [sid]}).json()["id"]
    assert a2 != a1 and c.get(f"/api/share/{tok}").status_code == 404


# ---------------------------------------------------------------- F4/F5 exports never land in photo folders

def test_destination_check_ignores_case_and_spelling(env):
    from joeylab import export

    _, lib = env
    make_jpeg(lib / "Photos" / "IMG_0.JPG", seed=0)
    add_root(lib / "Photos")
    run_all()
    for bad in (lib / "Photos" / "best", lib / "photos" / "best", lib / "PHOTOS"):
        with pytest.raises(ValueError):
            export.check_dest(bad)
    with pytest.raises(ValueError):
        export.check_dest("")
    assert export.check_dest(lib / "elsewhere")


# ---------------------------------------------------------------- F6/F7 hiccups aren't permanent

def test_exiftool_only_marks_files_it_read(env):
    from joeylab import exif

    if not exif.available():
        pytest.skip("exiftool not installed")
    _, lib = env
    good = make_jpeg(lib / "IMG_0.JPG", seed=0)
    out, done = exif.read_many([str(good), str(lib / "gone.JPG")])
    assert str(good) in done and str(lib / "gone.JPG") not in done


def test_a_file_that_cant_be_read_is_not_marked_missing(env, monkeypatch):
    from joeylab import scanner

    _, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    real = os.lstat

    def flaky(p, *a, **k):
        if str(p).endswith("IMG_1.JPG"):
            raise OSError(5, "Input/output error", str(p))
        return real(p, *a, **k)
    monkeypatch.setattr(os, "lstat", flaky)
    rep = scanner.scan(conn)
    monkeypatch.undo()
    assert not rep.missing
    assert conn.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0] == 3


def test_a_camera_clock_ahead_isnt_still_copying(env):
    from joeylab import scanner

    _, lib = env
    p = make_jpeg(lib / "IMG_0.JPG", seed=0)
    future = time.time() + 3 * 3600
    os.utime(p, (future, future))
    add_root(lib)
    from joeylab import db

    rep = scanner.scan(db.connect("t"))
    assert rep.added == 1 and not rep.settling


# ---------------------------------------------------------------- F8 named events survive an unplug

def test_named_event_keeps_its_name_while_its_photos_are_away(env):
    from joeylab import events, scanner

    _, lib = env
    for i in range(6):
        jpeg_at(lib / "party" / f"IMG_{i}.JPG", seed=i, when=f"2024:06:01 1{i}:00:00")
    add_root(lib)
    conn, _ = run_all()
    events.run(conn)
    eid = conn.execute("SELECT id FROM events WHERE kind='event'").fetchone()[0]
    events.rename(conn, eid, "Mum's 60th")
    shutil.move(str(lib / "party"), str(lib.parent / "away"))
    scanner.scan(conn)
    events.run(conn)
    shutil.move(str(lib.parent / "away"), str(lib / "party"))
    scanner.scan(conn)
    events.run(conn)
    assert conn.execute("SELECT name FROM events WHERE kind='event'").fetchone()[0] == "Mum's 60th"


# ---------------------------------------------------------------- F9/F10

def test_upgrade_requeues_photos_whose_vision_pass_never_ran(env):
    from joeylab import db

    _, lib = env
    make_jpeg(lib / "IMG_0.JPG", seed=0)
    add_root(lib)
    conn, _ = run_all()
    conn.execute("CREATE TABLE IF NOT EXISTS vision (shot_id INTEGER PRIMARY KEY, src TEXT, labels TEXT, text TEXT, utility INTEGER, score REAL)")
    conn.execute("INSERT INTO vision VALUES (1, 'x', '[]', '', NULL, NULL), (2, 'x', '[[\"dog\", 0.9]]', '', 0, 0.5)")
    db._m6_round6(conn)
    assert [r[0] for r in conn.execute("SELECT shot_id FROM vision WHERE src = ''")] == [1]


def test_preview_pruning_keeps_face_crops(tmp_path):
    from joeylab.web.server import _prune_cache

    (tmp_path / "faces" / "01").mkdir(parents=True)
    face = tmp_path / "faces" / "01" / "1_x.jpg"
    face.write_bytes(b"x" * 1000)
    os.utime(face, (1, 1))                      # the oldest file of all
    for i in range(5):
        (tmp_path / f"p{i}.jpg").write_bytes(b"y" * 400_000)
    _prune_cache(tmp_path, cap_mb=1)
    assert face.exists() and len(list(tmp_path.glob("p*.jpg"))) < 5


# ---------------------------------------------------------------- S: security and robustness

def test_only_its_own_pages_can_change_things(env):
    from joeylab.web.server import create_app

    _, lib = env
    make_jpeg(lib / "IMG_0.JPG", seed=0)
    add_root(lib)
    conn, _ = run_all()
    c = TestClient(create_app())
    # A page on another port of this Mac: same host name, no JoeyLab header, no JSON body.
    other = {"Origin": "http://testserver:3000", "Sec-Fetch-Site": "same-site"}
    assert c.post("/api/p/t/undo", headers=other).status_code == 403
    assert c.post("/api/p/t/accept", headers=other).status_code == 403
    assert c.post("/api/p/t/undo", headers={"Origin": "http://testserver", "X-JoeyLab": "1"}).status_code == 200
    assert c.post("/api/p/t/undo", json={}, headers={"Origin": "http://testserver"}).status_code == 200
    assert c.post("/api/p/t/undo").status_code == 200                       # curl, the CLI: not a browser
    assert c.get("/api/p/t/overview", headers={"Tailscale-Funnel-Request": "?1"}).status_code == 403


def test_only_the_owner_changes_things_with_or_without_an_identity(env):
    from joeylab.web.server import create_app

    _, lib = env
    make_jpeg(lib / "IMG_0.JPG", seed=0)
    add_root(lib)
    conn, _ = run_all()
    sid = conn.execute("SELECT id FROM shots").fetchone()[0]
    c = TestClient(create_app(owner="owner@example.com"))
    body = {"items": [{"id": sid, "rating": 3}]}
    assert c.post("/api/p/t/decide", json=body).status_code == 200                                    # at the Mac
    assert c.post("/api/p/t/decide", json=body, headers={"X-Forwarded-For": "100.64.0.9"}).status_code == 403
    assert c.post("/api/p/t/decide", json=body, headers={"Tailscale-User-Login": "guest@example.com"}).status_code == 403
    assert c.post("/api/p/t/decide", json=body, headers={"Tailscale-User-Login": "owner@example.com", "X-Forwarded-For": "100.64.0.8"}).status_code == 200
    assert c.get("/api/fs", headers={"Tailscale-User-Login": "guest@example.com"}).status_code == 403


def test_bad_inputs_are_refused_cleanly(env):
    from joeylab import db
    from joeylab.web.server import create_app

    _, lib = env
    make_jpeg(lib / "IMG_0.JPG", seed=0)
    add_root(lib)
    conn, _ = run_all()
    c = TestClient(create_app())
    assert c.post("/api/p/t/roots", json={"path": ""}).status_code == 400
    assert c.post("/api/projects", json={"name": "x", "path": ""}).status_code == 400
    assert len(c.get("/api/p/t/shots?limit=-1").json()["rows"]) == 1
    assert c.post("/api/p/t/shares", json={"kind": "album"}).status_code == 400
    assert c.post("/api/p/t/decide", json={"items": [{"id": 1, "rating": 99}]}).status_code == 400
    assert c.get("/api/p/t/events?year=99999").status_code == 400
    assert c.get("/api/p/nope/backups").status_code == 404
    with pytest.raises(ValueError):
        db.connect("share")


def test_gallery_never_replaces_someone_elses_page(env, tmp_path):
    from joeylab import portable
    from joeylab.library import Filter

    _, lib = env
    make_jpeg(lib / "IMG_0.JPG", seed=0)
    add_root(lib)
    conn, _ = run_all()
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>my blog</h1>")
    with pytest.raises(ValueError):
        portable.export_gallery(conn, Filter(), site, "x", render=lambda s: s["thumb"])
    assert (site / "index.html").read_text() == "<h1>my blog</h1>"
    with pytest.raises(ValueError):
        portable.export_gallery(conn, Filter(), "", "x", render=lambda s: s["thumb"])


def test_mcp_survives_odd_messages():
    from joeylab import mcp

    assert mcp.handle([1, 2], None, True)["error"]["code"] == -32600
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, None, True) is None
    init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}}, None, True)
    assert init["result"]["protocolVersion"] == mcp.PROTOCOL
    bad = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "photo_details", "arguments": {"id": "x"}}}, "nope", True)
    assert bad["result"]["isError"]


def test_duplicate_sets_keep_their_ids_across_updates(env):
    from joeylab import dupes

    _, lib = env
    make_jpeg(lib / "a" / "IMG_0.JPG", seed=0)
    (lib / "b").mkdir()
    shutil.copy2(lib / "a" / "IMG_0.JPG", lib / "b" / "IMG_0.JPG")
    add_root(lib)
    conn, _ = run_all()
    first = [r[0] for r in conn.execute("SELECT id FROM dup_sets")]
    dupes.run(conn)
    assert [r[0] for r in conn.execute("SELECT id FROM dup_sets")] == first and first


def test_two_photos_with_one_name_get_their_own_sidecars(env):
    from joeylab import export, library
    from joeylab.library import Filter

    _, lib = env
    # One name, two photos taken months apart (kept apart on purpose).
    jpeg_at(lib / "IMG_1.JPG", seed=1, when="2024:01:01 10:00:00")
    jpeg_at(lib / "IMG_1.jpeg", seed=2, when="2024:06:01 10:00:00")
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots")]
    assert len(ids) == 2
    library.decide(conn, [{"id": ids[0], "pick": -1}, {"id": ids[1], "rating": 5}])
    export.write_sidecars(conn, Filter(ids=ids), include=("rating",), project="t")
    sides = sorted(p.name for p in lib.glob("*.xmp"))
    assert sides == ["IMG_1.JPG.xmp", "IMG_1.jpeg.xmp"]


# ---------------------------------------------------------------- L1/L2 faces

def test_two_faces_in_one_photo_are_two_people(env):
    import numpy as np

    from joeylab import faces

    _, lib = env
    for i in range(6):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    shots = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    rng = np.random.default_rng(3)
    base = rng.normal(size=512)
    base /= np.linalg.norm(base)
    for sid in shots:                     # look-alike twins, side by side in every photo
        for x in (0.2, 0.6):
            v = base + rng.normal(scale=0.01, size=512)
            conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, area, sharp, closed, vec) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (sid, x, 0.3, 0.1, 0.15, 120, 0.02, 300, 0, (v / np.linalg.norm(v)).astype(np.float16).tobytes()))
    conn.commit()
    faces.cluster(conn)
    per_photo = conn.execute("SELECT MAX(k) FROM (SELECT cluster, shot_id, COUNT(*) k FROM faces GROUP BY cluster, shot_id)").fetchone()[0]
    assert per_photo == 1
    assert conn.execute("SELECT COUNT(DISTINCT cluster) FROM faces").fetchone()[0] == 2
    # A new photo with both of them later: placed without re-grouping, still one face per group per photo.
    make_jpeg(lib / "IMG_9.JPG", seed=9)
    from joeylab import scanner

    scanner.scan(conn)
    new = conn.execute("SELECT id FROM shots WHERE stem='IMG_9'").fetchone()[0]
    for x in (0.2, 0.6):
        v = base + rng.normal(scale=0.01, size=512)
        conn.execute("INSERT INTO faces(id, shot_id, x, y, w, h, px, area, sharp, closed, vec) VALUES ((SELECT MAX(id)+1 FROM faces),?,?,?,?,?,?,?,?,?,?)",
                     (new, x, 0.3, 0.1, 0.15, 120, 0.02, 300, 0, (v / np.linalg.norm(v)).astype(np.float16).tobytes()))
    conn.commit()
    res = faces.cluster(conn)
    assert res.get("full") is False and res["placed"] == 2
    assert conn.execute("SELECT COUNT(DISTINCT cluster) FROM faces WHERE shot_id=?", (new,)).fetchone()[0] == 2


# ---------------------------------------------------------------- L4 damaged files

def test_a_cut_off_jpeg_is_shown_and_flagged_damaged(env):
    from joeylab import scoring

    _, lib = env
    good = make_jpeg(lib / "IMG_0.JPG", seed=0, size=(1200, 800))
    data = good.read_bytes()
    cut = lib / "IMG_1.JPG"
    cut.write_bytes(data[: int(len(data) * 0.6)])            # what a recovered card often leaves
    os.utime(cut, (time.time() - 60, time.time() - 60))
    add_root(lib)
    conn, _ = run_all()
    r = conn.execute("SELECT id, analyzed_fp, error, thumb FROM shots WHERE stem='IMG_1'").fetchone()
    assert r["analyzed_fp"] and r["error"].startswith("Damaged file") and os.path.exists(r["thumb"])
    flags, sug = conn.execute("SELECT flags, suggestion FROM metrics WHERE shot_id=?", (r["id"],)).fetchone()
    assert "damaged" in json.loads(flags) and sug == "check"
    assert any("damaged" in w["text"] for w in scoring.explain(conn, r["id"]))


# ---------------------------------------------------------------- L6 backups

def test_daily_backups_are_slim_with_a_weekly_full_copy(env):
    import sqlite3
    from datetime import date, timedelta

    from joeylab import backup, config

    _, lib = env
    make_jpeg(lib / "IMG_0.JPG", seed=0)
    add_root(lib)
    conn, _ = run_all()
    conn.execute("CREATE TABLE IF NOT EXISTS search_vecs (shot_id INTEGER PRIMARY KEY, src TEXT, vecs BLOB)")
    conn.execute("INSERT INTO search_vecs VALUES (1, 'x', zeroblob(100000))")
    conn.commit()
    first = backup.backup("t")                                 # no full copy yet: a full one
    assert first.name.endswith(f"{date.today():%Y%m%d}.db")
    assert sqlite3.connect(first).execute("SELECT COUNT(*) FROM search_vecs").fetchone()[0] == 1
    d = config.HOME / "backups" / "t"
    first.rename(d / f"catalog-{date.today() - timedelta(days=2):%Y%m%d}.db")   # pretend it was two days ago
    slim = backup.backup("t")
    assert slim.name.endswith("-slim.db") and slim.stat().st_size < (d / f"catalog-{date.today() - timedelta(days=2):%Y%m%d}.db").stat().st_size
    assert sqlite3.connect(slim).execute("SELECT COUNT(*) FROM search_vecs").fetchone()[0] == 0
    assert backup.backup("t") is None                          # one a day
    for i in range(4):
        (d / f"pre-upgrade-v{i}-2026010{i}.db").write_bytes(b"x")
        os.utime(d / f"pre-upgrade-v{i}-2026010{i}.db", (i, i))
    backup.prune("t")
    assert sorted(p.name for p in d.glob("pre-upgrade-*")) == ["pre-upgrade-v2-20260102.db", "pre-upgrade-v3-20260103.db"]


# ---------------------------------------------------------------- fault injection

def test_two_same_named_drives_coming_back_swapped(env, tmp_path, monkeypatch):
    from joeylab import db, scanner, volumes

    vols = tmp_path / "Volumes"
    uuids = {}
    monkeypatch.setattr(volumes, "VOLUMES", vols)
    monkeypatch.setattr(volumes, "_info", lambda m: {"uuid": uuids.get(os.path.basename(m)), "name": os.path.basename(m)})
    volumes._cache.clear()
    make_jpeg(vols / "Card" / "A" / "IMG_1.JPG", seed=1)
    make_jpeg(vols / "Card 1" / "A" / "IMG_2.JPG", seed=2)
    uuids.update({"Card": "U1", "Card 1": "U2"})
    conn = db.connect("t")
    scanner.add_root(conn, vols / "Card" / "A")
    scanner.add_root(conn, vols / "Card 1" / "A")
    scanner.scan(conn)
    # Plugged in the other way round: each drive now has the other's name.
    os.rename(vols / "Card", vols / "swap")
    os.rename(vols / "Card 1", vols / "Card")
    os.rename(vols / "swap", vols / "Card 1")
    uuids.update({"Card": "U2", "Card 1": "U1"})
    volumes._cache.clear()
    rep = scanner.scan(conn)
    assert len(rep.roots_moved) == 2 and not rep.missing and not rep.roots_wrong_drive
    where = {stem: path for stem, path in conn.execute("SELECT s.stem, f.path FROM files f JOIN shots s ON s.id=f.shot_id")}
    assert where["IMG_1"].startswith(str(vols / "Card 1")) and where["IMG_2"].startswith(str(vols / "Card" / "A"))


def test_a_drive_unplugged_mid_scan_marks_nothing_missing(env, monkeypatch):
    from joeylab import scanner

    _, lib = env
    for i in range(3):
        make_jpeg(lib / "card" / f"IMG_{i}.JPG", seed=i)
    add_root(lib / "card")
    conn, _ = run_all()
    real = scanner._walk

    def walk_then_unplug(root, *a, **k):
        real(root, *a, **k)
        shutil.move(root, str(lib / "unplugged"))           # gone by the end of the walk
    monkeypatch.setattr(scanner, "_walk", walk_then_unplug)
    rep = scanner.scan(conn)
    assert str(lib / "card") in rep.roots_unavailable and not rep.missing
    assert conn.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0] == 3


def test_a_file_that_vanishes_before_analysis_is_tried_again(env):
    from joeylab import analysis, db, scanner

    _, lib = env
    make_jpeg(lib / "IMG_0.JPG", seed=0)
    add_root(lib)
    conn = db.connect("t")
    scanner.scan(conn)
    os.rename(lib / "IMG_0.JPG", lib.parent / "IMG_0.JPG")     # gone between scan and analysis
    analysis.run(conn, "t", use_faces=False, workers=1)
    r = conn.execute("SELECT error, error_fp FROM shots").fetchone()
    assert r["error"] and r["error_fp"] is None                 # not written off: retried next time


# ---------------------------------------------------------------- L3 events named by what they show

def test_events_without_a_folder_name_are_named_by_what_they_show(env, monkeypatch):
    import numpy as np

    from joeylab import events, scenes

    _, lib = env
    for i in range(6):
        jpeg_at(lib / "SD CARD 20250830" / "DCIM" / "100_FUJI" / f"IMG_{i}.JPG", seed=i, when=f"2025:08:30 1{i}:00:00")
        jpeg_at(lib / "SD CARD 20250915" / "DCIM" / "100_FUJI" / f"IMG_{i}.JPG", seed=10 + i, when=f"2025:09:15 1{i}:00:00")
        jpeg_at(lib / "SD CARD 20251001" / "DCIM" / "100_FUJI" / f"IMG_{i}.JPG", seed=20 + i, when=f"2025:10:01 1{i}:00:00")
    add_root(lib)
    conn, _ = run_all()
    rng = np.random.default_rng(0)
    M = np.linalg.qr(rng.normal(size=(768, 5)))[0].T.astype(np.float32)       # unrelated descriptions
    monkeypatch.setattr(scenes, "KINDS", [("Dance performance", []), ("Wedding", []), ("Beach", [])])
    # Two descriptions of the beach (the closer one counts), then one of nothing in particular.
    monkeypatch.setattr(scenes, "kind_vectors", lambda: (M, np.array([0, 1, 2, 2, 3])))
    conn.execute("CREATE TABLE IF NOT EXISTS search_vecs (shot_id INTEGER PRIMARY KEY, src TEXT, vecs BLOB)")
    for sid, d in conn.execute("SELECT id, dir FROM shots").fetchall():
        # The first shoot looks like a dance performance, the second a beach (its second description),
        # the third like nothing in particular.
        k = 0 if "20250830" in d else 3 if "20250915" in d else 4
        v = M[k] + rng.normal(scale=0.02, size=768)
        crops = np.tile(v / np.linalg.norm(v), (10, 1)).astype(np.float16)
        conn.execute("INSERT INTO search_vecs VALUES (?, 'x', ?)", (sid, crops.tobytes()))
    conn.commit()
    events.run(conn)
    names = sorted(r[0] for r in conn.execute("SELECT name FROM events WHERE kind='event'"))
    assert len(names) == 3
    assert [n.split(" · ")[0] for n in names if not n[0].isdigit()] == ["Beach", "Dance performance"]


def test_removing_or_merging_a_keyword_can_be_undone(env):
    from joeylab import keywords, library

    _, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    a, b, c = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    climb = keywords.ensure(conn, "Climbing")
    boulder = keywords.ensure(conn, "Bouldering")
    keywords.assign(conn, [a, b], add=[climb])
    keywords.assign(conn, [c], add=[boulder])

    r = keywords.delete(conn, climb)
    assert r["removed_from"] == 2 and "Climbing" not in {k["path"] for k in keywords.tree(conn)}
    assert library.undo(conn, batch=r["batch"])
    assert {k["id"] for k in keywords.of_shot(conn, a)} == {climb}
    assert "Climbing" in {k["path"] for k in keywords.tree(conn)}      # back in the tree, same id

    r = keywords.merge(conn, boulder, climb)
    assert r["moved"] == 1 and {k["id"] for k in keywords.of_shot(conn, c)} == {climb}
    library.undo(conn, batch=r["batch"])
    assert {k["id"] for k in keywords.of_shot(conn, c)} == {boulder}
    assert "Bouldering" in {k["path"] for k in keywords.tree(conn)}
    # A removed keyword's id isn't handed to the next new one.
    keywords.delete(conn, boulder)
    assert keywords.ensure(conn, "Hiking") not in (climb, boulder)


def test_bulk_changes_everything_matching_not_just_whats_loaded(env):
    from joeylab import keywords, library
    from joeylab.web.server import create_app

    _, lib = env
    for i in range(5):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    library.set_decision(conn, ids[0], rating=5)
    c = TestClient(create_app())
    unrated = {"status": "undecided"}
    assert c.post("/api/p/t/bulk", json={"filter": unrated, "count_only": True}).json()["n"] == 4
    r = c.post("/api/p/t/bulk", json={"filter": unrated, "rating": 2, "what": "2★"}).json()
    assert r["n"] == 4 and r["batch"]
    assert {x[0] for x in conn.execute("SELECT rating FROM decisions")} == {5, 2}
    assert c.post("/api/p/t/undo", json={"batch": r["batch"]}).json()["undone"]
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE rating=2").fetchone()[0] == 0

    r = c.post("/api/p/t/bulk", json={"filter": {}, "keywords_add": ["Trip › Cornwall"]}).json()
    assert r["n"] == 5
    cornwall = keywords.find(conn, "Cornwall")
    assert all(cornwall in {k["id"] for k in keywords.of_shot(conn, i)} for i in ids)
    r = c.post("/api/p/t/bulk", json={"filter": {}, "keywords_remove": [cornwall]}).json()
    assert r["n"] == 5 and not keywords.of_shot(conn, ids[0])
    assert c.post("/api/p/t/bulk", json={"filter": {}}).status_code == 400
    assert c.post("/api/p/t/bulk", json={"filter": {}, "rating": 9}).status_code == 400


def test_jump_list_names_people_keywords_folders_and_years(env):
    from joeylab import keywords
    from joeylab.web.server import create_app

    _, lib = env
    (lib / "Cornwall").mkdir(parents=True, exist_ok=True)
    jpeg_at(lib / "Cornwall" / "IMG_0.JPG", 0, "2024:03:05 10:00:00")
    add_root(lib)
    conn, _ = run_all()
    keywords.assign(conn, [conn.execute("SELECT id FROM shots").fetchone()[0]], add=[keywords.ensure(conn, "Trips > Cornwall")])
    d = TestClient(create_app()).get("/api/p/t/jump").json()
    assert {"Trips", "Trips › Cornwall"} <= {k["path"] for k in d["keywords"]}
    assert any(f["path"].endswith("/Cornwall") for f in d["folders"])
    assert d["years"] and all(len(y["year"]) == 4 for y in d["years"])
    assert not any(e["name"][0].isdigit() for e in d["events"])          # date-only names are left to the years


def test_search_vectors_live_in_one_mapped_file_reused_after_a_restart(env):
    import numpy as np

    from joeylab import config, db, embeddings, search

    _, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    search.ensure_schema(conn)
    rng = np.random.default_rng(0)
    want = {}
    for sid, fp in conn.execute("SELECT id, analyzed_fp FROM shots").fetchall():
        v = rng.standard_normal((search.N_CROPS, 768)).astype(np.float16)
        want[sid] = v
        conn.execute("INSERT OR REPLACE INTO search_vecs(shot_id, src, vecs) VALUES (?,?,?)", (sid, f"{fp}:{search.INDEX_VERSION}", v.tobytes()))
    conn.commit()
    search._MATRIX.clear()
    ids, mat = search._matrix(conn)
    assert isinstance(mat, np.memmap) and mat.dtype == np.float16 and mat.shape == (3 * search.N_CROPS, 768)
    for k, sid in enumerate(ids):
        assert np.array_equal(mat[k * search.N_CROPS : (k + 1) * search.N_CROPS], want[int(sid)])
    files = sorted(p.name for p in (config.HOME / "cache" / "search").iterdir())
    assert len(files) == 2

    # A restart opens the same file without reading the catalog's vectors again.
    search._MATRIX.clear()
    conn.execute("UPDATE search_vecs SET vecs = zeroblob(length(vecs))")   # would show if it re-read
    ids2, mat2 = search._matrix(conn)
    assert np.array_equal(np.asarray(mat2), np.asarray(mat))

    # Re-indexing makes a new file and drops the old one.
    db.bump(conn, "search_gen")
    conn.commit()
    ids3, mat3 = search._matrix(conn)
    assert not np.asarray(mat3).any()
    assert len(list((config.HOME / "cache" / "search").iterdir())) == 2 and files != sorted(p.name for p in (config.HOME / "cache" / "search").iterdir())
    frames = embeddings.whole_frame(conn)
    assert len(frames) == 3 and int(ids3[0]) in frames and frames[int(ids3[0])].dtype == np.float32


def test_overview_is_kept_until_the_catalog_changes(env):
    from joeylab.web.server import create_app

    _, lib = env
    for i in range(2):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    sid = conn.execute("SELECT id FROM shots").fetchone()[0]
    c = TestClient(create_app())
    a = c.get("/api/p/t/overview").json()
    assert c.get("/api/p/t/overview").json()["counts"] == a["counts"]
    assert c.post("/api/p/t/decide", json={"items": [{"id": sid, "pick": 1}]}).status_code == 200
    assert c.get("/api/p/t/overview").json()["counts"]["picks"] == a["counts"]["picks"] + 1
    conn.execute("UPDATE decisions SET pick=0")          # another program (the CLI) changes it
    conn.commit()
    assert c.get("/api/p/t/overview").json()["counts"]["picks"] == a["counts"]["picks"]


def test_time_order_comes_from_an_index_kept_by_triggers(env):
    from joeylab import library
    from joeylab.library import Filter
    from joeylab.web.server import create_app

    _, lib = env
    jpeg_at(lib / "b.jpg", 1, "2024:05:01 10:00:00")
    jpeg_at(lib / "a.jpg", 2, "2024:05:02 10:00:00")
    make_jpeg(lib / "c.jpg", seed=3)                       # no date: last
    add_root(lib)
    conn, _ = run_all()
    ids = {r["stem"]: r["id"] for r in conn.execute("SELECT id, stem FROM shots")}
    order = lambda sort: [r["id"] for r in library.page(conn, Filter(), sort, 0, 50)[0]]
    assert order("time") == [ids["b"], ids["a"], ids["c"]]
    assert order("time_desc") == [ids["a"], ids["b"], ids["c"]]
    conn.execute("UPDATE shots SET eff_ts = eff_ts - 10 * 86400 WHERE id=?", (ids["a"],))   # a clock fix
    assert order("time") == [ids["a"], ids["b"], ids["c"]]
    plan = " ".join(r[3] for r in conn.execute(
        f"EXPLAIN QUERY PLAN SELECT * FROM (SELECT {library.LIGHT_COLS} {library.LIGHT_FROM} WHERE s.status='present')"
        f" ORDER BY {library.OUTER_SORTS['time']} LIMIT 10"))
    assert "shots_sort" in plan and "TEMP B-TREE" not in plan
    conn.commit()
    r = TestClient(create_app()).get("/api/p/t/overview", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"


def test_taste_retrains_once_after_a_run_of_decisions(env, monkeypatch):
    from joeylab.web import server

    calls = []
    bg = server.Background(type("J", (), {"running": {}})())
    monkeypatch.setattr(bg, "_go", lambda key, project, fn, wait=0.0: calls.append(key))
    monkeypatch.setattr(server.Background, "QUIET_S", 0.2)
    monkeypatch.setattr(server.Background, "MIN_GAP_S", 0.0)
    for _ in range(5):
        bg.decided("t", 10)                  # 50 decisions in quick succession
        time.sleep(0.05)
    time.sleep(0.5)
    assert calls == ["retrain:t"]
    bg.decided("t", 3)
    time.sleep(0.4)
    assert calls == ["retrain:t"]            # not yet 20 more


def test_taste_trains_on_an_even_sample_of_a_big_archive(env, monkeypatch):
    from joeylab import taste

    monkeypatch.setattr(taste, "MAX_EACH", 15)
    _, lib = env
    for i in range(40):
        make_jpeg(lib / f"IMG_{i:02d}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    from joeylab import library

    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    library.decide(conn, [{"id": i, "pick": 1 if k % 2 else -1} for k, i in enumerate(ids)], what="test")
    meta = taste.train_all()
    assert meta["positives"] == 20 and meta["negatives"] == 20 and meta["sampled"] == 30


def test_new_ids_never_collide_when_requests_race(env):
    import threading

    from joeylab import db

    db.connect("t").close()
    ids, errors = [], []
    start = threading.Barrier(6)

    def name_someone(k):
        conn = db.connect("t")
        start.wait()
        try:
            for j in range(10):
                pid = db.new_id(conn, "people")
                conn.execute("INSERT INTO people(id, name, kind, hidden) VALUES (?, ?, 'person', 0)", (pid, f"P{k}-{j}"))
                conn.commit()
                ids.append(pid)
        except Exception as e:      # noqa: BLE001 - the test reports any failure
            errors.append(e)

    threads = [threading.Thread(target=name_someone, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(ids) == 60 and len(set(ids)) == 60


def test_a_folder_on_the_macs_own_disk_is_not_called_read_only(monkeypatch):
    import os
    import subprocess

    from joeylab import integrity

    mount = ("/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
             "/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse)\n"
             "/dev/disk5s2 on /Volumes/Photo Drive (exfat, local, nodev, nosuid, noowners, noatime)\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type("R", (), {"stdout": mount})())
    monkeypatch.setattr(os.path, "realpath", lambda p: p)
    monkeypatch.setattr(os.path, "exists", lambda p: p.startswith("/System/Volumes/Data/Users"))
    monkeypatch.setattr(os, "statvfs", lambda p: type("V", (), {"f_flag": os.ST_RDONLY if p == "/" else 0})())
    home = integrity.mount_info("/Users/someone/Pictures/2024")
    assert home["mount"] == "/System/Volumes/Data" and home["read_only"] is False and not home["external"]
    drive = integrity.mount_info("/Volumes/Photo Drive/2026")
    assert drive["mount"] == "/Volumes/Photo Drive" and drive["fs"] == "exfat" and drive["noatime"] and drive["external"]
    assert integrity.mount_info("/")["read_only"] is True


def test_metadata_is_read_and_saved_a_chunk_at_a_time(env, monkeypatch):
    """A big drive's metadata shows progress and keeps what was read if the update stops part-way."""
    from joeylab import db, exif, scanner

    _, lib = env
    for i in range(7):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn = db.connect("t")
    monkeypatch.setattr(scanner, "META_CHUNK", 3)
    calls, seen = [], []
    real = exif.read_many

    def read_many(paths, *a, **k):
        calls.append(len(paths))
        if len(calls) == 3:                               # the second chunk of photos fails: the update stops
            raise KeyboardInterrupt
        return real(paths, *a, **k)

    monkeypatch.setattr(exif, "read_many", read_many)
    import pytest

    with pytest.raises(KeyboardInterrupt):
        scanner.scan(conn, progress=lambda what, done, total: seen.append((done, total)))
    saved = conn.execute("SELECT SUM(exif_done) FROM files").fetchone()[0]
    assert saved == 3                                     # the first chunk was kept
    assert (0, 7) in seen and (3, 7) in seen              # the count moved


def test_shifting_photos_times_moves_them_keeps_them_moved_and_undoes(env):
    """Two bodies of one camera model, one clock 12 hours out: shift that body's photos, not the camera."""
    from conftest import jpeg_at

    from joeylab import clocksync, dating, db, library, scanner

    _, lib = env
    for i in range(3):
        jpeg_at(lib / "night" / f"IMG_{i}.JPG", i, f"2025:10:01 22:0{i}:00", size=(300, 200))
        jpeg_at(lib / "day" / f"IMG_{i + 5}.JPG", i + 5, f"2025:10:02 10:1{i}:00", size=(300, 200))
    add_root(lib)
    conn, _ = run_all()
    night = [r[0] for r in conn.execute("SELECT id FROM shots WHERE dir LIKE '%night' ORDER BY id")]
    before = {r[0]: r[1] for r in conn.execute("SELECT id, eff_ts FROM shots")}
    r = dating.shift_times(conn, night, 12 * 3600)
    assert sorted(r["changed"]) == night and r["undated"] == 0
    after = {r[0]: r[1] for r in conn.execute("SELECT id, eff_ts FROM shots")}
    assert all(after[i] == before[i] + 12 * 3600 for i in night)
    assert all(after[i] == before[i] for i in before if i not in night)
    # In time order the shifted photos now sit with the day's: 22:00 + 12 h is 10:00 the next morning.
    order = [s["id"] for s in library.shots(conn, library.Filter(), sort="time")]
    assert order.index(night[0]) < order.index(max(set(before) - set(night)))
    # An update keeps the shift, and a camera correction adds to it.
    scanner.scan(conn)
    assert {r[0]: r[1] for r in conn.execute("SELECT id, eff_ts FROM shots")} == after
    cam = conn.execute("SELECT camera, body_serial FROM shots WHERE id=?", (night[0],)).fetchone()
    from joeylab.grouping import camera_key

    clocksync.set_offset(conn, camera_key(cam[0], cam[1]), 60)
    assert conn.execute("SELECT eff_ts FROM shots WHERE id=?", (night[0],)).fetchone()[0] == before[night[0]] + 12 * 3600 + 60
    clocksync.set_offset(conn, camera_key(cam[0], cam[1]), 0)
    # Undo takes the shift back; redo brings it again.
    library.undo(conn, batch=r["batch"])
    assert {i: conn.execute("SELECT eff_ts FROM shots WHERE id=?", (i,)).fetchone()[0] for i in night} == {i: before[i] for i in night}
    assert dating.shifts(conn, night) == {}
    library.redo(conn)
    assert all(conn.execute("SELECT eff_ts FROM shots WHERE id=?", (i,)).fetchone()[0] == before[i] + 12 * 3600 for i in night)
    with __import__("pytest").raises(ValueError):
        dating.shift_times(conn, night, 0)
