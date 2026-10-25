"""Where to listen and who may connect: loopback and the Tailscale network by default."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess

TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
LOOPBACK = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
DEFAULT_ALLOWED = [*LOOPBACK, TAILSCALE_V4, TAILSCALE_V6]

_TS_CLI = ["tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]


# The macOS app binary acts as the CLI only when told to; under launchd (no terminal) it
# otherwise tries to open its GUI and does nothing.
_TS_ENV = dict(os.environ, TAILSCALE_BE_CLI="1")


def _tailscale_cli() -> str | None:
    for c in _TS_CLI:
        if shutil.which(c) or (c.startswith("/") and os.path.exists(c)):
            return c
    return None


def tailscale_ips() -> list[str]:
    """This machine's Tailscale addresses, read from the network interfaces."""
    ips: list[str] = []
    for cmd in (["ifconfig"], ["ip", "-o", "addr"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for m in re.finditer(r"inet6?\s+([0-9a-fA-F:.]+)", out):
            try:
                ip = ipaddress.ip_address(m.group(1).split("%")[0])
            except ValueError:
                continue
            if ip in TAILSCALE_V4 and str(ip) not in ips:
                ips.append(str(ip))
        if ips:
            break
    return ips


def tailscale_name() -> str | None:
    """MagicDNS name of this machine, e.g. my-mac.tail1234.ts.net."""
    cli = _tailscale_cli()
    if not cli:
        return None
    try:
        out = subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=4, env=_TS_ENV).stdout
        name = json.loads(out).get("Self", {}).get("DNSName", "")
        return name.rstrip(".") or None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def tailscale_owner() -> str | None:
    """The Tailscale login this Mac is signed in with (its owner), lower-cased."""
    cli = _tailscale_cli()
    if not cli:
        return None
    try:
        d = json.loads(subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=4, env=_TS_ENV).stdout)
        user = (d.get("User") or {}).get(str((d.get("Self") or {}).get("UserID")), {})
        login = user.get("LoginName")
        return login.strip().lower() if login else None
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


def parse_networks(values: list[str]) -> list:
    return [ipaddress.ip_network(v, strict=False) for v in values]


def is_allowed(host: str | None, networks: list) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return host == "testclient"  # FastAPI's TestClient
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in n for n in networks)


def tailscale_serve(port: int) -> tuple[str | None, str | None, str | None]:
    """Publish http://127.0.0.1:port to the tailnet through Tailscale itself.

    Tailscale is a signed app the macOS firewall lets through; a self-built Python often
    isn't, so binding the Tailscale IP directly can silently drop every connection.
    Returns (url, mode, error): HTTPS when the tailnet has certificates enabled, else HTTP.
    """
    cli = _tailscale_cli()
    if not cli:
        return None, None, "Tailscale command-line tool not found"
    name = tailscale_name()
    err = None
    for mode in ("https", "http"):
        try:
            r = subprocess.run([cli, "serve", "--bg", "--yes", f"--{mode}={port}", f"http://127.0.0.1:{port}"],
                               capture_output=True, text=True, timeout=25, env=_TS_ENV)
        except (OSError, subprocess.SubprocessError) as e:
            err = str(e)
            continue
        if r.returncode == 0:
            host = name or (tailscale_ips() or ["<tailscale-ip>"])[0]
            return f"{mode}://{host}:{port}", mode, None
        err = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["tailscale serve failed"]
        err = err[0]
    return None, None, err


def tailscale_unserve(port: int, mode: str) -> None:
    cli = _tailscale_cli()
    if cli and mode:
        try:
            subprocess.run([cli, "serve", f"--{mode}={port}", "off"], capture_output=True, timeout=10, env=_TS_ENV)
        except (OSError, subprocess.SubprocessError):
            pass
