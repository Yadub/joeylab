"""joeylab — sort, group, rate and find the good stuff, without touching your folders.

Made by Yaduraj Bhageria. MIT licence."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table

from . import config, db, export, library, locks, pipeline, scanner, termimg
from .library import Filter

app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich",
                  help=__doc__)
export_app = typer.Typer(no_args_is_help=True, help="Albums, lists, CSV and XMP sidecars. Originals are never modified.")
roots_app = typer.Typer(no_args_is_help=True, help="The folders a library includes.")
library_app = typer.Typer(no_args_is_help=True, help="Your libraries: list, rename or remove one (JoeyLab's records only; "
                                                     "photo folders are never touched).")
service_app = typer.Typer(no_args_is_help=True, help="Keep the web app running in the background (starts at login).")
app.add_typer(service_app, name="service")


def _version(value: bool) -> None:
    if value:
        from . import __author__, __version__

        console.print(f"JoeyLab {__version__} · made by {__author__} · MIT licence")
        raise typer.Exit()


@app.callback()
def _root(version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True,
                                                help="Show the version and exit.")] = False) -> None:
    pass
app.add_typer(export_app, name="export")
app.add_typer(roots_app, name="roots")
app.add_typer(library_app, name="library")
console = Console(highlight=False)

# (--project and JOEYLAB_PROJECT, the old names, still work.)
Project = Annotated[str, typer.Option("--library", "--project", "-p", envvar=["JOEYLAB_LIBRARY", "JOEYLAB_PROJECT"],
                                      help="Which library (e.g. wedding, archive).")]
DEFAULT_PROJECT = "default"

# Shared filter options
Picks = Annotated[bool, typer.Option("--picks", help="Only shots you picked.")]
Rejects = Annotated[bool, typer.Option("--rejects", help="Only shots you rejected.")]
Undecided = Annotated[bool, typer.Option("--undecided", help="Only shots with no rating/pick yet.")]
MinRating = Annotated[Optional[int], typer.Option("--min-rating", "-r", min=0, max=5, help="Minimum stars.")]
Suggest = Annotated[Optional[str], typer.Option("--suggested", help="Suggestion: pick | reject | check (worth checking).")]
Flag = Annotated[Optional[str], typer.Option("--flag", help="blurry, soft, eyes-closed, overexposed, underexposed, duplicate, best-in-group, softer-than-twin")]
MinQ = Annotated[Optional[float], typer.Option("--min-quality", "-q", help="Minimum quality score (0-100).")]
MaxQ = Annotated[Optional[float], typer.Option("--max-quality", help="Maximum quality score.")]
Camera = Annotated[Optional[str], typer.Option("--camera", help="Camera model or serial substring.")]
PathSub = Annotated[Optional[str], typer.Option("--in", help="Folder path substring.")]
DateFrom = Annotated[Optional[str], typer.Option("--from", help="Captured on/after YYYY-MM-DD.")]
DateTo = Annotated[Optional[str], typer.Option("--to", help="Captured on/before YYYY-MM-DD.")]
Sort = Annotated[str, typer.Option("--sort", help="time | quality | sharpness | rating | path")]


def _flt(picks=False, rejects=False, undecided=False, min_rating=None, suggested=None, flag=None,
         min_quality=None, max_quality=None, camera=None, path_in=None, date_from=None, date_to=None) -> Filter:
    return Filter(picks=picks, rejects=rejects, undecided=undecided, min_rating=min_rating,
                  suggestion=suggested, flag=flag, min_quality=min_quality, max_quality=max_quality,
                  camera=camera, path_contains=path_in, date_from=date_from, date_to=date_to)


def _conn(project: str, create: bool = False):
    try:
        return db.connect(project, create=create)
    except (FileNotFoundError, ValueError, db.CatalogDamaged) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)


class RichReporter(pipeline.Reporter):
    """Shows pipeline progress as a rich progress bar plus coloured log lines."""

    COLORS = {"ok": "green", "warn": "yellow", "error": "red", "info": "dim"}

    def __init__(self):
        self.prog = Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
                             TimeRemainingColumn(), console=console, transient=True)
        self.task = None

    def __enter__(self):
        self.prog.start()
        return self

    def __exit__(self, *exc):
        self.prog.stop()

    def stage(self, name: str, total: int = 0) -> None:
        if self.task is not None:
            self.prog.remove_task(self.task)
        self.task = self.prog.add_task(name, total=total or None)

    def advance(self, done: int, total: int | None = None) -> None:
        if self.task is not None:
            self.prog.update(self.task, completed=done, total=total)

    def log(self, msg: str, level: str = "info") -> None:
        head, sep, rest = msg.partition(": ")
        color = self.COLORS.get(level, "dim")
        if level == "ok" and sep and len(head) < 12:
            console.print(f"[green]{head}:[/] {rest}")
        else:
            console.print(f"[{color}]{msg}[/]")


def _pipeline(conn, project: str, paths: list[Path] | None = None, **kw) -> None:
    with RichReporter() as rep:
        if paths:
            try:
                pipeline.add_roots(conn, paths, rep, project)
            except (NotADirectoryError, ValueError) as e:
                console.print(f"[red]Cannot add folder: {e}[/]")
                raise typer.Exit(1)
        if kw.get("do_scan", True) and not conn.execute("SELECT 1 FROM roots").fetchone():
            console.print("[red]No folders yet. Give a path: joeylab run ~/Pictures/Wedding[/]")
            raise typer.Exit(1)
        from . import locks

        if locks.is_busy(project):
            rep.log("The background service is updating this library; waiting for it to finish…", "warn")
        try:
            pipeline.run_all(conn, project, rep, lock_wait=1800, **kw)
        except locks.Busy as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(1)


@app.command()
def run(paths: Annotated[Optional[list[Path]], typer.Argument(help="Folders to add (optional after the first time).")] = None,
        project: Project = DEFAULT_PROJECT,
        faces: Annotated[bool, typer.Option(help="Detect faces / closed eyes.")] = True,
        workers: Annotated[Optional[int], typer.Option(help="Parallel workers.")] = None,
        ai_layer: Annotated[bool, typer.Option("--ai/--no-ai", help="CLIP embeddings + aesthetic score (if installed).")] = True):
    """[bold]Update a library[/]: find new and changed photos, analyse them, stack them, find duplicates and suggest picks, then faces, search and places. Safe to re-run any time."""
    conn = _conn(project, create=True)
    _pipeline(conn, project, paths, faces=faces, workers=workers, ai_layer=ai_layer)
    console.print(f"\nNext: [bold]joeylab cull -p {project}[/]  or  [bold]joeylab status -p {project}[/]")


@app.command()
def scan(paths: Annotated[Optional[list[Path]], typer.Argument()] = None, project: Project = DEFAULT_PROJECT):
    """Only look for new, changed, moved and deleted files (read-only); `joeylab run` does the rest."""
    conn = _conn(project, create=True)
    with locks.project_lock(project, wait=1800), RichReporter() as rep:      # never alongside the service's update
        try:
            pipeline.add_roots(conn, paths or [], rep, project)
        except (NotADirectoryError, ValueError) as e:
            console.print(f"[red]Cannot add folder: {e}[/]")
            raise typer.Exit(1)
        pipeline.scan(conn, rep)


@app.command()
def analyze(project: Project = DEFAULT_PROJECT,
            force: Annotated[bool, typer.Option(help="Re-analyse everything.")] = False,
            faces: Annotated[bool, typer.Option(help="Detect faces / closed eyes.")] = True,
            workers: Annotated[Optional[int], typer.Option()] = None,
            ai_layer: Annotated[bool, typer.Option("--ai/--no-ai", help="CLIP embeddings + aesthetic score (if installed).")] = True):
    """Measure sharpness, exposure, eyes and how alike photos look (and the AI picture vectors) for new or changed photos."""
    _pipeline(_conn(project), project, do_scan=False, force=force, faces=faces, workers=workers, ai_layer=ai_layer)


@app.command()
def organize(project: Project = DEFAULT_PROJECT):
    """Stack, find duplicates, score, and find places and events again (fast; no photo is read)."""
    with locks.project_lock(project, wait=1800), RichReporter() as rep:
        pipeline.organize(_conn(project), rep, project)


# ---------------------------------------------------------------- viewing

@app.command()
def status(project: Project = DEFAULT_PROJECT):
    """A library at a glance."""
    conn = _conn(project)
    one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    t = Table(show_header=False, box=None)
    t.add_row("Folders", "\n".join(r[0] + ("" if os.path.isdir(r[0]) else "  [yellow](unavailable)[/]")
                                   for r in conn.execute("SELECT path FROM roots")))
    failed = one("SELECT COUNT(*) FROM shots WHERE status='present' AND error IS NOT NULL AND analyzed_fp IS NULL")
    n_shots = one("SELECT COUNT(*) FROM shots WHERE status='present'") - failed
    n_files = one("SELECT COUNT(*) FROM files WHERE status='present'")
    n_pairs = one("SELECT COUNT(*) FROM shots s WHERE status='present'"
                  " AND EXISTS (SELECT 1 FROM files f WHERE f.shot_id=s.id AND f.kind='raw' AND f.status='present')"
                  " AND EXISTS (SELECT 1 FROM files f WHERE f.shot_id=s.id AND f.kind='jpeg' AND f.status='present')")
    t.add_row("Photos", f"{n_shots}  [dim]({_count(n_files, 'file')}, {_count(n_pairs, 'RAW + JPEG pair')})[/]")
    if failed:
        t.add_row("Unreadable", f"[yellow]{_count(failed, 'file')}[/] couldn't be read as photos (`joeylab health` lists them)")
    missing = one("SELECT COUNT(*) FROM shots WHERE status='missing'")
    if missing:
        t.add_row("Missing", f"[yellow]{_count(missing, 'photo')}[/] no longer on disk (decisions kept; `joeylab prune` to forget them)")
    t.add_row("Analysed", str(one("SELECT COUNT(*) FROM shots s JOIN metrics m ON m.shot_id=s.id WHERE s.status='present'")))
    t.add_row("Stacks", f"{one('SELECT COUNT(*) FROM groups')} ({one('SELECT COUNT(*) FROM groups WHERE size>1')} with more than one frame)")
    exact = one("SELECT COUNT(*) FROM dup_sets WHERE kind='exact'")
    copies = one("SELECT COUNT(*) FROM dup_sets WHERE kind='copy'")
    t.add_row("Duplicates", f"{_count(exact, 'set')} of identical files, {_count(copies, 'set')} of re-saved copies")
    from . import copies as copies_mod

    if copies_mod.ensure(conn, project):
        c = copies_mod.counts(conn)
        t.add_row("Backups", f"{_count(c['elsewhere'], 'photo')} with a copy on another drive ({c['checked']:,} checked"
                             f" byte for byte) · {c['only']:,} only on this drive  [dim](`joeylab backups` for more)[/]")
    s_pick = one("SELECT COUNT(*) FROM metrics m JOIN shots s ON s.id=m.shot_id WHERE s.status='present' AND suggestion='pick'")
    s_rej = one("SELECT COUNT(*) FROM metrics m JOIN shots s ON s.id=m.shot_id WHERE s.status='present' AND suggestion='reject'")
    t.add_row("Suggested", f"{s_pick} picks · {s_rej} rejects")
    t.add_row("Your picks", str(one("SELECT COUNT(*) FROM decisions d JOIN shots s ON s.id=d.shot_id WHERE s.status='present' AND pick=1")))
    t.add_row("Your rejects", str(one("SELECT COUNT(*) FROM decisions d JOIN shots s ON s.id=d.shot_id WHERE s.status='present' AND pick=-1")))
    stars = conn.execute("SELECT rating, COUNT(*) FROM decisions d JOIN shots s ON s.id=d.shot_id WHERE s.status='present' AND rating>0 GROUP BY rating ORDER BY rating DESC").fetchall()
    t.add_row("Ratings", "  ".join(f"{'★' * r}:{n}" for r, n in stars) or "none yet")
    flags = {}
    for (fl,) in conn.execute("SELECT flags FROM metrics m JOIN shots s ON s.id=m.shot_id WHERE s.status='present'"):
        for f in library._row({"flags": fl})["flags"]:
            flags[f] = flags.get(f, 0) + 1
    t.add_row("Flags", "  ".join(f"{k}:{v}" for k, v in sorted(flags.items(), key=lambda x: -x[1])) or "-")
    t.add_row("Catalog", str(config.project_dir(project)))
    console.print(t)


@app.command("list")
def list_cmd(project: Project = DEFAULT_PROJECT, picks: Picks = False, rejects: Rejects = False,
             undecided: Undecided = False, min_rating: MinRating = None, suggested: Suggest = None,
             flag: Flag = None, min_quality: MinQ = None, max_quality: MaxQ = None, camera: Camera = None,
             path_in: PathSub = None, date_from: DateFrom = None, date_to: DateTo = None,
             sort: Sort = "time", limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
             show_paths: Annotated[bool, typer.Option("--paths", help="Print full paths only.")] = False):
    """List photos matching filters."""
    conn = _conn(project)
    rows = library.shots(conn, _flt(picks, rejects, undecided, min_rating, suggested, flag, min_quality,
                                    max_quality, camera, path_in, date_from, date_to), sort=sort, limit=limit or None)
    if show_paths:
        for r in rows:
            print(r["preview_path"])
        return
    t = Table(header_style="bold")
    for c in ("id", "captured", "file", "q", "flags", "sugg", "you", "group"):
        t.add_column(c)
    for r in rows:
        you = ("★" * (r["rating"] or 0)) + {1: " pick", -1: " reject"}.get(r["pick"], "")
        t.add_row(str(r["id"]), r["capture_str"] or "", r["stem"],
                  f"{r['quality']:.0f}" if r["quality"] is not None else "-",
                  ", ".join(r["flags"]), r["suggestion"] or "", you,
                  f"{r['group_rank']}/{r['group_size']}" if (r["group_size"] or 1) > 1 else "")
    console.print(t)
    console.print(f"[dim]{len(rows)} shown[/]")


@app.command()
def show(shot_id: int, project: Project = DEFAULT_PROJECT,
         full: Annotated[bool, typer.Option(help="Show full-size preview instead of thumbnail.")] = False):
    """Show one photo inline with its metrics."""
    conn = _conn(project)
    s = library.shot(conn, shot_id)
    if not s:
        console.print("[red]No such photo[/]")
        raise typer.Exit(1)
    from .imaging import load_preview
    from PIL import Image
    img = load_preview(s["preview_path"], s["preview_kind"], s["orientation"], 2048) if full else Image.open(s["thumb"])
    if not termimg.show(img):
        _image_hint()
        console.print("[dim](opening in Preview instead)[/]")
        termimg.open_external(s["preview_path"])
    m = conn.execute("SELECT * FROM metrics WHERE shot_id=?", (shot_id,)).fetchone()
    console.print(f"[bold]{s['stem']}[/] · {s['capture_str']} · {s['camera']} · {s['lens'] or ''} · {library.describe(s)}")
    for f in library.files_of(conn, shot_id, present_only=False):
        console.print(f"  {f['kind']:5} {f['path']}" + ("" if f["status"] == "present" else " [yellow](missing)[/]"))
    if m:
        console.print(f"  quality {m['quality']}  suggestion {m['suggestion']}  flags {', '.join(s['flags']) or '-'}")
        console.print(f"  sharp(subject) {m['sharp_subject']:.0f}  face-sharp {m['face_sharp'] or 0:.0f}  "
                      f"brightness {m['brightness']:.0f}  highlights-clipped {m['clip_high'] * 100:.1f}%  "
                      f"faces {m['main_faces']} (eyes closed {m['eyes_closed']})")


def _show_results(conn, hits: list[tuple[int, float]], album: Optional[Path], label: str) -> None:
    rows = [(library.shot(conn, sid), score) for sid, score in hits]
    rows = [(r, sc) for r, sc in rows if r]
    if not rows:
        console.print("No results (run `joeylab run` with the ai extra installed first).")
        return
    items = [{"thumb": r["thumb"], "index": i + 1, "rating": r["rating"], "pick": r["pick"],
              "suggestion": None, "subtitle": f"#{r['id']} {r['stem']} {label}{sc:.2f}"} for i, (r, sc) in enumerate(rows)]
    if termimg.protocol():
        termimg.show(termimg.contact_sheet(items[:24], cols=6, cell=260))
    t = Table(header_style="bold")
    for c in ("#", "id", "captured", "file", label.strip(":") or "score", "q", "you"):
        t.add_column(c)
    for i, (r, sc) in enumerate(rows, 1):
        you = ("★" * (r["rating"] or 0)) + {1: " pick", -1: " reject"}.get(r["pick"], "")
        t.add_row(str(i), str(r["id"]), r["capture_str"] or "", r["stem"], f"{sc:.3f}",
                  f"{r['quality']:.0f}" if r["quality"] is not None else "-", you)
    console.print(t)
    if album:
        try:
            album = export.check_dest(album)      # never inside a folder JoeyLab only reads
        except ValueError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(1)
        album.mkdir(parents=True, exist_ok=True)
        n = 0
        for r, _ in rows:
            for f in export.select_files(conn, r, "preferred"):
                link = album / export._album_name(r, f)
                if not link.exists() and not link.is_symlink():
                    os.symlink(f["path"], link)
                    n += 1
        console.print(f"Linked {n} photos into {album}")


@app.command()
def search(query: str, project: Project = DEFAULT_PROJECT,
           limit: Annotated[int, typer.Option("--limit", "-n")] = 24,
           album: Annotated[Optional[Path], typer.Option("--album", help="Also symlink results into this folder.")] = None):
    """[bold]Find photos by description[/], e.g. "bride and groom laughing", "baby", "group photo outdoors"."""
    from . import ai

    if not ai.available():
        console.print("Needs the AI layer: uv sync --extra ai")
        raise typer.Exit(1)
    conn = _conn(project)
    with console.status("Searching…"):
        hits = ai.search(conn, query, limit)
    _show_results(conn, hits, album, "match:")


@app.command()
def similar(shot_id: int, project: Project = DEFAULT_PROJECT,
            limit: Annotated[int, typer.Option("--limit", "-n")] = 24,
            album: Annotated[Optional[Path], typer.Option("--album")] = None):
    """Photos that look like photo ID (any time, any camera)."""
    from . import ai

    conn = _conn(project)
    _show_results(conn, ai.similar(conn, shot_id, limit), album, "sim:")


@app.command("dupes")
def dupes_cmd(project: Project = DEFAULT_PROJECT):
    """Show duplicate sets. Nothing is deleted; `export list --flag duplicate` gives the extras."""
    conn = _conn(project)
    sets = conn.execute("SELECT id, kind FROM dup_sets WHERE kind != 'backup' ORDER BY kind, id").fetchall()
    backups = conn.execute("SELECT COUNT(*) FROM dup_sets WHERE kind = 'backup'").fetchone()[0]
    if backups:
        console.print(f"[dim]{_count(backups, 'file')} also on another drive of this library: backups, not duplicates"
                      " (`joeylab backups`).[/]")
    if not sets:
        console.print("No duplicates found.")
        return
    for ds in sets:
        console.print(f"[bold]{ds['kind']}[/] set {ds['id']}")
        for m in conn.execute("SELECT dm.keep, f.path, f.size FROM dup_members dm JOIN files f ON f.id=dm.file_id WHERE dm.set_id=? ORDER BY dm.keep DESC", (ds["id"],)):
            console.print(f"  {'[green]keep[/] ' if m['keep'] else '[dim]extra[/]'} {m['path']} [dim]({m['size'] / 1e6:.1f} MB)[/]")


def _image_hint() -> None:
    if termimg.in_tmux() and os.environ.get("LC_TERMINAL") == "iTerm2":
        console.print(f"[yellow]{termimg.TMUX_HINT}[/]")
    else:
        console.print("[yellow]This terminal can't show inline images (use iTerm2, WezTerm, Kitty or Ghostty).[/]")


# ---------------------------------------------------------------- deciding

@app.command()
def cull(project: Project = DEFAULT_PROJECT, picks: Picks = False, rejects: Rejects = False,
         undecided: Undecided = False, min_rating: MinRating = None, suggested: Suggest = None,
         flag: Flag = None, min_quality: MinQ = None, max_quality: MaxQ = None, camera: Camera = None,
         path_in: PathSub = None, date_from: DateFrom = None, date_to: DateTo = None,
         groups_only: Annotated[bool, typer.Option("--groups-only", help="Only bursts/similar-scene groups.")] = False,
         page_size: Annotated[int, typer.Option(min=1, max=24)] = 8,
         cols: Annotated[int, typer.Option(min=1, max=8)] = 4,
         start: Annotated[int, typer.Option(help="Start at page N.")] = 1):
    """[bold]Interactive culling[/] in the terminal (inline images in iTerm2/WezTerm/Kitty/Ghostty)."""
    from .cull import Culler, build_pages

    if not sys.stdin.isatty():
        console.print("[red]cull needs an interactive terminal[/]")
        raise typer.Exit(1)
    conn = _conn(project)
    flt = _flt(picks, rejects, undecided, min_rating, suggested, flag, min_quality, max_quality, camera,
               path_in, date_from, date_to)
    rows = [r for r in library.shots(conn, flt, sort="time") if r.get("thumb")]
    # Order by group start so a group stays together even across interleaved cameras.
    starts = {g["id"]: g["start_ts"] for g in conn.execute("SELECT id, start_ts FROM groups")}
    rows.sort(key=lambda r: (starts.get(r["group_id"], r["capture_ts"] or 0), r["group_id"] or 0, r["capture_ts"] or 0))
    pages = build_pages(rows, page_size, groups_only)
    c = Culler(conn, project, pages, cols=cols)
    c.page = max(0, min(len(pages) - 1, start - 1)) if pages else 0
    if not c.images:
        _image_hint()
        console.print("[yellow]Showing a text list instead (press o to open a photo in Preview). Press any key…[/]")
        from .cull import getkey, raw_terminal
        with raw_terminal():
            getkey()
    c.run()
    console.print(f"Saved. {c.decided_count()} photos decided. Resume any time with [bold]joeylab cull -p {project} --undecided[/]")


@app.command()
def rate(shot_ids: list[int], project: Project = DEFAULT_PROJECT,
         stars: Annotated[Optional[int], typer.Option("--stars", "-s", min=0, max=5)] = None,
         pick: Annotated[bool, typer.Option("--pick")] = False,
         reject: Annotated[bool, typer.Option("--reject")] = False,
         clear: Annotated[bool, typer.Option("--clear", help="Remove pick/reject flag.")] = False):
    """Set stars / pick / reject for photos by id (non-interactive)."""
    conn = _conn(project)
    p = 1 if pick else -1 if reject else 0 if clear else None
    _, changed = library.decide(conn, [{"id": sid, "rating": stars, "pick": p} for sid in shot_ids], what="rate", source="cli")
    console.print(f"Updated {len(changed)} photos. [dim]`joeylab undo` reverses this.[/]")


@app.command("accept-suggestions")
def accept_suggestions(project: Project = DEFAULT_PROJECT,
                       only: Annotated[Optional[str], typer.Option(help="pick | reject")] = None):
    """Turn suggestions into picks/rejects for photos you haven't decided on yet."""
    conn = _conn(project)
    rows = library.shots(conn, Filter(undecided=True, no_flag="tiny"))
    items = [{"id": r["id"], "pick": 1 if r["suggestion"] == "pick" else -1} for r in rows
             if r.get("suggestion") in ("pick", "reject") and (only is None or only == r["suggestion"])]
    _, changed = library.decide(conn, items, what=f"accept suggested {only or 'picks and rejects'}", source="cli")
    console.print(f"Applied {len(changed)} suggestions. [dim]`joeylab undo` reverses them in one step.[/]")


