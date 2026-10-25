"""`joeylab serve`, `service install` and `share-server`: what they listen on, whether Tailscale is
used, and when identities are believed. Nothing here starts a real server or touches launchd."""

from __future__ import annotations

import plistlib

import pytest
from typer.testing import CliRunner


@pytest.fixture()
def fake_serve(env, monkeypatch):
    """Runs `serve` up to the point it would start answering, and records what it set up."""
    import uvicorn

    from joeylab.web import jobs as jobs_mod
    from joeylab.web import net
    from joeylab.web import server

    seen: dict = {"tailscale_serve": []}
    real = server.create_app

    def create_app(**kw):
        seen["app"] = kw
        return real(**kw)

    def run(self, sockets=None):
        seen["bound"] = [s.getsockname()[0] for s in sockets]
        seen["proxy_headers"] = self.config.proxy_headers
        for s in sockets:
            s.close()

    monkeypatch.setattr(server, "create_app", create_app)
    monkeypatch.setattr(uvicorn.Server, "run", run)
    monkeypatch.setattr(jobs_mod.JobManager, "start_watch", lambda self, instant=True: None)
    monkeypatch.setattr(net, "tailscale_serve", lambda port: seen["tailscale_serve"].append(port) or
                        (f"https://my-mac.tail1234.ts.net:{port}", "https", None))
    monkeypatch.setattr(net, "tailscale_unserve", lambda port, mode: None)
    monkeypatch.setattr(net, "tailscale_owner", lambda: "me@example.com")
    monkeypatch.setattr(net, "tailscale_name", lambda: "my-mac.tail1234.ts.net")
    return seen


def _serve(*args):
    from joeylab.cli import app

    return CliRunner().invoke(app, ["serve", "--port", "0", "--watch", "0", "--no-ai", "--no-faces", *args])


def said(r) -> str:
    """The command's output on one line (the console wraps long lines)."""
    return " ".join(r.output.split())


def test_serve_listens_on_this_mac_only_by_default(fake_serve):
    r = _serve()
    assert r.exit_code == 0, r.output
    assert fake_serve["bound"] == ["127.0.0.1"] and fake_serve["tailscale_serve"] == []
    # The app sees the real peer: uvicorn mustn't swap in the address X-Forwarded-For names.
    assert fake_serve["proxy_headers"] is False
    assert fake_serve["app"]["trust_identity"] is True and fake_serve["app"]["meta"]["tailnet_url"] is None
    assert "Only this Mac can connect" in said(r) and "--tailscale" in said(r)
    # This Mac's own tailnet name is still one of its names (for a `tailscale serve` set up by hand).
    assert "my-mac.tail1234.ts.net" in fake_serve["app"]["allow_hosts"]
    assert _serve("--no-tailscale").exit_code == 0 and fake_serve["tailscale_serve"] == []


def test_serve_with_tailscale_publishes_to_the_tailnet(fake_serve):
    r = _serve("--tailscale")
    assert r.exit_code == 0, r.output
    assert fake_serve["tailscale_serve"] and fake_serve["bound"] == ["127.0.0.1"]
    app = fake_serve["app"]
    assert app["trust_identity"] is True and app["owner"] == "me@example.com" and "writers" not in app
    assert "https://my-mac.tail1234.ts.net" in said(r)
    assert "signed in to Tailscale as me@example.com" in said(r) and "share links" in said(r)
    assert _serve("--writer", "a@example.com").exit_code == 2           # one user: there are no writers


def test_serve_on_other_addresses_needs_a_token_and_then_trusts_no_identity(fake_serve, monkeypatch):
    monkeypatch.delenv("JOEYLAB_TOKEN", raising=False)
    r = _serve("--host", "0.0.0.0")
    assert r.exit_code == 2 and "--token" in said(r) and "bound" not in fake_serve
    r = _serve("--host", "::1")                                        # still this Mac: no token needed
    assert r.exit_code == 0, r.output
    assert fake_serve["app"]["trust_identity"] is True
    r = _serve("--host", "0.0.0.0", "--token", "s3cret", "--tailscale")
    assert r.exit_code == 0, r.output
    assert fake_serve["bound"] == ["127.0.0.1", "0.0.0.0"]
    assert fake_serve["app"]["trust_identity"] is False and fake_serve["app"]["token"] == "s3cret"
    assert "Only you can open it" in said(r)


def test_service_install_writes_its_options_into_the_plist(env, monkeypatch):
    from joeylab import service
    from joeylab.cli import app

    pl = service.plist(9000, service.serve_args(10, True))
    args = pl["ProgramArguments"]
    assert args[args.index("serve"):] == ["serve", "--port", "9000", "--watch", "10", "--tailscale"]
    assert pl["ProcessType"] == "Standard" and pl["KeepAlive"] is True and pl["RunAtLoad"] is True
    assert service.describe(pl) == {"port": 9000, "watch": 10.0, "tailscale": True}
    assert service.describe(service.plist(8765, service.serve_args()))["tailscale"] is False

    installed: list = []
    monkeypatch.setattr(service, "install", lambda port, extra: installed.append((port, extra)))
    r = CliRunner().invoke(app, ["service", "install", "--port", "9001", "--tailscale"])
    assert r.exit_code == 0, r.output
    assert installed == [(9001, ["--watch", "15", "--tailscale"])]
    assert "your tailnet" in said(r)
    r = CliRunner().invoke(app, ["service", "install"])
    assert installed[-1] == (8765, ["--watch", "15"]) and "service install --tailscale" in said(r)


def test_service_status_shows_what_it_runs_with(env, monkeypatch, tmp_path):
    from joeylab import service
    from joeylab.cli import app

    plist_file = tmp_path / "app.joeylab.serve.plist"
    with open(plist_file, "wb") as fh:
        plistlib.dump(service.plist(8765, service.serve_args(15, True)), fh)
    monkeypatch.setattr(service, "PLIST", plist_file)
    monkeypatch.setattr(service, "running", lambda: True)
    r = CliRunner().invoke(app, ["service", "status"])
    assert r.exit_code == 0, r.output
    assert "Running" in said(r) and "port 8765" in said(r) and "tailnet" in said(r)


def test_share_server_listens_on_this_mac_and_serves_share_links_only(env, monkeypatch):
    import uvicorn

    from joeylab.cli import app

    seen = {}

    def run(self, sockets=None):
        seen["bound"] = [s.getsockname()[0] for s in sockets]
        seen["app"], seen["proxy_headers"] = self.config.app, self.config.proxy_headers
        for s in sockets:
            s.close()

    monkeypatch.setattr(uvicorn.Server, "run", run)
    r = CliRunner().invoke(app, ["share-server", "--port", "0"])
    assert r.exit_code == 0, r.output
    assert seen["bound"] == ["127.0.0.1"] and "/s/" in said(r) and seen["proxy_headers"] is False
    from fastapi.testclient import TestClient

    c = TestClient(seen["app"])
    assert c.get("/api/projects").status_code == 404 and c.get("/").status_code == 404
