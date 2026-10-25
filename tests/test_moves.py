"""Moving and renaming: moves made in Finder are followed without reading the files again, and moves
made in the app keep a photo's files together, never overwrite anything, keep the catalog (and
everything keyed by folder) in step so the next scan finds nothing changed, and can all be moved
back, even after a crash half way."""

from __future__ import annotations

import json
import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from conftest import add_root, make_jpeg, run_all


@pytest.fixture
def fake_trash(tmp_path, monkeypatch):
    """Stands in for the Mac's Trash (tests never touch the real one)."""
    from joeylab import cleanup

    bin_ = tmp_path / "trash"
    bin_.mkdir()

    def move(path):
        dest = bin_ / f"{len(list(bin_.iterdir()))}-{os.path.basename(path)}"
        os.rename(path, dest)
        return str(dest)

    monkeypatch.setattr(cleanup, "move_to_trash", move)
    return bin_


@pytest.fixture
def counted(monkeypatch):
    """How many files the scanner fingerprints (reads)."""
    from joeylab import scanner

    calls = []
    real = scanner.quick_fp
    monkeypatch.setattr(scanner, "quick_fp", lambda p, size: (calls.append(p), real(p, size))[1])
    return calls


def _old(*paths):
    t = time.time() - 60
    for p in paths:
        os.utime(p, (t, t))


def _blob(path, n=4096, seed=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes([seed % 251]) * 16 + os.urandom(n))
    _old(path)
    return path


def _photo_with_everything(folder, stem="IMG_1", seed=1):
    """A JPEG with its RAW, a Lightroom sidecar for each, and Apple's edit file."""
    make_jpeg(folder / f"{stem}.JPG", seed=seed)
    _blob(folder / f"{stem}.CR3", seed=seed)
    for side in (f"{stem}.xmp", f"{stem}.CR3.xmp", f"{stem}.AAE"):
        (folder / side).write_text(f"<x:xmpmeta>{side}</x:xmpmeta>")
        _old(folder / side)


def _live_photo(folder, stem="IMG_2", seed=2):
    make_jpeg(folder / f"{stem}.JPG", seed=seed)
    _blob(folder / f"{stem}.MOV", seed=seed)


def _shot(conn, stem):
    return conn.execute("SELECT id FROM shots WHERE stem=? AND status='present'", (stem,)).fetchone()[0]


def _paths(conn, sid):
    return sorted(r[0] for r in conn.execute("SELECT path FROM files WHERE shot_id=?", (sid,)))


def _quiet(rep):
    return (rep.added, rep.changed, rep.moved, rep.returned, rep.missing) == (0, 0, 0, 0, 0)


# ---------------------------------------------------------------- moves made in Finder

def test_a_folder_renamed_in_finder_is_followed_without_reading_a_file(env, counted):
    from joeylab import library, scanner

    _, lib = env
    for i in range(40):
        make_jpeg(lib / "2026" / "Phuket" / f"DSCF{i:04d}.JPG", seed=i, size=(120, 80))
    add_root(lib)
    conn, _ = run_all()
    sid = _shot(conn, "DSCF0007")
    library.set_decision(conn, sid, rating=4)
    os.rename(lib / "2026" / "Phuket", lib / "2026" / "Phuket 2026")
    counted.clear()
    rep = scanner.scan(conn)
    assert rep.moved == 40 and not rep.added and not rep.missing
    assert counted == []                                       # nothing read: same drive, same inodes
    assert conn.execute("SELECT rating FROM decisions WHERE shot_id=?", (sid,)).fetchone()[0] == 4
    assert conn.execute("SELECT dir FROM shots WHERE id=?", (sid,)).fetchone()[0] == str(lib / "2026" / "Phuket 2026")
    # The folder above renamed too: still nothing read.
    os.rename(lib / "2026", lib / "Year 2026")
    counted.clear()
    rep = scanner.scan(conn)
    assert rep.moved == 40 and counted == []
    assert _quiet(scanner.scan(conn))


def test_a_drive_that_gives_moved_files_new_inodes_costs_two_reads_per_folder(env, counted):
    from joeylab import scanner

    tmp, lib = env
    for i in range(30):
        make_jpeg(lib / "Phuket" / f"DSCF{i:04d}.JPG", seed=i, size=(120, 80))
    add_root(lib)
    conn, _ = run_all()
    before = {r[0]: r[1] for r in conn.execute("SELECT f.id, s.id FROM files f JOIN shots s ON s.id = f.shot_id")}
    # A copy that keeps times (as Finder's does), the original taken away: every inode is new.
    shutil.copytree(lib / "Phuket", lib / "Phuket copied")
    shutil.move(str(lib / "Phuket"), str(tmp / "away"))
    counted.clear()
    rep = scanner.scan(conn)
    assert rep.moved == 30 and not rep.added and not rep.missing
    assert len(counted) <= 2                                   # a spot check, not 30 reads
    assert {r[0]: r[1] for r in conn.execute("SELECT f.id, s.id FROM files f JOIN shots s ON s.id = f.shot_id")} == before


