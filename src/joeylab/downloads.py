"""Model downloads from a fixed address, each checked against its known SHA-256 before it's used.

A file that isn't exactly the expected one (changed upstream, cut short, tampered with) is
discarded with a clear message. Nothing is downloaded with JOEYLAB_OFFLINE.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path

from . import config


class Mismatch(OSError):
    """The download isn't the file JoeyLab expects."""


def fetch(url: str, sha256: str, dest: Path, timeout: float = 120) -> Path:
    """Download url to dest, keeping it only if its SHA-256 is sha256. Raises config.ModelMissing
    when downloads are off, Mismatch for a different file and OSError when the download fails;
    nothing is left behind then."""
    if config.offline():
        raise config.ModelMissing(f"{dest.name} isn't downloaded, and downloads are off (JOEYLAB_OFFLINE)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r, open(tmp, "wb") as fh:
            while chunk := r.read(1 << 20):
                digest.update(chunk)
                fh.write(chunk)
        got = digest.hexdigest()
        if got != sha256:
            raise Mismatch(f"The download of {dest.name} isn't the expected file (SHA-256 {got}, expected {sha256}), "
                           f"so it was discarded. Source: {url}. If this keeps happening, the file has changed "
                           "there: please report it.")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest
