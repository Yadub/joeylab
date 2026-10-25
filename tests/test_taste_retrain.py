"""Taste retraining after decisions is never dropped: when it can't run (an update is under way, the
library's lock is taken, a retrain is still running) it waits and tries again, and the decisions
still count. One damaged catalog doesn't stop training on the others."""

from __future__ import annotations

import time

import pytest


class _Jobs:
    def __init__(self):
        self.running: dict = {}


@pytest.fixture()
def bg(env, monkeypatch):
    from joeylab import db, scoring, taste
    from joeylab.web.server import Background

    db.connect("t").close()
    trained: list[float] = []
    monkeypatch.setattr(taste, "train_all", lambda: trained.append(time.time()) or {})
    monkeypatch.setattr(scoring, "run", lambda conn: None)
    b = Background(_Jobs())
    b.QUIET_S, b.MIN_GAP_S = 0.05, 0.1
    return b, trained


def _soon(cond, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_retraining_waits_for_a_running_update(bg):
    b, trained = bg
    b.jobs.running["t"] = object()
    b.decided("t", 20)
    time.sleep(0.4)                       # the timer fires while the update runs
    assert not trained and b.pending["t"] == 20
    del b.jobs.running["t"]
    assert _soon(lambda: trained) and _soon(lambda: b.pending["t"] == 0)


def test_retraining_waits_for_the_librarys_lock(bg):
    from joeylab import locks

    b, trained = bg
    with locks.project_lock("t"):         # an update from the command line, say
        b.decided("t", 20)
        time.sleep(0.4)
        assert not trained and _soon(lambda: b.pending["t"] == 20)      # still counted
    assert _soon(lambda: trained) and _soon(lambda: b.pending["t"] == 0)


def test_decisions_made_during_a_retrain_get_one_of_their_own(bg, monkeypatch):
    from joeylab import taste

    b, trained = bg
    slow = lambda: time.sleep(0.5) or trained.append(time.time()) or {}   # noqa: E731
    monkeypatch.setattr(taste, "train_all", slow)
    b.decided("t", 20)
    assert _soon(lambda: b.busy("retrain:t"))
    b.decided("t", 20)                    # its timer fires while the first retrain is still running
    assert _soon(lambda: len(trained) == 2) and _soon(lambda: b.pending["t"] == 0)


def test_a_damaged_catalog_doesnt_stop_training_on_the_others(env):
    from joeylab import config, db, taste

    db.connect("good").close()
    db.connect("bad").close()
    (config.project_dir("bad") / "catalog.db").write_bytes(b"this is not a database" * 100)
    meta = taste.train_all()
    assert meta["status"] == "learning" and meta["skipped"] == ["bad"]
