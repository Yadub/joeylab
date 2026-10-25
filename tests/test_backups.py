"""Copies on another drive are backups, not duplicates: duplicate sets per drive, the index every
library shares (copies.py), and what the Library, the viewer, the Trash dialogs and MCP say."""

from __future__ import annotations

import os
import shutil

import pytest
from conftest import make_jpeg, run_all
from PIL import Image


@pytest.fixture()
def drives(env, tmp_path, monkeypatch):
    """Folders under <tmp>/Volumes count as drives, each known by its own UUID."""
    from joeylab import volumes

    vols = tmp_path / "Volumes"
    uuids = {"DriveA": "UUID-A", "DriveB": "UUID-B", "DriveC": "UUID-C"}
    monkeypatch.setattr(volumes, "VOLUMES", vols)
    monkeypatch.setattr(volumes, "_info", lambda mount: {"uuid": uuids.get(os.path.basename(mount)),
                                                        "name": os.path.basename(mount)})
    volumes._cache.clear()
    yield vols, uuids
    volumes._cache.clear()


def _library(project: str, *folders):
    from joeylab import db, scanner

    conn = db.connect(project)
    for f in folders:
        scanner.add_root(conn, f)
    return conn


def _update(project: str):
    from joeylab import copies

    conn, _ = run_all(project)
    copies.refresh(conn, project)
    copies.sync(conn, project)
    return conn


def _shot(conn, path) -> int:
    return conn.execute("SELECT shot_id FROM files WHERE path=?", (str(path),)).fetchone()[0]


def _sets(conn, root) -> list:
    """Every duplicate set as (kind, paths relative to root, the kept one), in a stable order."""
    out = []
    for sid, kind in conn.execute("SELECT id, kind FROM dup_sets ORDER BY kind, id"):
        ms = conn.execute("SELECT f.path, dm.keep FROM dup_members dm JOIN files f ON f.id = dm.file_id WHERE dm.set_id=?",
                          (sid,)).fetchall()
        out.append((kind, sorted(os.path.relpath(p, root) for p, _ in ms),
                    next(os.path.relpath(p, root) for p, k in ms if k)))
    return sorted(out)


def _photos(base):
    for i in range(3):
        make_jpeg(base / "2024 Lisbon" / f"IMG_{i}.JPG", seed=i)
    (base / "Backup").mkdir(parents=True, exist_ok=True)
    for i in range(2):
        shutil.copy2(base / "2024 Lisbon" / f"IMG_{i}.JPG", base / "Backup" / f"IMG_{i}.JPG")


# ---------------------------------------------------------------- 1. same drive vs another drive

def test_one_drive_library_has_the_same_sets_as_before(drives, tmp_path):
    """Two folders on one external drive behave exactly like two folders on the Mac's disk did."""
    from joeylab import dupes

    vols, _ = drives
    local = tmp_path / "lib"
    _photos(local)
    on_drive = vols / "DriveA" / "Photos"
    _photos(on_drive)
    a = _library("a", local)
    a, _ = run_all("a")
    b = _library("b", on_drive)
    b, _ = run_all("b")
    assert b.execute("SELECT volume_uuid FROM roots").fetchone()[0] == "UUID-A"
    assert _sets(a, local) == _sets(b, on_drive)
    assert [k for k, _, _ in _sets(b, on_drive)] == ["exact", "exact"]
    assert dupes.duplicate_shot_ids(b) and not dupes.backup_copy_shot_ids(b)
    ex = dupes.extra_copies(b, [r[0] for r in b.execute("SELECT id FROM dup_sets")])
    assert len(ex["files"]) == 2


