"""Shared fixtures: an isolated JOEYLAB_HOME and synthetic photos.

The tests never download anything and never look at your own JoeyLab home or model caches:
before any JoeyLab module is imported, every home and cache points into a throwaway folder."""

from __future__ import annotations

import atexit
import importlib
import json
import os
import shutil
import tempfile
import time

_SCRATCH = tempfile.mkdtemp(prefix="joeylab-tests-")
atexit.register(shutil.rmtree, _SCRATCH, True)
os.makedirs(os.path.join(_SCRATCH, "user"))
os.environ.update({
    "HOME": os.path.join(_SCRATCH, "user"),                    # "~" too: nothing reads or lists your own folders
    "JOEYLAB_HOME": os.path.join(_SCRATCH, "home"),
    "JOEYLAB_OFFLINE": "1",                                  # no models, no town list
    "HF_HOME": os.path.join(_SCRATCH, "hf"), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
    "TORCH_HOME": os.path.join(_SCRATCH, "torch"), "MPLCONFIGDIR": os.path.join(_SCRATCH, "mpl"),
})

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PIL import Image, ImageFilter  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("JOEYLAB_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JOEYLAB_OFFLINE", "1")          # never download models or town lists in tests
    import joeylab.config as config

    importlib.reload(config)
    import joeylab.analysis, joeylab.db, joeylab.faces, joeylab.scanner  # noqa: E401

    # Reloading drops in-process MediaPipe models without closing them, and MediaPipe then
    # aborts at interpreter exit; close them first.
    joeylab.analysis._close_inproc()
    joeylab.faces._close_inproc()
    for m in (joeylab.db, joeylab.scanner, joeylab.analysis):
        importlib.reload(m)
    lib = tmp_path / "lib"
    lib.mkdir()
    return tmp_path, lib


def make_jpeg(path, seed: int, blur: float = 0.0, size=(900, 600)):
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 255, (size[1] // 30, size[0] // 30, 3), dtype=np.uint8)
    img = Image.fromarray(base).resize(size, Image.Resampling.NEAREST)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=90)
    old = time.time() - 60
    os.utime(path, (old, old))  # past the "still copying" window
    return path


def jpeg_at(path, seed, when: str | None = None, size=(900, 600)):
    """A synthetic photo with a capture time in EXIF (YYYY:MM:DD HH:MM:SS)."""
    make_jpeg(path, seed=seed, size=size)
    if when:
        img = Image.open(path)
        exif = img.getexif()
        exif.get_ifd(0x8769)[0x9003] = when          # DateTimeOriginal
        exif[0x0110] = "TestCam"                      # Model
        img.save(path, "JPEG", quality=90, exif=exif.tobytes())
        old = time.time() - 60
        os.utime(path, (old, old))
    return path


def run_all(project="t", faces=False):
    from joeylab import analysis, db, dupes, grouping, scanner, scoring, selections

    conn = db.connect(project)
    rep = scanner.scan(conn)
    analysis.run(conn, project, use_faces=faces, workers=2)
    dupes.run(conn)
    grouping.run(conn)
    selections.update(conn)
    scoring.run(conn)
    return conn, rep


def add_root(lib, project="t"):
    from joeylab import db, scanner

    scanner.add_root(db.connect(project), lib)


WEDDINGS = ("Weddings/Smith 2024/Ceremony", "Weddings/Smith 2024/Party", "Weddings/Our wedding")
EVERYDAY = ("Everyday/2024 Spring", "Everyday/2025")


