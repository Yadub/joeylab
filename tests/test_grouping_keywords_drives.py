"""Grouping strictness, "worth checking" suggestions, the keyword tree, drives recognised
after a rename, and children's face groups linked across years."""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import add_root, jpeg_at, make_jpeg, run_all


def test_grouping_strictness_levels(env):
    from joeylab import db, grouping

    _, lib = env
    # One scene: a 3-frame burst, a frame 30 s later, another 2 minutes after that.
    for i, t in enumerate(["10:00:00", "10:00:01", "10:00:02", "10:00:32", "10:02:30"]):
        jpeg_at(lib / f"IMG_{i}.JPG", seed=7, when=f"2025:05:01 {t}")
    add_root(lib)
    conn, _ = run_all()
    prev = grouping.preview(conn)
    assert [prev[lv]["stacks"] for lv in (1, 3, 5)] == [3, 2, 1]
    assert prev[1]["largest"] == 3 and prev[5]["largest"] == 5
    assert grouping.run(conn)["level"] == 3
    db.set_setting(conn, "grouping", 5)
    grouping.run(conn)
    assert conn.execute("SELECT COUNT(DISTINCT group_id) FROM shots").fetchone()[0] == 1
    db.set_setting(conn, "grouping", 1)
    grouping.run(conn)
    assert conn.execute("SELECT COUNT(DISTINCT group_id) FROM shots").fetchone()[0] == 3

    from joeylab.web.server import create_app

    c = TestClient(create_app())
    assert c.post("/api/p/t/settings", json={"grouping": 9}).status_code == 400
    d = c.get("/api/p/t/grouping").json()
    assert d["level"] == 1 and set(d["levels"]) == {"1", "2", "3", "4", "5"}


def test_worth_checking_is_a_hint_never_a_decision(env):
    from joeylab import library, scoring
    from joeylab.library import Filter

    _, lib = env
    for i in range(4):
        jpeg_at(lib / f"IMG_{i}.JPG", seed=i, when=f"2025:05:0{i + 1} 10:00:00")    # a day apart: four stacks
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    sharp = dict(sharp_subject=5000, face_sharp=None, main_faces=0, face_area=0, eyes_closed=0,
                 clip_high=0.0, clip_low=0.0, brightness=120)
    setups = [dict(sharp, sharp_subject=100),                                    # a little soft
              dict(sharp, sharp_subject=30),                                     # blurry
              dict(sharp),                                                       # fine
              dict(sharp, main_faces=1, face_area=0.05, face_sharp=100, eyes_closed=1)]   # a blink, no better frame
    for sid, m in zip(ids, setups):
        conn.execute("UPDATE metrics SET " + ", ".join(f"{k}=?" for k in m) + " WHERE shot_id=?", [*m.values(), sid])
    conn.commit()
    res = scoring.run(conn)
    sug = {sid: conn.execute("SELECT suggestion FROM metrics WHERE shot_id=?", (sid,)).fetchone()[0] for sid in ids}
    assert sug[ids[0]] == "check" and sug[ids[1]] == "reject" and sug[ids[3]] == "check"
    assert res["suggest_check"] == 2
    assert {r["id"] for r in library.shots(conn, Filter(suggestion="check"))} == {ids[0], ids[3]}
    assert any("second look" in w["text"] for w in scoring.explain(conn, ids[0]))

    from joeylab.web.server import create_app

    c = TestClient(create_app())
    c.post("/api/p/t/accept", json={})
    decided = {r[0]: r[1] for r in conn.execute("SELECT shot_id, pick FROM decisions")}
    assert ids[0] not in decided and ids[3] not in decided and decided.get(ids[1]) == -1
    assert c.get("/api/p/t/overview").json()["counts"]["suggest_check"] == 2      # still there to look at


