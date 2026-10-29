"""Catalog copies kept before a restore, and writing XMP sidecars: the copy holding the catalog as it
was before any restore is never thrown away, and a sidecar another app wrote is never changed after
a failed read, one failure never stops the rest, and nothing is written during an update."""

from __future__ import annotations

import errno
import os
import threading

import pytest

from conftest import add_root, make_jpeg, run_all


def test_the_first_copy_kept_before_a_restore_is_never_pruned(env):
    from joeylab import backup, config

    _, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    run_all()
    src = backup.backup("t", force=True)
    kept = [backup.restore("t", src)[1] for _ in range(4)]           # four restores, all in about a second
    assert len({k.name for k in kept}) == 4                             # none written over another
    d = config.HOME / "backups" / "t"
    for i, name in enumerate(["before-restore-20260101-090000.db", "before-restore-20260102-090000.db"]):
        (d / name).write_bytes(b"x")
        os.utime(d / name, (10**9 + i, 10**9 + i))                      # an old modified time doesn't matter
    backup.prune("t")
    left = sorted(p.name for p in d.glob("before-restore-*.db"))
    assert "before-restore-20260101-090000.db" in left                  # the catalog before any restore
    assert len(left) == 3 and kept[-1].name in left and kept[-2].name in left


@pytest.fixture
def two_sidecars(env):
    from joeylab import export, library

    _, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    make_jpeg(lib / "IMG_2.JPG", seed=2)
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    library.decide(conn, [{"id": i, "rating": 2, "label": "Red"} for i in ids])
    theirs = export._new_sidecar({}, (5, None), {}, ("rating",))          # another app's: ★5, no label
    for n in ("IMG_1.xmp", "IMG_2.xmp"):
        (lib / n).write_text(theirs, encoding="utf-8")
    return conn, lib, ids


def _tags(path):
    from joeylab import exif

    tags, _ = exif.read_many([str(path)], tags=["Rating", "Label"])
    return tags.get(str(path), {})


def test_a_sidecar_that_cant_be_read_is_left_alone(two_sidecars, monkeypatch):
    from joeylab import exif, export
    from joeylab.library import Filter

    if not exif.exiftool():
        pytest.skip("exiftool isn't installed")
    conn, lib, ids = two_sidecars
    before = (lib / "IMG_1.xmp").read_bytes()
    real = exif.read_many

    def flaky(paths, *a, **k):                                           # IMG_1.xmp's drive dropped out for a moment
        tags, done = real([p for p in paths if not p.endswith("IMG_1.xmp")], *a, **k)
        return tags, done

    monkeypatch.setattr(exif, "read_many", flaky)
    r = export.write_sidecars(conn, Filter(ids=ids), include=("rating", "label"), update_existing=True, project="t")
    monkeypatch.undo()
    assert (lib / "IMG_1.xmp").read_bytes() == before
    assert r.errors == [f"{lib / 'IMG_1.xmp'}: couldn't be read, so it was left as it is"]
    assert r.updated == 1 and _tags(lib / "IMG_2.xmp") == {**_tags(lib / "IMG_2.xmp"), "Rating": 5, "Label": "Red"}


def test_one_sidecar_failing_never_stops_the_rest(two_sidecars, monkeypatch):
    from joeylab import exif, export
    from joeylab.library import Filter

    if not exif.exiftool():
        pytest.skip("exiftool isn't installed")
    conn, lib, ids = two_sidecars
    real = export._backup_sidecar

    def full(side, d):
        if side.endswith("IMG_1.xmp"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(side, d)

    monkeypatch.setattr(export, "_backup_sidecar", full)
    r = export.write_sidecars(conn, Filter(ids=ids), include=("rating", "label"), update_existing=True, project="t")
    assert r.errors == [f"{lib / 'IMG_1.xmp'}: No space left on device"] and r.updated == 1
    assert _tags(lib / "IMG_1.xmp").get("Label") is None and _tags(lib / "IMG_2.xmp")["Label"] == "Red"


def test_sidecars_arent_written_during_an_update(two_sidecars, monkeypatch):
    from fastapi.testclient import TestClient

    from joeylab import locks
    from joeylab.web.routes import share
    from joeylab.web.server import create_app

    conn, lib, ids = two_sidecars
    monkeypatch.setattr(share, "XMP_LOCK_WAIT_S", 0.3)
    before = (lib / "IMG_1.xmp").read_bytes()
    ready, release = threading.Event(), threading.Event()

    def update():
        with locks.project_lock("t"):
            ready.set()
            release.wait(30)

    t = threading.Thread(target=update, daemon=True)
    t.start()
    assert ready.wait(10)
    try:
        r = TestClient(create_app()).post("/api/p/t/export/xmp", json={"confirm": True, "ids": ids, "update_existing": True,
                                                                        "include": ["rating", "label"]})
        assert r.status_code == 409 and "no sidecars were written" in r.json()["error"]
        assert (lib / "IMG_1.xmp").read_bytes() == before
    finally:
        release.set()
        t.join(10)
