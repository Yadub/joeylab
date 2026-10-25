"""Inline images in the terminal (iTerm2 / WezTerm / Kitty / Ghostty) and contact sheets."""

from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
import sys
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont


def in_tmux() -> bool:
    return "TMUX" in os.environ


@lru_cache(maxsize=1)
def tmux_passthrough() -> bool:
    try:
        out = subprocess.run(["tmux", "show", "-Apv", "allow-passthrough"], capture_output=True, text=True, timeout=2)
        return out.stdout.strip().splitlines()[-1:] in (["on"], ["all"])
    except (OSError, subprocess.SubprocessError):
        return False


TMUX_HINT = ("Inline images are blocked by tmux. Enable with:  tmux set -g allow-passthrough on\n"
             "(add `set -g allow-passthrough on` to ~/.tmux.conf to keep it)")


def protocol() -> str | None:
    forced = os.environ.get("JOEYLAB_IMAGES")
    if forced:
        return None if forced == "none" else forced
    tp = os.environ.get("TERM_PROGRAM", "")
    if in_tmux():
        # iTerm2's protocol survives tmux when passthrough is allowed.
        if os.environ.get("LC_TERMINAL") == "iTerm2" and tmux_passthrough():
            return "iterm"
        return None
    if tp in ("iTerm.app", "WezTerm") or os.environ.get("LC_TERMINAL") == "iTerm2":
        return "iterm"
    if "KITTY_WINDOW_ID" in os.environ or tp == "ghostty" or "kitty" in os.environ.get("TERM", ""):
        return "kitty"
    return None


def _encode(img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def show(img: Image.Image, max_cols: int | None = None, max_rows: int | None = None) -> bool:
    """Draw an image at the cursor. Returns False when the terminal can't."""
    proto = protocol()
    if proto is None:
        return False
    cols, rows = shutil.get_terminal_size((120, 40))
    max_cols = min(max_cols or cols, cols)
    max_rows = max_rows or max(8, rows - 10)
    data = _encode(img)
    if proto == "iterm":
        b64 = base64.b64encode(data).decode()
        seq = (f"\033]1337;File=inline=1;size={len(data)};width={max_cols};height={max_rows};"
               f"preserveAspectRatio=1:{b64}\a")
        if in_tmux():
            seq = "\033Ptmux;" + seq.replace("\033", "\033\033") + "\033\\"
        sys.stdout.write(seq + "\n")
    else:
        # Kitty needs PNG for f=100; it scales to c x r cells.
        buf = io.BytesIO()
        img.save(buf, "PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        w, h = img.size
        # Fit into the cell box preserving aspect (cells are ~2:1 tall).
        c = max_cols
        r = int(c * h / w / 2.1)
        if r > max_rows:
            r = max_rows
            c = int(r * 2.1 * w / h)
        chunks = [b64[i : i + 4096] for i in range(0, len(b64), 4096)]
        for i, ch in enumerate(chunks):
            head = f"a=T,f=100,c={c},r={r}," if i == 0 else ""
            more = 1 if i < len(chunks) - 1 else 0
            sys.stdout.write(f"\033_G{head}m={more};{ch}\033\\")
        sys.stdout.write("\n")
    sys.stdout.flush()
    return True


def open_external(path: str) -> None:
    """Open a file in the OS viewer (Preview/Quick Look on macOS). Read-only."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@lru_cache(maxsize=8)
def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for p in ("/System/Library/Fonts/SFNSMono.ttf", "/System/Library/Fonts/Menlo.ttc",
              "/System/Library/Fonts/Helvetica.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


@lru_cache(maxsize=512)
def _thumb(path: str, cell: int) -> Image.Image | None:
    try:
        im = Image.open(path)
        im.draft("RGB", (cell, cell))
        im = im.convert("RGB")
        im.thumbnail((cell, cell))
        return im
    except OSError:
        return None


COLORS = {
    "bg": (24, 24, 27), "cell": (39, 39, 42), "text": (228, 228, 231), "dim": (161, 161, 170),
    "sel": (250, 204, 21), "pick": (34, 197, 94), "reject": (239, 68, 68), "star": (250, 204, 21),
    "sug": (96, 165, 250),
}


def _stars(d: ImageDraw.ImageDraw, x: int, cy: int, n: int, r: int = 9) -> None:
    """Five star outlines, the first n filled (drawn as polygons: no font glyphs needed)."""
    import math

    for k in range(5):
        cx = x + k * (2 * r + 4) + r
        pts = []
        for j in range(10):
            ang = -math.pi / 2 + j * math.pi / 5
            rad = r if j % 2 == 0 else r * 0.45
            pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
        if k < n:
            d.polygon(pts, fill=COLORS["star"])
        else:
            d.polygon(pts, outline=COLORS["dim"])


def contact_sheet(items: list[dict], selected: int | None = None, cols: int = 4, cell: int = 360) -> Image.Image:
    """items: {thumb, index, title, subtitle, rating, pick, suggestion}."""
    cols = max(1, min(cols, len(items)))
    rows = (len(items) + cols - 1) // cols
    pad, label_h = 10, 56
    W = cols * (cell + pad) + pad
    H = rows * (cell + label_h + pad) + pad
    sheet = Image.new("RGB", (W, H), COLORS["bg"])
    d = ImageDraw.Draw(sheet)
    f_big, f_small = font(22), font(17)
    for i, it in enumerate(items):
        r, c = divmod(i, cols)
        x = pad + c * (cell + pad)
        y = pad + r * (cell + label_h + pad)
        d.rectangle([x, y, x + cell, y + cell + label_h], fill=COLORS["cell"])
        th = _thumb(it["thumb"], cell) if it.get("thumb") else None
        if th is not None:
            if it.get("pick") == -1:
                th = Image.blend(th, Image.new("RGB", th.size, (0, 0, 0)), 0.55)
            sheet.paste(th, (x + (cell - th.width) // 2, y + (cell - th.height) // 2))
        border = None
        if it.get("pick") == 1:
            border = COLORS["pick"]
        elif it.get("pick") == -1:
            border = COLORS["reject"]
        if border:
            d.rectangle([x, y, x + cell, y + cell + label_h], outline=border, width=5)
        if i == selected:
            d.rectangle([x - 6, y - 6, x + cell + 6, y + cell + label_h + 6], outline=COLORS["sel"], width=6)

        # Label bar: index, stars, pick/reject, suggestion
        ly = y + cell + 4
        d.text((x + 8, ly), f"{it['index']}", font=f_big, fill=COLORS["sel"] if i == selected else COLORS["text"])
        _stars(d, x + 50, ly + 12, it.get("rating") or 0)
        tag = {1: ("PICK", COLORS["pick"]), -1: ("REJECT", COLORS["reject"])}.get(it.get("pick") or 0)
        if tag:
            tw = d.textlength(tag[0], font=f_small)
            d.text((x + cell - tw - 8, ly + 2), tag[0], font=f_small, fill=tag[1])
        elif it.get("suggestion"):
            s = f"({it['suggestion']}?)"
            tw = d.textlength(s, font=f_small)
            d.text((x + cell - tw - 8, ly + 2), s, font=f_small, fill=COLORS["sug"])
        d.text((x + 8, ly + 28), it.get("subtitle", "")[:44], font=f_small, fill=COLORS["dim"])
    return sheet