def test_keyword_tree_tagging_undo_search_and_export(env):
    import json

    from joeylab import db, export, keywords, library, portable, query, scanner
    from joeylab.library import Filter

    _, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    a, b, c = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    climb = keywords.ensure(conn, "Activities > Climbing")
    keywords.assign(conn, [a, b], add=[climb], what="keywords: Climbing")
    keywords.assign(conn, [c], add=[keywords.ensure(conn, "Activities › Diving")], what="keywords: Diving")
    acts = keywords.find(conn, "Activities")
    tree = {k["path"]: k for k in keywords.tree(conn)}
    assert tree["Activities"]["total"] == 3 and tree["Activities › Climbing"]["total"] == 2
    assert {r["id"] for r in library.shots(conn, Filter(keyword=acts))} == {a, b, c}
    assert {r["id"] for r in library.shots(conn, Filter(keyword=climb))} == {a, b}
    assert query.parse("#Climbing", conn)["filter"]["keyword"] == climb
    assert query.parse("keyword:none", conn)["filter"]["untagged"] == "1"

    # Undo puts the last tagging back, like any decision.
    assert library.undo(conn)["label"] == "keywords: Diving"
    assert keywords.of_shot(conn, c) == []
    assert {r["id"] for r in library.shots(conn, Filter(untagged=True))} == {c}

    # Rename, then merge a new keyword into it: its photos follow.
    keywords.rename(conn, climb, "Rock climbing")
    boulder = keywords.ensure(conn, "Bouldering")
    keywords.assign(conn, [c], add=[boulder])
    keywords.merge(conn, boulder, climb)
    assert {r["id"] for r in library.shots(conn, Filter(keyword=climb))} == {a, b, c}

    # XMP: every level in dc:subject, the path in lr:hierarchicalSubject.
    library.set_decision(conn, a, rating=3)
    export.write_sidecars(conn, Filter(ids=[a]), include=("rating", "keywords"), project="t")
    side = (lib / "IMG_0.xmp").read_text()
    assert "Activities|Rock climbing" in side and "<rdf:li>Rock climbing</rdf:li>" in side and "<rdf:li>Activities</rdf:li>" in side

    # Portable: keywords come back on a fresh catalog.
    data = json.loads(json.dumps(portable.export_decisions(conn, "t")))
    fresh = db.connect("fresh")
    scanner.add_root(fresh, lib)
    scanner.scan(fresh)
    assert portable.import_decisions(fresh, data)["keywords"] == 3
    assert {k["path"] for k in keywords.tree(fresh)} >= {"Activities", "Activities › Rock climbing"}

    from joeylab.web.server import create_app

    cl = TestClient(create_app())
    r = cl.post("/api/p/t/keywords/assign", json={"ids": [b], "add": ["Places > Lisbon"]}).json()
    assert r["n"] == 1
    assert any(k["path"] == "Places › Lisbon" for k in cl.get("/api/p/t/keywords").json()["tree"])
    assert any(k["path"] == "Places › Lisbon" for k in cl.get(f"/api/p/t/shot/{b}").json()["keywords"])


def test_renamed_drive_is_followed_and_a_stranger_is_not_scanned(env, tmp_path, monkeypatch):
    import os
    import shutil

    from joeylab import db, library, scanner, volumes

    vols = tmp_path / "Volumes"
    uuids = {}
    monkeypatch.setattr(volumes, "VOLUMES", vols)
    monkeypatch.setattr(volumes, "_info", lambda mount: {"uuid": uuids.get(os.path.basename(mount)),
                                                        "name": os.path.basename(mount)})
    volumes._cache.clear()
    photos = vols / "Archive SSD" / "Photos"
    for i in range(2):
        make_jpeg(photos / f"IMG_{i}.JPG", seed=i)
    uuids["Archive SSD"] = "U1"
    conn = db.connect("t")
    scanner.add_root(conn, photos)
    conn, _ = run_all()
    assert conn.execute("SELECT volume_uuid, rel_path FROM roots").fetchone()[:] == ("U1", "Photos")
    a = conn.execute("SELECT id FROM shots ORDER BY stem").fetchone()[0]
    library.set_decision(conn, a, rating=5)
    db.set_setting(conn, "folder_names", {str(photos): "Our photos"})

    # Renamed in Finder: same drive, new name.
    os.rename(vols / "Archive SSD", vols / "Archive")
    uuids["Archive"] = "U1"
    volumes._cache.clear()
    rep = scanner.scan(conn)
    assert rep.roots_moved and rep.roots_moved[0]["to"] == str(vols / "Archive" / "Photos") and not rep.missing
    assert conn.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0] == 2
    assert conn.execute("SELECT rating FROM decisions WHERE shot_id=?", (a,)).fetchone()[0] == 5
    assert all(p.startswith(str(vols / "Archive")) for (p,) in conn.execute("SELECT path FROM files"))
    assert db.get_setting(conn, "folder_names") == {str(vols / "Archive" / "Photos"): "Our photos"}

    # Unplugged, and another drive called "Archive" shows up with a Photos folder: held back, nothing missing.
    shutil.move(str(vols / "Archive"), str(tmp_path / "unplugged"))
    make_jpeg(vols / "Archive" / "Photos" / "OTHER.JPG", seed=9)
    uuids["Archive"] = "U2"
    volumes._cache.clear()
    rep = scanner.scan(conn)
    assert rep.roots_wrong_drive == [str(vols / "Archive" / "Photos")] and not rep.added and not rep.missing
    assert conn.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0] == 2


