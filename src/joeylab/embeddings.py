"""One place to get each photo's whole-frame AI vector, cached in memory until it changes.

SigLIP 2 (the search model) is preferred; CLIP is used for photos only it has seen. Caches are
invalidated by counters the indexers bump (meta: search_gen, emb_gen), so a re-indexed photo is
never served stale.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

import numpy as np

from . import db

_CACHE: dict[str, tuple] = {}
_LOCK = threading.Lock()


def _stamp(conn) -> tuple:
    return (db.counter(conn, "search_gen"), db.counter(conn, "emb_gen"),
            conn.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0])


def _table(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def matrix(conn) -> tuple[np.ndarray, np.ndarray, str] | None:
    """(ids, unit vectors float16, model name), for every present photo with a vector."""
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    stamp = _stamp(conn)
    with _LOCK:
        hit = _CACHE.get(db_file)
        if hit and hit[0] == stamp:
            return hit[1]
    ids, vecs, model = [], [], None
    if _table(conn, "search_vecs"):
        from .search import N_CROPS, _matrix

        m = _matrix(conn)                    # the search store: every photo's first row is its whole frame
        if m is not None:
            out = (m[0], np.ascontiguousarray(m[1][::N_CROPS]), "siglip2")
            with _LOCK:
                _CACHE[db_file] = (stamp, out)
            return out
    if _table(conn, "embeddings"):
        from .ai import MODEL_TAG

        for sid, blob in conn.execute(
                "SELECT e.shot_id, e.vec FROM embeddings e JOIN shots s ON s.id = e.shot_id"
                " WHERE s.status='present' AND e.model = ? AND e.src = s.analyzed_fp", (MODEL_TAG,)):
            ids.append(sid)
            vecs.append(np.frombuffer(blob, dtype=np.float16))
        model = "clip"
    if not ids:
        return None
    out = (np.array(ids), np.stack(vecs), model)
    with _LOCK:
        _CACHE[db_file] = (stamp, out)
    return out


class Frames(Mapping):
    """{photo id: whole-frame vector}, read from the float16 matrix one photo at a time as asked
    (a float32 copy of every vector would be gigabytes in a big library)."""

    def __init__(self, ids: np.ndarray, mat: np.ndarray):
        self._mat = mat
        self._row = {int(i): k for k, i in enumerate(ids)}

    def __getitem__(self, sid) -> np.ndarray:
        return self._mat[self._row[sid]].astype(np.float32)

    def __contains__(self, sid) -> bool:
        return sid in self._row

    def __iter__(self):
        return iter(self._row)

    def __len__(self) -> int:
        return len(self._row)


def whole_frame(conn) -> Mapping[int, np.ndarray]:
    m = matrix(conn)
    if m is None:
        return {}
    ids, mat, _ = m
    return Frames(ids, mat)


def text_vector(conn, text: str) -> np.ndarray | None:
    """A text query in the same space as matrix()."""
    m = matrix(conn)
    if m is None:
        return None
    if m[2] == "siglip2":
        from .search import Model

        return Model.get().embed_text(text)
    from .ai import Models

    return Models.get().embed_text(text)
