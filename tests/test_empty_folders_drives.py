"""Empty folders on an external drive: when a different drive turns up under the library drive's name,
its folders aren't listed as the library's, and nothing on it is moved to the Trash, also when the
drives are swapped between the check and the move."""

from __future__ import annotations

import os
import shutil

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import make_jpeg, run_all


@pytest.fixture
def fake_trash(tmp_path, monkeypatch):
    """Stands in for the real Trash (tests never touch the user's)."""
    from joeylab import cleanup

    bin_ = tmp_path / "trash"
    bin_.mkdir()

    def move(path):
        dest = bin_ / f"{len(list(bin_.iterdir()))}-{os.path.basename(path)}"
        shutil.move(path, dest)
        return str(dest)

    monkeypatch.setattr(cleanup, "move_to_trash", move)
    return bin_


@pytest.fixture
def drives(env, tmp_path, monkeypatch):
    """A fake /Volumes where each drive's identity is set by the test: {drive name: volume uuid}."""
    from joeylab import volumes

    vols = tmp_path / "Volumes"
    uuids: dict[str, str] = {}
    monkeypatch.setattr(volumes, "VOLUMES", vols)
    monkeypatch.setattr(volumes, "_info", lambda mount: {"uuid": uuids.get(os.path.basename(mount)),
                                                        "name": os.path.basename(mount)})
    volumes._cache.clear()
    return vols, uuids


def _library_on_x9(vols, uuids):
    """A library folder on the drive X9 (U1), remembered by a scan."""
    from joeylab import db, scanner

    photos = vols / "X9" / "Photos"
    make_jpeg(photos / "IMG_0.JPG", seed=0)
    uuids["X9"] = "U1"
    scanner.add_root(db.connect("t"), photos)
    conn, _ = run_all()
    assert conn.execute("SELECT volume_uuid FROM roots").fetchone()[0] == "U1"
    return conn, photos


def _stranger_as_x9(vols, uuids, tmp_path):
    """Ours unplugged; another drive called X9 arrives, with a Photos folder holding an empty folder."""
    from joeylab import volumes

    shutil.move(str(vols / "X9"), str(tmp_path / "unplugged"))
    (vols / "X9" / "Photos" / "Theirs").mkdir(parents=True)
    uuids["X9"] = "U2"
    volumes._cache.clear()


def test_a_stranger_drive_with_the_same_name_isnt_looked_in(env, tmp_path, drives, fake_trash):
    from joeylab import cleanup
    from joeylab.cli import app
    from joeylab.web.server import create_app

    vols, uuids = drives
    conn, photos = _library_on_x9(vols, uuids)
    _stranger_as_x9(vols, uuids, tmp_path)

    r = CliRunner().invoke(app, ["empty-folders", "-p", "t", "--trash"], input="1\n")
    assert "Theirs" not in r.output and "No empty folders" in r.output
    assert (photos / "Theirs").is_dir() and not list(fake_trash.iterdir())

    c = TestClient(create_app())
    assert c.get("/api/p/t/health/empty-folders").json()["folders"] == []
    tok = c.post("/api/p/t/trash/plan", json={"what": "folders"}).json()["token"]
    for _ in range(200):
        j = c.get(f"/api/p/t/trash/{tok}").json()
        if j["state"] != "checking":
            break
    assert j["state"] == "ready" and j["folders"] == []
    assert (photos / "Theirs").is_dir()
    assert cleanup.folder_roots(conn) == ([], [str(photos)])


def test_drives_swapped_between_the_check_and_the_move(env, tmp_path, drives, fake_trash):
    from joeylab import cleanup, volumes

    vols, uuids = drives
    conn, photos = _library_on_x9(vols, uuids)
    (photos / "Ours").mkdir()
    roots = [r[0] for r in conn.execute("SELECT path FROM roots")]
    found = cleanup.empty_folders(roots)
    assert [os.path.basename(f.path) for f in found] == ["Ours"]
    # Before the count is typed, ours is unplugged and another X9 with an empty Photos/Ours arrives.
    shutil.move(str(vols / "X9"), str(tmp_path / "unplugged"))
    (vols / "X9" / "Photos" / "Ours").mkdir(parents=True)
    uuids["X9"] = "U2"
    volumes._cache.clear()
    cleanup.trash_folders(found, roots, "t-folders")
    assert (vols / "X9" / "Photos" / "Ours").is_dir() and not list(fake_trash.iterdir())
    assert "drive" in found[0].result
