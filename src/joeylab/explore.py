"""Explore: the whole library on one surface, arranged by a rule you can read off the page.

The arrangements (ARRANGEMENTS):
  * content  what the photos show: the picture-search vectors laid out so alike photos sit together
             (t-SNE), each region named with a plain word or two from VOCAB;
  * colour   a wheel: the main hue around it, more colourful towards the rim, black and white in the
             middle (colours.py measures each photo);
  * light    brightness across, contrast up, with clipped highlights and shadows marked;
  * time     the date across (a column a day, empty stretches squeezed), the time of day down;
  * quality  sharpness across, the quality score up: the strongest frames gather top right;
  * camera   an island a camera, its lenses in bands.

Every photo gets a square cell of its own on a grid: photos that would land on the same spot go to
the nearest free cell, so each can be seen and clicked at any zoom. Axes that measure something
(light, quality) are spaced by rank, so each stretch of an axis holds as many photos and nothing
piles up; their ticks show where the actual values fall. Positions come from the whole library,
so a filter hides photos without moving the others.

Layouts are made on the server and kept until what they're made from changes. The picture-vector
layout takes seconds, so it's made in the background (the page shows how far it is) and kept in
the catalog's settings. Region names come from the search model's own text encoder, on this Mac.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from collections import Counter
from datetime import date

import numpy as np

from . import colours, config, db, embeddings

ARRANGEMENTS = ("content", "colour", "light", "time", "quality", "camera")
CONTENT_KEY = "explore_content"         # settings: the picture-vector layout, made in the background
CONTENT_VERSION = 1                     # bump when that layout or its names are made differently
HIGHLIGHTS_CLIPPED = 0.03               # this share of the frame pure white (scoring starts counting it here)
SHADOWS_CLIPPED = 0.10                  # this share pure black
MARK_HIGHLIGHTS, MARK_SHADOWS = 1, 2

# Plain words for what a region of the picture-vector layout shows: scenes, subjects, occasions and
# a few styles (black and white photos sit together whatever they show, so they need a name too).
VOCAB = """Beach, Sea, Waves, Coast, Cliffs, Lake, River, Waterfall, Mountains, Snowy mountains, Hills, Valley,
Forest, Jungle, Trees, Fields, Farmland, Rice fields, Tea gardens, Desert, Sand dunes, Snow, Glacier, Sky, Clouds,
Sunset, Sunrise, Night sky, Stars, Moon, Rain, Fog, Rainbow, Countryside, Village, Town square, City streets, Alleys,
Skyline, Skyscrapers, City at night, Neon lights, Traffic, Roads, Highway, Bridges, Harbour, Boats, Ships,
Railway station, Trains, Metro, Airport, Aeroplanes, View from a plane, Cars, Motorbikes, Bicycles, Buses, Tuk-tuks,
Parks, Gardens, Flowers, Lawns, Playground, Stadium, Swimming pool, Camping, Tents, Campfire, Hiking trail, Viewpoint,
Ruins, Castle, Fort, Palace, Temple, Mosque, Church, Monastery, Statues, Monuments, Museum, Art gallery, Paintings,
Sculpture, Murals, Graffiti, Markets, Street market, Shops, Shop windows, Signs, Restaurants, Cafés, Bars, Hotel,
Hotel room, Underwater, Coral reef, Fish, Aquarium, Zoo, Living room, Kitchen, Bedroom, Bathroom, Office, Desk,
Computers, Laptops, Phone screens, Screenshots, Documents, Receipts, Books, Whiteboard, Classroom, Lecture, Meeting,
Conference, Stage, Concert, Theatre, Dance, Dancers, Musicians, Wedding, Bride, Ceremony, Prayer, Festival,
Fireworks, Lanterns, Candles, Christmas, Party, Birthday party, Birthday cake, Dinner party, Gym, Yoga, Portraits,
Close-up portraits, Selfies, Group photos, Couples, Families, Children, Babies, Toddlers, Teenagers, Elderly people,
Friends, Crowds, People walking, Street portraits, Hands, Feet, Silhouettes, Fashion, Dresses, Traditional clothes,
Sarees, Suits, Food, Plates of food, Street food, Breakfast, Coffee, Tea, Cocktails, Wine, Beer, Desserts, Cakes,
Fruit, Vegetables, Pizza, Burgers, Sushi, Indian food, Barbecue, Cooking, Bakery, Ice cream, Dogs, Puppies, Cats,
Kittens, Birds, Parrots, Pigeons, Ducks, Horses, Cows, Goats, Sheep, Camels, Elephants, Monkeys, Deer, Lions, Tigers,
Wildlife, Insects, Butterflies, Bees, Spiders, Snakes, Lizards, Turtles, Squirrels, Sports, Football, Cricket, Tennis,
Basketball, Running, Cycling, Climbing, Bouldering, Skiing, Snowboarding, Surfing, Kayaking, Sailing, Scuba diving,
Golf, Trekking, Road trip, Cameras, Watches, Shoes, Bags, Toys, Plants, Houseplants, Furniture, Windows, Doors, Stairs,
Walls, Textures, Patterns, Close-ups, Jewellery, Musical instruments, Guitars, Tools, Car interiors, Trophies, Gifts,
Balloons, Clocks, Lamps, Black-and-white photos, Black-and-white portraits, Black-and-white street photos,
Long exposures, Light trails, Reflections, Shadows, Minimalism, Abstract, Bokeh, Blurry photos, Dark photos,
Old photographs, Scans, Maps, Charts, Illustrations, Cartoons, Logos"""
VOCAB = [w.strip() for w in VOCAB.replace("\n", " ").split(",") if w.strip()]
SAME_WORD = 0.93           # two words whose text vectors are this close say the same thing ("Dance", "Dancers")
DISTINCT = 0.75            # how much a word gains for fitting one region better than it fits the others
SECOND_WITHIN = 0.012      # a second word must score nearly as well as the first

# The colour wheel: names around the rim, at OKLab hues.
HUE_NAMES = [(29, "Red"), (58, "Orange"), (100, "Yellow"), (138, "Green"), (190, "Teal"), (250, "Blue"),
             (305, "Purple"), (350, "Pink")]
HUE_TOP = 15.0             # the hue at the top of the wheel (between pink and red); hues run clockwise
HUE_BY_COUNT = 0.75        # how much of the rim is shared out by how many photos have each hue
OTHER_CAMERAS = "Other cameras"
TIME_ROWS = 72             # the time of day: a row every 20 minutes
TIME_GAP = 8               # rows between the lines of a wrapped timeline, for its dates


# ---------------------------------------------------------------- the photos

_COLS = ("s.id, s.capture_str, s.time_src, s.camera, s.lens, m.brightness, m.contrast, m.clip_high, m.clip_low,"
         " m.sharp_subject, m.quality, c.hue, c.chroma, c.light, c.main, COALESCE(d.pick, 0), s.analyzed_fp")


_PHOTOS: dict[str, tuple] = {}     # catalog -> (its file stamp, _photos())


def _photos(conn) -> dict:
    """Every present photo that has been analysed (so it has a thumbnail), by id, with what the
    arrangements are made from. Kept until anything commits to the catalog (db.file_stamp): reading
    them all took a few hundred milliseconds on each visit to a big library."""
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    stamp = db.file_stamp(db_file)
    hit = _PHOTOS.get(db_file)
    if hit and hit[0] == stamp and not conn.in_transaction:
        return hit[1]
    P = _read_photos(conn)
    _PHOTOS[db_file] = (stamp, P)
    return P


def _read_photos(conn) -> dict:
    # (+status: every photo read in one pass in id order, not looked up one by one from the status index)
    rows = conn.execute(f"SELECT {_COLS} FROM shots s LEFT JOIN metrics m ON m.shot_id = s.id"
                        " LEFT JOIN colours c ON c.shot_id = s.id LEFT JOIN decisions d ON d.shot_id = s.id"
                        " WHERE +s.status='present' AND s.analyzed_fp IS NOT NULL AND s.thumb IS NOT NULL ORDER BY s.id").fetchall()
    cols = list(zip(*rows)) if rows else [()] * 17
    num = lambda k: np.array([np.nan if v is None else float(v) for v in cols[k]], dtype=np.float64)
    return {
        "ids": np.array(cols[0], dtype=np.int64), "when": list(cols[1]), "time_src": list(cols[2]),
        "camera": list(cols[3]), "lens": list(cols[4]), "brightness": num(5), "contrast": num(6),
        "clip_high": num(7), "clip_low": num(8), "sharp": num(9), "quality": num(10), "hue": num(11),
        "chroma": num(12), "light": num(13), "main": list(cols[14]), "pick": [int(v) for v in cols[15]],
        "fp": list(cols[16]),
    }


def _rank(v: np.ndarray) -> np.ndarray:
    """Each value's place among the others, 0-1 (ties share the middle of their run; NaN ranks as
    the median)."""
    v = np.where(np.isnan(v), np.nanmedian(v) if np.isfinite(v).any() else 0.0, v)
    order = np.argsort(v, kind="stable")
    r = np.empty(len(v))
    r[order] = np.arange(len(v))
    # ties: the mean rank of equal values
    _, inv, counts = np.unique(v, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=r)
    r = sums[inv] / counts[inv]
    return (r + 0.5) / max(1, len(v))


def _at_rank(v: np.ndarray, value: float) -> float:
    """Where on a rank-spaced axis (0-1) this value falls."""
    s = np.sort(v[np.isfinite(v)])
    return float(np.searchsorted(s, value) / max(1, len(s)))


# ---------------------------------------------------------------- one photo, one cell

_NEAR: list[tuple[int, int]] = sorted(((dx, dy) for dx in range(-6, 7) for dy in range(-6, 7)),
                                      key=lambda o: (o[0] ** 2 + o[1] ** 2, abs(o[1]), o[0], o[1]))
_NEAR_DX, _NEAR_DY = np.array([o[0] for o in _NEAR]), np.array([o[1] for o in _NEAR])
_NEAR_FIRST = 9                  # tried one by one before the rest of the neighbourhood at once


def snap(x: np.ndarray, y: np.ndarray, cols: int, rows: int, taken: np.ndarray | None = None,
         order: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Give every point a cell of its own on a cols x rows grid: the cell it falls in when that's
    free, else the nearest free one. `taken` marks cells already used (rows x cols); `order` is the
    order points choose in (by default a fixed shuffle, so no corner of a pile is favoured)."""
    n = len(x)
    if n > cols * rows - (int(taken.sum()) if taken is not None else 0):
        raise ValueError("More points than free cells")
    occ = bytearray(rows * cols) if taken is None else bytearray(taken.astype(np.uint8).tobytes())
    view = np.frombuffer(occ, dtype=np.uint8)
    gx, gy = np.zeros(n, dtype=np.int32), np.zeros(n, dtype=np.int32)
    xs, ys = np.asarray(x, dtype=np.float64).tolist(), np.asarray(y, dtype=np.float64).tolist()
    if order is None:
        order = np.random.default_rng(7).permutation(n)
    piles: dict[tuple, object] = {}             # points that fall in the same place, and found it full more than once
    for i in np.asarray(order).tolist():
        fx, fy = xs[i], ys[i]
        cx, cy = min(cols - 1, max(0, int(fx))), min(rows - 1, max(0, int(fy)))
        for dx, dy in _NEAR[:_NEAR_FIRST]:      # nearly always free near where it falls
            X, Y = cx + dx, cy + dy
            if 0 <= X < cols and 0 <= Y < rows and not occ[Y * cols + X]:
                occ[Y * cols + X] = 1
                gx[i], gy[i] = X, Y
                break
        else:
            # The rest of the neighbourhood at once, in the same order (a crowd made thousands of
            # points each try every cell around one by one).
            X, Y = cx + _NEAR_DX, cy + _NEAR_DY
            inside = (X >= 0) & (X < cols) & (Y >= 0) & (Y < rows)
            cells = (Y * cols + X)[inside]
            free = view[cells] == 0
            if free.any():
                k = int(cells[int(np.argmax(free))])
            else:                               # no free cell around: the nearest free cell anywhere
                pile = piles.get((fx, fy))
                if pile is None:
                    piles[(fx, fy)] = True
                    k = _nearest_free(view.reshape(rows, cols), fx, fy, cx, cy)
                else:
                    if pile is True:
                        pile = piles[(fx, fy)] = _Pile(fx, fy, cx, cy, rows, cols)
                    k = pile.next_free(view)
            occ[k] = 1
            gx[i], gy[i] = k % cols, k // cols
    return gx, gy


