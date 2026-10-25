"""Power features of the Library and Search: more filters and sorts, search paging, stacks in bulk,
albums, dates for undated photos, the History page and Put Back from the Trash."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import add_root, make_jpeg, run_all


def _shot(path, seed, when=None, make=None, model=None):
    make_jpeg(path, seed=seed)
    img = Image.open(path)
    ex = img.getexif()
    if when:
        ex.get_ifd(0x8769)[0x9003] = when
    if make:
        ex[0x010F] = make
    if model:
        ex[0x0110] = model
    img.save(path, "JPEG", quality=90, exif=ex.tobytes())
    old = time.time() - 60
    os.utime(path, (old, old))
    return path


@pytest.fixture()
def lib_web(env):
    tmp, lib = env
    _shot(lib / "2023" / "DSCF0001.JPG", 1, "2023:05:02 10:00:00", "FUJIFILM", "X-T4")
    _shot(lib / "2023" / "DSCF0002.JPG", 2, "2023:05:20 11:00:00", "FUJIFILM", "X-T4")
    _shot(lib / "2023" / "IMG_0001.JPG", 3, "2023:06:01 09:00:00", "Apple", "iPhone 13 Pro")
    _shot(lib / "2024" / "IMG_0002.JPG", 4, "2024:05:03 09:00:00", "Apple", "iPhone 13 Pro")
    _shot(lib / "2024" / "R5_0001.JPG", 5, "2024:07:04 12:00:00", "Canon", "Canon EOS R5")
    shutil.copy2(lib / "2024" / "R5_0001.JPG", lib / "2024" / "R5_0001.CR2")      # its RAW (stands in for one)
    _shot(lib / "raw" / "ONLY_RAW.JPG", 6, "2024:08:01 12:00:00", "Canon", "Canon EOS R5")
    os.rename(lib / "raw" / "ONLY_RAW.JPG", lib / "raw" / "ONLY_RAW.DNG")
    make_jpeg(lib / "scans" / "scan001.jpg", seed=7)                                  # no capture date
    make_jpeg(lib / "scans" / "scan002.jpg", seed=8)
    add_root(lib)
    conn, _ = run_all()
    from joeylab.web.jobs import JobManager
    from joeylab.web.server import create_app

    client = TestClient(create_app(jobs=JobManager(watch_minutes=0, faces=False, ai_layer=False)))
    ids = {r["stem"]: r["id"] for r in conn.execute("SELECT id, stem FROM shots")}
    return client, conn, lib, ids


def _stems(client, **params):
    return sorted(r["stem"] for r in client.get("/api/p/t/shots", params={"all": 1, "limit": 100, **params}).json()["rows"])


def test_new_filters(lib_web):
    client, conn, lib, ids = lib_web
    assert _stems(client, filetype="pair") == ["R5_0001"]
    assert _stems(client, filetype="raw") == ["ONLY_RAW"]
    assert "R5_0001" not in _stems(client, filetype="jpeg") and "DSCF0001" in _stems(client, filetype="jpeg")
    # (An export's own "files" still says which of each photo's files to list.)
    assert "R5_0001.CR2" in client.get("/api/p/t/export/list", params={"files": "raw"}).text
    assert _stems(client, camera="X-T4,iPhone") == ["DSCF0001", "DSCF0002", "IMG_0001", "IMG_0002"]
    assert _stems(client, month=5) == ["DSCF0001", "DSCF0002", "IMG_0002"]

    a, b = (client.post("/api/p/t/keywords", json={"path": p}).json()["id"] for p in ("Trips › Japan", "Food"))
    client.post("/api/p/t/keywords/assign", json={"ids": [ids["DSCF0001"], ids["DSCF0002"]], "add": [a]})
    client.post("/api/p/t/keywords/assign", json={"ids": [ids["DSCF0002"], ids["IMG_0001"]], "add": [b]})
    parent = next(k for k in client.get("/api/p/t/keywords").json()["tree"] if k["name"] == "Trips")
    assert parent["name"] == "Trips"
    assert _stems(client, keyword=f"{parent['id']},{b}") == ["DSCF0002"]          # all of them
    assert _stems(client, not_keyword=str(parent["id"]), camera="X-T4,iPhone") == ["IMG_0001", "IMG_0002"]

    r = client.post("/api/p/t/albums", json={"name": "Some", "ids": [ids["DSCF0001"]]}).json()
    assert "DSCF0001" not in _stems(client, no_album=1) and "DSCF0002" in _stems(client, no_album=1)

    step = client.post("/api/p/t/decide", json={"items": [{"id": ids["scan001"], "rating": 3}, {"id": ids["IMG_0002"], "pick": 1}]}).json()
    assert _stems(client, step=step["batch"]) == ["IMG_0002", "scan001"]
    assert r["added"] == 1


def test_new_sorts(lib_web):
    client, conn, lib, ids = lib_web
    rows = lambda s: [r["stem"] for r in client.get("/api/p/t/shots", params={"all": 1, "sort": s}).json()["rows"]]
    assert rows("added")[0] == max(ids, key=ids.get)
    cams = [r["camera"] for r in client.get("/api/p/t/shots", params={"all": 1, "sort": "camera"}).json()["rows"]]
    named = [c for c in cams if c]
    assert named == sorted(named, key=str.lower) and cams[-1] is None
    sized = client.get("/api/p/t/shots", params={"all": 1, "sort": "size"}).json()["rows"]
    assert sized[0]["stem"] == "R5_0001" and sized[0]["bytes"] >= sized[1]["bytes"]       # the JPEG and its RAW


def test_month_offset_matches_the_listing(lib_web):
    client, *_ = lib_web
    for sort in ("time", "time_desc"):
        listing = client.get("/api/p/t/shots", params={"all": 1, "sort": sort}).json()["rows"]
        for month in ("2023-06", "2024-05", "undated"):
            off = client.get("/api/p/t/offset", params={"all": 1, "sort": sort, "month": month}).json()["offset"]
            first = next(i for i, r in enumerate(listing)
                         if (r["capture_str"] or "undated")[:7] == month or (month == "undated" and r["time_src"] == "file"))
            assert off == first, (sort, month)
    assert client.get("/api/p/t/offset", params={"month": "1999-01"}).json()["offset"] is None
    assert client.get("/api/p/t/offset", params={"month": "May"}).status_code == 400


def test_stack_frames_and_hidden_count(lib_web):
    client, conn, lib, ids = lib_web
    a, b = ids["DSCF0001"], ids["DSCF0002"]
    conn.execute("INSERT INTO groups(id, size) VALUES (900, 2)")
    conn.execute("UPDATE shots SET group_id=900 WHERE id IN (?, ?)", (a, b))
    conn.commit()
    got = client.post("/api/p/t/frames", json={"filter": {}, "ids": [a, ids["IMG_0002"]]}).json()["ids"]
    assert sorted(got) == sorted([a, b, ids["IMG_0002"]])
    # Only matching frames: a filter that leaves one frame out leaves it out here too.
    got = client.post("/api/p/t/frames", json={"filter": {"from": "2023-05-10"}, "ids": [b]}).json()["ids"]
    assert got == [b]
    # Browsing hides thumbnail-sized photos: the count says how many.
    conn.execute("UPDATE metrics SET flags='[\"tiny\"]' WHERE shot_id=?", (ids["scan001"],))
    conn.commit()
    r = client.get("/api/p/t/shots").json()
    assert r["hidden"] == 2 and r["total"] == len(ids) - 2       # and the RAW that couldn't be read
    assert client.get("/api/p/t/shots", params={"all": 1}).json()["hidden"] == 0


def test_search_pages_sorts_and_lists_every_match(lib_web, monkeypatch):
    client, conn, lib, ids = lib_web
    readable = sorted(i for stem, i in ids.items() if stem != "ONLY_RAW")      # (that RAW can't be read)
    first = client.get("/api/p/t/search", params={"q": "is:undecided", "limit": 3}).json()
    assert first["engine"] == "filter" and first["total"] == len(readable) and len(first["rows"]) == 3
    assert first["filter"] == {"status": "undecided"}
    rest = client.get("/api/p/t/search", params={"q": "is:undecided", "limit": 3, "offset": 3}).json()
    assert rest["total"] is None and not {r["id"] for r in rest["rows"]} & {r["id"] for r in first["rows"]}
    every = client.get("/api/p/t/search", params={"q": "is:undecided", "ids_only": 1}).json()["ids"]
    assert sorted(every) == readable
    newest = client.get("/api/p/t/search", params={"q": "2023", "sort": "newest"}).json()["rows"]
    assert [r["stem"] for r in newest] == ["IMG_0001", "DSCF0002", "DSCF0001"]
    assert client.get("/api/p/t/search", params={"q": "may 2024 iphone"}).json()["total"] == 1

    # A picture search is ranked once, then paged from that ranking.
    calls = []

    class Fake:
        @staticmethod
        def search(conn, text, limit, restrict=None):
            calls.append(text)
            order = sorted(ids.values()) if restrict is None else sorted(restrict)
            return [{"id": i, "score": 1 - k / 10, "prob": 0.1, "level": "strong" if k < 4 else "unlikely", "box": None,
                     "label": None, "label_conf": None, "text": False, "all_parts": True} for k, i in enumerate(order)][:limit]

        indexed_count = staticmethod(lambda conn: len(ids))
        concepts = staticmethod(lambda text: [text])

    from joeylab.web.routes import ai as r_ai

    monkeypatch.setattr(r_ai, "_search_mod", lambda conn: Fake)
    p1 = client.get("/api/p/t/search", params={"q": "dog", "limit": 2, "collapse": 0}).json()
    assert p1["total"] == 4 and p1["counts"] == {"strong": 4, "possible": 0, "unlikely": len(ids) - 4} and len(p1["rows"]) == 2
    p2 = client.get("/api/p/t/search", params={"q": "dog", "limit": 2, "offset": 2, "collapse": 0}).json()
    assert [r["id"] for r in p2["rows"]] == sorted(ids.values())[2:4] and calls == ["dog"]
    allm = client.get("/api/p/t/search", params={"q": "dog", "collapse": 0, "unlikely": 1, "ids_only": 1}).json()["ids"]
    assert len(allm) == len(ids) and calls == ["dog"]
    # A filter in the words limits the picture search to what it matches.
    r = client.get("/api/p/t/search", params={"q": "dog 2023", "collapse": 0, "unlikely": 1}).json()
    assert r["total"] == 3 and r["chips"] == ["2023"]


def test_album_items_add_remove_and_by_filter(lib_web):
    client, conn, lib, ids = lib_web
    aid = client.post("/api/p/t/albums", json={"name": "Trip", "ids": [ids["DSCF0001"]]}).json()["id"]
    r = client.post(f"/api/p/t/albums/{aid}/items", json={"add": [ids["DSCF0001"], ids["DSCF0002"]]}).json()
    assert r["added"] == 1 and r["added_ids"] == [ids["DSCF0002"]] and r["album"] == "Trip"
    r = client.post(f"/api/p/t/albums/{aid}/items", json={"filter": {"camera": "iPhone"}, "op": "add"}).json()
    assert sorted(r["added_ids"]) == sorted([ids["IMG_0001"], ids["IMG_0002"]])
    assert _stems(client, album=aid) == ["DSCF0001", "DSCF0002", "IMG_0001", "IMG_0002"]
    r = client.post(f"/api/p/t/albums/{aid}/items", json={"filter": {"album": aid, "camera": "X-T4"}, "op": "remove"}).json()
    assert r["removed"] == 2 and _stems(client, album=aid) == ["IMG_0001", "IMG_0002"]
    assert client.post(f"/api/p/t/albums/{aid}/items", json={"filter": {}}).status_code == 400
    light = client.get("/api/p/t/albums", params={"light": 1}).json()["albums"]
    assert light[0]["name"] == "Trip" and light[0]["items"] == 2
    smart = client.post("/api/p/t/albums", json={"name": "S", "smart": {"status": "picks"}}).json()["id"]
    assert client.post(f"/api/p/t/albums/{smart}/items", json={"add": [ids["DSCF0001"]]}).status_code == 400
    assert all(a["kind"] == "manual" for a in client.get("/api/p/t/albums", params={"light": 1}).json()["albums"])


def test_dates_for_undated_photos_undo_redo_and_rescan(lib_web):
    client, conn, lib, ids = lib_web
    s1, s2, dated = ids["scan001"], ids["scan002"], ids["DSCF0001"]
    assert client.post("/api/p/t/date", json={"ids": [s1], "date": "June 1998"}).status_code == 400
    r = client.post("/api/p/t/date", json={"ids": [s1, s2, dated], "date": "1998-06"}).json()
    assert r["n"] == 2 and r["kept"] == 1
    assert {x["capture_str"] for x in r["rows"]} == {"1998-06-01 12:00:00"}
    assert _stems(client, undated=1) == []
    assert _stems(client, **{"from": "1998-06-01", "to": "1998-06-30"}) == ["scan001", "scan002"]
    assert conn.execute("SELECT capture_str FROM shots WHERE id=?", (dated,)).fetchone()[0].startswith("2023")
    # An update keeps the date you gave (the file has none of its own).
    from joeylab import scanner

    os.utime(lib / "scans" / "scan001.jpg", (time.time() - 30, time.time() - 30))   # touched: read again
    scanner.scan(conn)
    assert conn.execute("SELECT capture_str, time_src FROM shots WHERE id=?", (s1,)).fetchone()[:] == ("1998-06-01 12:00:00", "manual")
    u = client.post("/api/p/t/undo", json={}).json()
    assert u["undone"] == "date: 1998-06-01"
    assert _stems(client, undated=1) == ["scan001", "scan002"]
    client.post("/api/p/t/redo", json={})
    assert _stems(client, undated=1) == []
    r = client.post("/api/p/t/date", json={"ids": [s1], "date": None}).json()
    assert r["n"] == 1 and _stems(client, undated=1) == ["scan001"]


def test_history_steps_by_kind_and_date(lib_web):
    client, conn, lib, ids = lib_web
    client.post("/api/p/t/decide", json={"items": [{"id": ids["DSCF0001"], "pick": 1}, {"id": ids["DSCF0002"], "pick": 1}], "label": "pick"})
    client.post("/api/p/t/decide", json={"items": [{"id": ids["IMG_0001"], "rating": 4}], "label": "4★"})
    kw = client.post("/api/p/t/keywords", json={"path": "Food"}).json()["id"]
    client.post("/api/p/t/keywords/assign", json={"ids": [ids["IMG_0002"]], "add": [kw]})
    client.post("/api/p/t/undo", json={})
    d = client.get("/api/p/t/history/steps").json()
    assert d["total"] == 4 and [s["label"] for s in d["steps"]][:2] == ["undo keywords: Food", "keywords: Food"]
    assert d["redo"] == d["steps"][0]["batch"]                          # the undo that Redo takes back
    assert {r["id"] for r in d["rows"]} == set(ids[k] for k in ("DSCF0001", "DSCF0002", "IMG_0001", "IMG_0002"))
    picks = client.get("/api/p/t/history/steps", params={"kind": "picks"}).json()
    assert [s["label"] for s in picks["steps"]] == ["pick"] and picks["steps"][0]["ids"] == [ids["DSCF0001"], ids["DSCF0002"]]
    assert [s["label"] for s in client.get("/api/p/t/history/steps", params={"kind": "stars"}).json()["steps"]] == ["4★"]
    assert client.get("/api/p/t/history/steps", params={"kind": "keywords"}).json()["total"] == 2
    assert client.get("/api/p/t/history/steps", params={"kind": "undo"}).json()["total"] == 1
    assert client.get("/api/p/t/history/steps", params={"until": "2001-01-01"}).json()["total"] == 0
    today = time.strftime("%Y-%m-%d")
    assert client.get("/api/p/t/history/steps", params={"since": today, "until": today}).json()["total"] == 4
    page = client.get("/api/p/t/history/steps", params={"limit": 3, "offset": 3}).json()
    assert len(page["steps"]) == 1 and page["total"] == 4
    assert client.get("/api/p/t/history/steps", params={"kind": "nope"}).status_code == 400


@pytest.fixture()
def trashed(lib_web):
    """Two photos moved to a stand-in for this account's Trash by the real Trash flow and log, and
    marked missing as the app does straight after."""
    from joeylab import cleanup

    client, conn, lib, ids = lib_web
    bin_ = Path.home() / ".Trash"
    bin_.mkdir(parents=True, exist_ok=True)

    def move(path):
        dest = bin_ / f"{time.time_ns()}-{os.path.basename(path)}"
        shutil.move(path, dest)
        return str(dest)

    paths = [str(lib / "2023" / "DSCF0001.JPG"), str(lib / "2023" / "DSCF0002.JPG")]
    items = cleanup.plan(conn, paths)
    for it in items:
        it.category = cleanup.LAST
    log = cleanup.trash_files(items, items, "t-files", mover=move)
    conn.execute(f"UPDATE files SET status='missing' WHERE path IN ({','.join('?' * len(paths))})", paths)
    conn.execute("UPDATE shots SET status='missing' WHERE id IN (?, ?)", (ids["DSCF0001"], ids["DSCF0002"]))
    conn.commit()
    yield client, conn, lib, ids, os.path.basename(log), paths
    shutil.rmtree(bin_, ignore_errors=True)


def test_trash_moves_are_listed_and_put_back(trashed):
    client, conn, lib, ids, log, paths = trashed
    assert "DSCF0001" not in _stems(client)
    moves = client.get("/api/p/t/history/trash").json()["moves"]
    assert moves[0]["log"] == log and moves[0]["moved"] == 2 and moves[0]["in_trash"] == 2
    assert {it["path"] for it in moves[0]["items"]} == set(paths)

    r = client.post("/api/p/t/history/putback", json={"log": log, "paths": [paths[0]]}).json()
    assert r["moved"] == 1 and r["results"] == [{"path": paths[0], "result": "put back"}]
    assert os.path.exists(paths[0]) and not os.path.exists(paths[1])
    assert "DSCF0001" in _stems(client)                                # shown again, without a rescan
    assert conn.execute("SELECT status FROM files WHERE path=?", (paths[0],)).fetchone()[0] == "present"
    from joeylab import scanner

    rep = scanner.scan(conn)                                           # and the next update agrees
    assert rep.missing == 0 and conn.execute("SELECT status FROM shots WHERE id=?", (ids["DSCF0001"],)).fetchone()[0] == "present"
    moves = client.get("/api/p/t/history/trash").json()["moves"]
    assert moves[0]["in_trash"] == 1 and {it["state"] for it in moves[0]["items"]} == {"back", "in_trash"}
    assert any(f.startswith("t-putback-") for f in os.listdir(Path(os.environ["JOEYLAB_HOME"]) / "trash-log"))


def test_put_back_never_overwrites_or_leaves_the_library(trashed):
    client, conn, lib, ids, log, paths = trashed
    Path(paths[1]).write_bytes(b"something new")                       # in the way now
    r = client.post("/api/p/t/history/putback", json={"log": log}).json()
    assert r["moved"] == 1
    assert dict((x["path"], x["result"]) for x in r["results"])[paths[1]] == "left alone: something else is at that place now"
    assert Path(paths[1]).read_bytes() == b"something new"
    again = client.post("/api/p/t/history/putback", json={"log": log, "paths": [paths[0]]}).json()
    assert again["results"][0]["result"] == "not in the Trash any more"
    # Only this library's own logs, by name.
    assert client.post("/api/p/t/history/putback", json={"log": "../catalog.db"}).status_code == 400
    assert client.post("/api/p/t/history/putback", json={"log": "other-files-20260101-000000.tsv"}).status_code == 400
    assert client.post("/api/p/t/history/putback", json={"log": log, "paths": ["/etc/hosts"]}).status_code == 400
    # Someone else on the tailnet can't see or use it.
    remote = {"X-Forwarded-For": "100.64.0.9", "Tailscale-User-Login": "guest@example.com"}
    assert client.get("/api/p/t/history/trash", headers=remote).status_code == 403
    assert client.post("/api/p/t/history/putback", json={"log": log}, headers=remote).status_code == 403


def test_put_back_only_into_the_library_from_a_trash(trashed, tmp_path):
    from joeylab import history

    client, conn, lib, ids, log, paths = trashed
    logf = Path(os.environ["JOEYLAB_HOME"]) / "trash-log" / log
    text = logf.read_text()
    outside = tmp_path / "elsewhere" / "x.JPG"
    outside.parent.mkdir()
    logf.write_text(text.replace(paths[0], str(outside)))                # a log edited to point elsewhere
    r = history.put_back(conn, "t", log)
    assert dict((x["path"], x["result"]) for x in r["results"])[str(outside)] == \
        "left alone: that place isn't inside this library's folders"
    assert not outside.exists()
    assert history.in_a_trash(str(Path.home() / ".Trash" / "a.jpg")) and not history.in_a_trash(str(lib / "a.jpg"))


def test_empty_folders_can_be_put_back(lib_web):
    from joeylab import cleanup, history

    client, conn, lib, ids = lib_web
    (lib / "2024" / "To sort").mkdir()
    bin_ = Path.home() / ".Trash"
    bin_.mkdir(parents=True, exist_ok=True)

    def move(path):
        dest = bin_ / f"{time.time_ns()}-{os.path.basename(path)}"
        shutil.move(path, dest)
        return str(dest)

    try:
        roots = cleanup.folder_roots(conn)[0]
        found = [f for f in cleanup.empty_folders(roots) if f.path.endswith("To sort")]
        log = os.path.basename(cleanup.trash_folders(found, roots, "t-folders", mover=move))
        assert not (lib / "2024" / "To sort").exists()
        moves = client.get("/api/p/t/history/trash").json()["moves"]
        assert moves[0]["what"] == "folders" and moves[0]["in_trash"] == 1
        r = history.put_back(conn, "t", log)
        assert r["moved"] == 1 and (lib / "2024" / "To sort").is_dir()
    finally:
        shutil.rmtree(bin_, ignore_errors=True)


def test_plain_language_cameras_and_months(lib_web):
    from joeylab import query

    client, conn, lib, ids = lib_web
    p = query.parse("may 2024 fuji", conn)
    assert p["filter"] == {"from": "2024-05-01", "to": "2024-05-31", "camera": "fujifilm"} and p["text"] == ""
    p = query.parse("iphone 2023", conn)
    assert p["filter"]["camera"] == "iphone" and p["filter"]["from"] == "2023-01-01" and p["text"] == ""
    assert query.parse("photos from june", conn)["filter"] == {"month": 6}
    assert query.parse("canon or iphone", conn)["filter"]["camera"] == "canon,iphone"
    assert query.parse("eos r5 picks", conn)["filter"] == {"status": "picks", "camera": "Canon EOS R5"}
    p = query.parse("that may be nikon", conn)                         # a verb, and a maker not in this library
    assert p["filter"] == {} and p["text"] == "that may be nikon"


def test_keywords_sort_naturally(env):
    from joeylab import db, keywords

    conn = db.connect("t")
    for name in ("Topic 10", "Topic 2", "Topic 1", "topic 21", "Alpha"):
        keywords.ensure(conn, name)
    assert [k["name"] for k in keywords.tree(conn)] == ["Alpha", "Topic 1", "Topic 2", "Topic 10", "topic 21"]


def test_put_back_works_on_a_drive_that_cannot_rename_without_replacing(tmp_path, monkeypatch):
    """exFAT under macOS's FSKit driver (an external SSD) answers renamex_np's RENAME_EXCL with ENOTSUP."""
    import ctypes
    import errno
    import sys

    from joeylab import history

    class NoExcl:
        def renamex_np(self, *a):
            return -1

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: NoExcl())
    monkeypatch.setattr(ctypes, "get_errno", lambda: errno.ENOTSUP)
    src, dst, taken = tmp_path / "a.jpg", tmp_path / "b.jpg", tmp_path / "c.jpg"
    src.write_bytes(b"photo")
    taken.write_bytes(b"other")
    history._rename_excl(str(src), str(dst))
    assert dst.read_bytes() == b"photo" and not src.exists()
    import pytest

    with pytest.raises(FileExistsError):
        history._rename_excl(str(dst), str(taken))
    assert taken.read_bytes() == b"other" and dst.exists()
