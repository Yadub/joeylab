"""Setting up: pinned model downloads, the face recognition opt-in, platforms and the demo library."""

from __future__ import annotations

import hashlib
import io

import pytest


def _serve(monkeypatch, data: bytes, calls: list):
    import urllib.request

    def urlopen(url, timeout=None):
        calls.append(url)
        return io.BytesIO(data)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def test_downloads_are_checked_and_a_different_file_is_refused(env, monkeypatch):
    from joeylab import config, downloads

    tmp, _ = env
    calls: list = []
    _serve(monkeypatch, b"model weights", calls)
    dest = tmp / "models" / "m.bin"
    good = hashlib.sha256(b"model weights").hexdigest()

    with pytest.raises(config.ModelMissing):                  # offline (as in every test): no request at all
        downloads.fetch("https://example.com/m.bin", good, dest)
    assert not calls and not dest.exists()

    monkeypatch.delenv("JOEYLAB_OFFLINE")
    with pytest.raises(downloads.Mismatch, match="isn't the expected file"):
        downloads.fetch("https://example.com/m.bin", "0" * 64, dest)
    assert not dest.exists() and not list(dest.parent.iterdir())     # nothing kept, not even the part file

    downloads.fetch("https://example.com/m.bin", good, dest)
    assert dest.read_bytes() == b"model weights"


def test_model_addresses_are_pinned():
    import re

    from joeylab import ai, config, faces

    pins = [(config.FACE_MODEL_URL, config.FACE_MODEL_SHA256), (faces.PACK_URL, faces.PACK_SHA256),
            (ai.AESTHETIC_URL, ai.AESTHETIC_SHA256)] + [(url, sha) for _, url, sha in faces.OPENCV_FILES.values()]
    for url, sha in pins:
        assert url.startswith("https://") and "latest" not in url and "/main/" not in url
        assert re.fullmatch(r"[0-9a-f]{64}", sha)
    # OpenCV's models from OpenCV's own copies, each at a fixed commit (not the Git LFS pointer files).
    for name, (dest, url, _) in faces.OPENCV_FILES.items():
        assert re.fullmatch(r"https://huggingface\.co/opencv/[a-z_]+/resolve/[0-9a-f]{40}/" + re.escape(name), url)
        assert dest.name == name
    assert {k for k in faces.MODELS} == {"insightface", "opencv"}
    for m in faces.MODELS.values():
        assert m.licence and m.good and m.download_mb > 0 and all(u.startswith("https://") for _, u in m.links)
    assert "non-commercial" in faces.MODELS["insightface"].label and "paid work" in faces.MODELS["opencv"].label


def _face_files(monkeypatch, models):
    """Point both sets of face models at a test's own folder."""
    from joeylab import faces

    monkeypatch.setattr(faces, "PACK_FILES", {"det_500m.onnx": models / "det.onnx", "w600k_mbf.onnx": models / "rec.onnx"})
    monkeypatch.setattr(faces, "OPENCV_FILES", {k: (models / k, url, sha) for k, (_, url, sha) in faces.OPENCV_FILES.items()})


