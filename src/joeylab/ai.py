"""Optional AI layer (pip extra `ai`): CLIP embeddings for semantic grouping and text search,
plus the LAION aesthetic predictor. Runs locally (Apple GPU via MPS when available)."""

from __future__ import annotations

import gc
import sys
import threading
import time
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
    """Whether the AI extra is installed, without importing it (torch takes seconds to import: the
    first page after a restart used to wait for it)."""
    from importlib.util import find_spec

    return all(find_spec(m) is not None for m in ("open_clip", "torch"))


_LOCK = threading.RLock()        # loading the model; the loaded one is shared by web requests and background jobs
IDLE_S = 600.0                   # the model and the vectors kept in memory are let go after this long unused


class Models:
    """CLIP, loaded when something needs it and let go when it has been idle for IDLE_S (the service
    runs for weeks; once a library has a picture-search index, only updates with new photos need it).
    text_only: for searches and words only: the text half, at half precision on the graphics
    processor (its text vectors match full precision's to five decimal places), about a fifth of the
    memory. A photo to embed loads the whole model at full precision, as the stored vectors were made."""

    _inst: "Models | None" = None
    _cpu_only = False                # the graphics processor stopped answering once: the CPU from then on

    def __init__(self, text_only: bool = False):
        import open_clip
        import torch

        self.torch = torch
        self.lock = threading.RLock()
        self.text_only = text_only
        self.used = time.monotonic()
        self.device = "mps" if torch.backends.mps.is_available() and not Models._cpu_only else "cpu"
        model, _, self.preprocess = config.offline_first(lambda: open_clip.create_model_and_transforms(CLIP_ARCH, pretrained=CLIP_PRETRAINED))
        self.dtype = torch.float16 if text_only and self.device == "mps" else torch.float32
        if text_only:
            model.visual = None
        self.model = model.to(dtype=self.dtype).to(self.device).eval()     # cast here: no full-size copy on the GPU
        self.tokenizer = config.offline_first(lambda: open_clip.get_tokenizer(CLIP_ARCH))
        self.aesthetic = None if text_only else self._load_aesthetic()

    @classmethod
    def get(cls, text_only: bool = False) -> "Models":
        """The loaded model, or a new one. Asked for photos while only the text half is loaded, it
        loads the whole model in its place."""
        with _LOCK:
            inst = cls._inst
            if inst is None or (getattr(inst, "text_only", False) and not text_only):
                cls._inst = inst = None            # the text half goes before the whole model loads
                inst = cls._inst = Models(text_only)
                _watch_idle()
            inst.used = time.monotonic()
            return inst

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
        if self.text_only:
            raise RuntimeError("This copy of the picture model has only its text half: use Models.get()")
        torch = self.torch
        self.used = time.monotonic()
        with self.lock, torch.no_grad():
            x = torch.stack([self.preprocess(im) for im in images]).to(self.device)
            e = self.model.encode_image(x).float()
            e = e / e.norm(dim=-1, keepdim=True)
            a = self.aesthetic(e).squeeze(-1)
        return e.cpu().numpy(), a.cpu().numpy()

    def embed_text(self, text: str) -> np.ndarray:
        torch = self.torch
        self.used = time.monotonic()
        with self.lock, torch.no_grad():
            t = self.model.encode_text(self.tokenizer([text]).to(self.device)).float()
            t = t / t.norm(dim=-1, keepdim=True)
        return t.cpu().numpy()[0]


_VECS: dict[str, tuple] = {}     # db file -> (stamp, ids, float32 matrix, {photo id: its row}, last used)
_WATCHING = False


def _watch_idle() -> None:
    """Start the thread that lets go of what's idle, unless it's running. (Call holding _LOCK.)"""
    global _WATCHING
    if not _WATCHING:
        _WATCHING = True
        threading.Thread(target=_idle_loop, name="ai-idle", daemon=True).start()


def _idle_loop() -> None:
    global _WATCHING
    while True:
        time.sleep(min(60.0, IDLE_S / 4))
        release_idle()
        with _LOCK:
            if Models._inst is None and not _VECS:
                _WATCHING = False
                return


