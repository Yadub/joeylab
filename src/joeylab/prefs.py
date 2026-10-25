"""Settings for this JoeyLab install as a whole, kept in settings.json in JoeyLab's home.

Each library keeps its own settings in its catalog; this file holds the few choices that are about
the Mac, such as whether face recognition is on (see faces.enabled).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import config


def path() -> Path:
    return config.HOME / "settings.json"


def load() -> dict:
    try:
        with open(path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update(**changes) -> dict:
    """Merge changes into the file (written whole, then swapped in, readable by you alone)."""
    data = {**load(), **changes}
    config.HOME.mkdir(parents=True, exist_ok=True)
    tmp = path().with_name("settings.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path())
    return data
