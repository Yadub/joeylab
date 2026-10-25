"""RAW + JPEG pairs: the RAWs you don't need go to the Trash, the photo stays with its JPEG and every
decision. Only pairs count; your choice beats the rules; every choice is undone like a rating; and the
move is checked, typed, checked again, logged and put back like every other move to the Trash."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import add_root, make_jpeg, run_all


def _old(p, ago=60):
    t = time.time() - ago
    os.utime(p, (t, t))
    return p


def _raw(path, size=6000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    return _old(path)


def _library(lib):
    """Four pairs (three RAW + JPEG, one RAW + TIFF), a RAW alone and a JPEG alone."""
    from PIL import Image

    for i in (1, 2, 3):
        make_jpeg(lib / "2024" / f"IMG_{i}.JPG", seed=i)
        _raw(lib / "2024" / f"IMG_{i}.NEF")
    make_jpeg(lib / "2023" / "DSC_4.JPG", seed=4)
    Image.open(lib / "2023" / "DSC_4.JPG").save(lib / "2023" / "DSC_4.TIF")
    os.remove(lib / "2023" / "DSC_4.JPG")
    _old(lib / "2023" / "DSC_4.TIF")
    _raw(lib / "2023" / "DSC_4.ARW")
    _raw(lib / "2023" / "DSC_5.NEF")                     # nothing beside it: it is the photo
    make_jpeg(lib / "2023" / "DSC_6.JPG", seed=6)


@pytest.fixture
def pairs(env):
    _, lib = env
    _library(lib)
    add_root(lib)
    conn, _ = run_all()
    ids = {r["stem"]: r["id"] for r in conn.execute("SELECT id, stem FROM shots")}
    return conn, lib, ids


@pytest.fixture
def fake_trash(tmp_path, monkeypatch):
    """Stands in for the real Trash: this test's own ~/.Trash (HOME is a scratch folder in tests)."""
    from joeylab import cleanup

    bin_ = Path.home() / ".Trash"
    bin_.mkdir(parents=True, exist_ok=True)

    def move(path):
        dest = bin_ / f"{time.time_ns()}-{os.path.basename(path)}"
        shutil.move(path, dest)
        return str(dest)

    monkeypatch.setattr(cleanup, "move_to_trash", move)
    yield bin_
    shutil.rmtree(bin_, ignore_errors=True)


def _states(conn, ids):
    from joeylab import raws

    return {stem: s["state"] for stem, s in ((k, raws.status(conn, [v])[v]) for k, v in ids.items())}


def test_only_pairs_count_and_the_rules_keep_by_default(pairs):
    from joeylab import library, raws

    conn, lib, ids = pairs
    st = _states(conn, ids)
    assert st == {"IMG_1": "unkept", "IMG_2": "unkept", "IMG_3": "unkept", "DSC_4": "unkept", "DSC_5": "only", "DSC_6": None}
    s = raws.summary(conn)
    assert s["pairs"]["photos"] == 4 and s["pairs"]["raws"] == 4 and s["pairs"]["bytes"] == 4 * 6000
    assert s["raw_only"] == {"photos": 1, "raws": 1, "bytes": 6000}
    assert s["pairs"]["removable"] == 4 and s["pairs"]["removable_bytes"] == 4 * 6000
    assert {f["path"].rsplit("/", 1)[1]: f["photos"] for f in s["folders"]} == {"2024": 3, "2023": 1}
    assert s["rules"] == raws.RULES

    def matching(**kw):
        return {r["stem"] for r in library.shots(conn, library.Filter(**kw))}

    assert matching(raw="pair") == {"IMG_1", "IMG_2", "IMG_3", "DSC_4"}
    assert matching(raw="only") == {"DSC_5"}
    assert matching(file_kind="anyraw") == {"IMG_1", "IMG_2", "IMG_3", "DSC_4", "DSC_5"}
    assert matching(raw="removable") == {"IMG_1", "IMG_2", "IMG_3", "DSC_4"}
    assert matching(raw="kept") == set()
    # A RAW alone is never a candidate, whatever you choose for it.
    r = raws.choose(conn, [ids["DSC_5"], ids["DSC_6"]], raws.REMOVE)
    assert r["changed"] == [] and r["not_pairs"] == 2 and r["batch"] is None
    assert not [i for i in raws.plan(conn, include_unkept=True) if i.shot_id in (ids["DSC_5"], ids["DSC_6"])]


