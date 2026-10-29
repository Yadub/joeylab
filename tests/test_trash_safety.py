"""Moves to the Trash cut short, Put Back and the update lock: a run stopped part way still has an
exact log and the catalog follows what went; Put Back only ever returns the very file that went; and
nothing moves to the Trash or back while an update of the library runs."""

from __future__ import annotations

import csv
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from conftest import add_root, make_jpeg, run_all


@pytest.fixture
def bin_(monkeypatch):
    """This test's own ~/.Trash (HOME is a scratch folder in tests), standing in for the real Trash."""
    from joeylab import cleanup

    d = Path.home() / ".Trash"
    d.mkdir(parents=True, exist_ok=True)

    def move(path):
        dest = d / os.path.basename(path)           # the Trash keeps a file's name, as Finder's does
        if dest.exists():
            dest = d / f"{time.time_ns()}-{os.path.basename(path)}"
        shutil.move(path, dest)
        return str(dest)

    monkeypatch.setattr(cleanup, "move_to_trash", move)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _library(lib):
    for k, name in enumerate(("IMG_1.JPG", "IMG_2.JPG", "IMG_3.JPG"), 1):
        make_jpeg(lib / "a" / name, seed=k)
        (lib / "b").mkdir(exist_ok=True)
        shutil.copy2(lib / "a" / name, lib / "b" / name)                 # an identical copy of each stays
    add_root(lib)
    conn, _ = run_all()
    return conn


def _rows(log) -> list[dict]:
    with open(log, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def _status(conn, path) -> str:
    return conn.execute("SELECT status FROM files WHERE path=?", (str(path),)).fetchone()[0]


def test_a_run_cut_short_leaves_an_exact_log_and_put_back_returns_what_went(env, bin_, monkeypatch):
    from joeylab import cleanup, history

    _, lib = env
    conn = _library(lib)
    paths = [str(lib / "b" / n) for n in ("IMG_1.JPG", "IMG_2.JPG", "IMG_3.JPG")]
    items = cleanup.plan(conn, paths)
    assert [i.category for i in items] == [cleanup.IDENTICAL] * 3
    real, calls = cleanup.move_to_trash, []

    def mover(path):
        calls.append(path)
        if len(calls) == 2:
            raise KeyboardInterrupt                                       # Ctrl-C (or the service stopped) mid-run
        return real(path)

    with pytest.raises(KeyboardInterrupt):
        cleanup.trash_files(items, items, "t-files", mover=mover, conn=conn)
    logs = list((Path(os.environ["JOEYLAB_HOME"]) / "trash-log").iterdir())
    assert len(logs) == 1
    rows = {r["file"]: r for r in _rows(logs[0])}
    assert rows[paths[0]]["result"] == cleanup.MOVED and rows[paths[0]]["now at"] == str(bin_ / "IMG_1.JPG")
    assert rows[paths[0]]["inode"] and rows[paths[0]]["modified ns"]
    assert rows[paths[1]]["result"] == rows[paths[2]]["result"] == cleanup.STOPPED
    assert os.path.exists(paths[1]) and os.path.exists(paths[2])
    assert _status(conn, paths[0]) == "missing" and _status(conn, paths[1]) == "present"

    r = history.put_back(conn, "t", logs[0].name)
    assert r["moved"] == 1 and os.path.exists(paths[0]) and _status(conn, paths[0]) == "present"


def test_a_catalog_error_after_a_move_still_logs_it_and_the_catalog_catches_up(env, bin_, monkeypatch):
    from joeylab import cleanup

    _, lib = env
    conn = _library(lib)
    paths = [str(lib / "b" / "IMG_1.JPG"), str(lib / "b" / "IMG_2.JPG")]
    items = cleanup.plan(conn, paths)
    real, fails = cleanup.mark_moved, []

    def mark(c, it):
        if not fails:
            fails.append(it.path)
            raise sqlite3.OperationalError("database is locked")
        return real(c, it)

    monkeypatch.setattr(cleanup, "mark_moved", mark)
    with pytest.raises(sqlite3.OperationalError):
        log = cleanup.trash_files(items, items, "t-files", conn=conn)
    log = next((Path(os.environ["JOEYLAB_HOME"]) / "trash-log").iterdir())
    rows = {r["file"]: r["result"] for r in _rows(log)}
    assert rows == {paths[0]: cleanup.MOVED, paths[1]: cleanup.STOPPED}
    assert not os.path.exists(paths[0]) and os.path.exists(paths[1])
    assert _status(conn, paths[0]) == "missing"                           # caught up once the catalog answered


def test_two_runs_in_one_second_get_a_log_each(env, bin_, monkeypatch):
    from joeylab import cleanup, history, libraries

    _, lib = env
    conn = _library(lib)
    monkeypatch.setattr(cleanup.time, "strftime", lambda fmt, *a: "20261027-120000")
    one = cleanup.plan(conn, [str(lib / "b" / "IMG_1.JPG")])
    two = cleanup.plan(conn, [str(lib / "b" / "IMG_2.JPG")])
    a = cleanup.trash_files(one, one, "t-files", conn=conn)
    b = cleanup.trash_files(two, two, "t-files", conn=conn)
    assert os.path.basename(a) == "t-files-20261027-120000.tsv" and os.path.basename(b) == "t-files-20261027-120000-2.tsv"
    assert "IMG_1.JPG" in Path(a).read_text() and "IMG_2.JPG" in Path(b).read_text()
    monkeypatch.undo()
    assert [m["log"] for m in history.trash_moves("t")] == [os.path.basename(b), os.path.basename(a)]
    assert {f.name for f in libraries._own_logs("t")} >= {os.path.basename(a), os.path.basename(b)}
    assert history.put_back(conn, "t", os.path.basename(b))["moved"] == 1 and (lib / "b" / "IMG_2.JPG").exists()


def _swap_in_trash(trashed: Path) -> None:
    """The Trash emptied, then another file of the same name and size moved there (all of one
    camera's RAWs can be one size)."""
    size = trashed.stat().st_size
    trashed.unlink()
    trashed.write_bytes(os.urandom(size))


def test_put_back_never_returns_another_file_of_the_same_name_and_size(env, bin_):
    from joeylab import cleanup, history

    _, lib = env
    conn = _library(lib)
    path = str(lib / "b" / "IMG_1.JPG")
    items = cleanup.plan(conn, [path])
    log = os.path.basename(cleanup.trash_files(items, items, "t-files", conn=conn))
    _swap_in_trash(bin_ / "IMG_1.JPG")
    r = history.put_back(conn, "t", log)
    assert r["moved"] == 0 and r["results"][0]["result"].startswith("left alone: what's in the Trash under that name isn't")
    assert not os.path.exists(path) and (bin_ / "IMG_1.JPG").exists() and _status(conn, path) == "missing"


def test_put_back_from_an_older_log_checks_the_catalog_instead(env, bin_):
    from joeylab import cleanup, history

    _, lib = env
    conn = _library(lib)
    paths = [str(lib / "b" / "IMG_1.JPG"), str(lib / "b" / "IMG_2.JPG")]
    items = cleanup.plan(conn, paths)
    log = cleanup.trash_files(items, items, "t-files", conn=conn)
    rows = _rows(log)
    with open(log, "w", encoding="utf-8", newline="") as fh:                 # as logs were written before
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["file", "what stays", "copy that stays", "bytes", "result", "now at"])
        for r in rows:
            w.writerow([r["file"], r["what stays"], r["copy that stays"], r["bytes"], r["result"], r["now at"]])
    _swap_in_trash(bin_ / "IMG_2.JPG")
    r = history.put_back(conn, "t", os.path.basename(log))
    assert {x["path"]: x["result"] for x in r["results"]} == {
        paths[0]: "put back",
        paths[1]: "left alone: what's in the Trash under that name isn't the file moved from there, or it has changed since"}
    assert _status(conn, paths[0]) == "present" and _status(conn, paths[1]) == "missing"


