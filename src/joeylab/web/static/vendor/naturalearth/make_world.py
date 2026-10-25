"""Make world.json, the land and borders of the offline map in Places, from Natural Earth.

    python make_world.py <folder with the Natural Earth GeoJSON files> [tolerance in degrees, 0.03]

It reads ne_50m_land.geojson and ne_50m_admin_0_boundary_lines_land.geojson (public domain, from
naturalearthdata.com), simplifies each outline (Douglas-Peucker), rounds to hundredths of a degree
and stores each point as the difference from the one before, which keeps the file near 230 KB.
"""

import json
import sys
from pathlib import Path

SCALE = 100


def simplify(pts, eps):
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        length = (dx * dx + dy * dy) ** 0.5
        best, at = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            d = abs(dy * px - dx * py + bx * ay - by * ax) / length if length else ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            if d > best:
                best, at = d, i
        if best > eps and at > 0:
            keep[at] = True
            stack += [(a, at), (at, b)]
    return [p for p, k in zip(pts, keep) if k]


def pack(pts, closed, eps):
    if closed and len(pts) > 3:          # a closed ring in two halves, so its shape survives
        mid = len(pts) // 2
        pts = simplify(pts[: mid + 1], eps)[:-1] + simplify(pts[mid:], eps)
    else:
        pts = simplify(pts, eps)
    q = []
    for x, y in pts:
        p = (round(x * SCALE), round(y * SCALE))
        if not q or q[-1] != p:
            q.append(p)
    if len(q) < (4 if closed else 2):
        return None
    flat, px, py = [], 0, 0
    for x, y in q:
        flat += [x - px, y - py]
        px, py = x, y
    return flat


def main(src: Path, eps: float) -> None:
    land = []
    for f in json.loads((src / "ne_50m_land.geojson").read_text())["features"]:
        g = f["geometry"]
        for poly in [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]:
            rings = [pack(r, True, eps) for r in poly]
            if rings[0]:
                land.append([r for r in rings if r])
    borders = []
    for f in json.loads((src / "ne_50m_admin_0_boundary_lines_land.geojson").read_text())["features"]:
        g = f["geometry"]
        for line in [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]:
            p = pack(line, False, eps)
            if p:
                borders.append(p)
    doc = {"about": "Natural Earth 1:50m land and land borders between countries (public domain, naturalearthdata.com), "
                    "simplified for JoeyLab's offline map. Coordinates are hundredths of a degree, [lon, lat], each pair "
                    "after the first given as the difference from the one before.",
           "scale": SCALE, "land": land, "borders": borders}
    out = Path(__file__).with_name("world.json")
    out.write_text(json.dumps(doc, separators=(",", ":")))
    print(f"{out}: {out.stat().st_size // 1024} KB, {len(land)} land outlines, {len(borders)} borders")


if __name__ == "__main__":
    main(Path(sys.argv[1]), float(sys.argv[2]) if len(sys.argv) > 2 else 0.03)