def test_copies_on_another_drive_of_one_library_are_backups_not_extras(drives):
    from joeylab import cleanup, dupes, library

    vols, _ = drives
    a, b = vols / "DriveA" / "Photos", vols / "DriveB" / "Mirror"
    for i in range(3):
        make_jpeg(a / "2024 Lisbon" / f"IMG_{i}.JPG", seed=i)
        b.mkdir(parents=True, exist_ok=True)
        shutil.copy2(a / "2024 Lisbon" / f"IMG_{i}.JPG", b / f"IMG_{i}.JPG")
    (a / "Old copy").mkdir()
    shutil.copy2(a / "2024 Lisbon" / "IMG_0.JPG", a / "Old copy" / "IMG_0.JPG")       # a duplicate on drive A
    _library("t", a, b)
    conn = _update("t")

    exact = conn.execute("SELECT id FROM dup_sets WHERE kind='exact'").fetchall()
    assert len(exact) == 1                       # two copies on A: one duplicate set; B's copy isn't in it
    members = {os.path.relpath(p, vols) for (p,) in conn.execute(
        "SELECT f.path FROM dup_members dm JOIN files f ON f.id=dm.file_id WHERE dm.set_id=?", (exact[0][0],))}
    assert members == {"DriveA/Photos/2024 Lisbon/IMG_0.JPG", "DriveA/Photos/Old copy/IMG_0.JPG"}
    assert conn.execute("SELECT COUNT(*) FROM dup_sets WHERE kind='backup'").fetchone()[0] == 3
    # A has one extra (the "Old copy"); B's copies are never extras, never rejected, never offered for the Trash.
    ex = dupes.extra_copies(conn, [r[0] for r in conn.execute("SELECT id FROM dup_sets")])
    extra_paths = [p for fs in ex["files"].values() for p, _ in fs]
    assert extra_paths == [str(a / "Old copy" / "IMG_0.JPG")]
    r = dupes.reject_extras(conn, [r[0] for r in conn.execute("SELECT id FROM dup_sets")], combine=False)
    assert r["rejected"] == 1
    assert {p for (p,) in conn.execute("SELECT f.path FROM decisions d JOIN files f ON f.shot_id=d.shot_id WHERE d.pick=-1")} \
        == {str(a / "Old copy" / "IMG_0.JPG")}
    assert dupes.extras(conn, [r[0] for r in conn.execute("SELECT id FROM dup_sets")]) == [str(a / "Old copy" / "IMG_0.JPG")]
    # One photo shown per picture: the others are B's backups (or A's extra), left out of culling.
    hidden = dupes.duplicate_shot_ids(conn) | dupes.backup_copy_shot_ids(conn)
    shown = {s for (s,) in conn.execute("SELECT id FROM shots WHERE status='present'")} - hidden
    assert sorted(library.shot(conn, s)["stem"] for s in shown) == ["IMG_0", "IMG_1", "IMG_2"]
    for s in dupes.backup_copy_shot_ids(conn):
        flags, sug = conn.execute("SELECT flags, suggestion FROM metrics WHERE shot_id=?", (s,)).fetchone()
        assert "backup-copy" in flags and "duplicate" not in flags and sug != "reject"
    # The Trash check: an identical copy on another drive never counts as "a copy stays".
    items = {i.path: i for i in cleanup.plan(conn, [str(b / "IMG_1.JPG"), str(a / "Old copy" / "IMG_0.JPG")])}
    assert items[str(b / "IMG_1.JPG")].category == cleanup.LAST
    assert items[str(a / "Old copy" / "IMG_0.JPG")].category == cleanup.IDENTICAL
    assert os.path.dirname(items[str(a / "Old copy" / "IMG_0.JPG")].stays).startswith(str(a))


def test_a_keep_rule_never_leaves_a_photo_with_no_copy_shown(drives):
    from joeylab import dupes

    vols, _ = drives
    a, b = vols / "DriveA" / "Photos", vols / "DriveB" / "Mirror"
    make_jpeg(a / "Shoot" / "IMG_0.JPG", seed=4)
    (a / "Desktop dump").mkdir(parents=True)
    shutil.copy2(a / "Shoot" / "IMG_0.JPG", a / "Desktop dump" / "IMG_0.JPG")
    b.mkdir(parents=True)
    shutil.copy2(a / "Shoot" / "IMG_0.JPG", b / "IMG_0.JPG")
    _library("t", a, b)
    conn = _update("t")
    for rule in (str(a / "Desktop dump"), str(a / "Shoot")):
        dupes.set_keep_rule(conn, rule)
        hidden = dupes.duplicate_shot_ids(conn) | dupes.backup_copy_shot_ids(conn)
        assert len({s for (s,) in conn.execute("SELECT id FROM shots")} - hidden) == 1
        shown_file = conn.execute("SELECT f.path FROM dup_members dm JOIN dup_sets ds ON ds.id=dm.set_id JOIN files f"
                                  " ON f.id=dm.file_id WHERE ds.kind='backup' AND dm.keep=1").fetchone()[0]
        kept_on_a = conn.execute("SELECT f.path FROM dup_members dm JOIN dup_sets ds ON ds.id=dm.set_id JOIN files f"
                                 " ON f.id=dm.file_id WHERE ds.kind='exact' AND dm.keep=1").fetchone()[0]
        assert shown_file in (kept_on_a, str(b / "IMG_0.JPG"))
        dupes.set_keep_rule(conn, rule, on=False)