def test_made_up_inode_numbers_are_never_trusted(env):
    """exFAT under macOS's FSKit numbers files down from 2^64 and may number them again: two identical
    copies (same size and time) swapping such numbers mustn't swap places in the catalog."""
    from joeylab import db, scanner

    fake = db.to_i64(2**64 - 1)
    rows = [{"id": 1, "path": "/d/A/IMG_1.JPG", "root_id": 1, "size": 10, "mtime_ns": 5, "fp": "x", "ino": fake},
            {"id": 2, "path": "/d/Backup/A/IMG_1.JPG", "root_id": 1, "size": 10, "mtime_ns": 5, "fp": "x", "ino": fake - 1}]
    on_disk = {"/d/A2/IMG_1.JPG": (1, 10, 5, ".jpg", "jpeg", 9, fake - 1),            # the numbers came back swapped
               "/d/Backup/A2/IMG_1.JPG": (1, 10, 5, ".jpg", "jpeg", 9, fake)}
    found = scanner._moved_without_reading(rows, list(on_disk), on_disk, {1: 9})
    assert found == {} or {p: r["id"] for p, r in found.items()} == {"/d/A2/IMG_1.JPG": 1, "/d/Backup/A2/IMG_1.JPG": 2}
    # Real inode numbers, unique: matched without reading.
    rows[0]["ino"], rows[1]["ino"] = 101, 102
    on_disk = {"/d/x/one.JPG": (1, 10, 5, ".jpg", "jpeg", 9, 101), "/d/y/two.JPG": (1, 10, 5, ".jpg", "jpeg", 9, 102)}
    found = scanner._moved_without_reading(rows, list(on_disk), on_disk, {1: 9})
    assert {p: r["id"] for p, r in found.items()} == {"/d/x/one.JPG": 1, "/d/y/two.JPG": 2}


def test_a_copy_never_takes_over_from_the_file_that_stayed_or_the_one_that_moved(env):
    from joeylab import library, scanner

    _, lib = env
    make_jpeg(lib / "a" / "IMG_1.JPG", seed=1)
    make_jpeg(lib / "a" / "IMG_2.JPG", seed=2)
    add_root(lib)
    conn, _ = run_all()
    one, two = _shot(conn, "IMG_1"), _shot(conn, "IMG_2")
    library.set_decision(conn, one, rating=5)
    library.set_decision(conn, two, rating=3)
    # Copied, not moved: the original keeps its decisions; the copy is a new photo.
    (lib / "b").mkdir()
    shutil.copy2(lib / "a" / "IMG_1.JPG", lib / "b" / "IMG_1.JPG")
    rep = scanner.scan(conn)
    assert rep.added == 1 and not rep.moved
    assert _paths(conn, one) == [str(lib / "a" / "IMG_1.JPG")]
    # Moved to c and copied to b at once (b comes first in the walk): the one that moved keeps them.
    shutil.copy2(lib / "a" / "IMG_2.JPG", lib / "b" / "IMG_2.JPG")
    (lib / "c").mkdir()
    os.rename(lib / "a" / "IMG_2.JPG", lib / "c" / "IMG_2.JPG")
    rep = scanner.scan(conn)
    assert rep.moved == 1 and rep.added == 1
    assert _paths(conn, two) == [str(lib / "c" / "IMG_2.JPG")]
    assert conn.execute("SELECT rating FROM decisions WHERE shot_id=?", (two,)).fetchone()[0] == 3


def test_a_raw_and_jpeg_moved_in_finder_stay_one_photo(env, counted):
    from joeylab import library, scanner

    _, lib = env
    make_jpeg(lib / "a" / "DSC_1.JPG", seed=1)
    _blob(lib / "a" / "DSC_1.NEF")
    add_root(lib)
    conn, _ = run_all()
    sid = _shot(conn, "DSC_1")
    library.set_decision(conn, sid, pick=1)
    (lib / "b").mkdir()
    for n in ("DSC_1.JPG", "DSC_1.NEF"):
        os.rename(lib / "a" / n, lib / "b" / n)
    counted.clear()
    rep = scanner.scan(conn)
    assert rep.moved == 2 and counted == []
    assert conn.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0] == 1
    assert _paths(conn, sid) == [str(lib / "b" / "DSC_1.JPG"), str(lib / "b" / "DSC_1.NEF")]
    assert conn.execute("SELECT pick FROM decisions WHERE shot_id=?", (sid,)).fetchone()[0] == 1


