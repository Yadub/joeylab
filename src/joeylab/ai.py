"""Optional AI layer (pip extra `ai`): CLIP embeddings for semantic grouping and text search,
plus the LAION aesthetic predictor. Runs locally (Apple GPU via MPS when available)."""

from __future__ import annotations

import threading
from typing import Callable

import numpy as np

from . import config, db
from .config import MODELS_DIR

CLIP_ARCH, CLIP_PRETRAINED = "ViT-L-14-quickgelu", "openai"
MODEL_TAG = f"{CLIP_ARCH}/{CLIP_PRETRAINED}"
AESTHETIC_PATH = MODELS_DIR / "aesthetic_l14.pth"
AESTHETIC_URL = ("https://github.com/christophschuhmann/improved-aesthetic-predictor/raw/"
                 "6934dd81792f086e613a121dbce43082cb8be85e/sac+logos+ava1-l14-linearMSE.pth")
AESTHETIC_SHA256 = "21dd590f3ccdc646f0d53120778b296013b096a035a2718c9cb0d511bff0f1e0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    shot_id INTEGER PRIMARY KEY,
    model   TEXT NOT NULL,
    src     TEXT NOT NULL,      -- analyzed_fp of the thumbnail it came from
    vec     BLOB NOT NULL       -- float16, L2-normalised
);
CREATE INDEX IF NOT EXISTS embeddings_shot ON embeddings(shot_id);     -- counted from this, not the vectors
"""


def available() -> bool:
    try:
        import open_clip  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


_LOCK = threading.RLock()        # loading the model; the loaded one is shared by web requests and background jobs
_MATRIX: dict[str, tuple] = {}   # db file -> (stamp, ids, matrix) for fast repeated searches


class Models:
    _inst: "Models | None" = None
    _cpu_only = False                # the graphics processor stopped answering once: the CPU from then on

    def __init__(self):
        import open_clip
        import torch

        self.torch = torch
        self.lock = threading.RLock()
        self.device = "mps" if torch.backends.mps.is_available() and not Models._cpu_only else "cpu"
        self.model, _, self.preprocess = config.offline_first(lambda: open_clip.create_model_and_transforms(CLIP_ARCH, pretrained=CLIP_PRETRAINED))
        self.model = self.model.to(self.device).eval()
        self.tokenizer = config.offline_first(lambda: open_clip.get_tokenizer(CLIP_ARCH))
        self.aesthetic = self._load_aesthetic()

    @classmethod
    def get(cls) -> "Models":
        with _LOCK:
            if cls._inst is None:
                cls._inst = Models()
            return cls._inst

    @classmethod
    def loaded(cls) -> bool:
        return cls._inst is not None

    @classmethod
    def drop_stuck(cls) -> None:
        """The model hung on the graphics processor: leave it behind; the next use loads one on the CPU."""
        with _LOCK:
            cls._inst = None
            cls._cpu_only = True

    def _load_aesthetic(self):
        torch = self.torch
        if not AESTHETIC_PATH.exists():
            from . import downloads

            downloads.fetch(AESTHETIC_URL, AESTHETIC_SHA256, AESTHETIC_PATH)
        nn = torch.nn
        mlp = nn.Sequential(
            nn.Linear(768, 1024), nn.Dropout(0.2), nn.Linear(1024, 128), nn.Dropout(0.2),
            nn.Linear(128, 64), nn.Dropout(0.1), nn.Linear(64, 16), nn.Linear(16, 1),
        )
        sd = torch.load(AESTHETIC_PATH, map_location="cpu")
        mlp.load_state_dict({k.replace("layers.", ""): v for k, v in sd.items()})
        return mlp.to(self.device).eval()

    def embed_images(self, images) -> tuple[np.ndarray, np.ndarray]:
        torch = self.torch
        with self.lock, torch.no_grad():
            x = torch.stack([self.preprocess(im) for im in images]).to(self.device)
            e = self.model.encode_image(x).float()
            e = e / e.norm(dim=-1, keepdim=True)
            a = self.aesthetic(e).squeeze(-1)
        return e.cpu().numpy(), a.cpu().numpy()

    def embed_text(self, text: str) -> np.ndarray:
        torch = self.torch
        with self.lock, torch.no_grad():
            t = self.model.encode_text(self.tokenizer([text]).to(self.device)).float()
            t = t / t.norm(dim=-1, keepdim=True)
        return t.cpu().numpy()[0]


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def pending(conn) -> list:
    ensure_schema(conn)
    return conn.execute(
        "SELECT s.id, s.thumb, s.analyzed_fp FROM shots s LEFT JOIN embeddings e ON e.shot_id = s.id"
        " WHERE s.status='present' AND s.thumb IS NOT NULL AND s.analyzed_fp IS NOT NULL"
        " AND (e.shot_id IS NULL OR e.src != s.analyzed_fp OR e.model != ?)",
        (MODEL_TAG,),
    ).fetchall()


class GpuStuck(RuntimeError):
    """The model stopped answering on the graphics processor (what was done is kept)."""


def run(conn, batch: int = 32, progress: Callable[[int, int], None] | None = None) -> int:
    """Embed pending photos. A batch the model doesn't finish in GPU_TIMEOUT_S raises GpuStuck, so
    a hang on the graphics processor can't hold up the update."""
    from PIL import Image

    from . import deadline

    rows = pending(conn)
    if not rows:
        return 0
    m = Models.get()
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        imgs, ok = [], []
        for r in chunk:
            try:
                imgs.append(Image.open(r["thumb"]).convert("RGB"))
                ok.append(r)
            except OSError:
                continue
        if imgs:
            try:
                emb, aes = deadline.call(m.embed_images, imgs, timeout=config.GPU_TIMEOUT_S, what="The picture model")
            except deadline.TimedOut as e:
                Models.drop_stuck()
                raise GpuStuck(str(e)) from None
            for r, e, a in zip(ok, emb, aes):
                conn.execute(
                    "INSERT INTO embeddings(shot_id, model, src, vec) VALUES (?,?,?,?)"
                    " ON CONFLICT(shot_id) DO UPDATE SET model=excluded.model, src=excluded.src, vec=excluded.vec",
                    (r["id"], MODEL_TAG, r["analyzed_fp"], e.astype(np.float16).tobytes()),
                )
                conn.execute("UPDATE metrics SET aesthetic=? WHERE shot_id=?", (float(a), r["id"]))
        db.bump(conn, "emb_gen")
        conn.commit()
        done += len(chunk)
        if progress:
            progress(done, len(rows))
    return done