# ---------------------------------------------------------------- 2. the index every library shares

def _two_libraries(vols):
    """x on DriveA, y on DriveB: three photos on both, one only on A, one only on B, a duplicate on A,
    and a re-saved version of one photo on B (the same picture, a different file)."""
    x, y = vols / "DriveA" / "Pictures", vols / "DriveB" / "Archive"
    for i in range(3):
        make_jpeg(x / "2024" / f"IMG_{i}.JPG", seed=10 + i)
        (y / "2024 copy").mkdir(parents=True, exist_ok=True)
        shutil.copy2(x / "2024" / f"IMG_{i}.JPG", y / "2024 copy" / f"IMG_{i}.JPG")
    make_jpeg(x / "2024" / "ONLY_A.JPG", seed=30)
    make_jpeg(y / "Other" / "ONLY_B.JPG", seed=31)
    (x / "dup").mkdir()
    shutil.copy2(x / "2024" / "IMG_0.JPG", x / "dup" / "IMG_0.JPG")
    make_jpeg(x / "2024" / "EDIT.JPG", seed=40)
    (y / "Exports").mkdir()
    Image.open(x / "2024" / "EDIT.JPG").save(y / "Exports" / "EDIT-web.jpg", "JPEG", quality=60)
    old = os.stat(x / "2024" / "EDIT.JPG").st_mtime
    os.utime(y / "Exports" / "EDIT-web.jpg", (old, old))
    _library("x", x)
    _library("y", y)
    return x, y


def test_backups_across_libraries_are_probable_until_read_whole(drives):
    from joeylab import copies, library

    vols, _ = drives
    x, _ = _two_libraries(vols)
    cx = _update("x")
    cy = _update("y")
    copies.sync(cx, "x")
    c = copies.counts(cx)
    # IMG_0 (both copies on A, the duplicate too), IMG_1 and IMG_2 have a copy on B; ONLY_A and EDIT
    # don't (the export on B is another file).
    assert c["known"] and c["elsewhere"] == 4 and c["checked"] == 0
    assert c["only"] == 2
    assert {r["stem"] for r in library.shots(cx, library.Filter(backup="only"))} == {"ONLY_A", "EDIT"}
    notes = copies.notes_for_paths(cx, "x", [str(x / "2024" / "IMG_1.JPG")])
    n = notes[str(x / "2024" / "IMG_1.JPG")]
    assert n["backup"] is None and n["unchecked"] == 1 and n["unchecked_on"] == "DriveB"      # dialogs name checked ones only

    # Only candidates are read: IMG_1 and IMG_2 on A (IMG_0's two copies there were hashed when x
    # looked for duplicates) and the three copies on B.
    assert copies.candidates() == 5
    r = copies.confirm(seconds=30, rate_mb=None)
    assert r["read"] == 5 and r["left"] == 0
    copies.sync(cx, "x")
    c = copies.counts(cx)
    assert c["elsewhere"] == 4 and c["checked"] == 4
    n = copies.notes_for_paths(cx, "x", [str(x / "2024" / "IMG_1.JPG")])[str(x / "2024" / "IMG_1.JPG")]
    assert n["backup"]["drive_name"] == "DriveB" and n["backup"]["library"] == "y"
    assert n["backup"]["rel"] == "Archive/2024 copy/IMG_1.JPG" and n["backup"]["connected"]
    d = copies.for_shot(cx, "x", _shot(cx, x / "2024" / "IMG_1.JPG"))
    assert d["all_backed_up"] and d["all_checked"] and d["files"][0]["backups"][0]["library"] == "y"
    d = copies.for_shot(cx, "x", _shot(cx, x / "2024" / "ONLY_A.JPG"))
    assert not d["all_backed_up"] and d["only_here"] == ["ONLY_A.JPG"]
    # The re-saved export on B is a version, never a backup of the original.
    d = copies.for_shot(cx, "x", _shot(cx, x / "2024" / "EDIT.JPG"))
    assert d["files"][0]["backups"] == []
    # The same photo, from y's side.
    copies.sync(cy, "y")
    assert copies.counts(cy)["elsewhere"] == 3 and copies.counts(cy)["only"] == 2
    rep = copies.report(cx, "x")
    drive = rep["drives"][0]
    assert drive["name"] == "DriveA" and drive["backed"] == 4 and drive["checked"] == 4 and drive["only"] == 2
    assert drive["where"][0]["name"] == "DriveB" and drive["where"][0]["photos"] == 4
    assert rep["holders"][0]["libraries"] == ["y"]