def release_idle(idle_s: float | None = None) -> bool:
    """Let go of the model and the vectors kept in memory when nothing has used them for idle_s
    (IDLE_S). A model in use is never let go. True when anything was."""
    idle_s = IDLE_S if idle_s is None else idle_s
    now = time.monotonic()
    gone = False
    with _LOCK:
        inst = Models._inst
        if inst is not None and now - inst.used >= idle_s and inst.lock.acquire(blocking=False):
            Models._inst = None
            inst.lock.release()
            gone = True
        for k in [k for k, v in _VECS.items() if now - v[4] >= idle_s]:
            del _VECS[k]
            gone = True
    if gone:
        inst = None
        gc.collect()
        torch = sys.modules.get("torch")
        if torch is not None and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    return gone


def search_needs_model() -> bool:
    """Whether a search could need CLIP: a library has its vectors but no picture-search index (or
    picture search isn't installed), so its searches fall back to them. Otherwise the service has no
    reason to load CLIP until an update has new photos to embed."""
    from . import search

    for name in config.list_projects():
        try:
            conn = db.connect(name, create=False)
        except Exception:
            continue
        try:
            if _has_rows(conn, "embeddings") and not (search.available() and _has_rows(conn, "search_vecs")):
                return True
        finally:
            conn.close()
    return False


def _has_rows(conn, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone())


def warm_up() -> None:
    """At the service's start: load CLIP's text half only when a library's searches would use it."""
    try:
        if search_needs_model():
            Models.get(text_only=True).embed_text("a photo")
    except Exception:
        pass


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


def _vectors(conn) -> tuple | None:
    """(ids, float32 matrix, {photo id: its row}) for present photos' current CLIP vectors, held once
    (the rows are views into the matrix) and kept until they change or go unused for IDLE_S."""
    try:
        db_file = conn.execute("PRAGMA database_list").fetchone()[2]
        stamp = (db.counter(conn, "emb_gen"), *conn.execute(
            "SELECT COUNT(*), (SELECT COUNT(*) FROM shots WHERE status='present') FROM embeddings").fetchone())
    except Exception:
        return None
    with _LOCK:
        hit = _VECS.get(db_file)
        if hit and hit[0] == stamp:
            _VECS[db_file] = (*hit[:4], time.monotonic())
            return hit[1:4]
    rows = conn.execute(
        "SELECT e.shot_id, e.vec FROM embeddings e JOIN shots s ON s.id=e.shot_id"
        " WHERE s.status='present' AND e.model=? AND e.src = s.analyzed_fp", (MODEL_TAG,)
    ).fetchall()
    if rows:
        rows = [r for r in rows if len(r[1]) == len(rows[0][1])]
    if not rows:
        return None
    ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
    mat = np.empty((len(rows), len(rows[0][1]) // 2), dtype=np.float32)
    for k, r in enumerate(rows):
        mat[k] = np.frombuffer(r[1], dtype=np.float16)
    del rows
    by_id = {int(sid): mat[k] for k, sid in enumerate(ids)}
    with _LOCK:
        _VECS.clear()            # one catalog at a time is plenty: keeps memory bounded
        _VECS[db_file] = (stamp, ids, mat, by_id, time.monotonic())
        _watch_idle()
    return ids, mat, by_id


def load_embeddings(conn) -> dict[int, np.ndarray]:
    """CLIP vectors of present photos, cached per catalog until they change (grouping, scoring and
    search used to load them separately on every run)."""
    v = _vectors(conn)
    return v[2] if v else {}


def _matrix(conn) -> tuple[np.ndarray, np.ndarray] | None:
    """All current embeddings as (ids, matrix): the same memory as load_embeddings."""
    v = _vectors(conn)
    return (v[0], v[1]) if v else None


def search(conn, text: str, limit: int = 30) -> list[tuple[int, float]]:
    m = _matrix(conn)
    if m is None:
        return []
    ids, mat = m
    sims = mat @ Models.get(text_only=True).embed_text(text)
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
