"""Find anything in your photos by describing it, with no tagging.

Each photo is indexed three ways, all locally:
  * SigLIP 2 embeddings of the whole frame plus 9 overlapping tiles, so small objects
    (a die on a table, a dog in the corner) are seen up close, and results can show where;
  * Apple Vision labels (1,300+ objects and scenes: dog, door, receipt, cake...);
  * Apple Vision text recognition (signs, cake writing, documents), in every language it reads.
A search combines "looks like" (SigLIP's calibrated match probability) with label and text hits.

Everything is computed from the local 768 px thumbnails, so originals (and their drives)
are not read again.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Callable

import numpy as np

from . import config, db

ARCH, PRETRAINED = "ViT-B-16-SigLIP2-384", "webli"
INDEX_VERSION = "1"
MODEL_TAG = f"{ARCH}/{PRETRAINED}/full+3x3"
GRID = 3                      # 3x3 tiles, each half the frame's width and height, 25% stride
TILE = 0.5
TEMPLATES = ("{}", "a photo of {}")
# SigLIP's probabilities are low in absolute terms for real photos (a clear dog photo scores
# ~5-17% for "a dog"; non-dogs score ~0.03%), so levels are set from measured separation.
STRONG_PROB, POSSIBLE_PROB = 0.03, 0.005
STRONG_LABEL, POSSIBLE_LABEL = 0.5, 0.15
STRONG_OF_TOP = 0.4           # "strong" must also be within reach of the best match for this query
VERY_SURE = 0.06              # SigLIP at least this sure overrides a disagreeing Apple label
BOX_MARGIN = 0.015            # a tile must beat the whole frame by this (cosine) to draw a box
LABEL_MIN_CONF = 0.15

SCHEMA = """
CREATE TABLE IF NOT EXISTS search_vecs (
    shot_id INTEGER PRIMARY KEY,
    src     TEXT NOT NULL,       -- analyzed_fp:INDEX_VERSION it was computed from
    vecs    BLOB NOT NULL        -- (1 + GRID*GRID) x 768 float16, L2-normalised; row 0 = whole frame
);
CREATE TABLE IF NOT EXISTS vision (
    shot_id INTEGER PRIMARY KEY,
    src     TEXT NOT NULL,
    labels  TEXT,                -- JSON [[label, confidence], ...]
    text    TEXT,                -- recognised text
    utility INTEGER,             -- screenshot / document / receipt-like
    score   REAL                 -- Apple's aesthetic score (-1..1)
);
CREATE INDEX IF NOT EXISTS search_vecs_shot ON search_vecs(shot_id);  -- counted from these, not the rows
CREATE INDEX IF NOT EXISTS vision_shot ON vision(shot_id);
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(labels, text, tokenize = 'porter unicode61');
"""


def crop_boxes() -> list[tuple[float, float, float, float]]:
    """(x, y, w, h) fractions for every indexed crop; index 0 is the whole frame."""
    step = (1 - TILE) / (GRID - 1)
    return [(0.0, 0.0, 1.0, 1.0)] + [(gx * step, gy * step, TILE, TILE) for gy in range(GRID) for gx in range(GRID)]


N_CROPS = 1 + GRID * GRID


def warm_up(delay: float = 15.0) -> None:
    """Load the picture-search model in the background once the service is up, so the first search
    from a phone isn't the one that waits ~15 s for it. The search store is tidied first."""
    try:
        tidy_store()
    except OSError:
        pass
    time.sleep(delay)
    try:
        Model.get().embed_text("a photo")
    except Exception:
        pass


def available() -> bool:
    """Whether the AI extra is installed, without importing it (as ai.available)."""
    from importlib.util import find_spec

    return all(find_spec(m) is not None for m in ("open_clip", "torch", "transformers"))


def vision_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Vision  # noqa: F401
    except ImportError:
        return False
    return True


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------- model

_LOCK = threading.RLock()        # loading the model; each loaded model has its own lock for using it


