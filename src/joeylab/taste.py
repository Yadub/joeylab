"""Learn what you keep from your own picks and rejects.

A regularised logistic regression over each photo's quality signals and (when the AI
layer is on) its CLIP embedding, trained on every project's decisions. It is only
allowed to shape suggestions once cross-validation shows it ranks keepers above
rejects reliably.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time

import numpy as np

from . import config, db

def _model_path():
    # Looked up on each use (not at import), so JOEYLAB_HOME always decides where it lives.
    return config.HOME / "taste.npz"


def _meta_path():
    return config.HOME / "taste.json"
MIN_EACH = 12          # positives and negatives needed before training
TRUST_AUC = 0.62       # cross-validated AUC needed before taste affects suggestions
LAMBDAS = (0.3, 3.0, 30.0, 300.0)
MAX_EACH = 3000        # beyond this many keepers or rejects, train on an even sample (same one each time)
EMB_DIM = 768


def _scalars(m) -> list[float]:
    has_face = 1.0 if (m["main_faces"] or 0) > 0 else 0.0
    return [
        float(np.log10(max(m["sharp_subject"] or 1, 1))),
        float(np.log10(max(m["face_sharp"] or 1, 1))) * has_face,
        has_face,
        (m["brightness"] or 0) / 255,
        min(1.0, (m["clip_high"] or 0) * 5),
        min(6, m["main_faces"] or 0) / 6,
        ((m["eyes_closed"] or 0) / m["main_faces"]) if m["main_faces"] else 0.0,
        float(m["smile"] or 0),
        float(m["aesthetic"] or 0),
        1.0 if m["aesthetic"] is not None else 0.0,
    ]


N_SCALAR = 10


def _has_embeddings(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'").fetchone())


def _rows(conn, labelled: bool, after_id: int = 0, limit: int | None = None):
    from .ai import MODEL_TAG

    emb_join = ("LEFT JOIN embeddings e ON e.shot_id = s.id AND e.src = s.analyzed_fp AND e.model = ?"
                if _has_embeddings(conn) else "")
    emb_col = "e.vec" if emb_join else "NULL"
    q = (f"SELECT s.id, m.*, d.rating, d.pick, {emb_col} AS vec FROM shots s JOIN metrics m ON m.shot_id = s.id"
         f" LEFT JOIN decisions d ON d.shot_id = s.id {emb_join} WHERE s.status='present' AND s.id > ?")
    args: list = ([MODEL_TAG] if emb_join else []) + [after_id]
    if labelled:
        q += " AND (d.pick != 0 OR d.rating IN (1, 4, 5))"
        # An extra copy of a duplicate is rejected for being a copy, not for how it looks: rejecting
        # thousands of them mustn't teach taste that those pictures are bad.
        q += (" AND NOT EXISTS (SELECT 1 FROM dup_members dm JOIN dup_sets ds ON ds.id = dm.set_id"
              " AND ds.kind IN ('exact', 'copy') WHERE dm.shot_id = s.id AND dm.keep = 0)")
    q += " ORDER BY s.id"
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, args).fetchall()


def _label(r) -> int | None:
    if r["pick"] == 1 or (r["rating"] or 0) >= 4:
        return 1
    if r["pick"] == -1 or r["rating"] == 1:
        return 0
    return None


def _features(rows, use_emb: bool, dtype=np.float64) -> np.ndarray:
    X = np.zeros((len(rows), N_SCALAR + (EMB_DIM if use_emb else 0)), dtype=dtype)
    for i, r in enumerate(rows):
        X[i, :N_SCALAR] = _scalars(r)
        if use_emb and r["vec"] is not None:
            X[i, N_SCALAR:] = np.frombuffer(r["vec"], dtype=np.float16)
    return X


# ---------------------------------------------------------------- the model

def _fit(X: np.ndarray, y: np.ndarray, lam: float, iters: int = 25) -> np.ndarray:
    """Newton's method for L2-regularised logistic regression (bias unpenalised)."""
    n, d = X.shape
    Xb = np.hstack([X, np.ones((n, 1))])
    w = np.zeros(d + 1)
    reg = np.full(d + 1, lam)
    reg[-1] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Xb @ w, -30, 30)))
        g = Xb.T @ (p - y) + reg * w
        H = (Xb * (p * (1 - p))[:, None]).T @ Xb + np.diag(reg + 1e-8)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w