class _Pile:
    """Every cell in order of distance from one place (the lowest-numbered of equals first), made a ring
    at a time as needed: the points of a pile (thousands of photos with no brightness fall in one place)
    take the next free one in turn, rather than each searching ever wider squares. Cells are only ever
    taken, never freed, so one passed as taken stays taken: what this gives is the nearest free cell,
    exactly as _nearest_free would."""

    def __init__(self, fx: float, fy: float, cx: int, cy: int, rows: int, cols: int):
        self.fx, self.fy, self.cx, self.cy, self.rows, self.cols = fx, fy, cx, cy, rows, cols
        self.r, self.bound, self.cells, self.at = 4, -1.0, np.zeros(0, dtype=np.int64), 0

    def next_free(self, view: np.ndarray) -> int:
        while True:
            while self.at < len(self.cells):
                k = int(self.cells[self.at])
                self.at += 1
                if not view[k]:
                    return k
            if self.bound == math.inf:
                raise ValueError("More points than free cells")
            self._ring()

    def _ring(self) -> None:
        """The next ring: the cells nearer than (2r + 0.5) and no nearer than the last ring's edge (a
        square of side 2r around the place holds every cell nearer than that)."""
        self.r *= 2
        r, cols, rows = self.r, self.cols, self.rows
        x0, x1, y0, y1 = max(0, self.cx - r), min(cols, self.cx + r + 1), max(0, self.cy - r), min(rows, self.cy + r + 1)
        ys, xs = np.mgrid[y0:y1, x0:x1]
        xs, ys = xs.ravel(), ys.ravel()
        d = (xs + 0.5 - self.fx) ** 2 + (ys + 0.5 - self.fy) ** 2
        whole = x0 == 0 and y0 == 0 and x1 == cols and y1 == rows
        edge = math.inf if whole else (r + 0.5) ** 2
        keep = (d >= self.bound) & (d < edge) if self.bound >= 0 else d < edge
        k = ys[keep] * cols + xs[keep]
        self.cells, self.at, self.bound = k[np.lexsort((k, d[keep]))], 0, edge


