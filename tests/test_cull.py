"""Culling on the server: stacks that keep their numbers and say how long they span and where each
burst starts, previews made ahead of time, and 1:1 zoom served in tiles."""

from __future__ import annotations

import io
import os
import time

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from conftest import add_root, run_all


def frame(path, seed: int, when: str, sub: int = 0, jitter: int = 0, size=(900, 600)):
    """One frame of a burst: the same scene (seed) with a little noise, at a capture time."""
    base = np.random.default_rng(seed).integers(0, 255, (20, 30, 3), dtype=np.uint8)
    img = np.asarray(Image.fromarray(base).resize(size, Image.Resampling.NEAREST)).astype(np.float32)
    img += np.random.default_rng(seed * 31 + jitter).normal(0, 2, img.shape)
    img = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
    exif = img.getexif()
    exif[0x0110] = "TestCam"
    ifd = exif.get_ifd(0x8769)
    ifd[0x9003] = when
    ifd[0x9291] = f"{sub:02d}"
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=92, exif=exif.tobytes())
    old = time.time() - 60
    os.utime(path, (old, old))


def test_stacks_keep_their_numbers_and_show_bursts_and_span(env):
    from joeylab.web.server import create_app

    _, lib = env
    # One moment covered by two bursts 20 seconds apart (one stack), then a single photo.
    for i in range(3):
        frame(lib / f"IMG_{i}.JPG", seed=4, when="2026:06:13 10:00:00", sub=i * 25, jitter=i)
    for i in range(3, 5):
        frame(lib / f"IMG_{i}.JPG", seed=4, when="2026:06:13 10:00:20", sub=(i - 3) * 25, jitter=i)
    frame(lib / "IMG_5.JPG", seed=9, when="2026:06:13 11:00:00")
    add_root(lib)
    conn, _ = run_all()
    c = TestClient(create_app())
    st = c.get("/api/p/t/stacks", params={"status": "undecided"}).json()["stacks"]
    assert [len(s["ids"]) for s in st] == [5, 1]
    burst, single = st
    assert burst["breaks"] == [3] and 19 < burst["span"] < 22 and burst["und"] == 5
    assert single["breaks"] == [] and single["span"] == 0
    assert [s["no"] for s in st] == [1, 2]
    # Deciding the first stack takes it off the list, but the other keeps its number.
    c.post("/api/p/t/decide", json={"items": [{"id": i, "pick": -1} for i in burst["ids"]]})
    st = c.get("/api/p/t/stacks", params={"status": "undecided"}).json()["stacks"]
    assert [(s["no"], s["und"]) for s in st] == [(2, 1)]
    c.post("/api/p/t/decide", json={"items": [{"id": burst["ids"][0], "pick": 0}]})
    assert c.get("/api/p/t/stacks", params={"status": "undecided"}).json()["stacks"][0]["und"] == 1


def test_previews_are_made_ahead_for_undecided_photos(env):
    from joeylab import config
    from joeylab.web.server import create_app

    _, lib = env
    for i in range(4):
        frame(lib / f"IMG_{i}.JPG", seed=20 + i, when=f"2026:06:13 1{i}:00:00")
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    app = create_app()
    c = TestClient(app)
    c.post("/api/p/t/decide", json={"items": [{"id": ids[0], "pick": 1}]})
    assert app.state.prerender.start("t")
    for _ in range(100):
        if "t" not in app.state.prerender.running:
            break
        time.sleep(0.05)
    made = {int(f.name.split("_")[0]) for f in (config.project_dir("t") / "previews").rglob("*.jpg")}
    assert made == set(ids[1:])                                  # the decided one is left for when it's viewed
    assert not app.state.prerender.start("t", soon=True)         # not again straight away


def test_a_shoot_stacks_tightly_unless_a_level_was_chosen(env, tmp_path):
    from joeylab import db
    from joeylab.web.server import create_app

    _, lib = env
    frame(lib / "IMG_0.JPG", seed=1, when="2026:06:13 10:00:00")
    c = TestClient(create_app())
    assert c.post("/api/projects", json={"name": "wedding", "path": str(lib), "purpose": "shoot"}).status_code == 200
    assert c.get("/api/p/wedding/settings").json()["grouping"] == 2
    add_root(lib)
    run_all()
    assert c.post("/api/p/t/settings", json={"purpose": "shoot"}).json()["grouping"] == 2
    db.set_setting(db.connect("t"), "grouping", 4)                 # chosen: kept
    assert c.post("/api/p/t/settings", json={"purpose": "archive"}).json()["grouping"] == 4
    assert c.post("/api/p/t/settings", json={"purpose": "shoot"}).json()["grouping"] == 4


def test_share_pages_have_both_thumbnail_sizes(env):
    from joeylab.web.server import create_app

    _, lib = env
    frame(lib / "IMG_0.JPG", seed=2, when="2026:06:13 10:00:00", size=(1600, 1000))
    add_root(lib)
    conn, _ = run_all()
    sid = conn.execute("SELECT id FROM shots").fetchone()[0]
    c = TestClient(create_app())
    album = c.post("/api/p/t/albums", json={"name": "Share", "ids": [sid]}).json()["id"]
    tok = c.post("/api/p/t/shares", json={"kind": "album", "ref": album}).json()["token"]
    sizes = {s: Image.open(io.BytesIO(c.get(f"/img/share/{tok}/{s}/{sid}").content)).size for s in ("t", "m")}
    assert max(sizes["t"]) == 400 and max(sizes["m"]) == 768


