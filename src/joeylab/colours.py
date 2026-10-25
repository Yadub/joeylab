"""Each photo's colours in a few numbers, measured from its 768 px thumbnail (never the original):
its main hue, how colourful it is, how light it is, and three palette colours. Explore's colour
wheel places photos by them, and a photo too small to show as a picture is drawn as a square of its
main colour.

Colours are compared in OKLab, where equal steps look about equally different, so "how colourful"
and "which hue" match what you see. Measuring is cheap (a few milliseconds a photo): the update
does it for new photos straight after stacking, and a library that predates it is measured in the
background the first time Explore needs it.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import numpy as np
from PIL import Image

from . import db

VERSION = "1"                 # bump when the measurement changes: every photo is measured again
SIDE = 48                     # the thumbnail is read at 1/8 size and reduced to about this many pixels across
GREY_CHROMA = 0.012           # mean chroma below this: black and white, or as good as
BATCH = 200


# ---------------------------------------------------------------- sRGB <-> OKLab

def _linear(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _gamma(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


_M1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                [0.2119034982, 0.6806995451, 0.1073969566],
                [0.0883024619, 0.2817188376, 0.6299787005]])
_M2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                [1.9779984951, -2.4285922050, 0.4505937099],
                [0.0259040371, 0.7827717662, -0.8086757660]])


def oklab(rgb: np.ndarray) -> np.ndarray:
    """(..., 3) sRGB in 0-255 to OKLab (L 0-1; a and b about ±0.3)."""
    lms = _linear(np.asarray(rgb, dtype=np.float64) / 255.0) @ _M1.T
    return np.cbrt(lms) @ _M2.T


def to_rgb(lab: np.ndarray) -> np.ndarray:
    """OKLab to sRGB 0-255 (out-of-gamut colours clipped)."""
    lms = (np.asarray(lab, dtype=np.float64) @ np.linalg.inv(_M2).T) ** 3
    return np.round(_gamma(lms @ np.linalg.inv(_M1).T) * 255).astype(int)


def hex_of(rgb) -> str:
    r, g, b = (int(v) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def hue_colour(hue: float, light: float = 0.72, chroma: float = 0.12) -> str:
    """The colour at this hue (degrees, OKLab's), for drawing the wheel's rim."""
    h = np.radians(hue)
    return hex_of(to_rgb(np.array([light, chroma * np.cos(h), chroma * np.sin(h)])))


# ---------------------------------------------------------------- measuring one photo

def _kmeans(X: np.ndarray, k: int, iters: int = 8) -> tuple[np.ndarray, np.ndarray]:
    """A small, deterministic k-means: (centres, how many pixels each has)."""
    order = np.argsort(X[:, 0])                     # start from evenly spaced lightnesses
    C = X[order[np.linspace(0, len(X) - 1, k).astype(int)]].copy()
    for _ in range(iters):
        lab = ((X[:, None, :] - C[None]) ** 2).sum(2).argmin(1)
        for j in range(k):
            sel = lab == j
            if sel.any():
                C[j] = X[sel].mean(0)
    lab = ((X[:, None, :] - C[None]) ** 2).sum(2).argmin(1)
    return C, np.bincount(lab, minlength=k)


def summarise(img: Image.Image) -> dict:
    """hue (degrees, None for a photo with no colour to speak of), chroma (how colourful: OKLab's
    mean chroma), light (mean lightness 0-1), main (the colour most of the photo is, as #rrggbb)
    and palette (up to three colours, the most common first)."""
    img = img.convert("RGB")
    if max(img.size) > SIDE:
        img = img.copy()
        img.thumbnail((SIDE, SIDE), Image.Resampling.BILINEAR)
    lab = oklab(np.asarray(img).reshape(-1, 3))
    L, a, b = lab[:, 0], lab[:, 1], lab[:, 2]
    C = np.hypot(a, b)
    chroma = float(C.mean())
    hue = None
    if chroma >= GREY_CHROMA / 2 and C.sum() > 0:
        # The hue most of the colour is in: a histogram of every pixel's hue, weighted by how
        # colourful the pixel is (a grey pixel's hue means nothing), smoothed around the circle.
        H = np.degrees(np.arctan2(b, a)) % 360
        hist = np.bincount((H // 10).astype(int) % 36, weights=C, minlength=36)
        hist = hist + 0.5 * (np.roll(hist, 1) + np.roll(hist, -1))
        peak = (int(hist.argmax()) + 0.5) * 10
        near = np.abs((H - peak + 180) % 360 - 180) <= 30
        w = C * near
        if w.sum() > 0:
            hue = float(np.degrees(np.arctan2((np.sin(np.radians(H)) * w).sum(), (np.cos(np.radians(H)) * w).sum())) % 360)
    k = 4
    centres, counts = _kmeans(lab, k)
    order = np.argsort(-counts)
    palette = [hex_of(to_rgb(centres[j])) for j in order[:3] if counts[j]]
    return {"hue": None if hue is None else round(hue, 1), "chroma": round(chroma, 4), "light": round(float(L.mean()), 4),
            "main": palette[0], "palette": palette}


def measure(path: str) -> dict | None:
    """Read a thumbnail at 1/8 size (the JPEG decoder does that almost for free) and measure it."""
    try:
        with Image.open(path) as im:
            im.draft("RGB", (SIDE * 2, SIDE * 2))
            return summarise(im)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- the catalog

def stamp_of(analyzed_fp: str | None) -> str:
    return f"{analyzed_fp}:{VERSION}"


def pending(conn) -> list[tuple[int, str, str]]:
    """(photo id, thumbnail, stamp) for every present photo whose colours aren't measured from its
    current thumbnail."""
    return [(r[0], r[1], stamp_of(r[2])) for r in conn.execute(
        "SELECT s.id, s.thumb, s.analyzed_fp FROM shots s LEFT JOIN colours c ON c.shot_id = s.id"     # (one pass)
        " WHERE +s.status='present' AND s.thumb IS NOT NULL AND s.analyzed_fp IS NOT NULL"
        " AND (c.src IS NULL OR c.src != s.analyzed_fp || ':' || ?)", (VERSION,))]


def measured_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM colours c JOIN shots s ON s.id = c.shot_id WHERE s.status='present'"
                        " AND c.src = s.analyzed_fp || ':' || ?", (VERSION,)).fetchone()[0]


def _save(conn, rows: list[tuple]) -> None:
    conn.executemany(
        "INSERT INTO colours(shot_id, src, hue, chroma, light, main, palette) VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(shot_id) DO UPDATE SET src=excluded.src, hue=excluded.hue, chroma=excluded.chroma,"
        " light=excluded.light, main=excluded.main, palette=excluded.palette", rows)
    conn.commit()


def run(conn, progress: Callable[[int, int], None] | None = None, workers: int | None = None,
        stop: threading.Event | None = None) -> int:
    """Measure every photo still to be measured. Only reads thumbnails and writes the catalog's
    colours table. Returns how many were measured."""
    todo = pending(conn)
    if not todo:
        return 0
    workers = workers or max(1, min(8, (os.cpu_count() or 4) - 2))
    done, rows = 0, []
    # Decoding happens outside the GIL, so a few threads keep several cores busy.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for k in range(0, len(todo), BATCH):
            if stop is not None and stop.is_set():
                break
            chunk = todo[k : k + BATCH]
            for (sid, _, src), m in zip(chunk, pool.map(lambda t: measure(t[1]), chunk)):
                if m is not None:
                    rows.append((sid, src, m["hue"], m["chroma"], m["light"], int(m["main"][1:], 16), ",".join(m["palette"])))
            _save(conn, rows)
            done += len(chunk)
            rows = []
            if progress:
                progress(done, len(todo))
    return done


# ---------------------------------------------------------------- measuring a whole library in the background

_STATE: dict[str, dict] = {}
_LOCK = threading.Lock()
# catalog -> its counts counter (db.COUNTS_GEN) when nothing was left to measure: until a photo comes or
# is analysed again (which moves it), Explore doesn't look through every photo on each request.
_CLEAR: dict[str, tuple] = {}


def backfill(project: str) -> dict:
    """Measure a library's photos in the background, if any are left: {running, done, total}.
    Called again, it reports how far it is. It needs no update lock: it only adds colour rows, which
    an update running at the same time would write just the same."""
    with _LOCK:
        st = _STATE.get(project)
        if st and st["running"]:
            return dict(st)
        conn = db.connect(project, create=False)
        try:
            path = conn.execute("PRAGMA database_list").fetchone()[2]
            gen = (os.stat(path).st_ino, db.counter(conn, db.COUNTS_GEN))      # (this catalog file, as it is now)
            left = 0 if _CLEAR.get(path) == gen else len(pending(conn))
        finally:
            conn.close()
        if not left:
            _CLEAR[path] = gen
            return {"running": False, "done": 0, "total": 0}
        st = _STATE[project] = {"running": True, "done": 0, "total": left, "started": time.time()}

    def work():
        conn = db.connect(project, create=False)
        try:
            run(conn, progress=lambda d, t: st.update(done=d, total=t))
        except Exception as e:                  # shown on the page that asked
            st["error"] = f"{type(e).__name__}: {e}"
        finally:
            conn.close()
            st["running"] = False

    threading.Thread(target=work, name=f"colours-{project}", daemon=True).start()
    return dict(st)