def test_a_library_folder_renamed_in_finder_is_followed(env):
    from joeylab import db, scanner

    tmp, lib = env
    for i in range(3):
        make_jpeg(lib / "trip" / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    db.set_setting(conn, "folder_names", {str(lib / "trip"): "Our trip"})
    os.rename(lib, tmp / "Photos")
    rep = scanner.scan(conn)
    new = str(tmp / "Photos")
    assert rep.roots_moved and rep.roots_moved[0]["to"] == new and not rep.missing and not rep.roots_unavailable
    assert conn.execute("SELECT path FROM roots").fetchone()[0] == new
    assert all(p.startswith(new + "/") for (p,) in conn.execute("SELECT path FROM files"))
    assert db.get_setting(conn, "folder_names") == {new + "/trip": "Our trip"}
    # A folder that only happens to have the old one's inode number isn't taken for it.
    os.rename(tmp / "Photos", tmp / "elsewhere")
    os.mkdir(tmp / "unrelated")
    conn.execute("UPDATE roots SET ino=?", (os.lstat(tmp / "unrelated").st_ino,))
    conn.commit()
    rep = scanner.scan(conn)
    assert rep.roots_unavailable == [new] and not rep.missing


# ---------------------------------------------------------------- moves made in the app

def _two_folders(lib):
    _photo_with_everything(lib / "a")
    _live_photo(lib / "a")
    make_jpeg(lib / "a" / "IMG_3.JPG", seed=3)               # stays behind
    (lib / "b").mkdir()


def test_moving_photos_on_one_drive_takes_every_file_and_sidecar_and_the_next_scan_finds_nothing(env):
    from joeylab import library, moves, scanner

    _, lib = env
    _two_folders(lib)
    add_root(lib)
    conn, _ = run_all()
    one, two = _shot(conn, "IMG_1"), _shot(conn, "IMG_2")
    library.set_decision(conn, one, rating=5)
    inode = os.lstat(lib / "a" / "IMG_1.CR3").st_ino
    plan = moves.plan_move_photos(conn, "t", [one, two], str(lib / "b"))
    assert not plan.refused and plan.photos == 2 and plan.files == 7 and not plan.copying()
    assert plan.what().startswith("Move 2 photos (7 files, ") and plan.what().endswith(f"to {lib / 'b'} · same drive, instant")
    res = moves.run(conn, plan)
    assert res["photos"] == 2 and res["files"] == 7
    assert sorted(os.listdir(lib / "b")) == ["IMG_1.AAE", "IMG_1.CR3", "IMG_1.CR3.xmp", "IMG_1.JPG", "IMG_1.xmp",
                                             "IMG_2.JPG", "IMG_2.MOV"]
    assert sorted(os.listdir(lib / "a")) == ["IMG_3.JPG"]
    assert os.lstat(lib / "b" / "IMG_1.CR3").st_ino == inode                       # renamed, not copied
    assert _paths(conn, one) == [str(lib / "b" / "IMG_1.CR3"), str(lib / "b" / "IMG_1.JPG")]
    assert _paths(conn, two) == [str(lib / "b" / "IMG_2.JPG"), str(lib / "b" / "IMG_2.MOV")]
    assert conn.execute("SELECT dir FROM shots WHERE id=?", (one,)).fetchone()[0] == str(lib / "b")
    assert _quiet(scanner.scan(conn))
    assert conn.execute("SELECT rating FROM decisions WHERE shot_id=?", (one,)).fetchone()[0] == 5
    logs = list((moves.log_dir()).iterdir())
    assert len(logs) == 1 and logs[0].name.endswith(".jsonl") and ".open." not in logs[0].name
    assert oct(logs[0].stat().st_mode & 0o777) == "0o600"


def test_nothing_is_ever_overwritten_names_taken_are_skipped_or_kept_both(env):
    from joeylab import moves, scanner

    _, lib = env
    _photo_with_everything(lib / "a")
    make_jpeg(lib / "b" / "IMG_1.JPG", seed=99)                # another photo with that name
    add_root(lib)
    conn, _ = run_all()
    sid = conn.execute("SELECT id FROM shots WHERE dir=?", (str(lib / "a"),)).fetchone()[0]
    theirs = (lib / "b" / "IMG_1.JPG").read_bytes()
    plan = moves.plan_move_photos(conn, "t", [sid], str(lib / "b"))
    assert not plan.steps and len(plan.conflicts) == 1
    assert plan.as_dict()["conflict_examples"] == [{"name": "IMG_1.JPG", "keep_both": "IMG_1 2.JPG"}]
    with pytest.raises(moves.Refused, match="something of that name is there already"):
        moves.run(conn, plan)                                 # skip: nothing of it moves
    assert (lib / "b" / "IMG_1.JPG").read_bytes() == theirs and (lib / "a" / "IMG_1.CR3").exists()
    plan = moves.plan_move_photos(conn, "t", [sid], str(lib / "b"))
    res = moves.run(conn, plan, keep_both=True)
    assert res["photos"] == 1
    assert sorted(os.listdir(lib / "b")) == ["IMG_1 2.AAE", "IMG_1 2.CR3", "IMG_1 2.CR3.xmp", "IMG_1 2.JPG", "IMG_1 2.xmp",
                                             "IMG_1.JPG"]
    assert (lib / "b" / "IMG_1.JPG").read_bytes() == theirs
    assert conn.execute("SELECT COUNT(*) FROM shots WHERE dir=? AND status='present'", (str(lib / "b"),)).fetchone()[0] == 2
    assert _quiet(scanner.scan(conn))
    # Something appears at the new name after the plan was made: left alone, never replaced.
    make_jpeg(lib / "c" / "keep.JPG", seed=5)
    scanner.scan(conn)
    sid2 = _shot(conn, "keep")
    plan = moves.plan_move_photos(conn, "t", [sid2], str(lib / "a"))
    (lib / "a" / "keep.JPG").write_bytes(b"not yours")
    res = moves.run(conn, plan)
    assert res["photos"] == 0 and (lib / "a" / "keep.JPG").read_bytes() == b"not yours" and (lib / "c" / "keep.JPG").exists()
    # And the system itself refuses to replace a file.
    with pytest.raises(OSError):
        moves.rename_excl(str(lib / "c" / "keep.JPG"), str(lib / "a" / "keep.JPG"))
    assert (lib / "a" / "keep.JPG").read_bytes() == b"not yours"


def test_renaming_a_folder_brings_everything_keyed_by_it_along(env):
    from joeylab import db, dupes, moves, scanner

    _, lib = env
    for i in range(3):
        make_jpeg(lib / "SD 20260124" / "beach" / f"IMG_{i}.JPG", seed=i)
    make_jpeg(lib / "other" / "IMG_9.JPG", seed=9)
    add_root(lib)
    conn, _ = run_all()
    old, new = str(lib / "SD 20260124"), str(lib / "2026-01-24 Phuket")
    db.set_setting(conn, "folder_names", {old: "Phuket", old + "/beach": "Beach", str(lib / "other"): "Other"})
    dupes.set_keep_rule(conn, old)
    conn.execute("INSERT INTO keep_folders(folder) VALUES (?)", (old + "/beach",))
    conn.execute("INSERT INTO albums(name, kind, query) VALUES ('Beach days', 'smart', ?)", (json.dumps({"folder": old, "min_rating": 2}),))
    conn.commit()
    plan = moves.plan_rename_folder(conn, "t", old, "2026-01-24 Phuket")
    assert not plan.refused and plan.photos == 3
    assert plan.what() == "Rename “SD 20260124” to “2026-01-24 Phuket” · 3 photos inside follow, instant"
    res = moves.run(conn, plan)
    assert res["folder"] == new and os.path.isdir(new + "/beach") and not os.path.exists(old)
    assert all(p.startswith(new) for (p,) in conn.execute("SELECT path FROM files WHERE path NOT LIKE '%other%'"))
    assert db.get_setting(conn, "folder_names") == {new: "Phuket", new + "/beach": "Beach", str(lib / "other"): "Other"}
    assert dupes.keep_rules(conn) == [new]
    assert [r[0] for r in conn.execute("SELECT folder FROM keep_folders")] == [new + "/beach"]
    assert json.loads(conn.execute("SELECT query FROM albums WHERE name='Beach days'").fetchone()[0]) == {"folder": new, "min_rating": 2}
    assert _quiet(scanner.scan(conn))
    # Taken names are refused, plainly.
    assert "already something called" in moves.plan_rename_folder(conn, "t", new, "other").refused
    assert moves.plan_rename_folder(conn, "t", new, "a/b").target.endswith("/a-b")             # no slashes in a name


def test_renaming_a_library_folder_itself_moves_the_library_with_it(env):
    from joeylab import moves, scanner

    tmp, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    conn, _ = run_all()
    res = moves.run(conn, moves.plan_rename_folder(conn, "t", str(lib), "Pictures"))
    assert res["folder"] == str(tmp / "Pictures")
    assert conn.execute("SELECT path FROM roots").fetchone()[0] == str(tmp / "Pictures")
    rep = scanner.scan(conn)
    assert _quiet(rep) and not rep.roots_unavailable and not rep.roots_moved


def test_moving_a_folder_and_the_limits(env, tmp_path, monkeypatch):
    from joeylab import moves, scanner

    tmp, lib = env
    for i in range(2):
        make_jpeg(lib / "inbox" / "Phuket" / f"IMG_{i}.JPG", seed=i)
    make_jpeg(lib / "2026" / "Phuket" / "IMG_7.JPG", seed=7)   # a folder of that name is there already
    (tmp / "outside").mkdir()
    add_root(lib)
    conn, _ = run_all()
    src = str(lib / "inbox" / "Phuket")
    assert "inside itself" in moves.plan_move_folder(conn, "t", src, src).refused
    assert "already" in moves.plan_move_folder(conn, "t", src, str(lib / "inbox")).refused
    assert "leave the library" in moves.plan_move_folder(conn, "t", src, str(tmp / "outside")).refused
    assert "library's own folders" in moves.plan_move_folder(conn, "t", str(lib), str(lib / "2026")).refused
    assert "leave the library" in moves.plan_move_photos(conn, "t", [_shot(conn, "IMG_0")], str(tmp / "outside")).refused
    assert "JoeyLab's own folder" in moves.plan_new_folder(conn, "t", str(tmp / "home"), "x").refused
    plan = moves.plan_move_folder(conn, "t", src, str(lib / "2026"))
    assert not plan.steps and plan.conflicts and plan.conflicts[0]["steps"][0].dst == str(lib / "2026" / "Phuket 2")
    res = moves.run(conn, plan, keep_both=True)
    assert res["folder"] == str(lib / "2026" / "Phuket 2") and sorted(os.listdir(lib / "2026" / "Phuket 2")) == ["IMG_0.JPG", "IMG_1.JPG"]
    assert os.listdir(lib / "2026" / "Phuket") == ["IMG_7.JPG"]
    assert _quiet(scanner.scan(conn))
    # A read-only drive: refused, with the reason.
    real = os.statvfs

    class RO:
        def __init__(self, st):
            self.st = st

        def __getattr__(self, k):
            return os.ST_RDONLY if k == "f_flag" else getattr(self.st, k)

    monkeypatch.setattr(os, "statvfs", lambda p: RO(real(p)))
    why = moves.plan_move_photos(conn, "t", [_shot(conn, "IMG_7")], str(lib / "inbox")).refused
    assert "read-only" in why


def test_a_new_folder_then_moving_it_back_takes_it_away_while_empty(env, fake_trash):
    from joeylab import moves

    _, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    conn, _ = run_all()
    res = moves.run(conn, moves.plan_new_folder(conn, "t", str(lib), "Phuket"))
    assert os.path.isdir(lib / "Phuket") and res["label"] == "Made the folder “Phuket”"
    assert [f["name"] for f in moves.subfolders(conn, str(lib))] == ["Phuket"]
    assert any(f["path"] == str(lib) for f in moves.folders(conn))
    moves.run(conn, moves.plan_back(conn, "t", res["id"]))
    assert not (lib / "Phuket").exists() and len(list(fake_trash.iterdir())) == 1


def test_renaming_photos_by_a_pattern_keeps_each_photos_files_together(env):
    from joeylab import moves, scanner

    _, lib = env
    _photo_with_everything(lib / "a")
    _live_photo(lib / "a")
    add_root(lib)
    conn, _ = run_all()
    conn.execute("UPDATE shots SET capture_str='2026-01-24 10:00:00', eff_ts=1769248800")
    conn.execute("INSERT INTO events(id, name, kind) VALUES (7, 'Phuket · beach', 'event')")
    conn.execute("UPDATE shots SET event_id=7")
    conn.commit()
    ids = [_shot(conn, "IMG_1"), _shot(conn, "IMG_2")]
    plan = moves.plan_rename_photos(conn, "t", ids, "{date} {event} {n}")
    assert [p["to"] for p in plan.preview] == ["2026-01-24 Phuket 001.JPG", "2026-01-24 Phuket 002.JPG"]
    assert "JoeyLab doesn't know {when}" in moves.plan_rename_photos(conn, "t", ids, "{when} {n}").refused
    res = moves.run(conn, plan)
    assert res["photos"] == 2
    assert sorted(os.listdir(lib / "a")) == ["2026-01-24 Phuket 001.AAE", "2026-01-24 Phuket 001.CR3", "2026-01-24 Phuket 001.CR3.xmp",
                                             "2026-01-24 Phuket 001.JPG", "2026-01-24 Phuket 001.xmp", "2026-01-24 Phuket 002.JPG",
                                             "2026-01-24 Phuket 002.MOV"]
    assert conn.execute("SELECT stem FROM shots WHERE id=?", (ids[0],)).fetchone()[0] == "2026-01-24 Phuket 001"
    assert conn.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0] == 2
    assert _quiet(scanner.scan(conn))
    moves.run(conn, moves.plan_back(conn, "t", res["id"]))
    assert sorted(os.listdir(lib / "a")) == ["IMG_1.AAE", "IMG_1.CR3", "IMG_1.CR3.xmp", "IMG_1.JPG", "IMG_1.xmp", "IMG_2.JPG", "IMG_2.MOV"]
    assert _quiet(scanner.scan(conn))