class Model:
    _inst: "Model | None" = None
    _cpu_only = False                # the graphics processor stopped answering once: the CPU from then on

    def __init__(self):
        import open_clip
        import torch

        self.torch = torch
        self.lock = threading.RLock()  # searches and indexing take turns
        self.device = "mps" if torch.backends.mps.is_available() and not Model._cpu_only else "cpu"
        model, _, self.preprocess = config.offline_first(lambda: open_clip.create_model_and_transforms(ARCH, pretrained=PRETRAINED))
        self.dtype = torch.float16 if self.device == "mps" else torch.float32
        self.model = model.to(self.device, dtype=self.dtype).eval()
        self.tokenizer = config.offline_first(lambda: open_clip.get_tokenizer(ARCH))
        self.scale = float(model.logit_scale.exp().detach())
        self.bias = float(model.logit_bias.detach())

    @classmethod
    def get(cls) -> "Model":
        with _LOCK:
            if cls._inst is None:
                cls._inst = Model()
            return cls._inst

    @classmethod
    def loaded(cls) -> bool:
        return cls._inst is not None

    @classmethod
    def drop_stuck(cls) -> None:
        """The model hung on the graphics processor: leave it (and the thread stuck in it) behind.
        The next search or update loads a fresh one on the CPU."""
        with _LOCK:
            cls._inst = None
            cls._cpu_only = True

    def embed_images(self, images) -> np.ndarray:
        torch = self.torch
        with self.lock, torch.no_grad():
            x = torch.stack([self.preprocess(im) for im in images]).to(self.device, dtype=self.dtype)
            e = self.model.encode_image(x).float()
            e = e / e.norm(dim=-1, keepdim=True)
        return e.cpu().numpy()

    def embed_text(self, text: str) -> np.ndarray:
        torch = self.torch
        with self.lock, torch.no_grad():
            t = self.model.encode_text(self.tokenizer(query_variants(text)).to(self.device)).float()
            t = t / t.norm(dim=-1, keepdim=True)
            v = t.mean(0)
            v = v / v.norm()
        return v.cpu().numpy()

    def probability(self, cos: np.ndarray) -> np.ndarray:
        """SigLIP's calibrated chance that the text describes the image (or tile)."""
        return 1 / (1 + np.exp(-(self.scale * cos + self.bias)))


def query_variants(text: str) -> list[str]:
    """Short queries match better phrased as a caption: "dogs" also tries "a photo of a dog"."""
    text = text.strip()
    out = [tpl.format(text) for tpl in TEMPLATES]
    words = text.lower().split()
    if 1 <= len(words) <= 3 and words[0] not in {"a", "an", "the", "some"}:
        singular = " ".join(words[:-1] + [_stem(words[-1])])
        art = "an" if singular[0] in "aeiou" else "a"
        out.append(f"a photo of {art} {singular}")
    return out


def crops_of(img) -> list:
    W, H = img.size
    out = []
    for x, y, w, h in crop_boxes():
        out.append(img if w == 1 else img.crop((int(x * W), int(y * H), int((x + w) * W), int((y + h) * H))))
    return out


# ---------------------------------------------------------------- Apple Vision

_OCR_LANGUAGES: list[str] | None = None


def _every_language(ocr) -> None:
    """Read text in every language Apple Vision can (English first), telling which one a photo's text is
    in, rather than English only. New text only: photos read before keep theirs (INDEX_VERSION is as it was)."""
    global _OCR_LANGUAGES
    if _OCR_LANGUAGES is None:
        try:
            got = ocr.supportedRecognitionLanguagesAndReturnError_(None)
            langs = [str(x) for x in (got[0] if isinstance(got, tuple) else got) or []]
        except Exception:
            langs = []
        _OCR_LANGUAGES = sorted(langs, key=lambda x: not x.lower().startswith("en"))
    if _OCR_LANGUAGES:
        ocr.setRecognitionLanguages_(_OCR_LANGUAGES)
    if hasattr(ocr, "setAutomaticallyDetectsLanguage_"):           # macOS 13 and later
        ocr.setAutomaticallyDetectsLanguage_(True)


