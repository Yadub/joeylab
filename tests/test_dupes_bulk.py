"""Rejecting duplicate extras a whole folder group (or everything) at a time, not just what's on screen."""

from __future__ import annotations

import shutil

from fastapi.testclient import TestClient

from conftest import add_root, make_jpeg, run_all


def test_reject_all_extras_whole_sets_keeps_kept_copies_and_undoes_in_one_step(env):
    from joeylab import library
    from joeylab.web.server import create_app

    _, lib = env
    for i in range(5):
        make_jpeg(lib / "keep" / f"IMG_{i}.JPG", seed=i)
        (lib / "copies").mkdir(parents=True, exist_ok=True)
        shutil.copy2(lib / "keep" / f"IMG_{i}.JPG", lib / "copies" / f"IMG_{i}.JPG")
    make_jpeg(lib / "keep" / "ONLY.JPG", seed=99)
    add_root(lib)
    conn, _ = run_all()
    sets = [r[0] for r in conn.execute("SELECT id FROM dup_sets WHERE kind='exact'")]
    assert len(sets) == 5
    shot = lambda folder, i: conn.execute("SELECT id FROM shots WHERE dir LIKE ? AND stem=?", (f"%/{folder}", f"IMG_{i}")).fetchone()[0]
    kept_ids = {r[0] for r in conn.execute("SELECT shot_id FROM dup_members WHERE keep=1")}
    extra_ids = {r[0] for r in conn.execute("SELECT shot_id FROM dup_members WHERE keep=0")}
    an_extra = sorted(extra_ids)[0]
    library.decide(conn, [{"id": an_extra, "rating": 4}])                     # a star given to the copy, not the keeper
    c = TestClient(create_app())

    dry = c.post("/api/p/t/dupes/reject-extras", json={"set_ids": sets, "dry": True}).json()
    assert dry["sets"] == 5 and dry["to_reject"] == 5 and dry["rejected"] == 0
    assert not conn.execute("SELECT COUNT(*) FROM decisions WHERE pick=-1").fetchone()[0]       # a dry run changes nothing

    r = c.post("/api/p/t/dupes/reject-extras", json={"set_ids": sets}).json()
    assert r["rejected"] == 5 and r["combined"] == 1 and r["batch"]
    rejected = {x[0] for x in conn.execute("SELECT shot_id FROM decisions WHERE pick=-1")}
    assert rejected == extra_ids and not (rejected & kept_ids)
    keeper_of_starred = conn.execute(
        "SELECT dk.shot_id FROM dup_members dm JOIN dup_members dk ON dk.set_id=dm.set_id AND dk.keep=1 WHERE dm.shot_id=?",
        (an_extra,)).fetchone()[0]
    assert conn.execute("SELECT rating FROM decisions WHERE shot_id=?", (keeper_of_starred,)).fetchone()[0] == 4
    only = conn.execute("SELECT id FROM shots WHERE stem='ONLY'").fetchone()[0]
    assert only not in rejected

    again = c.post("/api/p/t/dupes/reject-extras", json={"set_ids": sets, "dry": True}).json()
    assert again["to_reject"] == 0 and again["already"] == 5

    assert c.post("/api/p/t/undo", json={"batch": r["batch"]}).status_code == 200            # one Undo, everything back
    assert not conn.execute("SELECT COUNT(*) FROM decisions WHERE pick=-1").fetchone()[0]
    assert (conn.execute("SELECT rating FROM decisions WHERE shot_id=?", (keeper_of_starred,)).fetchone() or [0])[0] in (0, None)
    assert c.post("/api/p/t/dupes/reject-extras", json={"set_ids": []}).status_code == 400


def test_rejected_duplicate_copies_teach_taste_nothing(env):
    from joeylab import dupes, library, taste

    _, lib = env
    for i in range(3):
        make_jpeg(lib / "keep" / f"IMG_{i}.JPG", seed=i)
        (lib / "copies").mkdir(parents=True, exist_ok=True)
        shutil.copy2(lib / "keep" / f"IMG_{i}.JPG", lib / "copies" / f"IMG_{i}.JPG")
    make_jpeg(lib / "keep" / "BLURRY.JPG", seed=50)
    add_root(lib)
    conn, _ = run_all()
    sets = [r[0] for r in conn.execute("SELECT id FROM dup_sets")]
    dupes.reject_extras(conn, sets)
    blurry = conn.execute("SELECT id FROM shots WHERE stem='BLURRY'").fetchone()[0]
    library.decide(conn, [{"id": blurry, "pick": -1}])                      # a real "no"
    labelled = {r["id"] for r in taste._rows(conn, labelled=True)}
    assert labelled == {blurry}


def _copied_library(lib):
    for i in range(2):
        make_jpeg(lib / "SD Card" / f"IMG_{i}.JPG", seed=i)
        (lib / "Wedding").mkdir(parents=True, exist_ok=True)
        shutil.copy2(lib / "SD Card" / f"IMG_{i}.JPG", lib / "Wedding" / f"IMG_{i}.JPG")


