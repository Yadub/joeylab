"""Moving folders to another library, the parts that protect work: time shifts go with the photos, only
photos with files under the folders go, a stopped move is never finished over newer work, and a stop
just after the other library committed its copy can still be finished."""

from __future__ import annotations

import os

import pytest

from conftest import EVERYDAY, WEDDINGS, synthetic_library


def _shot_at(conn, path) -> int | None:
    r = conn.execute("SELECT shot_id FROM files WHERE path=? AND status='present'", (str(path),)).fetchone()
    return r[0] if r else None


def test_time_shifts_go_with_folders_moved_to_another_library(env):
    """Shifted photos moved to another library keep their shifts: a camera correction there, or an update,
    puts each one at its camera's time plus the correction plus its own shift, not back on camera time."""
    from joeylab import clocksync, dating, db, scanner, split
    from joeylab.grouping import camera_key

    _, lib = env
    s = synthetic_library(lib)
    conn = s["conn"]
    shifted = [r[0] for r in conn.execute(f"SELECT id FROM shots WHERE id IN ({','.join(map(str, s['wed']))})"
                                          " AND time_src = 'exif' ORDER BY id LIMIT 5")]
    assert len(shifted) == 5
    dating.shift_times(conn, shifted, 43_200)               # one body's clock 12 h out
    conn.close()

    split.run("t", [str(lib / "Weddings")], "weddings", "shoot")
    dst, src = db.connect("weddings"), db.connect("t")
    assert dating.shifts(dst) == {sid: 43_200.0 for sid in shifted}
    assert dating.shifts(src) == {}
    clocksync.set_offset(dst, camera_key("TestCam", None), 600)
    for sid, eff, cap in dst.execute("SELECT id, eff_ts, capture_ts FROM shots WHERE time_src = 'exif'"):
        assert eff == cap + 600 + (43_200 if sid in shifted else 0), sid
    scanner._summarize_shots(dst, set(shifted))
    for sid, eff, cap in dst.execute(f"SELECT id, eff_ts, capture_ts FROM shots WHERE id IN ({','.join(map(str, shifted))})"):
        assert eff == cap + 600 + 43_200
    # Moved back, they come home with their shifts.
    dst.close()
    split.move_back(split.history("t")[0]["id"])
    assert dating.shifts(db.connect("t")) == {sid: 43_200.0 for sid in shifted}


def test_a_photo_whose_files_are_in_a_folder_that_stays_stays(env):
    """A wedding photo moved in Finder to an everyday folder, with a file of it left behind (missing) in
    the wedding folder: it stays with the everyday folder when the wedding folders move."""
    from joeylab import db, library, scanner, split

    tmp, lib = env
    s = synthetic_library(lib)
    conn = s["conn"]
    jpg = lib / WEDDINGS[0] / "IMG_0001.JPG"
    sid = _shot_at(conn, jpg)
    raw = lib / WEDDINGS[0] / "IMG_0001.CR3"
    raw.write_bytes(b"\x02" * 16 + os.urandom(4096))
    old = os.stat(jpg).st_mtime
    os.utime(raw, (old, old))
    scanner.scan(conn)
    assert _shot_at(conn, raw) == sid
    os.rename(raw, tmp / "IMG_0001.CR3")                   # to the Trash
    moved = lib / EVERYDAY[0] / "IMG_0001.JPG"
    os.rename(jpg, moved)                                   # and the JPEG into an everyday folder
    scanner.scan(conn)
    assert _shot_at(conn, moved) == sid
    assert conn.execute("SELECT dir FROM shots WHERE id=?", (sid,)).fetchone()[0] == str(lib / EVERYDAY[0])
    before = library.shot(conn, sid)
    conn.close()

    p = split.plan("t", [str(lib / "Weddings")], "weddings")
    assert p["photos"] == len(s["wed"]) - 1
    split.run("t", [str(lib / "Weddings")], "weddings", "shoot")
    src, dst = db.connect("t"), db.connect("weddings")
    assert _shot_at(src, moved) == sid and library.shot(src, sid)["pick"] == before["pick"]
    assert not dst.execute("SELECT 1 FROM files WHERE path=?", (str(moved),)).fetchone()
    assert not dst.execute("SELECT 1 FROM shots WHERE dir=?", (str(lib / EVERYDAY[0]),)).fetchone()
    rep = scanner.scan(src)
    assert (rep.added, rep.missing, rep.moved) == (0, 0, 0)