def test_child_across_years_is_offered_and_a_no_is_remembered(env):
    import numpy as np

    from joeylab import faces

    _, lib = env
    plan = {"maya": [f"2022:0{m}:01" for m in range(1, 7)],        # named in 2022
            "young": ["2019:03:01", "2019:09:01", "2020:02:01", "2020:06:01"],   # a younger Maya, in her own group
            "stranger": ["2019:04:01", "2019:05:01", "2019:06:01"],
            "sameyears": ["2022:02:15", "2022:03:15", "2022:04:15"]}
    i = 0
    for who, dates in plan.items():
        for d in dates:
            jpeg_at(lib / f"{who}_{i}.JPG", seed=i, when=f"{d} 12:00:00")
            i += 1
    add_root(lib)
    conn, _ = run_all()
    rng = np.random.default_rng(1)
    unit = lambda v: (v / np.linalg.norm(v)).astype(np.float16).tobytes()
    base, drift = rng.normal(size=512), rng.normal(size=512)
    base, drift = base / np.linalg.norm(base), drift / np.linalg.norm(drift)
    maya = conn.execute("INSERT INTO people(name) VALUES ('Maya')").lastrowid
    vec = {"maya": lambda: unit(base + 0.25 * rng.normal(size=512) / 22),
           "young": lambda: unit(base + 0.8 * drift + 0.25 * rng.normal(size=512) / 22),
           "stranger": lambda: unit(rng.normal(size=512)),
           "sameyears": lambda: unit(base + 0.25 * rng.normal(size=512) / 22)}
    cluster = {"maya": 800, "young": 900, "stranger": 901, "sameyears": 902}
    for sid, stem in conn.execute("SELECT id, stem FROM shots").fetchall():
        who = stem.split("_")[0]
        conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, area, sharp, closed, vec, cluster, person_id, person_src)"
                     " VALUES (?, .4, .3, .1, .15, 90, .02, 120, 0, ?, ?, ?, ?)",
                     (sid, vec[who](), cluster[who], maya if who == "maya" else None, "user" if who == "maya" else None))
    # A sibling who looks just like young Maya, in the same years, and once in a photo with Maya:
    # the time rule alone would let it through (4 of 5 photos are outside Maya's years); being in a
    # photo together is what rules it out.
    shared = [conn.execute("SELECT id FROM shots WHERE stem LIKE 'maya_%' ORDER BY stem").fetchone()[0]]
    shared += [r[0] for r in conn.execute("SELECT id FROM shots WHERE stem LIKE 'young_%'")]
    for sid in shared:
        conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, area, sharp, closed, vec, cluster) VALUES (?, .1, .3, .1, .15, 90, .02, 120, 0, ?, 903)",
                     (sid, vec["young"]()))
    conn.commit()
    got = faces.across_years(conn)
    assert [(x["cluster"], x["direction"]) for x in got] == [(900, "younger")]
    assert got[0]["gap_years"] > 1 and len(got[0]["person_faces"]) == 3

    from joeylab.web.server import create_app

    c = TestClient(create_app())
    assert c.get("/api/p/t/people/across-years").json()["suggestions"][0]["name"] == "Maya"
    c.post(f"/api/p/t/people/{maya}/not-same", json={"cluster": 900})
    assert faces.across_years(conn) == []


def test_export_stars_also_go_on_the_original(env):
    import os
    import time

    from PIL import Image

    from joeylab import portable

    _, lib = env

    def shot(path, seed, software=None, rating=None):
        make_jpeg(path, seed=seed)
        img = Image.open(path)
        ex = img.getexif()
        ex.get_ifd(0x8769)[0x9003] = "2026:01:24 22:52:24"     # DateTimeOriginal
        ex.get_ifd(0x8769)[0xA431] = "5CA20305"                # BodySerialNumber
        ex[0x0110] = "X100VI"
        if software:
            ex[0x0131] = software
        if rating is not None:
            ex[0x4746] = rating                                 # Rating
        img.save(path, "JPEG", quality=90, exif=ex.tobytes())
        old = time.time() - 60
        os.utime(path, (old, old))

    shot(lib / "DSCF5848.JPG", seed=1)
    shot(lib / "Exports" / "2026-Lisbon-12.jpg", seed=2, software="Adobe Photoshop Lightroom Classic 15.1.1 (Macintosh)", rating=3)
    add_root(lib)
    conn, _ = run_all()
    ids = {stem: sid for sid, stem in conn.execute("SELECT id, stem FROM shots")}
    items, to_orig = portable.ratings_from_files(conn)
    got = {it["id"]: it["rating"] for it in items}
    assert got == {ids["2026-Lisbon-12"]: 3, ids["DSCF5848"]: 3} and to_orig == 1