@app.command()
def undo(project: Project = DEFAULT_PROJECT):
    """Reverse the latest change to your decisions (from the web app or the command line)."""
    conn = _conn(project)
    res = library.undo(conn)
    console.print(f"Undid: {res['label']} ({len(res['ids'])} photos)" if res else "Nothing to undo.")
    if res and res["skipped"]:
        one = res["skipped"] == 1
        console.print(f"[yellow]{res['skipped']} {'photo was' if one else 'photos were'} changed again since;"
                      f" {'that change was' if one else 'those changes were'} kept.[/]")


@app.command()
def redo(project: Project = DEFAULT_PROJECT):
    """Redo the change you just undid (only straight after an undo: a new decision ends it)."""
    conn = _conn(project)
    res = library.redo(conn)
    console.print(f"Redid: {res['label']} ({len(res['ids'])} photos)" if res else "Nothing to redo.")


@app.command()
def history(project: Project = DEFAULT_PROJECT, limit: Annotated[int, typer.Option("--limit", "-n")] = 20):
    """Recent changes to your decisions, newest first."""
    import time as _t

    conn = _conn(project)
    for h in library.history(conn, limit):
        when = _t.strftime("%d %b %H:%M", _t.localtime(h["ts"]))
        bits = [f"{h['picked']} picked" if h["picked"] else "", f"{h['rejected']} rejected" if h["rejected"] else "",
                f"{h['rated']} rated" if h["rated"] else ""]
        console.print(f"{when}  {h['label']}  [dim]{h['n']} photos · {', '.join(b for b in bits if b) or 'labels'}"
                      f" · {h['source']}{' · undone' if h['undone'] else ''}[/]")


