"""Which copy of a duplicate is kept, the one flow from rejecting extras to the Trash, and events,
stacks and Home made from kept copies only."""

from __future__ import annotations

import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from conftest import add_root, jpeg_at, make_jpeg, run_all


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


def _wait(c, url):
    for _ in range(200):
        j = c.get(url).json()
        if j["state"] not in ("checking", "moving"):
            return j
        time.sleep(0.05)
    raise AssertionError("never finished")


def _copy(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _archive(lib):
    """An organised library with its usual copies: a backup (shallower than the originals), a
    desktop dump holding some of them among photos of its own, and a selection picked from a card."""
    lisbon = lib / "Photos" / "2024" / "2024-05 Lisbon"
    for i in range(5):
        jpeg_at(lisbon / f"DSCF{i}.JPG", seed=i, when=f"2024:05:0{i + 1} 12:00:00")
        _copy(lisbon / f"DSCF{i}.JPG", lib / "Backup" / f"DSCF{i}.JPG")
    for i in range(3):
        _copy(lisbon / f"DSCF{i}.JPG", lib / "Desktop dump" / f"DSCF{i}.JPG")
    for i in range(3):
        jpeg_at(lib / "Desktop dump" / f"IMG_{i}.JPG", seed=40 + i, when=f"2023:01:0{i + 1} 09:00:00")
    card = lib / "DCIM" / "100_FUJI"
    for i in range(6):
        jpeg_at(card / f"DSCF7{i}.JPG", seed=20 + i, when=f"2025:08:0{i + 1} 10:00:00")
    for i in range(3):
        _copy(card / f"DSCF7{i}.JPG", lib / "Wedding picks" / f"DSCF7{i}.JPG")


def _kept_dirs(conn):
    return {os.path.relpath(os.path.dirname(r[0]), conn.execute("SELECT path FROM roots").fetchone()[0])
            for r in conn.execute("SELECT f.path FROM dup_members dm JOIN files f ON f.id = dm.file_id WHERE dm.keep = 1")}


def test_the_organised_folder_is_kept_not_the_backup_dump_or_selection(env):
    from joeylab import dupes

    _, lib = env
    _archive(lib)
    add_root(lib)
    conn, _ = run_all()
    # The old rule (shallowest path) kept "Backup" and "Desktop dump" and emptied 2024-05 Lisbon.
    assert _kept_dirs(conn) == {"Photos/2024/2024-05 Lisbon", "DCIM/100_FUJI"}
    groups = {tuple(os.path.basename(f["path"]) for f in g["folders"]): g for g in dupes.folder_groups(conn)["groups"]}
    g = groups[("Backup", "Desktop dump", "2024-05 Lisbon")]
    per = {os.path.basename(f["path"]): (f["kept_here"], f["removed_here"]) for f in g["folders"]}
    assert per == {"2024-05 Lisbon": (3, 0), "Backup": (0, 3), "Desktop dump": (0, 3)}

    # "Always keep copies under Backup": every set, now, and through a full recompute.
    assert dupes.set_keep_rule(conn, str(lib / "Backup")) == 5
    assert _kept_dirs(conn) == {"Backup", "DCIM/100_FUJI"}
    run_all()
    assert _kept_dirs(conn) == {"Backup", "DCIM/100_FUJI"} and dupes.keep_rules(conn) == [str(lib / "Backup")]
    assert dupes.set_keep_rule(conn, str(lib / "Backup"), on=False) == 5
    assert _kept_dirs(conn) == {"Photos/2024/2024-05 Lisbon", "DCIM/100_FUJI"} and dupes.keep_rules(conn) == []

    from joeylab.web.server import create_app

    c = TestClient(create_app())
    r = c.post("/api/p/t/dupes/rules", json={"folder": str(lib / "Desktop dump")}).json()
    assert r["rules"] == [str(lib / "Desktop dump")] and r["changed"] == 3
    assert c.get("/api/p/t/dupes/groups").json()["rules"] == [str(lib / "Desktop dump")]
    assert c.post("/api/p/t/dupes/rules", json={"folder": "/somewhere/else"}).status_code == 400
    assert c.post("/api/p/t/dupes/rules", json={"folder": str(lib / "Desktop dump"), "on": False}).json()["rules"] == []


def test_rejecting_extras_leads_to_a_trash_step_with_just_those_extras(env, fake_trash):
    from joeylab import library
    from joeylab.web.server import create_app

    _, lib = env
    _archive(lib)
    add_root(lib)
    conn, _ = run_all()
    c = TestClient(create_app())
    groups = c.get("/api/p/t/dupes/groups").json()["groups"]
    g = next(g for g in groups if len(g["folders"]) == 3)
    other = conn.execute("SELECT id FROM shots WHERE stem='IMG_0'").fetchone()[0]
    library.decide(conn, [{"id": other, "pick": -1}])                     # a reject that isn't a duplicate

    dry = c.post("/api/p/t/dupes/reject-extras", json={"set_ids": g["set_ids"], "dry": True}).json()
    assert dry["to_reject"] == 6 and dry["files"] == 6
    folders = {os.path.basename(f["path"]): (f["loses"], f["keeps"]) for f in dry["folders"]}
    assert folders == {"Backup": (3, 2), "Desktop dump": (3, 3)}
    assert c.post("/api/p/t/dupes/reject-extras", json={"set_ids": g["set_ids"]}).json()["rejected"] == 6
    again = c.post("/api/p/t/dupes/reject-extras", json={"set_ids": g["set_ids"], "dry": True}).json()
    assert again["to_reject"] == 0 and again["already"] == 6 and again["files"] == 6          # still offered the Trash

    j = _wait(c, "/api/p/t/trash/" + c.post("/api/p/t/trash/plan", json={"what": "rejects", "set_ids": g["set_ids"]}).json()["token"])
    assert j["total"] == 6 and j["go"]["copies"] == 6 and j["scope"]["photos"] == 6
    assert {os.path.basename(f["path"]): (f["copies"], f["total"]) for f in j["by_folder"]} == {"Backup": (3, 5), "Desktop dump": (3, 6)}
    assert c.post(f"/api/p/t/trash/{j['token']}/run", json={"count": 6}).status_code == 200
    done = _wait(c, f"/api/p/t/trash/{j['token']}")
    assert done["result"]["moved"] == 6 and len(done["result"]["gone"]) == 6
    assert (lib / "Desktop dump" / "IMG_0.JPG").exists()                   # rejected, but not one of these extras
    assert all((lib / "Photos" / "2024" / "2024-05 Lisbon" / f"DSCF{i}.JPG").exists() for i in range(5))

    ids = {r["id"] for r in c.get("/api/p/t/shots", params={"all": 1, "limit": 500}).json()["rows"]}
    assert not ids & set(done["result"]["gone"])                          # gone from the app at once
    left = [os.path.basename(f["path"]) for g in c.get("/api/p/t/dupes/groups").json()["groups"] for f in g["folders"]]
    assert sorted(left) == ["100_FUJI", "2024-05 Lisbon", "Backup", "Wedding picks"]      # those three sets are gone


def test_events_and_stacks_are_made_from_kept_copies(env):
    from joeylab import events, grouping
    from joeylab.web.server import create_app

    _, lib = env
    for i in range(6):                                  # one afternoon, five minutes apart
        jpeg_at(lib / "2024-05 Lisbon" / f"DSCF{i}.JPG", seed=i, when=f"2024:05:01 12:{i * 5:02d}:00")
        _copy(lib / "2024-05 Lisbon" / f"DSCF{i}.JPG", lib / "Desktop dump" / f"DSCF{i}.JPG")
        _copy(lib / "2024-05 Lisbon" / f"DSCF{i}.JPG", lib / "Lisbon copy" / f"DSCF{i}.JPG")
    add_root(lib)
    conn, _ = run_all()
    events.run(conn)
    evs = events.listing(conn)
    assert len(evs) == 1 and evs[0]["n"] == 6 and evs[0]["name"].startswith("Lisbon")
    extras = {r[0] for r in conn.execute("SELECT shot_id FROM dup_members WHERE keep = 0")}
    assert len(extras) == 12
    assert not conn.execute(f"SELECT COUNT(*) FROM shots WHERE event_id IS NOT NULL AND id IN ({','.join(map(str, extras))})").fetchone()[0]
    lines, _ = grouping.timeline(conn)
    assert sum(len(v) for v in lines.values()) == 6                      # a copy is not another frame
    c = TestClient(create_app())
    stacked = [i for s in c.get("/api/p/t/stacks").json()["stacks"] for i in s["ids"]]
    assert len(stacked) == 6 and not set(stacked) & extras


def test_events_rename_merge_and_hide_hold_through_an_update(env):
    from joeylab import events
    from joeylab.web.server import create_app

    _, lib = env
    for day, place in ((1, "Beach"), (4, "Market"), (7, "Castle")):
        for i in range(4):
            jpeg_at(lib / place / f"IMG_{day}{i}.JPG", seed=day * 10 + i, when=f"2024:06:0{day} 1{i}:00:00")
    add_root(lib)
    conn, _ = run_all()
    events.run(conn)
    evs = sorted(events.listing(conn), key=lambda e: e["start_ts"])
    assert len(evs) == 3
    c = TestClient(create_app())
    first, _, last = evs
    near = c.get(f"/api/p/t/events/{first['id']}/neighbours").json()["events"]
    assert [e["id"] for e in near] == [evs[1]["id"], last["id"]]
    assert c.post(f"/api/p/t/events/{first['id']}/name", json={"name": "Long weekend"}).status_code == 200
    r = c.post(f"/api/p/t/events/{first['id']}/merge", json={"other": last["id"]}).json()
    assert r["merged"] == 2                                              # the one between joins too
    merged = c.get("/api/p/t/events").json()["events"]
    assert len(merged) == 1 and merged[0]["id"] == first["id"] and merged[0]["n"] == 12 and merged[0]["name"] == "Long weekend"
    run_all()
    events.run(conn)                                                    # the merge and the name hold
    after = events.listing(conn)
    assert [(e["id"], e["n"], e["name"]) for e in after] == [(first["id"], 12, "Long weekend")]

    assert c.post(f"/api/p/t/events/{first['id']}/hidden", json={"hidden": True}).status_code == 200
    d = c.get("/api/p/t/events").json()
    assert d["events"] == [] and d["hidden"] == 1
    assert [e["id"] for e in c.get("/api/p/t/events", params={"hidden": 1}).json()["events"]] == [first["id"]]
    assert c.post(f"/api/p/t/events/{first['id']}/merge", json={"other": first["id"]}).status_code == 400


def test_an_event_merge_can_be_undone(env):
    from joeylab import events
    from joeylab.web.server import create_app

    _, lib = env
    for day, place in ((1, "Beach"), (4, "Market"), (7, "Castle")):
        for i in range(4):
            jpeg_at(lib / place / f"IMG_{day}{i}.JPG", seed=day * 10 + i, when=f"2024:06:0{day} 1{i}:00:00")
    add_root(lib)
    conn, _ = run_all()
    events.run(conn)
    first, middle, last = sorted(events.listing(conn), key=lambda e: e["start_ts"])
    c = TestClient(create_app())
    c.post(f"/api/p/t/events/{middle['id']}/name", json={"name": "Market day"})
    r = c.post(f"/api/p/t/events/{first['id']}/merge", json={"other": last["id"]}).json()
    assert r["merged"] == 2 and len(r["undo"]["joins"]) == 2
    merged = events.listing(conn)
    assert len(merged) == 1 and not merged[0]["name"].startswith("Trip")      # an event is named as an event
    back = c.post("/api/p/t/events/unmerge", json={"undo": r["undo"]})
    assert back.status_code == 200 and back.json()["restored"] == 2
    after = {e["id"]: (e["n"], e["name"]) for e in events.listing(conn)}
    assert set(after) == {first["id"], middle["id"], last["id"]}
    assert after[middle["id"]] == (4, "Market day") and after[first["id"]][0] == after[last["id"]][0] == 4
    assert not events.joins(conn, "event")                                # the merge no longer holds...
    events.run(conn)                                                      # ...so an update keeps them apart
    assert {e["id"]: (e["n"], e["name"]) for e in events.listing(conn)} == after
    assert c.post("/api/p/t/events/unmerge", json={"undo": "x"}).status_code == 400


def test_library_hides_extras_in_archives_and_says_how_many(env):
    from joeylab import db
    from joeylab.web.server import create_app

    _, lib = env
    for i in range(3):
        make_jpeg(lib / "Photos" / f"IMG_{i}.JPG", seed=i)
        _copy(lib / "Photos" / f"IMG_{i}.JPG", lib / "Backup" / f"IMG_{i}.JPG")
    add_root(lib)
    conn, _ = run_all()
    c = TestClient(create_app())
    r = c.get("/api/p/t/shots").json()
    assert r["total"] == 6 and r["duplicates"] == {"hidden": False, "n": 3}          # a shoot shows everything
    db.set_setting(conn, "purpose", "archive")
    r = c.get("/api/p/t/shots").json()
    assert r["total"] == 3 and r["duplicates"] == {"hidden": True, "n": 3}           # an archive hides the extras
    assert c.get("/api/p/t/settings").json()["hide_duplicates"] is True
    c.post("/api/p/t/settings", json={"hide_duplicates": False})
    assert c.get("/api/p/t/shots").json()["total"] == 6
    assert c.get("/api/p/t/shots", params={"flag": "duplicate"}).json()["total"] == 3


def test_home_falls_back_when_a_years_best_frame_is_tiny(env):
    from joeylab.web.server import create_app

    _, lib = env
    jpeg_at(lib / "IMG_1.JPG", seed=1, when="2019:03:01 10:00:00")
    jpeg_at(lib / "IMG_2.JPG", seed=2, when="2019:03:01 10:00:01")
    add_root(lib)
    conn, _ = run_all()
    a, b = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    gid = conn.execute("SELECT group_id FROM shots WHERE id=?", (a,)).fetchone()[0]
    conn.execute("UPDATE shots SET group_id=? WHERE id=?", (gid, b))
    conn.execute("UPDATE metrics SET flags='[\"tiny\"]', group_rank=1 WHERE shot_id=?", (a,))
    conn.execute("UPDATE metrics SET flags='[]', group_rank=2 WHERE shot_id=?", (b,))
    conn.commit()
    years = TestClient(create_app()).get("/api/p/t/home").json()["years"]
    assert years == [{"year": "2019", "n": 1, "best": [b]}]