def test_the_copy_that_stays_must_be_the_very_file_checked(env, bin_):
    from joeylab import cleanup

    _, lib = env
    conn = _library(lib)
    path, kept = str(lib / "b" / "IMG_1.JPG"), lib / "a" / "IMG_1.JPG"
    items = cleanup.plan(conn, [path])
    assert items[0].category == cleanup.IDENTICAL and items[0].stays == str(kept)
    st = kept.stat()
    kept.unlink()                                                          # replaced by another file of the same size and time
    kept.write_bytes(os.urandom(st.st_size))
    os.utime(kept, ns=(st.st_atime_ns, st.st_mtime_ns))
    cleanup.trash_files(items, items, "t-files", conn=conn)
    assert items[0].result == "left alone: the copy that should stay has changed or gone" and os.path.exists(path)


def _hold_lock(project: str, ready: threading.Event, release: threading.Event) -> threading.Thread:
    from joeylab import locks

    def hold():
        with locks.project_lock(project):
            ready.set()
            release.wait(30)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert ready.wait(10)
    return t


def test_nothing_goes_to_the_trash_or_back_during_an_update(env, bin_, monkeypatch):
    from fastapi.testclient import TestClient

    from joeylab import cleanup, history, library, locks
    from joeylab.web.routes import trash as trash_route
    from joeylab.web.server import create_app

    _, lib = env
    conn = _library(lib)
    path = str(lib / "b" / "IMG_1.JPG")
    items = cleanup.plan(conn, [path])
    log = os.path.basename(cleanup.trash_files(items, items, "t-files", conn=conn))
    sid = conn.execute("SELECT shot_id FROM files WHERE path=?", (str(lib / "b" / "IMG_2.JPG"),)).fetchone()[0]
    library.decide(conn, [{"id": sid, "pick": -1}])
    c = TestClient(create_app())

    ready, release = threading.Event(), threading.Event()
    t = _hold_lock("t", ready, release)                                    # an update of the library is running
    try:
        with pytest.raises(locks.Busy):
            history.put_back(conn, "t", log)
        assert not os.path.exists(path)
        monkeypatch.setattr(trash_route, "LOCK_WAIT_S", 0.3)
        j = c.post("/api/p/t/trash/plan", json={"what": "rejects"}).json()
        for _ in range(200):
            j = c.get(f"/api/p/t/trash/{j['token']}").json()
            if j["state"] != "checking":
                break
            time.sleep(0.05)
        assert j["go"]["copies"] == 1
        c.post(f"/api/p/t/trash/{j['token']}/run", json={"count": 1})
        for _ in range(200):
            j = c.get(f"/api/p/t/trash/{j['token']}").json()
            if j["state"] != "moving":
                break
            time.sleep(0.05)
        assert j["state"] == "error" and j["error"] == trash_route.UPDATE_RUNNING
        assert (lib / "b" / "IMG_2.JPG").exists()
    finally:
        release.set()
        t.join(10)
    assert history.put_back(conn, "t", log)["moved"] == 1 and os.path.exists(path)