def _nearest_free(grid: np.ndarray, fx: float, fy: float, cx: int, cy: int) -> int:
    """The free cell nearest (fx, fy), by the distance to its centre, the lowest-numbered of equals:
    looked for in a square around (cx, cy) that doubles until no free cell outside it can be nearer
    (any is at least r + 0.5 away), rather than across the whole grid for every point of a crowd."""
    rows, cols = grid.shape
    r = 8
    while True:
        x0, x1, y0, y1 = max(0, cx - r), min(cols, cx + r + 1), max(0, cy - r), min(rows, cy + r + 1)
        ys, xs = np.nonzero(grid[y0:y1, x0:x1] == 0)
        whole = x0 == 0 and y0 == 0 and x1 == cols and y1 == rows
        if len(xs):
            xs, ys = xs + x0, ys + y0
            d = (xs + 0.5 - fx) ** 2 + (ys + 0.5 - fy) ** 2
            best = d.min()
            if whole or best < (r + 0.5) ** 2:
                k = ys * cols + xs
                return int(k[d == best].min())
        elif whole:
            raise ValueError("More points than free cells")
        r *= 2


def _grid_for(n: int, aspect: float, fill: float) -> tuple[int, int]:
    """cols x rows of about this aspect (w/h) with room for n photos at this share of cells used."""
    cells = max(4.0, n / fill)
    rows = max(2, math.ceil(math.sqrt(cells / aspect)))
    cols = max(2, math.ceil(cells / rows))
    return cols, rows


# ---------------------------------------------------------------- the arrangements

def _light(P: dict) -> dict:
    n = len(P["ids"])
    cols, rows = _grid_for(n, 1.25, 0.8)
    b, c = P["brightness"], P["contrast"]
    x = _rank(b) * cols
    y = (1 - _rank(c)) * rows                       # punchier towards the top
    gx, gy = snap(x, y, cols, rows)
    # Brightness in the words the rest of the app uses for it: a share of full white.
    ticks_x = [[_at_rank(b, v * 2.55) * cols, f"{v}%"] for v in (10, 25, 50, 75, 90)]
    return {"gx": gx, "gy": gy, "cols": cols, "rows": rows, "guides": {
        "x": {"label": "Brightness", "low": "Darker", "high": "Brighter", "ticks": _ticks(ticks_x, cols)},
        "y": {"label": "Contrast", "low": "Flatter", "high": "Punchier", "ticks": []}}}


def _quality(P: dict) -> dict:
    n = len(P["ids"])
    cols, rows = _grid_for(n, 1.25, 0.8)
    s = np.log10(np.maximum(P["sharp"], 1.0))
    q = P["quality"]
    x = _rank(s) * cols
    y = (1 - _rank(q)) * rows
    gx, gy = snap(x, y, cols, rows)
    # The words the rest of the app uses for focus (scoring.flags_for): blurry under 60, soft under 150.
    ticks_x = [[_at_rank(s, math.log10(60)) * cols, "blurry"], [_at_rank(s, math.log10(150)) * cols, "soft"]]
    ticks_y = [[(1 - _at_rank(q, v)) * rows, str(v)] for v in (30, 50, 70, 85)]
    return {"gx": gx, "gy": gy, "cols": cols, "rows": rows, "guides": {
        "x": {"label": "Sharpness", "low": "Softer", "high": "Sharper", "ticks": _ticks(ticks_x, cols)},
        "y": {"label": "Quality score", "low": "Weaker", "high": "Stronger", "ticks": _ticks(ticks_y, rows)}}}


def _ticks(ticks: list, end: int) -> list:
    """Ticks that fall inside the axis (a value no photo comes near sits at an end: left out)."""
    return [[round(float(p), 2), t] for p, t in ticks if 0.5 < p < end - 0.5]