def vision_analyze(path: str) -> dict:
    import Vision
    from Foundation import NSURL

    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(path), None)
    cls = Vision.VNClassifyImageRequest.alloc().init()
    ocr = Vision.VNRecognizeTextRequest.alloc().init()
    ocr.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    _every_language(ocr)
    reqs = [cls, ocr]
    aes = None
    if hasattr(Vision, "VNCalculateImageAestheticsScoresRequest"):   # macOS 15+
        aes = Vision.VNCalculateImageAestheticsScoresRequest.alloc().init()
        reqs.append(aes)
    res = handler.performRequests_error_(reqs, None)
    ok, err = res if isinstance(res, tuple) else (res, None)
    # A classification that ran returns a score for every one of its ~1,300 labels. Nothing back
    # means Vision didn't run (it can fail under load in a background service): raise, so the photo
    # stays queued instead of being stored as "nothing in it".
    if not ok or not (cls.results() or []):
        raise RuntimeError(f"Apple Vision didn't run on this photo ({err or 'no results'}); it will be tried again")
    labels = sorted(((str(o.identifier()), round(float(o.confidence()), 3)) for o in (cls.results() or [])
                     if o.confidence() >= LABEL_MIN_CONF), key=lambda x: -x[1])[:25]
    text = " ".join(str(o.topCandidates_(1)[0].string()) for o in (ocr.results() or []) if o.topCandidates_(1))
    a = (aes.results() or [None])[0] if aes is not None else None
    return {"labels": labels, "text": text, "utility": int(bool(a.isUtility())) if a else None,
            "score": float(a.overallScore()) if a else None}


# ---------------------------------------------------------------- indexing

def pending(conn) -> list:
    ensure_schema(conn)
    return conn.execute(
        "SELECT s.id, s.thumb, s.analyzed_fp, sv.src AS vsrc, vi.src AS isrc FROM shots s"
        " LEFT JOIN search_vecs sv ON sv.shot_id = s.id LEFT JOIN vision vi ON vi.shot_id = s.id"
        " WHERE s.status='present' AND s.thumb IS NOT NULL AND s.analyzed_fp IS NOT NULL"
        " AND (sv.src IS NULL OR sv.src != s.analyzed_fp || ':' || ? OR"
        "      (? AND (vi.src IS NULL OR vi.src != s.analyzed_fp || ':' || ?)))",
        (INDEX_VERSION, int(vision_available()), INDEX_VERSION),
    ).fetchall()


def _label_words(labels: list) -> str:
    return " ".join(lbl.replace("_", " ") for lbl, _ in labels)


INDEXING_KEY = "search_indexing"   # meta: when the indexer last finished a batch (cleared when it's done)
INDEXING_STALE_S = 120.0           # no batch for this long: the indexer has stopped (or crashed)


def _indexing(conn, on: bool) -> None:
    if on:
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (INDEXING_KEY, repr(time.time())))
    else:
        conn.execute("DELETE FROM meta WHERE key=?", (INDEXING_KEY,))
    conn.commit()


def _is_indexing(conn) -> bool:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (INDEXING_KEY,)).fetchone()
    try:
        return bool(r) and time.time() - float(r[0]) < INDEXING_STALE_S
    except ValueError:
        return False


class GpuStuck(RuntimeError):
    """The search model stopped answering on the graphics processor (what was indexed is kept)."""


VISION_STUCK = "Apple Vision didn't answer"


