"""React to file changes in tracked folders as they happen (macOS FSEvents via watchdog).

Events are collected per project and acted on once the folder has been quiet for a while,
so copying a whole card causes one update rather than thousands.
"""

from __future__ import annotations

import os
import threading
import time

from watchdog.events import FileSystemEventHandler

from .. import config, db
from ..config import kind_for

QUIET_SECONDS = 12


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "FolderWatcher", project: str):
        super().__init__()
        self.watcher, self.project = watcher, project

    def dispatch(self, event) -> None:  # watchdog calls this for every event
        paths = [getattr(event, "src_path", ""), getattr(event, "dest_path", "")]
        left_out = self.watcher.excluded.get(self.project, ())
        for p in paths:
            if not p or os.path.basename(p).startswith("."):
                continue
            if any(p == e or p.startswith(e.rstrip(os.sep) + os.sep) for e in left_out):
                continue                     # moved to another library: that one's watch sees it
            if event.is_directory and event.event_type in ("moved", "deleted", "created"):
                self.watcher.touch(self.project)
                return
            if kind_for(os.path.splitext(p)[1]):
                self.watcher.touch(self.project)
                return


class FolderWatcher:
    def __init__(self, jobs):
        from watchdog.observers import Observer

        self.jobs = jobs
        self.observer = Observer()
        self.watches: dict[tuple[str, str], object] = {}
        self.excluded: dict[str, list[str]] = {}       # per library: the folders it leaves out
        self.dirty: dict[str, float] = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()

    def touch(self, project: str) -> None:
        with self.lock:
            self.dirty[project] = time.time()

    def refresh(self) -> None:
        """Watch every available tracked folder; drop watches for removed or unplugged ones."""
        wanted = set()
        for name in config.list_projects():
            try:
                conn = db.connect(name, create=False)
                roots = [r[0] for r in conn.execute("SELECT path FROM roots")]
                self.excluded[name] = db.excluded_folders(conn)
                conn.close()
            except Exception:
                continue
            wanted |= {(name, r) for r in roots if os.path.isdir(r)}
        for key in set(self.watches) - wanted:
            try:
                self.observer.unschedule(self.watches.pop(key))
            except Exception:
                self.watches.pop(key, None)
        first = not self.watches and not getattr(self, "_started", False)
        for key in wanted - set(self.watches):
            try:
                self.watches[key] = self.observer.schedule(_Handler(self, key[0]), key[1], recursive=True)
            except OSError:
                continue
            if not first:
                self.touch(key[0])   # catch anything that arrived before this folder was watched
        self._started = True

    def request_refresh(self) -> None:
        self._refresh_now = True

    def _loop(self) -> None:
        last_refresh = time.time()
        while not self._stop.wait(3):
            now = time.time()
            if now - last_refresh > 60 or getattr(self, "_refresh_now", False):
                self._refresh_now = False
                self.refresh()
                last_refresh = now
            with self.lock:
                ready = [p for p, t in self.dirty.items() if now - t >= QUIET_SECONDS and p not in self.jobs.running]
                for p in ready:
                    del self.dirty[p]
            for p in ready:
                self.jobs.start(p, kind="watch", only_if_changed=True)

    def start(self) -> None:
        self.refresh()
        self.observer.start()
        threading.Thread(target=self._loop, name="folder-watch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.observer.stop()
        except Exception:
            pass

    def status(self) -> dict:
        return {"folders": len(self.watches), "pending": sorted(self.dirty)}