def test_put_back_answers_busy_from_the_app(env, bin_, monkeypatch):
    from fastapi.testclient import TestClient

    from joeylab import cleanup
    from joeylab.web.routes import history as history_route
    from joeylab.web.server import create_app

    _, lib = env
    conn = _library(lib)
    items = cleanup.plan(conn, [str(lib / "b" / "IMG_1.JPG")])
    log = os.path.basename(cleanup.trash_files(items, items, "t-files", conn=conn))
    monkeypatch.setattr(history_route, "PUT_BACK_WAIT_S", 0.3)
    c = TestClient(create_app())
    ready, release = threading.Event(), threading.Event()
    t = _hold_lock("t", ready, release)
    try:
        r = c.post("/api/p/t/history/putback", json={"log": log})
        assert r.status_code == 409 and "update of this library is running" in r.json()["error"]
        assert not (lib / "b" / "IMG_1.JPG").exists()
    finally:
        release.set()
        t.join(10)


def test_empty_folders_never_include_another_librarys(env, bin_):
    from joeylab import cleanup

    _, lib = env
    make_jpeg(lib / "keep" / "IMG_1.JPG", seed=1)
    (lib / "given" / "Empty now").mkdir(parents=True)                   # moved to another library, emptied there
    (lib / "holder" / "given too").mkdir(parents=True)                  # holds only a folder given away
    (lib / "holder" / "spare").mkdir()
    (lib / "plain").mkdir()
    add_root(lib)
    conn, _ = run_all()
    conn.executemany("INSERT INTO excluded_folders(folder, library, added) VALUES (?, 'other', 0)",
                     [(str(lib / "given"),), (str(lib / "holder" / "given too"),)])
    conn.commit()
    roots, _ = cleanup.folder_roots(conn)
    found = cleanup.empty_folders(roots)
    assert {os.path.relpath(f.path, lib) for f in found} == {"plain", "holder/spare"}
    # Checked again just before the move, too.
    sneaky = found + [cleanup.EmptyFolder(str(lib / "given"), drive=found[0].drive)]
    cleanup.trash_folders(sneaky, roots, "t-folders")
    assert sneaky[-1].result == "left alone: it belongs to another library" and (lib / "given").is_dir()
    assert not (lib / "plain").exists() and (lib / "holder" / "given too").is_dir()


def test_a_raw_run_cut_short_logs_each_raw_and_sidecar_as_it_goes(env, bin_):
    from joeylab import cleanup, history, raws

    _, lib = env
    for i in (1, 2):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
        (lib / f"IMG_{i}.NEF").write_bytes(os.urandom(5000))
        t = time.time() - 60
        os.utime(lib / f"IMG_{i}.NEF", (t, t))
    (lib / "IMG_1.xmp").write_text("<x:xmpmeta/>")
    add_root(lib)
    conn, _ = run_all()
    raws.choose(conn, [r[0] for r in conn.execute("SELECT id FROM shots")], raws.REMOVE)
    items = raws.plan(conn)
    assert len(raws.going(items)) == 2
    real, calls = cleanup.move_to_trash, []

    def mover(path):
        calls.append(path)
        if len(calls) == 3:                                                # the second RAW: stopped
            raise KeyboardInterrupt
        return real(path)

    with pytest.raises(KeyboardInterrupt):
        raws.trash(conn, items, "t-files", mover=mover)
    log = next((Path(os.environ["JOEYLAB_HOME"]) / "trash-log").iterdir())
    rows = {os.path.basename(r["file"]): r["result"] for r in _rows(log)}
    assert rows == {"IMG_1.NEF": cleanup.MOVED, "IMG_1.xmp": cleanup.MOVED, "IMG_2.NEF": cleanup.STOPPED}
    assert _status(conn, lib / "IMG_1.NEF") == "missing" and _status(conn, lib / "IMG_2.NEF") == "present"
    r = history.put_back(conn, "t", log.name)
    assert r["moved"] == 2 and (lib / "IMG_1.NEF").exists() and (lib / "IMG_1.xmp").exists()
