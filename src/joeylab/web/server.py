"""HTTP API + image serving + the single-page app. Originals are only ever read.

Routes live in web/routes/*, by area. This module wires them up with the request guard: who may
connect, a same-origin check on every change, the optional access token, read-only share links,
and a background helper for re-scoring that respects the one-update-at-a-time lock. The same app
in share-only form (`joeylab share-server`) serves share links and nothing else.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from .. import config, db, library, locks, scoring, taste
from ..imaging import load_preview
from . import net
from .deps import conn_dep, guard_problem, is_owner, PRIVATE, viewer_of
from .jobs import JobManager
from .overview import Overviews

STATIC = Path(__file__).parent / "static"
SMALL_THUMB = 400
PREVIEW_SIDE = 2048
PREVIEW_CACHE_MB = int(os.environ.get("JOEYLAB_PREVIEW_CACHE_MB", "4000"))
SMALL_CACHE_MB = int(os.environ.get("JOEYLAB_SMALL_THUMB_CACHE_MB", "3000"))
IMMUTABLE = {"Cache-Control": "private, max-age=31536000, immutable"}
# Pages may load only JoeyLab's own scripts and styles, talk only to JoeyLab, and can't be framed.
# Map tiles (Places, when switched on) come from OpenStreetMap.
CSP = ("default-src 'self'; img-src 'self' data: blob: https://tile.openstreetmap.org https://*.tile.openstreetmap.org; "
       "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'")
SHARE_CSP = CSP.replace(" https://tile.openstreetmap.org https://*.tile.openstreetmap.org", "")
# What the share-link server answers: the share page, its data and images, and the files the page loads.
SHARE_PATHS = re.compile(r"^/(s/[^/]+|api/share/[^/]+(/fav)?|img/share/[^/]+/[a-z]/\d+)$")
SHARE_STATIC = ("/static/app.css", "/static/icon.svg", "/static/js/share.js", "/static/fonts/")
MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".heic": "image/heic",
         ".heif": "image/heif", ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp",
         ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".3gp": "video/3gpp"}


class _ApiGzip:
    """Compress the API's JSON (lists of thousands of photos are much smaller zipped, which matters
    on a phone over Tailscale); images are compressed already and go out as they are."""

    def __init__(self, app):
        from starlette.middleware.gzip import GZipMiddleware

        self.app = app
        self.gz = GZipMiddleware(app, minimum_size=2048, compresslevel=5)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/"):
            return await self.gz(scope, receive, send)
        return await self.app(scope, receive, send)


def _prune_cache(cache: Path, cap_mb: int) -> None:
    """Keep the large-preview cache under its cap, oldest first. Face crops (previews/faces) are
    small, needed by People all the time and costly to remake from RAW files: never pruned."""
    files = []
    faces = cache / "faces"
    for f in cache.rglob("*.jpg"):
        if faces in f.parents:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, f))
    total = sum(s for _, s, _ in files)
    if total <= cap_mb * 1e6:
        return
    for _, size, f in sorted(files):
        f.unlink(missing_ok=True)
        total -= size
        if total <= cap_mb * 0.8e6:
            break


def _save_jpeg(img: Image.Image, dest: Path, quality: int = 86) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{uuid.uuid4().hex}.tmp")
    img.save(tmp, "JPEG", quality=quality, progressive=True)
    os.replace(tmp, dest)


class Background:
    """Re-scoring and re-clustering after a change, off the request, never alongside a running
    update of the same library (the update will do it anyway)."""

    def __init__(self, jobs: JobManager):
        self.jobs = jobs
        self.pending: dict[str, int] = {}
        self.timers: dict[str, threading.Timer] = {}
        self.trained_at: dict[str, float] = {}
        self.again: set[str] = set()          # regroups asked for while one was running
        self.errors: dict[str, str] = {}      # the last failure of each kind of background work
        self.running: set[str] = set()
        self.lock = threading.Lock()

    def _go(self, key: str, project: str, fn, wait: float = 0.0) -> None:
        """Run fn(conn) in the background with the library's lock. When it can't run (the same work
        is running already, or the lock is taken) it's skipped, and fn.skipped() is called if fn has one."""
        skipped = getattr(fn, "skipped", None)
        with self.lock:
            busy = key in self.running
            if busy and key.startswith("regroup:"):
                self.again.add(key)                    # the level changed again: run once more after
            if not busy:
                self.running.add(key)
        if busy:
            if skipped:
                skipped()
            return

        def run():
            try:
                with locks.project_lock(project, wait=wait):
                    conn = db.connect(project, create=False)
                    try:
                        fn(conn)
                    finally:
                        conn.close()
            except locks.Busy:
                if skipped:
                    skipped()
            except Exception as e:            # kept, shown by /api/p/<lib>/jobs/current, and logged
                import traceback

                self.errors[key] = f"{type(e).__name__}: {e}"
                print(f"[joeylab] {key} failed:\n{traceback.format_exc(limit=4)}", file=sys.stderr, flush=True)
            else:
                self.errors.pop(key, None)
            finally:
                with self.lock:
                    self.running.discard(key)
                    again = key in self.again
                    self.again.discard(key)
                if again:
                    self._go(key, project, fn, wait)

        threading.Thread(target=run, name=key, daemon=True).start()

    def busy(self, key: str) -> bool:
        with self.lock:
            return key in self.running

    def regroup(self, project: str) -> None:
        """Restack on a new grouping level and re-score. Waits for a running update rather than
        skipping it, since that update may already be past its grouping step."""
        from .. import grouping

        def fn(conn):
            grouping.run(conn)
            scoring.run(conn)
        self._go(f"regroup:{project}", project, fn, wait=1800)

    def reorganise(self, project: str) -> None:
        """Restack, re-score and find events again: after another copy of some duplicates became the
        kept one, or photos went to the Trash (stacks and events are made from kept, present photos)."""
        from .. import events, grouping

        def fn(conn):
            grouping.run(conn)
            scoring.run(conn)
            events.run(conn)
        self._go(f"regroup:keep:{project}", project, fn, wait=1800)

    def retrip(self, project: str) -> None:
        """Name places and find events and trips again: after your homes or the trip distance
        changed, or you set (or took away) an event's place. Asked again while running, it runs
        once more after."""
        from .. import events, geo

        def fn(conn):
            geo.assign(conn, log=lambda m: None, download=False)
            events.run(conn)
        self._go(f"regroup:trips:{project}", project, fn, wait=600)

    def rescore(self, project: str) -> None:
        if project not in self.jobs.running:
            self._go(f"rescore:{project}", project, scoring.run)

    def recluster(self, project: str) -> None:
        from .. import faces

        if project not in self.jobs.running:
            self._go(f"recluster:{project}", project, faces.cluster)

    QUIET_S = 20.0           # retrain once decisions pause this long...
    MIN_GAP_S = 120.0        # ...and no more often than this

    def decided(self, project: str, n: int, every: int = 20) -> None:
        """Retrain taste (then re-score) once `every` decisions have been made and they've paused for
        a moment: a run of quick decisions, or one big change, trains once rather than over and over."""
        with self.lock:
            self.pending[project] = self.pending.get(project, 0) + n
            if self.pending[project] < every:
                return
            self._arm(project, max(self.QUIET_S, self.MIN_GAP_S - (time.time() - self.trained_at.get(project, 0))))

    def _arm(self, project: str, wait: float) -> None:
        """(Re)start the retrain timer. Called with self.lock held."""
        old = self.timers.pop(project, None)
        if old:
            old.cancel()
        t = threading.Timer(wait, self._retrain, args=(project,))
        t.daemon = True
        self.timers[project] = t
        t.start()

    def _try_later(self, project: str, n: int = 0) -> None:
        """Retraining couldn't run just now: its n decisions count again and it's tried later, unless
        newer decisions have set a timer already."""
        with self.lock:
            self.pending[project] = self.pending.get(project, 0) + n
            if project not in self.timers:
                self._arm(project, self.MIN_GAP_S)

    def _retrain(self, project: str) -> None:
        key = f"retrain:{project}"
        with self.lock:
            self.timers.pop(project, None)
        if project in self.jobs.running or self.busy(key):
            # The update under way trains at its end, but may be past that step already; or the last
            # retrain is still running and may have read the decisions before these.
            self._try_later(project)
            return
        with self.lock:
            n = self.pending.get(project, 0)          # this retrain takes them; given back if it can't run
            self.pending[project] = 0

        def retrain(conn):
            taste.train_all()
            with self.lock:
                self.trained_at[project] = time.time()
            scoring.run(conn)
        retrain.skipped = lambda: self._try_later(project, n)     # the library's lock was taken
        self._go(key, project, retrain)