def test_move_back_puts_everything_where_it_was(env):
    from joeylab import moves, scanner

    _, lib = env
    _two_folders(lib)
    add_root(lib)
    conn, _ = run_all()
    ids = [_shot(conn, "IMG_1"), _shot(conn, "IMG_2")]
    res = moves.run(conn, moves.plan_move_photos(conn, "t", ids, str(lib / "b")))
    rmdir = os.rmdir                                           # the folder they came from goes in the meantime
    os.rename(lib / "a" / "IMG_3.JPG", lib / "b" / "IMG_3.JPG")
    rmdir(lib / "a")
    scanner.scan(conn)
    back = moves.plan_back(conn, "t", res["id"])
    assert back.what().startswith("Move back: Moved 2 photos to b")
    moves.run(conn, back)
    assert sorted(os.listdir(lib / "a")) == ["IMG_1.AAE", "IMG_1.CR3", "IMG_1.CR3.xmp", "IMG_1.JPG", "IMG_1.xmp", "IMG_2.JPG", "IMG_2.MOV"]
    assert _paths(conn, ids[0]) == [str(lib / "a" / "IMG_1.CR3"), str(lib / "a" / "IMG_1.JPG")]
    assert _quiet(scanner.scan(conn))
    hist = moves.history("t")
    assert [h["state"] for h in hist] == ["done", "moved back"] and hist[0]["undoes"] == res["id"]
    assert "moved back already" in moves.plan_back(conn, "t", res["id"]).refused
    assert moves.plan_back(conn, "t", hist[0]["id"]).done_label() == "Again: Moved 2 photos to b"     # a redo