def _colour(P: dict) -> dict:
    """A wheel. Photos with colour go around the rim by hue, more colourful further out; black and
    white photos fill the middle, darker at the bottom. Each photo is drawn in the colour of its
    main hue (its main colour may be a dark or pale one that would read as the wrong hue here)."""
    n = len(P["ids"])
    chroma = np.nan_to_num(P["chroma"], nan=0.0)
    hue = P["hue"]
    grey = (chroma < colours.GREY_CHROMA) | np.isnan(hue)
    ring = np.flatnonzero(~grey)
    middle = np.flatnonzero(grey)
    r0 = math.sqrt(len(middle) / math.pi) if len(middle) else 0.0
    inner = r0 + 1.5 if len(middle) else 0.0
    R = math.sqrt(inner ** 2 + len(ring) / 0.8 / math.pi) + 0.5
    size = math.ceil(2 * R) + 2
    cx = cy = size / 2
    gx, gy = np.zeros(n, dtype=np.int32), np.zeros(n, dtype=np.int32)
    taken = np.zeros((size, size), dtype=bool)
    # The middle: black and white photos packed in a disc, in rows from darkest (bottom) to lightest.
    if len(middle):
        yy, xx = np.mgrid[0:size, 0:size]
        while True:
            inside = (xx + 0.5 - cx) ** 2 + (yy + 0.5 - cy) ** 2 <= r0 ** 2
            cells = np.flatnonzero(inside.ravel())
            if len(cells) >= len(middle):
                break
            r0 += 0.25
        cells = cells[np.lexsort((cells % size, -(cells // size)))][: len(middle)]      # bottom row first
        light = np.where(np.isnan(P["light"][middle]), P["brightness"][middle] / 255.0, P["light"][middle])
        by_light = middle[np.argsort(np.nan_to_num(light, nan=0.5), kind="stable")]
        gx[by_light], gy[by_light] = cells % size, cells // size
        taken.ravel()[cells] = True
    # The rim: the angle from the hue (mostly shared out by how many photos have each hue, so a
    # library of warm colours isn't one crowded wedge), the distance from how colourful.
    rim: dict = {}
    main = list(P["main"])
    if len(ring):
        h = (hue[ring] - HUE_TOP) % 360
        angle = HUE_BY_COUNT * _rank(h) + (1 - HUE_BY_COUNT) * h / 360
        u = _rank(chroma[ring])
        r = np.sqrt(inner ** 2 + u * (R ** 2 - inner ** 2))
        th = angle * 2 * np.pi
        rx, ry = snap(cx + r * np.sin(th), cy - r * np.cos(th), size, size, taken=taken)
        gx[ring], gy[ring] = rx, ry
        L = np.clip(np.nan_to_num(P["light"][ring], nan=0.6), 0.42, 0.82)
        C = np.clip(chroma[ring] * 2.2, 0.04, 0.2)
        hr = np.radians(hue[ring])
        rgb = colours.to_rgb(np.stack([L, C * np.cos(hr), C * np.sin(hr)], 1))
        for i, (rr, gg, bb) in zip(ring.tolist(), rgb.tolist()):
            main[i] = (rr << 16) | (gg << 8) | bb
        # Which hue is at each angle of the rim (to draw it), and where each colour name goes.
        hs = np.sort(h)
        grid = np.linspace(0, 360, 721)
        at = HUE_BY_COUNT * (np.searchsorted(hs, grid) / len(hs)) + (1 - HUE_BY_COUNT) * grid / 360
        stops = [[round(a * 360, 1), colours.hue_colour((float(np.interp(a, at, grid)) + HUE_TOP) % 360)]
                 for a in np.linspace(0, 1, 73)[:-1]]
        names = [[round(float(np.interp((hh - HUE_TOP) % 360, grid, at)) * 360, 1), name] for hh, name in HUE_NAMES]
        rim = {"stops": stops, "names": names}
    return {"gx": gx, "gy": gy, "cols": size, "rows": size, "main": main, "guides": {
        "wheel": {"cx": cx, "cy": cy, "r": R + 0.8, "r0": r0 if len(middle) else 0.0, "grey": int(len(middle)), **rim}}}


def _time(P: dict) -> dict:
    """The date across, a column or a few a day (as wide as its busiest twenty minutes need), with
    empty stretches squeezed; the time of day down, midnight at the top. A long timeline wraps onto
    more lines, like text, each line a full day tall."""
    ROWS, GAP = TIME_ROWS, TIME_GAP
    n = len(P["ids"])
    days: dict[date, list[tuple[float, int]]] = {}
    undated: list[int] = []
    for i, (w, src) in enumerate(zip(P["when"], P["time_src"])):
        try:
            if not w or src == "file":
                raise ValueError
            days.setdefault(date.fromisoformat(w[:10]), []).append((int(w[11:13]) + int(w[14:16]) / 60, i))
        except (ValueError, IndexError):
            undated.append(i)
    seq = []                                    # (day, width, gap before it, photos by time)
    prev = None
    for day in sorted(days):
        ph = sorted(days[day])
        per_row = Counter(min(ROWS - 1, int(h * ROWS / 24)) for h, _ in ph)
        # (and always room for every photo of the day: a day of thousands, a wedding or a card copied
        # at once, didn't fit 24 columns, and the whole arrangement failed)
        width = max(1, min(24, math.ceil(max(per_row.values()) * 0.6)), math.ceil(len(ph) / ROWS))
        gap = 0
        if prev is not None:
            apart = (day - prev).days
            gap = (0 if apart <= 1 else 1 if apart <= 7 else 2 if apart <= 31 else 3) + (day.year != prev.year)
        seq.append((day, width, gap, ph))
        prev = day
    und_w = math.ceil(len(undated) / ROWS) if undated else 0
    total = sum(w + g for _, w, g, _ in seq) + (und_w + 3 if undated else 0)
    lines = max(1, round(math.sqrt(total / ((ROWS + GAP) * 1.8))))
    target = total / lines
    gx, gy = np.zeros(n, dtype=np.int32), np.zeros(n, dtype=np.int32)
    widths: list[int] = []
    months: list[list] = []
    years: list[list] = []
    x = 0
    line = 0
    last_month = None
    for day, width, gap, ph in seq:
        new_month = (day.year, day.month) != last_month
        if x and (x + gap + width > target * 1.25 or (new_month and x + gap + width > target)):
            widths.append(x)
            line, x, gap = line + 1, 0, 0
        x += gap
        top = line * (ROWS + GAP) + GAP
        if new_month:
            months.append([x, top, day.strftime("%b"), day.year])
            if last_month is None or day.year != last_month[0] or not x:
                years.append([x, top, str(day.year)])
            last_month = (day.year, day.month)
        # Within the day's block, each photo at its time of day, the block's rows filled from the left.
        seen: Counter = Counter()
        tx, ty = [], []
        for h, _ in ph:
            r = min(ROWS - 1, int(h * ROWS / 24))
            tx.append(min(width - 0.5, seen[r] + 0.5))
            ty.append(h * ROWS / 24)
            seen[r] += 1
        bx, by = snap(np.array(tx), np.array(ty), width, ROWS, order=np.arange(len(ph)))
        idx = [i for _, i in ph]
        gx[idx], gy[idx] = bx + x, by + top
        x += width
    undated_at = None
    if undated:
        if x and x + 3 + und_w > target * 1.25:
            widths.append(x)
            line, x = line + 1, 0
        else:
            x += 3 if x else 0
        top = line * (ROWS + GAP) + GAP
        k = np.arange(len(undated))
        gx[undated], gy[undated] = x + k // ROWS, top + k % ROWS
        undated_at = [x, top, und_w, len(undated)]
        x += und_w
    widths.append(x)
    hours = [[h * ROWS / 24, f"{h:02d}:00"] for h in (0, 6, 12, 18, 24)]
    bands = [[5.5 * ROWS / 24, 8 * ROWS / 24, "Around sunrise"], [17 * ROWS / 24, 19.5 * ROWS / 24, "Around sunset"]]
    return {"gx": gx, "gy": gy, "cols": max(widths) + 1, "rows": len(widths) * (ROWS + GAP), "guides": {
        "lines": [[k * (ROWS + GAP) + GAP, w] for k, w in enumerate(widths)], "line_rows": ROWS,
        "years": years, "months": months, "hours": hours, "bands": bands, "undated": undated_at}}


def _camera(P: dict) -> dict:
    """An island a camera (most photos first), its photos in rows by lens and then by date."""
    n = len(P["ids"])
    groups: dict[str, list[int]] = {}
    for i, cam in enumerate(P["camera"]):
        groups.setdefault((cam or "").strip() or "Unknown camera", []).append(i)
    # Cameras with only a few photos share one island, banded by camera instead of by lens.
    few = [k for k in groups if len(groups[k]) < max(20, 0.005 * n)]
    others = len(few) > 1
    if others:
        groups[OTHER_CAMERAS] = [i for k in few for i in groups.pop(k)]
    order = sorted(groups, key=lambda k: (k == OTHER_CAMERAS, -len(groups[k]), k))

    def band(cam: str, i: int) -> str:
        return (P["camera"][i] or "Unknown camera") if cam == OTHER_CAMERAS else (P["lens"][i] or "")
    total = sum(math.ceil(math.sqrt(len(groups[k]) * 1.4)) ** 2 for k in order)
    width_all = max(12, int(math.sqrt(total) * 1.5))
    gx, gy = np.zeros(n, dtype=np.int32), np.zeros(n, dtype=np.int32)
    islands, lenses = [], []
    x = y = shelf = 0
    for cam in order:
        mem = groups[cam]
        w = max(3, math.ceil(math.sqrt(len(mem) * 1.4)))
        h = math.ceil(len(mem) / w)
        if x and x + w > width_all:
            x, y, shelf = 0, y + shelf + 4, 0
        top = y + 2                                     # a row for the island's name
        mem = sorted(mem, key=lambda i: (band(cam, i) or "~", P["when"][i] or "", i))
        for k, i in enumerate(mem):
            gx[i], gy[i] = x + k % w, top + k // w
        islands.append([x, y, w, h + 2, cam, len(mem)])
        k = 0
        while k < len(mem):                             # where each lens (or camera) starts
            lens = band(cam, mem[k])
            j = k
            while j < len(mem) and band(cam, mem[j]) == lens:
                j += 1
            if j - k >= max(8, len(mem) // 50) or cam == OTHER_CAMERAS:
                lenses.append([x + k % w, top + k // w, lens or "Lens not recorded", j - k])
            k = j
        x += w + 4
        shelf = max(shelf, h + 2)
    cols, rows = max(1, width_all + 2), max(1, y + shelf + 1)
    cols = max(cols, int(gx.max(initial=0)) + 1)
    return {"gx": gx, "gy": gy, "cols": cols, "rows": rows, "guides": {"islands": islands, "lenses": lenses}}


# ---------------------------------------------------------------- what's in them

def kmeans(X: np.ndarray, k: int, iters: int = 25, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Spherical k-means on unit vectors: (each row's cluster, the unit centres)."""
    n = len(X)
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)
    C = [X[int(rng.integers(n))]]
    d2 = np.full(n, np.inf)
    for _ in range(1, k):                        # k-means++ starts
        d2 = np.minimum(d2, np.maximum(0.0, 2 - 2 * (X @ C[-1])))
        p = d2 / d2.sum() if d2.sum() > 0 else None
        C.append(X[int(rng.choice(n, p=p))])
    Cm = np.stack(C)
    lab = np.zeros(n, dtype=np.int64)
    for _ in range(iters):
        lab = np.concatenate([(X[i : i + 4096] @ Cm.T).argmax(1) for i in range(0, n, 4096)])
        new = np.stack([X[lab == j].mean(0) if (lab == j).any() else Cm[j] for j in range(k)])
        new /= np.linalg.norm(new, axis=1, keepdims=True) + 1e-9
        if np.allclose(new, Cm, atol=1e-5):
            break
        Cm = new
    return lab, Cm


def _encoder_tag(model: str) -> str:
    if model == "siglip2":
        from . import search

        return search.MODEL_TAG
    from . import ai

    return ai.MODEL_TAG


def _model_words(model: str, words: list[str]) -> np.ndarray | None:
    """Text vectors for words, from the same model as the picture vectors ("siglip2" or "clip"),
    on this Mac. None when that model can't be loaded."""
    try:
        if model == "siglip2":
            from . import search

            if not search.available():
                return None
            m = search.Model.get()
        else:
            from . import ai

            if not ai.available():
                return None
            m = ai.Models.get()
        return np.stack([m.embed_text(w) for w in words]).astype(np.float32)
    except Exception:
        return None


def word_vectors(model: str) -> np.ndarray | None:
    """VOCAB's text vectors (unit rows), worked out once and kept in ~/.joeylab/models."""
    key = hashlib.sha1(json.dumps([_encoder_tag(model), VOCAB]).encode()).hexdigest()[:12]
    path = config.MODELS_DIR / f"explore-words-{key}.npz"
    if path.exists():
        try:
            with np.load(path) as f:
                return f["vectors"]
        except (OSError, ValueError, KeyError):
            pass
    if encode_words is _model_words and config.offline():
        return None                    # the tests (and CI) never load a model for this
    W = encode_words(model, VOCAB)
    if W is None or len(W) != len(VOCAB):
        return None
    W = W / (np.linalg.norm(W, axis=1, keepdims=True) + 1e-9)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}-{os.getpid()}.npz")
    np.savez(tmp, vectors=W.astype(np.float32))
    os.replace(tmp, path)
    for old in path.parent.glob("explore-words-*"):
        if old != path:
            old.unlink(missing_ok=True)
    return W


encode_words = _model_words       # (the tests put a stand-in text encoder here)


def name_clusters(centres: np.ndarray, W: np.ndarray, words: list[str] = VOCAB) -> list[str]:
    """A word or two for each cluster: the words its centre is closest to, favouring words that
    fit it better than they fit the other clusters (so "Portraits" doesn't name half the library).
    A second word is added when it says something the first doesn't."""
    S = centres @ W.T
    score = S + DISTINCT * (S - S.mean(0))
    WW = W @ W.T
    out = []
    for j in range(len(centres)):
        best = np.argsort(-score[j])
        first = int(best[0])
        name = words[first]
        for b in best[1:6]:
            b = int(b)
            if WW[first, b] < SAME_WORD and score[j, b] > score[j, first] - SECOND_WITHIN and not _overlap(words[first], words[b]):
                name = f"{words[first]} · {words[b].lower()}"
                break
        out.append(name)
    return out


def _overlap(a: str, b: str) -> bool:
    """Do two names share a word ("Portraits", "Street portraits")?"""
    wa = {w.rstrip("s") for w in a.lower().replace("-", " ").split()}
    return bool(wa & {w.rstrip("s") for w in b.lower().replace("-", " ").split()})


def _tsne(X: np.ndarray, perplexity: float = 30.0, iters: int = 600, seed: int = 0, progress=None) -> np.ndarray:
    """Exact t-SNE in numpy, for up to a couple of thousand points."""
    n = len(X)
    X = X.astype(np.float64)
    sq = (X ** 2).sum(1)
    D = np.maximum(0, sq[:, None] + sq[None, :] - 2 * X @ X.T)
    P = np.zeros((n, n))
    target = np.log(perplexity)
    for i in range(n):
        lo, hi, beta = 1e-20, 1e20, 1.0
        di = np.delete(D[i], i)
        for _ in range(60):
            p = np.exp(-di * beta)
            sp = p.sum() or 1e-12
            H = np.log(sp) + beta * (di * p).sum() / sp
            if abs(H - target) < 1e-4:
                break
            if H > target:
                lo = beta
                beta = beta * 2 if hi == 1e20 else (beta + hi) / 2
            else:
                hi = beta
                beta = (beta + lo) / 2
        P[i, np.arange(n) != i] = p / sp
    P = (P + P.T) / (2 * n)
    P = np.maximum(P, 1e-12)
    rng = np.random.default_rng(seed)
    Y = rng.normal(0, 1e-4, (n, 2))
    vel = np.zeros_like(Y)
    gains = np.ones_like(Y)
    lr = max(n / 12, 50)
    for it in range(iters):
        exag = 12.0 if it < 120 else 1.0
        sy = (Y ** 2).sum(1)
        num = 1 / (1 + np.maximum(0, sy[:, None] + sy[None, :] - 2 * Y @ Y.T))
        np.fill_diagonal(num, 0)
        Q = np.maximum(num / num.sum(), 1e-12)
        PQ = (exag * P - Q) * num
        grad = 4 * ((np.diag(PQ.sum(1)) - PQ) @ Y)
        gains = np.where(np.sign(grad) != np.sign(vel), gains + 0.2, gains * 0.8).clip(0.01)
        mom = 0.5 if it < 250 else 0.8
        vel = mom * vel - lr * gains * grad
        Y += vel
        Y -= Y.mean(0)
        if progress and it % 30 == 0:
            progress(it, iters)
    return Y


def _content(conn, P: dict, progress=None, landmarks: int = 1400) -> dict | None:
    """t-SNE on a sample of landmarks; every other photo goes beside the landmarks it most resembles.
    Landmarks that are still in the library keep their places from the last layout, so an update
    that adds a few photos doesn't redraw the whole map. Regions are clusters of the vectors, named."""
    m = embeddings.matrix(conn)
    if m is None:
        return None
    vids, mat16, model = m
    keep = np.isin(vids, P["ids"])
    vids, mat = vids[keep], mat16[keep].astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
    if len(vids) < 3:
        return None
    rng = np.random.default_rng(1)
    k = min(landmarks, len(vids))
    pos = {int(v): i for i, v in enumerate(vids)}
    old = db.get_setting(conn, CONTENT_KEY + ":landmarks") or {}
    old_ids = [int(i) for i in old.get("ids", [])]
    still = [i for i in old_ids if i in pos]
    if old_ids and len(still) >= 0.9 * len(old_ids) and len(still) >= 0.6 * k and old.get("model") == model:
        lm = np.array([pos[i] for i in still])
        Y = np.array(old["y"], dtype=np.float64)[[j for j, i in enumerate(old_ids) if i in pos]]
    else:
        lm = rng.choice(len(vids), size=k, replace=False)
        Y = _tsne(mat[lm], progress=progress)
        conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (CONTENT_KEY + ":landmarks", json.dumps({"model": model, "ids": [int(vids[j]) for j in lm],
                                                              "y": np.round(Y, 3).tolist()})))
        conn.commit()
    pts = np.zeros((len(vids), 2))
    pts[lm] = Y
    rest = np.setdiff1d(np.arange(len(vids)), lm)
    spread = float(np.median(np.abs(Y - np.median(Y, 0)))) or 1.0
    for s in range(0, len(rest), 2048):
        chunk = rest[s:s + 2048]
        S = mat[chunk] @ mat[lm].T
        nn = np.argsort(-S, axis=1)[:, :5]
        w = np.take_along_axis(S, nn, 1)
        w = np.exp((w - w.max(1, keepdims=True)) * 20)
        w /= w.sum(1, keepdims=True)
        pts[chunk] = (Y[nn] * w[:, :, None]).sum(1) + rng.normal(0, 0.01 * spread, (len(chunk), 2))
    # Photos without a vector yet (being indexed) wait in a row underneath.
    lo, hi = pts.min(0), pts.max(0)
    span = np.maximum(hi - lo, 1e-9)
    aspect = float(min(2.2, max(0.6, span[0] / span[1])))
    cols, rows = _grid_for(len(vids), aspect, 0.42)
    x = (pts[:, 0] - lo[0]) / span[0] * (cols - 1) + 0.5
    y = (pts[:, 1] - lo[1]) / span[1] * (rows - 1) + 0.5
    gx, gy = snap(x, y, cols, rows)
    # Regions: clusters of the vectors, coarse and fine, each named and placed where most of it is.
    n = len(vids)
    regions = []
    region = np.full(len(vids), -1, dtype=np.int32)        # each photo's fine region, for the peek
    W = word_vectors(model)
    for level, kk in ((0, int(np.clip(round(math.sqrt(n) / 8), 3, 14))), (1, int(np.clip(round(math.sqrt(n) / 2.2), 6, 60)))):
        if n < kk * 4:
            continue
        lab, C = kmeans(mat, kk, seed=level)
        names = name_clusters(C, W) if W is not None and W.shape[1] == C.shape[1] else [None] * len(C)
        for j in range(len(C)):
            mem = np.flatnonzero(lab == j)
            if len(mem) < max(8, 0.004 * n) or names[j] is None:
                continue
            px, py = _heart(gx[mem].astype(float), gy[mem].astype(float))
            if level == 1:
                region[mem] = len(regions)
            regions.append({"name": names[j], "x": px, "y": py, "n": int(len(mem)), "level": level})
    out_ids = vids
    ids = P["ids"]
    missing = np.setdiff1d(ids, out_ids)
    if len(missing):
        rows_needed = math.ceil(len(missing) / cols)
        extra_y = rows + 2
        mx = np.arange(len(missing)) % cols
        my = extra_y + np.arange(len(missing)) // cols
        out_ids = np.concatenate([out_ids, missing])
        gx = np.concatenate([gx, mx.astype(np.int32)])
        gy = np.concatenate([gy, my.astype(np.int32)])
        region = np.concatenate([region, np.full(len(missing), -1, dtype=np.int32)])
        rows = extra_y + rows_needed
    order = np.argsort(out_ids)
    return {"ids": out_ids[order], "gx": gx[order], "gy": gy[order], "region": region[order], "cols": cols, "rows": rows,
            "guides": {"regions": regions, "named": W is not None, "unplaced": int(len(missing)), "model": model}}


def _heart(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Where most of a set of points is: the point with the most of the others close by (a region
    split in two is named at its bigger part, not between them)."""
    if len(x) > 300:
        pick = np.linspace(0, len(x) - 1, 300).astype(int)
        sx, sy = x[pick], y[pick]
    else:
        sx, sy = x, y
    r = max(2.0, 0.5 * math.sqrt(len(x)))
    d2 = (sx[:, None] - sx[None]) ** 2 + (sy[:, None] - sy[None]) ** 2
    best = int((d2 <= r * r).sum(1).argmax())
    near = d2[best] <= (2 * r) ** 2
    return round(float(sx[near].mean()) + 0.5, 2), round(float(sy[near].mean()) + 0.5, 2)


# ---------------------------------------------------------------- layouts, cached

_CACHE: dict[tuple, tuple] = {}
_WORK: dict[tuple, dict] = {}
_LOCK = threading.Lock()


def _inputs_key(by: str, P: dict) -> str:
    h = hashlib.blake2b(digest_size=12)
    h.update(by.encode())
    h.update(P["ids"].tobytes())
    if by == "colour":
        h.update(np.nan_to_num(P["hue"]).round(1).tobytes() + np.nan_to_num(P["chroma"]).round(4).tobytes())
        h.update(np.nan_to_num(P["light"]).round(3).tobytes())
    elif by == "light":
        h.update(np.nan_to_num(P["brightness"]).round(1).tobytes() + np.nan_to_num(P["contrast"]).round(1).tobytes())
    elif by == "quality":
        h.update(np.nan_to_num(P["sharp"]).round(0).tobytes() + np.nan_to_num(P["quality"]).round(1).tobytes())
    elif by == "time":
        h.update(json.dumps([P["when"], P["time_src"]]).encode())
    elif by == "camera":
        h.update(json.dumps([P["camera"], P["lens"]]).encode())
    return h.hexdigest()


def _content_key(conn, P: dict) -> str:
    words = hashlib.sha1("|".join(VOCAB).encode()).hexdigest()[:8]           # new words: the regions named again
    return (f"{CONTENT_VERSION}:{words}:{db.counter(conn, 'search_gen')}:{db.counter(conn, 'emb_gen')}:{len(P['ids'])}:"
            f"{int(P['ids'].sum()) % 1000003}")


def available(conn) -> dict:
    """Which arrangements can be shown: True, or why not."""
    out = {k: True for k in ARRANGEMENTS}
    if embeddings.matrix(conn) is None:
        from . import search

        out["content"] = ("Arranging by what's in the photos needs the picture-search index, which is made during updates "
                          "once the AI extra is installed." if not search.available() else
                          "Arranging by what's in the photos needs the picture-search index, which fills in during updates.")
    return out


def layout(conn, project: str, by: str) -> dict:
    """An arrangement of every analysed photo: {ready: True, ids, cells, cols, rows, guides, ...},
    or {ready: False, progress} while it's being made in the background (or the colours it needs
    are being measured), or {ready: False, missing} when it can't be made here."""
    if by not in ARRANGEMENTS:
        raise ValueError(f"Arrange by one of: {', '.join(ARRANGEMENTS)}")
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    measuring = colours.backfill(project)
    if by == "colour" and measuring.get("running"):
        return {"ready": False, "progress": {"what": "Measuring each photo's colours", "done": measuring["done"],
                                             "total": measuring["total"]}}
    P = _photos(conn)
    if not len(P["ids"]):
        return {"ready": False, "missing": "No photos have been analysed yet."}
    key = _content_key(conn, P) if by == "content" else _inputs_key(by, P)
    hit = _CACHE.get((db_file, by))
    if hit and hit[0] == key:
        return _finish(P, hit[1])
    if by != "content":
        made = _kept(project, by, key)
        if made is None:
            made = {"light": _light, "colour": _colour, "time": _time, "quality": _quality, "camera": _camera}[by](P)
            _keep(project, by, key, made)
        _CACHE[(db_file, by)] = (key, made)
        return _finish(P, made)
    saved = _saved(conn)
    if saved and saved["key"] == key:
        _CACHE[(db_file, by)] = (key, saved)
        return _finish(P, saved)
    why = available(conn)["content"]
    if why is not True:
        return {"ready": False, "missing": why}
    out = _in_background(project, db_file, key)
    if out.get("ready"):
        return _finish(P, out["made"])
    if saved:                                       # the last layout, to show meanwhile
        out["stale"] = _finish(P, saved)
    return out


# The other arrangements take a second or two to make for a big library, so each is also kept in the
# library's own folder (explore/<arrangement>.json), with the key of what it was made from: a restart of
# the app doesn't make them again. JoeyLab's own data, like the atlas sheets next to it.
def _kept_path(project: str, by: str):
    return config.project_dir(project) / "explore" / f"{by}.json"


def _kept(project: str, by: str, key: str) -> dict | None:
    try:
        with open(_kept_path(project, by), encoding="utf-8") as f:
            k = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(k, dict) or k.get("key") != key:
        return None
    made = dict(k["made"])
    made["gx"], made["gy"] = np.array(made["gx"], dtype=np.int32), np.array(made["gy"], dtype=np.int32)
    return made


def _keep(project: str, by: str, key: str, made: dict) -> None:
    def plain(v):
        return v.tolist() if isinstance(v, np.ndarray) else v.item() if isinstance(v, np.generic) else str(v)

    path = _kept_path(project, by)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump({"key": key, "made": made}, f, default=plain, separators=(",", ":"))
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _saved(conn) -> dict | None:
    """The picture-vector layout kept in the catalog's settings, as made."""
    s = db.get_setting(conn, CONTENT_KEY)
    if not s or not s.get("ids"):
        return None
    cells = np.array(s["cells"], dtype=np.int32)
    return {"key": s["key"], "ids": np.array(s["ids"], dtype=np.int64), "gx": cells[0::2], "gy": cells[1::2],
            "region": np.array(s.get("region") or [-1] * len(s["ids"]), dtype=np.int32), "cols": s["cols"], "rows": s["rows"],
            "guides": s["guides"]}


def _in_background(project: str, db_file: str, key: str) -> dict:
    """Make the picture-vector layout on a thread of its own; say how far it is."""
    with _LOCK:
        work = _WORK.get((db_file, "content"))
        if work and not work["running"] and work["key"] == key and work.get("made") is not None:
            return {"ready": True, "made": work["made"]}
        if work and not work["running"] and work.get("error"):
            _WORK.pop((db_file, "content"))          # said once; the next request tries again
            return {"ready": False, "error": work["error"]}
        if not work or (not work["running"] and work["key"] != key):
            work = _WORK[(db_file, "content")] = {"running": True, "key": key, "done": 0, "total": 100, "error": None}
            threading.Thread(target=_make_content, args=(project, db_file, work), name=f"explore-{project}", daemon=True).start()
    return {"ready": False, "progress": {"what": "Laying the photos out by what's in them", "done": work["done"], "total": 100}}


def _make_content(project: str, db_file: str, work: dict) -> None:
    conn = db.connect(project, create=False)
    try:
        P = _photos(conn)
        made = _content(conn, P, progress=lambda d, t: work.update(done=int(90 * d / t)))
        if made is None:
            work["error"] = "The picture-search index is empty."
            return
        made["key"] = work["key"]
        cells = np.empty(2 * len(made["ids"]), dtype=np.int64)
        cells[0::2], cells[1::2] = made["gx"], made["gy"]
        db.set_setting(conn, CONTENT_KEY, {"key": work["key"], "ids": made["ids"].tolist(), "cells": cells.tolist(),
                                           "region": made["region"].tolist(), "cols": made["cols"], "rows": made["rows"],
                                           "guides": made["guides"]})
        _CACHE[(db_file, "content")] = (work["key"], made)
        work["made"] = made
    except Exception as e:                          # said on the page that asked
        work["error"] = f"{type(e).__name__}: {e}"
    finally:
        conn.close()
        work["running"] = False


def _finish(P: dict, made: dict) -> dict:
    """The layout as the page gets it: ids in order, a cell each, each photo's main colour, marks and
    pick, and a version for each atlas sheet. Photos that came or went since the layout was made
    are left out (it's made again)."""
    at = {int(v): i for i, v in enumerate(P["ids"])}
    ids = np.asarray(made.get("ids", P["ids"]))
    have = np.array([int(i) in at for i in ids.tolist()], dtype=bool)
    ids, gx, gy = ids[have], np.asarray(made["gx"])[have], np.asarray(made["gy"])[have]
    idx = [at[int(i)] for i in ids.tolist()]
    main, marks = [], []
    own = made.get("main") or P["main"]
    for i in idx:
        c = own[i]
        if c is None:                           # not measured yet: a grey as light as the photo
            b = P["brightness"][i]
            g = int(128 if np.isnan(b) else b)
            c = (g << 16) | (g << 8) | g
        main.append(int(c))
        mk = 0
        if (P["clip_high"][i] or 0) >= HIGHLIGHTS_CLIPPED:
            mk |= MARK_HIGHLIGHTS
        if (P["clip_low"][i] or 0) >= SHADOWS_CLIPPED:
            mk |= MARK_SHADOWS
        marks.append(mk)
    cells = np.empty(2 * len(ids), dtype=np.int64)
    cells[0::2], cells[1::2] = gx, gy
    out = {"ready": True, "ids": [int(i) for i in ids.tolist()], "cells": cells.tolist(), "cols": int(made["cols"]),
           "rows": int(made["rows"]), "guides": made["guides"], "main": main, "marks": marks,
           "pick": [P["pick"][i] for i in idx], "sheets": sheet_versions(ids.tolist(), [P["fp"][i] for i in idx]),
           "measured": int(sum(1 for i in idx if P["main"][i] is not None))}
    if made.get("region") is not None:
        out["region"] = np.asarray(made["region"])[have].tolist()
    return out


# ---------------------------------------------------------------- small thumbnails, many to an image

SHEET = 1024                # photos to an atlas sheet: ids k*SHEET .. (k+1)*SHEET - 1, 32 x 32
ATLAS_SIDE = 32
CELL = 48                   # pixels a photo


def sheet_versions(ids, fps) -> dict[str, str]:
    """A version for each sheet, from the photos in it and the files their thumbnails came from."""
    out: dict[int, hashlib.blake2b] = {}
    for i, fp in zip(ids, fps):
        h = out.setdefault(int(i) // SHEET, hashlib.blake2b(digest_size=6))
        h.update(f"{int(i)}:{fp};".encode())
    return {str(k): h.hexdigest() for k, h in sorted(out.items())}


def atlas_sheet(project: str, conn, k: int):
    """Sheet k of the library's small thumbnails (square, CELL pixels), photo id i at cell i - k*SHEET,
    made from the 768 px thumbnails and kept in the library's own folder until they change."""
    from PIL import Image

    lo, hi = k * SHEET, (k + 1) * SHEET
    rows = conn.execute("SELECT id, thumb, analyzed_fp FROM shots WHERE id >= ? AND id < ? AND status='present'"
                        " AND analyzed_fp IS NOT NULL AND thumb IS NOT NULL ORDER BY id", (lo, hi)).fetchall()
    v = sheet_versions([r[0] for r in rows], [r[2] for r in rows]).get(str(k), "empty")
    folder = config.project_dir(project) / "atlas"
    dest = folder / f"{k}_{v}.jpg"
    if dest.exists():
        return dest
    sheet = Image.new("RGB", (ATLAS_SIDE * CELL, ATLAS_SIDE * CELL), (128, 128, 128))
    for sid, thumb, _ in rows:
        try:
            with Image.open(thumb) as im:
                im.draft("RGB", (CELL * 2, CELL * 2))
                im = im.convert("RGB")
                w, h = im.size
                s = min(w, h)
                im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).resize((CELL, CELL), Image.Resampling.BILINEAR)
        except (OSError, ValueError):
            continue
        j = sid - lo
        sheet.paste(im, ((j % ATLAS_SIDE) * CELL, (j // ATLAS_SIDE) * CELL))
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{k}-{os.getpid()}-{threading.get_ident()}.jpg"
    sheet.save(tmp, "JPEG", quality=78)
    os.replace(tmp, dest)
    for old in folder.glob(f"{k}_*.jpg"):
        if old != dest:
            old.unlink(missing_ok=True)
    return dest


def shown_ids(conn, flt) -> list[int]:
    """The ids a Library filter shows (as the grid would), among the photos Explore places."""
    from .library import LIGHT_FROM

    where, args = flt.sql()
    where = (where + " AND " if where else " WHERE ") + "s.analyzed_fp IS NOT NULL AND s.thumb IS NOT NULL"
    return [r[0] for r in conn.execute(f"SELECT s.id {LIGHT_FROM}{where} ORDER BY s.id", args)]


def facts(conn, sid: int) -> dict:
    """What a photo's place in each arrangement is made from, for the peek."""
    r = conn.execute("SELECT m.brightness, m.contrast, m.clip_high, m.clip_low, m.sharp_subject, m.quality,"
                     " c.hue, c.chroma, c.palette, s.camera, s.lens FROM shots s LEFT JOIN metrics m ON m.shot_id = s.id"
                     " LEFT JOIN colours c ON c.shot_id = s.id WHERE s.id = ?", (sid,)).fetchone()
    if not r:
        return {}
    b, c, ch, cl, sh, q, hue, chroma, pal, camera, lens = r
    out = {"camera": camera, "lens": lens,
           "brightness": None if b is None else round(b / 2.55), "contrast": None if c is None else round(c / 1.28),
           "highlights": bool(ch and ch >= HIGHLIGHTS_CLIPPED), "shadows": bool(cl and cl >= SHADOWS_CLIPPED),
           "focus": None if sh is None else "blurry" if sh < 60 else "soft" if sh < 150 else "sharp",
           "quality": None if q is None else round(q), "palette": pal.split(",") if pal else [], "colourful": None, "hue": None}
    if chroma is not None:
        out["colourful"] = "black and white" if chroma < colours.GREY_CHROMA / 2 else (
            "muted" if chroma < colours.GREY_CHROMA * 2.5 else "colourful" if chroma < 0.08 else "vivid")
    if hue is not None and chroma is not None and chroma >= colours.GREY_CHROMA:
        out["hue"] = min(HUE_NAMES, key=lambda hn: abs((hue - hn[0] + 180) % 360 - 180))[1]
    return out