def run(conn, progress: Callable[[int, int], None] | None = None, batch: int = 3,
        errors: list | None = None) -> int:
    """Index pending photos. Anything that couldn't be indexed is appended to `errors` as
    (shot_id, reason), and one retry pass picks up stragglers, so nothing is skipped silently.

    Nothing can hang the update: a photo Apple Vision doesn't answer for in VISION_TIMEOUT_S
    leaves Vision out for the rest of this update (those photos stay queued for labels and text),
    and a batch the model doesn't finish in GPU_TIMEOUT_S stops indexing with GpuStuck."""
    errors = errors if errors is not None else []
    state = {"vision": vision_available()}
    try:
        done = _run_once(conn, progress, batch, errors, state)
        if pending(conn):                 # e.g. a transient read failure: try those once more
            done += _run_once(conn, None, batch, errors, state)
    finally:
        _indexing(conn, False)            # the next search builds a store with everything
    return done


def _run_once(conn, progress, batch: int, errors: list, state: dict) -> int:
    # Small batches: searches share the model lock, so they never wait long behind indexing.
    from PIL import Image

    from . import deadline

    def stamp_of(r):
        return f"{r['analyzed_fp']}:{INDEX_VERSION}"

    rows = [r for r in pending(conn) if r["vsrc"] != stamp_of(r) or (state["vision"] and r["isrc"] != stamp_of(r))]
    if not rows:
        return 0
    model = Model.get()
    done = 0
    with deadline.Pool(2, "vision") as pool:      # Vision fails under heavier load in a background service
        for i in range(0, len(rows), batch):
            chunk = rows[i : i + batch]
            stamp = {r["id"]: stamp_of(r) for r in chunk}
            vis = {r["id"]: pool.submit(vision_analyze, r["thumb"]) for r in chunk
                   if state["vision"] and r["isrc"] != stamp[r["id"]]}
            todo = [r for r in chunk if r["vsrc"] != stamp[r["id"]]]
            crops, owners = [], []
            for r in todo:
                try:
                    with Image.open(r["thumb"]) as im:
                        img = im.convert("RGB")
                except Exception as e:
                    errors.append((r["id"], f"thumbnail unreadable: {type(e).__name__}: {e}"))
                    continue
                crops += crops_of(img)
                owners.append(r["id"])
            if crops:
                try:
                    vecs = deadline.call(model.embed_images, crops, timeout=config.GPU_TIMEOUT_S,
                                         what="The search model").astype(np.float16)
                except deadline.TimedOut as e:
                    Model.drop_stuck()
                    raise GpuStuck(str(e)) from None
                for k, sid in enumerate(owners):
                    conn.execute(
                        "INSERT INTO search_vecs(shot_id, src, vecs) VALUES (?,?,?) ON CONFLICT(shot_id)"
                        " DO UPDATE SET src=excluded.src, vecs=excluded.vecs",
                        (sid, stamp[sid], vecs[k * N_CROPS : (k + 1) * N_CROPS].tobytes()))
            for sid, fut in vis.items():
                try:
                    # Once Vision has hung, the rest of this batch only counts if it's already back.
                    v = fut.result(timeout=config.VISION_TIMEOUT_S if state["vision"] else 0)
                except FutureTimeout:
                    state["vision"] = False
                    errors.append((sid, f"{VISION_STUCK} in {config.VISION_TIMEOUT_S:.0f} s; its labels and text"
                                        " are left for the next update"))
                    continue
                except Exception as e:
                    errors.append((sid, f"Apple Vision failed: {type(e).__name__}: {e}"))
                    continue
                conn.execute(
                    "INSERT INTO vision(shot_id, src, labels, text, utility, score) VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(shot_id) DO UPDATE SET src=excluded.src, labels=excluded.labels,"
                    " text=excluded.text, utility=excluded.utility, score=excluded.score",
                    (sid, stamp[sid], json.dumps(v["labels"]), v["text"], v["utility"], v["score"]))
                conn.execute("DELETE FROM search_fts WHERE rowid=?", (sid,))
                conn.execute("INSERT INTO search_fts(rowid, labels, text) VALUES (?,?,?)",
                             (sid, _label_words(v["labels"]), v["text"]))
            db.bump(conn, "search_gen")      # tells in-memory indexes to reload
            _indexing(conn, True)            # (commits)
            done += len(chunk)
            if progress:
                progress(done, len(rows))
    return done


# ---------------------------------------------------------------- searching

