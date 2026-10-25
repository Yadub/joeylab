"""A demo library, for trying JoeyLab, taking screenshots and testing: generated, photo-like pictures
(no real photos, nobody real) with what a real library has. Five outings over five months from three
camera bodies, bursts (one frame soft, one too bright), exact duplicates in a backup folder, a
re-saved copy, two undated scans, and GPS on three of the outings.

The same seed always gives the same pictures.
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

PROJECT = "demo"
DEFAULT_DEST = "~/JoeyLab Demo"
MARKER = ".joeylab-demo"         # marks a folder this module made, so it can be used again

CAMERAS = {
    "fuji": {"make": "FUJIFILM", "model": "X-T5", "serial": "5AB12345", "lens": "XF16-55mmF2.8 R LM WR",
             "name": "DSCF{:04d}.JPG", "first": 101, "size": (1800, 1200), "f": 2.8},
    "sony": {"make": "SONY", "model": "ILCE-7M4", "serial": "4012345", "lens": "FE 24-70mm F2.8 GM II",
             "name": "DSC{:05d}.JPG", "first": 2001, "size": (1800, 1200), "f": 4.0},
    "phone": {"make": "Apple", "model": "iPhone 15", "serial": None, "lens": "iPhone 15 back camera 6.1mm f/1.6",
              "name": "IMG_{:04d}.JPG", "first": 4301, "size": (1200, 1600), "f": 1.6},
}
# folder, camera, scene, first shot, photos, bursts (at which photo, frames), GPS, time zone
OUTINGS = [
    ("2025-06-14 Beach day", "fuji", "beach", datetime(2025, 6, 14, 10, 5), 8, {2: 6}, (38.6979, -9.4215), "+01:00"),
    ("2025-07-20 Mountain walk", "sony", "mountains", datetime(2025, 7, 20, 8, 40), 7, {3: 4}, (45.9237, 6.8694), "+02:00"),
    ("2025-08-09 City evening", "phone", "city", datetime(2025, 8, 9, 20, 15), 7, {}, (41.1496, -8.6109), "+01:00"),
    ("DCIM/100MSDCF", "sony", "garden", datetime(2025, 9, 3, 16, 30), 6, {1: 3}, None, "+01:00"),
    ("2025-10-12 Autumn park", "fuji", "park", datetime(2025, 10, 12, 14, 0), 6, {4: 3}, None, "+01:00"),
]


# ---------------------------------------------------------------- pictures

def _gradient(w: int, h: int, stops: list) -> np.ndarray:
    """A vertical gradient through (position 0-1, (r, g, b)) stops."""
    y = np.linspace(0, 1, h)
    cols = np.array([c for _, c in stops], dtype=float)
    line = np.stack([np.interp(y, [p for p, _ in stops], cols[:, k]) for k in range(3)], axis=1)
    return np.repeat(line[:, None, :], w, axis=1)


def _img(a: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def _ridge(rng, w: int, base: float, rough: float, n: int = 9) -> list[tuple[float, float]]:
    xs = np.linspace(-0.05 * w, 1.05 * w, n)
    ys = base + rng.uniform(-rough, rough, n)
    return list(zip(xs, ys))


def _soften(mask: Image.Image, radius: float) -> Image.Image:
    """A wide blur, done small and scaled back up (much faster, and it looks the same)."""
    small = mask.resize((max(1, mask.width // 4), max(1, mask.height // 4)), Image.Resampling.BILINEAR)
    return small.filter(ImageFilter.GaussianBlur(radius / 4)).resize(mask.size, Image.Resampling.BILINEAR)


def _clouds(img: Image.Image, rng, top: float, bottom: float, n: int) -> Image.Image:
    w, h = img.size
    mask = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(mask)
    for _ in range(n):
        x, y, r = rng.uniform(0, w), rng.uniform(top, bottom) * h, rng.uniform(0.04, 0.09) * w
        for _ in range(4):
            dx, dy = rng.uniform(-1.4, 1.4) * r, rng.uniform(-0.3, 0.3) * r
            d.ellipse((x + dx - r, y + dy - r * 0.5, x + dx + r, y + dy + r * 0.5), fill=int(rng.uniform(150, 230)))
    img.paste((250, 250, 252), mask=_soften(mask, w * 0.012))
    return img


def _tint(c, k: float) -> tuple:
    return tuple(int(max(0, min(255, v * k))) for v in c)


def _beach(rng, w, h, shift):
    hz = h * rng.uniform(0.4, 0.58)
    sand = h * rng.uniform(0.70, 0.76)
    img = _img(_gradient(w, h, [(0, (96, 160, 222)), (hz / h, (196, 224, 242)), (hz / h + 1e-3, (38, 104, 156)),
                                (sand / h, (78, 164, 184)), (sand / h + 1e-3, (232, 212, 172)), (1, (206, 180, 136))]))
    d = ImageDraw.Draw(_clouds(img, rng, 0.05, hz / h - 0.12, int(rng.integers(0, 5))))
    sx, sy, sr = w * rng.uniform(0.6, 0.85), h * rng.uniform(0.1, 0.22), w * 0.045
    glow = Image.new("L", img.size, 0)
    ImageDraw.Draw(glow).ellipse((sx - 3 * sr, sy - 3 * sr, sx + 3 * sr, sy + 3 * sr), fill=110)
    img.paste((255, 246, 214), mask=_soften(glow, sr))
    d.ellipse((sx - sr, sy - sr, sx + sr, sy + sr), fill=(255, 248, 222))
    for i in range(7):                                         # waves
        y = hz + (sand - hz) * (i + 1) / 8
        d.line([(x, y + 3 * np.sin(x / 40 + i)) for x in range(0, w, 20)], fill=(150, 205, 215), width=2)
    ux = w * rng.uniform(0.1, 0.45)                            # an umbrella
    d.line((ux, sand - h * 0.02, ux, h * 0.95), fill=(90, 70, 50), width=6)
    d.pieslice((ux - w * 0.09, sand - h * 0.12, ux + w * 0.09, sand + h * 0.04), 180, 360,
               fill=tuple(int(v) for v in rng.choice([(214, 64, 52), (240, 180, 40), (40, 120, 190)])))
    bx, by, br = w * (0.55 + 0.05 * shift), h * 0.86 - 40 * abs(np.sin(shift)), w * 0.025   # a ball in the air
    d.ellipse((bx - br * 1.4, h * 0.9, bx + br * 1.4, h * 0.92), fill=(190, 165, 125))
    d.ellipse((bx - br, by - br, bx + br, by + br), fill=(226, 60, 70), outline=(250, 250, 250), width=4)
    return img


def _mountains(rng, w, h, shift):
    img = _img(_gradient(w, h, [(0, (62, 104, 168)), (0.45, (170, 170, 196)), (0.62, (244, 186, 140)), (1, (60, 84, 60))]))
    d = ImageDraw.Draw(_clouds(img, rng, 0.05, 0.3, int(rng.integers(1, 6))))
    layers = [(0.42, 0.10, (122, 128, 160)), (0.52, 0.08, (88, 100, 128)), (0.64, 0.06, (52, 72, 70))]
    for k, (base, rough, col) in enumerate(layers):
        pts = _ridge(rng, w, h * base, h * rough, 7 + 2 * k)
        d.polygon(pts + [(w * 1.05, h), (-w * 0.05, h)], fill=col)
        if k == 0:                                              # snow on the far peaks
            for x, y in pts:
                if y < h * (base - rough * 0.4):
                    d.polygon([(x, y), (x - w * 0.04, y + h * 0.05), (x + w * 0.04, y + h * 0.05)], fill=(240, 242, 248))
    d.rectangle((0, h * 0.8, w, h), fill=(64, 96, 54))
    for _ in range(14):                                          # pines
        x, y, s = rng.uniform(0, w), h * rng.uniform(0.72, 0.95), h * rng.uniform(0.05, 0.12)
        d.polygon([(x, y - s), (x - s * 0.3, y), (x + s * 0.3, y)], fill=(30, 58, 40))
    wx = w * (0.3 + 0.03 * shift)                                 # a walker
    d.ellipse((wx - 9, h * 0.74 - 52, wx + 9, h * 0.74 - 34), fill=(60, 40, 40))
    d.line((wx, h * 0.74 - 34, wx, h * 0.74), fill=(200, 60, 40), width=12)
    return img


def _city(rng, w, h, shift):
    img = _img(_gradient(w, h, [(0, (34, 28, 76)), (0.55, (150, 80, 120)), (0.8, (238, 132, 86)), (1, (30, 30, 40))]))
    d = ImageDraw.Draw(img)
    mx, my = w * rng.uniform(0.15, 0.85), h * rng.uniform(0.08, 0.2)
    d.ellipse((mx - 30, my - 30, mx + 30, my + 30), fill=(246, 240, 214))
    x = -20.0
    while x < w:
        bw, bh = w * rng.uniform(0.08, 0.2), h * rng.uniform(0.25, 0.6)
        top = h - bh
        d.rectangle((x, top, x + bw, h), fill=_tint((32, 30, 48), rng.uniform(0.8, 1.3)))
        for wy in np.arange(top + 18, h - 30, 34):
            for wx in np.arange(x + 12, x + bw - 18, 28):
                if rng.random() < 0.45:
                    d.rectangle((wx, wy, wx + 12, wy + 16), fill=(250, 214, 124))
        x += bw + w * rng.uniform(0.0, 0.03)
    return img


def _garden(rng, w, h, shift):
    img = _img(_gradient(w, h, [(0, (120, 160, 96)), (1, (46, 86, 44))]))
    bokeh = Image.new("RGB", img.size, (0, 0, 0))
    b = ImageDraw.Draw(bokeh)
    for _ in range(26):
        x, y, r = rng.uniform(0, w), rng.uniform(0, h), rng.uniform(20, 80)
        b.ellipse((x - r, y - r, x + r, y + r), fill=tuple(int(v) for v in rng.choice(
            [(200, 230, 150), (250, 240, 200), (170, 210, 120), (240, 190, 210)])))
    img = Image.blend(img, _soften(bokeh, 18), 0.35)
    d = ImageDraw.Draw(img)
    cx, cy = w * (0.5 + 0.01 * shift), h * rng.uniform(0.4, 0.5)
    d.line((cx, cy, cx + 20, h), fill=(40, 110, 40), width=14)
    petal = tuple(int(v) for v in rng.choice([(236, 110, 160), (250, 200, 60), (240, 240, 245), (190, 110, 220)]))
    R = min(w, h) * 0.16
    for a in np.linspace(0, 2 * np.pi, 9)[:-1]:
        px, py = cx + np.cos(a) * R * 0.6, cy + np.sin(a) * R * 0.6
        d.ellipse((px - R * 0.45, py - R * 0.45, px + R * 0.45, py + R * 0.45), fill=petal)
    d.ellipse((cx - R * 0.35, cy - R * 0.35, cx + R * 0.35, cy + R * 0.35), fill=(240, 186, 40))
    return img


def _park(rng, w, h, shift):
    img = _img(_gradient(w, h, [(0, (176, 196, 214)), (0.55, (226, 220, 206)), (0.56, (132, 140, 70)), (1, (92, 104, 50))]))
    d = ImageDraw.Draw(_clouds(img, rng, 0.04, 0.25, int(rng.integers(0, 4))))
    d.polygon([(w * 0.45, h * 0.56), (w * 0.55, h * 0.56), (w * 0.85, h), (w * 0.15, h)], fill=(196, 172, 132))
    for _ in range(7):
        x, top = rng.uniform(0, w), h * rng.uniform(0.15, 0.3)
        d.rectangle((x - 14, top + h * 0.2, x + 14, h * rng.uniform(0.6, 0.75)), fill=(84, 58, 40))
        for _ in range(9):
            cx, cy, r = x + rng.uniform(-120, 120), top + rng.uniform(0, h * 0.25), rng.uniform(50, 110)
            d.ellipse((cx - r, cy - r * 0.8, cx + r, cy + r * 0.8), fill=tuple(int(v) for v in rng.choice(
                [(214, 96, 40), (232, 160, 50), (180, 54, 40), (200, 130, 60)])))
    for _ in range(160):                                          # fallen leaves
        x, y = rng.uniform(0, w), rng.uniform(h * 0.6, h)
        d.ellipse((x, y, x + 9, y + 6), fill=(206, 110, 44))
    kx = w * (0.48 + 0.02 * shift)                                 # a dog on the path
    d.ellipse((kx - 40, h * 0.8 - 22, kx + 40, h * 0.8 + 22), fill=(70, 50, 40))
    d.ellipse((kx + 30, h * 0.8 - 44, kx + 64, h * 0.8 - 10), fill=(70, 50, 40))
    return img


def _scan(rng, w, h, shift):
    img = _img(_gradient(w, h, [(0, (240, 236, 226)), (1, (226, 220, 206))]))
    d = ImageDraw.Draw(img)
    d.rectangle((w * 0.08, h * 0.07, w * 0.6, h * 0.11), fill=(70, 70, 80))
    y = h * 0.16
    while y < h * 0.9:
        d.rectangle((w * 0.08, y, w * rng.uniform(0.6, 0.92), y + 10), fill=(120, 118, 124))
        y += 30 if rng.random() > 0.15 else 60
    return img


def _old_print(rng, w, h, shift):
    grey = np.asarray(_mountains(rng, w, h, shift).convert("L"), dtype=float)[..., None]
    sepia = grey * np.array([1.07, 0.92, 0.72]) + np.array([18, 10, 0])
    img = _img(sepia)
    ImageDraw.Draw(img).rectangle((0, 0, w - 1, h - 1), outline=(246, 242, 232), width=int(w * 0.04))
    return img


SCENES = {"beach": _beach, "mountains": _mountains, "city": _city, "garden": _garden, "park": _park,
          "scan": _scan, "old_print": _old_print}


def picture(scene: str, variant: int, size: tuple[int, int], shift: float = 0.0, soft: float = 0.0,
            bright: float = 1.0, seed: int = 0) -> Image.Image:
    """One photo-like picture. The same variant with another shift is the next frame of a burst."""
    rng = np.random.default_rng((seed, variant))
    w, h = size
    img = SCENES[scene](rng, w, h, shift).filter(ImageFilter.GaussianBlur(0.7 + soft))
    a = np.asarray(img, dtype=np.float32) * bright
    a += np.random.default_rng((seed, variant, int(shift * 10))).standard_normal((h, w, 1), dtype=np.float32) * 2.5  # grain
    return _img(a)


# ---------------------------------------------------------------- metadata

def _dms(v: float) -> tuple[float, float, float]:
    d = int(v)
    m = int((v - d) * 60)
    return float(d), float(m), round((v - d - m / 60) * 3600, 2)


def exif_bytes(cam: dict, when: datetime, subsec: int, tz: str, gps: tuple | None, rng) -> bytes:
    ex = Image.Exif()
    ex[0x010F], ex[0x0110], ex[0x0112] = cam["make"], cam["model"], 1
    ifd = ex.get_ifd(0x8769)
    stamp = when.strftime("%Y:%m:%d %H:%M:%S")
    ifd[0x9003] = ifd[0x9004] = stamp                     # DateTimeOriginal, CreateDate
    ifd[0x9291] = f"{subsec:02d}"                         # SubSecTimeOriginal
    ifd[0x9011] = tz                                      # OffsetTimeOriginal
    ifd[0x829A] = float(rng.choice([1 / 1000, 1 / 500, 1 / 250, 1 / 125, 1 / 60]))
    ifd[0x829D] = cam["f"]
    ifd[0x8827] = int(rng.choice([100, 200, 400, 800, 1600]))
    ifd[0x920A] = float(rng.choice([16, 23, 35, 50, 55]) if cam["make"] != "Apple" else 6.1)
    ifd[0xA434] = cam["lens"]
    if cam["serial"]:
        ifd[0xA431] = cam["serial"]
    if gps:
        lat, lon = gps[0] + rng.uniform(-0.004, 0.004), gps[1] + rng.uniform(-0.004, 0.004)
        g = ex.get_ifd(0x8825)
        g[1], g[2] = ("N" if lat >= 0 else "S"), _dms(abs(lat))
        g[3], g[4] = ("E" if lon >= 0 else "W"), _dms(abs(lon))
    return ex.tobytes()


def _save(img: Image.Image, path: Path, when: datetime, exif: bytes | None = None, quality: int = 90) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=quality, **({"exif": exif} if exif else {}))
    ts = time.mktime(when.timetuple())                   # file times as a camera card leaves them
    os.utime(path, (ts, ts))
    return path


# ---------------------------------------------------------------- the library

def make(dest: Path, seed: int = 0, progress: Callable[[int, int], None] | None = None, scale: float = 1.0) -> dict:
    """Write the demo photos into dest (a new or empty folder). Returns counts. scale < 1 makes
    smaller pictures, for quick tests."""

    def sized(size):
        return int(size[0] * scale), int(size[1] * scale)

    dest.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    counters = {k: c["first"] for k, c in CAMERAS.items()}
    total = sum(n + sum(f - 1 for f in bursts.values()) for _, _, _, _, n, bursts, _, _ in OUTINGS) + 6
    made: list[Path] = []
    by_folder: dict[str, list[Path]] = {}

    def tick():
        if progress:
            progress(len(made), total)

    for o, (folder, cam_key, scene, when, n, bursts, gps, tz) in enumerate(OUTINGS):
        cam = CAMERAS[cam_key]
        for i in range(n):
            variant = o * 100 + i
            frames = bursts.get(i, 1)
            for f in range(frames):
                soft = 3.5 if frames > 2 and f == frames - 1 else 0.0          # the last frame of a burst missed focus
                bright = 1.55 if frames > 3 and f == 1 else 1.0                # and one came out too bright
                path = dest / folder / cam["name"].format(counters[cam_key])
                counters[cam_key] += 1
                at = when + timedelta(milliseconds=300 * f)
                img = picture(scene, variant, sized(cam["size"]), shift=float(f), soft=soft, bright=bright, seed=seed)
                made.append(_save(img, path, at, exif_bytes(cam, at, at.microsecond // 10000, tz, gps, rng)))
                by_folder.setdefault(folder, []).append(path)
                tick()
            when += timedelta(minutes=int(rng.integers(3, 25)), seconds=int(rng.integers(0, 60)))

    # Exact duplicates: a backup of part of the beach day, byte for byte.
    beach = by_folder[OUTINGS[0][0]]
    backup = dest / "Backup" / OUTINGS[0][0]
    backup.mkdir(parents=True, exist_ok=True)
    dupes = [Path(shutil.copy2(p, backup / p.name)) for p in beach[:3]]
    made += dupes
    # A re-saved copy: the same picture saved again, smaller, under the same name (as a chat app does).
    src = by_folder[OUTINGS[1][0]][0]
    with Image.open(src) as im:
        resaved = _save(im.convert("RGB"), dest / "Sent to family" / src.name, datetime(2025, 7, 22, 9, 0),
                        exif=im.info.get("exif"), quality=72)
    made.append(resaved)
    # Undated: a scanned page and an old print, with no camera metadata at all.
    for name, scene, size in (("Scan 001.jpg", "scan", (1240, 1754)), ("Old print.jpg", "old_print", (1500, 1000))):
        made.append(_save(picture(scene, 900 + len(made), sized(size), seed=seed), dest / "Scans" / name,
                          datetime(2025, 11, 2, 18, 0)))
    tick()
    (dest / MARKER).write_text("Made by joeylab demo. Generated pictures only; delete this folder whenever you like.\n",
                               encoding="utf-8")
    return {"photos": len(made), "exact_duplicates": len(dupes), "resaved": 1, "undated": 2,
            "outings": len(OUTINGS), "cameras": len(CAMERAS)}


def check_dest(dest: Path) -> str | None:
    """Why dest can't hold the demo, or None. Never inside JoeyLab's home or a folder any library
    tracks, and never a folder that already has other things in it."""
    import sqlite3

    from . import config, export

    dest = Path(os.path.realpath(dest))
    if export.within(dest, config.HOME):
        return f"{dest} is inside JoeyLab's own folder ({config.HOME}). Choose another place."
    if export.within(config.HOME, dest):
        return f"{dest} holds JoeyLab's own folder ({config.HOME}). Choose another place."
    for name in config.list_projects():
        try:                                     # read-only: just the folders it tracks
            conn = sqlite3.connect(f"file:{config.project_dir(name) / 'catalog.db'}?mode=ro", uri=True)
            roots = [r[0] for r in conn.execute("SELECT path FROM roots")]
            conn.close()
        except sqlite3.Error:
            continue
        for root in roots:
            if export.within(dest, root) and not (name == PROJECT and (dest / MARKER).exists()):
                return f"{dest} is inside {root}, which the library {name!r} tracks. Choose a folder outside your photos."
    if dest.exists() and not dest.is_dir():
        return f"{dest} is a file. Choose a folder."
    if dest.is_dir() and any(dest.iterdir()) and not (dest / MARKER).exists():
        return f"{dest} already has things in it. Choose a new or empty folder."
    return None