def test_face_recognition_is_off_until_its_models_are_chosen(env, monkeypatch):
    import urllib.request

    from fastapi.testclient import TestClient

    from conftest import add_root, make_jpeg
    from joeylab import db, faces, mcp, pipeline, prefs
    from joeylab.web.server import create_app

    tmp, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    _face_files(monkeypatch, tmp / "models")
    assert not faces.enabled() and not faces.chosen() and faces.current() is None

    # A new install, with downloads allowed: the update says how to turn it on, once, finds no faces
    # and downloads nothing.
    monkeypatch.delenv("JOEYLAB_OFFLINE")
    asked: list = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: asked.append(url) or io.BytesIO(b"x"))
    logs = []

    class Rep(pipeline.Reporter):
        def log(self, msg, level="info"):
            logs.append(msg)

    conn = db.connect("t")
    out = pipeline.run_all(conn, "t", Rep(), faces=True, ai_layer=False)
    assert out["faces"] == 0 and sum("faces enable" in m for m in logs) == 1
    assert not [u for u in asked if "opencv/face_" in u or "insightface" in u]

    # The People page, the per-library switch and an assistant all say it's off.
    c = TestClient(create_app())
    people = c.get("/api/p/t/people").json()
    assert people["enabled"] is False and people["recognition"] is False and people["model"] is None
    assert c.post("/api/p/t/faces/enable", json={"enabled": True}).status_code == 409
    got = mcp.call("people", {"library": "t"}, "t", False)
    assert got["people"] == [] and "faces enable" in got["note"]

    # The app offers both sets, nothing chosen: each says what it's good at, its licence and its size.
    owned = TestClient(create_app(owner="me@example.com"))
    st = owned.get("/api/faces").json()
    assert st["enabled"] is False and st["model"] is None and st["switch_note"]
    assert [x["key"] for x in st["choices"]] == ["insightface", "opencv"]
    for x in st["choices"]:
        assert x["good"] and x["licence"] and x["links"] and x["download_mb"] > 0 and not x["downloaded"]

    # Turning it on needs a choice and the licence agreed, and only the owner can.
    guest = {"Tailscale-User-Login": "guest@example.com", "X-Forwarded-For": "100.64.0.9"}
    assert owned.post("/api/faces", json={"enabled": True, "accept_licence": True}).status_code == 400
    assert owned.post("/api/faces", json={"enabled": True, "model": "nope", "accept_licence": True}).status_code == 400
    assert owned.post("/api/faces", json={"enabled": True, "model": "opencv"}).status_code == 400
    assert owned.post("/api/faces", json={"enabled": True, "model": "opencv", "accept_licence": True},
                      headers=guest).status_code == 403
    assert not faces.enabled() and "faces" not in prefs.load()
    r = owned.post("/api/faces", json={"enabled": True, "model": "opencv", "accept_licence": True}).json()
    assert r["enabled"] is True and r["model"] == "opencv" and r["licence_url"] == faces.YUNET_LICENCE_URL
    saved = prefs.load()["faces"]
    assert saved == {**saved, "enabled": True, "model": "opencv", "via": "web"}
    assert (prefs.path().stat().st_mode & 0o777) == 0o600

    # The next update downloads the chosen models, and only those (here the stand-in download
    # isn't the expected file, so it's refused and faces wait).
    logs.clear()
    pipeline.run_all(conn, "t", Rep(), faces=True, ai_layer=False)
    got = [u for u in asked if "opencv/face_" in u or "insightface" in u]
    assert got and set(got) <= {url for _, url, _ in faces.OPENCV_FILES.values()}
    assert any("isn't the expected file" in m for m in logs)
    assert not (tmp / "models").exists() or not list((tmp / "models").iterdir())
    # With downloads off, the chosen models aren't fetched either.
    monkeypatch.setenv("JOEYLAB_OFFLINE", "1")
    asked.clear()
    logs.clear()
    pipeline.run_all(conn, "t", Rep(), faces=True, ai_layer=False)
    assert not asked and any("downloads are off" in m for m in logs)

    # Turned off: the choice is kept for next time, and the update no longer suggests turning it on.
    owned.post("/api/faces", json={"enabled": False})
    assert not faces.enabled() and faces.current() == "opencv"
    logs.clear()
    pipeline.run_all(conn, "t", Rep(), faces=True, ai_layer=False)
    assert not any("faces enable" in m for m in logs)


def test_the_command_line_asks_which_models_and_takes_a_flag(env, monkeypatch):
    from typer.testing import CliRunner

    from joeylab import cli, faces, prefs

    tmp, _ = env
    _face_files(monkeypatch, tmp / "models")
    runner = CliRunner()
    r = runner.invoke(cli.app, ["faces", "status"])
    assert "off" in r.output and "aren't chosen" in r.output

    # Both choices are shown, nothing is chosen for you, and no means no.
    r = runner.invoke(cli.app, ["faces", "enable"], input="2\nn\n")
    assert r.exit_code == 1 and not faces.enabled() and faces.current() is None
    for m in faces.MODELS.values():
        assert m.label in r.output and m.links[0][1] in r.output
    # A script has to say which.
    r = runner.invoke(cli.app, ["faces", "enable", "--yes"])
    assert r.exit_code == 2 and "--model" in r.output and not faces.enabled()
    assert runner.invoke(cli.app, ["faces", "enable", "--model", "nope", "--yes"]).exit_code == 2

    r = runner.invoke(cli.app, ["faces", "enable"], input="1\ny\n")
    assert r.exit_code == 0 and faces.enabled() and faces.current() == "insightface"
    saved = prefs.load()["faces"]
    assert saved["model"] == "insightface" and saved["licence"] == faces.LICENCE_URL and saved["via"] == "cli"
    assert faces.MODELS["insightface"].label in runner.invoke(cli.app, ["faces", "status"]).output

    # The flag, for scripts: here a switch, which says what happens to the faces already found.
    r = runner.invoke(cli.app, ["faces", "enable", "--model", "opencv", "--yes"])
    assert r.exit_code == 0 and faces.current() == "opencv" and "found again" in r.output
    assert "already on" in runner.invoke(cli.app, ["faces", "enable", "--model", "opencv"]).output

    # Off, and the models removed (both sets).
    for p in faces.files("opencv"):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"model")
    assert runner.invoke(cli.app, ["faces", "disable", "--remove-models"]).exit_code == 0
    assert not faces.enabled() and faces.chosen() and not any(p.exists() for p in faces.files("opencv"))