_MATRIX: dict[str, tuple] = {}
_MATRIX_LOCK = threading.Lock()
_VALID = ("SELECT {cols} FROM search_vecs sv JOIN shots s ON s.id = sv.shot_id"
          " WHERE s.status='present' AND sv.src = s.analyzed_fp || ':' || ?")


def _store_dir():
    return config.HOME / "cache" / "search"


def _matrix(conn):
    """All crop vectors as one float16 matrix (rows = photos x crops), kept in a memory-mapped file
    in ~/.joeylab/cache/search until the index changes: filled straight from the catalog a row at
    a time, so it never needs two copies in memory, and reused as is after a restart."""
    ensure_schema(conn)
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    stamp = (db.counter(conn, "search_gen"),) + tuple(conn.execute(
        "SELECT COUNT(*), (SELECT COUNT(*) FROM shots WHERE status='present') FROM search_vecs sv").fetchone())
    hit = _MATRIX.get(db_file)
    if hit and _still_good(conn, hit, stamp):
        return hit[1], hit[2]
    with _MATRIX_LOCK:                        # one load at a time; a second caller gets the first one's
        hit = _MATRIX.get(db_file)
        if hit and _still_good(conn, hit, stamp):
            return hit[1], hit[2]
        m = _open_store(conn, db_file, stamp)
        if m is None:
            _MATRIX.pop(db_file, None)
            return None
        _MATRIX[db_file] = (stamp, *m, time.time())
        return m


REBUILD_WHILE_INDEXING_S = 300.0


def _still_good(conn, hit: tuple, stamp: tuple) -> bool:
    """Can the store in memory answer this search? Yes when nothing changed. While an update is
    indexing photos, also when all it lacks is what was indexed since it was built, for up to
    REBUILD_WHILE_INDEXING_S: rebuilding means reading the whole index again, and indexing changes
    it every few photos. A photo leaving the library (or its index) always rebuilds it."""
    built, at = hit[0], hit[3]
    if built == stamp:
        return True
    return (stamp[2] == built[2] and stamp[1] >= built[1] and time.time() - at < REBUILD_WHILE_INDEXING_S
            and _is_indexing(conn))


def _prefix(db_file: str) -> str:
    """The start of a catalog's store file names."""
    return hashlib.sha1(db_file.encode()).hexdigest()[:12]


_TIDIED = False


def tidy_store(stale_s: float = 3600.0) -> int:
    """Clear ~/.joeylab/cache/search of what isn't needed: temporary files a build left when it
    stopped part way, and the stores of libraries that no longer exist. A recent temporary file may
    be a build under way in another process, so it stays. Everything here can be rebuilt from the
    catalogs. Returns how many files went."""
    folder = _store_dir()
    if not folder.is_dir():
        return 0
    live = set()
    for name in config.list_projects():
        cat = config.project_dir(name) / "catalog.db"
        live |= {_prefix(str(cat)), _prefix(os.path.realpath(cat))}   # SQLite names it with links resolved
    gone, now = 0, time.time()
    for f in folder.iterdir():
        try:
            if f.name.startswith("."):                       # a build's temporary file
                stray = now - f.stat().st_mtime > stale_s
            else:
                stray = f.name.split("-", 1)[0] not in live
            if stray:
                f.unlink()
                gone += 1
        except OSError:
            continue
    return gone


