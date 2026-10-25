"""One update at a time per library, across processes (the CLI and the background service).

A pipeline run, a re-score after retraining taste, and face re-clustering all rewrite computed
data; two at once would duplicate the work and race on the same thumbnail files.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

from .config import project_dir

try:
    import fcntl
except ImportError:          # Windows, where JoeyLab doesn't run (it says so at start)
    fcntl = None

_local = threading.local()


class Busy(RuntimeError):
    """Another JoeyLab process or thread is updating this library."""


@contextmanager
def project_lock(project: str, wait: float = 0.0):
    """Hold the library's update lock. wait=0 fails at once with Busy if it's taken; a positive
    wait retries for that many seconds. Re-entrant within one thread."""
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = {}
    if held.get(project):
        held[project] += 1
        try:
            yield
        finally:
            held[project] -= 1
        return
    path = project_dir(project) / "update.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.time() + wait
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise Busy(f"Another JoeyLab update of {project!r} is running. Try again when it finishes.")
                time.sleep(0.5)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        held[project] = 1
        try:
            yield
        finally:
            held[project] = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def is_busy(project: str) -> bool:
    try:
        with project_lock(project):
            return False
    except Busy:
        return True