def test_refresh_is_incremental_and_follows_moves_and_files_gone(drives):
    from joeylab import copies, scanner

    vols, _ = drives
    x, _ = _two_libraries(vols)
    cx = _update("x")
    _update("y")
    again = copies.refresh(cx, "x")
    assert (again["added"], again["changed"], again["removed"]) == (0, 0, 0)
    ix = copies.connect()
    fid = cx.execute("SELECT id FROM files WHERE path=?", (str(x / "2024" / "IMG_2.JPG"),)).fetchone()[0]
    assert ix.execute("SELECT rel FROM copies WHERE library='x' AND file_id=?", (fid,)).fetchone()[0] == "Pictures/2024/IMG_2.JPG"

    # Renamed in Finder: the scan follows the folder (same inodes), and so does the index.
    os.rename(x / "2024", x / "2024 Lisbon")
    scanner.scan(cx)
    r = copies.refresh(cx, "x")
    assert r["changed"] >= 1 and r["removed"] == 0 and r["added"] == 0
    assert ix.execute("SELECT rel FROM copies WHERE library='x' AND file_id=?", (fid,)).fetchone()[0] == "Pictures/2024 Lisbon/IMG_2.JPG"

    # Gone (this test's own scratch file): it leaves the index at x's next update, and y's copy is
    # no longer backed up.
    from joeylab import db

    cy = db.connect("y")
    copies.sync(cy, "y", force=True)
    assert copies.counts(cy)["elsewhere"] == 3
    os.unlink(x / "2024 Lisbon" / "IMG_2.JPG")
    scanner.scan(cx)
    assert copies.refresh(cx, "x")["removed"] == 1
    copies.sync(cy, "y")
    assert copies.counts(cy)["elsewhere"] == 2
    ix.close()


def test_an_unplugged_drive_keeps_its_entries_with_when_it_was_last_seen(drives, tmp_path):
    from joeylab import copies, scanner

    vols, _ = drives
    x, _ = _two_libraries(vols)
    cx = _update("x")
    cy = _update("y")
    shutil.move(str(vols / "DriveB"), str(tmp_path / "unplugged"))      # DriveB unplugged
    rep = scanner.scan(cy)
    assert rep.roots_unavailable and not rep.missing
    r = copies.refresh(cy, "y")
    assert r["removed"] == 0
    copies.sync(cx, "x")
    assert copies.counts(cx)["elsewhere"] == 4
    n = copies.for_shot(cx, "x", _shot(cx, x / "2024" / "IMG_1.JPG"))["files"][0]["backups"][0]
    assert n["drive_name"] == "DriveB" and not n["connected"] and n["last_seen"]
    states = copies.drive_states()
    assert states["UUID-B"]["connected"] is False and states["UUID-A"]["connected"] is True
    # Nothing on an unplugged drive can be read: those candidates wait.
    assert copies.confirm(seconds=10, rate_mb=None)["left"] == 3


def test_a_catalog_is_written_only_when_its_own_backups_change(drives):
    """Every write to the index changes its generation; a library whose backups didn't change isn't
    written to (so its pages' caches stay warm while other libraries' copies are checked)."""
    from joeylab import copies

    vols, _ = drives
    _two_libraries(vols)
    make_jpeg(vols / "DriveC" / "Other" / "SOLO.JPG", seed=77)
    _library("z", vols / "DriveC" / "Other")
    cx = _update("x")
    _update("y")
    cz = _update("z")
    copies.sync(cz, "z", force=True)
    writes = cz.total_changes
    assert copies.confirm(seconds=30, rate_mb=None)["read"]                # x's and y's candidates: the generation moves on
    assert copies.sync(cz, "z") is False and cz.total_changes == writes
    assert copies.sync(cx, "x") is True and copies.counts(cx)["checked"] == 4


def test_a_disk_mounted_outside_volumes_is_still_known_while_unplugged(drives, tmp_path, monkeypatch):
    """A disk image mounted in some folder can't be asked who it is once it's gone: the library
    remembers the drive each of its folders was on when last seen."""
    from joeylab import copies, scanner, volumes

    vols, _ = drives
    x, _y = _two_libraries(vols)
    image = tmp_path / "mounted" / "Photos"
    make_jpeg(image / "IMG_9.JPG", seed=90)
    shutil.copy2(image / "IMG_9.JPG", x / "2024" / "IMG_9.JPG")
    real = volumes.drive_of
    monkeypatch.setattr(volumes, "drive_of", lambda p: {"key": "UUID-IMG", "name": "Card image", "mount": str(tmp_path / "mounted"),
                                                       "rel": os.path.relpath(p, tmp_path / "mounted")}
                        if str(p).startswith(str(tmp_path / "mounted")) else real(p))
    cz = _library("z", image)
    cz = _update("z")
    cx = _update("x")
    assert copies.for_shot(cx, "x", _shot(cx, x / "2024" / "IMG_9.JPG"))["files"][0]["backups"][0]["drive_name"] == "Card image"
    shutil.move(str(tmp_path / "mounted"), str(tmp_path / "away"))          # unplugged
    scanner.scan(cz)
    assert volumes.root_drives(cz)[1] == {"key": "UUID-IMG", "name": "Card image", "base": "Photos"}
    assert copies.refresh(cz, "z")["removed"] == 0
    rep = copies.report(cz, "z")
    assert [d["name"] for d in rep["drives"]] == ["Card image"] and not rep["drives"][0]["connected"]


def test_a_file_changed_after_indexing_is_not_read_and_hashes_match_the_scans(drives):
    from joeylab import copies, hashing

    vols, _ = drives
    _, y = _two_libraries(vols)
    _update("x")
    _update("y")
    p = y / "2024 copy" / "IMG_1.JPG"
    st = os.stat(p)
    os.utime(p, (st.st_atime, st.st_mtime + 5))           # written again since: not the file indexed
    copies.confirm(seconds=30, rate_mb=None)
    ix = copies.connect()
    rows = {r["rel"]: r["full_hash"] for r in ix.execute("SELECT rel, full_hash FROM copies")}
    assert rows["Archive/2024 copy/IMG_1.JPG"] is None
    assert rows["Archive/2024 copy/IMG_2.JPG"] == hashing.full_hash(str(y / "2024 copy" / "IMG_2.JPG"))
    ix.close()


def test_library_renamed_or_removed_and_a_damaged_index(drives):
    from joeylab import config, copies, libraries

    vols, _ = drives
    _two_libraries(vols)
    _update("x")
    cy = _update("y")
    cy.close()
    libraries.rename("y", "archive")
    ix = copies.connect()
    assert {r[0] for r in ix.execute("SELECT DISTINCT library FROM copies")} == {"x", "archive"}
    ix.close()
    libraries.remove("archive", "archive")
    ix = copies.connect()
    assert {r[0] for r in ix.execute("SELECT DISTINCT library FROM copies")} == {"x"}
    ix.close()
    copies.path().write_bytes(b"not a database at all" * 100)
    for side in ("-wal", "-shm"):
        try:
            os.unlink(str(copies.path()) + side)
        except OSError:
            pass
    ix = copies.connect()
    assert ix.execute("SELECT COUNT(*) FROM copies").fetchone()[0] == 0
    ix.close()
    assert any(p.name.startswith("copies.db.damaged-") for p in config.HOME.iterdir())


# ---------------------------------------------------------------- 3. what the owner sees

def _wait(client, url: str) -> dict:
    import time

    for _ in range(200):
        job = client.get(url).json()
        if job["state"] not in ("checking", "moving"):
            return job
        time.sleep(0.05)
    raise AssertionError("the check didn't finish")