def test_another_drive_copies_checks_then_trashes_and_move_back_puts_the_originals_back(env, fake_trash, monkeypatch):
    from joeylab import hashing, moves, scanner

    _, lib = env
    _two_folders(lib)
    add_root(lib)
    conn, _ = run_all()
    monkeypatch.setattr(moves, "same_drive", lambda a, b: False)       # b stands in for another drive
    ids = [_shot(conn, "IMG_1"), _shot(conn, "IMG_2")]
    originals = {n: (lib / "a" / n).read_bytes() for n in os.listdir(lib / "a")}
    inode = os.lstat(lib / "a" / "IMG_1.JPG").st_ino
    plan = moves.plan_move_photos(conn, "t", ids, str(lib / "b"))
    assert plan.copying() and "copied and checked, then the original goes to the Trash" in plan.what()
    res = moves.run(conn, plan)
    assert res["photos"] == 2 and not res["trash_failed"]
    for n in os.listdir(lib / "b"):
        assert (lib / "b" / n).read_bytes() == originals[n]
        assert not n.startswith(".joeylab")
    assert sorted(os.listdir(lib / "a")) == ["IMG_3.JPG"]
    assert len(list(fake_trash.iterdir())) == 7                     # the originals: in the Trash, not deleted
    row = conn.execute("SELECT path, full_hash FROM files WHERE path LIKE '%IMG_1.JPG'").fetchone()
    assert row[0] == str(lib / "b" / "IMG_1.JPG") and row[1] == hashing.full_hash(row[0])
    assert _quiet(scanner.scan(conn))
    # Moving back: the originals come back from the Trash (the very same files), the copies go there.
    moves.run(conn, moves.plan_back(conn, "t", res["id"]))
    assert os.lstat(lib / "a" / "IMG_1.JPG").st_ino == inode
    assert sorted(os.listdir(lib / "b")) == []
    assert _quiet(scanner.scan(conn))


