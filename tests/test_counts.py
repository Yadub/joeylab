"""Counts on big libraries: the overview kept between requests and counted again in the background, the
live counts pages confirm actions with (exact straight after a decision, an Undo or a move to the
Trash), the file it's kept in across restarts, and the indexes that let counts skip the big rows."""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from conftest import add_root, jpeg_at, make_jpeg, run_all


def _library(lib, n=4):
    for i in range(n):
        jpeg_at(lib / f"IMG_{i}.JPG", i, f"2024:05:0{i + 1} 10:00:00")
    add_root(lib)
    return run_all()[0]


def _quick(monkeypatch):
    """Background counts straight away (the tests can't wait seconds for each)."""
    from joeylab.web import overview

    monkeypatch.setattr(overview, "DEBOUNCE_S", 0.0)
    monkeypatch.setattr(overview, "MIN_GAP_S", 0.0)


def _until(fn, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("waited too long")


def test_the_overview_is_served_at_once_and_counted_again_in_the_background(env, monkeypatch):
    from joeylab.web.server import create_app

    _quick(monkeypatch)
    _, lib = env
    conn = _library(lib)
    c = TestClient(create_app())
    a = c.get("/api/p/t/overview").json()
    assert a["fresh"] and a["seq"] and a["counts"]["photos"] == 4
    assert {x["camera"] for x in a["cameras"]} == {"TestCam"}
    assert c.get("/api/p/t/jobs/current").json()["overview"] == a["seq"]
    # Something only the full count reads changes (by another program): the last numbers are served at
    # once, marked as not fresh, and newer ones follow in the background with a new seq.
    conn.execute("UPDATE shots SET camera='OtherCam'")
    conn.commit()
    b = c.get("/api/p/t/overview").json()
    assert not b["fresh"] and {x["camera"] for x in b["cameras"]} == {"TestCam"} and b["seq"] == a["seq"]
    seq = _until(lambda: (s := c.get("/api/p/t/jobs/current").json()["overview"]) != a["seq"] and s)
    d = c.get("/api/p/t/overview").json()
    assert d["fresh"] and d["seq"] == seq and {x["camera"] for x in d["cameras"]} == {"OtherCam"}
    # A count that finds nothing new keeps the seq: open pages don't fetch the same numbers again.
    conn.execute("UPDATE shots SET camera='OtherCam'")
    conn.commit()
    c.get("/api/p/t/overview")
    _until(lambda: c.get("/api/p/t/overview").json()["fresh"])
    assert c.get("/api/p/t/jobs/current").json()["overview"] == seq


def test_counts_that_confirm_actions_are_exact_straight_after_them_and_after_undo(env, monkeypatch):
    from joeylab import db
    from joeylab.web import overview
    from joeylab.web.server import create_app

    _, lib = env
    conn = _library(lib)
    monkeypatch.setattr(overview, "MIN_GAP_S", 3600.0)    # no background count: only the live counts can be right
    c = TestClient(create_app())
    a = c.get("/api/p/t/overview").json()["counts"]
    assert (a["picks"], a["undecided"], a["undecided_stacks"]) == (0, 4, a["undecided_stacks"])
    sid = conn.execute("SELECT id FROM shots ORDER BY id").fetchone()[0]
    gen = db.counter(conn, db.COUNTS_GEN)
    assert c.post("/api/p/t/decide", json={"items": [{"id": sid, "pick": 1}]}).status_code == 200
    assert db.counter(conn, db.COUNTS_GEN) > gen
    b = c.get("/api/p/t/overview").json()
    assert not b["fresh"] and b["counts"]["picks"] == 1 and b["counts"]["undecided"] == 3
    assert b["today"]["picked"] == 1
    assert c.post("/api/p/t/undo", json={}).json()["undone"]
    u = c.get("/api/p/t/overview").json()["counts"]
    assert (u["picks"], u["undecided"], u["undecided_stacks"]) == (0, 4, a["undecided_stacks"])
    # A move to the Trash (as cleanup does it): the photo and its file are gone from the counts at once.
    fid = conn.execute("SELECT id FROM files WHERE shot_id=?", (sid,)).fetchone()[0]
    conn.execute("UPDATE files SET status='missing' WHERE id=?", (fid,))
    conn.execute("UPDATE shots SET status='missing' WHERE id=?", (sid,))
    conn.commit()
    t = c.get("/api/p/t/overview").json()["counts"]
    assert (t["photos"], t["files"], t["missing"], t["undecided"]) == (3, 3, 1, 3)
    # The settings that hide photos from browsing count at once too, and the settings come with every answer.
    assert c.post("/api/p/t/settings", json={"hide_tiny": False}).status_code == 200
    assert c.get("/api/p/t/overview").json()["settings"]["hide_tiny"] is False
    # What the counts don't depend on leaves the counter alone.
    gen = db.counter(conn, db.COUNTS_GEN)
    conn.execute("UPDATE shots SET camera='X', lens='Y'")
    conn.execute("UPDATE decisions SET label='Red'")
    db.set_setting(conn, "folder_names", {"a": "b"})
    conn.commit()
    assert db.counter(conn, db.COUNTS_GEN) == gen


def test_a_restart_or_another_library_opens_from_the_kept_file(env, monkeypatch):
    from joeylab import config
    from joeylab.web import overview
    from joeylab.web.routes import library as routes
    from joeylab.web.server import create_app

    _quick(monkeypatch)
    _, lib = env
    _library(lib).close()           # (the last connection to close moves the WAL into the file: a change)
    a = TestClient(create_app()).get("/api/p/t/overview").json()
    kept = config.project_dir("t") / overview.FILE
    assert kept.exists() and kept.stat().st_mode & 0o077 == 0
    calls = []
    real = routes.overview_full
    monkeypatch.setattr(routes, "overview_full", lambda *x: calls.append(1) or real(*x))
    b = TestClient(create_app()).get("/api/p/t/overview").json()
    assert b["counts"] == a["counts"] and b["seq"] == a["seq"] and b["name"] == "t"
    time.sleep(0.3)
    assert not calls                                       # nothing changed: not counted again
    # A file from another version of the counts is counted again, not trusted.
    data = json.loads(kept.read_text())
    kept.write_text(json.dumps({**data, "format": -1}))
    TestClient(create_app()).get("/api/p/t/overview")
    assert calls


def test_pages_polling_cannot_put_off_the_count_and_busy_updates_slow_it(env, monkeypatch):
    from joeylab.web import overview
    from joeylab.web.server import create_app

    monkeypatch.setattr(overview, "DEBOUNCE_S", 0.3)
    monkeypatch.setattr(overview, "MIN_GAP_S", 0.0)
    _, lib = env
    conn = _library(lib)
    app = create_app()
    c = TestClient(app)
    seq = c.get("/api/p/t/overview").json()["seq"]
    conn.execute("UPDATE shots SET camera='OtherCam'")
    conn.commit()
    end = time.time() + 10
    while c.get("/api/p/t/jobs/current").json()["overview"] == seq:      # a page polling ten times a second
        assert time.time() < end, "the count never ran"
        time.sleep(0.1)
    # An update in a heavy step (analysing, faces, search) is left alone: counts wait BUSY_GAP_S.
    ov = app.state.overviews
    job = type("J", (), {"step": "analyse"})()
    monkeypatch.setattr(app.state.jobs, "running", {"t": job})
    assert ov.busy("t")
    job.step = "group"
    assert not ov.busy("t")
    app.state.bg.running.add("regroup:t")
    assert ov.busy("t") and not ov.busy("other")


def test_counts_read_indexes_not_the_big_rows(env):
    from joeylab import ai, db, search

    _, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    conn = run_all()[0]
    conn.executescript(ai.SCHEMA)
    conn.executescript(search.SCHEMA)
    plan = lambda q: " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + q))
    for table, index in db.COUNT_INDEXES.items():
        assert f"COVERING INDEX {index}" in plan(f"SELECT COUNT(*) FROM {table}")
    assert "COVERING INDEX search_vecs_shot" in plan(
        "SELECT COUNT(*) FROM search_vecs sv JOIN shots s ON s.id = sv.shot_id WHERE s.status='present'")
    assert "COVERING INDEX faces_by_group" in plan(
        "SELECT COUNT(DISTINCT cluster) FROM faces WHERE cluster IS NOT NULL AND (+person_id IS NULL OR +person_id <= 0)")
    assert "COVERING INDEX faces_by_person" in plan("SELECT shot_id FROM faces WHERE person_id = 3")
    # The step is safe to run again, and a catalog made before the vector tables existed gets their
    # indexes when they're made.
    db._m16_counts(conn)
    conn.commit()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('index', 'trigger')")}
    assert {"faces_by_group", "faces_by_person", "files_shot_status", "decisions_counted_upd", "settings_counted_ins"} <= names
