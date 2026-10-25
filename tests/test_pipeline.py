"""End-to-end tests on synthetic images: incremental scans, moves, deletes, pairing, dupes, exports."""

from __future__ import annotations

import os
import shutil
import time

from conftest import add_root, make_jpeg, run_all


def test_scan_move_rename_delete_restore(env):
    from joeylab import library

    _, lib = env
    for i in range(6):
        make_jpeg(lib / "a" / f"IMG_{i:04d}.JPG", seed=i)
    add_root(lib)
    conn, rep = run_all()
    assert rep.added == 6 and conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 6

    sid = conn.execute("SELECT id FROM shots WHERE stem='IMG_0001'").fetchone()[0]
    library.set_decision(conn, sid, rating=5, pick=1)

    # rename + move into another folder
    (lib / "b").mkdir()
    shutil.move(lib / "a" / "IMG_0001.JPG", lib / "b" / "renamed.JPG")
    conn, rep = run_all()
    assert rep.moved == 1 and rep.added == 0 and rep.missing == 0
    s = library.shot(conn, sid)
    assert s["stem"] == "renamed" and s["rating"] == 5 and s["pick"] == 1
    assert conn.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0] == 6

    # delete: decision survives as missing
    moved = lib / "b" / "renamed.JPG"
    keep = moved.read_bytes()
    moved.unlink()
    conn, rep = run_all()
    assert rep.missing == 1
    assert library.shot(conn, sid)["status"] == "missing"
    assert len(library.shots(conn, library.Filter(picks=True))) == 0

    # restore somewhere else: recognised by content, decision back
    moved2 = lib / "c" / "back.JPG"
    moved2.parent.mkdir()
    moved2.write_bytes(keep)
    old = time.time() - 60
    os.utime(moved2, (old, old))
    conn, rep = run_all()
    assert rep.moved == 1
    s = library.shot(conn, sid)
    assert s["status"] == "present" and s["pick"] == 1


def test_modified_file_is_reanalysed(env):
    from joeylab import analysis

    _, lib = env
    p = make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    conn, _ = run_all()
    make_jpeg(p, seed=99, blur=6)  # content changes, same path
    conn, rep = run_all()
    assert rep.changed == 1
    assert analysis.pending_tasks(conn, "t") == []
    assert conn.execute("SELECT sharp_subject FROM metrics").fetchone()[0] < 50


def test_unavailable_root_does_not_mark_missing(env):
    tmp, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    run_all()
    (tmp / "unplugged").mkdir()
    lib.rename(tmp / "unplugged" / "lib")          # gone from where it was (renamed in place would be followed)
    conn, rep = run_all()
    assert rep.roots_unavailable and rep.missing == 0
    assert conn.execute("SELECT status FROM files").fetchone()[0] == "present"


def test_settling_files_skipped(env):
    _, lib = env
    p = make_jpeg(lib / "IMG_1.JPG", seed=1)
    os.utime(p, None)  # just written
    add_root(lib)
    conn, rep = run_all()
    assert rep.settling and rep.added == 0


def test_raw_jpeg_pairing_and_preview_choice(env):
    _, lib = env
    make_jpeg(lib / "DSC_0001.JPG", seed=1)
    raw = lib / "DSC_0001.NEF"
    raw.write_bytes(os.urandom(4096))
    old = time.time() - 60
    os.utime(raw, (old, old))
    add_root(lib)
    conn, _ = run_all()
    shots = conn.execute("SELECT id, preview_file_id FROM shots").fetchall()
    assert len(shots) == 1
    kinds = {r[0] for r in conn.execute("SELECT kind FROM files WHERE shot_id=?", (shots[0][0],))}
    assert kinds == {"raw", "jpeg"}
    assert conn.execute("SELECT kind FROM files WHERE id=?", (shots[0][1],)).fetchone()[0] == "jpeg"


def test_exact_duplicates_and_bursts(env):
    from joeylab.dupes import duplicate_shot_ids

    _, lib = env
    make_jpeg(lib / "a" / "IMG_1.JPG", seed=1)
    (lib / "backup").mkdir()
    shutil.copy2(lib / "a" / "IMG_1.JPG", lib / "backup" / "IMG_1.JPG")
    add_root(lib)
    conn, _ = run_all()
    assert conn.execute("SELECT COUNT(*) FROM dup_sets WHERE kind='exact'").fetchone()[0] == 1
    extra = duplicate_shot_ids(conn)
    assert len(extra) == 1
    flags = conn.execute("SELECT flags, suggestion FROM metrics WHERE shot_id=?", (next(iter(extra)),)).fetchone()
    assert "duplicate" in flags[0] and flags[1] == "reject"


def test_blurry_image_flagged_and_ranked_lower(env):
    _, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=5)
    make_jpeg(lib / "IMG_2.JPG", seed=5, blur=8)
    add_root(lib)
    conn, _ = run_all()
    q = dict(conn.execute("SELECT s.stem, m.quality FROM metrics m JOIN shots s ON s.id=m.shot_id").fetchall())
    assert q["IMG_1"] > q["IMG_2"]
    f = conn.execute("SELECT m.flags FROM metrics m JOIN shots s ON s.id=m.shot_id WHERE s.stem='IMG_2'").fetchone()[0]
    assert "blurry" in f


