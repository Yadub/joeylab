"""Background updates (one per library at a time) and the periodic rescan."""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .. import backup, config, db, locks, pipeline

_ids = itertools.count(1)


@dataclass
class Job:
    project: str
    kind: str                      # run | watch
    id: int = field(default_factory=lambda: next(_ids))
    state: str = "running"         # running | done | error
    stage: str = "Starting"
    step: str | None = None        # which of pipeline.STEPS the update is on
    ready: list = field(default_factory=list)    # the steps it has finished
    done: int = 0
    total: int = 0
    log: list = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    summary: dict | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "id", "project", "kind", "state", "stage", "step", "ready", "done", "total", "started", "finished",
            "error")} | {
            "log": self.log[-60:], "changed": bool(self.summary and not self.summary.get("skipped")),
            "untouched": (self.summary or {}).get("untouched")}


class JobReporter(pipeline.Reporter):
    def __init__(self, job: Job, lock: threading.Lock, after_analysis: list | None = None, on_grouped=None):
        self.job, self.lock, self.after_analysis, self.on_grouped = job, lock, after_analysis or [], on_grouped

    analysed = False               # this update analysed new or changed photos

    def step(self, key: str) -> None:
        with self.lock:
            self.job.step, self.job.done, self.job.total = key, 0, 0

    def finished(self, step: str) -> None:
        with self.lock:
            if step not in self.job.ready:
                self.job.ready.append(step)
        if step == "analyse":
            for fn in self.after_analysis:
                try:
                    fn(self.job.project)
                except Exception:
                    pass
        if step == "group" and self.analysed and self.on_grouped:
            self.on_grouped(self.job.project)      # pages already open can show the new stacks and suggestions

    def stage(self, name: str, total: int = 0) -> None:
        with self.lock:
            self.job.stage, self.job.done, self.job.total = name, 0, total
            self.analysed = self.analysed or (self.job.step == "analyse" and total > 0)

    def advance(self, done: int, total: int | None = None) -> None:
        with self.lock:
            self.job.done = done
            if total:
                self.job.total = total

    def log(self, msg: str, level: str = "info") -> None:
        with self.lock:
            self.job.log.append({"t": time.time(), "level": level, "msg": msg})