def _open_store(conn, db_file: str, stamp: tuple):
    folder = _store_dir()
    prefix = _prefix(db_file)
    key = hashlib.sha1(repr((stamp, INDEX_VERSION, N_CROPS)).encode()).hexdigest()[:12]
    ids_path, mat_path = folder / f"{prefix}-{key}.ids.npy", folder / f"{prefix}-{key}.f16"
    try:
        if ids_path.exists() and mat_path.exists():
            ids = np.load(ids_path)
            if len(ids):
                mat = np.memmap(mat_path, dtype=np.float16, mode="r")
                return ids, mat.reshape(len(ids) * N_CROPS, -1)
    except (OSError, ValueError):
        pass
    n = conn.execute(_VALID.format(cols="COUNT(*)"), (INDEX_VERSION,)).fetchone()[0]
    if not n:
        return None
    global _TIDIED
    if not _TIDIED:                           # once per process, also when the service didn't warm up search
        _TIDIED = True
        tidy_store()
    folder.mkdir(parents=True, exist_ok=True)
    cur = conn.execute(_VALID.format(cols="sv.shot_id, sv.vecs"), (INDEX_VERSION,))
    first = cur.fetchone()
    if first is None:
        return None
    dim = len(first[1]) // 2 // N_CROPS
    tmp = folder / f".{uuid.uuid4().hex}.tmp"
    ids = np.empty(n, dtype=np.int64)
    mm = np.memmap(tmp, dtype=np.float16, mode="w+", shape=(n * N_CROPS, dim))
    k = 0
    for sid, blob in itertools.chain([first], cur):
        if k >= n:                            # added since the count: next time
            break
        v = np.frombuffer(blob, dtype=np.float16)
        if v.size != N_CROPS * dim:
            continue
        mm[k * N_CROPS : (k + 1) * N_CROPS] = v.reshape(N_CROPS, dim)
        ids[k] = sid
        k += 1
    mm.flush()
    del mm
    ids = ids[:k]
    if k < n:                                 # rows went away while reading: trim the file to fit
        with open(tmp, "r+b") as f:
            f.truncate(k * N_CROPS * dim * 2)
    tmp_ids = folder / f".{uuid.uuid4().hex}.npy"
    np.save(tmp_ids, ids)
    os.replace(tmp_ids, ids_path)
    os.replace(tmp, mat_path)
    for old in folder.glob(f"{prefix}-*"):    # earlier versions of this catalog's store
        if old.name not in (ids_path.name, mat_path.name):
            old.unlink(missing_ok=True)
    return ids, np.memmap(mat_path, dtype=np.float16, mode="r").reshape(k * N_CROPS, dim)


_POOL = None


def _pool():
    global _POOL
    with _LOCK:
        if _POOL is None:
            from concurrent.futures import ThreadPoolExecutor

            _POOL = ThreadPoolExecutor(min(8, os.cpu_count() or 1), thread_name_prefix="search-score")
        return _POOL


def _dot(mat16: np.ndarray, v: np.ndarray, chunk: int = 32768, rows: np.ndarray | None = None) -> np.ndarray:
    """mat16 @ v (only these rows of it, with `rows`) in float32 a chunk at a time, so the index can stay
    float16 in memory. The chunks go to several threads at once (each row's product is the same as
    one at a time): a search of 75,000 photos took half a second on one."""
    v = v.astype(np.float32)
    n = len(mat16) if rows is None else len(rows)

    def part(i: int) -> np.ndarray:
        block = mat16[i : i + chunk] if rows is None else mat16[rows[i : i + chunk]]
        return block.astype(np.float32) @ v

    if n <= chunk:
        return part(0) if n else np.empty(0, dtype=np.float32)
    return np.concatenate(list(_pool().map(part, range(0, n, chunk))))


def indexed_count(conn) -> int:
    ensure_schema(conn)
    return conn.execute("SELECT COUNT(*) FROM search_vecs").fetchone()[0]


_WORD = re.compile(r"[a-z0-9]+")
_VOCAB: dict[str, tuple] = {}


def _label_vocab(conn) -> set[str]:
    """Stemmed words of every Apple Vision label seen in this library (cached)."""
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    n = conn.execute("SELECT COUNT(*) FROM vision").fetchone()[0]
    hit = _VOCAB.get(db_file)
    if hit and hit[0] == n:
        return hit[1]
    words: set[str] = set()
    for (labels,) in conn.execute("SELECT labels FROM vision"):
        for lbl, _ in json.loads(labels or "[]"):
            words |= {_stem(w) for w in _WORD.findall(lbl.lower())}
    _VOCAB[db_file] = (n, words)
    return words


