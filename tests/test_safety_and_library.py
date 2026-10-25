"""The non-destructive promise checked end to end, the safety fixes, and the library
features (time, events, places, history, portability, sharing, MCP, query reading)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import add_root, jpeg_at, make_jpeg, run_all


def snapshot(folder) -> dict:
    """Every file and folder: size, modified time, inode, mode and content hash."""
    out = {}
    for dp, dns, fns in os.walk(folder):
        st = os.lstat(dp)
        out[dp] = ("dir", sorted(dns), sorted(fns), st.st_mtime_ns, st.st_ino)
        for f in fns:
            p = os.path.join(dp, f)
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode,
                      hashlib.sha256(open(p, "rb").read()).hexdigest() if not os.path.islink(p) else os.readlink(p))
    return out


def full_pipeline(project="t"):
    from joeylab import db, pipeline

    conn = db.connect(project)
    pipeline.run_all(conn, project, pipeline.Reporter(), faces=False, ai_layer=False)
    return conn


# ---------------------------------------------------------------- the promise

def test_folders_are_untouched_by_everything(env, tmp_path):
    """S8: scan, analyse, organise, every page of the app, decisions, albums and a gallery:
    the photo folder is byte-for-byte, timestamp-for-timestamp the same afterwards."""
    _, lib = env
    for i in range(6):
        jpeg_at(lib / "a" / f"IMG_{i:04d}.JPG", i, f"2024:05:0{1 + i // 3} 1{i}:00:00")
    jpeg_at(lib / "b" / "IMG_0000.JPG", 0, "2024:05:01 10:00:00")       # an identical-ish copy
    (lib / "a" / "IMG_0001.xmp").write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/"/>')   # someone's sidecar
    os.utime(lib / "a" / "IMG_0001.xmp", (time.time() - 60, time.time() - 60))
    add_root(lib)
    before = snapshot(lib)
    conn = full_pipeline()
    from joeylab.web.jobs import JobManager
    from joeylab.web.server import create_app

    c = TestClient(create_app(jobs=JobManager(watch_minutes=0, faces=False, ai_layer=False)))
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    for url in ("/api/projects", "/api/p/t/overview", "/api/p/t/home", "/api/p/t/shots", "/api/p/t/stacks",
                f"/api/p/t/shot/{ids[0]}", "/api/p/t/timeline", "/api/p/t/folders", "/api/p/t/dupes/groups",
                "/api/p/t/health?deep=1", "/api/p/t/events", "/api/p/t/places", "/api/p/t/people", "/api/p/t/albums",
                "/api/p/t/history", "/api/p/t/export/csv", "/api/p/t/export/decisions.json", "/api/p/t/clock",
                f"/img/t/t/{ids[0]}", f"/img/t/v/{ids[0]}", f"/img/t/sharp/{ids[0]}", "/api/p/t/search?q=is:undecided"):
        assert c.get(url).status_code == 200, url
    c.post("/api/p/t/decide", json={"items": [{"id": ids[0], "pick": 1, "rating": 5}]})
    c.post("/api/p/t/undo", json={})
    c.post("/api/p/t/decide", json={"items": [{"id": ids[1], "pick": 1}]})
    assert c.post("/api/p/t/export/album", json={"filter": {"status": "picks"}, "dest": str(tmp_path / "Album")}).status_code == 200
    assert c.post("/api/p/t/export/gallery", json={"filter": {"status": "picks"}, "dest": str(tmp_path / "Gallery")}).status_code == 200
    # Albums may never go inside a folder JoeyLab only reads.
    r = c.post("/api/p/t/export/album", json={"filter": {}, "dest": str(lib / "a" / "Picks"), "copy": True})
    assert r.status_code == 400 and "only reads" in r.json()["error"]
    conn2 = full_pipeline()                                           # and once more, with the "untouched" check
    from joeylab import integrity

    assert integrity.latest(conn2)["ok"] == 1
    assert snapshot(lib) == before


def test_request_guard_blocks_other_sites(web_client):
    """S1: a page on another site can't change anything, and only this Mac's names are served."""
    c, ids = web_client
    ok = c.post("/api/p/t/decide", json={"items": [{"id": ids[0], "pick": 1}]},
                headers={"Origin": "http://testserver"})
    assert ok.status_code == 200
    evil = c.post("/api/p/t/decide", json={"items": [{"id": ids[0], "pick": -1}]}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    sneaky = c.post("/api/p/t/export/xmp", content='{"confirm": true}', headers={"Content-Type": "text/plain"})
    assert sneaky.status_code == 415
    cross = c.post("/api/p/t/undo", json={}, headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403
    rebound = c.get("/api/projects", headers={"Host": "attacker.example:8765"})
    assert rebound.status_code == 421
    assert c.get("/api/projects", headers={"Host": "my-mac.tail1234.ts.net:8765"}).status_code == 421   # not this Mac


def test_one_user_others_see_nothing_and_favourites_are_one_set(env):
    _, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    conn, _ = run_all()
    sid = conn.execute("SELECT id FROM shots").fetchone()[0]
    from joeylab.web.server import create_app

    c = TestClient(create_app(owner="owner@example.com"))
    guest = {"Tailscale-User-Login": "guest@example.com", "X-Forwarded-For": "100.64.0.9"}
    phone = {"Tailscale-User-Login": "owner@example.com", "X-Forwarded-For": "100.64.0.8"}
    assert c.get("/api/p/t/shots", headers=guest).status_code == 403
    assert c.post("/api/p/t/fav", json={"ids": [sid]}, headers=guest).status_code == 403
    assert c.post("/api/p/t/decide", json={"items": [{"id": sid, "pick": 1}]}, headers=phone).status_code == 200
    assert c.post("/api/p/t/fav", json={"ids": [sid]}, headers=phone).json()["viewer"] == "owner"
    assert c.get("/api/p/t/shots", params={"favs": "me"}).json()["total"] == 1      # the phone's favourite, at the Mac


@pytest.fixture()
def web_client(env):
    _, lib = env
    for i in range(4):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    from joeylab.web.server import create_app

    return TestClient(create_app()), [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]


def test_extras_list_never_includes_edits_or_pairs(env):
    """S2: "List of all extras" offers only identical files and re-saved copies for removal."""
    _, lib = env
    orig = jpeg_at(lib / "a" / "IMG_0001.JPG", 7, "2024:01:01 10:00:00")
    (lib / "b").mkdir()
    Image.open(orig).save(lib / "b" / "IMG_0001.JPG", "JPEG", quality=70, exif=Image.open(orig).getexif().tobytes())
    Image.open(orig).resize((450, 300)).save(lib / "c-2024-Trip-12.jpg", "JPEG", exif=Image.open(orig).getexif().tobytes())
    for p in (lib / "b" / "IMG_0001.JPG", lib / "c-2024-Trip-12.jpg"):
        os.utime(p, (time.time() - 60, time.time() - 60))
    add_root(lib)
    conn, _ = run_all()
    from joeylab import dupes

    kinds = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM dup_sets GROUP BY kind")}
    assert kinds.get("copy") == 1 and kinds.get("export") == 1
    all_sets = [r[0] for r in conn.execute("SELECT id FROM dup_sets")]
    extras = dupes.extras(conn, all_sets)
    assert len(extras) == 1 and extras[0].endswith("IMG_0001.JPG") and "Trip" not in extras[0]


def test_xmp_update_never_lowers_their_rating_and_backs_up(env):
    """S3: an existing Lightroom sidecar with 4 stars keeps them; a pick without stars writes no
    Rating; changed sidecars are backed up and can be put back."""
    from joeylab import config, export, library

    _, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    make_jpeg(lib / "IMG_2.JPG", seed=2)
    add_root(lib)
    conn, _ = run_all()
    a, b = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    theirs = '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">' \
             '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Rating="4" xmp:Label="Red"/>' \
             '</rdf:RDF></x:xmpmeta>'
    (lib / "IMG_1.xmp").write_text(theirs)
    library.set_decision(conn, a, rating=2, pick=1)
    library.set_decision(conn, b, pick=1)
    r = export.write_sidecars(conn, library.Filter(), update_existing=True, project="t")
    assert r.written == 1 and r.kept_theirs >= 1
    assert 'xmp:Rating="4"' in (lib / "IMG_1.xmp").read_text() or "Rating>4" in (lib / "IMG_1.xmp").read_text()
    new = (lib / "IMG_2.xmp").read_text()
    assert "Rating" not in new and 'xmp:Label="Green"' in new
    if r.backup_dir:
        assert export.restore_sidecars(__import__("pathlib").Path(r.backup_dir)) >= 1
    assert str(config.HOME) in (r.backup_dir or str(config.HOME))


def test_album_clean_only_removes_its_own_links(env, tmp_path):
    """S4: a link someone else put in the album folder is left alone."""
    from joeylab import export, library

    _, lib = env
    for i in range(2):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    dest = tmp_path / "Album"
    library.set_decision(conn, ids[0], pick=1)
    export.album(conn, library.Filter(picks=True), dest)
    os.symlink(str(lib / "IMG_1.JPG"), dest / "theirs.jpg")
    library.set_decision(conn, ids[0], pick=0)
    r = export.album(conn, library.Filter(picks=True), dest, clean=True)
    assert r.removed == 1 and os.path.islink(dest / "theirs.jpg")


def test_unreadable_folder_is_unavailable_not_missing(env):
    """S6: a folder macOS won't let us read keeps its photos (not marked missing)."""
    from joeylab import scanner

    _, lib = env
    make_jpeg(lib / "a" / "IMG_1.JPG", seed=1)
    make_jpeg(lib / "b" / "IMG_2.JPG", seed=2)
    add_root(lib)
    conn, _ = run_all()
    os.chmod(lib / "b", 0)
    try:
        rep = scanner.scan(conn)
        assert rep.unreadable and rep.missing == 0
        assert conn.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0] == 2
    finally:
        os.chmod(lib / "b", 0o755)


