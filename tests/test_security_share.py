"""Share links: the share-only server answers nothing but share pages, and event or trip links keep
the photos they were made with."""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from conftest import add_root, make_jpeg, run_all


@pytest.fixture()
def shared(env):
    """A library of four photos: two in one event, two in another, an album of one, and a link to each."""
    _, lib = env
    for i in range(4):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    now = time.time()
    for eid, members in ((1, ids[:2]), (2, ids[2:])):
        conn.execute("INSERT INTO events(id, name, kind, start_ts, end_ts, n) VALUES (?,?,?,?,?,?)",
                     (eid, f"Event {eid}", "event", now, now, len(members)))
        conn.executemany("UPDATE shots SET event_id=? WHERE id=?", [(eid, i) for i in members])
    conn.commit()
    from joeylab.web.server import create_app

    c = TestClient(create_app())
    aid = c.post("/api/p/t/albums", json={"name": "Best", "ids": ids[:1]}).json()["id"]
    album = c.post("/api/p/t/shares", json={"kind": "album", "ref": aid}).json()["token"]
    event = c.post("/api/p/t/shares", json={"kind": "event", "ref": 1}).json()["token"]
    return c, conn, ids, album, event


def _ids(c, token):
    return [r["id"] for r in c.get(f"/api/share/{token}").json()["rows"]]


def test_an_event_link_keeps_its_photos_when_events_are_regrouped(shared):
    c, conn, ids, _, event = shared
    assert sorted(_ids(c, event)) == ids[:2]
    assert c.get(f"/api/share/{event}").json()["title"] == "Event 1"
    # An update regroups: event 1 now holds other photos, under another name.
    conn.execute("UPDATE shots SET event_id = CASE WHEN event_id = 1 THEN 2 ELSE 1 END")
    conn.execute("UPDATE events SET name='Someone else''s day' WHERE id=1")
    conn.commit()
    assert sorted(_ids(c, event)) == ids[:2]
    assert c.get(f"/api/share/{event}").json()["title"] == "Event 1"
    assert c.get(f"/img/share/{event}/t/{ids[0]}").status_code == 200
    assert c.get(f"/img/share/{event}/t/{ids[2]}").status_code == 404
    # A photo rejected (or gone) since leaves the link.
    c.post("/api/p/t/decide", json={"items": [{"id": ids[1], "pick": -1}]})
    assert _ids(c, event) == [ids[0]]
    # Turning the link off forgets what it held.
    c.delete(f"/api/p/t/shares/{event}")
    assert c.get(f"/api/share/{event}").status_code == 404
    assert conn.execute("SELECT COUNT(*) FROM share_photos WHERE token=?", (event,)).fetchone()[0] == 0


def test_links_made_before_still_follow_their_event(shared):
    c, conn, ids, *_ = shared
    conn.execute("INSERT INTO shares(token, kind, ref, label, created) VALUES ('oldlink', 'event', '2', '', ?)", (time.time(),))
    conn.commit()
    assert sorted(_ids(c, "oldlink")) == ids[2:]
    conn.execute("UPDATE shots SET event_id = 2 WHERE id = ?", (ids[0],))
    conn.commit()
    assert sorted(_ids(c, "oldlink")) == sorted([ids[0], *ids[2:]])


def test_an_event_with_nothing_to_show_is_not_shared(shared):
    c, conn, ids, *_ = shared
    c.post("/api/p/t/decide", json={"items": [{"id": i, "pick": -1} for i in ids[2:]]})
    r = c.post("/api/p/t/shares", json={"kind": "event", "ref": 2})
    assert r.status_code == 400 and "nothing" in r.json()["error"]
    assert c.post("/api/p/t/shares", json={"kind": "event", "ref": 99}).status_code == 404


def test_the_share_server_answers_share_links_and_nothing_else(shared):
    c, conn, ids, album, event = shared
    from joeylab.web.server import create_share_app

    s = TestClient(create_share_app())
    page = s.get(f"/s/{album}")
    assert page.status_code == 200 and "share.js" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert _ids(s, album) == ids[:1] and sorted(_ids(s, event)) == ids[:2]
    assert s.get(f"/img/share/{album}/t/{ids[0]}").status_code == 200
    assert s.get(f"/img/share/{album}/v/{ids[0]}").status_code == 200
    assert s.get(f"/img/share/{album}/t/{ids[1]}").status_code == 404
    assert s.get("/api/share/not-a-token").status_code == 404
    # Nothing of the library itself.
    for url in ("/", "/library", "/api/projects", "/api/meta", "/api/p/t/shots", "/api/p/t/shares",
                f"/img/t/t/{ids[0]}", f"/img/t/o/{ids[0]}", f"/img/share/{album}/t/{ids[0]}/x", "/s/", "/sw.js",
                "/manifest.webmanifest", "/static/index.html", "/static/js/app.js", "/static/fonts/../js/app.js",
                "/static/fonts/%2e%2e/js/app.js"):
        assert s.get(url).status_code == 404, url
    for url in ("/api/p/t/decide", "/api/p/t/roots", "/api/p/t/fav"):
        assert s.post(url, json={}).status_code == 404, url
    # It may be published under any name, through a tunnel or Tailscale Funnel.
    assert s.get(f"/api/share/{album}", headers={"Host": "photos.example.com"}).status_code == 200
    assert s.get(f"/api/share/{album}", headers={"Tailscale-Funnel-Request": "?1"}).status_code == 200
    # Favourites from the page are a guest's, whatever identity a request claims.
    r = s.post(f"/api/share/{album}/fav", json={"id": ids[0]},
               headers={"Tailscale-User-Login": "me@example.com", "X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 200
    viewers = [v[0] for v in conn.execute("SELECT viewer FROM viewer_decisions WHERE shot_id=?", (ids[0],))]
    assert viewers == [f"guest:{album[:6]}"]
    # The main app keeps serving share links too.
    assert c.get(f"/s/{album}").status_code == 200 and _ids(c, album) == ids[:1]


def test_the_share_server_serves_every_file_the_share_page_loads(env):
    from joeylab.web.server import STATIC, create_share_app

    s = TestClient(create_share_app())
    html = (STATIC / "share.html").read_text()
    wanted = set(re.findall(r'(?:href|src)="(/static/[^"]+)"', html))
    js = (STATIC / "js" / "share.js").read_text()
    wanted |= {"/static/js/" + m.lstrip("./") for m in re.findall(r'from\s+"(\./[^"]+)"', js)}
    css = (STATIC / "fonts" / "fonts.css").read_text()
    wanted |= {"/static/fonts/" + m for m in re.findall(r'url\("?([^")]+)"?\)', css) if not m.startswith(("data:", "/", "http"))}
    assert "/static/js/share.js" in wanted and "/static/app.css" in wanted
    for url in sorted(wanted):
        assert s.get(url).status_code == 200, url
