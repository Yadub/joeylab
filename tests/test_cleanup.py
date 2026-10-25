"""Moving duplicate extras, rejects and empty folders to the Trash: only after a check and a typed
count, never a file's last copy unless asked, and nothing that changed since it was checked."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import time

import pytest
from PIL import Image
from typer.testing import CliRunner

from conftest import add_root, make_jpeg, run_all


@pytest.fixture
def fake_trash(tmp_path, monkeypatch):
    """Stands in for the real Trash (tests never touch the user's)."""
    from joeylab import cleanup

    bin_ = tmp_path / "trash"
    bin_.mkdir()

    def move(path):
        dest = bin_ / f"{len(list(bin_.iterdir()))}-{os.path.basename(path)}"
        shutil.move(path, dest)
        return str(dest)

    monkeypatch.setattr(cleanup, "move_to_trash", move)
    return bin_


def _old(p):
    t = time.time() - 60
    os.utime(p, (t, t))


def _library(lib):
    make_jpeg(lib / "a" / "IMG_1.JPG", seed=1)
    (lib / "b").mkdir(parents=True)
    shutil.copy2(lib / "a" / "IMG_1.JPG", lib / "b" / "IMG_1.JPG")          # an identical copy
    make_jpeg(lib / "c" / "IMG_2.JPG", seed=2)                               # one of a kind
    make_jpeg(lib / "d" / "IMG_3.JPG", seed=3)
    Image.open(lib / "d" / "IMG_3.JPG").save(lib / "d" / "IMG_3.TIF")       # the same photo's other file
    _old(lib / "d" / "IMG_3.TIF")
    make_jpeg(lib / "e" / "IMG_4.JPG", seed=4)


def test_a_list_is_checked_and_only_files_with_a_copy_go_by_default(env, fake_trash):
    from joeylab import cleanup
    from joeylab.cli import app

    _, lib = env
    _library(lib)
    add_root(lib)
    conn, _ = run_all()
    changed = lib / "e" / "IMG_4.JPG"
    with open(changed, "ab") as f:
        f.write(b"edited since")                                             # changed after JoeyLab read it
    listing = [str(lib / "b" / "IMG_1.JPG"), str(lib / "c" / "IMG_2.JPG"), str(lib / "d" / "IMG_3.JPG"),
               str(changed), str(lib / "nowhere.JPG"), "", "# a comment"]
    items = {os.path.relpath(i.path, lib): i for i in cleanup.plan(conn, listing)}
    assert items["b/IMG_1.JPG"].category == cleanup.IDENTICAL and items["b/IMG_1.JPG"].stays == str(lib / "a" / "IMG_1.JPG")
    assert items["c/IMG_2.JPG"].category == cleanup.LAST
    assert items["d/IMG_3.JPG"].category == cleanup.TWIN and items["d/IMG_3.JPG"].stays.endswith("IMG_3.TIF")
    assert items["e/IMG_4.JPG"].category == cleanup.SKIP and "changed" in items["e/IMG_4.JPG"].why
    assert items["nowhere.JPG"].category == cleanup.SKIP

    lst = lib.parent / "list.txt"
    lst.write_text("\n".join(listing))
    runner = CliRunner()
    r = runner.invoke(app, ["trash-list", str(lst), "-p", "t"], input="\n")          # Enter: stop
    assert r.exit_code == 1 and "Nothing was moved" in r.output and not list(fake_trash.iterdir())
    r = runner.invoke(app, ["trash-list", str(lst), "-p", "t"], input="3\n")         # the wrong count: stop
    assert r.exit_code == 1 and not list(fake_trash.iterdir())
    assert "2 files" in r.output and "no copy that stays will be left alone" in r.output
    r = runner.invoke(app, ["trash-list", str(lst), "-p", "t"], input="1\n")
    assert r.exit_code == 0, r.output
    assert not (lib / "b" / "IMG_1.JPG").exists() and (lib / "a" / "IMG_1.JPG").exists()
    assert (lib / "c" / "IMG_2.JPG").exists() and (lib / "d" / "IMG_3.JPG").exists()
    assert "1 folder left empty" in r.output
    logs = list((lib.parent / "home" / "trash-log").iterdir())
    assert len(logs) == 1 and "IMG_2.JPG" in logs[0].read_text()                   # the log covers what stayed too

    # Last copies go only when asked for, and the warning says so.
    r = runner.invoke(app, ["trash-list", str(lst), "-p", "t", "--include-last-copies"], input="2\n")
    assert r.exit_code == 0, r.output
    assert "no copy anywhere" in r.output
    assert not (lib / "c" / "IMG_2.JPG").exists() and not (lib / "d" / "IMG_3.JPG").exists()
    assert (lib / "d" / "IMG_3.TIF").exists() and changed.exists()


def test_two_copies_listed_together_are_both_last_copies(env, fake_trash):
    from joeylab import cleanup

    _, lib = env
    _library(lib)
    add_root(lib)
    conn, _ = run_all()
    items = cleanup.plan(conn, [str(lib / "a" / "IMG_1.JPG"), str(lib / "b" / "IMG_1.JPG")])
    assert [i.category for i in items] == [cleanup.LAST, cleanup.LAST]
    assert cleanup.chosen(items, include_last=False) == []


def test_nothing_moves_if_it_or_its_copy_changed_after_the_check(env, fake_trash):
    from joeylab import cleanup

    _, lib = env
    _library(lib)
    add_root(lib)
    conn, _ = run_all()
    items = cleanup.plan(conn, [str(lib / "b" / "IMG_1.JPG")])
    assert items[0].category == cleanup.IDENTICAL
    (lib / "a" / "IMG_1.JPG").unlink()                                       # the copy that would stay is gone
    cleanup.trash_files(items, cleanup.chosen(items, False), "t-files")
    assert (lib / "b" / "IMG_1.JPG").exists() and "copy that should stay" in items[0].result

    shutil.copy2(lib / "b" / "IMG_1.JPG", lib / "a" / "IMG_1.JPG")
    items = cleanup.plan(conn, [str(lib / "c" / "IMG_2.JPG")])
    with open(lib / "c" / "IMG_2.JPG", "ab") as f:
        f.write(b"x")
    cleanup.trash_files(items, cleanup.chosen(items, True), "t-files")
    assert (lib / "c" / "IMG_2.JPG").exists() and "changed" in items[0].result


def test_empty_folders_are_listed_and_moved_only_while_still_empty(env, fake_trash):
    from joeylab import cleanup
    from joeylab.cli import app

    _, lib = env
    make_jpeg(lib / "w" / "IMG_1.JPG", seed=1)
    (lib / "x").mkdir()                                                          # nothing at all
    (lib / "y").mkdir()
    (lib / "y" / ".DS_Store").write_bytes(b"\0")
    (lib / "y" / "._IMG_9.JPG").write_bytes(b"\0")                                # only leftovers
    (lib / "z" / "sub").mkdir(parents=True)
    (lib / "z" / "sub2").mkdir()
    (lib / "z" / "sub2" / "Thumbs.db").write_bytes(b"\0")                        # empty all the way down
    (lib / "v" / ".git").mkdir(parents=True)                                     # a hidden folder counts as content
    (lib / "u").mkdir()
    os.symlink(lib / "w", lib / "u" / "link")                                    # so does a link
    (lib / "t" / "notes.txt").parent.mkdir()
    (lib / "t" / "notes.txt").write_text("keep me")                              # and any other file
    (lib / ".Spotlight-V100").mkdir()                                            # the system's own: never listed
    add_root(lib)
    run_all()

    found = {os.path.relpath(f.path, lib): f for f in cleanup.empty_folders([str(lib)])}
    assert set(found) == {"x", "y", "z"}
    assert found["z"].folders == 2 and len(found["y"].clutter) == 2
    assert not cleanup.empty_folders([str(lib / "x")])                           # a library folder itself never is

    runner = CliRunner()
    r = runner.invoke(app, ["empty-folders", "-p", "t"])
    assert r.exit_code == 0 and "3 empty folders in all" in r.output and "Nothing was changed" in r.output
    assert (lib / "x").exists()
    (lib / "y" / "IMG_5.JPG").write_bytes(b"new")                                # arrives before the move
    real = cleanup.empty_folders
    try:
        cleanup.empty_folders = lambda roots: [f for f in real(roots)] + [cleanup.EmptyFolder(str(lib / "y"))]
        r = runner.invoke(app, ["empty-folders", "-p", "t", "--trash"], input="3\n")
    finally:
        cleanup.empty_folders = real
    assert r.exit_code == 0, r.output
    assert not (lib / "x").exists() and not (lib / "z").exists()
    assert (lib / "y" / "IMG_5.JPG").exists() and "no longer empty" in r.output
    assert (lib / "w" / "IMG_1.JPG").exists() and (lib / "t" / "notes.txt").exists() and (lib / "v").exists()


def _wait(c, url, states=("checking", "moving")):
    for _ in range(200):
        j = c.get(url).json()
        if j["state"] not in states:
            return j
        time.sleep(0.05)
    raise AssertionError("never finished")


def test_the_app_moves_only_rejected_photos_and_only_for_the_owner(env, fake_trash):
    from fastapi.testclient import TestClient

    from joeylab import library
    from joeylab.web.server import create_app

    _, lib = env
    _library(lib)
    add_root(lib)
    conn, _ = run_all()
    sid = {r["stem"] + r["dir"][-1]: r["id"] for r in conn.execute("SELECT id, stem, dir FROM shots")}
    library.decide(conn, [{"id": sid["IMG_1b"], "pick": -1}, {"id": sid["IMG_2c"], "pick": -1}])   # a copy, and a one-off
    app = create_app(owner="me@example.com")
    c = TestClient(app)
    other = {"Tailscale-User-Login": "guest@example.com", "X-Forwarded-For": "100.64.0.9"}
    owner = {"Tailscale-User-Login": "me@example.com", "X-Forwarded-For": "100.64.0.8"}
    assert c.post("/api/p/t/trash/plan", json={"what": "rejects"}, headers=other).status_code == 403
    assert c.get("/api/meta", headers=other).status_code == 403
    assert c.get("/api/meta", headers=owner).json()["can_trash"] is True
    assert c.get("/api/meta").json()["can_trash"] is True                                  # at the Mac

    # Whatever the filter says, only rejected photos are ever looked at.
    j = c.post("/api/p/t/trash/plan", json={"what": "rejects", "filter": {"status": "picks"}}, headers=owner).json()
    j = _wait(c, f"/api/p/t/trash/{j['token']}")
    assert j["state"] == "ready" and j["go"] == {"copies": 1, "copies_bytes": j["go"]["copies_bytes"], "all": 2, "all_bytes": j["go"]["all_bytes"]}
    assert set(j["summary"]) == {"identical", "last"}
    run = f"/api/p/t/trash/{j['token']}/run"
    assert c.post(run, json={"count": 2}, headers=owner).status_code == 400                # the count must match
    assert c.post(run, json={"count": 1}, headers=other).status_code == 403
    assert c.post(run, json={"count": 1}, headers=owner).status_code == 200
    done = _wait(c, f"/api/p/t/trash/{j['token']}")
    assert done["state"] == "done" and done["result"]["moved"] == 1 and done["result"]["emptied"] == 1
    assert not (lib / "b" / "IMG_1.JPG").exists() and (lib / "a" / "IMG_1.JPG").exists() and (lib / "c" / "IMG_2.JPG").exists()
    assert c.post(run, json={"count": 1}, headers=owner).status_code == 409                # a check is used once

    # Last copies go only when asked for, with the count that includes them.
    j = _wait(c, "/api/p/t/trash/" + c.post("/api/p/t/trash/plan", json={"what": "rejects"}).json()["token"])
    assert j["go"]["copies"] == 0 and j["go"]["all"] == 1
    run = f"/api/p/t/trash/{j['token']}/run"
    assert c.post(run, json={"count": 1}).status_code == 400
    assert c.post(run, json={"count": 1, "include_last": True}).status_code == 200
    assert _wait(c, f"/api/p/t/trash/{j['token']}")["result"]["moved"] == 1 and not (lib / "c" / "IMG_2.JPG").exists()
    assert (lib / "e" / "IMG_4.JPG").exists() and (lib / "d" / "IMG_3.JPG").exists()           # never rejected

    # Empty folders, the same way.
    j = _wait(c, "/api/p/t/trash/" + c.post("/api/p/t/trash/plan", json={"what": "folders"}).json()["token"])
    assert {os.path.basename(f["path"]) for f in j["folders"]} == {"b", "c"}
    assert c.post(f"/api/p/t/trash/{j['token']}/run", json={"count": 2}).status_code == 200
    assert _wait(c, f"/api/p/t/trash/{j['token']}")["result"]["moved"] == 2 and not (lib / "b").exists()


def test_a_file_is_never_its_own_copy_however_its_path_is_spelled(env, fake_trash):
    from joeylab import cleanup, db, scanner

    _, lib = env
    _library(lib)
    add_root(lib)
    conn, _ = run_all()
    # The same folder added again in other letters is the same root, not a second one.
    other = Path(str(lib).replace("lib", "LIB")) if "lib" in str(lib) else lib
    rid, note = scanner.add_root(conn, other)
    assert conn.execute("SELECT COUNT(*) FROM roots").fetchone()[0] == 1
    _, note = scanner.add_root(conn, Path(str(other) + "/A"))
    assert note and "already covered" in note
    # Even if a second spelling of one file got into the catalog, it isn't "a copy that stays".
    real = lib / "c" / "IMG_2.JPG"
    row = conn.execute("SELECT * FROM files WHERE path=?", (str(real),)).fetchone()
    ghost = str(real).replace("IMG_2.JPG", "img_2.jpg")
    conn.execute("INSERT INTO files(root_id, path, shot_id, kind, ext, size, mtime_ns, fp, status) VALUES (?,?,?,?,?,?,?,?, 'present')",
                 (row["root_id"], ghost, None, row["kind"], row["ext"], row["size"], row["mtime_ns"], row["fp"]))
    conn.commit()
    items = cleanup.plan(conn, [str(real)])
    assert items[0].category == cleanup.LAST
    assert cleanup.chosen(items, include_last=False) == []


def test_only_one_move_to_the_trash_at_a_time(env, fake_trash):
    from joeylab import cleanup

    _, lib = env
    _library(lib)
    add_root(lib)
    conn, _ = run_all()
    items = cleanup.plan(conn, [str(lib / "b" / "IMG_1.JPG")])
    with cleanup.trash_lock():
        with pytest.raises(cleanup.Busy):
            cleanup.trash_files(items, cleanup.chosen(items, False), "t-files")
    assert (lib / "b" / "IMG_1.JPG").exists()


def test_a_photo_un_rejected_after_the_check_stays(env, fake_trash):
    from fastapi.testclient import TestClient

    from joeylab import library
    from joeylab.web.server import create_app

    _, lib = env
    _library(lib)
    add_root(lib)
    conn, _ = run_all()
    sid = conn.execute("SELECT shot_id FROM files WHERE path=?", (str(lib / "b" / "IMG_1.JPG"),)).fetchone()[0]
    library.decide(conn, [{"id": sid, "pick": -1}])
    c = TestClient(create_app())
    j = _wait(c, "/api/p/t/trash/" + c.post("/api/p/t/trash/plan", json={"what": "rejects"}).json()["token"])
    assert j["go"]["copies"] == 1
    library.decide(conn, [{"id": sid, "pick": 1}])                       # changed your mind, on another device
    c.post(f"/api/p/t/trash/{j['token']}/run", json={"count": 1})
    done = _wait(c, f"/api/p/t/trash/{j['token']}")
    assert done["result"]["moved"] == 0 and "no longer rejected" in done["result"]["left"][0]["why"]
    assert (lib / "b" / "IMG_1.JPG").exists()


def test_moved_files_leave_the_catalog_at_once_a_scan_agrees_and_put_back_returns_them(env, fake_trash):
    from joeylab import cleanup, db, library, scanner

    _, lib = env
    _library(lib)
    add_root(lib)
    conn, _ = run_all()
    extra = str(lib / "b" / "IMG_1.JPG")
    sid = {p: conn.execute("SELECT shot_id FROM files WHERE path=?", (str(lib / p),)).fetchone()[0]
           for p in ("b/IMG_1.JPG", "d/IMG_3.JPG")}
    status = lambda s: conn.execute("SELECT status FROM shots WHERE id=?", (s,)).fetchone()[0]       # noqa: E731
    assert conn.execute("SELECT COUNT(*) FROM dup_sets").fetchone()[0] == 1
    items = cleanup.plan(conn, [extra])
    cleanup.trash_files(items, items, "t-files", conn=conn)
    assert items[0].photo_gone and status(sid["b/IMG_1.JPG"]) == "missing"
    assert conn.execute("SELECT status FROM files WHERE path=?", (extra,)).fetchone()[0] == "missing"
    assert conn.execute("SELECT COUNT(*) FROM dup_sets").fetchone()[0] == 0                 # a set of one is no set
    rows, _ = library.page(conn, library.Filter())
    assert sid["b/IMG_1.JPG"] not in {r["id"] for r in rows}

    # A photo with two files stays while one is left, and goes with the last.
    jpeg = cleanup.plan(conn, [str(lib / "d" / "IMG_3.JPG")])
    cleanup.trash_files(jpeg, jpeg, "t-files", conn=conn)
    assert not jpeg[0].photo_gone and status(sid["d/IMG_3.JPG"]) == "present"
    tif = cleanup.plan(conn, [str(lib / "d" / "IMG_3.TIF")])
    cleanup.trash_files(tif, tif, "t-files", conn=conn)
    assert tif[0].photo_gone and status(sid["d/IMG_3.JPG"]) == "missing"

    before = [tuple(r) for r in conn.execute("SELECT id, status, shot_id FROM files ORDER BY id")]
    rep = scanner.scan(db.connect("t"))                                   # the next scan changes nothing more
    assert rep.missing == 0 and rep.returned == 0 and rep.added == 0
    assert [tuple(r) for r in conn.execute("SELECT id, status, shot_id FROM files ORDER BY id")] == before
    assert status(sid["b/IMG_1.JPG"]) == "missing"

    shutil.move(items[0].trashed_to, extra)                                # Put Back
    rep = scanner.scan(db.connect("t"))
    assert rep.returned == 1 and status(sid["b/IMG_1.JPG"]) == "present"


def test_exported_files_never_land_on_photos_or_in_photo_folders(env, tmp_path):
    from joeylab.cli import app

    _, lib = env
    _library(lib)
    add_root(lib)
    run_all()
    photo = lib / "a" / "IMG_1.JPG"
    before = photo.read_bytes()
    runner = CliRunner()
    assert runner.invoke(app, ["export", "csv", str(photo), "-p", "t"]).exit_code == 1
    assert runner.invoke(app, ["export", "list", "--rejects", "-o", str(lib / "list.txt"), "-p", "t"]).exit_code == 1
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "holiday.jpg").write_bytes(b"a photo")
    assert runner.invoke(app, ["export", "decisions", str(elsewhere / "holiday.jpg"), "-p", "t"]).exit_code == 1
    assert (elsewhere / "holiday.jpg").read_bytes() == b"a photo" and photo.read_bytes() == before
    (elsewhere / "old.csv").write_text("x")
    assert runner.invoke(app, ["export", "csv", str(elsewhere / "old.csv"), "-p", "t"]).exit_code == 0     # an earlier export: fine
    assert not (lib / "list.txt").exists()