def test_other_platforms_get_a_clear_message(env, monkeypatch, tmp_path):
    import sys

    from typer.testing import CliRunner

    import joeylab
    from joeylab import cleanup, cli, service

    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SystemExit) as e:
        joeylab.main()
    assert "runs on macOS, and partly on Linux" in str(e.value.code)

    monkeypatch.setattr(sys, "platform", "linux")
    for cmd in (["service", "install"], ["service", "status"], ["service", "stop"]):
        r = CliRunner().invoke(cli.app, cmd)
        assert r.exit_code == 1 and "launchd, which is macOS only" in r.output
    with pytest.raises(RuntimeError, match="macOS only"):
        service.running()
    f = tmp_path / "IMG_1.JPG"
    f.write_bytes(b"x")
    with pytest.raises(OSError, match="Trash works on macOS only"):
        cleanup.move_to_trash(str(f))
    assert f.exists()


def test_one_opencv_and_a_clear_message_if_its_half_removed(monkeypatch):
    import sys
    import types
    from importlib import metadata

    import joeylab

    if sys.platform == "darwin":            # the build MediaPipe needs is the only one
        names = {d.metadata["Name"].lower() for d in metadata.distributions()}
        assert "opencv-python-headless" not in names and "opencv-python" not in names
    monkeypatch.setitem(sys.modules, "cv2", types.ModuleType("cv2"))       # what's left after removing one of two
    with pytest.raises(SystemExit) as e:
        joeylab.main()
    assert "OpenCV isn't installed properly" in str(e.value.code)


def test_demo_library(env, monkeypatch):
    from functools import partial

    from typer.testing import CliRunner

    from joeylab import cli, config, db, demo, exif

    tmp, lib = env
    monkeypatch.setattr(demo, "make", partial(demo.make, scale=0.4))        # small pictures: quick
    dest = tmp / "JoeyLab Demo"
    r = CliRunner().invoke(cli.app, ["demo", str(dest), "--no-ai"])
    assert r.exit_code == 0, r.output
    assert "Made 52 demo photos" in r.output and (dest / demo.MARKER).exists()
    conn = db.connect("demo")
    count = lambda sql: conn.execute(sql).fetchone()[0]
    assert count("SELECT COUNT(*) FROM shots WHERE status='present'") == 52
    assert count("SELECT COUNT(*) FROM dup_sets WHERE kind='exact'") == 3
    if exif.available():
        assert count("SELECT COUNT(*) FROM shots WHERE capture_ts IS NULL") == 2           # the two scans
        assert count("SELECT COUNT(DISTINCT camera) FROM shots WHERE camera IS NOT NULL") == 3
        assert count("SELECT COUNT(*) FROM shots WHERE gps_lat IS NOT NULL") >= 30
        assert count("SELECT COUNT(*) FROM dup_sets WHERE kind='copy'") == 1                # the re-saved copy
        assert count("SELECT COUNT(*) FROM (SELECT group_id FROM shots GROUP BY group_id HAVING COUNT(*) >= 3)") >= 4
    first = sorted(p.read_bytes() for p in dest.rglob("*.JPG"))[:3]

    # Again: the same folder is used as it is, and nothing changes in it.
    r = CliRunner().invoke(cli.app, ["demo", str(dest), "--no-ai"])
    assert r.exit_code == 0 and "Using the demo photos already in" in r.output
    assert sorted(p.read_bytes() for p in dest.rglob("*.JPG"))[:3] == first

    # Never inside JoeyLab's home or a tracked folder, never into a folder with other things in it.
    from conftest import add_root

    add_root(lib)
    (tmp / "busy").mkdir()
    (tmp / "busy" / "notes.txt").write_text("mine")
    for where, why in ((config.HOME / "x", "JoeyLab's own folder"), (lib / "demo", "which the library 't' tracks"),
                       (tmp / "busy", "already has things in it")):
        assert why in demo.check_dest(where)
        r = CliRunner().invoke(cli.app, ["demo", str(where), "--no-ai"])
        assert r.exit_code == 1 and not (where / demo.MARKER).exists()
    assert demo.check_dest(dest) is None and demo.check_dest(tmp / "new") is None


def test_an_install_with_face_models_already_downloaded_keeps_faces_on(env, monkeypatch):
    from fastapi.testclient import TestClient

    from joeylab import faces, prefs
    from joeylab.web.server import create_app

    tmp, _ = env
    _face_files(monkeypatch, tmp / "m")
    assert not faces.enabled() and faces.current() is None
    for p in faces.files("insightface"):
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"model")
    # From before there was a choice: InsightFace's models are there, so they're the ones chosen.
    assert faces.enabled() and not faces.chosen() and faces.current() == "insightface"
    st = TestClient(create_app()).get("/api/faces").json()
    assert st["enabled"] and st["model"] == "insightface" and st["downloaded"] and st["earlier"]
    faces.set_enabled(False, via="cli")
    assert not faces.enabled() and faces.current() == "insightface"   # an explicit choice wins over the models being there

    # Turned on before there was a choice (agreeing to InsightFace's licence): still InsightFace's.
    prefs.update(faces={"enabled": True, "when": "2026-09-01 10:00:00", "via": "web", "licence": faces.LICENCE_URL})
    for p in faces.files("insightface"):
        p.unlink()
    assert faces.enabled() and faces.current() == "insightface" and faces.active() == "insightface"