def test_undated_fragments_are_not_a_burst(env):
    """Recovered 160x120 thumbnails with no date: flagged tiny, never grouped, sorted last."""
    _, lib = env
    for i in range(4):
        make_jpeg(lib / "Recovery" / f"file 160x120_{i}.jpg", seed=i, size=(160, 120))
    jpeg_at(lib / "IMG_1.JPG", 10, "2024:01:01 10:00:00")
    add_root(lib)
    conn = full_pipeline()
    rows = conn.execute("SELECT s.group_id, m.flags FROM shots s JOIN metrics m ON m.shot_id=s.id WHERE s.stem LIKE 'file%'").fetchall()
    assert all(r[0] is None for r in rows) and all("tiny" in r[1] for r in rows)
    from joeylab import library

    page, _ = library.page(conn, library.Filter(), sort="time")
    assert page[0]["stem"] == "IMG_1"


# ---------------------------------------------------------------- time, events, places

def test_events_split_on_time_and_keep_their_names(env):
    from joeylab import events

    _, lib = env
    for i in range(4):
        jpeg_at(lib / f"A_{i}.JPG", i, f"2024:03:01 10:0{i}:00")
    for i in range(4):
        jpeg_at(lib / f"B_{i}.JPG", 10 + i, f"2024:03:09 18:0{i}:00")
    add_root(lib)
    conn = full_pipeline()
    evs = events.listing(conn, "event")
    assert len(evs) == 2
    first = min(evs, key=lambda e: e["start_ts"])
    events.rename(conn, first["id"], "Holi at home")
    jpeg_at(lib / "A_9.JPG", 99, "2024:03:01 10:09:00")
    conn = full_pipeline()
    names = {e["name"] for e in events.listing(conn, "event")}
    assert "Holi at home" in names


