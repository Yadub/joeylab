"""Explore: each photo's colours, the arrangements (their axes run the right way), positions that
stay put under filters, named regions, the colours migration and measuring in the background."""

from __future__ import annotations

import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import add_root, jpeg_at, make_jpeg, run_all


def solid(rgb, size=(120, 80)):
    return Image.new("RGB", size, rgb)


def spearman(a, b) -> float:
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


# ---------------------------------------------------------------- colours

def test_known_colours_give_known_hues():
    from joeylab import colours, explore

    red, green, blue = colours.summarise(solid((220, 30, 30))), colours.summarise(solid((40, 170, 60))), colours.summarise(solid((30, 70, 220)))
    assert 20 <= red["hue"] <= 40 and 125 <= green["hue"] <= 150 and 250 <= blue["hue"] <= 275
    assert red["main"] == "#dc1e1e" and red["palette"][0] == "#dc1e1e"
    assert red["chroma"] > 0.15 and blue["chroma"] > 0.15
    grey = colours.summarise(solid((128, 128, 128)))
    assert grey["hue"] is None and grey["chroma"] < 0.001 and 0.55 < grey["light"] < 0.65
    assert colours.summarise(solid((20, 20, 20)))["light"] < colours.summarise(solid((230, 230, 230)))["light"]
    # Mostly grey with a red part: the hue is the red one, and the main colour the grey most of it is.
    im = solid((128, 128, 128), (100, 100))
    im.paste(solid((220, 30, 30), (30, 100)), (0, 0))
    m = colours.summarise(im)
    assert 20 <= m["hue"] <= 40 and m["main"] == "#808080" and "#dc1e1e" in m["palette"]
    # The names the peek uses.
    assert min(explore.HUE_NAMES, key=lambda hn: abs((blue["hue"] - hn[0] + 180) % 360 - 180))[1] == "Blue"


def test_colours_are_measured_from_thumbnails_and_stamped(env):
    from joeylab import colours, db, pipeline

    tmp, lib = env
    for i, rgb in enumerate([(220, 30, 30), (40, 170, 60), (30, 70, 220), (128, 128, 128)]):
        path = lib / f"IMG_{i}.JPG"
        solid(rgb, (600, 400)).save(path, "JPEG", quality=92)
    for p in lib.iterdir():
        old = time.time() - 60
        import os

        os.utime(p, (old, old))
    add_root(lib)
    conn, _ = run_all()
    assert len(colours.pending(conn)) == 4
    rep = pipeline.Reporter()
    assert pipeline.measure_colours(conn, rep) == 4
    assert colours.pending(conn) == [] and colours.measured_count(conn) == 4
    hues = dict(conn.execute("SELECT s.stem, c.hue FROM colours c JOIN shots s ON s.id = c.shot_id"))
    assert 20 <= hues["IMG_0"] <= 40 and hues["IMG_3"] is None
    # A photo analysed again (its file changed) is measured again.
    conn.execute("UPDATE shots SET analyzed_fp = analyzed_fp || 'x' WHERE stem = 'IMG_1'")
    conn.commit()
    assert [r[0] for r in colours.pending(conn)] == [conn.execute("SELECT id FROM shots WHERE stem='IMG_1'").fetchone()[0]]
    # A photo that leaves the catalog takes its colours with it.
    sid = conn.execute("SELECT id FROM shots WHERE stem='IMG_2'").fetchone()[0]
    db.forget_computed(conn, [sid])
    assert not conn.execute("SELECT 1 FROM colours WHERE shot_id=?", (sid,)).fetchone()


def test_backfill_measures_a_library_in_the_background(env):
    from joeylab import colours, db

    tmp, lib = env
    for i in range(6):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    st = colours.backfill("t")
    assert st["running"] and st["total"] == 6
    for _ in range(100):
        if not colours.backfill("t").get("running"):
            break
        time.sleep(0.05)
    conn = db.connect("t")
    assert colours.measured_count(conn) == 6
    assert colours.backfill("t") == {"running": False, "done": 0, "total": 0}
    # Nothing left: not looked for again on every visit, until a photo is analysed again.
    looked = []
    real = colours.pending
    colours.pending = lambda c: looked.append(1) or real(c)
    try:
        assert colours.backfill("t") == {"running": False, "done": 0, "total": 0} and not looked
        conn.execute("UPDATE shots SET analyzed_fp = analyzed_fp || 'x' WHERE id = (SELECT MIN(id) FROM shots)")
        conn.commit()
        assert colours.backfill("t")["total"] == 1 and looked
    finally:
        colours.pending = real
    for _ in range(100):
        if not colours.backfill("t").get("running"):
            break
        time.sleep(0.05)


