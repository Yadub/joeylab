"""Interactive culling in the terminal: pages of grouped frames, one keystroke per decision."""

from __future__ import annotations

import os
import select
import shutil
import sys
import termios
import tty
from contextlib import contextmanager

from rich.console import Console

from . import library, termimg
from .imaging import load_preview

KEYS_HELP = (
    "←→↑↓/hjkl move · 1-5 stars · 0 clear stars · p pick · x reject · a accept suggestions (page)\n"
    "space zoom · o open in Preview · n/⏎ next page · b prev page · u undo · q quit"
)

console = Console(highlight=False)


@contextmanager
def raw_terminal():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\033[?25l")  # hide cursor
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()


def getkey() -> str:
    ch = os.read(sys.stdin.fileno(), 1).decode(errors="ignore")
    if ch == "\x1b":
        seq = ""
        while select.select([sys.stdin], [], [], 0.03)[0]:
            seq += os.read(sys.stdin.fileno(), 1).decode(errors="ignore")
        return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(seq, "esc")
    if ch in ("\r", "\n"):
        return "enter"
    return ch


def build_pages(rows: list[dict], page_size: int, groups_only: bool) -> list[dict]:
    """Multi-frame groups become their own page(s); consecutive singles are batched."""
    by_group: dict = {}
    order = []
    for r in rows:
        key = r["group_id"] if r["group_id"] is not None else f"s{r['id']}"
        if key not in by_group:
            by_group[key] = []
            order.append(key)
        by_group[key].append(r)
    pages, singles = [], []

    def flush():
        while singles:
            pages.append({"kind": "singles", "shots": singles[:page_size]})
            del singles[:page_size]

    for key in order:
        members = by_group[key]
        is_group = (members[0].get("group_size") or 1) > 1 and len(members) > 1
        if is_group:
            flush()
            for i in range(0, len(members), page_size):
                pages.append({"kind": "group", "shots": members[i : i + page_size],
                              "part": (i // page_size + 1, (len(members) + page_size - 1) // page_size)})
        elif not groups_only:
            singles.extend(members)
            if len(singles) >= page_size:
                flush()
    flush()
    return pages


class Culler:
    def __init__(self, conn, project: str, pages: list[dict], cols: int = 4):
        self.conn, self.project, self.pages, self.cols = conn, project, pages, cols
        self.page = 0
        self.sel = 0
        self.zoom = False
        self.undo: list[list[tuple[int, int | None, int]]] = []  # batches of (shot, rating, pick)
        self.images = termimg.protocol() is not None
        self.total = sum(len(p["shots"]) for p in pages)

    # --- state helpers -------------------------------------------------
    def current(self) -> list[dict]:
        return self.pages[self.page]["shots"]

    def refresh(self) -> None:
        fresh = []
        for s in self.current():
            f = library.shot(self.conn, s["id"])
            fresh.append(f or s)
        self.pages[self.page]["shots"] = fresh

    def decide(self, s: dict, rating: int | None = None, pick: int | None = None) -> None:
        self.undo.append([(s["id"], s.get("rating"), s.get("pick") or 0)])
        library.set_decision(self.conn, s["id"], rating=rating, pick=pick)
        self.refresh()

    def decided_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM decisions d JOIN shots s ON s.id=d.shot_id"
            " WHERE s.status='present' AND (d.pick != 0 OR COALESCE(d.rating,0) > 0)"
        ).fetchone()[0]

    # --- drawing -------------------------------------------------------
    def draw(self) -> None:
        cols_t, rows_t = shutil.get_terminal_size((120, 40))
        sys.stdout.write("\033[2J\033[3J\033[H")
        pg = self.pages[self.page]
        shots = self.current()
        first = shots[0]
        kind = "group" if pg["kind"] == "group" else "singles"
        part = f" (part {pg['part'][0]}/{pg['part'][1]})" if pg.get("part") and pg["part"][1] > 1 else ""
        title = (f"[bold]JoeyLab[/] · {self.project} · page {self.page + 1}/{len(self.pages)} · "
                 f"{'burst/scene of ' + str(first.get('group_size')) if kind == 'group' else str(len(shots)) + ' singles'}{part}"
                 f" · {first.get('capture_str') or ''} · {first.get('camera') or ''} · "
                 f"decided {self.decided_count()}")
        console.print(title)
        s = shots[self.sel]
        if self.zoom:
            if self.images:
                try:
                    img = load_preview(s["preview_path"], s["preview_kind"], s["orientation"], 2048)
                    termimg.show(img, max_rows=rows_t - 6)
                except OSError as e:
                    console.print(f"[red]cannot load preview: {e}[/]")
            else:
                console.print("[dim](no inline images in this terminal; press o to open)[/]")
        else:
            items = [{
                "thumb": x.get("thumb"), "index": i + 1, "rating": x.get("rating"), "pick": x.get("pick"),
                "suggestion": x.get("suggestion"),
                "subtitle": f"q{x['quality']:.0f} " + " ".join(f for f in x["flags"] if f != "best-in-group")
                if x.get("quality") is not None else "not analysed",
            } for i, x in enumerate(shots)]
            if self.images:
                sheet = termimg.contact_sheet(items, selected=self.sel, cols=self.cols)
                termimg.show(sheet, max_rows=rows_t - 6)
            else:
                for it, x in zip(items, shots):
                    mark = ">" if it["index"] - 1 == self.sel else " "
                    console.print(f"{mark} {it['index']:>2} {x['stem']:<20} {it['subtitle']:<40} "
                                  f"{'★' * (x.get('rating') or 0):<5} {x.get('suggestion') or ''}")
        stars = "★" * (s.get("rating") or 0)
        pick = {1: "[green]PICK[/]", -1: "[red]REJECT[/]"}.get(s.get("pick") or 0, "")
        sug = f"suggest: [blue]{s['suggestion']}[/]" if s.get("suggestion") else ""
        flags = ", ".join(s["flags"])
        q = f"q{s['quality']:.0f}" if s.get("quality") is not None else ""
        console.print(f"[bold]#{self.sel + 1}[/] {s['stem']}  {q}  {library.describe(s)}  "
                      f"[yellow]{stars}[/] {pick}  {sug}  [dim]{flags}[/]")
        console.print(f"[dim]{KEYS_HELP}[/]")
        sys.stdout.flush()

    # --- loop ----------------------------------------------------------
    def run(self) -> None:
        if not self.pages:
            console.print("Nothing to cull with these filters.")
            return
        with raw_terminal():
            while True:
                self.draw()
                k = getkey()
                shots = self.current()
                s = shots[self.sel]
                n = len(shots)
                if k in ("q", "esc") and not self.zoom:
                    break
                if k in ("esc", "q") and self.zoom:
                    self.zoom = False
                elif k in ("right", "l"):
                    self.sel = (self.sel + 1) % n
                elif k in ("left", "h"):
                    self.sel = (self.sel - 1) % n
                elif k in ("down", "j"):
                    self.sel = min(n - 1, self.sel + self.cols)
                elif k in ("up", "k"):
                    self.sel = max(0, self.sel - self.cols)
                elif k in "12345" and len(k) == 1:
                    self.decide(s, rating=int(k))
                elif k == "0":
                    self.decide(s, rating=0)
                elif k == "p":
                    self.decide(s, pick=0 if s.get("pick") == 1 else 1)
                elif k == "x":
                    self.decide(s, pick=0 if s.get("pick") == -1 else -1)
                elif k == "a":
                    batch = []
                    for x in shots:
                        if (x.get("pick") or 0) == 0 and x.get("suggestion") in ("pick", "reject"):
                            batch.append((x["id"], x.get("rating"), 0))
                            library.set_decision(self.conn, x["id"], pick=1 if x["suggestion"] == "pick" else -1)
                    if batch:
                        self.undo.append(batch)
                    self.refresh()
                elif k == "u" and self.undo:
                    for sid, r, p in self.undo.pop():
                        library.set_decision(self.conn, sid, rating=r or 0, pick=p)
                    self.refresh()
                elif k in (" ", "z"):
                    self.zoom = not self.zoom
                elif k == "o":
                    termimg.open_external(s["preview_path"])
                elif k in ("n", "enter", "]"):
                    if self.page < len(self.pages) - 1:
                        self.page += 1
                        self.sel = 0
                        self.zoom = False
                        self.refresh()
                elif k in ("b", "["):
                    if self.page > 0:
                        self.page -= 1
                        self.sel = 0
                        self.zoom = False
                        self.refresh()