_POWER: dict = {"at": 0.0, "battery": False}


def on_battery() -> bool:
    """Whether this Mac is running on its battery right now (asked at most once a minute)."""
    if sys.platform != "darwin":
        return False
    if time.time() - _POWER["at"] > 60:
        import subprocess

        try:
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        _POWER.update(at=time.time(), battery="Battery Power" in out)
    return _POWER["battery"]


class Prerender:
    """Large previews made ahead of time, so culling never waits for one to be drawn: once photos are
    analysed (and when Cull opens), the undecided photos' previews in culling order, a bounded number,
    one at a time with a pause between them, and not while the Mac runs on its battery. Only the
    preview cache is written."""

    LIMIT = 1500              # photos per run
    PAUSE_S = 0.03            # between previews: leaves the cores to requests and the update
    AGAIN_S = 120.0           # Cull opening again within this long doesn't start another run

    def __init__(self, render, path_of, cap_mb: int):
        self.render, self.path_of, self.cap_mb = render, path_of, cap_mb
        self.running: set[str] = set()
        self.last: dict[str, float] = {}
        self.lock = threading.Lock()

    def start(self, project: str, soon: bool = False) -> bool:
        """Start a run for this library unless one is running (or, `soon`, ran a moment ago)."""
        with self.lock:
            if project in self.running or (soon and time.time() - self.last.get(project, 0) < self.AGAIN_S):
                return False
            self.running.add(project)
        threading.Thread(target=self._run, args=(project, config.HOME), name=f"prerender-{project}", daemon=True).start()
        return True

    def _run(self, project: str, home: Path) -> None:
        made = budget = 0
        try:
            if on_battery() or config.HOME != home:
                return
            try:
                conn = db.connect(project, create=False)
            except (OSError, ValueError, db.CatalogDamaged):
                return                            # the library went away (or is being restored)
            try:
                rows = conn.execute(
                    "SELECT s.id, s.thumb, s.analyzed_fp, s.orientation, f.path, f.kind FROM shots s"
                    " JOIN files f ON f.id = s.preview_file_id LEFT JOIN decisions d ON d.shot_id = s.id"
                    " WHERE s.status = 'present' AND s.analyzed_fp IS NOT NULL AND COALESCE(s.media, 'photo') = 'photo'"
                    " AND COALESCE(d.pick, 0) = 0 AND COALESCE(d.rating, 0) = 0"
                    " ORDER BY COALESCE(s.time_src, 'exif') = 'file', s.eff_ts IS NULL, s.eff_ts, s.id LIMIT ?",
                    (self.LIMIT,)).fetchall()
            finally:
                conn.close()
            for r in rows:
                if config.HOME != home:
                    break                         # JoeyLab's folder moved under it (only the tests do this)
                if self.path_of(project, r).exists():
                    continue
                if made % 50 == 49 and on_battery():
                    break
                try:
                    budget += self.render(project, r, "", PREVIEW_SIDE, prune=False).stat().st_size
                except Exception:
                    continue                      # unreadable right now: it's drawn when it's viewed
                made += 1
                if budget > self.cap_mb * 0.5e6:
                    break                         # never more than half the cache in one go
                time.sleep(self.PAUSE_S)
        finally:
            with self.lock:
                self.running.discard(project)
                self.last[project] = time.time()


