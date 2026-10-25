"""Web API tests: browsing, decisions, images, folders, jobs, exports and access control."""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import add_root, make_jpeg, run_all


@pytest.fixture()
def web(env):
    tmp, lib = env
    for i in range(5):
        make_jpeg(lib / "a" / f"IMG_{i:04d}.JPG", seed=i)
    make_jpeg(lib / "b" / "IMG_9000.JPG", seed=3, blur=8)
    add_root(lib)
    conn, _ = run_all()
    from joeylab.web.jobs import JobManager
    from joeylab.web.server import create_app

    jobs = JobManager(watch_minutes=0, faces=False, ai_layer=False)
    client = TestClient(create_app(jobs=jobs))
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    return client, tmp, lib, ids


def test_index_and_projects(web):
    client, *_ = web
    assert client.get("/").status_code == 200
    ps = client.get("/api/projects").json()
    assert ps[0]["name"] == "t" and ps[0]["photos"] == 6


def test_overview(web):
    client, *_ = web
    o = client.get("/api/p/t/overview").json()
    assert o["counts"]["photos"] == 6 and o["counts"]["undecided"] == 6
    assert "blurry" in o["flags"]
    assert o["timeline"] is None or o["timeline"]["buckets"]
    assert client.get("/api/p/nope/overview").status_code == 404


def test_shots_paging_filters_and_stacks(web):
    client, _, lib, ids = web
    r = client.get("/api/p/t/shots", params={"limit": 2, "offset": 0}).json()
    assert r["total"] == 6 and len(r["rows"]) == 2 and "ar" in r["rows"][0] and "v" in r["rows"][0]
    r = client.get("/api/p/t/shots", params={"flag": "blurry"}).json()
    assert [x["stem"] for x in r["rows"]] == ["IMG_9000"]
    r = client.get("/api/p/t/shots", params={"folder": str(lib / "b")}).json()
    assert r["total"] == 1
    r = client.get("/api/p/t/shots", params={"stack": 1}).json()
    assert r["total"] <= 6 and all("stack_n" in x for x in r["rows"])
    st = client.get("/api/p/t/stacks", params={"status": "undecided"}).json()["stacks"]
    assert sum(len(s["ids"]) for s in st) == 6
    rows = client.get("/api/p/t/rows", params={"ids": f"{ids[1]},{ids[0]}"}).json()["rows"]
    assert [x["id"] for x in rows] == [ids[1], ids[0]]


def test_decide_undo_and_accept(web):
    client, _, _, ids = web
    r = client.post("/api/p/t/decide", json={"items": [{"id": ids[0], "rating": 4, "pick": 1}]}).json()
    assert r["rows"][0]["rating"] == 4 and r["rows"][0]["pick"] == 1
    assert client.get("/api/p/t/shots", params={"status": "picks"}).json()["total"] == 1
    client.post("/api/p/t/decide", json={"items": [{"id": ids[0], "rating": 0, "pick": 0}]})
    assert client.get("/api/p/t/shots", params={"status": "undecided"}).json()["total"] == 6
    assert client.post("/api/p/t/decide", json={"items": [{"id": ids[0], "rating": 9}]}).status_code == 400
    acc = client.post("/api/p/t/accept", json={"filter": {}}).json()
    assert acc["n"] >= 1
    decided = client.get("/api/p/t/shots", params={"status": "undecided"}).json()["total"]
    assert decided < 6
    # Undo lives on the server: it survives a reload and reverses the whole accept as one step.
    u = client.post("/api/p/t/undo", json={}).json()
    assert u["undone"].startswith("accept")
    assert client.get("/api/p/t/shots", params={"status": "undecided"}).json()["total"] == 6
    hist = client.get("/api/p/t/history").json()
    assert hist["steps"][0]["undone"] == 0 and any(s["undone"] for s in hist["steps"])
    # Color labels
    client.post("/api/p/t/decide", json={"items": [{"id": ids[1], "label": "Blue"}]})
    assert client.get("/api/p/t/shots", params={"label": "blue"}).json()["total"] == 1
    assert client.post("/api/p/t/decide", json={"items": [{"id": ids[1], "label": "Teal"}]}).status_code == 400


def test_shot_detail(web):
    client, _, _, ids = web
    d = client.get(f"/api/p/t/shot/{ids[0]}").json()
    assert d["row"]["id"] == ids[0] and d["files"][0]["kind"] == "jpeg" and d["metrics"]["sharp_subject"] > 0
    assert client.get("/api/p/t/shot/999999").status_code == 404