def _predict(Xs: np.ndarray, w: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(Xs @ w[:-1] + w[-1], -30, 30)))


def auc(y: np.ndarray, s: np.ndarray) -> float:
    pos, neg = s[y == 1], s[y == 0]
    if not len(pos) or not len(neg):
        return 0.5
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty(len(order))
    ranks[order] = np.arange(1, len(order) + 1)
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def _cv(X, y, lam, folds=5, seed=0) -> float:
    rng = np.random.default_rng(seed)
    idx = {c: rng.permutation(np.nonzero(y == c)[0]) for c in (0, 1)}
    scores = []
    for k in range(folds):
        test = np.concatenate([idx[c][k::folds] for c in (0, 1)])
        train = np.setdiff1d(np.arange(len(y)), test)
        mu, sd = X[train].mean(0), X[train].std(0) + 1e-6
        w = _fit((X[train] - mu) / sd, y[train], lam)
        scores.append(auc(y[test], _predict((X[test] - mu) / sd, w)))
    return float(np.mean(scores))


def train_all() -> dict:
    """Train on every project's decisions. Returns the stats saved alongside the model."""
    started = time.time()
    rows = []
    skipped = []
    emb_ok = True
    for name in config.list_projects():
        # A library whose catalog can't be read (damaged, or removed meanwhile) is left out: the
        # others still train, and their updates don't stop over it.
        try:
            conn = db.connect(name, create=False)
        except (db.CatalogDamaged, FileNotFoundError) as e:
            skipped.append(name)
            print(f"[joeylab] taste: left out {name!r}: {e}", file=sys.stderr, flush=True)
            continue
        try:
            rows += [r for r in _rows(conn, labelled=True) if _label(r) is not None]
        except sqlite3.DatabaseError as e:
            skipped.append(name)
            print(f"[joeylab] taste: left out {name!r}: {e}", file=sys.stderr, flush=True)
        finally:
            conn.close()
    y = np.array([_label(r) for r in rows], dtype=np.float64)
    n_pos, n_neg = int((y == 1).sum()), int((y == 0).sum())
    meta = {"positives": n_pos, "negatives": n_neg, "trained_at": time.time(), "trusted": False, "auc": None,
            "needed": MIN_EACH}
    if skipped:
        meta["skipped"] = skipped
    if n_pos < MIN_EACH or n_neg < MIN_EACH:
        meta["status"] = "learning"
        _save_meta(meta)
        _model_path().unlink(missing_ok=True)
        return meta
    # A big archive can hold tens of thousands of ratings; a few thousand of each kind teach as much
    # and keep retraining to seconds.
    if n_pos > MAX_EACH or n_neg > MAX_EACH:
        rng = np.random.default_rng(0)
        keep = np.concatenate([rng.permutation(np.nonzero(y == c)[0])[:MAX_EACH] for c in (0, 1)])
        keep.sort()
        rows, y = [rows[i] for i in keep], y[keep]
        meta["sampled"] = len(rows)
    emb_ok = sum(r["vec"] is not None for r in rows) >= 0.9 * len(rows)
    X = _features(rows, emb_ok)
    folds = max(2, min(5, int((y == 1).sum()), int((y == 0).sum())))
    cv = {lam: _cv(X, y, lam, folds) for lam in LAMBDAS}
    lam = max(cv, key=cv.get)
    mu, sd = X.mean(0), X.std(0) + 1e-6
    w = _fit((X - mu) / sd, y, lam)
    config.HOME.mkdir(parents=True, exist_ok=True)
    # Written to a temporary file and swapped in, so a reader never sees half a model.
    tmp = _model_path().with_name(f".taste-{os.getpid()}-{threading.get_ident()}.npz")
    np.savez(tmp, w=w, mu=mu, sd=sd, use_emb=np.array([emb_ok]))
    config.private(tmp)
    os.replace(tmp, _model_path())
    meta.update(auc=round(cv[lam], 3), trusted=cv[lam] >= TRUST_AUC, lam=lam, uses_embeddings=emb_ok,
                status="trusted" if cv[lam] >= TRUST_AUC else "weak", seconds=round(time.time() - started, 2),
                explain=_explain(w, sd, emb_ok))
    _save_meta(meta)
    return meta