def test_zoom_tiles_cover_the_full_size_photo(env):
    from joeylab.web.server import TILE, create_app

    _, lib = env
    frame(lib / "IMG_0.JPG", seed=3, when="2026:06:13 10:00:00", size=(1300, 700))
    add_root(lib)
    conn, _ = run_all()
    sid = conn.execute("SELECT id FROM shots").fetchone()[0]
    c = TestClient(create_app())
    info = c.get(f"/img/t/tiles/{sid}").json()
    assert info == {"w": 1300, "h": 700, "tile": TILE}
    assert c.get(f"/img/t/tiles/{sid}").json() == info                     # kept
    first = Image.open(io.BytesIO(c.get(f"/img/t/tile/{sid}/1/0/0").content))
    last = Image.open(io.BytesIO(c.get(f"/img/t/tile/{sid}/1/2/1").content))
    assert first.size == (TILE, TILE) and last.size == (1300 - 2 * TILE, 700 - TILE)
    half = Image.open(io.BytesIO(c.get(f"/img/t/tile/{sid}/2/1/0").content))
    assert half.size == (650 - TILE, 350)
    assert c.get(f"/img/t/tile/{sid}/1/3/0").status_code == 404
    assert c.get(f"/img/t/tile/{sid}/3/0/0").status_code == 404


def test_a_frame_you_tag_covers_its_shot_once_picked(env):
    from joeylab import db
    from joeylab.web.server import create_app

    _, lib = env
    for i in range(3):
        frame(lib / f"IMG_{i}.JPG", seed=10 + i, when=f"2026:06:13 1{i}:00:00")
    add_root(lib)
    conn, _ = run_all()
    a, b, other = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    c = TestClient(create_app())
    c.post("/api/p/t/shotlist", json={"title": "Rings"})
    c.post("/api/p/t/shotlist", json={"title": "First dance"})
    rings, dance = c.get("/api/p/t/shotlist").json()["items"]
    tag = lambda iid, ids, on=True: c.post(f"/api/p/t/shotlist/{iid}/tag", json={"ids": ids, "on": on})

    # Tagged but not picked yet: listed, not covering it.
    assert tag(rings["id"], [a, b, 99999]).json()["tagged"] == [a, b]       # an unknown photo is left out
    rings = c.get("/api/p/t/shotlist").json()["items"][0]
    assert rings["tagged"] == [a, b] and rings["covered"] == [] and not rings["done"]
    c.post("/api/p/t/decide", json={"items": [{"id": b, "pick": 1}]})
    d = c.get("/api/p/t/shotlist").json()
    rings, dance = d["items"]
    assert rings["covered"] == [b] and rings["done"] and not dance["done"]
    assert {r["id"] for r in d["rows"]} >= {a, b}

    # Untagging; and a rejected frame never covers.
    assert tag(rings["id"], [b], on=False).json()["tagged"] == [a]
    c.post("/api/p/t/decide", json={"items": [{"id": a, "pick": -1}]})
    assert not c.get("/api/p/t/shotlist").json()["items"][0]["done"]
    assert tag(12345, [other]).status_code == 404
    assert tag(dance["id"], []).status_code == 400

    # Removing an item takes its tags with it.
    tag(dance["id"], [other])
    c.delete(f"/api/p/t/shotlist/{dance['id']}")
    assert set(db.get_setting(db.connect("t"), "shotlist_tags")) == {str(rings["id"])}


def test_shot_list_tags_follow_the_photo_that_stays(env):
    from joeylab import db, smart

    conn = db.connect("t")
    smart._save_tags(conn, {1: [5, 6]})
    smart.carry_shot_tags(conn, 5, 7)                        # two files became one photo
    assert smart.shot_tags(conn) == {1: [7, 6]}
    smart.carry_shot_tags(conn, 6, 8, keep=True)             # a copy stays while the other goes to the Trash
    assert smart.shot_tags(conn) == {1: [7, 6, 8]}
    smart.carry_shot_tags(conn, 7, 8)                        # there already: not twice
    assert smart.shot_tags(conn) == {1: [6, 8]}


def test_a_balanced_set_says_which_photos_you_have_not_reviewed(env):
    from joeylab.web.server import create_app

    _, lib = env
    for i in range(4):
        frame(lib / f"IMG_{i}.JPG", seed=20 + i, when=f"2026:06:13 1{i}:00:00")
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    conn.execute(f"UPDATE metrics SET suggestion = CASE WHEN shot_id IN ({ids[1]}, {ids[2]}) THEN 'pick' END")
    conn.commit()
    c = TestClient(create_app())
    c.post("/api/p/t/decide", json={"items": [{"id": ids[0], "pick": 1}]})
    d = c.post("/api/p/t/balance", json={"target": 3}).json()
    assert sorted(d["ids"]) == sorted(ids[:3]) and d["from_picks"] == 1
    assert sorted(d["suggested"]) == sorted(ids[1:3])
