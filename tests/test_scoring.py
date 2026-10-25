"""Scoring: exposure judged on average brightness, exposure outliers in a burst, and "Why" explaining
where a frame stands in its stack."""

from __future__ import annotations

import json
import os
import time

import numpy as np
from PIL import Image, ImageFilter

from conftest import add_root, run_all


def frame(path, seed: int, when: str, sub: int = 0, expo: float = 1.0, blur: float = 0.0, jitter: int = 0):
    """One frame of a burst: the same scene (seed) with a little noise, at a capture time (with
    sub-seconds), darkened or blurred if asked."""
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 255, (20, 30, 3), dtype=np.uint8)
    img = np.asarray(Image.fromarray(base).resize((900, 600), Image.Resampling.NEAREST)).astype(np.float32)
    img += np.random.default_rng(seed * 31 + jitter).normal(0, 2, img.shape)
    img = Image.fromarray(np.clip(img * expo, 0, 255).astype(np.uint8))
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    exif = img.getexif()
    exif[0x0110] = "TestCam"
    ifd = exif.get_ifd(0x8769)
    ifd[0x9003] = when
    ifd[0x9291] = f"{sub:02d}"
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=92, exif=exif.tobytes())
    old = time.time() - 60
    os.utime(path, (old, old))


def metrics_of(conn):
    return {r["stem"]: {**dict(r), "flags": json.loads(r["flags"] or "[]")} for r in conn.execute(
        "SELECT s.id, s.stem, s.group_id, m.quality, m.flags, m.suggestion, m.group_rank, m.brightness, m.clip_low"
        " FROM shots s JOIN metrics m ON m.shot_id = s.id")}


def test_a_dark_frame_is_flagged_ranked_below_its_burst_and_never_strong_on_exposure(env):
    from joeylab import scoring

    _, lib = env
    for i in range(5):
        frame(lib / f"IMG_{i}.JPG", seed=7, when="2026:06:13 10:00:00", sub=i * 20, jitter=i,
              expo=0.22 if i == 2 else 1.0)
    add_root(lib)
    conn, _ = run_all()
    m = metrics_of(conn)
    dark = m["IMG_2"]
    assert len({x["group_id"] for x in m.values()}) == 1                     # one burst
    assert dark["brightness"] < 45 and dark["clip_low"] < 0.15                # dark, but its shadows aren't crushed
    assert "underexposed" in dark["flags"] and "darker-than-burst" in dark["flags"]
    assert "softer-than-twin" not in dark["flags"]                             # dark isn't soft
    assert dark["group_rank"] == 5 and dark["suggestion"] == "reject"
    assert all("darker-than-burst" not in x["flags"] for k, x in m.items() if k != "IMG_2")
    why = scoring.explain(conn, dark["id"])
    text = " ".join(w["text"] for w in why)
    assert "5th of 5" in text and "Very dark" in text and "stops darker" in text
    assert "Strongest: exposure" not in text


def test_a_dark_single_photo_is_worth_checking_not_a_pick(env):
    _, lib = env
    frame(lib / "IMG_0.JPG", seed=3, when="2026:06:13 10:00:00", expo=0.25)
    for i in range(1, 5):
        frame(lib / f"IMG_{i}.JPG", seed=10 + i, when=f"2026:06:13 1{i}:00:00")
    add_root(lib)
    conn, _ = run_all()
    dark = metrics_of(conn)["IMG_0"]
    assert "underexposed" in dark["flags"] and dark["suggestion"] == "check"


def test_why_explains_rank_in_the_stack_and_breaks_ties(env):
    from joeylab import scoring

    _, lib = env
    frame(lib / "IMG_0.JPG", seed=5, when="2026:06:13 10:00:00", sub=0, jitter=0)
    frame(lib / "IMG_1.JPG", seed=5, when="2026:06:13 10:00:00", sub=30, jitter=1, blur=1.2)
    frame(lib / "IMG_2.JPG", seed=5, when="2026:06:13 10:00:00", sub=60, jitter=2)
    add_root(lib)
    conn, _ = run_all()
    m = metrics_of(conn)
    soft = m["IMG_1"]
    assert soft["group_rank"] == 3
    first = next(x for x in m.values() if x["group_rank"] == 1)
    second = next(x for x in m.values() if x["group_rank"] == 2)
    line = scoring.explain(conn, soft["id"])[0]["text"]
    assert line.startswith("3rd of 3: ") and f"behind {first['stem']}" in line and "mostly on focus" in line
    # The two sharp frames score alike: the tie is broken, and said so.
    conn.execute("UPDATE metrics SET quality = ? WHERE shot_id = ?", (first["quality"], second["id"]))
    best = scoring.explain(conn, first["id"])[0]
    assert best["kind"] == "good" and best["text"].startswith("Best of 3: ")
    tie = scoring.explain(conn, second["id"])[0]["text"]
    assert tie.startswith("2nd of 3: level with") and f"{first['stem']} ranks first as the" in tie
    assert scoring.ordinal(1) == "1st" and scoring.ordinal(12) == "12th" and scoring.ordinal(23) == "23rd"