def test_album_export_is_symlinks_only(env):
    from joeylab import export, library

    tmp, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    library.set_decision(conn, ids[0], pick=1)
    library.set_decision(conn, ids[1], rating=4)
    before = sorted(os.listdir(lib))
    album = tmp / "Best"
    r = export.album(conn, library.Filter(picks=True), album)
    assert r.created == 1 and all(os.path.islink(album / n) for n in os.listdir(album))
    r = export.album(conn, library.Filter(min_rating=4), album, clean=True)
    assert r.removed == 1 and r.created == 1
    assert sorted(os.listdir(lib)) == before  # originals untouched

    # scanning a folder containing the album does not double count
    conn, rep = run_all()
    assert rep.seen == 3


def test_xmp_sidecar(env):
    from joeylab import export, library

    _, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    conn, _ = run_all()
    sid = conn.execute("SELECT id FROM shots").fetchone()[0]
    library.set_decision(conn, sid, rating=3, pick=1)
    orig = (lib / "IMG_1.JPG").read_bytes()
    r = export.write_sidecars(conn, library.Filter())
    assert r.written == 1
    text = (lib / "IMG_1.xmp").read_text()
    assert 'xmp:Rating="3"' in text and 'xmp:Label="Green"' in text
    assert (lib / "IMG_1.JPG").read_bytes() == orig
    r = export.write_sidecars(conn, library.Filter())
    assert r.skipped_existing == 1


def test_resaved_copy_is_duplicate_but_renamed_edit_is_not(env):
    from PIL import Image

    from joeylab.dupes import duplicate_shot_ids

    _, lib = env
    orig = make_jpeg(lib / "a" / "IMG_0001.JPG", seed=7)
    for dest, q in ((lib / "b" / "IMG_0001.JPG", 70), (lib / "c" / "2026-Trip-12.jpg", 80)):
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.open(orig).save(dest, "JPEG", quality=q)        # same picture, re-encoded
        old = time.time() - 60
        os.utime(dest, (old, old))
    add_root(lib)
    conn, _ = run_all()
    kinds = sorted(r[0] for r in conn.execute("SELECT kind FROM dup_sets"))
    assert kinds == ["copy", "export"]
    stems = {r[0] for r in conn.execute(
        "SELECT s.stem FROM shots s WHERE s.id IN (%s)" % ",".join(map(str, duplicate_shot_ids(conn))))}
    assert stems == {"IMG_0001"}                               # only a same-name copy is an extra
    edit = conn.execute("SELECT m.flags FROM metrics m JOIN shots s ON s.id=m.shot_id WHERE s.stem='2026-Trip-12'").fetchone()[0]
    assert "duplicate" not in edit


def test_offline_run_skips_missing_models(env):
    """With JOEYLAB_OFFLINE (as in every test) nothing is downloaded: features whose models
    aren't on this Mac are skipped with a note, and the update still finishes."""
    from joeylab import config, db, pipeline

    tmp, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    logs = []

    class Rep(pipeline.Reporter):
        def log(self, msg, level="info"):
            logs.append((level, msg))

    conn = db.connect("t")
    out = pipeline.run_all(conn, "t", Rep(), faces=True, ai_layer=True)
    assert out["analysed"] == 3 and out["faces"] == 0 and out["embedded"] == 0 and out["search_indexed"] == 0
    assert not config.MODELS_DIR.exists() or not any(config.MODELS_DIR.rglob("*.*"))
    from joeylab import ai, search

    if ai.available():
        assert any(m.startswith("AI: skipped") for _, m in logs)
    if search.available():
        assert any(m.startswith("Search: not indexed") for _, m in logs)


def test_heic_opens(tmp_path):
    """HEIC (iPhone) and HEIF (.hif) decode through pi-heif. A real HEIC is made with macOS's sips
    when it's there; elsewhere only the registration is checked."""
    import shutil
    import subprocess

    from PIL import Image

    from joeylab import imaging

    assert "HEIF" in Image.OPEN
    sips = shutil.which("sips")
    if not sips:
        return
    src = make_jpeg(tmp_path / "a.jpg", seed=1)
    out = tmp_path / "a.heic"
    subprocess.run([sips, "-s", "format", "heic", str(src), "--out", str(out)], capture_output=True)
    if not out.exists():
        return
    img = imaging.load_preview(str(out), "other")
    assert img.size == (900, 600) and img.mode == "RGB"


def test_missing_exiftool_is_said_once_and_shown_in_meta(env, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from joeylab import cli, exif
    from joeylab.web.server import create_app

    tmp, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    monkeypatch.setattr(exif, "exiftool", lambda: None)
    out = CliRunner().invoke(cli.app, ["scan", str(lib), "-p", "t"]).output
    assert out.count("exiftool isn't installed") == 1 and "undated" in out
    assert TestClient(create_app()).get("/api/meta").json()["exiftool"] is False


def test_exiftool_is_found_outside_path(tmp_path, monkeypatch):
    """A launchd service starts with a bare PATH: Homebrew's and MacPorts' folders are looked in too."""
    from joeylab import exif

    tool = tmp_path / "bin" / "exiftool"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setattr(exif.shutil, "which", lambda name: None)
    monkeypatch.setattr(exif, "SEARCH_DIRS", ("/nonexistent", str(tool.parent)))
    monkeypatch.setattr(exif, "_found", None)
    assert exif.exiftool() == str(tool) and exif.available()
    assert exif.run_env(str(tool))["PATH"].startswith(str(tool.parent))