def test_rules_decide_and_your_choice_beats_them(pairs):
    from joeylab import library, raws

    conn, lib, ids = pairs
    library.decide(conn, [{"id": ids["IMG_1"], "pick": 1}, {"id": ids["IMG_2"], "rating": 3}])
    conn.execute("INSERT INTO viewer_decisions(viewer, shot_id, fav, updated) VALUES ('owner', ?, 1, 0)", (ids["IMG_3"],))
    album = conn.execute("INSERT INTO albums(name, kind) VALUES ('Best', 'manual')").lastrowid
    f = conn.execute("SELECT id FROM files WHERE shot_id=? AND kind='other'", (ids["DSC_4"],)).fetchone()[0]
    conn.execute("INSERT INTO album_items(album_id, file_id, shot_id, name) VALUES (?,?,?,'x')", (album, f, ids["DSC_4"]))
    conn.commit()
    assert _states(conn, {k: ids[k] for k in ("IMG_1", "IMG_2", "IMG_3", "DSC_4")}) == \
        {"IMG_1": "pick", "IMG_2": "stars", "IMG_3": "fav", "DSC_4": "album"}

    raws.set_rules(conn, {"min_stars": 4})
    assert raws.state_of(conn, ids["IMG_2"]) == "unkept"
    raws.set_rules(conn, {"min_stars": 3, "pick": False, "fav": False, "album": False})
    assert _states(conn, {k: ids[k] for k in ("IMG_1", "IMG_2", "IMG_3", "DSC_4")}) == \
        {"IMG_1": "unkept", "IMG_2": "stars", "IMG_3": "unkept", "DSC_4": "unkept"}
    with pytest.raises(ValueError):
        raws.set_rules(conn, {"min_stars": 0})
    with pytest.raises(ValueError):
        raws.set_rules(conn, {"nope": True})
    raws.set_rules(conn, {"pick": True})

    # Edited: develop settings in a sidecar, or a RAW saved well after its JPEG.
    conn.execute("UPDATE shots SET edited=1 WHERE id=?", (ids["IMG_3"],))
    conn.execute("UPDATE files SET mtime_ns = mtime_ns + 7200 * 1000000000 WHERE shot_id=? AND kind='raw'", (ids["DSC_4"],))
    conn.commit()
    assert raws.state_of(conn, ids["IMG_3"]) == "edited" and raws.state_of(conn, ids["DSC_4"]) == "edited"
    raws.set_rules(conn, {"edited": False})
    assert raws.state_of(conn, ids["IMG_3"]) == "unkept"
    raws.set_rules(conn, {"edited": True})

    # Your choice comes first: a picked photo's RAW can go, an unkept one can stay.
    raws.choose(conn, [ids["IMG_1"]], raws.REMOVE)
    raws.choose(conn, [ids["IMG_3"]], raws.KEEP)
    assert raws.state_of(conn, ids["IMG_1"]) == "notneeded" and raws.state_of(conn, ids["IMG_3"]) == "you"
    assert {r["stem"] for r in library.shots(conn, library.Filter(raw="notneeded"))} == {"IMG_1"}
    assert {r["stem"] for r in library.shots(conn, library.Filter(raw="kept"))} == {"IMG_2", "IMG_3", "DSC_4"}
    s = raws.summary(conn)["states"]
    assert s["notneeded"]["photos"] == 1 and s["you"]["photos"] == 1 and s["stars"]["photos"] == 1 and s["edited"]["photos"] == 1
    raws.choose(conn, [ids["IMG_1"]], "")
    assert raws.state_of(conn, ids["IMG_1"]) == "pick"


def test_choices_are_undone_and_redone_like_ratings(pairs):
    from joeylab import library, raws

    conn, lib, ids = pairs
    r = raws.choose(conn, [ids["IMG_1"], ids["IMG_2"]], raws.REMOVE)
    assert sorted(r["changed"]) == sorted([ids["IMG_1"], ids["IMG_2"]])
    library.decide(conn, [{"id": ids["IMG_3"], "rating": 2}])
    raws.choose(conn, [ids["IMG_1"]], raws.KEEP)                          # changed again since
    assert library.history(conn)[0]["label"] == "Keep RAW"
    u = library.undo(conn)                                                # the latest: Keep RAW
    assert u["label"] == "Keep RAW" and raws.choices(conn, [ids["IMG_1"]]) == {ids["IMG_1"]: raws.REMOVE}
    library.undo(conn)                                                    # the rating
    u = library.undo(conn, batch=r["batch"])                              # RAW not needed, on both
    assert raws.choices(conn, [ids["IMG_1"], ids["IMG_2"]]) == {}
    assert raws.state_of(conn, ids["IMG_1"]) == "unkept"
    library.redo(conn)
    assert raws.choices(conn, [ids["IMG_1"], ids["IMG_2"]]) == {ids["IMG_1"]: raws.REMOVE, ids["IMG_2"]: raws.REMOVE}
    # An undo leaves a photo whose choice changed since alone.
    b = raws.choose(conn, [ids["IMG_3"]], raws.KEEP)["batch"]
    raws.choose(conn, [ids["IMG_3"]], raws.REMOVE)
    assert library.undo(conn, batch=b)["skipped"] == 1 and raws.choices(conn, [ids["IMG_3"]]) == {ids["IMG_3"]: raws.REMOVE}