def _stop_after_the_copy(monkeypatch, *args):
    """Run a move that stops after the other library committed its copy, before the photos left."""
    from joeylab import split

    real = split._remove_from_source

    def stop(cs, chk, moved, maps):
        real(cs, chk, moved, maps)
        raise KeyboardInterrupt

    monkeypatch.setattr(split, "_remove_from_source", stop)
    with pytest.raises(KeyboardInterrupt):
        split.run(*args)
    monkeypatch.setattr(split, "_remove_from_source", real)
    return split.history(args[0])[0]


def test_finishing_a_stopped_move_never_throws_away_newer_work(env, monkeypatch):
    from joeylab import db, keywords, libraries, library, split

    _, lib = env
    s = synthetic_library(lib)
    s["conn"].close()
    weddings = str(lib / "Weddings")
    op = _stop_after_the_copy(monkeypatch, "t", [weddings], "weddings", "shoot")
    assert op["state"] == "copied"

    # Meanwhile, in the library they came from, a wedding photo gets stars.
    src = db.connect("t")
    sid = s["wed"][5]
    library.decide(src, [{"id": sid, "rating": 3}], what="stars")
    src.close()
    with pytest.raises(split.Refused, match="one of those photos has changed in “t”") as e:
        split.finish(op["id"])
    assert "remove that library" in str(e.value)
    src = db.connect("t")
    assert library.shot(src, sid)["rating"] == 3 and db.excluded_folders(src) == []
    assert split.load(op["id"])["state"] == "copied"
    # History alone counts too: undone, the stars are gone but the history has both steps.
    library.undo(src)
    src.close()
    with pytest.raises(split.Refused, match="changed in “t”"):
        split.finish(op["id"])

    # Taking the copy out of the other library closes the move with nothing moved; then it can be made again.
    libraries.remove("weddings", "weddings")
    assert split.finish(op["id"])["state"] == "failed"
    src = db.connect("t")
    assert db.excluded_folders(src) == [] and src.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == \
        len(s["wed"]) + len(s["day"])
    library.decide(src, [{"id": sid, "rating": 4}], what="stars")
    kw = keywords.ensure(src, "Late tag")
    keywords.assign(src, [sid], add=[kw], what="tag")
    src.close()
    op = split.run("t", [weddings], "weddings", "shoot")
    assert op["state"] == "done"
    dst = db.connect("weddings")
    assert library.shot(dst, sid)["rating"] == 4
    assert dst.execute("SELECT COUNT(*) FROM shot_keywords k JOIN keywords w ON w.id = k.keyword_id"
                       " WHERE k.shot_id=? AND w.name='Late tag'", (sid,)).fetchone()[0] == 1