def test_images(web):
    client, _, _, ids = web
    for url in (f"/img/t/t/{ids[0]}", f"/img/t/t/{ids[0]}?s=m", f"/img/t/v/{ids[0]}", f"/img/t/z/{ids[0]}"):
        r = client.get(url)
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg", url
        assert "immutable" in r.headers["cache-control"]
    small = Image.open(__import__("io").BytesIO(client.get(f"/img/t/t/{ids[0]}").content))
    assert max(small.size) <= 400
    fid = client.get(f"/api/p/t/shot/{ids[0]}").json()["files"][0]["id"]
    assert client.get(f"/img/t/o/{fid}").status_code == 200
    assert client.get("/img/t/v/999999").status_code == 404


def test_folders_and_fs(web, tmp_path):
    client, tmp, lib, _ = web
    roots = client.get("/api/p/t/roots").json()
    assert roots[0]["available"] and roots[0]["files"] == 6
    assert client.post("/api/p/t/roots", json={"path": "/definitely/not/here", "scan": False}).status_code == 400
    assert client.get("/api/fs", params={"path": "/etc"}).status_code == 400
    home = client.get("/api/fs").json()
    assert home["path"] == str(os.path.expanduser("~")) or home["path"].startswith("/")


def test_exports(web):
    client, tmp, lib, ids = web
    client.post("/api/p/t/decide", json={"items": [{"id": ids[0], "pick": 1}, {"id": ids[1], "pick": 1}]})
    dest = tmp / "Album"
    r = client.post("/api/p/t/export/album", json={"filter": {"status": "picks"}, "dest": str(dest)}).json()
    assert r["created"] == 2 and all(os.path.islink(dest / n) for n in os.listdir(dest))
    r = client.post("/api/p/t/export/album", json={"ids": [ids[2]], "dest": str(tmp / "Sel")}).json()
    assert r["created"] == 1
    bad = client.post("/api/p/t/export/album", json={"dest": str(tmp / "home" / "x")})
    assert bad.status_code == 400
    txt = client.get("/api/p/t/export/list", params={"status": "picks"}).text
    assert len(txt.strip().splitlines()) == 2
    csv_ = client.get("/api/p/t/export/csv").text
    assert csv_.splitlines()[0].startswith("id,") and len(csv_.splitlines()) == 7
    assert client.post("/api/p/t/export/xmp", json={}).status_code == 400
    r = client.post("/api/p/t/export/xmp", json={"confirm": True}).json()
    assert r["written"] == 2


def test_job_runs_pipeline_after_changes(web):
    client, tmp, lib, _ = web
    make_jpeg(lib / "c" / "NEW_0001.JPG", seed=42)
    client.post("/api/p/t/jobs", json={})
    for _ in range(200):
        j = client.get("/api/p/t/jobs/current").json()
        if j["job"] and j["job"]["state"] != "running":
            break
        time.sleep(0.1)
    assert j["job"]["state"] == "done", j["job"]["log"]
    assert j["version"] == 2               # once the new photo is stacked and scored, and again at the end
    assert j["job"]["ready"][:2] == ["analyse", "group"] and j["job"]["untouched"]["ok"]
    assert client.get("/api/p/t/overview").json()["counts"]["photos"] == 7


def test_create_project(web):
    client, tmp, lib, _ = web
    assert client.post("/api/projects", json={"name": "api", "path": str(lib)}).status_code == 400
    assert client.post("/api/projects", json={"name": "t", "path": str(lib)}).status_code == 409
    assert client.post("/api/projects", json={"name": "new", "path": "/nope"}).status_code == 400
    inside = client.post("/api/projects", json={"name": "new", "path": str(lib / "a")})     # "t" has it: one library each
    assert inside.status_code == 400 and "already includes" in inside.json()["error"]
    make_jpeg(tmp / "other" / "IMG_1.JPG", seed=11)
    r = client.post("/api/projects", json={"name": "new", "path": str(tmp / "other")})
    assert r.status_code == 200 and r.json()["name"] == "new"
    for _ in range(300):   # let its import finish before the test folder is deleted
        j = client.get("/api/p/new/jobs/current").json()["job"]
        if j and j["state"] != "running":
            break
        time.sleep(0.1)
    assert j["state"] == "done", j["log"]


def test_access_control(env):
    from joeylab.web.server import create_app

    app = create_app()
    assert TestClient(app, client=("8.8.8.8", 5000)).get("/api/projects").status_code == 403
    assert TestClient(app, client=("100.101.102.103", 5000)).get("/api/projects").status_code == 403   # not the owner
    assert TestClient(app, client=("127.0.0.1", 5000)).get("/api/projects").status_code == 200

    tapp = create_app(token="s3cret")
    c = TestClient(tapp, client=("127.0.0.1", 5000))
    assert c.get("/api/projects").status_code == 401
    r = c.get("/?token=s3cret", follow_redirects=False)
    assert r.status_code == 303 and "joeylab_token" in r.headers["set-cookie"]
    c.cookies.set("joeylab_token", "s3cret")
    assert c.get("/api/projects").status_code == 200