def test_the_check_agrees_with_the_move_and_the_photo_keeps_everything(pairs, fake_trash):
    from joeylab import cleanup, keywords, library, raws

    conn, lib, ids = pairs
    sid = ids["IMG_1"]
    library.decide(conn, [{"id": sid, "rating": 2, "label": "Blue"}])
    keywords.assign(conn, [sid], add=[keywords.ensure(conn, "Holiday")])
    conn.execute("INSERT INTO comments(shot_id, viewer, text, ts) VALUES (?, 'owner', 'print this', 0)", (sid,))
    conn.commit()
    raws.choose(conn, [sid], raws.REMOVE)
    items = raws.plan(conn, include_unkept=True)
    go = raws.going(items)
    assert len(go) == 4 and not raws.skipped(items)
    one = next(i for i in go if i.shot_id == sid)
    assert one.path.endswith("IMG_1.NEF") and one.stays.endswith("IMG_1.JPG") and one.state == "notneeded"
    assert {r["path"].rsplit("/", 1)[1]: r["raws"] for r in raws.by_folder(items)} == {"2024": 3, "2023": 1}
    log = raws.trash(conn, items, "t-files")
    done, size = raws.moved(items)
    assert len(done) == len(go) == 4 and size == 4 * 6000
    for i in go:
        assert not os.path.exists(i.path) and os.path.exists(i.stays)
    assert (lib / "2023" / "DSC_5.NEF").exists() and (lib / "2023" / "DSC_6.JPG").exists()
    # The photo stays, shown from its JPEG, with all you made about it.
    assert conn.execute("SELECT status FROM shots WHERE id=?", (sid,)).fetchone()[0] == "present"
    assert [f["kind"] for f in library.files_of(conn, sid)] == ["jpeg"]
    row = library.shot(conn, sid)
    assert (row["rating"], row["label"], row["preview_kind"]) == (2, "Blue", "jpeg")
    assert [k["name"] for k in keywords.of_shot(conn, sid)] == ["Holiday"]
    assert conn.execute("SELECT COUNT(*) FROM comments WHERE shot_id=?", (sid,)).fetchone()[0] == 1
    assert raws.choices(conn, [sid]) == {sid: raws.REMOVE}                 # the choice stays recorded
    assert raws.status(conn, [sid])[sid]["state"] == "gone"
    assert sid in {r["id"] for r in library.page(conn, library.Filter())[0]}
    assert raws.summary(conn)["pairs"]["photos"] == 0
    # Logged like every move to the Trash, under the name History reads.
    assert os.path.basename(log).startswith("t-files-")
    text = Path(log).read_text()
    assert text.count("moved to the Trash") == 4 and cleanup.DESCRIBE[cleanup.TWIN] in text
    # A later update agrees, and doesn't bring the RAW back.
    from joeylab import db, scanner

    rep = scanner.scan(db.connect("t"))
    assert rep.missing == 0 and conn.execute("SELECT status FROM shots WHERE id=?", (sid,)).fetchone()[0] == "present"