def test_a_folder_to_another_drive_is_copied_whole_then_the_original_goes_to_the_trash(env, fake_trash, monkeypatch):
    from joeylab import moves, scanner

    _, lib = env
    for i in range(3):
        make_jpeg(lib / "inbox" / "Phuket" / "day 1" / f"IMG_{i}.JPG", seed=i)
    (lib / "inbox" / "Phuket" / "notes.txt").write_text("not a photo, still goes")
    (lib / "2026").mkdir()
    add_root(lib)
    conn, _ = run_all()
    monkeypatch.setattr(moves, "same_drive", lambda a, b: False)
    res = moves.run(conn, moves.plan_move_folder(conn, "t", str(lib / "inbox" / "Phuket"), str(lib / "2026")))
    new = lib / "2026" / "Phuket"
    assert res["folder"] == str(new) and (new / "notes.txt").read_text() == "not a photo, still goes"
    assert sorted(os.listdir(new / "day 1")) == ["IMG_0.JPG", "IMG_1.JPG", "IMG_2.JPG"]
    assert not (lib / "inbox" / "Phuket").exists() and len(list(fake_trash.iterdir())) == 1
    assert _quiet(scanner.scan(conn))
    moves.run(conn, moves.plan_back(conn, "t", res["id"]))
    assert sorted(os.listdir(lib / "inbox" / "Phuket" / "day 1")) == ["IMG_0.JPG", "IMG_1.JPG", "IMG_2.JPG"]
    assert not new.exists()
    assert _quiet(scanner.scan(conn))


