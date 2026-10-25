"""Keywords on everything matching a filter (/bulk): a keyword removed since the page loaded comes back
when it's added again, as it does when its name is typed, and an id that never existed is a clear
error rather than a crash."""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import add_root, make_jpeg, run_all


def _library(lib):
    for i in range(2):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    return conn


def _client():
    from joeylab.web.server import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def test_adding_a_removed_keyword_by_id_brings_it_back(env):
    from joeylab import keywords

    _, lib = env
    conn = _library(lib)
    dog = keywords.ensure(conn, "Animals › Dog")
    keywords.delete(conn, dog)
    assert conn.execute("SELECT deleted FROM keywords WHERE id=?", (dog,)).fetchone()[0] == 1
    c = _client()
    r = c.post("/api/p/t/bulk", json={"filter": {}, "keywords_add": [dog]})
    assert r.status_code == 200 and r.json()["n"] == 2
    assert conn.execute("SELECT COALESCE(deleted, 0) FROM keywords WHERE id=?", (dog,)).fetchone()[0] == 0
    tree = c.get("/api/p/t/keywords").json()["tree"]
    assert any(k["path"] == "Animals › Dog" for k in tree)
    # The photos carry a keyword that's in the tree, not a hidden one.
    live = conn.execute("SELECT COUNT(*) FROM shot_keywords sk JOIN keywords k ON k.id = sk.keyword_id"
                        " WHERE COALESCE(k.deleted, 0) = 0").fetchone()[0]
    assert live == 2


def test_an_unknown_keyword_id_is_a_clear_error(env):
    _, lib = env
    _library(lib)
    c = _client()
    for body in ({"keywords_add": [987654]}, {"keywords_remove": ["987654"]}):
        r = c.post("/api/p/t/bulk", json={"filter": {}, **body})
        assert r.status_code == 400 and "keyword" in r.json()["error"].lower()