def test_web_and_mcp_say_where_the_backups_are(drives):
    from fastapi.testclient import TestClient

    from joeylab import copies, library, mcp
    from joeylab.web.server import create_app

    vols, _ = drives
    x, _ = _two_libraries(vols)
    cx = _update("x")
    _update("y")
    copies.confirm(seconds=30, rate_mb=None)
    c = TestClient(create_app())
    o = c.get("/api/p/x/overview").json()["backups"]
    assert o["known"] and (o["elsewhere"], o["checked"], o["only"]) == (4, 4, 2)
    assert {r["stem"] for r in c.get("/api/p/x/shots", params={"backup": "only"}).json()["rows"]} == {"ONLY_A", "EDIT"}
    assert {r["stem"] for r in c.get("/api/p/x/shots", params={"backup": "checked", "all": 1}).json()["rows"]} == \
        {"IMG_0", "IMG_1", "IMG_2"}
    img1, only_a = _shot(cx, x / "2024" / "IMG_1.JPG"), _shot(cx, x / "2024" / "ONLY_A.JPG")
    d = c.get(f"/api/p/x/shot/{img1}").json()["backups"]
    b = d["files"][0]["backups"][0]
    assert d["all_backed_up"] and b["drive_name"] == "DriveB" and b["library"] == "y" and b["checked"] and b["connected"]
    assert c.get(f"/api/p/x/shot/{only_a}").json()["backups"]["only_here"] == ["ONLY_A.JPG"]
    rep = c.get("/api/p/x/copies").json()
    assert rep["drives"][0]["only"] == 2 and rep["drives"][0]["where"][0]["name"] == "DriveB"
    assert c.get("/api/p/x/dupes/groups").json()["backups"]["elsewhere"] == 4

    # The Trash dialog says, per file, where its checked backup is or that it's the only copy anywhere.
    library.decide(cx, [{"id": img1, "pick": -1}, {"id": only_a, "pick": -1}])
    job = c.post("/api/p/x/trash/plan", json={"what": "rejects", "ids": [img1, only_a]}).json()
    job = _wait(c, f"/api/p/x/trash/{job['token']}")
    files = {os.path.basename(f["path"]): f for f in job["backups"]["files"]}
    assert files["IMG_1.JPG"]["backup"]["drive_name"] == "DriveB" and not files["IMG_1.JPG"]["only"]
    assert files["ONLY_A.JPG"]["only"] and files["ONLY_A.JPG"]["backup"] is None
    assert job["backups"]["summary"] == {"backed_up": 1, "only_copy": 1, "unchecked": 0, "files": 2}
    assert job["go"]["copies"] == 0              # information only: a backup never makes a file go

    out = mcp.call("photo_details", {"id": img1, "library": "x"}, None, False)
    assert out["backups"]["files"][0]["backed_up_on"][0]["drive"] == "DriveB"
    assert out["backups"]["files"][0]["backed_up_on"][0]["checked_byte_for_byte"]
    out = mcp.call("photo_details", {"id": only_a, "library": "x"}, None, False)
    assert out["backups"]["files"][0]["note"].startswith("Only copy")


def test_the_raw_trash_dialog_says_which_raws_are_backed_up(drives):
    from fastapi.testclient import TestClient

    from joeylab import copies, raws
    from joeylab.web.server import create_app

    vols, _ = drives
    x, y = vols / "DriveA" / "Shoot", vols / "DriveB" / "RAW backup"
    for i in (1, 2):
        make_jpeg(x / f"IMG_{i}.JPG", seed=i)
        (x / f"IMG_{i}.NEF").write_bytes(os.urandom(9000))
        os.utime(x / f"IMG_{i}.NEF", (os.stat(x / f"IMG_{i}.JPG").st_mtime,) * 2)
    y.mkdir(parents=True)
    shutil.copy2(x / "IMG_1.NEF", y / "IMG_1.NEF")
    _library("x", x)
    _library("y", y)
    cx = _update("x")
    _update("y")
    copies.confirm(seconds=30, rate_mb=None)
    ids = [r[0] for r in cx.execute("SELECT id FROM shots ORDER BY stem")]
    raws.choose(cx, ids, raws.REMOVE)
    c = TestClient(create_app())
    job = c.post("/api/p/x/raws/trash/plan", json={"ids": ids}).json()
    job = _wait(c, f"/api/p/x/raws/trash/{job['token']}")
    items = {os.path.basename(i["raw"]): i for i in job["items"]}
    assert items["IMG_1.NEF"]["backup"]["drive_name"] == "DriveB" and not items["IMG_1.NEF"]["only"]
    assert items["IMG_2.NEF"]["only"] and items["IMG_2.NEF"]["backup"] is None
    assert job["go"]["raws"] == 2                # whether a RAW goes is your choice alone