# ---------------------------------------------------------------- export

def _export_filters(picks, rejects, undecided, min_rating, suggested, flag, min_quality, max_quality,
                    camera, path_in, date_from, date_to) -> Filter:
    flt = _flt(picks, rejects, undecided, min_rating, suggested, flag, min_quality, max_quality, camera,
               path_in, date_from, date_to)
    if not any([picks, rejects, undecided, min_rating, suggested, flag, min_quality, max_quality]):
        console.print("[yellow]No selection filter given (e.g. --picks, --min-rating 4); using all photos.[/]")
    return flt


Which = Annotated[str, typer.Option("--files", help="preferred (RAW if any) | raw | jpeg | all")]


@export_app.command("album")
def export_album(dest: Path, project: Project = DEFAULT_PROJECT, picks: Picks = False, rejects: Rejects = False,
                 undecided: Undecided = False, min_rating: MinRating = None, suggested: Suggest = None,
                 flag: Flag = None, min_quality: MinQ = None, max_quality: MaxQ = None, camera: Camera = None,
                 path_in: PathSub = None, date_from: DateFrom = None, date_to: DateTo = None,
                 which: Which = "preferred",
                 copy: Annotated[bool, typer.Option("--copy", help="Copy files instead of symlinking.")] = False,
                 clean: Annotated[bool, typer.Option("--clean", help="Remove symlinks in DEST that no longer match.")] = False):
    """Make a folder of [bold]symlinks[/] (or copies) to matching photos — a virtual album."""
    if which not in export.FILE_CHOICES:
        raise typer.BadParameter(f"--files must be one of {export.FILE_CHOICES}")
    conn = _conn(project)
    flt = _export_filters(picks, rejects, undecided, min_rating, suggested, flag, min_quality, max_quality,
                          camera, path_in, date_from, date_to)
    try:
        r = export.album(conn, flt, dest, which=which, copy=copy, clean=clean)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    console.print(f"{'Copied' if copy else 'Linked'} {r.created} new, {r.skipped} already there"
                  + (f", removed {r.removed} stale links" if r.removed else "") + f" → {r.dest}")


@export_app.command("list")
def export_list(project: Project = DEFAULT_PROJECT, picks: Picks = False, rejects: Rejects = False,
                undecided: Undecided = False, min_rating: MinRating = None, suggested: Suggest = None,
                flag: Flag = None, min_quality: MinQ = None, max_quality: MaxQ = None, camera: Camera = None,
                path_in: PathSub = None, date_from: DateFrom = None, date_to: DateTo = None,
                which: Which = "all",
                out: Annotated[Optional[Path], typer.Option("--out", "-o")] = None):
    """Print (or save) file paths, e.g. `export list --rejects -o rejects.txt` to review/delete later."""
    conn = _conn(project)
    flt = _export_filters(picks, rejects, undecided, min_rating, suggested, flag, min_quality, max_quality,
                          camera, path_in, date_from, date_to)
    ps = export.paths(conn, flt, which=which)
    if out:
        out = _file_dest(out, "list")
        out.write_text("\n".join(ps) + ("\n" if ps else ""), encoding="utf-8")
        console.print(f"Wrote {len(ps)} paths to {out}")
    else:
        for p in ps:
            print(p)


@export_app.command("csv")
def export_csv(out: Path, project: Project = DEFAULT_PROJECT, picks: Picks = False, rejects: Rejects = False,
               undecided: Undecided = False, min_rating: MinRating = None, suggested: Suggest = None,
               flag: Flag = None, min_quality: MinQ = None, max_quality: MaxQ = None, camera: Camera = None,
               path_in: PathSub = None, date_from: DateFrom = None, date_to: DateTo = None):
    """Spreadsheet of photos, scores, flags and your decisions."""
    conn = _conn(project)
    out = _file_dest(out, "csv")
    n = export.to_csv(conn, _flt(picks, rejects, undecided, min_rating, suggested, flag, min_quality,
                                 max_quality, camera, path_in, date_from, date_to), out)
    console.print(f"Wrote {n} rows to {out}")


@export_app.command("xmp")
def export_xmp(project: Project = DEFAULT_PROJECT,
               write_sidecars: Annotated[bool, typer.Option("--write-sidecars", help="Required: confirms writing .xmp files next to your photos.")] = False,
               style: Annotated[str, typer.Option(help="stem (IMG_1.xmp: Lightroom/Capture One) | full (IMG_1.CR3.xmp: darktable)")] = "stem",
               update_existing: Annotated[bool, typer.Option(help="Update sidecars that already exist (never lowers a "
                                                                  "rating another app set; backed up first).")] = False,
               include: Annotated[str, typer.Option(help="What to write: rating,label,keywords,people,regions")] = "rating,label"):
    """Write your stars/picks/rejects (and optionally people, keywords, face regions) as XMP
    sidecars so Lightroom, Capture One, darktable or digiKam see them.

    This is the only command that writes into your photo folders, and it only
    creates small .xmp files; photos themselves are never modified.
    """
    if not write_sidecars:
        console.print("This writes .xmp sidecar files next to your photos. Re-run with [bold]--write-sidecars[/] to confirm.")
        raise typer.Exit(1)
    conn = _conn(project)
    r = export.write_sidecars(conn, Filter(), style=style, update_existing=update_existing,
                              include=tuple(x.strip() for x in include.split(",") if x.strip()), project=project)
    console.print(f"Sidecars: {r.written} written, {r.updated} updated, {r.skipped_existing} existing left untouched"
                  + (f", {r.kept_theirs} kept another app's rating/label" if r.kept_theirs else ""))
    for e in r.errors[:5]:
        console.print(f"[red]{e}[/]")
    if r.backup_dir:
        console.print(f"[dim]Previous versions of changed sidecars: {r.backup_dir}[/]")


@export_app.command("decisions")
def export_decisions(out: Path, project: Project = DEFAULT_PROJECT):
    """Every decision, album, person, event and folder name as portable JSON (re-attachable to a new catalog)."""
    import json as _json

    from . import portable

    out = _file_dest(out, "json")
    data = portable.export_decisions(_conn(project), project)
    out.write_text(_json.dumps(data, indent=1), encoding="utf-8")
    console.print(f"Wrote {len(data['decisions'])} decisions, {len(data['albums'])} albums, "
                  f"{len(data['people'])} named faces to {out}")


# ---------------------------------------------------------------- housekeeping

@roots_app.command("list")
def roots_list(project: Project = DEFAULT_PROJECT):
    conn = _conn(project)
    for r in conn.execute("SELECT id, path FROM roots"):
        n = conn.execute("SELECT COUNT(*) FROM files WHERE root_id=? AND status='present'", (r["id"],)).fetchone()[0]
        console.print(f"{r['id']:>3}  {r['path']}  [dim]{n} files{'' if os.path.isdir(r['path']) else ', unavailable'}[/]")


