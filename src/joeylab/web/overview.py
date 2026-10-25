"""The counts behind the top bar, Home and the Library's rail (`/api/p/<library>/overview`), kept per
library so that no page waits for them after the first time.

A full count reads the whole catalog: on a library of 75,000 photos and 300,000 faces it takes about a
second, and anything that commits (an update, the check of backups on other drives, previews made
ahead of culling, taste retraining) changes what it would say. So the numbers are kept in three ways:

* **always**: a few that cost nothing (the settings, the folders, people, events), read on every request;
* **live**: what pages confirm your actions with (photos and files, picks and rejects, what's left to
  cull and the suggestions, duplicate sets), counted again (about a tenth of a second) whenever the
  catalog's counts counter (`db.COUNTS_GEN`) has moved. Triggers move it in the same transaction as the
  change, so these are exact straight after a decision, an Undo or a move to the Trash, whoever made it;
* everything else (cameras, folders, the timeline, best photos, faces, search), from the last full
  count, served at once even when the catalog has changed since. A request that finds it out of date
  asks for a new full count in the background: one at a time per library, a moment after the change
  (a run of changes is counted once), at most every MIN_GAP_S, and while an update is analysing,
  finding faces or indexing (or the library is being restacked or rescored), at most every BUSY_GAP_S.

The last full count is kept on disk (`overview.json` in the library's folder), so a restart or a switch
of library opens at once too. Every count that changes the numbers gets a new `seq`; `/jobs/current`
reports it, so an open page fetches the new numbers when they're ready.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime

from .. import config, db

FORMAT = 1                    # of overview.json; a new layout of the counts is a new format
DEBOUNCE_S = 1.0              # a full count starts this long after the change that called for it...
MIN_GAP_S = 10.0              # ...and at least this long after the last one finished
BUSY_GAP_S = 300.0            # while an update is in a heavy step: at most this often
HEAVY_STEPS = {"analyse", "faces", "ai", "search"}
FILE = "overview.json"


def today() -> float:
    """The start of today, local time: "today you picked…" and when the full count was made."""
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def catalog_stamp(project: str) -> list:
    """Changes whenever anything commits to the library's catalog (db.file_stamp); a list, as kept in
    the file."""
    return list(db.file_stamp(str(config.project_dir(project) / "catalog.db")))


def _read(conn, fn, *args):
    """fn(conn, *args) on one snapshot of the catalog, with the counts counter as of that snapshot."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        return db.counter(conn, db.COUNTS_GEN), fn(conn, *args)
    finally:
        if own:
            conn.rollback()                    # only read