def test_colours_migration_upgrades_an_older_catalog(env):
    from joeylab import db

    conn = db.connect("old")
    assert db.SCHEMA_VERSION >= 12 and db.MIGRATIONS[11].__name__ == "_m12_colours"
    conn.execute("DROP TABLE colours")
    conn.execute("PRAGMA user_version = 11")
    conn.commit()
    conn.close()
    conn = db.connect("old")
    assert {r[1] for r in conn.execute("PRAGMA table_info(colours)")} >= {"shot_id", "src", "hue", "chroma", "light", "main", "palette"}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    db.MIGRATIONS[11](conn)                          # running it again changes nothing
    conn.rollback()


# ---------------------------------------------------------------- the arrangements

def fake_photos(n: int, seed: int = 0) -> dict:
    """What explore.py arranges, made up: n photos with every measurement."""
    rng = np.random.default_rng(seed)
    days = rng.integers(0, 400, n)
    hours = rng.integers(0, 24, n)
    mins = rng.integers(0, 60, n)
    import datetime as dt

    when = [(dt.date(2023, 1, 1) + dt.timedelta(days=int(d))).isoformat() + f" {h:02d}:{m:02d}:00" for d, h, m in zip(days, hours, mins)]
    hue = rng.uniform(0, 360, n)
    chroma = rng.uniform(0, 0.15, n)
    hue[chroma < 0.012] = np.nan
    return {"ids": np.arange(1, n + 1, dtype=np.int64), "when": when, "time_src": ["exif"] * n,
            "camera": [["Cam A", "Cam B", "Cam C"][i % 3] for i in range(n)], "lens": [["L1", "L2"][i % 2] for i in range(n)],
            "brightness": rng.uniform(5, 250, n), "contrast": rng.uniform(10, 100, n), "clip_high": rng.uniform(0, 0.2, n),
            "clip_low": rng.uniform(0, 0.3, n), "sharp": np.exp(rng.uniform(2, 9, n)), "quality": rng.uniform(10, 95, n),
            "hue": hue, "chroma": chroma, "light": rng.uniform(0.1, 0.9, n), "main": [0x808080] * n, "pick": [0] * n,
            "fp": ["x"] * n}


def distinct(made) -> bool:
    cells = set(zip(made["gx"].tolist(), made["gy"].tolist()))
    return len(cells) == len(made["gx"]) and min(made["gx"]) >= 0 and min(made["gy"]) >= 0 \
        and max(made["gx"]) < made["cols"] and max(made["gy"]) < made["rows"]


def test_light_and_quality_axes_run_with_their_measures():
    from joeylab import explore

    P = fake_photos(800)
    m = explore._light(P)
    assert distinct(m)
    assert spearman(m["gx"], P["brightness"]) > 0.97            # brighter to the right
    assert spearman(m["gy"], -P["contrast"]) > 0.97             # punchier towards the top
    assert m["guides"]["x"]["low"] == "Darker" and m["guides"]["y"]["high"] == "Punchier"
    ticks = [p for p, _ in m["guides"]["x"]["ticks"]]
    assert ticks == sorted(ticks)
    q = explore._quality(P)
    assert distinct(q)
    assert spearman(q["gx"], P["sharp"]) > 0.97 and spearman(q["gy"], -P["quality"]) > 0.97


