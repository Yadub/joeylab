"""The first update does the useful steps first (stacks, duplicates, suggestions), and work that
can hang (Apple Vision, the graphics processor) has a time limit."""

from __future__ import annotations

import shutil
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import add_root, jpeg_at, make_jpeg, run_all


def _client():
    from joeylab.web.jobs import JobManager
    from joeylab.web.server import create_app

    jobs = JobManager(watch_minutes=0, faces=False, ai_layer=False)
    return TestClient(create_app(jobs=jobs)), jobs


def _wait(c, p, until=lambda j: j["state"] != "running", tries=300):
    for _ in range(tries):
        j = c.get(f"/api/p/{p}/jobs/current").json()
        if j["job"] and until(j["job"]):
            return j
        time.sleep(0.05)
    raise AssertionError(f"the update of {p} didn't get there: {j}")


def _shoot(lib):
    """A burst (two near-identical frames a second apart), a photo on its own, and an identical copy."""
    jpeg_at(lib / "Shoot" / "IMG_0001.JPG", 1, "2024:05:01 10:00:00")
    jpeg_at(lib / "Shoot" / "IMG_0002.JPG", 1, "2024:05:01 10:00:01")
    jpeg_at(lib / "Shoot" / "IMG_0003.JPG", 7, "2024:05:01 12:00:00")
    (lib / "Backup").mkdir(parents=True)
    shutil.copy2(lib / "Shoot" / "IMG_0003.JPG", lib / "Backup" / "IMG_0003.JPG")


# ---------------------------------------------------------------- the update's order

def test_update_stacks_duplicates_and_suggestions_before_the_slow_steps(env, monkeypatch):
    """Cull's stacks, the duplicates and the suggestions are in as soon as the photos are analysed;
    faces, picture vectors and search come after (here: stand-ins that note what they find)."""
    from joeylab import db, pipeline

    _, lib = env
    _shoot(lib)
    add_root(lib)
    seen = {}

    def slow(name):
        def run(conn, rep, *a, **k):
            seen[name] = {
                "stacks": conn.execute("SELECT COUNT(*) FROM groups WHERE size > 1").fetchone()[0],
                "dupes": conn.execute("SELECT COUNT(*) FROM dup_sets").fetchone()[0],
                "suggestions": conn.execute("SELECT COUNT(*) FROM metrics WHERE suggestion IS NOT NULL").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            }
            return 0
        return run

    for name in ("run_faces", "run_face_quality", "run_ai", "run_search_index", "run_pets"):
        monkeypatch.setattr(pipeline, name, slow(name))
    order = []

    class Rep(pipeline.Reporter):
        def step(self, key):
            order.append(("step", key))

        def finished(self, step):
            order.append(("done", step))

    conn = db.connect("t")
    out = pipeline.run_all(conn, "t", Rep(), faces=True, ai_layer=True)
    for name in ("run_faces", "run_face_quality", "run_ai", "run_search_index", "run_pets"):
        assert seen[name]["stacks"] == 1 and seen[name]["dupes"] == 1 and seen[name]["suggestions"] > 0, (name, seen)
        assert seen[name]["events"] == 0                       # places and events come after the slow steps
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0
    steps = [k for what, k in order if what == "step"]
    assert steps == ["scan", "analyse", "group", "places", "check"]          # (the stand-ins report no steps)
    done = [k for what, k in order if what == "done"]
    assert done == ["analyse", "group", "faces", "ai", "search", "places"]
    assert out["groups"]["multi_frame_groups"] == 1 and out["untouched"]["ok"]


def test_pages_see_stacks_while_search_is_still_indexing(env, monkeypatch):
    from joeylab import pipeline

    _, lib = env
    _shoot(lib)
    add_root(lib)
    go, inside = threading.Event(), threading.Event()

    def indexing(conn, rep, enabled=True):
        rep.step("search")
        rep.stage("Indexing for search", 10)
        rep.advance(4, 10)
        inside.set()
        go.wait(20)
        return 0

    monkeypatch.setattr(pipeline, "run_search_index", indexing)
    from joeylab.web.jobs import JobManager
    from joeylab.web.server import create_app

    jobs = JobManager(watch_minutes=0, faces=False, ai_layer=True)
    c = TestClient(create_app(jobs=jobs))
    try:
        assert c.post("/api/p/t/jobs", json={}).status_code == 200
        assert inside.wait(20)
        j = c.get("/api/p/t/jobs/current").json()
        assert j["job"]["state"] == "running" and j["job"]["step"] == "search" and (j["job"]["done"], j["job"]["total"]) == (4, 10)
        assert "group" in j["job"]["ready"] and "search" not in j["job"]["ready"]
        assert j["version"] == 1                                # open pages learn the stacks are in
        stacks = c.get("/api/p/t/stacks", params={"status": "undecided"}).json()["stacks"]
        assert max(len(s["ids"]) for s in stacks) == 2
        o = c.get("/api/p/t/overview").json()["counts"]
        assert o["dup_sets"] == 1 and o["suggest_pick"] + o["suggest_reject"] > 0
    finally:
        go.set()
    j = _wait(c, "t")
    assert j["job"]["state"] == "done" and j["version"] == 2 and j["job"]["untouched"]["ok"]