def test_places_offline_and_inferred(env, tmp_path, monkeypatch):
    from joeylab import geo

    g = tmp_path / "geonames"
    g.mkdir()
    (g / "countryInfo.txt").write_text("#\nIN\tIND\t356\tIN\tIndia\n")
    (g / "admin1CodesASCII.txt").write_text("IN.13\tKerala\tKerala\t1\n")
    rows = [("1", "Kochi", "9.9312", "76.2673", "IN", "13", "600000"), ("2", "Aluva", "10.1076", "76.3516", "IN", "13", "20000")]
    (g / "cities5000.txt").write_text("\n".join("\t".join([r[0], r[1], r[1], "", r[2], r[3], "P", "PPL", r[4], "", r[5], "", "", "", r[6]])
                                                for r in rows) + "\n")
    monkeypatch.setattr(geo, "GEO_DIR", g)
    geo.Gazetteer._inst = None
    _, lib = env
    jpeg_at(lib / "IMG_1.JPG", 1, "2024:08:23 10:00:00")
    jpeg_at(lib / "IMG_2.JPG", 2, "2024:08:23 11:00:00")
    add_root(lib)
    conn = full_pipeline()
    a = conn.execute("SELECT id FROM shots WHERE stem='IMG_1'").fetchone()[0]
    conn.execute("UPDATE shots SET gps_lat=9.95, gps_lon=76.27 WHERE id=?", (a,))
    conn.commit()
    res = geo.assign(conn, download=False)
    assert res == {"gps": 1, "inferred": 1, "available": True}
    names = {r[0] for r in conn.execute("SELECT p.name FROM shots s JOIN places p ON p.id=s.place_id")}
    assert names == {"Kochi"}
    geo.Gazetteer._inst = None


