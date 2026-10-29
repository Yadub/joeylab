"""Moves and renames never join two photos into one, follow a file moved back to its own drive,
leave folders given to another library alone, and one move that can't be finished after a stop
never holds up the others."""

from __future__ import annotations

import errno
import os
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import add_root, make_jpeg, run_all


@pytest.fixture
def fake_trash(tmp_path, monkeypatch):
    from joeylab import cleanup

    bin_ = tmp_path / "trash"
    bin_.mkdir()

    def move(path):
        dest = bin_ / f"{len(list(bin_.iterdir()))}-{os.path.basename(path)}"
        os.rename(path, dest)
        return str(dest)

    monkeypatch.setattr(cleanup, "move_to_trash", move)
    return bin_


def _png(path, seed):
    make_jpeg(path.with_suffix(".tmp.jpg"), seed=seed)
    Image.open(path.with_suffix(".tmp.jpg")).save(path)
    os.remove(path.with_suffix(".tmp.jpg"))
    t = time.time() - 60
    os.utime(path, (t, t))


def _shots_in(conn, folder) -> int:
    return conn.execute("SELECT COUNT(*) FROM shots WHERE dir=? AND status='present'", (str(folder),)).fetchone()[0]


def test_a_move_never_joins_two_photos_by_their_name(env):
    from joeylab import moves, scanner

    _, lib = env
    _png(lib / "a" / "IMG_1.png", seed=1)                         # a screenshot...
    make_jpeg(lib / "b" / "IMG_1.JPG", seed=2)                    # ...and another camera's photo of the same name
    add_root(lib)
    conn, _ = run_all()
    sid = conn.execute("SELECT id FROM shots WHERE dir=?", (str(lib / "a"),)).fetchone()[0]
    plan = moves.plan_move_photos(conn, "t", [sid], str(lib / "b"))
    assert not plan.steps and plan.as_dict()["conflict_examples"] == [{"name": "IMG_1.png", "keep_both": "IMG_1 2.png"}]
    with pytest.raises(moves.Refused):
        moves.run(conn, plan)                                     # skipped: nothing moves
    res = moves.run(conn, moves.plan_move_photos(conn, "t", [sid], str(lib / "b")), keep_both=True)
    assert res["photos"] == 1 and sorted(os.listdir(lib / "b")) == ["IMG_1 2.png", "IMG_1.JPG"]
    assert _shots_in(conn, lib / "b") == 2
    scanner.scan(conn)
    assert _shots_in(conn, lib / "b") == 2


def test_a_pattern_rename_never_gives_two_photos_one_name(env):
    from joeylab import moves, scanner

    _, lib = env
    make_jpeg(lib / "a" / "IMG_1.JPG", seed=1)
    _png(lib / "a" / "IMG_2.png", seed=2)
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    plan = moves.plan_rename_photos(conn, "t", ids, "Trip")          # no counter: both would be "Trip"
    assert sorted(p["to"] for p in plan.preview) == ["Trip 2.png", "Trip.JPG"] or \
        sorted(p["to"] for p in plan.preview) == ["Trip 2.JPG", "Trip.png"]
    moves.run(conn, plan, keep_both=True)
    assert _shots_in(conn, lib / "a") == 2
    scanner.scan(conn)
    assert _shots_in(conn, lib / "a") == 2


def test_moving_back_from_another_drive_puts_the_file_back_under_its_own_library_folder(env, fake_trash, monkeypatch):
    from joeylab import db, moves, scanner

    _, lib = env
    make_jpeg(lib / "A" / "IMG_1.JPG", seed=1)
    (lib / "B").mkdir()
    add_root(lib / "A")
    add_root(lib / "B")
    conn, _ = run_all()
    root = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM roots")}
    monkeypatch.setattr(moves, "same_drive", lambda a, b: False)       # B stands in for another drive
    sid = conn.execute("SELECT id FROM shots").fetchone()[0]
    res = moves.run(conn, moves.plan_move_photos(conn, "t", [sid], str(lib / "B")))
    root_of = lambda: conn.execute("SELECT root_id FROM files WHERE shot_id=?", (sid,)).fetchone()[0]   # noqa: E731
    assert root_of() == root[str(lib / "B")]
    back = moves.plan_back(conn, "t", res["id"])
    assert back.steps[0].put_back                                       # the original comes back from the Trash
    moves.run(conn, back)
    assert (lib / "A" / "IMG_1.JPG").exists() and root_of() == root[str(lib / "A")]
    assert db.connect("t").execute("SELECT path FROM files WHERE shot_id=?", (sid,)).fetchone()[0] == str(lib / "A" / "IMG_1.JPG")
    rep = scanner.scan(conn)
    assert (rep.added, rep.moved, rep.missing) == (0, 0, 0)