_VISION_IDS: dict[str, tuple] = {}


def _vision_ids(conn) -> set[int]:
    """Photos Apple Vision gave labels to (cached until the index changes: reading every photo's labels
    took half a second on a big library)."""
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    stamp = (db.counter(conn, "search_gen"), conn.execute("SELECT COUNT(*) FROM vision").fetchone()[0])
    hit = _VISION_IDS.get(db_file)
    if hit and hit[0] == stamp:
        return hit[1]
    ids = {r[0] for r in conn.execute("SELECT shot_id FROM vision WHERE labels IS NOT NULL AND labels != '[]'")}
    _VISION_IDS[db_file] = (stamp, ids)
    return ids


def _stem(w: str) -> str:
    for suf in ("ies", "es", "s"):
        if w.endswith(suf) and len(w) > len(suf) + 2:
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


_LABEL_STEMS: dict[str, frozenset] = {}


def _label_stems(label: str) -> frozenset:
    """A Vision label's stemmed words (worked out once per label: there are about 1,300)."""
    out = _LABEL_STEMS.get(label)
    if out is None:
        if len(_LABEL_STEMS) > 20000:
            _LABEL_STEMS.clear()
        out = _LABEL_STEMS[label] = frozenset(_stem(x) for x in _WORD.findall(label.lower()))
    return out


def _keyword_hits(conn, query: str, among: set[int] | None = None) -> dict[int, dict]:
    """Photos whose Vision labels or recognised text mention the query's words (only those `among`
    these ids, when given: a word like "people" names most photos' labels)."""
    words = [w for w in _WORD.findall(query.lower()) if len(w) > 2 and w not in {"the", "and", "with", "photo", "picture"}]
    if not words:
        return {}
    fts_q = " OR ".join(f'"{w}"' for w in words)
    hits: dict[int, dict] = {}
    stems = {_stem(w) for w in words}
    try:
        rows = conn.execute(
            "SELECT f.rowid, v.labels, v.text FROM search_fts f JOIN vision v ON v.shot_id = f.rowid"
            " WHERE search_fts MATCH ?", (fts_q,)).fetchall()
    except Exception:
        return {}
    phrase = query.lower().strip()
    for sid, labels, text in rows:
        if among is not None and sid not in among:
            continue
        best, which = 0.0, None
        for lbl, conf in json.loads(labels or "[]"):
            if conf > best and _label_stems(lbl) & stems:
                best, which = conf, lbl
        text_hit = bool(text) and phrase in text.lower()
        if best or text_hit:
            hits[sid] = {"label": which, "label_conf": best, "text": text_hit}
    return hits


_CONNECTORS = re.compile(r"\s+(?:on|with|and|in|at|near|by|holding|wearing|under|beside|next to|plus|&)\s+", re.I)


def concepts(query: str) -> list[str]:
    """ "dog on a beach" -> ["dog", "a beach"]: the parts a result should all show."""
    parts = [p.strip(" ,.") for p in _CONNECTORS.split(query.strip()) if p.strip(" ,.")]
    parts = [p for p in parts if p.lower() not in {"a", "an", "the"}]
    return parts if 2 <= len(parts) <= 4 else [query]


def _concept_ok(conn, model, mat, n: int, text: str, rows: np.ndarray | None = None) -> np.ndarray:
    """Which photos show this concept at least possibly (picture match or Apple label/text)."""
    s = _dot(mat, model.embed_text(text), rows=rows).reshape(n, N_CROPS).max(axis=1)
    ok = model.probability(s) >= POSSIBLE_PROB
    return ok


def _rows_of(ids: np.ndarray, hits) -> np.ndarray:
    """Where these photo ids are in `ids` (photos not in it are left out)."""
    return np.nonzero(np.isin(ids, np.fromiter(hits, dtype=ids.dtype, count=len(hits))))[0]