class JobManager:
    def __init__(self, watch_minutes: float = 15.0, faces: bool = True, ai_layer: bool = True):
        self.lock = threading.Lock()
        self.running: dict[str, Job] = {}
        self.last: dict[str, Job] = {}
        self.versions: dict[str, int] = {}
        self.watch_minutes = watch_minutes
        self.faces, self.ai_layer = faces, ai_layer
        self.after_analysis: list = []     # fn(project), once an update has analysed its photos
        self._stop = threading.Event()
        self._copies_wake = threading.Event()
        self._copies: dict = {"running": False, "last": None}

    # ------------------------------------------------------------------
    def version(self, project: str) -> int:
        return self.versions.get(project, 0)

    def current(self, project: str) -> Job | None:
        with self.lock:
            return self.running.get(project) or self.last.get(project)

    def start(self, project: str, paths: list[str] | None = None, force: bool = False,
              kind: str = "run", only_if_changed: bool = False, quick: bool = False) -> Job:
        with self.lock:
            if project in self.running:
                return self.running[project]
            job = Job(project=project, kind=kind)
            self.running[project] = job
        threading.Thread(target=self._run, args=(job, paths or [], force, only_if_changed, quick),
                         name=f"job-{project}", daemon=True).start()
        return job

    def _bump(self, project: str) -> None:
        with self.lock:
            self.versions[project] = self.versions.get(project, 0) + 1

    def _run(self, job: Job, paths: list[str], force: bool, only_if_changed: bool, quick: bool = False) -> None:
        rep = JobReporter(job, self.lock, self.after_analysis, on_grouped=self._bump)
        try:
            conn = db.connect(job.project)
            if paths:
                pipeline.add_roots(conn, [Path(p) for p in paths], rep, job.project)
            if not conn.execute("SELECT 1 FROM roots").fetchone():
                raise ValueError("This library has no folders yet. Add one under Settings › Folders.")
            # quick: duplicates and quality first; faces and AI are left for the next run.
            try:
                job.summary = pipeline.run_all(conn, job.project, rep, only_if_changed=only_if_changed, force=force,
                                               faces=self.faces and not quick, ai_layer=self.ai_layer and not quick)
            finally:
                conn.close()
            job.state = "done"
            job.stage = "Up to date" if job.summary.get("skipped") else "Finished"
        except locks.Busy:
            # The command line (or another thread) is updating this library right now; it will
            # bring the catalog up to date, so this run simply steps aside.
            job.state, job.stage = "done", "Another update is running"
            job.summary = {"skipped": True, "busy": True}
            rep.log("Another JoeyLab update of this library is running (perhaps from the command line); skipped.", "warn")
        except Exception as e:  # surfaced to the UI
            job.state, job.error = "error", f"{type(e).__name__}: {e}"
            rep.log(job.error, "error")
            rep.log(traceback.format_exc(limit=4), "error")
        finally:
            job.finished = time.time()
            with self.lock:
                self.running.pop(job.project, None)
                if job.kind != "watch" or job.state == "error" or (job.summary and not job.summary.get("skipped")):
                    self.last[job.project] = job
                if job.summary and not job.summary.get("skipped"):
                    self.versions[job.project] = self.versions.get(job.project, 0) + 1
                    self._copies_wake.set()          # new files may be backups: check them once the updates are done

    # ------------------------------------------------------------------
    def start_watch(self, instant: bool = True) -> None:
        """Periodic rescans (a backstop), instant folder watching, and daily catalog backups."""
        self.watcher = None
        if instant:
            try:
                from .watch import FolderWatcher

                self.watcher = FolderWatcher(self)
                self.watcher.start()
            except Exception:
                self.watcher = None
        threading.Thread(target=self._backup_loop, name="backup", daemon=True).start()
        threading.Thread(target=self._copies_loop, name="copies", daemon=True).start()
        if self.watch_minutes > 0:
            threading.Thread(target=self._watch_loop, name="watch", daemon=True).start()

    # ------------------------------------------------------------------ backups on other drives
    COPIES_EVERY_S = 600.0       # every library's rows in the index refreshed, and candidates checked, this often...
    COPIES_SOON_S = 20.0         # ...or again soon while there are candidates it can read
    COPIES_BUDGET_S = 120.0      # reading for at most this long in one go
    COPIES_RATE_MB = 40.0        # and at most this fast, leaving the drives to everything else

    def copies_kick(self) -> None:
        """Check the probable backups now rather than at the next round."""
        self._copies_wake.set()

    def copies_state(self) -> dict:
        return dict(self._copies)

    def _copies_loop(self) -> None:
        try:
            from .. import copies

            # Which drive is which (diskutil, up to seconds a drive), asked once now and remembered, so the
            # viewer's first photo (its backups) doesn't wait for it.
            copies.drive_states()
        except Exception:
            pass
        if self._stop.wait(30):
            return
        while not self._stop.is_set():
            try:
                wait = self._copies_round()
            except Exception:
                wait = self.COPIES_EVERY_S
            self._copies_wake.clear()
            end = time.time() + wait
            while time.time() < end and not self._stop.is_set() and not self._copies_wake.is_set():
                self._copies_wake.wait(min(5.0, max(0.0, end - time.time())))

    def _copies_round(self) -> float:
        """Refresh every library's files in the index (cheap: only changes are written), then read the
        candidates whole, paced, while no update is running and the Mac isn't on its battery. Reading
        never blocks an update: it stops as soon as one starts."""
        from .. import copies
        from .server import on_battery

        for name in config.list_projects():
            if name in self.running or self._stop.is_set():
                continue
            try:
                conn = db.connect(name, create=False)
            except Exception:
                continue
            try:
                copies.refresh(conn, name)
                copies.sync(conn, name)
            except Exception:
                pass
            finally:
                conn.close()
        if self.running or on_battery():
            return self.COPIES_SOON_S * 3
        self._copies["running"] = True
        try:
            r = copies.confirm(seconds=self.COPIES_BUDGET_S, rate_mb=self.COPIES_RATE_MB,
                               stop=lambda: bool(self.running) or self._stop.is_set())
        finally:
            self._copies["running"] = False
        self._copies["last"] = {"at": time.time(), **r}
        return self.COPIES_SOON_S if r["read"] and r["left"] else self.COPIES_EVERY_S

    def _backup_loop(self) -> None:
        while not self._stop.is_set():
            try:
                backup.backup_all()
            except Exception:
                pass
            if self._stop.wait(3600):   # once a day in practice: backup() skips when today's exists
                return

    def _watch_loop(self) -> None:
        if self._stop.wait(10):          # first pass shortly after start-up
            return
        while not self._stop.is_set():
            for name in config.list_projects():
                if name not in self.running:
                    self.start(name, kind="watch", only_if_changed=True)
                    # one project at a time keeps the machine responsive
                    while name in self.running and not self._stop.is_set():
                        time.sleep(2)
            if self._stop.wait(self.watch_minutes * 60):
                return

    def stop(self) -> None:
        self._stop.set()
        self._copies_wake.set()
        if getattr(self, "watcher", None):
            self.watcher.stop()