def _mark_up(conn, sid):
    from joeylab import keywords, library, portable

    aid = portable.create_album(conn, "Best") if hasattr(portable, "create_album") else None
    if aid is None:
        aid = conn.execute("INSERT INTO albums(name, kind) VALUES ('Best', 'manual')").lastrowid
    portable.add_to_album(conn, aid, [sid])
    keywords.assign(conn, [sid], add=[keywords.ensure(conn, "Dance")])
    conn.execute("INSERT INTO viewer_decisions(viewer, shot_id, fav, updated) VALUES ('owner', ?, 1, 0)", (sid,))
    conn.execute("INSERT INTO comments(shot_id, viewer, text, ts) VALUES (?, 'owner', 'lovely', 0)", (sid,))
    library.decide(conn, [{"id": sid, "rating": 4}])
    library.decide(conn, [{"id": sid, "pick": -1}])
    conn.commit()
    return aid


def test_the_copy_that_stays_takes_over_albums_keywords_favourites_comments_and_stars(env, tmp_path):
    from joeylab import cleanup, keywords

    _, lib = env
    _copied_library(lib)
    add_root(lib)
    conn, _ = run_all()
    gone = conn.execute("SELECT id FROM shots WHERE dir LIKE '%/SD Card' AND stem='IMG_0'").fetchone()[0]
    stays = conn.execute("SELECT id FROM shots WHERE dir LIKE '%/Wedding' AND stem='IMG_0'").fetchone()[0]
    aid = _mark_up(conn, gone)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    items = cleanup.plan(conn, [str(lib / "SD Card" / "IMG_0.JPG")])
    assert items[0].category == cleanup.IDENTICAL
    cleanup.trash_files(items, items, "t-files", mover=lambda p: shutil.move(p, bin_ / "x.jpg"), conn=conn)
    assert {r[0] for r in conn.execute("SELECT shot_id FROM album_items WHERE album_id=?", (aid,))} == {stays}
    assert {k["path"] for k in keywords.of_shot(conn, stays)} == {"Dance"}
    assert conn.execute("SELECT fav FROM viewer_decisions WHERE shot_id=?", (stays,)).fetchone()[0] == 1
    assert conn.execute("SELECT shot_id FROM comments").fetchone()[0] == stays
    d = conn.execute("SELECT rating, pick FROM decisions WHERE shot_id=?", (stays,)).fetchone()
    assert d["rating"] == 4 and (d["pick"] or 0) != -1                       # stars carried, the reject not
    assert {k["path"] for k in keywords.of_shot(conn, gone)} == {"Dance"}    # nothing taken off: Put Back loses nothing


def test_repair_lets_earlier_moves_catch_up_and_can_run_twice(env, tmp_path):
    from joeylab import cleanup

    _, lib = env
    _copied_library(lib)
    add_root(lib)
    conn, _ = run_all()
    gone = conn.execute("SELECT id FROM shots WHERE dir LIKE '%/SD Card' AND stem='IMG_1'").fetchone()[0]
    stays = conn.execute("SELECT id FROM shots WHERE dir LIKE '%/Wedding' AND stem='IMG_1'").fetchone()[0]
    aid = _mark_up(conn, gone)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    items = cleanup.plan(conn, [str(lib / "SD Card" / "IMG_1.JPG")])
    cleanup.trash_files(items, items, "t-files", mover=lambda p: shutil.move(p, bin_ / "y.jpg"))      # the old way: no carry
    assert {r[0] for r in conn.execute("SELECT shot_id FROM album_items WHERE album_id=?", (aid,))} == {gone}
    r = cleanup.repair_from_logs(conn, "t")
    assert r["files"] == 1 and r["decisions"] == 1
    assert {r[0] for r in conn.execute("SELECT shot_id FROM album_items WHERE album_id=?", (aid,))} == {stays}
    again = cleanup.repair_from_logs(conn, "t")
    assert again["decisions"] == 0
    assert conn.execute("SELECT COUNT(*) FROM album_items WHERE album_id=?", (aid,)).fetchone()[0] == 1


def test_a_photo_with_a_unique_file_is_never_rejected_as_an_extra(env):
    import os
    import time

    from PIL import Image

    from joeylab import dupes

    _, lib = env
    deep = lib / "Cards" / "2023" / "DCIM"                                    # deeper: its copy loses
    make_jpeg(deep / "IMG_1.JPG", seed=1)
    Image.open(deep / "IMG_1.JPG").save(deep / "IMG_1.TIF")                  # the photo's other file: no copy anywhere
    old = time.time() - 60
    os.utime(deep / "IMG_1.TIF", (old, old))
    (lib / "Shared").mkdir(parents=True)
    shutil.copy2(deep / "IMG_1.JPG", lib / "Shared" / "IMG_1.JPG")
    add_root(lib)
    conn, _ = run_all()
    pair = conn.execute("SELECT shot_id FROM files WHERE path LIKE '%IMG_1.TIF'").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM files WHERE shot_id=?", (pair,)).fetchone()[0] == 2
    sets = [r[0] for r in conn.execute("SELECT id FROM dup_sets")]
    extra_shot = conn.execute("SELECT shot_id FROM dup_members WHERE keep=0").fetchone()[0]
    r = dupes.reject_extras(conn, sets)
    if extra_shot == pair:                       # the JPEG in the pair is the extra copy: the photo stays
        assert r["unique_skipped"] == 1 and r["rejected"] == 0
    assert not conn.execute("SELECT 1 FROM decisions WHERE shot_id=? AND pick=-1", (pair,)).fetchone()