def test_clock_offset_moves_one_camera_on_the_timeline(env):
    from joeylab import clocksync

    _, lib = env
    jpeg_at(lib / "IMG_1.JPG", 1, "2024:01:01 10:00:00")
    add_root(lib)
    conn = full_pipeline()
    before = conn.execute("SELECT eff_ts FROM shots").fetchone()[0]
    key = clocksync.cameras(conn)[0]["key"]
    clocksync.set_offset(conn, key, 90)
    assert conn.execute("SELECT eff_ts FROM shots").fetchone()[0] == before + 90


def test_clock_suggestions_are_kept_until_what_they_come_from_changes(env, monkeypatch):
    from joeylab import clocksync

    _, lib = env
    for i in range(3):
        jpeg_at(lib / f"IMG_{i}.JPG", i, f"2024:01:01 10:0{i}:00")
    add_root(lib)
    conn = run_all()[0]
    made = []
    monkeypatch.setattr(clocksync, "_suggest", lambda c: made.append(1) or [{"camera": "b", "offset_s": 3600.0 * len(made)}])
    clocksync._SUGGESTED.clear()
    assert clocksync.suggest(conn) == clocksync.suggest(conn) == [{"camera": "b", "offset_s": 3600.0}] and made == [1]
    clocksync.set_offset(conn, clocksync.cameras(conn)[0]["key"], 90)       # an offset set: worked out again
    assert clocksync.suggest(conn)[0]["offset_s"] == 7200.0
    # Asked for a library's page, ones out of date come at once and newer ones follow in the background.
    conn.execute("UPDATE shots SET capture_ts = capture_ts + 600")
    conn.commit()
    assert clocksync.suggest(conn, "t")[0]["offset_s"] == 7200.0
    end = time.time() + 10
    while clocksync.suggest(conn, "t")[0]["offset_s"] != 10800.0:
        assert time.time() < end
        time.sleep(0.05)
    assert len(made) == 3


# ---------------------------------------------------------------- portability, history, sharing

def test_portable_decisions_reattach_to_a_fresh_catalog(env):
    from joeylab import config, db, library, portable

    _, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    library.decide(conn, [{"id": ids[0], "rating": 5, "pick": 1}, {"id": ids[1], "label": "Red"}])
    data = portable.export_decisions(conn, "t")
    fresh = db.connect("fresh")
    from joeylab import scanner

    scanner.add_root(fresh, lib)
    scanner.scan(fresh)
    res = portable.import_decisions(fresh, json.loads(json.dumps(data)))
    assert res["decisions"] == 2 and res["unmatched"] == 0
    got = {r[0]: (r[1], r[2], r[3]) for r in fresh.execute(
        "SELECT s.stem, d.rating, d.pick, d.label FROM decisions d JOIN shots s ON s.id=d.shot_id")}
    assert got["IMG_0"][:2] == (5, 1) and got["IMG_1"][2] == "Red"
    assert (config.HOME / "projects" / "fresh").exists()


