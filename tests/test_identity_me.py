"""Who "me" is: one per install, used by search ("me in 2023"), kept through a rename, and only the
owner can set it."""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import add_root, make_jpeg, run_all


def test_me_is_yours_and_search_understands_it(env):
    from joeylab import identity, query
    from joeylab.web.server import create_app

    _, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    sid = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    ana = conn.execute("INSERT INTO people(name) VALUES ('Ana')").lastrowid
    bo = conn.execute("INSERT INTO people(name) VALUES ('Bo')").lastrowid
    for s, pid in ((sid[0], ana), (sid[1], bo), (sid[2], bo)):
        conn.execute("INSERT INTO faces(shot_id, x, y, w, h, person_id, person_src) VALUES (?, .1, .1, .1, .1, ?, 'user')", (s, pid))
    conn.commit()
    c = TestClient(create_app(owner="me@example.com"))

    assert c.get("/api/p/t/me").json()["person_id"] is None
    assert c.post("/api/p/t/me", json={"person_id": bo}).json()["name"] == "Bo"
    assert identity.person_id(conn, None) == bo
    assert c.get("/api/p/t/people").json()["people"][0]["id"] == bo           # you first
    parsed = c.get("/api/p/t/query/parse", params={"q": "me"}).json()
    assert parsed["filter"].get("people") == str(bo) and "you" in parsed["chips"]
    assert query.parse("person:me", conn, me=bo)["filter"]["people"] == str(bo)
    assert "people" not in query.parse("me", conn)["filter"]                  # nobody set: "me" means nothing

    # One user: nobody else can set it (or see anything).
    guest = {"Tailscale-User-Login": "guest@example.com", "X-Forwarded-For": "100.64.0.9"}
    assert c.post("/api/p/t/me", json={"person_id": ana}, headers=guest).status_code == 403
    phone = {"Tailscale-User-Login": "me@example.com", "X-Forwarded-For": "100.64.0.8"}
    assert c.get("/api/p/t/me", headers=phone).json()["person_id"] == bo          # the same "me" on your phone

    # A rename keeps "me"; clearing works.
    c.post(f"/api/p/t/people/{bo}/rename", json={"name": "Bo Smith"})
    assert identity.name_of(None) == "Bo Smith" and identity.person_id(conn, None) == bo
    c.post("/api/p/t/me", json={"person_id": None})
    assert identity.person_id(conn, None) is None
