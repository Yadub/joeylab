"""Run `joeylab serve` in the background at login with launchd (macOS)."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from . import config

LABEL = "app.joeylab.serve"
MAC_ONLY = ("The background service uses launchd, which is macOS only. Elsewhere, run joeylab serve "
            "with your system's service manager (systemd on Linux).")
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = config.HOME / "logs"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def serve_args(watch: float = 15, tailscale: bool = False) -> list[str]:
    """The `joeylab serve` options the service runs with (after --port)."""
    out = ["--watch", f"{watch:g}"]
    if tailscale:
        out.append("--tailscale")
    return out


def describe(pl: dict) -> dict:
    """What an installed service runs with, read back from its plist: port, watch, tailscale."""
    args = list(pl.get("ProgramArguments") or [])
    args = args[args.index("serve") + 1:] if "serve" in args else []
    out: dict = {"port": 8765, "watch": 15.0, "tailscale": False}
    i = 0
    while i < len(args):
        a, nxt = args[i], (args[i + 1] if i + 1 < len(args) else None)
        if a == "--port" and nxt is not None:
            out["port"], i = int(nxt), i + 1
        elif a == "--watch" and nxt is not None:
            out["watch"], i = float(nxt), i + 1
        elif a in ("--tailscale", "--no-tailscale"):
            out["tailscale"] = a == "--tailscale"
        i += 1
    return out


def settings() -> dict | None:
    """describe() of the installed service, or None when it isn't installed (or can't be read)."""
    try:
        with open(PLIST, "rb") as fh:
            return describe(plistlib.load(fh))
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def plist(port: int = 8765, extra: list[str] | None = None) -> dict:
    from . import exif

    path = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    tool = exif.exiftool()
    if tool and os.path.dirname(tool) not in path:
        path.insert(0, os.path.dirname(tool))
    env = {
        # launchd starts with a bare PATH; exiftool's folder is on it.
        "PATH": ":".join(path),
        "PYTHONUNBUFFERED": "1",
    }
    if os.environ.get("JOEYLAB_HOME"):
        env["JOEYLAB_HOME"] = os.environ["JOEYLAB_HOME"]
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "joeylab", "serve", "--port", str(port), *(extra or [])],
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        # Not "Background": macOS throttles a background job's disk access whenever anything else
        # (Spotlight, Photos analysis) touches the drive, which made imports ~50x slower.
        "ProcessType": "Standard",
        "StandardOutPath": str(LOG_DIR / "serve.log"),
        "StandardErrorPath": str(LOG_DIR / "serve.err.log"),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    if sys.platform != "darwin":
        raise RuntimeError(MAC_ONLY)
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def installed() -> bool:
    return PLIST.exists()


def running() -> bool:
    r = _launchctl("print", f"{_domain()}/{LABEL}")
    return r.returncode == 0 and "state = running" in r.stdout


def prepare_logs() -> None:
    """The service's logs, made here so launchd appends to files only you can read."""
    config.secure_home()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("serve.log", "serve.err.log"):
        os.close(os.open(LOG_DIR / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
        config.private(LOG_DIR / name)


def install(port: int = 8765, extra: list[str] | None = None) -> None:
    prepare_logs()
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    if installed():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
        # bootout returns before the old instance is gone; loading again too soon fails with
        # "Input/output error", so wait for it to disappear.
        for _ in range(50):
            if _launchctl("print", f"{_domain()}/{LABEL}").returncode != 0:
                break
            time.sleep(0.2)
    with open(PLIST, "wb") as fh:
        plistlib.dump(plist(port, extra), fh)
    for attempt in range(5):
        r = _launchctl("bootstrap", _domain(), str(PLIST))
        if r.returncode == 0 or "already" in (r.stderr + r.stdout).lower():
            return
        time.sleep(1)
    raise RuntimeError((r.stderr or r.stdout).strip() or "launchctl bootstrap failed")


def uninstall() -> None:
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    PLIST.unlink(missing_ok=True)


def stop() -> None:
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    for _ in range(50):
        if _launchctl("print", f"{_domain()}/{LABEL}").returncode != 0:
            break
        time.sleep(0.2)


def start() -> None:
    if not installed():
        raise RuntimeError("The service isn't installed: joeylab service install")
    r = _launchctl("bootstrap", _domain(), str(PLIST))
    if r.returncode != 0 and "already" not in (r.stderr + r.stdout).lower():
        raise RuntimeError((r.stderr or r.stdout).strip() or "launchctl bootstrap failed")


def restart() -> None:
    r = _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "launchctl kickstart failed")


def log_tail(lines: int = 40) -> str:
    out = []
    for name in ("serve.log", "serve.err.log"):
        p = LOG_DIR / name
        if p.exists():
            text = p.read_text(errors="replace").splitlines()[-lines:]
            out.append(f"--- {p}\n" + "\n".join(text))
    return "\n".join(out) or "No logs yet."