class Crash(BaseException):
    """The process stopping dead (power cut, kill -9)."""


def test_a_move_stopped_half_way_is_finished_from_its_log(env, monkeypatch):
    from joeylab import moves, scanner

    _, lib = env
    _two_folders(lib)
    add_root(lib)
    conn, _ = run_all()
    one = _shot(conn, "IMG_1")
    plan = moves.plan_move_photos(conn, "t", [one], str(lib / "b"))
    real, calls = moves.rename_excl, []

    def dies_after_two(a, b):
        if len(calls) == 2:
            raise Crash()
        calls.append(a)
        real(a, b)

    recover = moves.recover
    monkeypatch.setattr(moves, "rename_excl", dies_after_two)
    monkeypatch.setattr(moves, "recover", lambda c, p: [])          # nothing gets to tidy up
    with pytest.raises(Crash):
        moves.run(conn, plan)
    conn.rollback()                                                   # what a dead process's transaction does
    monkeypatch.setattr(moves, "rename_excl", real)
    monkeypatch.setattr(moves, "recover", recover)
    assert len(os.listdir(lib / "b")) == 2                            # half the photo moved
    open_logs = list(moves.log_dir().glob("*.open.jsonl"))
    assert len(open_logs) == 1 and moves.history("t")[0]["state"] == "stopped"
    out = moves.recover(conn, "t")
    assert out and out[0]["finished"] == 3                          # the other three files of the photo
    assert sorted(os.listdir(lib / "b")) == ["IMG_1.AAE", "IMG_1.CR3", "IMG_1.CR3.xmp", "IMG_1.JPG", "IMG_1.xmp"]
    assert _paths(conn, one) == [str(lib / "b" / "IMG_1.CR3"), str(lib / "b" / "IMG_1.JPG")]
    assert not list(moves.log_dir().glob("*.open.jsonl"))
    assert _quiet(scanner.scan(conn))
    moves.run(conn, moves.plan_back(conn, "t", out[0]["id"]))        # and it can be moved back like any other
    assert sorted(os.listdir(lib / "b")) == []


def test_a_copy_to_another_drive_stopped_half_way_leaves_the_originals_and_no_stray_copies(env, fake_trash, monkeypatch):
    from joeylab import moves, scanner

    _, lib = env
    _two_folders(lib)
    add_root(lib)
    conn, _ = run_all()
    monkeypatch.setattr(moves, "same_drive", lambda a, b: False)
    one = _shot(conn, "IMG_1")
    plan = moves.plan_move_photos(conn, "t", [one], str(lib / "b"))
    real, calls = moves.copy_checked, []

    def dies_on_the_third(a, b):
        calls.append(a)
        out = real(a, b)
        if len(calls) == 3:
            raise Crash()
        return out

    recover = moves.recover
    monkeypatch.setattr(moves, "copy_checked", dies_on_the_third)
    monkeypatch.setattr(moves, "recover", lambda c, p: [])
    with pytest.raises(Crash):
        moves.run(conn, plan)
    monkeypatch.setattr(moves, "copy_checked", real)
    monkeypatch.setattr(moves, "recover", recover)
    assert len([n for n in os.listdir(lib / "b") if n.startswith(".joeylab-partial-")]) == 3
    out = moves.recover(conn, "t")
    assert out[0]["removed"] == 3 and out[0]["finished"] == 0
    assert os.listdir(lib / "b") == [] and len(os.listdir(lib / "a")) == 8 and not list(fake_trash.iterdir())
    assert _paths(conn, one) == [str(lib / "a" / "IMG_1.CR3"), str(lib / "a" / "IMG_1.JPG")]
    assert _quiet(scanner.scan(conn))


def test_an_update_finishes_a_stopped_move_before_it_scans(env, monkeypatch):
    from joeylab import moves, pipeline

    _, lib = env
    _two_folders(lib)
    add_root(lib)
    conn, _ = run_all()
    one = _shot(conn, "IMG_1")
    plan = moves.plan_move_photos(conn, "t", [one], str(lib / "b"))
    real, recover = moves.rename_excl, moves.recover

    def dies_after_one(a, b):
        real(a, b)
        raise Crash()

    monkeypatch.setattr(moves, "rename_excl", dies_after_one)
    monkeypatch.setattr(moves, "recover", lambda c, p: [])
    with pytest.raises(Crash):
        moves.run(conn, plan)
    conn.rollback()
    monkeypatch.setattr(moves, "rename_excl", real)
    monkeypatch.setattr(moves, "recover", recover)
    out = pipeline.run_all(conn, "t", pipeline.Reporter(), faces=False, ai_layer=False)
    assert out["scan"]["moved"] == 0 and out["scan"]["added"] == 0 and out["scan"]["missing"] == 0
    assert _paths(conn, one) == [str(lib / "b" / "IMG_1.CR3"), str(lib / "b" / "IMG_1.JPG")]