def test_colour_wheel_hue_around_colourful_outwards():
    from joeylab import explore

    P = fake_photos(900, seed=1)
    m = explore._colour(P)
    assert distinct(m)
    wh = m["guides"]["wheel"]
    dx, dy = m["gx"] + 0.5 - wh["cx"], m["gy"] + 0.5 - wh["cy"]
    r = np.hypot(dx, dy)
    angle = (np.degrees(np.arctan2(dx, -dy)) + 360) % 360           # clockwise from the top
    grey = np.isnan(P["hue"])
    # Black and white in the middle, inside every photo with colour.
    assert r[grey].max() < np.percentile(r[~grey], 5) + 1.5
    # Around the rim in hue order (starting from the top of the wheel), further out when more colourful.
    h = (P["hue"][~grey] - explore.HUE_TOP) % 360
    assert spearman(angle[~grey], h) > 0.95
    assert spearman(r[~grey], P["chroma"][~grey]) > 0.8
    # Lighter black-and-white photos higher up.
    assert spearman(-m["gy"][grey], P["light"][grey]) > 0.95
    assert [n for _, n in wh["names"]][:3] == ["Red", "Orange", "Yellow"]


def test_time_runs_by_date_then_time_of_day():
    from joeylab import explore

    P = fake_photos(600, seed=2)
    P["time_src"][0] = "file"                                       # no date of its own
    m = explore._time(P)
    assert distinct(m)
    rows = explore.TIME_ROWS + explore.TIME_GAP
    line = m["gy"] // rows
    order = np.lexsort((m["gx"], line))
    dates = [P["when"][i][:10] for i in order if i != 0]
    assert dates == sorted(dates)                                   # reading the lines in order: the days in order
    # Within a day, later in the day is further down.
    day = {}
    for i, w in enumerate(P["when"]):
        day.setdefault(w[:10], []).append(i)
    for photos in day.values():
        minutes = [int(P["when"][i][11:13]) * 60 + int(P["when"][i][14:16]) for i in photos]
        by_time = [photos[k] for k in np.argsort(minutes, kind="stable")]
        down = (m["gy"][by_time] % rows - explore.TIME_GAP).tolist()
        assert down == sorted(down)
        # (a quiet day: each photo in the row for its own twenty minutes)
        if len(photos) <= 2:
            assert down == sorted(mm * explore.TIME_ROWS // (24 * 60) for mm in minutes)
    assert m["guides"]["undated"][3] == 1


def test_camera_islands_hold_their_cameras():
    from joeylab import explore

    P = fake_photos(300, seed=3)
    m = explore._camera(P)
    assert distinct(m)
    islands = {name: (x, y, w, h) for x, y, w, h, name, _ in m["guides"]["islands"]}
    assert set(islands) == {"Cam A", "Cam B", "Cam C"}
    for i, cam in enumerate(P["camera"]):
        x, y, w, h = islands[cam]
        assert x <= m["gx"][i] < x + w and y <= m["gy"][i] < y + h


def test_snap_keeps_free_points_and_gives_every_point_a_cell():
    from joeylab import explore

    x = np.array([0.2, 5.5, 5.5, 5.5, 5.5, 9.9])
    y = np.array([0.2, 5.5, 5.5, 5.5, 5.5, 9.9])
    gx, gy = explore.snap(x, y, 10, 10)
    assert (gx[0], gy[0]) == (0, 0) and (gx[5], gy[5]) == (9, 9)       # alone in their cells: kept
    assert len(set(zip(gx.tolist(), gy.tolist()))) == 6
    assert all(abs(gx[i] - 5) <= 1 and abs(gy[i] - 5) <= 1 for i in range(1, 5))   # a pile spreads next door
    with pytest.raises(ValueError):
        explore.snap(np.zeros(5), np.zeros(5), 2, 2)


def _snap_one_by_one(x, y, cols, rows):
    """snap as it was first written: each crowded point searched the whole grid for the nearest free cell."""
    from joeylab import explore

    occ = np.zeros(rows * cols, dtype=bool)
    gx, gy = np.zeros(len(x), dtype=np.int32), np.zeros(len(x), dtype=np.int32)
    for i in np.random.default_rng(7).permutation(len(x)).tolist():
        cx, cy = min(cols - 1, max(0, int(x[i]))), min(rows - 1, max(0, int(y[i])))
        for dx, dy in explore._NEAR:
            X, Y = cx + dx, cy + dy
            if 0 <= X < cols and 0 <= Y < rows and not occ[Y * cols + X]:
                k = Y * cols + X
                break
        else:
            free = np.flatnonzero(~occ)
            k = int(free[np.argmin((free % cols + 0.5 - x[i]) ** 2 + (free // cols + 0.5 - y[i]) ** 2)])
        occ[k] = True
        gx[i], gy[i] = k % cols, k // cols
    return gx, gy


def test_crowds_and_piles_get_the_cells_the_whole_grid_search_gave():
    from joeylab import explore

    rng = np.random.default_rng(5)
    for _ in range(25):
        cols, rows = int(rng.integers(5, 70)), int(rng.integers(5, 70))
        n = int(rng.integers(1, cols * rows + 1))
        pile = rng.random(n) < 0.5                                   # half of them in one place
        x = np.where(pile, 2.5, rng.normal(cols / 2, cols / 8, n))
        y = np.where(pile, 3.25, rng.normal(rows / 2, rows / 8, n))
        got, want = explore.snap(x, y, cols, rows), _snap_one_by_one(x, y, cols, rows)
        assert np.array_equal(got[0], want[0]) and np.array_equal(got[1], want[1])


def test_a_day_of_thousands_fits_the_time_arrangement():
    from joeylab import explore

    P = fake_photos(3000, seed=4)
    P["when"] = [f"2024-05-01 10:{i % 60:02d}:00" for i in range(3000)]      # one wedding morning: 3,000 photos
    m = explore._time(P)
    assert distinct(m)


def test_arrangements_are_kept_in_the_library_folder(web):
    from joeylab import config, explore

    client, conn, ids = web
    first = wait_ready(client, "light")
    kept = config.project_dir("t") / "explore" / "light.json"
    assert kept.exists() and kept.stat().st_mode & 0o077 == 0
    explore._CACHE.clear()                                               # a restart
    made = []
    real = explore._light
    explore._light = lambda P: made.append(1) or real(P)
    try:
        assert client.get("/api/p/t/explore/light").json()["cells"] == first["cells"] and not made
    finally:
        explore._light = real


# ---------------------------------------------------------------- what's in them, named

def test_clusters_are_named_with_the_words_they_are_closest_to():
    from joeylab import explore

    rng = np.random.default_rng(0)
    words = ["Beach", "Dogs", "Mountains", "Photos"]
    W = np.eye(8)[:4].astype(np.float32)
    W[3] = np.ones(8) / np.sqrt(8)                    # "Photos": a little like everything
    centres = np.stack([W[0] + 0.1 * rng.normal(size=8), W[1] + 0.1 * rng.normal(size=8), W[2] + 0.1 * rng.normal(size=8)])
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    assert [n.split(" · ")[0] for n in explore.name_clusters(centres, W, words)] == ["Beach", "Dogs", "Mountains"]
    # A second word only when it says something the first doesn't.
    W2 = np.stack([W[0], W[0] * 0.999 + 0.04 * W[1], W[1], W[2]])
    W2 /= np.linalg.norm(W2, axis=1, keepdims=True)
    assert explore.name_clusters(centres[:1], W2, ["Beach", "Beaches", "Dogs", "Mountains"])[0] == "Beach"


def test_content_layout_names_its_regions_with_a_stand_in_encoder(env, monkeypatch):
    from joeylab import db, explore, search

    tmp, lib = env
    for i in range(48):
        make_jpeg(lib / f"IMG_{i:03d}.JPG", seed=i, size=(240, 160))
    add_root(lib)
    conn, _ = run_all()
    search.ensure_schema(conn)
    # Three kinds of picture: each photo's vector near one of three directions.
    rng = np.random.default_rng(1)
    dim = 768
    axes = np.eye(dim, dtype=np.float32)[:3]
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    kind = {sid: k % 3 for k, sid in enumerate(ids)}
    for sid, fp in conn.execute("SELECT id, analyzed_fp FROM shots"):
        v = np.zeros((search.N_CROPS, dim), dtype=np.float32)
        v[:] = axes[kind[sid]] + 0.05 * rng.normal(size=dim)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        conn.execute("INSERT INTO search_vecs(shot_id, src, vecs) VALUES (?, ?, ?)",
                     (sid, f"{fp}:{search.INDEX_VERSION}", v.astype(np.float16).tobytes()))
    db.bump(conn, "search_gen")
    conn.commit()
    targets = {"Beach": 0, "Dogs": 1, "Mountains": 2}

    def stand_in(model, words):
        out = np.full((len(words), dim), 0.001, dtype=np.float32)
        for k, w in enumerate(words):
            if w in targets:
                out[k, targets[w]] = 1.0
            else:
                out[k, 3 + k % 50] = 1.0
        return out

    monkeypatch.setattr(explore, "encode_words", stand_in)
    out = explore.layout(conn, "t", "content")
    for _ in range(200):
        if out.get("ready") or out.get("error"):
            break
        time.sleep(0.05)
        out = explore.layout(conn, "t", "content")
    assert out["ready"], out
    names = {r["name"].split(" · ")[0] for r in out["guides"]["regions"]}
    assert names >= {"Beach", "Dogs", "Mountains"}
    # Each photo's region is the one its kind is named after.
    regions = out["guides"]["regions"]
    for sid, reg in zip(out["ids"], out["region"]):
        if reg >= 0:
            assert targets[regions[reg]["name"].split(" · ")[0]] == kind[sid]
    # The words' vectors are kept, so the model isn't needed again.
    from joeylab import config

    assert list(config.MODELS_DIR.glob("explore-words-*.npz"))


# ---------------------------------------------------------------- the page's API

@pytest.fixture()
def web(env):
    tmp, lib = env
    for i in range(12):
        jpeg_at(lib / f"IMG_{i:04d}.JPG", seed=i, when=f"2024:05:{1 + i % 4:02d} {8 + i:02d}:00:00")
    add_root(lib)
    conn, _ = run_all()
    from joeylab.web.jobs import JobManager
    from joeylab.web.server import create_app

    client = TestClient(create_app(jobs=JobManager(watch_minutes=0, faces=False, ai_layer=False)))
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    return client, conn, ids


def wait_ready(client, by):
    for _ in range(200):
        d = client.get(f"/api/p/t/explore/{by}").json()
        if d.get("ready") or d.get("missing"):
            return d
        time.sleep(0.05)
    return d


def test_layouts_stay_put_under_filters_and_decisions(web):
    from joeylab import library

    client, conn, ids = web
    first = {by: wait_ready(client, by) for by in ("light", "time", "quality", "camera", "colour")}
    for by, d in first.items():
        assert d["ready"] and sorted(d["ids"]) == sorted(ids), by
        assert len(d["cells"]) == 2 * len(ids) and len(d["main"]) == len(ids)
    library.decide(conn, [{"id": ids[0], "pick": 1}, {"id": ids[1], "pick": -1}])
    shown = client.get("/api/p/t/explore", params={"status": "picks"}).json()
    assert shown["shown"] == [ids[0]] and shown["available"]["light"] is True
    for by in first:
        again = wait_ready(client, by)
        assert again["cells"] == first[by]["cells"], by                  # nothing moved
    light = client.get("/api/p/t/explore/light").json()
    assert light["pick"][light["ids"].index(ids[0])] == 1
    # Without picture vectors there's no "what's in them", and the page is told why.
    d = client.get("/api/p/t/explore/content").json()
    assert not d["ready"] and "picture-search index" in d["missing"]
    assert client.get("/api/p/t/explore/nonsense").status_code == 400
    f = client.get(f"/api/p/t/explore/photo/{ids[0]}").json()
    assert {"brightness", "palette", "focus", "camera"} <= set(f)


def test_atlas_sheet_holds_small_thumbnails_and_changes_with_them(web):
    from joeylab import explore

    client, conn, ids = web
    d = wait_ready(client, "light")
    k = str(ids[0] // explore.SHEET)
    r = client.get(f"/img/t/atlas/{k}", params={"v": d["sheets"][k]})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    from io import BytesIO

    im = Image.open(BytesIO(r.content))
    assert im.size == (explore.ATLAS_SIDE * explore.CELL, explore.ATLAS_SIDE * explore.CELL)
    conn.execute("UPDATE shots SET analyzed_fp = analyzed_fp || 'x' WHERE id = ?", (ids[0],))
    conn.commit()
    assert client.get("/api/p/t/explore/light").json()["sheets"][k] != d["sheets"][k]