_LOADED: dict[str, tuple] = {}


def load_embeddings(conn) -> dict[int, np.ndarray]:
    """CLIP vectors of present photos, cached per catalog until they change (grouping, scoring and
    search used to load them separately on every run)."""
    try:
        db_file = conn.execute("PRAGMA database_list").fetchone()[2]
        stamp = (db.counter(conn, "emb_gen"), conn.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0],
                 conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])
    except Exception:
        return {}
    hit = _LOADED.get(db_file)
    if hit and hit[0] == stamp:
        return hit[1]
    rows = conn.execute(
        "SELECT e.shot_id, e.vec FROM embeddings e JOIN shots s ON s.id=e.shot_id"
        " WHERE s.status='present' AND e.model=? AND e.src = s.analyzed_fp", (MODEL_TAG,)
    ).fetchall()
    out = {r[0]: np.frombuffer(r[1], dtype=np.float16).astype(np.float32) for r in rows}
    _LOADED.clear()          # one catalog at a time is plenty: keeps memory bounded
    _LOADED[db_file] = (stamp, out)
    return out


def _matrix(conn) -> tuple[np.ndarray, np.ndarray] | None:
    """All current embeddings as (ids, matrix), cached until the embeddings table changes."""
    try:
        db_file = conn.execute("PRAGMA database_list").fetchone()[2]
        stamp = (db.counter(conn, "emb_gen"),) + tuple(conn.execute(
            "SELECT COUNT(*), (SELECT COUNT(*) FROM shots WHERE status='present') FROM embeddings e").fetchone())
    except Exception:
        return None
    hit = _MATRIX.get(db_file)
    if hit and hit[0] == tuple(stamp):
        return hit[1], hit[2]
    embs = load_embeddings(conn)
    if not embs:
        return None
    ids = np.array(list(embs.keys()))
    mat = np.stack(list(embs.values()))
    _MATRIX[db_file] = (tuple(stamp), ids, mat)
    return ids, mat


def search(conn, text: str, limit: int = 30) -> list[tuple[int, float]]:
    m = _matrix(conn)
    if m is None:
        return []
    ids, mat = m
    sims = mat @ Models.get().embed_text(text)
    order = np.argsort(-sims)[:limit]
    return [(int(ids[i]), float(sims[i])) for i in order]


def similar(conn, shot_id: int, limit: int = 30) -> list[tuple[int, float]]:
    m = _matrix(conn)
    if m is None:
        return []
    ids, mat = m
    where = np.nonzero(ids == shot_id)[0]
    if not len(where):
        return []
    sims = mat @ mat[where[0]]
    order = [i for i in np.argsort(-sims) if ids[i] != shot_id][:limit]
    return [(int(ids[i]), float(sims[i])) for i in order]
