"""Setting up a library: checking a folder before it becomes one, library names, renaming and
removing a library (JoeyLab's records only, never the photo folders), and files that can't be
read not counting as photos."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import add_root, jpeg_at, make_jpeg, run_all
from test_safety_and_library import snapshot


def _client():
    from joeylab.web.jobs import JobManager
    from joeylab.web.server import create_app

    jobs = JobManager(watch_minutes=0, faces=False, ai_layer=False)
    return TestClient(create_app(jobs=jobs)), jobs


def _wait(c, p, until=lambda j: j["state"] != "running", tries=300):
    for _ in range(tries):
        j = c.get(f"/api/p/{p}/jobs/current").json()
        if j["job"] and until(j["job"]):
            return j
        time.sleep(0.05)
    raise AssertionError(f"the update of {p} didn't get there: {j}")


def _shoot(lib):
    """A burst (two near-identical frames a second apart), a photo on its own, and an identical copy."""
    jpeg_at(lib / "Shoot" / "IMG_0001.JPG", 1, "2024:05:01 10:00:00")
    jpeg_at(lib / "Shoot" / "IMG_0002.JPG", 1, "2024:05:01 10:00:01")
    jpeg_at(lib / "Shoot" / "IMG_0003.JPG", 7, "2024:05:01 12:00:00")
    (lib / "Backup").mkdir(parents=True)
    shutil.copy2(lib / "Shoot" / "IMG_0003.JPG", lib / "Backup" / "IMG_0003.JPG")


# ---------------------------------------------------------------- names

def test_library_names_become_slugs():
    from joeylab import libraries

    assert libraries.slug("Smith Wedding 2024") == "smith-wedding-2024"
    assert libraries.slug("Café trip!") == "cafe-trip"
    assert libraries.slug("  ..x__y  ") == "x_y"
    assert libraries.slug("Lisbon / Porto, 2023") == "lisbon-porto-2023"
    assert libraries.slug("!!!") == "" and libraries.slug("") == ""
    long = libraries.slug("a" * 63 + " b")
    assert len(long) <= 64 and not long.endswith("-")
    for typed in ("Smith Wedding 2024", "Café trip!", "a" * 100):
        from joeylab import config

        config.project_dir(libraries.slug(typed))          # always a valid name
    assert "letter or digit" in libraries.name_problem("")
    assert "web addresses" in libraries.name_problem("api")


def test_new_library_takes_a_slug_and_says_why_a_name_or_folder_wont_do(env):
    tmp, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    c, _ = _client()
    r = c.post("/api/projects", json={"name": "Smith Wedding", "path": str(lib), "purpose": "shoot"})
    assert r.status_code == 200 and r.json()["name"] == "smith-wedding" and r.json()["check"]["photos"] == 1
    _wait(c, "smith-wedding")
    again = c.post("/api/projects", json={"name": "smith wedding", "path": str(lib)})
    assert again.status_code == 409 and "already a library" in again.json()["error"]
    twice = c.post("/api/projects", json={"name": "another", "path": str(lib)})   # a folder is in one library only
    assert twice.status_code == 400 and "“smith-wedding” already includes" in twice.json()["error"]
    other = tmp / "Holiday 2025"
    make_jpeg(other / "IMG_2.JPG", seed=2)
    no_name = c.post("/api/projects", json={"name": "", "path": str(other)})      # named after the folder
    assert no_name.status_code == 200 and no_name.json()["name"] == "holiday-2025"
    _wait(c, "holiday-2025")
    bad = c.post("/api/projects", json={"name": "x", "path": str(tmp / "nope")})
    assert bad.status_code == 400 and "There's no folder at" in bad.json()["error"]
    assert "project" not in bad.json()["error"].lower()
    assert c.get("/api/p/nope/overview").json()["error"] == "There's no library called 'nope'."


# ---------------------------------------------------------------- checking a folder

def test_folder_check_gives_plain_reasons(env, tmp_path):
    from joeylab import config, libraries

    _, lib = env
    assert "full path" in libraries.check_folder("Pictures/2024")["reason"]
    assert "Browse" in libraries.check_folder("")["reason"]
    assert "There's no folder at" in libraries.check_folder(str(tmp_path / "nope"))["reason"]
    assert "isn't connected" in libraries.check_folder("/Volumes/No Such Drive 123/DCIM")["reason"]
    (tmp_path / "a.txt").write_text("x")
    assert "is a file, not a folder" in libraries.check_folder(str(tmp_path / "a.txt"))["reason"]
    config.HOME.mkdir(parents=True, exist_ok=True)
    assert "JoeyLab's own folder" in libraries.check_folder(str(config.HOME))["reason"]

    empty = tmp_path / "Empty"
    (empty / "sub").mkdir(parents=True)
    (empty / "notes.txt").write_text("not a photo")
    (empty / ".hidden.jpg").write_bytes(b"x")
    r = libraries.check_folder(str(empty))
    assert not r["ok"] and r["empty"] and r["reason"].startswith(f"No photos in {empty}")

    shoot = tmp_path / "Shoot"
    make_jpeg(shoot / "DSCF0001.JPG", seed=1)
    (shoot / "DSCF0001.RAF").write_bytes(b"raw")              # a RAW + JPEG pair is one photo
    make_jpeg(shoot / "day 2" / "DSCF0002.JPG", seed=2)
    (shoot / "clip.MOV").write_bytes(b"mov")
    r = libraries.check_folder(str(shoot))
    assert r["ok"] and (r["photos"], r["videos"]) == (2, 1) and not r["partial"]
    assert libraries.describe(r) == "2 photos and 1 video"
    assert libraries.check_folder(str(shoot), enough=1)["partial"]

    if os.geteuid() != 0:
        locked = tmp_path / "Locked"
        locked.mkdir()
        os.chmod(locked, 0)
        try:
            assert "doesn't let JoeyLab read" in libraries.check_folder(str(locked))["reason"]
        finally:
            os.chmod(locked, 0o755)


def test_empty_folder_is_refused_unless_asked_and_its_library_is_empty(env, tmp_path):
    empty = tmp_path / "Card import"
    empty.mkdir()
    c, _ = _client()
    r = c.post("/api/projects", json={"name": "card", "path": str(empty)})
    assert r.status_code == 400 and r.json()["error"].startswith("No photos in")
    assert c.get("/api/fs/check", params={"path": str(empty)}).json()["empty"] is True
    r = c.post("/api/projects", json={"name": "card", "path": str(empty), "empty_ok": True})
    assert r.status_code == 200
    _wait(c, "card")
    o = c.get("/api/p/card/overview").json()
    assert o["counts"]["photos"] == 0 and o["counts"]["undecided_stacks"] == 0
    # Settings › Folders checks the same way.
    other = tmp_path / "Other"
    other.mkdir()
    assert c.post("/api/p/card/roots", json={"path": str(other), "scan": False}).status_code == 400


def test_unreadable_files_are_not_counted_as_photos(env):
    tmp, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    (lib / "broken.jpg").write_bytes(b"\xff\xd8\xff not really a JPEG")
    old = time.time() - 60
    os.utime(lib / "broken.jpg", (old, old))
    add_root(lib)
    conn, _ = run_all()
    c, _ = _client()
    o = c.get("/api/p/t/overview").json()["counts"]
    assert (o["photos"], o["failed"], o["undecided"], o["undecided_stacks"]) == (1, 1, 1, 1)
    assert c.get("/api/projects").json()[0]["photos"] == 1
    assert c.get("/api/p/t/shots").json()["total"] == 1
    assert sum(len(s["ids"]) for s in c.get("/api/p/t/stacks").json()["stacks"]) == 1


# ---------------------------------------------------------------- renaming and removing

def _own_records(name):
    """A trash log, a move log and a backup, as JoeyLab writes them for a library."""
    from joeylab import backup, config

    (config.HOME / "trash-log").mkdir(parents=True, exist_ok=True)
    (config.HOME / "trash-log" / f"{name}-files-20261016-120000.tsv").write_text("file\tresult\n")
    (config.HOME / "move-log").mkdir(parents=True, exist_ok=True)
    (config.HOME / "move-log" / f"{name}-20261016-120000-abc123.jsonl").write_text(json.dumps({"id": "x"}) + "\n")
    backup.backup(name, force=True)


def test_rename_library_moves_only_its_own_records(env):
    from joeylab import config, db, libraries, library, locks

    tmp, lib = env
    _shoot(lib)
    add_root(lib)
    conn, _ = run_all()
    sid = conn.execute("SELECT id FROM shots ORDER BY id").fetchone()[0]
    library.decide(conn, [{"id": sid, "pick": 1, "rating": 4}])
    conn.close()
    _own_records("t")
    (config.HOME / "projects" / "other").mkdir(parents=True)
    (config.HOME / "projects" / "other" / "catalog.db").write_bytes(b"")
    before = snapshot(lib)

    with pytest.raises(ValueError, match="already a library"):
        libraries.rename("t", "Other")
    res = {}
    with locks.project_lock("t"):                  # an update is running (here, in another thread)
        t = threading.Thread(target=lambda: res.update(e=_try(libraries.rename, "t", "x")))
        t.start()
        t.join()
    assert isinstance(res["e"], locks.Busy) and (config.HOME / "projects" / "t").exists()

    assert libraries.rename("t", "Trip 2024") == "trip-2024"
    assert not (config.HOME / "projects" / "t").exists()
    moved = db.connect("trip-2024", create=False)
    assert moved.execute("SELECT pick, rating FROM decisions WHERE shot_id=?", (sid,)).fetchone()[:] == (1, 4)
    assert (config.HOME / "trash-log" / "trip-2024-files-20261016-120000.tsv").exists()
    assert (config.HOME / "move-log" / "trip-2024-20261016-120000-abc123.jsonl").exists()
    assert list((config.HOME / "backups" / "trip-2024").glob("catalog-*.db"))
    assert snapshot(lib) == before                                    # the photo folders: not a byte, not a timestamp

    # A move that stopped half way has to be finished (by an update) first.
    (config.HOME / "move-log" / "trip-2024-20261016-130000-def456.open.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="stopped half way"):
        libraries.rename("trip-2024", "trip")


def _try(fn, *args):
    try:
        fn(*args)
    except Exception as e:  # handed back to the test
        return e
    return None


def test_remove_library_keeps_a_backup_and_the_folders(env):
    from joeylab import config, libraries, library

    tmp, lib = env
    _shoot(lib)
    add_root(lib)
    conn, _ = run_all()
    sid = conn.execute("SELECT id FROM shots ORDER BY id").fetchone()[0]
    library.decide(conn, [{"id": sid, "pick": -1}])
    conn.close()
    _own_records("t")
    before = snapshot(lib)

    with pytest.raises(ValueError, match="Type the library's name"):
        libraries.remove("t", "T")
    assert (config.HOME / "projects" / "t" / "catalog.db").exists()
    out = libraries.remove("t", "t")
    assert not (config.HOME / "projects" / "t").exists() and "t" not in config.list_projects()
    import sqlite3

    kept = sqlite3.connect(out["catalog"])
    assert kept.execute("SELECT pick FROM decisions WHERE shot_id=?", (sid,)).fetchone()[0] == -1
    assert os.path.dirname(out["catalog"]) == out["kept"] and out["kept"].startswith(str(config.HOME / "backups" / "removed"))
    assert {p.name for p in (config.HOME / "backups" / "removed").iterdir()} == {os.path.basename(out["kept"])}
    assert sorted(os.listdir(os.path.join(out["kept"], "logs"))) == ["t-20261016-120000-abc123.jsonl",
                                                                     "t-files-20261016-120000.tsv"]
    assert os.listdir(os.path.join(out["kept"], "backups"))
    assert snapshot(lib) == before
    with pytest.raises(FileNotFoundError):
        libraries.remove("t", "t")


def test_rename_and_remove_from_the_app(env):
    from joeylab import config

    tmp, lib = env
    _shoot(lib)
    add_root(lib)
    run_all()
    before = snapshot(lib)
    c, jobs = _client()
    jobs.versions["t"] = 3
    r = c.post("/api/projects/t/rename", json={"to": "Lisbon Trip"})
    assert r.status_code == 200 and r.json()["name"] == "lisbon-trip"
    assert jobs.versions == {"lisbon-trip": 3}
    assert c.get("/api/p/lisbon-trip/overview").status_code == 200
    assert c.post("/api/projects/nope/rename", json={"to": "x"}).status_code == 404
    r = c.post("/api/projects/lisbon-trip/remove", json={"confirm": "lisbon"})
    assert r.status_code == 400 and "Type the library's name" in r.json()["error"]
    r = c.post("/api/projects/lisbon-trip/remove", json={"confirm": "lisbon-trip"})
    assert r.status_code == 200 and os.path.exists(r.json()["catalog"])
    assert c.get("/api/projects").json() == [] and not (config.HOME / "projects" / "lisbon-trip").exists()
    assert snapshot(lib) == before
    # Someone else on the tailnet can't.
    from joeylab.web.server import create_app

    guest = TestClient(create_app(owner="me@example.com"))
    hdrs = {"Tailscale-User-Login": "guest@example.com", "X-Forwarded-For": "100.64.0.9"}
    assert guest.post("/api/projects/t/remove", json={"confirm": "t"}, headers=hdrs).status_code in (401, 403)


def test_cli_library_commands(env):
    from typer.testing import CliRunner

    from joeylab import cli, config

    tmp, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    run_all()
    runner = CliRunner()

    def out(*args, input=None):
        r = runner.invoke(cli.app, list(args), input=input)
        return r.exit_code, " ".join(r.output.split())

    assert "t" in out("library", "list")[1]
    code, text = out("library", "rename", "t", "My Shoot")
    assert code == 0 and "'my-shoot'" in text and "weren't touched" in text
    assert out("status", "--library", "my-shoot")[0] == 0
    assert out("status", "--project", "my-shoot")[0] == 0               # the old name still works
    code, text = out("library", "remove", "my-shoot", input="nope\n")
    assert code == 1 and "Nothing was removed" in text and "my-shoot" in config.list_projects()
    code, text = out("library", "remove", "my-shoot", input="my-shoot\n")
    assert code == 0 and "backup of its catalog" in text and not config.list_projects()
    code, text = out("status", "-p", "my-shoot")
    assert code == 1 and "no library called" in text