class Overviews:
    """The overview of each library this server has been asked for (see the module's notes)."""

    def __init__(self, jobs, bg=None):
        self.jobs, self.bg = jobs, bg
        self.lock = threading.Lock()
        self.entries: dict[str, dict] = {}
        self.asked: dict[str, float] = {}      # when a request last found the full count out of date
        self.workers: dict[str, threading.Thread] = {}
        self.counting: dict[str, threading.Lock] = defaultdict(threading.Lock)    # one full count at a time

    # ------------------------------------------------------------ for requests
    def get(self, project: str, conn) -> tuple[dict, bool]:
        """The overview as of now: (body, fresh). `fresh` is False while a newer full count is due;
        the live and always counts are exact either way."""
        from .routes.library import overview_always, overview_live

        day = today()
        e = self._entry(project)
        if e is None:
            with self.counting[project]:       # a count under way (or just finished) is used, not repeated
                e = self.entries.get(project) or self._count(project, conn)
        gen = db.counter(conn, db.COUNTS_GEN)
        if (e["gen"], e["day"]) != (gen, day):
            gen, live = _read(conn, overview_live, day)
            e = self._keep(project, {**e, "gen": gen, "day": day, "body": _merge(e["body"], live)}, live=True)
        body = _merge(e["body"], overview_always(conn))
        body["name"] = project
        fresh = e["stamp"] == catalog_stamp(project) and e["full_day"] == day
        if not fresh:
            self.ask(project)
        return body, fresh

    def seq(self, project: str) -> int | None:
        """The number of the latest overview of this library, None when it hasn't been asked for."""
        e = self.entries.get(project)
        return e["seq"] if e else None

    def check(self, project: str) -> None:
        """A page is open (it polls): if the catalog changed since the last full count, count again
        in the background, so the page's numbers follow changes made by anything."""
        e = self.entries.get(project)
        if e and (e["stamp"] != catalog_stamp(project) or e["full_day"] != today()):
            self.ask(project)

    def forget(self, project: str) -> None:
        """The library was renamed or removed: nothing of it is kept in memory (its folder, with the
        file, went with it)."""
        with self.lock:
            self.entries.pop(project, None)
            self.asked.pop(project, None)

    # ------------------------------------------------------------ the full count
    def ask(self, project: str) -> None:
        """Count again in the background, soon (see the module's notes for when). Asking again before
        that count starts changes nothing: pages polling every second can't put it off."""
        with self.lock:
            self.asked.setdefault(project, time.time())
            if project in self.workers:
                return
            t = threading.Thread(target=self._work, args=(project, config.HOME), name=f"overview-{project}", daemon=True)
            self.workers[project] = t
        t.start()

    def busy(self, project: str) -> bool:
        """Whether an update of this library is in a heavy step, or it's being restacked, rescored or
        regrouped, as far as this server can tell."""
        job = self.jobs.running.get(project) if self.jobs else None
        if job is not None and job.step in HEAVY_STEPS:
            return True
        running = getattr(self.bg, "running", None) or set()
        return any(k.endswith(f":{project}") for k in list(running))

    def _work(self, project: str, home) -> None:
        try:
            while True:
                with self.lock:
                    asked = self.asked.get(project)
                    if asked is None:            # nothing more was asked for while counting
                        self.workers.pop(project, None)      # (under the lock ask() starts workers with)
                        return
                e = self.entries.get(project)
                done = e["counted"] if e else 0.0
                wake = max(asked + DEBOUNCE_S, done + (BUSY_GAP_S if self.busy(project) else MIN_GAP_S))
                if time.time() < wake:
                    time.sleep(min(2.0, max(0.05, wake - time.time())))     # (and look at busy again)
                    continue
                if config.HOME != home:          # the tests' home changed under a thread left over
                    return
                with self.lock:
                    self.asked.pop(project, None)    # what's asked from now on is after this count began
                try:
                    conn = db.connect(project, create=False)
                except Exception:
                    return
                try:
                    with self.counting[project]:
                        cur = self.entries.get(project)
                        if not cur or cur["stamp"] != catalog_stamp(project) or cur["full_day"] != today():
                            self._count(project, conn)
                except Exception as err:         # the next request asks again
                    import sys

                    print(f"[joeylab] counting {project} failed: {type(err).__name__}: {err}", file=sys.stderr, flush=True)
                finally:
                    conn.close()
        finally:
            with self.lock:
                if self.workers.get(project) is threading.current_thread():
                    self.workers.pop(project, None)

    def _count(self, project: str, conn) -> dict:
        """A full count, kept in memory and on disk. Backups on other drives are brought in first, so
        the counts include what another library's update found."""
        from .. import copies
        from .routes.library import overview_full

        copies.ensure(conn, project)
        stamp, day = catalog_stamp(project), today()
        gen, body = _read(conn, overview_full, project, day)
        e = {"stamp": stamp, "gen": gen, "day": day, "full_day": day, "body": body, "counted": time.time()}
        e = self._keep(project, e)
        self._save(project, e)
        return e

    # ------------------------------------------------------------ keeping
    def _keep(self, project: str, e: dict, live: bool = False) -> dict:
        """Keep a newer overview. A full count that began before a live count of later changes keeps
        that live count's numbers (the counter only goes up). A new seq when the numbers changed."""
        from .routes.library import LIVE_KEYS

        with self.lock:
            old = self.entries.get(project)
            if old and not live and old["gen"] > e["gen"]:
                e = {**e, "gen": old["gen"], "day": old["day"], "body": _merge(e["body"], _live_part(old["body"], LIVE_KEYS))}
            if old and not live:
                e["counted"] = time.time()
            elif old:
                e["counted"] = old["counted"]
            same = old is not None and old["body"] == e["body"]
            seq = old["seq"] if same else max(int(time.time() * 1000), (old or e).get("seq", 0) + 1)
            e = {**e, "seq": seq}
            self.entries[project] = e
            return e

    def _entry(self, project: str) -> dict | None:
        e = self.entries.get(project)
        if e is not None:
            return e
        e = self._load(project)
        if e is not None:
            with self.lock:
                e = self.entries.setdefault(project, e)
        return e

    def _load(self, project: str) -> dict | None:
        try:
            with open(config.project_dir(project) / FILE, encoding="utf-8") as f:
                e = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(e, dict) or e.get("format") != FORMAT or not isinstance(e.get("body"), dict):
            return None
        e.pop("format", None)
        e["counted"] = 0.0                     # a count may follow straight away
        return e

    def _save(self, project: str, e: dict) -> None:
        path = config.project_dir(project) / FILE
        tmp = path.with_name(f".{FILE}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"format": FORMAT, **{k: v for k, v in e.items() if k != "counted"}}, f, separators=(",", ":"))
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _live_part(body: dict, keys: dict) -> dict:
    return {"counts": {k: body["counts"][k] for k in keys["counts"] if k in body.get("counts", {})},
            **{k: body[k] for k in keys["top"] if k in body}}


def _merge(body: dict, part: dict) -> dict:
    """The overview with some of its numbers replaced (counts merge; anything else is replaced)."""
    out = {**body, **{k: v for k, v in part.items() if k != "counts"}}
    out["counts"] = {**body.get("counts", {}), **part.get("counts", {})}
    return out
