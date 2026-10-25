"""One user: only the owner (at the Mac, or on a device signed in to the Mac's own Tailscale account)
can open the app; anyone else gets share links and nothing more. Which identity headers are believed;
the host check; the Content-Security-Policy."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import add_root, make_jpeg, run_all

OWNER, FRIEND, GUEST = "me@example.com", "friend@example.com", "guest@example.com"


def fwd(login: str | None = None, ip: str = "100.64.0.9") -> dict:
    """A request as `tailscale serve` forwards it from the tailnet: X-Forwarded-For, and for a
    signed-in user (not a tagged device) their login."""
    h = {"X-Forwarded-For": ip}
    if login:
        h["Tailscale-User-Login"] = login
    return h


@pytest.fixture()
def lib_app(env):
    tmp, lib = env
    for i in range(3):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    from joeylab.web.server import create_app

    app = create_app(owner=OWNER)
    return TestClient(app), ids, tmp, lib


def _decide(c, sid, headers=None):
    return c.post("/api/p/t/decide", json={"items": [{"id": sid, "rating": 3}]}, headers=headers or {}).status_code


# Every route that reads or writes folders on the Mac outside the catalog, with a body that would
# otherwise get past the role check.
OWNER_ONLY = [
    ("get", "/api/fs", None),
    ("post", "/api/projects", {"name": "other", "path": "/nonexistent"}),
    ("post", "/api/p/t/roots", {"path": "/nonexistent", "scan": False}),
    ("delete", "/api/p/t/roots/999", None),
    ("post", "/api/p/t/jobs", {"paths": ["/nonexistent"]}),
    ("post", "/api/p/t/export/album", {"dest": "/nonexistent/album"}),
    ("post", "/api/p/t/export/xmp", {"confirm": True}),
    ("post", "/api/p/t/export/xmp/restore", {"backup_dir": "/nonexistent"}),
    ("post", "/api/p/t/export/gallery", {"dest": "/nonexistent/gallery"}),
    ("post", "/api/p/t/albums/1/linkfolder", {"dest": "/nonexistent/links"}),
    ("post", "/api/p/t/import/lightroom", {"path": "/nonexistent.lrcat"}),
]


def _call(c, method, url, body, headers):
    if method == "get":
        return c.get(url, headers=headers)
    if method == "delete":
        return c.delete(url, headers=headers)
    return c.post(url, json=body, headers=headers)


@pytest.mark.parametrize("method,url,body", OWNER_ONLY)
def test_folders_and_files_on_the_mac_are_the_owners_alone(lib_app, method, url, body):
    c, *_ = lib_app
    for who in (fwd(FRIEND), fwd(GUEST), fwd()):                  # other accounts, a tagged device
        r = _call(c, method, url, body, who)
        assert r.status_code == 403, (url, who, r.text)
    # At the Mac, and on the owner's phone, the request gets past the check (and then fails on the
    # made-up path, which is fine here).
    loose = TestClient(c.app, raise_server_exceptions=False)
    for owner in ({}, fwd(OWNER)):
        assert _call(loose, method, url, body, owner).status_code != 403, (url, owner)


def test_nobody_but_the_owner_opens_it_and_share_links_still_work(lib_app):
    c, ids, *_ = lib_app
    aid = c.post("/api/p/t/albums", json={"name": "Best", "ids": ids[:1]}).json()["id"]
    tok = c.post("/api/p/t/shares", json={"kind": "album", "ref": aid}).json()["token"]
    for who in ({}, fwd(OWNER)):
        assert _decide(c, ids[0], who) == 200
        assert c.get("/api/p/t/shots", headers=who).status_code == 200
        assert c.get("/", headers=who).status_code == 200
        assert c.get("/api/meta", headers=who).json()["owner"] is True
    for who in (fwd(FRIEND), fwd(GUEST), fwd()):
        for r in (c.get("/api/p/t/shots", headers=who), c.get("/api/meta", headers=who), c.get("/", headers=who),
                  c.get("/api/p/t/shares", headers=who), c.post("/api/p/t/fav", json={"ids": [ids[1]]}, headers=who),
                  c.post("/api/p/t/comments", json={"shot_id": ids[1], "text": "hi"}, headers=who)):
            assert r.status_code == 403 and "private" in r.text, (who, r.request.url)
        assert _decide(c, ids[0], who) == 403
        assert c.get(f"/s/{tok}", headers=who).status_code == 200            # share links: for anyone
        assert c.get(f"/api/share/{tok}", headers=who).status_code == 200
        assert c.get("/static/app.css", headers=who).status_code == 200


def test_owner_unknown_means_only_the_mac(env):
    _, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    conn, _ = run_all()
    sid = conn.execute("SELECT id FROM shots").fetchone()[0]
    from joeylab.web.server import create_app

    c = TestClient(create_app())                          # the Mac's Tailscale account unknown
    assert _decide(c, sid) == 200
    assert _decide(c, sid, fwd(OWNER)) == 403


def test_identity_headers_count_only_when_tailscale_serve_forwarded_them(lib_app):
    c, ids, *_ = lib_app
    # On a loopback connection without X-Forwarded-For the login is ignored, and the request isn't
    # "at the Mac" either.
    claim = {"Tailscale-User-Login": OWNER}
    assert _decide(c, ids[0], claim) == 403
    # A direct connection (bound with --host) can't claim an identity, forwarded or not.
    from joeylab.web.server import create_app

    app = create_app(owner=OWNER)
    direct = TestClient(app, client=("100.101.102.103", 5000))
    for h in (claim, fwd(OWNER)):
        assert direct.post("/api/p/t/decide", json={"items": [{"id": ids[0], "rating": 2}]}, headers=h).status_code == 403
    assert TestClient(app, client=("127.0.0.1", 5000)).post(
        "/api/p/t/decide", json={"items": [{"id": ids[0], "rating": 2}]}).status_code == 200      # at the Mac
    # Listening directly as well (--host): identities are never believed, even through tailscale serve.
    untrusting = TestClient(create_app(owner=OWNER, trust_identity=False))
    assert _decide(untrusting, ids[0], fwd(OWNER)) == 403
    assert _decide(untrusting, ids[0]) == 200


def test_the_access_token_makes_you_the_owner_from_elsewhere(lib_app):
    c, ids, *_ = lib_app
    from joeylab.web.server import create_app

    remote = TestClient(create_app(owner=OWNER, token="s3cret", trust_identity=False), client=("100.101.102.103", 5000))
    assert remote.get("/api/p/t/shots").status_code == 401
    assert remote.get("/?token=s3cret", follow_redirects=False).status_code == 303          # sets the cookie
    assert remote.get("/api/p/t/shots").status_code == 200
    assert remote.post("/api/p/t/decide", json={"items": [{"id": ids[0], "rating": 2}]}).status_code == 200


def test_host_check_accepts_this_macs_names_only(env):
    from joeylab.web.server import create_app

    c = TestClient(create_app(meta={"tailnet_url": "https://my-mac.tail1234.ts.net:8765"}, allow_hosts=["photos.lan"]))
    for host in ("localhost:8765", "127.0.0.1:8765", "[::1]:8765", "my-mac.tail1234.ts.net:8765",
                 "my-mac.tail1234.ts.net", "100.101.102.103:8765", "[fd7a:115c:a1e0::1]:8765", "photos.lan"):
        assert c.get("/api/projects", headers={"Host": host}).status_code == 200, host
    for host in ("other-mac.tail1234.ts.net:8765", "my-mac.tail9999.ts.net", "evil.ts.net", "attacker.example",
                 "my-mac.tail1234.ts.net.attacker.example", "8.8.8.8"):
        assert c.get("/api/projects", headers={"Host": host}).status_code == 421, host


def test_pages_carry_a_content_security_policy(lib_app):
    c, *_ = lib_app
    for url in ("/", "/library", "/s/sometoken"):
        csp = c.get(url).headers.get("content-security-policy", "")
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp, url
        assert "default-src 'self'" in csp and "connect-src 'self'" in csp and "form-action 'self'" in csp
        assert "img-src 'self' data: blob:" in csp and "tile.openstreetmap.org" in csp
        assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";")[0]
    assert "content-security-policy" not in c.get("/api/meta").headers