TILE = 512


class _Decoded:
    """The last few full-size pictures decoded for 1:1 tiles, so the tiles of one view cost one
    decode, not one each. Kept to a budget of pixels."""

    BUDGET = 80_000_000

    def __init__(self):
        self.items: dict = {}
        self.lock = threading.Lock()
        self.locks: dict = {}

    def get(self, key, make):
        with self.lock:
            if key in self.items:
                self.items[key] = self.items.pop(key)          # most recent last
                return self.items[key]
            one = self.locks.setdefault(key, threading.Lock())
        with one:                                           # one decode per picture, however many tiles ask
            with self.lock:
                if key in self.items:
                    return self.items[key]
            im = make()
            with self.lock:
                self.items[key] = im
                while len(self.items) > 1 and sum(x.width * x.height for x in self.items.values()) > self.BUDGET:
                    self.items.pop(next(iter(self.items)))
                self.locks.pop(key, None)
            return im


def create_app(jobs: JobManager | None = None, token: str | None = None, allow: list | None = None,
               meta: dict | None = None, on_shutdown: list | None = None, allow_hosts: list[str] | None = None,
               owner: str | None = None, trust_identity: bool = True, share_only: bool = False) -> FastAPI:
    """The web app, for one user. `owner`: the Mac's own Tailscale login (devices signed in with it are
    the owner too); `trust_identity`: believe the identity `tailscale serve` adds (off when the server
    also listens on other addresses); `share_only`: serve share links and nothing else."""
    jobs = jobs or JobManager(watch_minutes=0)
    networks = allow if allow is not None else net.DEFAULT_ALLOWED
    extra_hosts = {h.lower() for h in (allow_hosts or [])}
    tail = (meta or {}).get("tailnet_url") or ""
    if tail:
        full = tail.split("://", 1)[-1].split(":", 1)[0].lower()
        extra_hosts |= {full, full.split(".", 1)[0]}

    @asynccontextmanager
    async def lifespan(_app):
        yield
        # Runs on Ctrl+C / SIGTERM before uvicorn re-raises the signal, so cleanup really happens.
        for fn in on_shutdown or []:
            try:
                fn()
            except Exception:
                pass

    app = FastAPI(title="JoeyLab", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.jobs = jobs
    app.state.meta = meta or {}
    app.state.owner = owner.strip().lower() if owner else None   # the Mac's Tailscale account
    app.state.trust_identity = trust_identity and not share_only
    app.state.trash_jobs = {}
    app.state.move_jobs = {}
    app.state.bg = Background(jobs)
    app.state.overviews = Overviews(jobs, app.state.bg)
    app.state.preview_writes = 0
    app.add_middleware(_ApiGzip)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.client.host if request.client else None
        if not net.is_allowed(host, networks):
            return PlainTextResponse("JoeyLab only answers on this Mac and your Tailscale network.", 403)
        problem = guard_problem(request, extra_hosts, public=share_only)
        if problem:
            return JSONResponse({"error": problem[1]}, status_code=problem[0])
        path = request.url.path
        if share_only and not (SHARE_PATHS.match(path) or (path.startswith(SHARE_STATIC) and ".." not in path)):
            return PlainTextResponse("Not found.", 404)        # the library itself isn't on this server
        shared = path.startswith(("/s/", "/api/share/", "/img/share/")) or path.startswith("/static/") or \
            path in ("/manifest.webmanifest", "/sw.js")
        if token and not shared:
            given = request.query_params.get("token")
            if given and hmac.compare_digest(given.encode(), token.encode()):
                resp = RedirectResponse(path or "/", 303)
                resp.set_cookie("joeylab_token", token, httponly=True, samesite="strict", max_age=90 * 86400)
                return resp
            if not hmac.compare_digest(request.cookies.get("joeylab_token", "").encode(), token.encode()):
                return PlainTextResponse("Open the link printed by `joeylab serve` (it carries the access token).", 401)
            request.state.token_ok = True
        # One user: anyone else gets share links and nothing more.
        if not shared and not share_only and not is_owner(request):
            if path.startswith("/api/") or path.startswith("/img/"):
                return JSONResponse({"error": PRIVATE}, status_code=403)
            return PlainTextResponse(PRIVATE, 403)
        resp = await call_next(request)
        if path.startswith("/static/") or path == "/" or path == "/sw.js":
            resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        if resp.headers.get("content-type", "").startswith("text/html"):
            resp.headers["Content-Security-Policy"] = SHARE_CSP if share_only else CSP
        return resp

    @app.exception_handler(HTTPException)
    async def http_error(_req, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(locks.Busy)
    async def busy_error(_req, exc: locks.Busy):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(db.CatalogDamaged)
    async def damaged_error(_req, exc: db.CatalogDamaged):
        return JSONResponse({"error": str(exc)}, status_code=503)

    @app.exception_handler(ValueError)
    async def bad_value(_req, exc: ValueError):
        # JoeyLab's own checks raise ValueError with a sentence meant for you.
        return JSONResponse({"error": str(exc) or "That value isn't valid."}, status_code=400)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript")

    from .routes import ai as r_ai
    from .routes import explore as r_explore
    from .routes import history as r_history
    from .routes import keywords as r_keywords
    from .routes import library as r_library
    from .routes import moves as r_moves
    from .routes import split as r_split
    from .routes import people as r_people
    from .routes import places as r_places
    from .routes import raws as r_raws
    from .routes import share as r_share
    from .routes import tidy as r_tidy
    from .routes import trash as r_trash

    for mod in (r_library, r_tidy, r_people, r_places, r_share, r_ai, r_explore, r_keywords, r_trash, r_history, r_raws, r_moves, r_split):
        app.include_router(mod.router)

    # ------------------------------------------------------------ images
    def _img_row(conn, sid: int):
        r = conn.execute(
            "SELECT s.id, s.thumb, s.analyzed_fp, s.orientation, f.path, f.kind FROM shots s"
            " JOIN files f ON f.id = s.preview_file_id WHERE s.id=?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404, "No such photo.")
        return r

    def small_thumb(p: str, conn, sid: int) -> Path:
        r = _img_row(conn, sid)
        if not r["thumb"] or not os.path.exists(r["thumb"]):
            raise HTTPException(404, "This photo hasn't been analysed yet.")
        v = library.version_tag(r["analyzed_fp"])
        dest = config.project_dir(p) / "thumbs_s" / f"{sid % 100:02d}" / f"{sid}_{v}.jpg"
        if not dest.exists():
            im = Image.open(r["thumb"])
            im.draft("RGB", (SMALL_THUMB, SMALL_THUMB))
            im = im.convert("RGB")
            im.thumbnail((SMALL_THUMB, SMALL_THUMB), Image.Resampling.LANCZOS)
            _save_jpeg(im, dest, 82)
            for old in dest.parent.glob(f"{sid}_*.jpg"):
                if old != dest:
                    old.unlink(missing_ok=True)
            app.state.preview_writes += 1
            if app.state.preview_writes % 500 == 0:
                _prune_cache(config.project_dir(p) / "thumbs_s", SMALL_CACHE_MB)
        return dest

    def medium_thumb(conn, sid: int) -> Path:
        r = _img_row(conn, sid)
        if not r["thumb"] or not os.path.exists(r["thumb"]):
            raise HTTPException(404, "This photo hasn't been analysed yet.")
        return Path(r["thumb"])

    def preview_path(p: str, r, suffix: str = "") -> Path:
        v = library.version_tag(r["analyzed_fp"])
        return config.project_dir(p) / "previews" / f"{r['id'] % 100:02d}" / f"{r['id']}_{v}{suffix}.jpg"

    def cached_render(p: str, r, suffix: str, side: int, prune: bool = True) -> Path:
        cache = config.project_dir(p) / "previews"
        dest = preview_path(p, r, suffix)
        if dest.exists():
            os.utime(dest)  # keeps recently viewed previews in the cache
            return dest
        if not os.path.exists(r["path"]):
            if r["thumb"] and os.path.exists(r["thumb"]):
                return Path(r["thumb"])          # drive unplugged: the catalog's own thumbnail
            raise HTTPException(404, "The original file isn't on disk right now.")
        img = load_preview(r["path"], r["kind"], r["orientation"], side)
        _save_jpeg(img, dest, 88)
        app.state.preview_writes += 1
        if prune and app.state.preview_writes % 100 == 0:
            _prune_cache(cache, PREVIEW_CACHE_MB)
        return dest

    app.state.images = {"small": small_thumb, "medium": medium_thumb, "render": cached_render, "row": _img_row}
    app.state.prerender = Prerender(cached_render, preview_path, PREVIEW_CACHE_MB)
    jobs.after_analysis.append(app.state.prerender.start)
    decoded = _Decoded()

    def full_size(p: str, r, d: int) -> Image.Image:
        """The photo at full size (d=1) or half (d=2), decoded once for all its tiles."""
        if not os.path.exists(r["path"]):
            raise HTTPException(404, "The original file isn't on disk right now.")
        v = library.version_tag(r["analyzed_fp"])
        full = decoded.get((p, r["id"], v, 1), lambda: load_preview(r["path"], r["kind"], r["orientation"], 100_000))
        if d == 1:
            return full
        return decoded.get((p, r["id"], v, 2), lambda: full.reduce(2))

    def tile_dir(p: str, r) -> Path:
        return config.project_dir(p) / "previews" / f"{r['id'] % 100:02d}" / f"{r['id']}_{library.version_tag(r['analyzed_fp'])}_tiles"

    @app.get("/img/{p}/t/{sid}")
    def img_thumb(p: str, sid: int, s: str = "s", conn=Depends(conn_dep)):
        path = small_thumb(p, conn, sid) if s == "s" else medium_thumb(conn, sid)
        return FileResponse(path, media_type="image/jpeg", headers=IMMUTABLE)

    @app.get("/img/{p}/atlas/{k}")
    def img_atlas(p: str, k: int, conn=Depends(conn_dep)):
        """Explore's small thumbnails, a thousand to an image (explore.atlas_sheet)."""
        from .. import explore

        return FileResponse(explore.atlas_sheet(p, conn, max(0, k)), media_type="image/jpeg", headers=IMMUTABLE)

    @app.get("/img/{p}/v/{sid}")
    def img_preview(p: str, sid: int, conn=Depends(conn_dep)):
        r = _img_row(conn, sid)
        return FileResponse(cached_render(p, r, "", PREVIEW_SIDE), media_type="image/jpeg", headers=IMMUTABLE)

    @app.get("/img/{p}/z/{sid}")
    def img_zoom(p: str, sid: int, conn=Depends(conn_dep)):
        """Full resolution for 1:1 zoom: the JPEG itself, or a full-size render of RAW/HEIC."""
        r = _img_row(conn, sid)
        if r["kind"] == "jpeg" and os.path.exists(r["path"]):
            return FileResponse(r["path"], media_type="image/jpeg", headers=IMMUTABLE)
        return FileResponse(cached_render(p, r, "_z", 12000), media_type="image/jpeg", headers=IMMUTABLE)

    @app.get("/img/{p}/tiles/{sid}")
    def img_tiles(p: str, sid: int, conn=Depends(conn_dep)):
        """The photo's full size, for zooming to 50% and 100% in tiles: the visible part is sent, not
        the whole original."""
        r = _img_row(conn, sid)
        info = tile_dir(p, r) / "info.json"
        if info.exists():
            return FileResponse(info, media_type="application/json", headers=IMMUTABLE)
        im = full_size(p, r, 1)
        body = {"w": im.width, "h": im.height, "tile": TILE}
        info.parent.mkdir(parents=True, exist_ok=True)
        tmp = info.with_name(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(body))
        os.replace(tmp, info)
        return JSONResponse(body, headers=IMMUTABLE)

    @app.get("/img/{p}/tile/{sid}/{d}/{x}/{y}")
    def img_tile(p: str, sid: int, d: int, x: int, y: int, conn=Depends(conn_dep)):
        """One TILE-pixel square of the photo at full size (d=1) or half (d=2), cut once and kept with
        the previews."""
        if d not in (1, 2) or x < 0 or y < 0:
            raise HTTPException(404, "No such tile.")
        r = _img_row(conn, sid)
        dest = tile_dir(p, r) / f"{d}_{x}_{y}.jpg"
        if not dest.exists():
            im = full_size(p, r, d)
            if x * TILE >= im.width or y * TILE >= im.height:
                raise HTTPException(404, "No such tile.")
            _save_jpeg(im.crop((x * TILE, y * TILE, min(im.width, (x + 1) * TILE), min(im.height, (y + 1) * TILE))), dest, 90)
        return FileResponse(dest, media_type="image/jpeg", headers=IMMUTABLE)

    @app.get("/img/{p}/f/{fid}")
    def img_face(p: str, fid: int, conn=Depends(conn_dep)):
        f = conn.execute("SELECT shot_id, x, y, w, h FROM faces WHERE id=?", (fid,)).fetchone()
        if not f:
            raise HTTPException(404, "No such face.")
        r = _img_row(conn, f["shot_id"])
        v = library.version_tag(r["analyzed_fp"])
        dest = config.project_dir(p) / "previews" / "faces" / f"{fid % 100:02d}" / f"{fid}_{v}.jpg"
        if not dest.exists():
            im = Image.open(cached_render(p, r, "", PREVIEW_SIDE))
            W, H = im.size
            cx, cy = (f["x"] + f["w"] / 2) * W, (f["y"] + f["h"] / 2) * H
            s = max(f["w"] * W, f["h"] * H) * 0.8
            face = im.crop((int(cx - s), int(cy - s), int(cx + s), int(cy + s))).convert("RGB")
            face = face.resize((200, 200), Image.Resampling.LANCZOS)
            _save_jpeg(face, dest, 85)
        return FileResponse(dest, media_type="image/jpeg", headers=IMMUTABLE)

    @app.get("/img/{p}/o/{fid}")
    def img_original(p: str, fid: int, conn=Depends(conn_dep)):
        f = conn.execute("SELECT path, status FROM files WHERE id=?", (fid,)).fetchone()
        if not f or f["status"] != "present" or not os.path.exists(f["path"]):
            raise HTTPException(404, "The original file isn't on disk right now.")
        ext = os.path.splitext(f["path"])[1].lower()
        return FileResponse(f["path"], media_type=MEDIA.get(ext, "application/octet-stream"),
                            filename=os.path.basename(f["path"]),
                            content_disposition_type="inline" if ext in MEDIA else "attachment")

    @app.get("/img/{p}/sharp/{sid}")
    def img_sharpness_map(p: str, sid: int, conn=Depends(conn_dep)):
        """Where the photo is sharp: focus measured per tile of the catalog's thumbnail (never the
        original), for the loupe's overlay."""
        import cv2
        import numpy as np

        path = medium_thumb(conn, sid)
        gray = cv2.cvtColor(np.asarray(Image.open(path).convert("RGB")), cv2.COLOR_RGB2GRAY)
        lap = cv2.Laplacian(gray, cv2.CV_64F)
        h, w = gray.shape
        n = 12
        grid = [[float(lap[i * h // n:(i + 1) * h // n, j * w // n:(j + 1) * w // n].var()) for j in range(n)] for i in range(n)]
        flat = [v for row in grid for v in row]
        top = max(flat) or 1.0
        return {"n": n, "grid": [[round(min(1.0, (np.log10(v + 1) / np.log10(top + 1))), 3) for v in row] for row in grid]}

    # ------------------------------------------------------------ read-only share pages
    @app.get("/s/{token}", include_in_schema=False)
    def share_page(token: str):
        return FileResponse(STATIC / "share.html", media_type="text/html")

    @app.get("/{rest:path}", include_in_schema=False)
    def fallback(rest: str):
        if rest.startswith(("api/", "img/")):
            raise HTTPException(404, "Not found.")
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))

    return app


def create_share_app(allow: list | None = None) -> FastAPI:
    """Share links only (`joeylab share-server`): the share page, its photos and favourites, and
    nothing of the library itself. Safe to publish beyond your tailnet through a proxy or tunnel."""
    return create_app(allow=allow, share_only=True)


def viewer(request: Request) -> str | None:
    return viewer_of(request)