def test_a_move_waits_for_a_running_update(env):
    from joeylab import locks, moves

    _, lib = env
    _two_folders(lib)
    add_root(lib)
    conn, _ = run_all()
    plan = moves.plan_move_photos(conn, "t", [_shot(conn, "IMG_2")], str(lib / "b"))
    import threading

    held, waited = threading.Event(), []

    def update():
        with locks.project_lock("t"):
            held.set()
            time.sleep(1.2)

    t = threading.Thread(target=update)
    t.start()
    held.wait()
    with pytest.raises(locks.Busy):
        moves.run(conn, plan)
    assert (lib / "a" / "IMG_2.JPG").exists()
    res = moves.run(conn, plan, wait=10, on_wait=lambda: waited.append(1))
    t.join()
    assert waited and res["photos"] == 1 and (lib / "b" / "IMG_2.JPG").exists()


# ---------------------------------------------------------------- the app

@pytest.fixture
def web(env):
    tmp, lib = env
    _two_folders(lib)
    add_root(lib)
    conn, _ = run_all()
    from joeylab.web.jobs import JobManager
    from joeylab.web.server import create_app

    c = TestClient(create_app(jobs=JobManager(watch_minutes=0, faces=False, ai_layer=False), owner="me@example.com"))
    return c, conn, lib


def _follow(c, job):
    for _ in range(100):
        if job["state"] not in ("moving", "waiting"):
            return job
        time.sleep(0.05)
        job = c.get(f"/api/p/t/moves/{job['token']}").json()
    raise AssertionError(job)


def test_the_app_shows_the_plan_moves_lists_and_moves_back(web):
    c, conn, lib = web
    ids = [_shot(conn, "IMG_1"), _shot(conn, "IMG_2")]
    folders = c.get("/api/p/t/moves/folders").json()["folders"]
    assert {f["path"] for f in folders} >= {str(lib), str(lib / "a")}
    assert [f["name"] for f in c.get("/api/p/t/moves/folders", params={"path": str(lib)}).json()["folders"]] == ["a", "b"]
    job = c.post("/api/p/t/moves/plan", json={"op": "move_photos", "ids": ids, "into": str(lib / "b")}).json()
    assert job["state"] == "ready" and job["plan"]["what"].startswith("Move 2 photos (7 files")
    job = _follow(c, c.post(f"/api/p/t/moves/{job['token']}/run", json={}).json())
    assert job["state"] == "done" and job["result"]["photos"] == 2, job
    assert c.post(f"/api/p/t/moves/{job['token']}/run", json={}).status_code == 409             # once only
    listed = c.get("/api/p/t/moves").json()["moves"]
    assert listed[0]["label"] == "Moved 2 photos to b" and listed[0]["can_move_back"]
    back = _follow(c, c.post(f"/api/p/t/moves/{listed[0]['id']}/back", json={"run": True}).json())
    assert back["state"] == "done" and (lib / "a" / "IMG_1.JPG").exists()
    assert c.get("/api/p/t/moves").json()["moves"][1]["state"] == "moved back"
    preview = c.post("/api/p/t/moves/plan", json={"op": "rename_photos", "ids": ids, "pattern": "Phuket {n}", "preview": True}).json()
    assert preview["token"] is None and [p["to"] for p in preview["plan"]["preview"]] == ["Phuket 001.JPG", "Phuket 002.JPG"]
    refused = c.post("/api/p/t/moves/plan", json={"op": "move_photos", "ids": ids, "into": "/"}).json()
    assert "leave the library" in refused["plan"]["refused"]
    assert c.post(f"/api/p/t/moves/{refused['token']}/run", json={}).status_code == 400


def test_only_the_owner_moves_anything(web):
    c, conn, lib = web
    other = {"X-Forwarded-For": "100.64.0.9", "Tailscale-User-Login": "friend@example.com"}
    sid = _shot(conn, "IMG_1")
    for method, url, body in (("post", "/api/p/t/moves/plan", {"op": "move_photos", "ids": [sid], "into": str(lib / "b")}),
                              ("get", "/api/p/t/moves", None), ("get", "/api/p/t/moves/folders", None),
                              ("post", "/api/p/t/moves/20260101-000000-abcdef/back", {"run": True}),
                              ("post", "/api/p/t/moves/recover", {})):
        r = c.post(url, json=body, headers=other) if method == "post" else c.get(url, headers=other)
        assert r.status_code == 403, url
    assert (lib / "a" / "IMG_1.JPG").exists()