def test_a_folder_given_to_another_library_is_never_renamed_or_moved_here(env):
    from joeylab import moves

    _, lib = env
    make_jpeg(lib / "Weddings" / "Smith" / "IMG_1.JPG", seed=1)
    make_jpeg(lib / "Weddings" / "Jones" / "IMG_2.JPG", seed=2)
    (lib / "Other").mkdir()
    add_root(lib)
    conn, _ = run_all()
    conn.execute("INSERT INTO excluded_folders(folder, library, added) VALUES (?, 'smith', 0)", (str(lib / "Weddings" / "Smith"),))
    conn.commit()
    holds = moves.plan_rename_folder(conn, "t", str(lib / "Weddings"), "Shoots")
    assert holds.refused and "holds" in holds.refused and "the library “smith”" in holds.refused and not holds.steps
    inside = moves.plan_rename_folder(conn, "t", str(lib / "Weddings" / "Smith"), "Smith 2024")
    assert inside.refused and "was moved to the library “smith”" in inside.refused
    moved = moves.plan_move_folder(conn, "t", str(lib / "Weddings"), str(lib / "Other"))
    assert moved.refused and "holds" in moved.refused
    ok = moves.plan_rename_folder(conn, "t", str(lib / "Weddings" / "Jones"), "Jones 2024")
    assert not ok.refused
    # A folder renamed before its inside was given away can't be moved back over it either.
    res = moves.run(conn, moves.plan_rename_folder(conn, "t", str(lib / "Other"), "Others"))
    (lib / "Others" / "Given").mkdir()
    conn.execute("INSERT INTO excluded_folders(folder, library, added) VALUES (?, 'smith', 0)", (str(lib / "Others" / "Given"),))
    conn.commit()
    back = moves.plan_back(conn, "t", res["id"])
    assert back.refused and "holds" in back.refused and (lib / "Others").is_dir()


class Crash(BaseException):
    """The process stopping dead."""


def test_one_move_that_cant_be_finished_never_holds_up_the_others(env, monkeypatch):
    from joeylab import moves
    from joeylab.web.server import create_app

    _, lib = env
    for i in (1, 2):
        make_jpeg(lib / "a" / f"IMG_{i}.JPG", seed=i)
        (lib / "a" / f"IMG_{i}.xmp").write_text("<x:xmpmeta/>")
    (lib / "b").mkdir()
    add_root(lib)
    conn, _ = run_all()
    real_rename, recover = moves.rename_excl, moves.recover
    monkeypatch.setattr(moves, "recover", lambda c, p, stuck=None: [])
    ids = []
    for stem in ("IMG_1", "IMG_2"):                                   # two moves, each stopped half way
        calls = []

        def dies_after_one(a, b, calls=calls):
            if calls:
                raise Crash()
            calls.append(a)
            real_rename(a, b)

        monkeypatch.setattr(moves, "rename_excl", dies_after_one)
        sid = conn.execute("SELECT id FROM shots WHERE stem=?", (stem,)).fetchone()[0]
        with pytest.raises(Crash):
            moves.run(conn, moves.plan_move_photos(conn, "t", [sid], str(lib / "b")))
        conn.rollback()
        monkeypatch.setattr(moves, "rename_excl", real_rename)
        ids.append(next(m["id"] for m in moves.history("t") if m["state"] == "stopped" and m["id"] not in ids))
        time.sleep(1.1)                                               # a log name per second
    monkeypatch.setattr(moves, "recover", recover)
    real_one = moves._recover

    def drive_gone(c, log):
        if log.id == ids[0]:
            raise OSError(errno.EIO, "Input/output error")
        return real_one(c, log)

    monkeypatch.setattr(moves, "_recover", drive_gone)
    stuck: list = []
    out = moves.recover(conn, "t", stuck)
    assert [o["id"] for o in out] == [ids[1]] and stuck == [{"id": ids[0], "label": stuck[0]["label"], "error": "Input/output error"}]
    states = {m["id"]: m["state"] for m in moves.history("t")}
    assert states[ids[0]] == "stopped" and states[ids[1]] != "stopped"
    # A new move still goes ahead, and the app says which one is stuck.
    sid = conn.execute("SELECT id FROM shots WHERE stem='IMG_2'").fetchone()[0]
    assert moves.run(conn, moves.plan_rename_photos(conn, "t", [sid], "Kept"))["photos"] == 1
    r = TestClient(create_app()).post("/api/p/t/moves/recover", json={})
    assert r.status_code == 409 and "couldn't finish" in r.json()["error"]
    monkeypatch.setattr(moves, "_recover", real_one)
    assert [o["id"] for o in moves.recover(conn, "t")] == [ids[0]]


def test_a_photo_renamed_in_case_only_isnt_kept_apart_from_its_own_missing_raw(env):
    from joeylab import moves, scanner

    _, lib = env
    make_jpeg(lib / "a" / "img_1.JPG", seed=1)
    (lib / "a" / "img_1.CR3").write_bytes(os.urandom(4000))
    t = time.time() - 60
    os.utime(lib / "a" / "img_1.CR3", (t, t))
    add_root(lib)
    conn, _ = run_all()
    (lib / "a" / "img_1.CR3").unlink()                              # its RAW went to the Trash
    scanner.scan(conn)
    sid = conn.execute("SELECT id FROM shots WHERE stem='img_1'").fetchone()[0]
    plan = moves.plan_rename_photos(conn, "t", [sid], "IMG_1")
    assert [s.dst for s in plan.steps] == [str(lib / "a" / "IMG_1.JPG")] and not plan.conflicts