def search(conn, query: str, limit: int = 120, restrict: set[int] | None = None) -> list[dict]:
    """Ranked results: {id, score, prob, box, label, text, all_parts}. With several parts ("dog on
    a beach"), photos showing every part rank first and only they can be "strong" matches.
    `restrict` limits the search to these photo ids (a filter): only their vectors are scored, and a
    "strong" match is judged against the best match among them."""
    m = _matrix(conn)
    if m is None:
        return []
    ids, mat = m
    rows = None
    if restrict is not None:
        restrict = restrict if isinstance(restrict, (set, frozenset)) else set(restrict)
        among = _rows_of(ids, restrict)
        if not len(among):
            return []
        ids = ids[among]
        rows = (among[:, None] * N_CROPS + np.arange(N_CROPS)).ravel()
    model = Model.get()
    sims = _dot(mat, model.embed_text(query), rows=rows).reshape(len(ids), N_CROPS)
    best_crop = sims.argmax(axis=1)
    best = sims.max(axis=1)
    prob = model.probability(best)
    # A tile only counts as "the place" when it clearly beats the whole frame.
    tile_wins = (best_crop > 0) & (best - sims[:, 0] > BOX_MARGIN) & (prob >= POSSIBLE_PROB)
    strong_bar = max(STRONG_PROB, STRONG_OF_TOP * float(prob.max()))
    # If Apple Vision knows the query's word (it has a "cat" label) and classified a photo
    # without it, a middling SigLIP score there is probably a look-alike (a fluffy dog for "cats").
    vocab = _label_vocab(conn)
    qstems = {_stem(w) for w in _WORD.findall(query.lower())}
    checkable = bool(qstems & vocab)
    has_vision = _vision_ids(conn) if checkable else set()
    kw = _keyword_hits(conn, query, restrict)
    score = prob.copy()
    for k in _rows_of(ids, kw) if kw else ():               # (the few photos a label or text names)
        h = kw[int(ids[k])]
        score[k] += 0.5 * h["label_conf"] + (0.3 if h["text"] else 0)
    parts = concepts(query)
    all_parts = np.ones(len(ids), dtype=bool)
    if len(parts) > 1:
        for part in parts:
            ok = _concept_ok(conn, model, mat, len(ids), part, rows)
            hits = _keyword_hits(conn, part, restrict)
            if hits:
                ok[_rows_of(ids, hits)] = True
            all_parts &= ok
        score = score + all_parts.astype(np.float32)        # every part shown: ahead of partial matches
    boxes = crop_boxes()
    order = [k for k in np.argsort(-score)[:limit] if np.isfinite(score[k])]
    out = []
    for k in order:
        sid = int(ids[k])
        h = kw.get(sid, {})
        lc = h.get("label_conf") or 0
        level = ("strong" if prob[k] >= strong_bar or lc >= STRONG_LABEL else
                 "possible" if prob[k] >= POSSIBLE_PROB or lc >= POSSIBLE_LABEL or h.get("text") else "unlikely")
        if level == "strong" and checkable and not lc and sid in has_vision and prob[k] < VERY_SURE:
            level = "possible"
        if len(parts) > 1 and not all_parts[k]:
            level = "possible" if level == "strong" else level
        out.append({"id": sid, "score": round(float(score[k]), 4), "prob": round(float(prob[k]), 4), "level": level,
                    "box": list(boxes[int(best_crop[k])]) if tile_wins[k] else None,
                    "label": h.get("label"), "label_conf": h.get("label_conf"), "text": h.get("text", False),
                    "all_parts": bool(all_parts[k])})
    return out


def similar(conn, shot_id: int, limit: int = 60) -> list[tuple[int, float]]:
    m = _matrix(conn)
    if m is None:
        return []
    ids, mat = m
    where = np.nonzero(ids == shot_id)[0]
    if not len(where):
        return []
    full = mat[::N_CROPS]
    sims = _dot(full, full[where[0]])
    order = [i for i in np.argsort(-sims) if ids[i] != shot_id][:limit]
    return [(int(ids[i]), float(sims[i])) for i in order]