@roots_app.command("remove")
def roots_remove(root_id: int, project: Project = DEFAULT_PROJECT):
    """Stop tracking a folder (its photos become 'missing'; decisions kept until prune)."""
    scanner.remove_root(_conn(project), root_id)
    console.print("Removed. Run `joeylab organize` to refresh the stacks.")


@app.command()
def prune(project: Project = DEFAULT_PROJECT, yes: Annotated[bool, typer.Option("--yes")] = False):
    """Forget photos that are missing from disk (catalog only; touches no files)."""
    conn = _conn(project)
    ids = [r[0] for r in conn.execute("SELECT id FROM shots WHERE status='missing'")]
    if not ids:
        console.print("Nothing missing.")
        return
    if not yes:
        console.print(f"{len(ids)} missing photos would be forgotten (with their ratings). Re-run with --yes.")
        raise typer.Exit(1)
    with locks.project_lock(project, wait=1800):
        console.print(f"Forgot {scanner.prune_missing(conn, project)} missing photos.")


@app.command("import")
def import_cmd(what: Annotated[str, typer.Argument(help="decisions FILE.json | lightroom CATALOG.lrcat | camera-ratings")],
               path: Annotated[Optional[Path], typer.Argument()] = None, project: Project = DEFAULT_PROJECT,
               overwrite: Annotated[bool, typer.Option(help="Also replace decisions you already made.")] = False):
    """Bring decisions in (one undoable step): a JoeyLab export, a Lightroom Classic catalog (read
    from a copy), or stars set in camera / by other apps."""
    import json as _json

    from . import portable

    conn = _conn(project)
    try:
        if what == "decisions" and path:
            r = portable.import_decisions(conn, _json.loads(path.read_text(encoding="utf-8")), overwrite=overwrite)
        elif what == "lightroom" and path:
            r = portable.import_lightroom(conn, str(path), overwrite=overwrite)
        elif what == "camera-ratings":
            items, _ = portable.ratings_from_files(conn)
            _, changed = library.decide(conn, items, what="ratings from camera and other apps", source="cli")
            r = {"imported": len(changed)}
        else:
            raise ValueError("Use: import decisions FILE.json | import lightroom CATALOG.lrcat | import camera-ratings")
    except (ValueError, OSError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    console.print(" · ".join(f"{k}: {v}" for k, v in r.items() if k != "batch") + "  [dim](`joeylab undo` reverses)[/]")


@app.command()
def health(project: Project = DEFAULT_PROJECT, deep: Annotated[bool, typer.Option(help="Also check backup coverage and sidecars.")] = False):
    """Library health: duplicates, photos on only one drive, unreadable files, undated, and more."""
    from . import health as health_mod

    conn = _conn(project)
    h = health_mod.report(conn, project, deep=deep)
    t = Table(show_header=False, box=None)
    t.add_row("Duplicates", f"{h['duplicates']['extra_files']:,} extra files · {h['duplicates']['bytes'] / 1e9:.1f} GB")
    t.add_row("Couldn't read", f"{h['failed']['n']:,} photos")
    t.add_row("Undated", f"{h['undated']:,}")
    t.add_row("Thumbnails/fragments", f"{h['tiny']:,}")
    t.add_row("Screenshots & documents", f"{h['utility']:,}")
    t.add_row("Wrong extension", f"{h['wrong_extension']['n']:,}")
    t.add_row("To-sort folders", ", ".join(f"{os.path.basename(x['dir'])} ({x['n']})" for x in h["to_sort"][:5]) or "-")
    u = h["untouched"]
    t.add_row("Untouched check", "never run" if not u else
              f"{'[green]OK[/]' if u['ok'] else '[yellow]changes seen[/]'} · {u['checked']:,} files · {u['runs_ok']} of {u['runs']} runs clean")
    for d in h["drives"]:
        m = d["mount"]
        how = ("read-only" if m.get("read_only") else "writable") + (", noatime" if m.get("noatime") else "") if m else "not connected"
        t.add_row("Drive", f"{d['path']}  [dim]{how}[/]")
    if deep:
        c = h["coverage"]
        t.add_row("Backed up", f"{c['backed_up']:,} of {c['files']:,} files have a copy on another drive"
                               f" ({c['checked']:,} checked byte for byte)" if c["known"] else "not indexed yet (update the library)")
        t.add_row("Only one copy", "  ".join(f"{x['drive']}: {x['files']:,} ({x['bytes'] / 1e9:.1f} GB)" for x in c["by_drive"]) or "-")
        t.add_row("Orphan sidecars", f"{len(h['orphan_sidecars'])}")
    console.print(t)


@app.command("backups")
def backups_cmd(project: Project = DEFAULT_PROJECT,
                check: Annotated[bool, typer.Option("--check", help="Read the files that may be backups whole now (read-only) to confirm them.")] = False,
                seconds: Annotated[float, typer.Option(help="With --check: stop after this long.")] = 600.0):
    """Which photos have a byte-identical copy on another drive (a backup), and which are only on one
    drive, per drive and folder, across every library. Copies on the same drive are duplicates
    (`joeylab dupes`), not backups. Read-only."""
    import time

    from . import copies

    conn = _conn(project)
    with locks.project_lock(project, wait=600):
        copies.refresh(conn, project)
    if check:
        left = copies.candidates()
        console.print(f"Checking {_count(left, 'possible backup')} byte for byte (read-only, up to {seconds:.0f} s)…")
        r = copies.confirm(seconds=seconds, rate_mb=None)
        console.print(f"Read {_count(r['read'], 'file')} ({_gb(r['bytes'])}); {r['left']:,} still to check"
                      + (" (on drives that aren't connected, or out of time)" if r["left"] else "") + ".")
    rep = copies.report(conn, project, folders=8)
    if not rep["photos"]:
        console.print("No photos in this library yet.")
        return
    console.print(f"[bold]{_count(rep['backed'], 'photo')}[/] of {rep['photos']:,} have a copy on another drive"
                  f" ({rep['checked']:,} checked byte for byte) · [bold]{rep['only']:,}[/] only on one drive ({_gb(rep['only_bytes'])})")
    for d in rep["drives"]:
        state = "connected" if d["connected"] else "not connected" + (
            f", last seen {time.strftime('%-d %b %Y', time.localtime(d['last_seen']))}" if d["last_seen"] else "")
        console.print(f"\n[bold]{d['name']}[/] [dim]({state})[/]: {d['backed']:,} of {d['photos']:,} backed up · "
                      f"{d['only']:,} only here ({_gb(d['only_bytes'])})")
        for w in d["where"][:4]:
            console.print(f"  backed up on {w['name']}: {w['photos']:,}" + ("" if w["connected"] else " [dim](not connected)[/]"))
        for f in [f for f in d["folders"] if f["only"]][:8]:
            root = next((r for r in d["roots"] if f["path"] == r or f["path"].startswith(r.rstrip("/") + "/")), None)
            where = os.path.relpath(f["path"], os.path.dirname(root)) if root else f["path"]
            console.print(f"  [yellow]{f['only']:,}[/] only here in {where}  [dim]({_gb(f['only_bytes'])})[/]")
    if rep["unchecked_files"]:
        console.print(f"\n[dim]{_count(rep['unchecked_files'], 'file')} probably backed up, not checked byte for byte yet:"
                      f" `joeylab backups -p {project} --check` reads them now (the background service does it slowly).[/]")


@app.command("verify-list")
def verify_list(listfile: Path, project: Project = DEFAULT_PROJECT):
    """Right before deleting a downloaded list of duplicate extras: check each one still has its
    kept copy (identical files are re-hashed). Read-only."""
    from . import health as health_mod

    conn = _conn(project)
    res = health_mod.verify_extras(conn, listfile.read_text(encoding="utf-8").splitlines())
    bad = [r for r in res if not r["ok"]]
    console.print(f"{len(res) - len(bad)} of {len(res)} verified safe to remove.")
    for r in bad[:50]:
        console.print(f"[yellow]keep[/] {r['path']}  [dim]{r['why']}[/]")
    if bad:
        raise typer.Exit(2)


def _file_dest(out: Path, kind: str) -> Path:
    try:
        return export.check_file_dest(out, kind)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)