def test_a_stopped_move_into_an_existing_library_says_how_to_keep_newer_work(env, monkeypatch, tmp_path):
    from conftest import add_root, jpeg_at, run_all
    from joeylab import db, library, split

    _, lib = env
    s = synthetic_library(lib)
    s["conn"].close()
    other = tmp_path / "family"
    jpeg_at(other / "DSC_0001.JPG", seed=600, when="2023:03:01 10:00:00")
    add_root(other, "family")
    run_all("family")[0].close()
    op = _stop_after_the_copy(monkeypatch, "t", [str(lib / "Weddings")], "family", None)
    src = db.connect("t")
    library.decide(src, [{"id": s["wed"][1], "pick": -1}, {"id": s["wed"][2], "pick": -1}], what="reject")
    src.close()
    with pytest.raises(split.Refused, match="2 of those photos have changed") as e:
        split.finish(op["id"])
    assert f"joeylab restore -p family --from {op['backups']['family']}" in str(e.value)
    # Bringing back family's catalog from before the move takes the copy out: the move closes.
    from joeylab import backup

    backup.restore("family", op["backups"]["family"])
    assert split.finish(op["id"])["state"] == "failed"
    assert db.connect("family").execute("SELECT COUNT(*) FROM shots").fetchone()[0] == 1
    assert db.connect("t").execute("SELECT COUNT(*) FROM decisions WHERE pick=-1").fetchone()[0] == 2


def test_an_unchanged_stopped_move_still_finishes(env, monkeypatch):
    from joeylab import db, split

    _, lib = env
    s = synthetic_library(lib)
    s["conn"].close()
    op = _stop_after_the_copy(monkeypatch, "t", [str(lib / "Weddings")], "weddings", "shoot")
    src = db.connect("t")
    from joeylab import scanner

    scanner.scan(src)                       # an update meanwhile changes nothing of yours
    src.close()
    assert split.finish(op["id"])["state"] == "done"
    assert {r[0] for r in db.connect("t").execute("SELECT id FROM shots")} == set(s["day"])


def test_a_stop_just_after_the_other_library_commits_can_be_finished(env, monkeypatch):
    """The other library has its copy but the move's log wasn't updated (a stop at that moment): the move
    isn't marked as failed with the photos in both libraries; it waits to be finished, and finishes."""
    from joeylab import config, db, split

    _, lib = env
    s = synthetic_library(lib)
    s["conn"].close()
    weddings = str(lib / "Weddings")

    # Stopped as the new library is put in place.
    real_rename = os.rename

    def rename(a, b, *rest, **kw):
        real_rename(a, b, *rest, **kw)
        if str(b) == str(config.project_dir("weddings")):
            raise KeyboardInterrupt

    monkeypatch.setattr(split.os, "rename", rename)
    with pytest.raises(KeyboardInterrupt):
        split.run("t", [weddings], "weddings", "shoot")
    monkeypatch.setattr(split.os, "rename", real_rename)
    op = split.history("t")[0]
    assert op["state"] == "copied" and "weddings" in config.list_projects()
    assert split.finish(op["id"])["state"] == "done"
    assert {r[0] for r in db.connect("t").execute("SELECT id FROM shots")} == set(s["day"])
    assert {r[0] for r in db.connect("weddings").execute("SELECT id FROM shots")} == set(s["wed"])
    split.move_back(op["id"])

    # Stopped before the log said "copied" (the process ended there): finishing sees the copy is there.
    real_save = split._save

    def save(op):
        if op.get("state") == "copied":
            raise KeyboardInterrupt
        real_save(op)

    monkeypatch.setattr(split, "_save", save)
    with pytest.raises(KeyboardInterrupt):
        split.run("t", [weddings], "weddings2", "shoot")
    monkeypatch.setattr(split, "_save", real_save)
    op = split.history("t")[0]
    assert op["state"] == "copying" and op["dst"] == "weddings2"
    with pytest.raises(split.Refused, match="stopped half way"):
        split.plan("t", [str(lib / "Everyday")], "everyday")
    assert split.finish(op["id"])["state"] == "done"
    assert {r[0] for r in db.connect("weddings2").execute("SELECT id FROM shots")} == set(s["wed"])

    # Stopped before the other library committed anything: closed as failed, the source as it was.
    monkeypatch.setattr(split, "_verify", lambda ct: (_ for _ in ()).throw(RuntimeError("power cut")))
    with pytest.raises(RuntimeError):
        split.run("weddings2", [weddings], "t")
    assert split.history("weddings2")[0]["state"] == "failed"