def synthetic_library(lib, project="t", per=4):
    """Wedding shoots and everyday photos in the folders of one library, with something of every kind
    of work in it: faces with named people (one with other names, one in both kinds of folder, an
    unnamed group), decisions with their history (one step undone), keywords, two albums, RAW choices,
    a date given by hand, favourites, comments, backups, a shot-list item, merged events and rules
    kept by folder. Returns where things are: {"wed": shot ids, "day": shot ids, "people": {name: id},
    "undated": shot id, ...}."""
    from joeylab import colours, dating, db, keywords, library, smart

    seed = 0
    for k, folder in enumerate(WEDDINGS + EVERYDAY):
        for i in range(per):
            seed += 1
            jpeg_at(lib / folder / f"IMG_{seed:04d}.JPG", seed=seed, when=f"20{24 + k // 2}:0{1 + k}:1{i} 1{i}:00:00")
    make_jpeg(lib / WEDDINGS[2] / "SCAN_0001.JPG", seed=900)          # no date of its own
    add_root(lib, project)
    conn, _ = run_all(project)
    colours.run(conn)
    rows = conn.execute("SELECT id, dir FROM shots ORDER BY id").fetchall()
    wed = [r[0] for r in rows if "/Weddings/" in r[1]]
    day = [r[0] for r in rows if "/Everyday/" in r[1]]
    undated = conn.execute("SELECT id FROM shots WHERE stem = 'SCAN_0001'").fetchone()[0]

    people = {}
    for name in ("Bride", "Sam", "Grandad"):
        people[name] = db.new_id(conn, "people")
        conn.execute("INSERT INTO people(id, name, kind) VALUES (?, ?, 'person')", (people[name], name))
    conn.executemany("INSERT INTO person_aliases(person_id, name) VALUES (?, ?)",
                     [(people["Bride"], "The bride"), (people["Bride"], "B")])
    rng = np.random.default_rng(7)

    def face(sid, x, person=None, cluster=None):
        conn.execute("INSERT INTO faces(id, shot_id, x, y, w, h, px, area, sharp, closed, vec, cluster, person_id, person_src)"
                     " VALUES (?, ?, ?, .2, .1, .15, 120, .02, 200, 0, ?, ?, ?, ?)",
                     (db.new_id(conn, "faces"), sid, x, rng.normal(size=512).astype(np.float16).tobytes(), cluster, person,
                      "user" if person else None))

    for sid in wed[:6]:
        face(sid, .1, people["Bride"], 1)
    for sid in wed[2:5] + day[:3]:
        face(sid, .5, people["Sam"], 2)                     # in both kinds of folder
    for sid in day[3:6]:
        face(sid, .3, people["Grandad"], 3)
    for sid in wed[6:9]:
        face(sid, .7, None, 4)                              # an unnamed group
    db.set_setting(conn, "hwm_clusters", 4)

    library.decide(conn, [{"id": s, "pick": 1} for s in wed[:3]] + [{"id": day[0], "pick": 1}], what="pick")
    library.decide(conn, [{"id": wed[3], "rating": 4, "label": "Red"}, {"id": day[1], "rating": 2}], what="stars")
    library.decide(conn, [{"id": wed[4], "pick": -1}], what="reject")
    library.undo(conn)                                       # the reject is undone: history keeps both
    kw = keywords.ensure(conn, "Events › Weddings › Ceremony")
    kw2 = keywords.ensure(conn, "Family")
    keywords.assign(conn, wed[:2], add=[kw, kw2], what="tag")
    keywords.assign(conn, [day[0]], add=[kw2], what="tag")
    dating.set_dates(conn, [undated], "2024-06-01 12:00")
    conn.execute("INSERT INTO raw_choices(shot_id, keep, updated) VALUES (?, 1, 1.0)", (wed[1],))
    conn.execute("INSERT INTO viewer_decisions(viewer, shot_id, fav, updated) VALUES ('owner', ?, 1, 1.0)", (wed[2],))
    conn.execute("INSERT INTO comments(shot_id, viewer, text, ts) VALUES (?, 'owner', 'first dance', 1.0)", (wed[3],))
    albums = {}
    for name, ids in (("Best of", [wed[0], day[0]]), ("Wedding album", wed[:3])):
        albums[name] = db.new_id(conn, "albums")
        conn.execute("INSERT INTO albums(id, name, kind, created, updated) VALUES (?, ?, 'manual', 1, 1)", (albums[name], name))
        for sid in ids:
            f = conn.execute("SELECT id, path FROM files WHERE shot_id=?", (sid,)).fetchone()
            conn.execute("INSERT INTO album_items(album_id, file_id, shot_id, name) VALUES (?,?,?,?)",
                         (albums[name], f["id"], sid, os.path.basename(f["path"])))
    for sid in (wed[0], day[0]):
        f = conn.execute("SELECT id FROM files WHERE shot_id=?", (sid,)).fetchone()[0]
        conn.execute("INSERT INTO backups(file_id, shot_id, checked, drives) VALUES (?, ?, 1, 'OTHER-DRIVE')", (f, sid))
    item = conn.execute("INSERT INTO shotlist(title, people, min_count, position) VALUES ('First dance', ?, 1, 1)",
                        (json.dumps([people["Bride"]]),)).lastrowid
    conn.commit()
    smart.tag_shots(conn, item, [wed[3]])
    db.set_setting(conn, "event_joins", [{"kind": "event", "a": wed[0], "b": wed[4]}])
    db.set_setting(conn, "folder_names", {str(lib / WEDDINGS[0]): "The vows", str(lib / EVERYDAY[0]): "Spring"})
    db.set_setting(conn, "keep_under", [str(lib / WEDDINGS[2])])
    conn.commit()
    return {"conn": conn, "wed": wed, "day": day, "people": people, "undated": undated, "albums": albums,
            "keywords": [kw, kw2], "shotlist": item}