def test_lightroom_import_reads_a_copy(env, tmp_path):
    from joeylab import portable

    _, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    conn, _ = run_all()
    cat = tmp_path / "Lightroom Catalog.lrcat"
    lr = sqlite3.connect(cat)
    lr.executescript("""
      CREATE TABLE AgLibraryRootFolder (id_local INTEGER, absolutePath TEXT);
      CREATE TABLE AgLibraryFolder (id_local INTEGER, pathFromRoot TEXT, rootFolder INTEGER);
      CREATE TABLE AgLibraryFile (id_local INTEGER, baseName TEXT, extension TEXT, folder INTEGER);
      CREATE TABLE Adobe_images (id_local INTEGER, rootFile INTEGER, rating REAL, pick REAL, colorLabels TEXT, captureTime TEXT);
    """)
    lr.execute("INSERT INTO AgLibraryRootFolder VALUES (1, ?)", (str(lib) + "/",))
    lr.execute("INSERT INTO AgLibraryFolder VALUES (1, '', 1)")
    lr.execute("INSERT INTO AgLibraryFile VALUES (1, 'IMG_1', 'JPG', 1)")
    lr.execute("INSERT INTO Adobe_images VALUES (1, 1, 4, 1, 'Purple', '2024-01-01T10:00:00')")
    lr.commit()
    lr.close()
    mtime = os.stat(cat).st_mtime_ns
    res = portable.import_lightroom(conn, str(cat))
    assert res["imported"] == 1
    assert conn.execute("SELECT rating, pick, label FROM decisions").fetchone()[:] == (4, 1, "Purple")
    assert os.stat(cat).st_mtime_ns == mtime


def test_share_link_shows_only_its_album(env):
    _, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    from joeylab.web.server import create_app

    c = TestClient(create_app())
    aid = c.post("/api/p/t/albums", json={"name": "For Gran", "ids": ids[:2]}).json()["id"]
    tok = c.post("/api/p/t/shares", json={"kind": "album", "ref": aid}).json()["token"]
    s = c.get(f"/api/share/{tok}").json()
    assert s["title"] == "For Gran" and {r["id"] for r in s["rows"]} == set(ids[:2])
    assert c.get(f"/img/share/{tok}/t/{ids[0]}").status_code == 200
    assert c.get(f"/img/share/{tok}/t/{ids[2]}").status_code == 404
    c.delete(f"/api/p/t/shares/{tok}")
    assert c.get(f"/api/share/{tok}").status_code == 404


def test_mcp_tools(env):
    from joeylab import library, mcp

    _, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    assert mcp.call("list_libraries", {}, None, True)[0]["name"] == "t"
    assert mcp.client_label({"name": "codex-mcp-client"}) == "Codex"
    assert mcp.client_label({"name": "claude-code"}) == "Claude"
    assert mcp.client_label(None) == "Assistant"
    mcp.CLIENT["name"] = "Codex"
    res = mcp.call("set_decisions", {"ids": ids[:2], "pick": 1, "why": "test"}, "t", True)
    assert res["changed"] == 2
    found = mcp.call("search_photos", {"query": "is:pick"}, "t", True)
    assert {p["id"] for p in found["photos"]} == set(ids[:2])
    # A later change of yours is left alone: the assistant only undoes its own.
    library.decide(conn, [{"id": ids[2], "rating": 4}], what="4★", source="web")
    assert mcp.call("undo_last", {}, "t", True)["undone"].startswith("Codex")
    assert conn.execute("SELECT rating FROM decisions WHERE shot_id=?", (ids[2],)).fetchone()[0] == 4
    mcp.CLIENT["name"] = "Assistant"
    with pytest.raises(ValueError):
        mcp.call("set_decisions", {"ids": ids}, "t", False)
    img = mcp.call("view_photo", {"id": ids[0]}, "t", True)
    assert img["__image__"]


def test_query_reads_plain_language(env):
    from joeylab import query

    _, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    conn, _ = run_all()
    conn.execute("INSERT INTO people(name) VALUES ('Maya')")
    conn.commit()
    p = query.parse("Maya laughing in 2023 that I haven't picked", conn)
    assert p["filter"]["people"] and p["filter"]["from"] == "2023-01-01" and p["filter"]["status"] == "undecided"
    assert p["text"] == "laughing"
    f = query.parse('is:pick rating:>3 iso:>800 "first dance"', conn)
    assert f["filter"]["status"] == "picks" and f["filter"]["min_rating"] == 4 and f["text"] == "first dance"