def test_each_raw_is_checked_again_right_before_it_goes(pairs, fake_trash):
    from joeylab import library, raws

    conn, lib, ids = pairs
    items = raws.plan(conn, include_unkept=True)
    assert len(raws.going(items)) == 4
    (lib / "2024" / "IMG_1.JPG").unlink()                                 # the JPEG went since the check
    data = (lib / "2024" / "IMG_2.JPG").read_bytes()
    (lib / "2024" / "IMG_2.JPG").write_bytes(data[: len(data) // 2])      # cut off: can't be read whole
    library.decide(conn, [{"id": ids["IMG_3"], "pick": 1}])                # kept by a rule since the check
    raws.trash(conn, items, "t-files")
    why = {os.path.basename(i.path): i.result for i in items}
    assert why["IMG_1.NEF"] == "left alone: its JPEG is missing"
    assert why["IMG_2.NEF"] == "left alone: its JPEG can't be read whole (damaged or cut off)"
    assert why["IMG_3.NEF"] == "left alone: kept now (picked since the check)"
    assert why["DSC_4.ARW"] == "moved to the Trash"
    for n in ("IMG_1", "IMG_2", "IMG_3"):
        assert (lib / "2024" / f"{n}.NEF").exists()
    assert conn.execute("SELECT status FROM files WHERE path=?", (str(lib / "2024" / "IMG_3.NEF"),)).fetchone()[0] == "present"

    # The check itself skips them, and says why.
    items = raws.plan(conn, include_unkept=True)
    assert dict(raws.skipped(items)) == {"its JPEG is missing": 1, "its JPEG can't be read whole (damaged or cut off)": 1}
    # A RAW changed since JoeyLab read it is left alone too.
    with open(lib / "2024" / "IMG_3.NEF", "ab") as fh:
        fh.write(b"edited")
    raws.choose(conn, [ids["IMG_3"]], raws.REMOVE)
    assert "changed since JoeyLab last read it" in next(i.why for i in raws.plan(conn, include_unkept=True) if i.shot_id == ids["IMG_3"])


def test_a_raw_takes_its_own_sidecars_and_never_the_jpegs(pairs, fake_trash):
    from joeylab import raws

    conn, lib, ids = pairs
    d = lib / "2024"
    plain = '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description xmp:Rating="3"/></rdf:RDF></x:xmpmeta>'
    developed = plain.replace('xmp:Rating="3"', 'crs:ProcessVersion="11.0" crs:HasSettings="True"')
    for name, text in (("IMG_1.xmp", plain), ("IMG_1.NEF.xmp", plain), ("IMG_1.JPG.xmp", developed),
                       ("IMG_2.xmp", developed), ("IMG_3.xmp", plain)):
        (d / name).write_text(text)
    # IMG_3.xmp is shared by another photo of the same name in that folder: it isn't IMG_3.NEF's alone.
    conn.execute("INSERT INTO shots(id, dir, stem, status) VALUES (9999, ?, 'IMG_3', 'present')", (str(d),))
    conn.commit()
    items = {os.path.basename(i.path): i for i in raws.plan(conn, include_unkept=True)}
    assert [os.path.basename(s.path) for s in items["IMG_1.NEF"].sidecars] == ["IMG_1.NEF.xmp", "IMG_1.xmp"]
    assert items["IMG_2.NEF"].why == "kept: it has develop settings in its sidecar"      # edited after all
    assert items["IMG_3.NEF"].sidecars == []
    log = raws.trash(conn, list(items.values()), "t-files")
    assert not (d / "IMG_1.xmp").exists() and not (d / "IMG_1.NEF.xmp").exists()
    assert (d / "IMG_1.JPG.xmp").exists() and (d / "IMG_3.xmp").exists() and (d / "IMG_2.xmp").exists()
    assert Path(log).read_text().count("the RAW's own sidecar") == 2

    # Your own "RAW not needed" beats the edited rule: the RAW goes, with its develop settings.
    raws.choose(conn, [ids["IMG_2"]], raws.REMOVE)
    items = raws.plan(conn, include_unkept=True)
    assert [os.path.basename(s.path) for i in raws.going(items) for s in i.sidecars] == ["IMG_2.xmp"]
    (d / "IMG_2.NEF.xmp").write_text(plain)                                 # a new sidecar since the check
    raws.trash(conn, items, "t-files")
    assert items[0].result == "left alone: its sidecars changed since the check" and (d / "IMG_2.NEF").exists()


def test_put_back_returns_the_raw_and_its_sidecar(pairs, fake_trash):
    from joeylab import history, raws

    conn, lib, ids = pairs
    (lib / "2024" / "IMG_1.xmp").write_text("<x:xmpmeta/>")
    raws.choose(conn, [ids["IMG_1"]], raws.REMOVE)
    items = [i for i in raws.plan(conn, include_unkept=True) if i.shot_id == ids["IMG_1"]]
    log = os.path.basename(raws.trash(conn, items, "t-files"))
    assert not (lib / "2024" / "IMG_1.NEF").exists()
    moves = history.trash_moves("t")
    assert moves[0]["log"] == log and moves[0]["moved"] == 2 and moves[0]["in_trash"] == 2
    r = history.put_back(conn, "t", log)
    assert r["moved"] == 2 and (lib / "2024" / "IMG_1.NEF").exists() and (lib / "2024" / "IMG_1.xmp").exists()
    assert raws.status(conn, [ids["IMG_1"]])[ids["IMG_1"]]["state"] == "notneeded"     # its choice is still yours
    # Old moves are repaired from the same logs without touching these.
    from joeylab import cleanup

    assert cleanup.repair_from_logs(conn, "t")["files"] == 0


def test_the_app_moves_raws_only_for_the_owner_and_only_the_count_typed(pairs, fake_trash):
    from fastapi.testclient import TestClient

    from joeylab.web.server import create_app

    conn, lib, ids = pairs
    c = TestClient(create_app(owner="me@example.com"))
    other = {"Tailscale-User-Login": "guest@example.com", "X-Forwarded-For": "100.64.0.9"}
    s = c.get("/api/p/t/raws").json()
    assert s["pairs"]["photos"] == 4 and s["raw_only"]["photos"] == 1
    assert c.post("/api/p/t/raws/trash/plan", json={}, headers=other).status_code == 403
    assert c.post("/api/p/t/raws/choose", json={"ids": [ids["IMG_1"]], "choice": "keep"}, headers=other).status_code == 403
    assert c.post("/api/p/t/raws/rules", json={"pick": False}, headers=other).status_code == 403

    r = c.post("/api/p/t/raws/choose", json={"filter": {"folder": str(lib / "2024")}, "choice": "keep"}).json()
    assert len(r["changed"]) == 3 and r["status"][str(ids["IMG_1"])]["state"] == "you"
    assert c.post("/api/p/t/undo", json={"batch": r["batch"]}).json()["undone"] == "Keep RAW"
    assert c.get("/api/p/t/raws/status", params={"ids": ids["IMG_1"]}).json()["status"][str(ids["IMG_1"])]["state"] == "unkept"
    shown = c.get("/api/p/t/shots", params={"raw": "removable"}).json()
    assert shown["total"] == 4

    j = c.post("/api/p/t/raws/trash/plan", json={"filter": {"raw": "removable", "folder": str(lib / "2024")}, "include_unkept": True}).json()
    j = _wait(c, f"/api/p/t/raws/trash/{j['token']}")
    assert j["state"] == "ready" and j["go"]["raws"] == 3 and len(j["items"]) == 3
    assert c.get(f"/api/p/t/raws/trash/{j['token']}/list.tsv").text.count("IMG_") >= 6
    run = f"/api/p/t/raws/trash/{j['token']}/run"
    assert c.post(f"/api/p/t/trash/{j['token']}/run", json={"count": 3, "include_last": True}).status_code == 404
    assert c.post(run, json={"count": 4}).status_code == 400
    assert c.post(run, json={"count": 3}, headers=other).status_code == 403
    assert c.post(run, json={"count": "3"}).status_code == 200
    done = _wait(c, f"/api/p/t/raws/trash/{j['token']}")
    assert done["state"] == "done" and done["result"]["moved"] == 3 and done["result"]["bytes"] == 3 * 6000
    assert c.post(run, json={"count": 3}).status_code == 409                # a check is used once
    assert (lib / "2023" / "DSC_4.ARW").exists() and not (lib / "2024" / "IMG_2.NEF").exists()
    assert c.get("/api/p/t/raws").json()["pairs"]["photos"] == 1
    assert c.get("/api/p/t/history/trash").json()["moves"][0]["moved"] == 3


def _wait(c, url):
    for _ in range(300):
        j = c.get(url).json()
        if j["state"] not in ("checking", "moving"):
            return j
        time.sleep(0.05)
    raise AssertionError("never finished")


def test_the_command_line_lists_and_moves_after_the_count(pairs, fake_trash):
    from joeylab.cli import app

    conn, lib, ids = pairs
    runner = CliRunner()
    r = runner.invoke(app, ["raw-list", "-p", "t", "--include-unkept"])
    assert r.exit_code == 0, r.output
    assert "4 RAWs" in r.output and "Nothing was changed" in r.output and (lib / "2024" / "IMG_1.NEF").exists()
    r = runner.invoke(app, ["raw-list", "-p", "t", "--trash", "--include-unkept"], input="3\n")
    assert r.exit_code == 1 and "Nothing was moved" in r.output and (lib / "2024" / "IMG_1.NEF").exists()
    r = runner.invoke(app, ["raw-list", "-p", "t", "--trash", "--include-unkept"], input="4\n")
    assert r.exit_code == 0, r.output
    assert "Moved 4 RAWs" in r.output and not (lib / "2024" / "IMG_1.NEF").exists() and (lib / "2024" / "IMG_1.JPG").exists()
    assert (lib / "2023" / "DSC_5.NEF").exists()


def test_figures_stay_quick_with_thousands_of_pairs(env):
    from joeylab import db, library, raws

    conn = db.connect("big")
    rows_s, rows_f = [], []
    for i in range(1, 4001):
        rows_s.append((i, f"/x/{2000 + i % 20}", f"IMG_{i}", f"{2000 + i % 20}-01-01 10:00:00"))
        rows_f.append((2 * i, f"/x/{2000 + i % 20}/IMG_{i}.NEF", i, "raw", ".nef", 25_000_000))
        if i % 10:
            rows_f.append((2 * i + 1, f"/x/{2000 + i % 20}/IMG_{i}.JPG", i, "jpeg", ".jpg", 5_000_000))
    conn.executemany("INSERT INTO shots(id, dir, stem, capture_str, status) VALUES (?,?,?,?, 'present')", rows_s)
    conn.executemany("INSERT INTO files(id, path, shot_id, kind, ext, size, mtime_ns, fp) VALUES (?,?,?,?,?,?,0,'x')", rows_f)
    conn.executemany("INSERT INTO decisions(shot_id, pick, rating) VALUES (?, 1, 0)", [(i,) for i in range(1, 4001, 7)])
    conn.commit()
    t = time.time()
    s = raws.summary(conn)
    rows, total = library.page(conn, library.Filter(raw="removable"), limit=200)
    assert time.time() - t < 3
    assert s["pairs"]["photos"] == 3600 and s["raw_only"]["photos"] == 400 and len(s["years"]) == 18          # 2000 and 2010 hold only RAWs alone
    assert s["states"]["pick"]["photos"] + s["pairs"]["removable"] == 3600 and total == s["pairs"]["removable"]


def test_only_the_raws_you_rejected_go_unless_asked(pairs, fake_trash):
    """Reject RAW is what sends a RAW to the Trash; one no rule keeps stays unless include_unkept."""
    from joeylab import raws

    conn, lib, ids = pairs
    assert raws.going(raws.plan(conn)) == []                         # nothing rejected yet: nothing goes
    raws.choose(conn, [ids["IMG_1"], ids["IMG_2"]], raws.REMOVE)
    go = raws.going(raws.plan(conn))
    assert sorted(i.shot_id for i in go) == sorted([ids["IMG_1"], ids["IMG_2"]])
    assert len(raws.going(raws.plan(conn, include_unkept=True))) == 4
    # Taking a rejection back between the check and the move keeps that RAW.
    items = raws.plan(conn)
    raws.choose(conn, [ids["IMG_2"]], "")
    raws.trash(conn, items, "t-files")
    assert not (lib / "2024" / "IMG_1.NEF").exists() and (lib / "2024" / "IMG_2.NEF").exists()
    left = next(i for i in items if i.shot_id == ids["IMG_2"])
    assert "took back Reject RAW" in left.result


def test_counts_for_an_event_or_selection_by_state(pairs):
    from fastapi.testclient import TestClient

    from joeylab import library, raws
    from joeylab.web.server import create_app

    conn, lib, ids = pairs
    library.decide(conn, [{"id": ids["IMG_3"], "pick": 1}])
    raws.choose(conn, [ids["IMG_1"]], raws.REMOVE)
    c = raws.count(conn, list(ids.values()))
    assert c["pairs"] == 4 and c["bytes"] == 4 * 6000
    st = c["states"]
    assert st["notneeded"]["ids"] == [ids["IMG_1"]] and st["pick"]["ids"] == [ids["IMG_3"]]
    assert sorted(st["unkept"]["ids"]) == sorted([ids["IMG_2"], ids["DSC_4"]])
    client = TestClient(create_app())
    r = client.post("/api/p/t/raws/count", json={"filter": {"folder": str(lib / "2024"), "all": 1}}).json()
    assert r["pairs"] == 3 and set(r["states"]) == {"notneeded", "pick", "unkept"}
