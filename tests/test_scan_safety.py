"""What a scan does when files change under it in Finder, and what it keeps about each photo: a file moved
or put onto a catalogued path never takes that path's decisions; an edited file stays the same photo; a
folder renamed in Finder takes what's kept by its path along; a folder given to another library stays
out when renamed; and every table about a photo is handled when photos join or are forgotten."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from conftest import add_root, jpeg_at, make_jpeg, run_all, synthetic_library


def _quiet(rep):
    return (rep.added, rep.changed, rep.moved, rep.returned, rep.missing) == (0, 0, 0, 0, 0)


def _shot_at(conn, path) -> int | None:
    r = conn.execute("SELECT shot_id FROM files WHERE path=? AND status='present'", (str(path),)).fetchone()
    return r[0] if r else None


def _decision(conn, sid):
    r = conn.execute("SELECT rating, pick FROM decisions WHERE shot_id=?", (sid,)).fetchone()
    return (r[0] or None, r[1]) if r else (None, 0)


def _old(p):
    import time

    t = time.time() - 60
    os.utime(p, (t, t))


# ---------------------------------------------------------------- the tables about each photo

def _created_tables() -> dict[str, list[str]]:
    """Every table any of JoeyLab's code makes (some only when first needed), with its columns: each
    CREATE TABLE statement in the source, run on an empty database."""
    import joeylab

    mem = sqlite3.connect(":memory:")
    out = {}
    for f in Path(joeylab.__file__).parent.rglob("*.py"):
        if f.name == "copies.py":               # the index of backups every library shares: a database of its own
            continue
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\(", text):
            depth, i = 1, m.end()
            while depth and i < len(text):
                depth += {"(": 1, ")": -1}.get(text[i], 0)
                i += 1
            sql = text[m.start():i]
            if "{" in sql:                      # built with f-strings: not a plain table
                continue
            try:
                mem.execute(sql)
            except sqlite3.Error:
                continue
            out[m.group(1)] = [r[1] for r in mem.execute(f'PRAGMA table_info("{m.group(1)}")')]
    return out


def test_every_table_about_a_photo_is_in_the_registry(env):
    from joeylab import ai, dating, db, scanner, search, split
    from joeylab.web.routes import share

    _, lib = env
    s = synthetic_library(lib)
    conn = s["conn"]
    for sql in (ai.SCHEMA, search.SCHEMA):
        db._script(conn, sql)
    dating.ensure(conn)
    conn.execute(share.SHARE_PHOTOS)
    tables = {t: [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
              for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    tables.update(_created_tables())
    about_a_photo = {t for t, cols in tables.items() if "shot_id" in cols} - {"files"}     # (the photo's files themselves)
    assert about_a_photo, "no tables found"
    assert about_a_photo <= set(db.PER_PHOTO) | set(db.PER_PHOTO_LINKED), about_a_photo - set(db.PER_PHOTO) - set(db.PER_PHOTO_LINKED)
    assert not set(db.PER_PHOTO) & set(db.PER_PHOTO_LINKED)
    # Moving folders copies every one of them; two photos joining carries every one of yours.
    assert set(split.SIMPLE) == set(db.PER_PHOTO)
    assert set(scanner._NEWER_BY) == set(db.MADE_PER_PHOTO) - {"decisions", "viewer_decisions"}
    # Every catalog has the time shifts and hand-given dates from the start (not only once used).
    fresh = db.connect("fresh")
    have = {r[0] for r in fresh.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"shot_offsets", "manual_dates"} <= have
    assert "ino" in {r[1] for r in fresh.execute("PRAGMA table_info(excluded_folders)")}


def test_joining_two_photos_keeps_their_shift_date_and_raw_choice(env):
    """A RAW moved in Finder next to its JPEG joins it: a time shift, a hand-given date and a RAW choice
    made on either come along (the newer one where both had one), and the shifted time is kept."""
    from joeylab import dating, library, scanner

    tmp, lib = env
    jpeg_at(lib / "a" / "DSC_1.JPG", seed=1, when="2024:05:01 10:00:00")
    raw = lib / "b" / "DSC_1.NEF"
    raw.parent.mkdir()
    raw.write_bytes(b"\x01" * 16 + os.urandom(4096))
    _old(raw)
    add_root(lib)
    conn, _ = run_all()
    jpeg, nef = _shot_at(conn, lib / "a" / "DSC_1.JPG"), _shot_at(conn, raw)
    assert jpeg != nef
    library.set_decision(conn, jpeg, pick=1)
    conn.execute("INSERT INTO shot_offsets(shot_id, offset_s, set_at) VALUES (?, 3600, 5)", (nef,))
    conn.execute("INSERT INTO raw_choices(shot_id, keep, updated) VALUES (?, 1, 5)", (nef,))
    conn.execute("INSERT INTO raw_choices(shot_id, keep, updated) VALUES (?, 0, 1)", (jpeg,))   # older
    conn.commit()
    os.rename(raw, lib / "a" / "DSC_1.NEF")
    scanner.scan(conn)
    assert conn.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0] == 1
    assert _shot_at(conn, lib / "a" / "DSC_1.NEF") == jpeg
    assert dating.shifts(conn) == {jpeg: 3600.0}
    assert dict(conn.execute("SELECT shot_id, keep FROM raw_choices")) == {jpeg: 1}
    eff, cap = conn.execute("SELECT eff_ts, capture_ts FROM shots WHERE id=?", (jpeg,)).fetchone()
    assert eff == cap + 3600
    # Forgetting a missing photo forgets what you made about it, its shift and date too.
    conn.execute("INSERT INTO manual_dates(shot_id, capture_str, set_at) VALUES (?, '2020-01-01 12:00:00', 1)", (jpeg,))
    conn.commit()
    os.rename(lib / "a", tmp / "away")
    scanner.scan(conn)
    scanner.prune_missing(conn)
    for t in ("decisions", "raw_choices", "manual_dates", "shot_offsets"):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t


def test_a_hand_given_date_gives_way_to_the_files_own_and_no_camera_correction_moves_it(env):
    from joeylab import clocksync, dating, scanner
    from joeylab.grouping import camera_key
    from PIL import Image

    _, lib = env
    p = make_jpeg(lib / "scan.jpg", seed=1)
    # A camera model but no date: a hand-given date is the time itself.
    q = make_jpeg(lib / "nodate.jpg", seed=2)
    img = Image.open(q)
    exif = img.getexif()
    exif[0x0110] = "TestCam"
    img.save(q, "JPEG", quality=90, exif=exif.tobytes())
    _old(q)
    jpeg_at(lib / "dated.jpg", seed=3, when="2024:03:03 10:00:00")
    add_root(lib)
    conn, _ = run_all()
    undated, cam_only = _shot_at(conn, p), _shot_at(conn, q)
    dated = _shot_at(conn, lib / "dated.jpg")
    dating.set_dates(conn, [undated, cam_only], "1998-06-14")
    given = dating.ts_of("1998-06-14 12:00:00")
    assert conn.execute("SELECT camera FROM shots WHERE id=?", (cam_only,)).fetchone()[0] == "TestCam"
    clocksync.set_offset(conn, camera_key("TestCam", None), 7200)
    assert conn.execute("SELECT eff_ts FROM shots WHERE id=?", (cam_only,)).fetchone()[0] == given
    eff, cap = conn.execute("SELECT eff_ts, capture_ts FROM shots WHERE id=?", (dated,)).fetchone()
    assert eff == cap + 7200
    # The scan gets its own date (written into the file by another app): the given one goes.
    jpeg_at(p, seed=1, when="2001:01:01 09:00:00")
    rep = scanner.scan(conn)
    assert rep.changed == 1
    assert dating.manual(conn, [undated, cam_only]) == {cam_only: "1998-06-14 12:00:00"}
    assert tuple(conn.execute("SELECT time_src, capture_str FROM shots WHERE id=?", (undated,)).fetchone()) == \
        ("exif", "2001-01-01 09:00:00")


# ---------------------------------------------------------------- a file put onto a catalogued path

def _x_and_y(lib):
    jpeg_at(lib / "X" / "IMG_0001.JPG", seed=1, when="2024:01:01 10:00:00")
    jpeg_at(lib / "Y" / "IMG_0001.JPG", seed=2, when="2024:02:02 12:00:00")
    add_root(lib)
    conn, _ = run_all()
    x, y = _shot_at(conn, lib / "X" / "IMG_0001.JPG"), _shot_at(conn, lib / "Y" / "IMG_0001.JPG")
    from joeylab import library

    library.set_decision(conn, x, pick=-1)                 # rejected
    library.set_decision(conn, y, rating=5, pick=1)        # a keeper
    return conn, x, y


def _the_keeper_is_where_it_went(conn, lib, x, y):
    from joeylab import library

    assert _shot_at(conn, lib / "X" / "IMG_0001.JPG") == y
    assert _decision(conn, y) == (5, 1) and library.shot(conn, y)["status"] == "present"
    assert library.shot(conn, x)["status"] == "missing" and _decision(conn, x) == (None, -1)
    rejected = conn.execute("SELECT COUNT(*) FROM shots s JOIN decisions d ON d.shot_id = s.id"
                            " WHERE s.status='present' AND d.pick=-1").fetchone()[0]
    assert rejected == 0                                   # a rejects Trash run has nothing to take


def test_a_file_moved_onto_a_catalogued_path_brings_its_own_decisions(env):
    """X's photo (rejected) went to the Trash in Finder; Y's (a 5-star pick) was moved into X: the keeper
    keeps its stars, and the reject stays with the photo that went."""
    from joeylab import library, scanner

    tmp, lib = env
    conn, x, y = _x_and_y(lib)
    os.rename(lib / "X" / "IMG_0001.JPG", tmp / "Trash-IMG_0001.JPG")
    os.rename(lib / "Y" / "IMG_0001.JPG", lib / "X" / "IMG_0001.JPG")
    rep = scanner.scan(conn)
    assert (rep.moved, rep.missing, rep.added, rep.changed) == (1, 1, 0, 0)
    _the_keeper_is_where_it_went(conn, lib, x, y)
    assert _quiet(scanner.scan(conn))
    # X's photo comes back from the Trash into another folder: found by its content, still rejected.
    (lib / "Z").mkdir()
    os.rename(tmp / "Trash-IMG_0001.JPG", lib / "Z" / "IMG_0001.JPG")
    rep = scanner.scan(conn)
    assert rep.moved == 1 and not rep.added
    assert _shot_at(conn, lib / "Z" / "IMG_0001.JPG") == x and library.shot(conn, x)["status"] == "present"
    assert _decision(conn, x) == (None, -1)
    assert _quiet(scanner.scan(conn))


def test_finder_replace_brings_the_moved_files_decisions(env):
    from joeylab import scanner

    _, lib = env
    conn, x, y = _x_and_y(lib)
    os.replace(lib / "Y" / "IMG_0001.JPG", lib / "X" / "IMG_0001.JPG")      # "Replace" when moving it into X
    rep = scanner.scan(conn)
    assert (rep.moved, rep.missing, rep.added, rep.changed) == (1, 1, 0, 0)
    _the_keeper_is_where_it_went(conn, lib, x, y)
    assert _quiet(scanner.scan(conn))


def test_another_photo_put_in_a_catalogued_place_is_a_new_photo(env):
    """A photo from outside the library replaces one in it: it's a new photo, with nothing decided, and
    the one it replaced is missing with its decisions."""
    from joeylab import library, scanner

    tmp, lib = env
    conn, x, y = _x_and_y(lib)
    outside = jpeg_at(tmp / "card" / "IMG_0001.JPG", seed=3, when="2025:03:03 09:00:00")
    os.replace(outside, lib / "X" / "IMG_0001.JPG")
    rep = scanner.scan(conn)
    assert (rep.added, rep.missing, rep.moved, rep.changed) == (1, 1, 0, 0)
    new = _shot_at(conn, lib / "X" / "IMG_0001.JPG")
    assert new not in (x, y) and _decision(conn, new) == (None, 0)
    assert library.shot(conn, x)["status"] == "missing" and _decision(conn, x) == (None, -1)
    assert _quiet(scanner.scan(conn))


def test_an_edited_file_stays_the_same_photo(env):
    """Written again where it is (the same file), or saved as a new file in its place by an editor that
    keeps the capture time and camera: the same photo, with its decisions, analysed again."""
    from joeylab import scanner

    _, lib = env
    conn, x, y = _x_and_y(lib)
    p = lib / "X" / "IMG_0001.JPG"
    ino = os.stat(p).st_ino
    jpeg_at(p, seed=11, when="2024:01:01 10:00:00")       # rewritten in place
    assert os.stat(p).st_ino == ino
    rep = scanner.scan(conn)
    assert (rep.changed, rep.added, rep.missing, rep.moved) == (1, 0, 0, 0)
    assert _shot_at(conn, p) == x and _decision(conn, x) == (None, -1)
    saved = jpeg_at(lib / "X" / ".IMG_0001.JPG.sb-tmp", seed=12, when="2024:01:01 10:00:00")
    os.replace(saved, p)                                  # a new file in its place, same moment and camera
    assert os.stat(p).st_ino != ino
    rep = scanner.scan(conn)
    assert (rep.changed, rep.added, rep.missing, rep.moved) == (1, 0, 0, 0)
    assert _shot_at(conn, p) == x and _decision(conn, x) == (None, -1)
    assert _quiet(scanner.scan(conn))


def test_two_files_that_swap_names_keep_their_own_decisions(env):
    from joeylab import library, scanner

    tmp, lib = env
    a = jpeg_at(lib / "trip" / "IMG_1.JPG", seed=1, when="2024:01:01 10:00:00")
    b = jpeg_at(lib / "trip" / "IMG_2.JPG", seed=2, when="2024:01:01 11:00:00")
    add_root(lib)
    conn, _ = run_all()
    one, two = _shot_at(conn, a), _shot_at(conn, b)
    library.set_decision(conn, one, rating=5)
    library.set_decision(conn, two, pick=-1)
    os.rename(a, tmp / "swap")
    os.rename(b, a)
    os.rename(tmp / "swap", b)
    rep = scanner.scan(conn)
    assert (rep.moved, rep.added, rep.missing, rep.changed) == (2, 0, 0, 0)
    assert _shot_at(conn, b) == one and _decision(conn, one) == (5, 0)
    assert _shot_at(conn, a) == two and _decision(conn, two) == (None, -1)
    assert conn.execute("SELECT stem FROM shots WHERE id=?", (one,)).fetchone()[0] == "IMG_2"
    assert _quiet(scanner.scan(conn))


# ---------------------------------------------------------------- folders renamed in Finder

def test_a_folder_renamed_in_finder_takes_what_is_kept_by_its_path_along(env):
    from joeylab import db, dupes, scanner

    _, lib = env
    for i in range(3):
        make_jpeg(lib / "SD 20260124" / "beach" / f"IMG_{i}.JPG", seed=i)
    make_jpeg(lib / "SD 20260124" / "IMG_5.JPG", seed=5)
    make_jpeg(lib / "other" / "IMG_9.JPG", seed=9)
    add_root(lib)
    conn, _ = run_all()
    old, new, other = str(lib / "SD 20260124"), str(lib / "2026-01-24 Phuket"), str(lib / "other")
    db.set_setting(conn, "folder_names", {old: "Phuket", old + "/beach": "Beach", other: "Other"})
    dupes.set_keep_rule(conn, old)
    conn.execute("INSERT INTO keep_folders(folder) VALUES (?)", (old + "/beach",))
    conn.execute("INSERT INTO albums(name, kind, query) VALUES ('Beach days', 'smart', ?)",
                 (json.dumps({"folder": old + "/beach", "min_rating": 2}),))
    conn.commit()
    os.rename(old, new)
    rep = scanner.scan(conn)
    assert rep.moved == 4 and not rep.added and not rep.missing
    assert db.get_setting(conn, "folder_names") == {new: "Phuket", new + "/beach": "Beach", other: "Other"}
    assert dupes.keep_rules(conn) == [new]
    assert [r[0] for r in conn.execute("SELECT folder FROM keep_folders")] == [new + "/beach"]
    assert json.loads(conn.execute("SELECT query FROM albums WHERE name='Beach days'").fetchone()[0])["folder"] == new + "/beach"
    assert _quiet(scanner.scan(conn))
    # Moved into another folder whole (not renamed): the same.
    os.rename(new + "/beach", other + "/beach")
    rep = scanner.scan(conn)
    assert rep.moved == 3
    assert db.get_setting(conn, "folder_names") == {new: "Phuket", other + "/beach": "Beach", other: "Other"}
    assert [r[0] for r in conn.execute("SELECT folder FROM keep_folders")] == [other + "/beach"]
    # Only some of a folder's photos moved (a folder split in two): what's kept by it stays where it was.
    os.mkdir(lib / "split")
    os.rename(other + "/beach/IMG_0.JPG", lib / "split" / "IMG_0.JPG")
    os.rename(other + "/beach", lib / "split" / "beach")
    rep = scanner.scan(conn)
    assert rep.moved == 3
    assert db.get_setting(conn, "folder_names")[other + "/beach"] == "Beach"
    assert [r[0] for r in conn.execute("SELECT folder FROM keep_folders")] == [other + "/beach"]


def test_a_folder_given_to_another_library_stays_out_when_renamed_in_finder(env):
    from joeylab import db, scanner, split

    _, lib = env
    s = synthetic_library(lib)
    s["conn"].close()
    smith = lib / "Weddings" / "Smith 2024"
    split.run("t", [str(smith)], "smith", "shoot")
    src = db.connect("t")
    assert src.execute("SELECT ino FROM excluded_folders").fetchone()[0] == db.to_i64(os.lstat(smith).st_ino)
    count = lambda: src.execute("SELECT COUNT(*) FROM files").fetchone()[0]  # noqa: E731
    n = count()
    # Renamed where it is.
    os.rename(smith, lib / "Weddings" / "Smith wedding")
    rep = scanner.scan(src)
    assert _quiet(rep) and count() == n
    assert db.excluded_folders(src) == [str(lib / "Weddings" / "Smith wedding")]
    # The folder above it renamed: the photos left in it follow, the folder given away stays out.
    os.rename(lib / "Weddings", lib / "Weddings old")
    rep = scanner.scan(src)
    assert rep.moved > 0 and not rep.added and not rep.missing and count() == n
    assert db.excluded_folders(src) == [str(lib / "Weddings old" / "Smith wedding")]
    assert _quiet(scanner.scan(src))
    # A folder that happens to have its inode number but holds this library's photos is never taken for it.
    src.execute("UPDATE excluded_folders SET folder=?, ino=?",
                (str(lib / "gone"), db.to_i64(os.lstat(lib / "Everyday").st_ino)))
    src.commit()
    scanner.scan(src)
    assert db.excluded_folders(src) == [str(lib / "gone")]