SCALAR_NAMES = ["subject focus", "face focus", "faces in frame", "brightness", "clipped highlights",
                "more people", "closed eyes", "smiles", "aesthetic score", "aesthetic known"]


def _explain(w: np.ndarray, sd: np.ndarray, emb: bool) -> list[dict]:
    """What the model learned, in words: the scalar signals that push a photo up or down most.
    (Weights are on standardised features, so they are comparable.)"""
    out = []
    for i, name in enumerate(SCALAR_NAMES):
        if name == "aesthetic known":
            continue
        out.append({"signal": name, "weight": round(float(w[i]), 3)})
    out.sort(key=lambda x: -abs(x["weight"]))
    if emb:
        e = float(np.abs(w[N_SCALAR:-1]).sum())
        s = float(np.abs(w[:N_SCALAR]).sum())
        out.append({"signal": "what's in the picture (AI embedding)", "share": round(e / max(1e-9, e + s), 2)})
    return out


def _save_meta(meta: dict) -> None:
    config.HOME.mkdir(parents=True, exist_ok=True)
    tmp = _meta_path().with_name(f".taste-{os.getpid()}-{threading.get_ident()}.json")
    tmp.write_text(json.dumps(meta))
    config.private(tmp)
    os.replace(tmp, _meta_path())


def meta() -> dict:
    try:
        return json.loads(_meta_path().read_text())
    except (OSError, ValueError):
        return {"status": "learning", "positives": 0, "negatives": 0, "needed": MIN_EACH, "trusted": False}


SCORE_CHUNK = 4000


def score(conn) -> int:
    """Write metrics.taste (0-1) for every present photo, a chunk at a time in float32 (so a
    500k-photo library doesn't need gigabytes). Only scores that changed are written, a chunk to a
    commit: writing every photo's in one go held the catalog's write lock for seconds, and other
    changes (a decision from the phone) waited. Returns how many were scored."""
    if not _model_path().exists():
        conn.execute("UPDATE metrics SET taste = NULL")
        conn.commit()
        return 0
    try:
        m = np.load(_model_path())
        w, mu, sd, use_emb = m["w"], m["mu"].astype(np.float32), m["sd"].astype(np.float32), bool(m["use_emb"][0])
    except (OSError, ValueError, KeyError):
        return 0
    n, after = 0, 0
    while True:
        rows = _rows(conn, labelled=False, after_id=after, limit=SCORE_CHUNK)
        if not rows:
            break
        X = _features(rows, use_emb, dtype=np.float32)
        if X.shape[1] != len(mu):
            # A model trained on another feature set (e.g. a different embedding): its old
            # scores would be misleading, so clear them until the next training.
            conn.execute("UPDATE metrics SET taste = NULL")
            conn.commit()
            return 0
        p = _predict((X - mu) / sd, w.astype(np.float32))
        changed = [(t, r["id"]) for v, r in zip(p, rows) if (t := round(float(v), 4)) != r["taste"]]
        if changed:
            conn.executemany("UPDATE metrics SET taste=? WHERE shot_id=?", changed)
            conn.commit()
        n += len(rows)
        after = rows[-1]["id"]
    conn.commit()
    return n
