"""The search store (~/.joeylab/cache/search): searching while photos are being indexed reuses it
rather than rebuilding it after every few photos, and what crashed builds and removed libraries
left there is cleared away."""

from __future__ import annotations

import os
import time

import numpy as np

from conftest import make_jpeg


class _FakeModel:
    """Stands in for SigLIP: the store only needs some unit vectors."""

    def embed_images(self, images):
        v = np.random.default_rng(len(images)).normal(size=(len(images), 16)).astype(np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)


def _library(conn, tmp, n):
    for i in range(n):
        thumb = make_jpeg(tmp / "thumbs" / f"{i}.jpg", seed=i, size=(120, 90))
        conn.execute("INSERT INTO shots(dir, stem, thumb, analyzed_fp) VALUES ('/lib', ?, ?, ?)",
                     (f"IMG_{i}", str(thumb), f"fp{i}"))
    conn.commit()


def _index_offline(monkeypatch):
    from joeylab import search

    monkeypatch.setattr(search.Model, "get", classmethod(lambda cls: _FakeModel()))
    monkeypatch.setattr(search, "vision_available", lambda: False)


def test_searching_while_indexing_doesnt_rebuild_the_store_each_time(env, monkeypatch):
    from joeylab import db, search

    tmp, _ = env
    conn = db.connect("t")
    _library(conn, tmp, 30)
    _index_offline(monkeypatch)
    builds = []
    real = search._open_store
    monkeypatch.setattr(search, "_open_store", lambda *a: builds.append(1) or real(*a))
    found = []
    # A search after every batch of 3, as someone searching from a phone during an update would.
    search.run(conn, progress=lambda done, total: found.append(len(search._matrix(conn)[0])), batch=3)
    assert len(found) == 10
    assert len(builds) == 1                   # the first search: there was no store yet
    # Indexing over: the next search has every photo.
    assert len(search._matrix(conn)[0]) == 30 and len(builds) == 2


def test_a_removed_photo_leaves_the_store_at_once_even_while_indexing(env, monkeypatch):
    from joeylab import db, search

    tmp, _ = env
    conn = db.connect("t")
    _library(conn, tmp, 6)
    _index_offline(monkeypatch)
    search.run(conn)
    assert len(search._matrix(conn)[0]) == 6
    search._indexing(conn, True)              # an update is indexing other photos meanwhile
    conn.execute("UPDATE shots SET status='missing' WHERE stem='IMG_0'")
    conn.commit()
    assert len(search._matrix(conn)[0]) == 5


def test_stray_files_and_removed_libraries_stores_are_cleared(env, monkeypatch):
    from joeylab import config, db, search

    tmp, _ = env
    conn = db.connect("t")
    _library(conn, tmp, 3)
    _index_offline(monkeypatch)
    search.run(conn)
    search._matrix(conn)
    folder = search._store_dir()
    live = sorted(p.name for p in folder.iterdir())
    assert len(live) == 2                      # this library's store: ids and vectors
    gone = search._prefix(str(config.HOME / "projects" / "old" / "catalog.db"))
    for name in (f"{gone}-abc.ids.npy", f"{gone}-abc.f16"):
        (folder / name).write_bytes(b"x")
    old_tmp, new_tmp = folder / ".dead.tmp", folder / ".building.tmp"
    old_tmp.write_bytes(b"x")
    new_tmp.write_bytes(b"x")
    hour_ago = time.time() - 3 * 3600
    os.utime(old_tmp, (hour_ago, hour_ago))
    search.tidy_store()
    # A build under way (a recent temporary file) is left to finish.
    assert sorted(p.name for p in folder.iterdir()) == sorted(live + [new_tmp.name])
    assert len(search._matrix(conn)[0]) == 3


def test_the_service_tidies_the_store_when_it_starts(env, monkeypatch):
    from joeylab import search

    folder = search._store_dir()
    folder.mkdir(parents=True)
    (folder / "0123456789ab-abc.f16").write_bytes(b"x")         # no library has this one
    monkeypatch.setattr(search.Model, "get", classmethod(lambda cls: _FakeModel()))
    monkeypatch.setattr(_FakeModel, "embed_text", lambda self, text: None, raising=False)
    search.warm_up(delay=0)
    assert not list(folder.iterdir())