def _count(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e8 else f"{n / 1e6:.1f} MB" if n >= 1e5 else f"{n / 1e3:.0f} KB"


def _typed_ok(count: int, what: str) -> bool:
    """Ask for the count to be typed back: nothing moves on Enter, a wrong number or no terminal."""
    try:
        answer = typer.prompt(f"Type {count} to move {what} to the Trash (Enter stops)", default="", show_default=False)
    except (typer.Abort, EOFError):
        return False
    return answer.strip().replace(",", "") == str(count)


@app.command("trash-list")
def trash_list(listfile: Path, project: Project = DEFAULT_PROJECT,
               include_last_copies: Annotated[bool, typer.Option(
                   "--include-last-copies", help="Also move files that are the last copy (e.g. a list of rejects).")] = False):
    """Check a list of files (duplicate extras, rejects), show what stays and what doesn't, and
    after you type the count, move them to the Trash. Files whose copy stays go by default; last
    copies only with --include-last-copies. Nothing is deleted: empty the Trash yourself."""
    from . import cleanup

    conn = _conn(project)
    paths = listfile.read_text(encoding="utf-8").splitlines()
    with Progress(TextColumn("Checking files and their copies (read-only)"), BarColumn(), MofNCompleteColumn(),
                  TimeRemainingColumn(), console=console, transient=True) as prog:
        task = prog.add_task("check", total=len(paths))
        items = cleanup.plan(conn, paths, progress=lambda n, total: prog.update(task, completed=n, total=total))
    sm = cleanup.summary(items)
    t = Table(title=f"{_count(len(items), 'file')} in {listfile.name}", show_lines=False)
    t.add_column("Files", justify="right")
    t.add_column("Size", justify="right")
    t.add_column("What stays behind")
    t.add_column("")
    for c in (cleanup.IDENTICAL, cleanup.RESAVED, cleanup.TWIN, cleanup.LAST, cleanup.SKIP):
        n, size = sm[c]
        if not n:
            continue
        goes = c in cleanup.HAS_COPY or (include_last_copies and c in (cleanup.TWIN, cleanup.LAST))
        mark = "[green]goes[/]" if goes else "[yellow]stays[/]" if c != cleanup.SKIP else "[dim]stays[/]"
        style = "red" if c in (cleanup.TWIN, cleanup.LAST) and goes else ""
        t.add_row(f"{n:,}", _gb(size) if size else "", f"[{style}]{cleanup.DESCRIBE[c]}[/]" if style else cleanup.DESCRIBE[c], mark)
    console.print(t)
    skipped = [i for i in items if i.category == cleanup.SKIP]
    if skipped:
        from collections import Counter

        for why, n in Counter(i.why for i in skipped).most_common():
            console.print(f"  [dim]skipped {n:,}: {why}[/]")
    last = [i for i in items if i.category in (cleanup.TWIN, cleanup.LAST)]
    # Backups on other drives, for information only: they never make a file go.
    from . import copies

    try:
        notes = copies.notes_for_paths(conn, project, [i.path for i in items if i.category != cleanup.SKIP])
    except Exception:
        notes = {}
    if notes:
        s = copies.trash_summary(notes, {i.path for i in last})
        console.print(f"Backups: {_count(s['backed_up'], 'file')} with a checked backup on another drive · "
                      f"{s['only_copy']:,} the only copy anywhere JoeyLab knows"
                      + (f" · {s['unchecked']:,} with a possible backup not checked yet" if s["unchecked"] else "")
                      + " [dim](information only: nothing goes because of a backup)[/]")

    def where(i) -> str:
        n = notes.get(i.path) or {}
        if n.get("backup"):
            return f"  [dim]backed up on {n['backup']['drive_name']} › {n['backup']['rel']}[/]"
        return "  [dim]the only copy anywhere[/]" if notes and not n.get("copies") else ""

    if last and not include_last_copies:
        console.print(f"[yellow]{_count(len(last), 'file')} ({_gb(sum(i.size for i in last))}) with no copy that stays will be left alone"
                      " (a copy counts only on the same drive). Add --include-last-copies to move them too.[/]")
    elif last:
        console.print(f"[bold red]{_count(len(last), 'file')} ({_gb(sum(i.size for i in last))}) with no copy anywhere on the same drive will go."
                      " Once you empty the Trash, they're gone from there.[/]")
        for i in last[:5]:
            console.print(f"  [red]{i.path}[/]{where(i)}")
        if len(last) > 5:
            console.print(f"  [red]…and {len(last) - 5:,} more (all listed in the log)[/]")
    go = cleanup.chosen(items, include_last_copies)
    if not go:
        console.print("Nothing to move.")
        return
    total = sum(i.size for i in go)
    if not _typed_ok(len(go), f"{_count(len(go), 'file')} ({_gb(total)})"):
        console.print("Stopped. Nothing was moved.")
        raise typer.Exit(1)
    try:
        log = cleanup.trash_files(items, go, f"{project}-files", conn=conn)
    except cleanup.Busy as e:
        console.print(f"[yellow]{e}[/] Nothing was moved.")
        raise typer.Exit(1)
    moved = [i for i in go if i.result == "moved to the Trash"]
    console.print(f"[green]Moved {_count(len(moved), 'file')} ({_gb(sum(i.size for i in moved))}) to the Trash.[/]")
    for i in [i for i in go if i.result != "moved to the Trash"][:10]:
        console.print(f"  [yellow]{i.result}[/]: {i.path}")
    emptied = cleanup.emptied_by(go)
    if emptied:
        console.print(f"{_count(len(emptied), 'folder')} left empty. See them with: joeylab empty-folders -p {project}")
    console.print(f"Log: {log}")
    console.print("[dim]Until you empty the Trash, Finder's Put Back returns any of them. On an external drive the space comes"
                  " back once the Trash is emptied. JoeyLab notices the files are gone at its next update.[/]")


@app.command("trash-repair")
def trash_repair(project: Project = DEFAULT_PROJECT):
    """After moving files to the Trash with an older JoeyLab: let each copy that stays take over the
    moved file's albums, keywords, favourites, comments and stars (from the Trash logs). Catalog only."""
    from . import cleanup

    conn = _conn(project)
    r = cleanup.repair_from_logs(conn, project)
    console.print(f"Checked {_count(r['files'], 'moved file')}: {r['carried']:,} album places, keywords, favourites, comments and "
                  f"face names carried to the copies that stay; stars or picks raised on {_count(r['decisions'], 'photo')}.")


@app.command("raw-list")
def raw_list(project: Project = DEFAULT_PROJECT,
             trash: Annotated[bool, typer.Option("--trash", help="Move them to the Trash (asks first).")] = False,
             files: Annotated[bool, typer.Option("--files", help="List every RAW that would go, with the JPEG that stays.")] = False,
             include_unkept: Annotated[bool, typer.Option("--include-unkept", help="Also the RAWs no rule keeps, not only those you rejected.")] = False):
    """RAW + JPEG pairs: which RAWs stay and why, which you rejected, then a read-only check of those:
    each RAW, the JPEG that stays (it must be there and read whole) and why any is left alone. With
    --trash, move them and their own sidecars to the Trash after you type the count. The photos stay,
    shown from their JPEGs. Choose per photo or per event in the app (Keep RAW / Reject RAW); a RAW with no
    JPEG beside it is never listed."""
    from . import cleanup, raws

    conn = _conn(project)
    s = raws.summary(conn)
    pairs, rules = s["pairs"], s["rules"]
    console.print(f"{_count(pairs['photos'], 'photo')} with a RAW and a JPEG; their RAWs take {_gb(pairs['bytes'])}.")
    t = Table(title="What becomes of their RAWs", title_justify="left")
    t.add_column("Photos", justify="right")
    t.add_column("Space", justify="right")
    t.add_column("")
    for st in raws.KEPT + raws.GOES:
        x = s["states"][st]
        if x["photos"]:
            word = "[green]stays[/]" if st in raws.KEPT else "[yellow]rejected[/]" if st == "notneeded" else "[dim]undecided[/]"
            t.add_row(f"{x['photos']:,}", _gb(x["bytes"]), f"{word}: {raws.why(st, rules)}")
    if pairs["photos"]:
        console.print(t)
    alone = s["raw_only"]["photos"]
    if alone:
        console.print(f"[dim]{_count(alone, 'RAW')} with no JPEG beside {'it always stays' if alone == 1 else 'them always stay'}:"
                      f" {'it is' if alone == 1 else 'each is'} the photo itself.[/]")
    on = [raws.why(k, rules) for k in ("pick", "stars", "fav", "album", "edited") if rules[k]]
    said = ", ".join(on[:-1]) + f" or {on[-1]}" if len(on) > 1 else on[0] if on else ""
    console.print(f"[dim]Rules: {'the RAW stays when the photo is ' + said if on else 'no rule keeps a RAW'}."
                  " Change them in the app: Tidy › RAW + JPEG.[/]")
    rejected = s["states"]["notneeded"]
    unkept = s["states"]["unkept"]
    if not include_unkept and unkept["photos"]:
        console.print(f"[dim]{_count(unkept['photos'], 'RAW')} ({_gb(unkept['bytes'])}) no rule keeps; they stay unless you reject them"
                      f" in the app, or add --include-unkept.[/]")
    if not (pairs["removable"] if include_unkept else rejected["photos"]):
        console.print("No RAWs to move." if include_unkept else "No rejected RAWs to move. Reject RAWs in the app first (per event, or on a selection).")
        return
    with Progress(TextColumn("Checking each RAW and the JPEG that stays (read-only)"), BarColumn(), MofNCompleteColumn(),
                  TimeRemainingColumn(), console=console, transient=True) as prog:
        task = prog.add_task("check", total=pairs["removable_raws"] if include_unkept else rejected["photos"])
        items = raws.plan(conn, progress=lambda n, total: prog.update(task, completed=n, total=total), include_unkept=include_unkept)
    go = raws.going(items)
    t = Table(title=f"{_count(len(go), 'RAW')} would go", title_justify="left")
    t.add_column("Folder", overflow="fold")
    t.add_column("RAWs", justify="right")
    t.add_column("Space", justify="right")
    t.add_column("Left alone", justify="right")
    folders = raws.by_folder(items)
    roots = [r[0] for r in conn.execute("SELECT path FROM roots")]
    for f in folders[:30]:
        root = next((r for r in roots if f["path"].startswith(r.rstrip("/") + "/")), None)
        t.add_row(os.path.relpath(f["path"], root) if root else f["path"], f"{f['raws']:,}", _gb(f["bytes"]) if f["bytes"] else "",
                  f"{f['skipped']:,}" if f["skipped"] else "")
    if go:
        console.print(t)
    if len(folders) > 30:
        console.print(f"  [dim]…and {_count(len(folders) - 30, 'more folder')}[/]")
    for why_not, n in raws.skipped(items):
        console.print(f"  [dim]left alone {n:,}: {why_not}[/]")
    from . import copies

    try:
        notes = copies.notes_for_paths(conn, project, [i.path for i in go])
    except Exception:
        notes = {}
    if notes and go:
        s = copies.trash_summary(notes, {i.path for i in go})
        console.print(f"Backups: {_count(s['backed_up'], 'RAW')} with a checked backup on another drive · {s['only_copy']:,} the only"
                      " copy anywhere JoeyLab knows [dim](information only: whether a RAW goes is your choice)[/]")
    if files:
        for i in go:
            b = (notes.get(i.path) or {}).get("backup")
            console.print(f"{i.path}  [dim]→ stays: {os.path.basename(i.stays)}"
                          + (f"; with {', '.join(os.path.basename(x.path) for x in i.sidecars)}" if i.sidecars else "")
                          + (f"; backed up on {b['drive_name']}" if b else "; the only copy anywhere" if notes and not notes[i.path]["copies"] else "")
                          + "[/]")
    if not go:
        console.print("Nothing to move.")
        return
    total = sum(i.size for i in go) + sum(x.size for i in go for x in i.sidecars)
    sides = sum(len(i.sidecars) for i in go)
    if sides:
        console.print(f"{_count(sides, 'sidecar')} (the RAWs' own .xmp files) would go with them; a JPEG's own sidecar stays.")
    if not trash:
        console.print(f"Nothing was changed. To move them to the Trash: joeylab raw-list -p {project} --trash")
        return
    if not _typed_ok(len(go), f"{_count(len(go), 'RAW')} ({_gb(total)})"):
        console.print("Stopped. Nothing was moved.")
        raise typer.Exit(1)
    try:
        log = raws.trash(conn, items, f"{project}-files")
    except cleanup.Busy as e:
        console.print(f"[yellow]{e}[/] Nothing was moved.")
        raise typer.Exit(1)
    done, size = raws.moved(items)
    console.print(f"[green]Moved {_count(len(done), 'RAW')} ({_gb(size)}) to the Trash. Their photos stay, shown from their JPEGs.[/]")
    for i in [i for i in go if i.result != "moved to the Trash"][:10]:
        console.print(f"  [yellow]{i.result}[/]: {i.path}")
    console.print(f"Log: {log}")
    console.print("[dim]Until you empty the Trash, Put Back (in the app's History, or Finder's) returns any of them. On an external"
                  " drive the space comes back once the Trash is emptied.[/]")


@app.command("empty-folders")
def empty_folders_cmd(project: Project = DEFAULT_PROJECT,
                      trash: Annotated[bool, typer.Option("--trash", help="Move them to the Trash (asks first).")] = False):
    """List folders in this library's folders that hold nothing (or only .DS_Store-type leftovers).
    With --trash, move them to the Trash after you type the count."""
    from . import cleanup

    conn = _conn(project)
    roots, held = cleanup.folder_roots(conn)
    for r in held:
        console.print(f"[yellow]{r} is on a different drive with the same name, so it wasn't looked at.[/]")
    for r in roots:
        if not os.path.isdir(r):
            console.print(f"[dim]{r} isn't available (drive not connected?), so it wasn't looked at.[/]")
    with console.status("Looking for empty folders (read-only)…"):
        found = cleanup.empty_folders(roots)
    if not found:
        console.print("No empty folders.")
        return
    for r in roots:
        mine = [f for f in found if f.path.startswith(r.rstrip("/") + "/")]
        if not mine:
            continue
        t = Table(title=f"{_count(len(mine), 'empty folder')} in {r}", title_justify="left")
        t.add_column("Folder", overflow="fold")
        t.add_column("Leftover files", justify="right")
        t.add_column("Empty folders inside", justify="right")
        for f in mine:
            t.add_row(os.path.relpath(f.path, r), str(len(f.clutter)) if f.clutter else "", str(f.folders) if f.folders else "")
        console.print(t)
    console.print(f"{_count(len(found), 'empty folder')} in all.")
    if not trash:
        console.print(f"Nothing was changed. To move them to the Trash: joeylab empty-folders -p {project} --trash")
        return
    if not _typed_ok(len(found), f"these {_count(len(found), 'folder')}"):
        console.print("Stopped. Nothing was moved.")
        raise typer.Exit(1)
    try:
        log = cleanup.trash_folders(found, roots, f"{project}-folders")
    except cleanup.Busy as e:
        console.print(f"[yellow]{e}[/] Nothing was moved.")
        raise typer.Exit(1)
    moved = [f for f in found if f.result == "moved to the Trash"]
    console.print(f"[green]Moved {_count(len(moved), 'folder')} to the Trash.[/]")
    for f in [f for f in found if f.result != "moved to the Trash"][:10]:
        console.print(f"  [yellow]{f.result}[/]: {f.path}")
    console.print(f"Log: {log}")


@app.command()
def restore(project: Project = DEFAULT_PROJECT,
            backup_file: Annotated[Optional[Path], typer.Option("--from", help="A backup file (default: the latest daily one).")] = None,
            yes: Annotated[bool, typer.Option("--yes")] = False):
    """Bring back a catalog backup. The current catalog is kept beside the backups first. Stop the
    background service while restoring (`joeylab service stop`)."""
    from . import backup as backup_mod, locks, service

    if service.running():
        console.print("[red]The background service is running. Stop it first: joeylab service stop[/]")
        raise typer.Exit(1)
    for b in backup_mod.list_backups(project)[:8]:
        console.print(f"  {b['name']}  [dim]{b['size'] / 1e6:.1f} MB[/]")
    if not yes:
        console.print("Re-run with --yes to restore " + (str(backup_file) if backup_file else "the latest daily backup") + ".")
        raise typer.Exit(1)
    try:
        with locks.project_lock(project):
            src, kept = backup_mod.restore(project, backup_file)
    except (FileNotFoundError, db.CatalogDamaged, locks.Busy) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    console.print(f"Restored from {src}." + (f" The previous catalog is kept as {kept}." if kept else ""))


@app.command()
def events(project: Project = DEFAULT_PROJECT, trips: Annotated[bool, typer.Option("--trips")] = False):
    """Events and trips JoeyLab found (name them in the web app)."""
    from . import events as events_mod

    conn = _conn(project)
    for e in events_mod.listing(conn, "trip" if trips else "event")[:200]:
        console.print(f"{e['id']:>5}  {e['name']}  [dim]{e['n']} photos[/]")


@app.command()
def keywords(project: Project = DEFAULT_PROJECT):
    """Your keyword tree with photo counts (tag photos with `joeylab tag`, or T in the web app)."""
    from . import keywords as kw_mod

    conn = _conn(project)
    tree = kw_mod.tree(conn)
    if not tree:
        console.print("No keywords yet. Add some with: joeylab tag ID... --add \"Activities > Climbing\"")
    for k in tree:
        console.print(f"{'  ' * k['depth']}{k['name']}  [dim]{k['total']}[/]")


@app.command()
def tag(ids: Annotated[list[int], typer.Argument(help="Photo ids")],
        add: Annotated[Optional[list[str]], typer.Option("--add", help='A keyword or path, e.g. "Activities > Climbing" (repeatable)')] = None,
        remove: Annotated[Optional[list[str]], typer.Option("--remove", help="A keyword to take off (repeatable)")] = None,
        project: Project = DEFAULT_PROJECT):
    """Add or remove keywords on photos (catalog only; undo with `joeylab undo`)."""
    from . import keywords as kw_mod

    conn = _conn(project)
    add_ids = [kw_mod.ensure(conn, k) for k in add or [] if kw_mod.split_path(k)]
    rm_ids = [k for k in (kw_mod.find(conn, x) for x in remove or []) if k]
    names = [kw_mod.path_of(conn, k)[-1] for k in add_ids] + [f"-{kw_mod.path_of(conn, k)[-1]}" for k in rm_ids]
    _, changed = kw_mod.assign(conn, ids, add=add_ids, remove=rm_ids, what=f"keywords: {', '.join(names)}"[:60], source="cli")
    console.print(f"Keywords changed on {len(changed)} photos. [dim]`joeylab undo` reverses it.[/]")


@app.command()
def places(project: Project = DEFAULT_PROJECT):
    """Where your photos were taken (offline reverse geocoding)."""
    from . import geo

    conn = _conn(project)
    for p in geo.summary(conn)[:100]:
        console.print(f"{p['n']:>6}  {p['name']}, {p['country']}  [dim]{p['gps']} with GPS[/]")


@app.command()
def mcp(project: Annotated[Optional[str], typer.Option("--library", "--project", "-p", help="The library tools use when none is named.")] = None,
        allow_changes: Annotated[bool, typer.Option("--allow-changes/--read-only",
                                                    help="Let the assistant set picks, stars and albums (logged, undoable).")] = True):
    """Run JoeyLab as an MCP server over stdio, so an AI assistant (Claude Code, Codex, Claude
    Desktop) can search and organise your library. Add it with:
    claude mcp add joeylab -- /path/to/.venv/bin/joeylab mcp
    codex mcp add joeylab -- /path/to/.venv/bin/joeylab mcp"""
    from . import mcp as mcp_mod

    mcp_mod.serve(project, allow_changes=allow_changes)


def _is_loopback(host: str) -> bool:
    import ipaddress

    if host.strip("[]").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _listen(hosts: list[str], port: int) -> list:
    import socket

    sockets = []
    for h in hosts:
        sock = socket.socket(socket.AF_INET6 if ":" in h else socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((h.strip("[]"), port))
        except OSError as e:
            console.print(f"[red]Can't listen on {h}:{port}: {e}. Is JoeyLab already running?[/]")
            raise typer.Exit(1)
        sock.listen(128)
        sockets.append(sock)
    return sockets


@app.command()
def serve(port: Annotated[int, typer.Option(help="Port (on this Mac, and on your tailnet with --tailscale).")] = 8765,
          tailscale: Annotated[bool, typer.Option("--tailscale/--no-tailscale",
              help="Also open it on your tailnet with `tailscale serve` (HTTPS when your tailnet allows it). "
                   "Off by default: only this Mac can connect.")] = False,
          host: Annotated[Optional[list[str]], typer.Option(
              help="Also listen on these addresses directly (repeatable), e.g. 0.0.0.0. Needs --token. "
                   "Usually not needed.")] = None,
          allow: Annotated[Optional[list[str]], typer.Option(
              help="Extra networks allowed to connect directly, e.g. 192.168.1.0/24.")] = None,
          token: Annotated[Optional[str], typer.Option(envvar="JOEYLAB_TOKEN",
              help="Require this access token (the printed link sets a cookie).")] = None,
          allow_host: Annotated[Optional[list[str]], typer.Option(
              help="Extra host names this server answers to (besides localhost and your tailnet name).")] = None,
          watch: Annotated[float, typer.Option(help="Rescan every N minutes and update the library (0 = off).")] = 15,
          faces: Annotated[bool, typer.Option(help="Face / closed-eye checks in background jobs.")] = True,
          ai_layer: Annotated[bool, typer.Option("--ai/--no-ai", help="AI embeddings in background jobs.")] = True):
    """[bold]Web GUI[/] on this Mac, and with --tailscale on your other devices too."""
    import socket

    import uvicorn

    from .web import net
    from .web.jobs import JobManager
    from .web.server import create_app

    config.secure_home()
    extra = [h for h in (host or []) if h != "127.0.0.1"]
    direct = [h for h in extra if not _is_loopback(h)]
    if direct and not token:
        console.print(f"[red]Listening on {', '.join(direct)} lets other devices connect straight to JoeyLab, so it "
                      "needs an access token: add --token with a secret of your choice (or set JOEYLAB_TOKEN). "
                      "To open it on your own devices, --tailscale is simpler.[/]")
        raise typer.Exit(2)
    hosts = ["127.0.0.1", *extra]
    sockets = _listen(hosts, port)

    ts_url, ts_mode = None, None
    if tailscale:
        with console.status("Publishing to your tailnet…"):
            ts_url, ts_mode, err = net.tailscale_serve(port)
        if err:
            console.print(f"[yellow]Couldn't publish with Tailscale ({err}). Only this Mac can connect.[/]")
    owner, ts_name = net.tailscale_owner(), net.tailscale_name()
    from . import identity

    identity.fold_into_owner(owner)       # favourites from your own phone join the one set

    suffix = f"/?token={token}" if token else "/"
    console.print("[bold]JoeyLab[/] is running. Open:")
    console.print(f"  http://localhost:{port}{suffix}   [dim](this Mac)[/]")
    if ts_url:
        console.print(f"  {ts_url}{suffix}   [dim](devices on your tailnet)[/]")
    for h in extra:
        console.print(f"  http://{h}:{port}{suffix}   [dim](direct{', with the token' if token else ''})[/]")
    if direct:
        console.print("[dim]Only you can open it: at this Mac, or with the link above (it carries the token).[/]")
    elif ts_url:
        who = f"you: at this Mac, or on devices signed in to Tailscale as {owner}" if owner else "you, at this Mac"
        console.print(f"[dim]Only {who}. Anyone else on your tailnet sees only share links you send them.[/]")
    else:
        console.print("[dim]Only this Mac can connect. To open it on your phone and other devices: "
                      "joeylab serve --tailscale[/]")
    if watch:
        console.print(f"[dim]Checking folders for changes every {watch:g} min. Ctrl+C to stop.[/]")

    jobs = JobManager(watch_minutes=watch, faces=faces, ai_layer=ai_layer)
    networks = net.DEFAULT_ALLOWED + net.parse_networks(allow or [])

    def cleanup():
        jobs.stop()
        if ts_mode:
            net.tailscale_unserve(port, ts_mode)

    hosts_ok = list(allow_host or []) + [h for h in hosts if h not in ("0.0.0.0", "::")]
    if ts_name:                                     # this Mac's own tailnet name, never any *.ts.net
        hosts_ok += [ts_name, ts_name.split(".")[0]]
    if any(h in ("0.0.0.0", "::") for h in hosts):
        hosts_ok += [socket.gethostname().lower(), socket.gethostname().lower().split(".")[0] + ".local"]
    app_ = create_app(jobs=jobs, token=token, allow=networks, allow_hosts=hosts_ok,
                      meta={"tailnet_url": ts_url, "port": port}, on_shutdown=[cleanup], owner=owner,
                      trust_identity=not direct)
    jobs.start_watch()
    console.print("[dim]Watching folders for changes as they happen; catalogs are backed up daily.[/]")
    if ai_layer:
        import threading

        from . import ai

        if ai.available():  # load CLIP in the background so the first search is quick
            threading.Thread(target=ai.Models.get, name="ai-warmup", daemon=True).start()
        from . import search as _search

        if _search.available():  # and the picture-search model, which a first search used to wait for
            threading.Thread(target=_search.warm_up, name="search-warmup", daemon=True).start()
    # proxy_headers off: the app must see who really connected (this Mac, for `tailscale serve`), not the
    # address a proxy says it forwarded for; that's how it tells a forwarded request from one at the Mac.
    uvicorn.Server(uvicorn.Config(app_, log_level="warning", timeout_graceful_shutdown=2,
                                  proxy_headers=False)).run(sockets=sockets)


def _service_line(st: dict) -> str:
    where = "this Mac and your tailnet (tailscale serve)" if st["tailscale"] else "this Mac only"
    every = f" · checks folders every {st['watch']:g} min" if st["watch"] else ""
    return f"port {st['port']} · {where}{every}"


@service_app.command("install")
def service_install(port: Annotated[int, typer.Option()] = 8765,
                    watch: Annotated[float, typer.Option(help="Backstop rescan interval in minutes.")] = 15,
                    tailscale: Annotated[bool, typer.Option("--tailscale/--no-tailscale",
                        help="Also open it on your tailnet with `tailscale serve`, for your phone and other devices.")] = False):
    """Start JoeyLab now and at every login; restart it if it ever stops. Installing again
    replaces the options: give every one you want each time."""
    from . import service

    args = service.serve_args(watch, tailscale)
    try:
        service.install(port, args)
    except RuntimeError as e:
        console.print(f"[red]Couldn't install the service: {e}[/]")
        raise typer.Exit(1)
    st = service.describe(service.plist(port, args))
    console.print(f"[green]Installed.[/] JoeyLab runs in the background and starts at login.\n"
                  f"  Runs on: {_service_line(st)}\n"
                  f"  Logs: {service.LOG_DIR}\n  Stop and remove: joeylab service uninstall")
    if not tailscale:
        console.print("[dim]Only this Mac can open it. To use it on your phone too: joeylab service install --tailscale[/]")


@service_app.command("uninstall")
def service_uninstall():
    """Stop the background app and don't start it at login."""
    from . import service

    service.uninstall()
    console.print("Removed. JoeyLab no longer runs in the background.")


@service_app.command("restart")
def service_restart():
    """Restart the background app (for example after updating JoeyLab)."""
    from . import service

    try:
        service.restart()
    except RuntimeError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    console.print("Restarted.")


@service_app.command("stop")
def service_stop():
    """Stop the background app until `joeylab service start` (or the next login)."""
    from . import service

    service.stop()
    console.print("Stopped.")


@service_app.command("start")
def service_start():
    from . import service

    try:
        service.start()
    except RuntimeError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    console.print("Started.")


@service_app.command("status")
def service_status():
    from . import service

    if not service.installed():
        console.print("Not installed. Run: joeylab service install")
        return
    console.print("[green]Running[/]" if service.running() else "[yellow]Installed but not running[/] (see joeylab service logs)")
    st = service.settings()
    if st:
        console.print(f"  {_service_line(st)}")


@service_app.command("logs")
def service_logs(lines: Annotated[int, typer.Option("-n")] = 40):
    from . import service

    print(service.log_tail(lines))


@app.command("share-server")
def share_server(port: Annotated[int, typer.Option(help="Port on this Mac (it listens on 127.0.0.1 only).")] = 8766):
    """[bold]Share links only[/], to publish them beyond your tailnet: the pages your share links open
    (/s/…) and their photos, and nothing else of your library. Point a tunnel or reverse proxy at this
    port; the app itself stays private."""
    import uvicorn

    from .web.server import create_share_app

    config.secure_home()
    sockets = _listen(["127.0.0.1"], port)
    console.print(f"[bold]Share links[/] are served at http://127.0.0.1:{port}/s/<link>. Ctrl+C to stop.\n"
                  "[dim]Publish this port with a tunnel or reverse proxy, then swap the start of a share link for "
                  "its address. Only share pages, their photos and favourites are on it: no library, no settings, "
                  "no folders.[/]")
    uvicorn.Server(uvicorn.Config(create_share_app(), log_level="warning", timeout_graceful_shutdown=2,
                                  proxy_headers=False)).run(sockets=sockets)


@app.command("backup")
def backup_cmd(project: Annotated[Optional[str], typer.Option("--library", "--project", "-p", help="Only this library.")] = None):
    """Back up catalogs now (the server also does this daily; kept: 14 days, then weekly for 8 weeks, monthly for a year)."""
    from . import backup

    paths = [backup.backup(project, force=True)] if project else backup.backup_all(force=True)
    for pth in paths:
        console.print(f"Backed up to {pth}")


@library_app.command("list")
def library_list():
    """Every library, with where JoeyLab keeps its catalog."""
    for p in config.list_projects():
        console.print(f"{p}  [dim]{config.project_dir(p)}[/]")


@app.command(hidden=True)
def projects():
    """List libraries (the old name of `joeylab library list`)."""
    library_list()


@library_app.command("rename")
def library_rename(old: str, new: Annotated[str, typer.Argument(help="The new name; spaces and capitals become a short name, e.g. smith-wedding.")]):
    """Rename a library. Only JoeyLab's own records change: its catalog, backups and logs."""
    from . import libraries

    try:
        name = libraries.rename(old, new)
    except (FileNotFoundError, ValueError, OSError, locks.Busy) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    console.print(f"Renamed {old!r} to {name!r}. Its photo folders weren't touched." if name != old
                  else f"{old!r} is already called that.")


@library_app.command("remove")
def library_remove(name: str, yes: Annotated[bool, typer.Option("--yes", help="Don't ask; remove it.")] = False):
    """Remove a library from JoeyLab. Its catalog is backed up first; its photo folders aren't touched."""
    from . import libraries

    if not yes:
        typed = typer.prompt(f"This removes the library {name!r} from JoeyLab (a backup of its catalog is kept, and "
                             "no photo is touched). Type its name to go ahead", default="", show_default=False)
        if typed.strip() != name:
            console.print("Nothing was removed.")
            raise typer.Exit(1)
    try:
        out = libraries.remove(name, name)
    except (FileNotFoundError, ValueError, OSError, locks.Busy) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    console.print(f"Removed {name!r}. Its photo folders weren't touched. A backup of its catalog is in {out['kept']}.\n"
                  f"To bring it back: copy {out['catalog']} to {config.project_dir(name) / 'catalog.db'}, then run "
                  f"[bold]joeylab analyze -p {name} --force[/] to remake its thumbnails.")


def _regroup_after_move(names: list[str]) -> None:
    """Stacks, duplicates, events, scores and search worked out again in each library, as after an update."""
    from . import split

    for name in names:
        if name not in config.list_projects():
            continue
        conn = _conn(name)
        t0 = time.time()
        with locks.project_lock(name, wait=1800), RichReporter() as rep:
            rep.stage(f"Regrouping {name}")
            took = split.recompute(conn, name, rep)
        console.print(f"[green]Regrouped {name}[/] in {time.time() - t0:.1f} s "
                      f"[dim](stacks, duplicates, events and scores {took['organise']:.1f} s, faces {took['faces']:.1f} s, "
                      f"search {took['search']:.1f} s)[/]")


def _say_moved(op: dict) -> None:
    c = op["counts"]
    t = op.get("times", {})
    console.print(f"[green]Moved {c['photos']:,} photos[/] ({c['files']:,} files, {c['faces']:,} faces, "
                  f"{c['history']:,} history entries) from {op['src']} to {op['dst']} in {op['seconds']:.1f} s "
                  f"[dim](" + ", ".join(f"{k} {v:.1f} s" for k, v in t.items()) + ")[/]")
    console.print("Catalog backups from just before: " + ", ".join(op.get("backups", {}).values()))
    if op.get("removed_library"):
        console.print(f"“{op['removed_library']['name']}” was empty again, so it was removed "
                      f"(its catalog is kept in {op['removed_library']['kept']}).")
    elif not op.get("back_of"):
        console.print(f"To move them back: [bold]joeylab library move-back {op['id']}[/]")


@library_app.command("split")
def library_split(
        library: Annotated[str, typer.Argument(help="The library the folders are in now.")],
        more: Annotated[Optional[list[str]], typer.Argument(help="More folders (after --folders).", show_default=False)] = None,
        folders: Annotated[Optional[list[str]], typer.Option("--folders", "--folder", "-f",
                           help="A folder to move: its full path, or its path inside one of the library's folders.")] = None,
        to: Annotated[str, typer.Option("--to", help="The library they go to: an existing one, or a name for a new one.")] = "",
        purpose: Annotated[Optional[str], typer.Option("--purpose", help="For a new library: shoot (to cull) or archive (to tidy).")] = None,
        yes: Annotated[bool, typer.Option("--yes", help="Don't ask; go ahead once the plan is shown.")] = False,
        regroup: Annotated[bool, typer.Option("--regroup/--no-regroup", help="Regroup both libraries afterwards.")] = True,
        finish: Annotated[Optional[str], typer.Option("--finish", help="Finish a move that stopped half way (its id).")] = None):
    """[bold]Move folders to another library[/], with their decisions, names, faces and history (nothing is
    read again; catalog only). Both catalogs are backed up first, and it can be moved back."""
    from . import split

    if finish:
        try:
            op = split.finish(finish)
        except (split.Refused, locks.Busy) as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(1)
        console.print(f"Finished: the photos are in {op['dst']} only now." if op["state"] == "done"
                      else f"That move had changed nothing; {op['src']} is as it was.")
        return
    chosen = list(folders or []) + list(more or [])
    try:
        p = split.plan(library, chosen, to, purpose)
    except split.Refused as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    console.print(p["what"])
    for f in p["folders"]:
        console.print(f"  [dim]{f}[/]")
    if p["new"] and not p["purpose"]:
        console.print("[dim]Its purpose isn't set: the app asks the first time it's opened (or give --purpose).[/]")
    if not yes and not typer.confirm("Go ahead?", default=False):
        console.print("Nothing was moved.")
        raise typer.Exit(1)
    try:
        op = split.run(library, chosen, to, purpose, lock_wait=1800,
                       on_wait=lambda: console.print("[dim]Waiting for an update of one of the libraries to finish…[/]"),
                       progress=lambda m: console.print(f"[dim]{m}…[/]"))
    except (split.Refused, locks.Busy) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    _say_moved(op)
    if regroup:
        _regroup_after_move([op["src"], op["dst"]])


@library_app.command("move-back")
def library_move_back(move: Annotated[str, typer.Argument(help="The move's id (shown when it was made; joeylab library moves lists them).")],
                      regroup: Annotated[bool, typer.Option("--regroup/--no-regroup", help="Regroup both libraries afterwards.")] = True):
    """Move folders moved to another library back where they came from (the same move in reverse)."""
    from . import split

    try:
        op = split.move_back(move, lock_wait=1800, progress=lambda m: console.print(f"[dim]{m}…[/]"))
    except (split.Refused, locks.Busy) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    _say_moved(op)
    if regroup:
        _regroup_after_move([op["src"], op["dst"]])


@library_app.command("moves")
def library_moves(library: Annotated[Optional[str], typer.Argument(help="Only this library's.")] = None):
    """Folders moved between libraries, newest first, with their ids."""
    from datetime import datetime

    from . import split

    ops = split.history(library)
    if not ops:
        console.print("No folders have been moved between libraries.")
    for op in ops:
        when = datetime.fromtimestamp(op["ts"]).strftime("%d %b %Y %H:%M")
        n = op.get("counts", {}).get("photos")
        state = {"done": "moved back" if op.get("moved_back") else "", "failed": "stopped; nothing changed",
                 "copied": "stopped half way: finish it with --finish", "removed": "stopped half way: finish it with --finish",
                 "copying": "stopped; nothing changed"}.get(op["state"], op["state"])
        console.print(f"[bold]{op['id']}[/]  {when}  {op['src']} → {op['dst']}"
                      + (f"  {n:,} photos" if n is not None else "") + (f"  [dim]{state}[/]" if state else ""))
        for f in op["folders"]:
            console.print(f"    [dim]{f}[/]")


@app.command()
def demo(folder: Annotated[Optional[Path], typer.Argument(help="Where to put the demo photos (default: ~/JoeyLab Demo).")] = None,
         faces: Annotated[bool, typer.Option(help="Detect faces / closed eyes.")] = True,
         ai_layer: Annotated[bool, typer.Option("--ai/--no-ai", help="CLIP embeddings + aesthetic score (if installed).")] = True,
         seed: Annotated[int, typer.Option(help="Another number gives other pictures.")] = 0):
    """Make a demo library ("demo") from generated photo-like pictures, to try JoeyLab without your own photos."""
    from . import demo as demo_mod

    dest = Path(os.path.realpath(Path(folder or demo_mod.DEFAULT_DEST).expanduser()))
    why = demo_mod.check_dest(dest)
    if why:
        console.print(f"[red]{why}[/]")
        raise typer.Exit(1)
    if demo_mod.PROJECT in config.list_projects():
        roots = [r[0] for r in _conn(demo_mod.PROJECT).execute("SELECT path FROM roots")]
        if any(r != str(dest) for r in roots):
            console.print(f"[red]A library called {demo_mod.PROJECT!r} already tracks other folders ({', '.join(roots)}).[/]")
            raise typer.Exit(1)
    if (dest / demo_mod.MARKER).exists():
        console.print(f"Using the demo photos already in {dest}")
    else:
        with Progress(TextColumn("Making demo photos"), BarColumn(), MofNCompleteColumn(), console=console, transient=True) as prog:
            task = prog.add_task("demo")
            made = demo_mod.make(dest, seed=seed, progress=lambda n, total: prog.update(task, completed=n, total=total))
        console.print(f"Made {made['photos']} demo photos in {dest}: {made['outings']} outings from {made['cameras']} cameras, "
                      f"with bursts, {made['exact_duplicates']} exact duplicates, a re-saved copy and {made['undated']} undated scans.")
    conn = _conn(demo_mod.PROJECT, create=True)
    _pipeline(conn, demo_mod.PROJECT, [dest], faces=faces, ai_layer=ai_layer)
    console.print(f"\nNext: [bold]joeylab serve[/] and open the library {demo_mod.PROJECT!r}, or "
                  f"[bold]joeylab cull -p {demo_mod.PROJECT}[/]. To remove it, delete {dest} and "
                  f"{config.project_dir(demo_mod.PROJECT)}.")


@service_app.callback()
def _service_mac_only() -> None:
    from . import service

    if sys.platform != "darwin":
        console.print(f"[red]{service.MAC_ONLY}[/]")
        raise typer.Exit(1)


# ---------------------------------------------------------------- face recognition

faces_app = typer.Typer(no_args_is_help=True, help="Face recognition for People: off until you turn it on and choose its models.")
app.add_typer(faces_app, name="faces")

FaceModels = Annotated[Optional[str], typer.Option(
    "--model", help="The face models: insightface (most accurate; personal, non-commercial use only) or opencv "
                    "(free for any use, including paid work).")]


def _faces_choices() -> None:
    """The sets of face models, as the app shows them: what each is good at, its licence and its download."""
    from . import faces

    for i, m in enumerate(faces.MODELS.values(), 1):
        console.print(f"\n[bold]{i}. {m.label}[/]  (--model {m.key})", soft_wrap=True)
        console.print(f"   {m.good}", soft_wrap=True)
        console.print(f"   Licence: {m.licence} {' · '.join(url for _, url in m.links)}", soft_wrap=True)
        kept = f", {m.kept_mb} MB kept" if m.kept_mb != m.download_mb else ""
        console.print(f"   A one-time download of about {m.download_mb} MB{kept}: {m.credit}.", soft_wrap=True)


@faces_app.command("status")
def faces_status():
    """Whether face recognition is on, and which models it uses, with their licence."""
    from . import faces

    key = faces.current()
    console.print(f"Face recognition is [bold]{'on' if faces.enabled() else 'off'}[/] for every library on this Mac.")
    if key is None:
        console.print("Its models aren't chosen yet: [bold]joeylab faces enable[/] shows the choice.")
        return
    m = faces.MODELS[key]
    console.print(f"Models: {m.credit}. [bold]{m.label}[/].", soft_wrap=True)
    if not faces.available(key):
        console.print("[yellow]They need the faces extra: uv sync --extra faces[/]")
    elif faces.enabled():
        console.print("They're downloaded." if faces.downloaded(key) else "They're downloaded during the next update.")
    console.print(f"[dim]Licence: {m.licence} {' · '.join(url for _, url in m.links)}[/]", soft_wrap=True)


@faces_app.command("enable")
def faces_enable(model: FaceModels = None,
                 yes: Annotated[bool, typer.Option("--yes", help="Agree to the models' licence (and a switch) without asking.")] = False):
    """Turn on face recognition (finding and naming people): choose its models, read their licence and
    agree. With other models than before, it switches: every face is found again, and names are kept."""
    from . import faces

    if model is not None and model not in faces.MODELS:
        console.print(f"[red]--model is one of: {', '.join(faces.MODELS)}.[/]")
        raise typer.Exit(2)
    was = faces.current()
    if model is None and faces.enabled() and was:
        console.print(f"Face recognition is already on, with the models {faces.MODELS[was].label}. To switch, add "
                      f"--model {next(k for k in faces.MODELS if k != was)}.")
        return
    if model is None and yes:
        if not was:
            console.print("[red]Choose the face models with --model:[/]")
            _faces_choices()
            raise typer.Exit(2)
        model = was
    if model is None:
        console.print("Face recognition finds and names the people in your photos, on this Mac only. Choose its models "
                      "(you can switch later):")
        _faces_choices()
        import click

        keys = list(faces.MODELS)
        pick = typer.prompt(f"\nWhich models? ({' or '.join(str(i) for i in range(1, len(keys) + 1))})",
                            type=click.Choice([str(i) for i in range(1, len(keys) + 1)]), show_choices=False)
        model = keys[int(pick) - 1]
    m = faces.MODELS[model]
    if faces.enabled() and was == model:
        console.print(f"Face recognition is already on, with the models {m.label}.")
        return
    switching = was is not None and was != model
    console.print(f"\n{m.credit}: [bold]{m.label}[/]", soft_wrap=True)
    console.print(f"Licence: {m.licence} {' · '.join(url for _, url in m.links)}", soft_wrap=True)
    if switching:
        console.print(f"\nSwitching from {faces.MODELS[was].label}. {faces.SWITCH_NOTE}")
    question = "Agree to this licence and switch to these models?" if switching else \
        "Agree to this licence and turn on face recognition?"
    if not yes and not typer.confirm(question, default=False):
        console.print("Nothing changed." if switching or faces.enabled() else "Face recognition stays off.")
        raise typer.Exit(1)
    faces.set_enabled(True, via="cli", model=model)
    console.print("Switched. Every library's faces are found again during its next update ([bold]joeylab run[/])."
                  if switching else "Face recognition is on. Faces are found during the next update ([bold]joeylab run[/]).")
    if not faces.available(model):
        console.print("[yellow]These models need the faces extra: uv sync --extra faces[/]")


@faces_app.command("disable")
def faces_disable(remove_models: Annotated[bool, typer.Option("--remove-models", help="Also delete the downloaded models.")] = False):
    """Turn off face recognition for every library. Faces and names already found are kept."""
    from . import faces

    faces.set_enabled(False, via="cli")
    if remove_models:
        for key in faces.MODELS:
            for p in faces.files(key):
                p.unlink(missing_ok=True)
    console.print("Face recognition is off. Faces and names already found are kept; to remove them from a "
                  "library, use Settings › People & privacy › Forget all face data.")


def main() -> None:
    app()