# ---------------------------------------------------------------- time limits

def test_deadline_helpers():
    from joeylab import deadline

    assert deadline.call(lambda a, b: a + b, 2, 3, timeout=5) == 5
    with pytest.raises(ZeroDivisionError):
        deadline.call(lambda: 1 / 0, timeout=5)
    stuck = threading.Event()
    t0 = time.monotonic()
    with pytest.raises(deadline.TimedOut, match="The search model didn't finish in 0 s"):
        deadline.call(stuck.wait, 30, timeout=0.2, what="The search model")
    assert time.monotonic() - t0 < 2
    pool = deadline.Pool(1, "test")
    started = threading.Event()
    first = pool.submit(lambda: started.set() or stuck.wait(30))
    queued = pool.submit(lambda: "never")
    assert started.wait(5)
    pool.close()                             # what hasn't started is dropped; the stuck call is left be
    assert queued.cancelled() and not first.done()
    stuck.set()
    assert first.result(timeout=5) is True


class _FakeModel:
    """Stands in for the search model: vectors at once, or never (a stuck graphics processor)."""

    def __init__(self, hang: threading.Event | None = None):
        self.hang = hang
        self.lock = threading.RLock()

    def embed_images(self, images):
        if self.hang is not None:
            self.hang.wait(30)
        v = np.random.default_rng(len(images)).normal(size=(len(images), 8)).astype(np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)


def _analysed_library(lib, n=5):
    for i in range(n):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    return conn


def test_stuck_apple_vision_cant_hang_the_update(env, monkeypatch):
    from joeylab import config, search

    _, lib = env
    conn = _analysed_library(lib)
    monkeypatch.setattr(config, "VISION_TIMEOUT_S", 0.3)
    release = threading.Event()
    calls = []

    def vision(path):
        calls.append(path)
        if len(calls) == 1:
            release.wait(30)                 # the first photo never comes back
        return {"labels": [["dog", 0.9]], "text": "", "utility": 0, "score": 0.5}

    monkeypatch.setattr(search, "vision_available", lambda: True)
    monkeypatch.setattr(search, "vision_analyze", vision)
    monkeypatch.setattr(search.Model, "get", classmethod(lambda cls: _FakeModel()))
    errors = []
    t0 = time.monotonic()
    try:
        search.run(conn, errors=errors)
    finally:
        release.set()
    assert time.monotonic() - t0 < 10
    assert conn.execute("SELECT COUNT(*) FROM search_vecs").fetchone()[0] == 5      # every photo is searchable
    stuck = [sid for sid, why in errors if why.startswith(search.VISION_STUCK)]
    assert stuck and len(calls) <= 3                       # Vision is left out once it has hung
    left = search.pending(conn)
    assert {r["id"] for r in left} >= set(stuck)            # those photos wait for the next update


def test_stuck_graphics_processor_cant_hang_the_update(env, monkeypatch):
    from joeylab import ai, config, pipeline, search

    _, lib = env
    conn = _analysed_library(lib, n=3)
    monkeypatch.setattr(config, "GPU_TIMEOUT_S", 0.3)
    monkeypatch.setattr(search, "vision_available", lambda: False)
    monkeypatch.setattr(search, "available", lambda: True)
    hang = threading.Event()
    stuck = _FakeModel(hang)
    monkeypatch.setattr(search.Model, "_inst", stuck)
    monkeypatch.setattr(search.Model, "_cpu_only", False)
    logs = []

    class Rep(pipeline.Reporter):
        def log(self, msg, level="info"):
            logs.append((level, msg))

    t0 = time.monotonic()
    try:
        assert pipeline.run_search_index(conn, Rep()) == 0
        assert time.monotonic() - t0 < 10
        assert any(level == "warn" and msg.startswith("Search: stopped. The search model didn't finish") for level, msg in logs)
        assert search.Model._inst is None and search.Model._cpu_only        # the next load is on the CPU
        assert conn.execute("SELECT value FROM meta WHERE key=?", (search.INDEXING_KEY,)).fetchone() is None

        # The picture model's step the same way.
        monkeypatch.setattr(ai.Models, "_inst", type("M", (), {"embed_images": lambda self, imgs: hang.wait(30)})())
        monkeypatch.setattr(ai.Models, "_cpu_only", False)
        with pytest.raises(ai.GpuStuck):
            ai.run(conn)
        assert ai.Models._inst is None and ai.Models._cpu_only
    finally:
        hang.set()