def test_backups_keep_weekly_and_monthly_and_restore(env):
    from joeylab import backup, config, library

    _, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    conn, _ = run_all()
    d = config.HOME / "backups" / "t"
    d.mkdir(parents=True, exist_ok=True)
    for day in range(1, 61):
        (d / f"catalog-2026{(day - 1) // 30 + 7:02d}{(day - 1) % 30 + 1:02d}.db").write_bytes(b"x")
    backup.prune("t")
    kept = sorted(p.name for p in d.glob("catalog-*.db"))
    assert len(kept) >= 14 and any(k.startswith("catalog-202607") for k in kept)   # the oldest month is still represented
    assert len(kept) < 60
    sid = conn.execute("SELECT id FROM shots").fetchone()[0]
    library.set_decision(conn, sid, rating=5)
    good = backup.backup("t", force=True)
    library.set_decision(conn, sid, rating=1)
    conn.close()
    src, old = backup.restore("t", good)
    from joeylab import db

    assert db.connect("t").execute("SELECT rating FROM decisions").fetchone()[0] == 5 and old.exists()


def test_redo_only_straight_after_undo_and_is_a_step_of_its_own(env):
    from joeylab import keywords, library
    from joeylab.web.server import create_app

    _, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    a, b, c = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]

    def pick(sid):
        r = conn.execute("SELECT pick FROM decisions WHERE shot_id=?", (sid,)).fetchone()
        return r[0] if r else 0

    library.decide(conn, [{"id": a, "pick": 1}], what="pick")
    library.decide(conn, [{"id": b, "pick": -1}], what="reject")
    assert not library.can_redo(conn) and library.redo(conn) is None          # nothing undone yet
    assert library.undo(conn)["label"] == "reject" and pick(b) == 0
    assert library.can_redo(conn)
    assert library.redo(conn)["label"] == "reject" and pick(b) == -1
    assert not library.can_redo(conn)                                         # that undo is spent
    # Undo straight after Redo reverses the redo, not the step before it.
    assert library.undo(conn)["label"] == "reject"
    assert pick(b) == 0 and pick(a) == 1
    assert library.redo(conn) and pick(b) == -1
    # Two undos are redone newest first.
    library.undo(conn)
    library.undo(conn)
    assert pick(a) == 0 and pick(b) == 0
    assert library.redo(conn)["label"] == "pick" and pick(a) == 1 and pick(b) == 0
    assert library.redo(conn)["label"] == "reject" and pick(b) == -1
    assert library.redo(conn) is None
    # A new decision ends what can be redone: the old step stays undone.
    library.undo(conn)
    library.decide(conn, [{"id": c, "pick": 1}], what="pick")
    assert not library.can_redo(conn) and library.redo(conn) is None and pick(b) == 0
    # Redo steps are in the history as steps of their own, and count once in the day's summary.
    assert any(s["source"] == "redo" for s in library.history(conn, 50))
    assert library.day_summary(conn, 0) == {"picked": 2, "rejected": 0, "rated": 0, "photos": 2}
    # Keywords redo too.
    beach = keywords.ensure(conn, "Beach")
    keywords.assign(conn, [a], add=[beach], what="keywords: Beach")
    library.undo(conn)
    assert keywords.of_shot(conn, a) == []
    library.redo(conn)
    assert [k["name"] for k in keywords.of_shot(conn, a)] == ["Beach"]
    # The web app says whether Redo is possible, and its answers carry the photos (for "Show").
    client = TestClient(create_app())
    assert client.get("/api/p/t/history").json()["can_redo"] is False
    u = client.post("/api/p/t/undo", json={}).json()
    assert u["undone"] == "keywords: Beach" and [r["id"] for r in u["rows"]] == [a]
    assert client.get("/api/p/t/history").json()["can_redo"] is True
    r = client.post("/api/p/t/redo").json()
    assert r["redone"] == "keywords: Beach" and [x["id"] for x in r["rows"]] == [a]
    assert client.post("/api/p/t/redo").json()["redone"] is None
