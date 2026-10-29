"""Events and trips: the natural units of an archive, detected from time, place and folders.

* An event is a run of photos with no gap longer than a few hours (all cameras together, on
  the shared timeline). A jump of more than ~100 km also starts a new one.
* An event is away when its place is further than the trip distance (100 km unless you change it)
  from every home, or in another country than every home. Homes are the ones you set (for the
  whole Mac, see geo.homes) or, until you set any, JoeyLab's guess: the place photographed in the
  most months.
* A trip is a run of away events with no more than two days between them, lasting a day or more;
  abroad, a single day is a trip too.
* An event's place comes from its photos' GPS (or that of photos taken around them), a place you
  set, or else, as a guess, a folder that names a town, region or country, or cameras' clocks set
  to another time zone than home's.

Ids are stable: re-detection reuses the id of the old event most of its photos were in, so a
name you give stays. Everything here is catalog-only.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timezone

from . import db, geo

EVENT_GAP = 3 * 3600
TRIP_GAP = 2 * 86400
JUMP_KM = 100
MIN_EVENT = 3                  # fewer photos than this (and no name) are folded into neighbours' lists

_GENERIC = re.compile(
    r"^(dcim|\d{3}[_-]?[a-z0-9]*|\d{3,4}|sd ?card.*|backup.*|.*to sort.*|recovered.*|pictures|photos|camera|camera roll|"
    r"untitled.*|import.*|raw|raf|jpe?g|jpegs|edits?|exports?|selected.*|chosen.*|picks?|shared|"
    r"misc|new folder.*|images?|img|fuji.*|fujifilm.*|canon.*|sony.*|nikon.*|gopro.*|"
    r"unsure|unsorted|maybe|to delete|delete|rejects?|keepers?|selects?|best ones?|favou?rites|todo|temp|tmp|"
    r"mov|mp4|videos?|clips?|footage|random|various|other|others|"
    r"volumes|users|desktop|downloads|documents|movies|icloud.*|dropbox|google drive|onedrive|"
    r"(cam|camera|card|disk|drive|body|shooter|photographer|second shooter|2nd shooter)[\s_-]*\d*|"
    r"[a-z]?\d+[\s_-]*(cam|camera)?)$", re.I)
# A leading date, however it's written: "2023-09", "202308", "2026-06-01to02" (a range of days).
_DATE = re.compile(r"^\s*(\d{4}([-_. ]?\d{1,2}){0,2})(\s*(to|–|-)\s*\d{1,2}(?!\d))?\s*[-_.]?\s*", re.I)


_TAIL = re.compile(r"[\s_-]+(jpe?gs?|raws?|rafs?|edits?|exports?|selects?|selected|high res|hi-?res|photos?|pics|images|"
                   r"backups?|copy( \d+)?|copies|\(\d+\)|\(to sort\))$", re.I)
# Places copies end up, which never name an event ("Desktop dump", "Old Laptop", "Lisbon copy" is Lisbon).
_DUMP = re.compile(r"^(.*\bdumps?\b.*|desktop\b.*|old (laptop|mac|macbook|computer|pc|phone|iphone|drive|disk|card)s?\b.*|"
                   r".*\bbackups?\b.*|copy of .*|shared .*)$", re.I)


def generic_folder(name: str) -> bool:
    """A folder name that says nothing about what's in it ("DCIM", "Pictures", "Desktop dump")."""
    return bool(_GENERIC.match(name) or _DUMP.match(name))


def _own_parts(path_dir: str) -> list[str]:
    """A photo folder's own names: never the drive's ("/Volumes/Photo Drive") or the home folder's
    (/Users/name), which say nothing about the photos."""
    parts = path_dir.split(os.sep)
    if len(parts) > 2 and parts[0] == "" and parts[1] == "Volumes":
        parts = parts[3:]
    home = os.path.expanduser("~").split(os.sep)
    if parts[: len(home)] == home:
        parts = parts[len(home):]
    return [x for x in parts if x]


# Folders a camera makes inside a card ("DCIM", "100_FUJI", "101MSDCF"): the folder above says more.
_CAMERA_DIR = re.compile(r"^(dcim|\d{3}[_-]?[a-z0-9]*|\d{3,4}|raws?|raf|arw|cr[23w]|nef|orf|rw2|dng|jpe?gs?|heic|hif|tiff?|mov|mp4|videos?)$", re.I)


def folder_label(path_dir: str) -> str | None:
    """The folder to show for a photo as it's named on disk, generic or not ("SD Card 20251110 (TO
    SORT)"), skipping the folders a camera makes inside a card."""
    for part in reversed(_own_parts(path_dir)):
        if not _CAMERA_DIR.match(part):
            return part
    return None


def folder_hint(path_dir: str) -> str | None:
    """A descriptive folder name ("2023-09 Cornwall - Family Holiday" -> "Cornwall - Family Holiday",
    "carnival_photos" -> "Carnival", "Rome JPEGs" -> "Rome")."""
    for part in reversed(_own_parts(path_dir)[-3:]):
        name = _DATE.sub("", part).strip(" -_")
        name = re.sub(r"_+", " ", name).strip()
        while _TAIL.search(name):
            name = _TAIL.sub("", name).strip(" -_")
        if name and not generic_folder(name) and not generic_folder(part) and len(name) > 2:
            return name[0].upper() + name[1:]
    return None


def _date_range(a: float, b: float) -> str:
    da, dbb = datetime.fromtimestamp(a, timezone.utc), datetime.fromtimestamp(b, timezone.utc)
    if da.date() == dbb.date():
        return da.strftime("%-d %b %Y")
    if (da.year, da.month) == (dbb.year, dbb.month):
        return f"{da.day}–{dbb.strftime('%-d %b %Y')}"
    if da.year == dbb.year:
        return f"{da.strftime('%-d %b')} – {dbb.strftime('%-d %b %Y')}"
    return f"{da.strftime('%-d %b %Y')} – {dbb.strftime('%-d %b %Y')}"


# What detection reads of each photo. A place guessed from a folder name is worked out again each
# time, so it's read as no place.
_COLS = ("s.id, s.eff_ts, s.dir, CASE WHEN s.place_src = 'folder' THEN NULL ELSE s.place_id END AS place_id,"
         " s.place_src, s.event_id, s.tz_offset, s.camera, s.body_serial, m.quality")


def detect(conn) -> list[dict]:
    """Events from kept photos only: an extra copy of a photo kept elsewhere (a backup, a dump) makes
    no event of its own, splits none and names none; nor does its backup on another drive."""
    from .dupes import backup_copy_shot_ids, duplicate_shot_ids

    extras = duplicate_shot_ids(conn) | backup_copy_shot_ids(conn)
    rows = conn.execute(
        f"SELECT {_COLS} FROM shots s LEFT JOIN metrics m ON m.shot_id = s.id"
        " WHERE s.status='present' AND s.eff_ts IS NOT NULL AND COALESCE(s.time_src, 'exif') = 'exif'"
        " ORDER BY s.eff_ts, s.id").fetchall()
    events: list[list] = []
    last_place = None
    for r in rows:
        if r["id"] in extras:
            continue
        if events:
            gap = r["eff_ts"] - events[-1][-1]["eff_ts"]
            jump = (r["place_id"] and last_place and r["place_id"] != last_place
                    and (geo.distance_km(conn, r["place_id"], last_place) or 0) > JUMP_KM)
            if gap <= EVENT_GAP and not jump:
                events[-1].append(r)
                last_place = r["place_id"] or last_place
                continue
        events.append([r])
        last_place = r["place_id"]
    events = _merge(events)
    runs = _joined(events, joins(conn, "event"), lambda ev: [r["id"] for r in ev])
    return [{"rows": [r for i in run for r in events[i]]} for run in runs]


JOINS_KEY = "event_joins"


def joins(conn, kind: str) -> list[tuple[int, int]]:
    """Pairs of photos you said belong to one event (or one trip), by merging: kept per library, so
    a merge holds through every update."""
    return [(int(j["a"]), int(j["b"])) for j in db.get_setting(conn, JOINS_KEY, []) or []
            if isinstance(j, dict) and j.get("kind") == kind and j.get("a") and j.get("b")]


def _joined(groups: list, pairs: list[tuple[int, int]], ids_of) -> list[list[int]]:
    """Runs of group indexes to make one: the groups holding each pair of merged photos, and every
    group between them. Every group is in exactly one run, in order."""
    at = {sid: i for i, g in enumerate(groups) for sid in ids_of(g)}
    link = [False] * len(groups)
    for a, b in pairs:
        if a in at and b in at:
            i, j = sorted((at[a], at[b]))
            for k in range(i, j):
                link[k] = True
    runs: list[list[int]] = []
    cur: list[int] = []
    for i in range(len(groups)):
        cur.append(i)
        if not link[i]:
            runs.append(cur)
            cur = []
    return runs


SAME_NAME_GAP = 36 * 3600     # one wedding over two days, shot in bursts, is still one event
SMALL = 5                     # fewer photos than this join a neighbour within SMALL_GAP
SMALL_GAP = 6 * 3600


def _name_of(rows) -> str | None:
    hints = Counter(folder_hint(r["dir"]) for r in rows)
    hints.pop(None, None)
    if not hints:
        return None
    name, k = hints.most_common(1)[0]
    return name if k >= 0.5 * len(rows) else None


def _merge(events: list[list]) -> list[list]:
    """Join neighbours that are clearly one occasion: the same folder name within a day and a
    half, or a handful of photos a few hours from a bigger event."""
    if not events:
        return events
    names = [_name_of(e) for e in events]
    out, out_names = [events[0]], [names[0]]
    for ev, name in zip(events[1:], names[1:]):
        gap = ev[0]["eff_ts"] - out[-1][-1]["eff_ts"]
        if (name and name == out_names[-1] and gap <= SAME_NAME_GAP) or \
                ((len(ev) < SMALL or len(out[-1]) < SMALL) and gap <= SMALL_GAP and not (name and out_names[-1] and name != out_names[-1])):
            out[-1] = out[-1] + ev
            out_names[-1] = out_names[-1] or name
        else:
            out.append(ev)
            out_names.append(name)
    return out


def _describe(conn, ev: dict) -> dict:
    rows = ev["rows"]
    ids = [r["id"] for r in rows]
    places = Counter(r["place_id"] for r in rows if r["place_id"])
    folders = Counter(folder_hint(r["dir"]) for r in rows)
    folders.pop(None, None)
    best = max(rows, key=lambda r: r["quality"] or 0)
    place_names = []
    for pid, k in places.most_common(4):
        p = conn.execute("SELECT name, country FROM places WHERE id=?", (pid,)).fetchone()
        if p:
            place_names.append([f"{p[0]}, {p[1]}" if p[1] else p[0], k])
    people: list = []         # filled in after photos are assigned to events
    when = _date_range(rows[0]["eff_ts"], rows[-1]["eff_ts"])
    folder, fk = folders.most_common(1)[0] if folders else (None, 0)
    folder = folder if folder and fk >= 0.5 * len(rows) else None
    labels = Counter(folder_label(r["dir"]) for r in rows)
    labels.pop(None, None)
    label, lk = labels.most_common(1)[0] if labels else (None, 0)
    place = place_names[0][0].split(",")[0] if place_names else None
    name = folder or place
    return {"ids": ids, "start": rows[0]["eff_ts"], "end": rows[-1]["eff_ts"], "n": len(rows),
            "cover": best["id"], "places": place_names, "people": people, "place_counts": places,
            "folder_name": folder, "place_name": place, "when": when, "alt_name": None,
            "folder_label": label if label and lk >= 0.5 * len(rows) else None,
            "auto_name": f"{name} · {when}" if name else when, "main_place": places.most_common(1)[0][0] if places else None}


def _name_by_kind(conn, found: list[dict]) -> None:
    """What each event looks like, from its photos, on this Mac ("Dance performance", "Climbing in
    Chamonix"): its other name, beside the folder's. An event whose folders don't name it is called by
    it ("Dance performance · 30–31 Aug")."""
    if not found:
        return
    from . import scenes

    for ev, kind in zip(found, scenes.event_kinds(conn, [ev["ids"] for ev in found])):
        if kind:
            ev["kind_label"] = kind
            ev["alt_name"] = f"{kind} in {ev['place_name']}" if ev["place_name"] else kind
            if not ev["folder_name"]:
                ev["auto_name"] = f"{ev['alt_name']} · {ev['when']}"


def _trip_label(conn, counts: Counter) -> str:
    """"Chamonix" when the places share a region, "Portugal and Spain" across
    countries, else the main place."""
    rows = []
    for pid, k in counts.most_common(6):
        r = conn.execute("SELECT name, admin1, country FROM places WHERE id=?", (pid,)).fetchone()
        if r:
            rows.append((r[0], r[1], r[2], k))
    if not rows:
        return "Trip"
    countries = list(dict.fromkeys(r[2] for r in rows if r[2]))
    if len(countries) > 1:
        return " and ".join(countries[:2])
    regions = {r[1] for r in rows}
    if len(rows) > 1 and len(regions) == 1 and rows[0][1]:
        return rows[0][1]
    if len(rows) > 2:
        return countries[0] if countries else rows[0][0]
    return rows[0][1] if rows[0][1] and rows[0][3] < sum(r[3] for r in rows) else rows[0][0]


def _stable_ids(conn, groups: list[dict], old_of: dict[int, int], kind: str) -> list[int]:
    hi_old = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    # Both as numbers: meta keeps text, and text sorts above any number, so a plain MAX would wind
    # the mark back below ids just handed out to events, and a trip would be given one of them.
    conn.execute("INSERT INTO meta(key, value) VALUES ('hwm:events', ?) ON CONFLICT(key) DO UPDATE SET"
                 " value = MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))", (hi_old,))
    old_kind = {r[0]: r[1] for r in conn.execute("SELECT id, kind FROM events")}
    taken: set[int] = set()
    out: list[int | None] = [None] * len(groups)
    for i in sorted(range(len(groups)), key=lambda i: -groups[i]["n"]):
        votes = Counter(old_of[s] for s in groups[i]["ids"] if s in old_of and old_kind.get(old_of[s]) == kind)
        for eid, _ in votes.most_common():
            if eid not in taken:
                out[i] = eid
                taken.add(eid)
                break
    return [e if e is not None else db.new_id(conn, "events") for e in out]


class Rule:
    """What decides trips in one detection: your homes (or JoeyLab's guess while you set none),
    how far from them is away, and where folder names and cameras' clocks say events were."""

    def __init__(self, conn):
        from .grouping import camera_key

        r = geo.rule(conn)
        self.key = geo.rule_key(r)
        self.km = r["km"]
        self.homes = r["homes"] or ([r["guess"]] if r["guess"] else [])
        self.home_ccs = {h["cc"] for h in self.homes if h.get("cc")}
        self.g = geo.Gazetteer.get() if geo.available() else None
        self.roots = sorted((x[0] for x in conn.execute("SELECT path FROM roots")), key=len, reverse=True)
        self._dirs: dict[str, dict | None] = {}
        self._abroad: dict[int, frozenset[int]] = {}
        self.camera_key = camera_key
        # Cameras whose clock follows the time zone they're in: a few of their photos at least are on
        # home's time. A camera never set from its factory zone says nothing about where it was.
        seen, home = Counter(), Counter()
        if self.homes:
            for x in conn.execute("SELECT eff_ts, tz_offset, camera, body_serial FROM shots WHERE status='present'"
                                  " AND tz_offset IS NOT NULL AND eff_ts IS NOT NULL"):
                k = camera_key(x["camera"], x["body_serial"])
                seen[k] += 1
                home[k] += x["tz_offset"] in self.home_offsets(x["eff_ts"])
        self.trusted = {k for k in seen if home[k] and home[k] >= min(3, seen[k])}

    def home_offsets(self, ts: float) -> frozenset[int]:
        return frozenset().union(*(geo.utc_offsets(h.get("tz"), ts) for h in self.homes))

    def abroad_offsets(self, ts: float) -> frozenset[int]:
        """Every time zone of the home countries: a clock on none of them is abroad."""
        year = datetime.fromtimestamp(ts, timezone.utc).year
        if year not in self._abroad:
            zones = {z for cc in self.home_ccs for z in (self.g.zones_of(cc) if self.g else ())}
            self._abroad[year] = frozenset().union(self.home_offsets(ts), *(geo.utc_offsets(z, ts) for z in zones))
        return self._abroad[year]

    def folder(self, d: str) -> dict | None:
        """The place a photo's folders name: from the library folder's own name down."""
        if d not in self._dirs:
            root = next((r for r in self.roots if d == r or d.startswith(r.rstrip(os.sep) + os.sep)), None)
            parts = ([os.path.basename(root.rstrip(os.sep))] + d[len(root):].split(os.sep)) if root else d.split(os.sep)
            self._dirs[d] = self.g.folder_place([x for x in parts if x][-4:], self.home_ccs) if self.g else None
        return self._dirs[d]

    def where(self, conn, g: dict, rows) -> dict | None:
        """Where an event was: its photos' place (GPS, the photos around them, or one you set), else
        a guess from its folders' names or its cameras' clocks."""
        if g["main_place"]:
            p = conn.execute("SELECT id, name, admin1, country, cc, lat, lon FROM places WHERE id=?", (g["main_place"],)).fetchone()
            if p:
                srcs = Counter(r["place_src"] for r in rows if r["place_id"] == g["main_place"])
                return {"kind": "town", **dict(p), "src": "manual" if srcs.get("manual") else "gps" if srcs.get("gps") else "inferred"}
        votes, guesses = Counter(), {}
        for r in rows:
            f = self.folder(r["dir"])
            if f:
                k = (f["kind"], f["name"], f["cc"])
                guesses[k] = f
                votes[k] += 1
        if votes:
            k, n = votes.most_common(1)[0]
            if n >= 0.5 * len(rows):
                f = guesses[k]
                return {**{x: f[x] for x in ("kind", "id", "name", "admin1", "country", "cc", "lat", "lon")}, "src": "folder"}
        offs = Counter(r["tz_offset"] for r in rows if r["tz_offset"] is not None
                       and self.camera_key(r["camera"], r["body_serial"]) in self.trusted)
        if offs and rows:
            off, n = offs.most_common(1)[0]
            if n >= 0.5 * sum(offs.values()) and n >= min(3, len(rows)) and off not in self.home_offsets(rows[0]["eff_ts"]):
                return {"kind": "clock", "src": "clock", "offset": off}
        return None

    def away(self, where: dict | None, ts: float) -> dict | None:
        """Why an event is away: {km, home, abroad} (km from the nearest home, when known), or None
        when it isn't. Further than the trip distance from every home, or in another country than
        every home; a clock on another time zone than every home's."""
        if not where:
            return None
        if not self.homes:            # nowhere to call home: any placed event may be part of a trip
            return None if where["src"] == "clock" else {"km": None, "home": None, "abroad": False}
        if where["src"] == "clock":
            h = self.homes[0]
            return {"km": None, "home": h["name"], "abroad": where["offset"] not in self.abroad_offsets(ts),
                    "home_offset": geo.offset_at(h.get("tz"), ts), "homes": len(self.homes)}
        abroad = bool(where.get("cc")) and where["cc"] not in self.home_ccs
        km, near = None, self.homes[0]
        if where.get("lat") is not None and where["kind"] != "country":
            near = min(self.homes, key=lambda h: geo.km_between(where["lat"], where["lon"], h["lat"], h["lon"]))
            km = geo.km_between(where["lat"], where["lon"], near["lat"], near["lon"])
        if not abroad and (km is None or km <= self.km):
            return None
        return {"km": round(km) if km is not None else None, "home": near["name"], "abroad": abroad}

    def place(self, conn, g: dict, rows) -> None:
        """Fill in g's where, away and place (what the catalog keeps and the pages show)."""
        g["where"] = self.where(conn, g, rows)
        g["away"] = self.away(g["where"], g["start"])
        g["place"] = {**g["where"], "away": g["away"]} if g["where"] else None


def run(conn) -> dict:
    """Detect events and trips, keeping ids and names you gave them."""
    rule = Rule(conn)
    found = []
    for ev in detect(conn):
        g = _describe(conn, ev)
        rule.place(conn, g, ev["rows"])
        found.append(g)
    try:
        _name_by_kind(conn, found)
    except Exception:          # naming by look is a nicety: never let it stop the update
        pass
    old_event = {r[0]: r[1] for r in conn.execute("SELECT id, event_id FROM shots WHERE event_id IS NOT NULL")}
    old_trip = {}
    for r in conn.execute("SELECT s.id, e.parent_id FROM shots s JOIN events e ON e.id = s.event_id WHERE e.parent_id IS NOT NULL"):
        old_trip[r[0]] = r[1]
    names = {r["id"]: dict(r) for r in conn.execute("SELECT id, name, user_named, hidden FROM events")}

    # Trips: consecutive events away from home.
    trips: list[list[int]] = []
    for i, ev in enumerate(found):
        if not ev["away"]:
            continue
        if trips and ev["start"] - found[trips[-1][-1]]["end"] <= TRIP_GAP:
            trips[-1].append(i)
        else:
            trips.append([i])
    # Trips you merged are one, with every event between them.
    trips = [list(range(trips[r[0]][0], trips[r[-1]][-1] + 1))
             for r in _joined(trips, joins(conn, "trip"), lambda t: [s for i in t for s in found[i]["ids"]])]
    trip_groups = []
    for t in trips:
        evs = [found[i] for i in t]
        away = [e for e in evs if e["away"]]
        abroad = any(e["away"]["abroad"] for e in away)
        # A day or more, unless abroad: a day trip to another country is a trip too.
        if evs[-1]["end"] - evs[0]["start"] < 20 * 3600 and len(evs) < 2 and not (abroad and evs[0]["n"] >= MIN_EVENT):
            continue
        rows = [{"id": s, "eff_ts": None} for e in evs for s in e["ids"]]
        places = Counter()
        for e in evs:
            for p, k in e["places"]:
                places[p] += k
        counts = sum((e["place_counts"] for e in evs), Counter())
        main = max(away, key=lambda e: (e["away"]["abroad"], e["where"]["src"] != "clock", e["n"]))
        label = _trip_label(conn, counts) if counts else main["where"].get("name")
        name = f"{label} trip" if label else "Trip abroad" if abroad else "Trip"
        trip_groups.append({"members": t, "ids": [r["id"] for r in rows], "n": len(rows), "start": evs[0]["start"],
                            "end": evs[-1]["end"], "cover": max(evs, key=lambda e: e["n"])["cover"],
                            "places": [[p, k] for p, k in places.most_common(6)], "people": [],
                            "place": {**main["where"], "away": main["away"]},
                            "auto_name": f"{name} · {_date_range(evs[0]['start'], evs[-1]['end'])}"})

    event_ids = _stable_ids(conn, found, old_event, "event")
    trip_ids = _stable_ids(conn, trip_groups, old_trip, "trip")
    parent: dict[int, int] = {}
    for tg, tid in zip(trip_groups, trip_ids):
        for i in tg["members"]:
            parent[i] = tid
    keep = set(event_ids) | set(trip_ids)
    # Events you named keep their name while their photos are away (a drive unplugged): those photos
    # stay linked, and the event comes back with its name when they do.
    keep |= {r[0] for r in conn.execute(
        "SELECT DISTINCT e.id FROM events e JOIN shots s ON s.event_id = e.id WHERE e.user_named = 1 AND s.status = 'missing'")}
    old_of = {r[0]: r[1] for r in conn.execute("SELECT id, event_id FROM shots WHERE status = 'present' AND event_id IS NOT NULL")}
    new_of: dict[int, int] = {}
    if keep:
        conn.execute(f"DELETE FROM events WHERE id NOT IN ({','.join(str(int(x)) for x in keep)})")
    else:
        conn.execute("DELETE FROM events")

    def upsert(eid: int, kind: str, g: dict, parent_id: int | None) -> None:
        prev = names.get(eid, {})
        user = bool(prev.get("user_named"))
        conn.execute(
            "INSERT INTO events(id, name, auto_name, user_named, kind, parent_id, start_ts, end_ts, n, cover_shot_id, places, people, hidden,"
            " place, alt_name, folder) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
            " auto_name=excluded.auto_name, user_named=excluded.user_named, kind=excluded.kind, parent_id=excluded.parent_id,"
            " start_ts=excluded.start_ts, end_ts=excluded.end_ts, n=excluded.n, cover_shot_id=excluded.cover_shot_id,"
            " places=excluded.places, people=excluded.people, place=excluded.place, alt_name=excluded.alt_name, folder=excluded.folder",
            (eid, prev["name"] if user else g["auto_name"], g["auto_name"], int(user), kind, parent_id, g["start"], g["end"], g["n"],
             g["cover"], json.dumps(g["places"]), json.dumps(g["people"]), int(prev.get("hidden") or 0),
             json.dumps(g["place"]) if g.get("place") else None, g.get("alt_name"), g.get("folder_label")))

    for i, (g, eid) in enumerate(zip(found, event_ids)):
        upsert(eid, "event", g, parent.get(i))
        for sid in g["ids"]:
            new_of[sid] = eid
    # Only photos whose event changed are written.
    conn.executemany("UPDATE shots SET event_id = NULL WHERE id = ?", [(sid,) for sid in old_of if sid not in new_of])
    conn.executemany("UPDATE shots SET event_id = ? WHERE id = ?", [(eid, sid) for sid, eid in new_of.items() if old_of.get(sid) != eid])
    for g, tid in zip(trip_groups, trip_ids):
        upsert(tid, "trip", g, None)
    # An event's photos with no place of their own are, in all likelihood, where the event was; a
    # folder naming a town places them too, as a guess made again each time.
    if rule.g:
        conn.execute("UPDATE shots SET place_id = NULL, place_src = NULL WHERE place_src = 'folder'")
    for g in found:
        w = g["where"] or {}
        if g["main_place"]:
            pid, src = g["main_place"], "inferred"
        elif rule.g and w.get("src") == "folder" and w.get("id") and geo.store_town(conn, w["id"]):
            pid, src = w["id"], "folder"
        else:
            continue
        for k in range(0, len(g["ids"]), 900):
            chunk = g["ids"][k : k + 900]
            conn.execute(f"UPDATE shots SET place_id=?, place_src=? WHERE place_id IS NULL AND id IN"
                         f" ({','.join('?' * len(chunk))})", (pid, src, *chunk))
    # Who is in each event (and trip), most photographed first.
    people: dict[int, list] = {}
    for r in conn.execute(
        "SELECT s.event_id, f.person_id, COUNT(DISTINCT f.shot_id) AS n FROM faces f JOIN shots s ON s.id = f.shot_id"
        " LEFT JOIN people p ON p.id = f.person_id WHERE s.event_id IS NOT NULL AND f.person_id > 0"
        " AND COALESCE(p.hidden, 0) = 0 GROUP BY s.event_id, f.person_id ORDER BY n DESC"):
        people.setdefault(r[0], []).append([r[1], r[2]])
    trip_people: dict[int, Counter] = {}
    for i, eid in enumerate(event_ids):
        if i in parent:
            for pid, k in people.get(eid, []):
                trip_people.setdefault(parent[i], Counter())[pid] += k
    conn.executemany("UPDATE events SET people=? WHERE id=?",
                     [(json.dumps(people.get(eid, [])[:8]), eid) for eid in event_ids] +
                     [(json.dumps([[p, k] for p, k in trip_people.get(tid, Counter()).most_common(8)]), tid) for tid in trip_ids])
    db.set_setting(conn, RULE_KEY, rule.key)      # commits; the rule these trips were found by
    return {"events": len(found), "trips": len(trip_groups)}


RULE_KEY = "trip_rule"


def stale(conn) -> bool:
    """Trips were found by other homes or another distance than now (changed for the whole Mac,
    maybe from another library), so they're to be found again."""
    return db.get_setting(conn, RULE_KEY) != geo.rule_key(geo.rule(conn))


def set_place(conn, event_id: int, place_id: int | None) -> dict:
    """Your place for an event (or every event of a trip): set on its photos without GPS, where it
    beats any guess and stays through every update. None takes yours away again (the photos' own
    places and guesses come back as events are found again)."""
    e = conn.execute("SELECT kind FROM events WHERE id=?", (event_id,)).fetchone()
    if not e:
        raise ValueError("That event isn't there any more (events are found again on each update).")
    which = "event_id = ?" if e["kind"] == "event" else "event_id IN (SELECT id FROM events WHERE parent_id = ?)"
    rows = conn.execute(f"SELECT id, gps_lat, place_id, place_src FROM shots WHERE status='present' AND {which}", (event_id,)).fetchall()
    ids = [r["id"] for r in rows if r["gps_lat"] is None]
    before = Counter(r["place_id"] for r in rows if r["place_src"] == "manual")
    if place_id is not None and not geo.store_town(conn, place_id):
        raise ValueError("That town isn't in the town list.")
    for k in range(0, len(ids), 900):
        chunk = ids[k : k + 900]
        q = ",".join("?" * len(chunk))
        if place_id is not None:
            conn.execute(f"UPDATE shots SET place_id=?, place_src='manual' WHERE id IN ({q})", (place_id, *chunk))
        else:
            conn.execute(f"UPDATE shots SET place_id=NULL, place_src=NULL WHERE place_src='manual' AND id IN ({q})", chunk)
    conn.commit()
    return {"n": len(ids), "gps": len(rows) - len(ids), "previous": before.most_common(1)[0][0] if before else None}


def rename(conn, event_id: int, name: str | None) -> None:
    """Your name for an event (None/empty: back to the automatic one)."""
    if name and name.strip():
        conn.execute("UPDATE events SET name=?, user_named=1 WHERE id=?", (name.strip()[:120], event_id))
    else:
        conn.execute("UPDATE events SET name=auto_name, user_named=0 WHERE id=?", (event_id,))
    conn.commit()


def neighbours(conn, event_id: int, k: int = 6) -> list[dict]:
    """The events (or trips) just before and after this one, to merge it with."""
    e = conn.execute("SELECT kind, start_ts FROM events WHERE id=?", (event_id,)).fetchone()
    if not e:
        return []
    cols = "id, name, kind, start_ts, end_ts, n, cover_shot_id, hidden"
    before = conn.execute(f"SELECT {cols} FROM events WHERE kind=? AND id!=? AND start_ts <= ? ORDER BY start_ts DESC LIMIT ?",
                          (e["kind"], event_id, e["start_ts"], k)).fetchall()
    after = conn.execute(f"SELECT {cols} FROM events WHERE kind=? AND id!=? AND start_ts > ? ORDER BY start_ts LIMIT ?",
                         (e["kind"], event_id, e["start_ts"], k)).fetchall()
    return [dict(r) for r in reversed(before)] + [dict(r) for r in after]


def merge(conn, keep_id: int, other_id: int) -> dict:
    """Make two events (or two trips), and any between them, one: this one's id stays, and so does
    the name you gave it (an automatic name is made again from all the photos). The merge is kept
    as a pair of their photos, so it holds through every update."""
    a = conn.execute("SELECT * FROM events WHERE id=?", (keep_id,)).fetchone()
    b = conn.execute("SELECT * FROM events WHERE id=?", (other_id,)).fetchone()
    if not a or not b or keep_id == other_id:
        raise ValueError("Choose another event to merge with.")
    if a["kind"] != b["kind"]:
        raise ValueError("An event can be merged with another event, and a trip with another trip.")
    kind = a["kind"]
    lo, hi = min(a["start_ts"], b["start_ts"]), max(a["start_ts"], b["start_ts"])
    gone = [r[0] for r in conn.execute("SELECT id FROM events WHERE kind=? AND id!=? AND start_ts BETWEEN ? AND ?",
                                       (kind, keep_id, lo, hi))]
    # The events whose photos become this one's: for trips, every event between the two as well.
    events_in = [keep_id, *gone] if kind == "event" else [r[0] for r in conn.execute(
        f"SELECT id FROM events WHERE kind='event' AND (parent_id IN ({','.join('?' * (len(gone) + 1))})"
        " OR start_ts BETWEEN ? AND ?)", (keep_id, *gone, lo, max(hi, b["end_ts"], a["end_ts"])))]
    anchor = lambda eid: conn.execute(        # noqa: E731
        "SELECT MIN(s.id) FROM shots s LEFT JOIN events e ON e.id = s.event_id WHERE s.status='present'"
        " AND (s.event_id = ? OR e.parent_id = ?)", (eid, eid)).fetchone()[0]
    mine = anchor(keep_id)
    pairs = [p for p in ({"kind": kind, "a": mine, "b": anchor(g)} for g in gone) if p["a"] and p["b"]]
    q = ",".join("?" * len(gone))
    # What Undo needs: the pairs added, and the events as they were (their names, and which photos or
    # events were theirs), so unmerging gives each its own id and name back.
    undo = {"kind": kind, "keep": keep_id, "joins": pairs,
            "events": [dict(r) for r in conn.execute(f"SELECT * FROM events WHERE id IN ({q})", gone)]}
    if kind == "event":
        undo["photos"] = {str(g): [r[0] for r in conn.execute("SELECT id FROM shots WHERE event_id=?", (g,))] for g in gone}
    else:
        ins = ",".join("?" * len(events_in))
        undo["parents"] = {str(r[0]): r[1] for r in conn.execute(f"SELECT id, parent_id FROM events WHERE id IN ({ins})", events_in)}
    db.set_setting(conn, JOINS_KEY, (db.get_setting(conn, JOINS_KEY, []) or []) + pairs)
    if kind == "event":
        conn.execute(f"UPDATE shots SET event_id=? WHERE event_id IN ({q})", (keep_id, *gone))
    else:
        conn.execute(f"UPDATE events SET parent_id=? WHERE id IN ({','.join('?' * len(events_in))})", (keep_id, *events_in))
    conn.execute(f"DELETE FROM events WHERE id IN ({q})", gone)
    _refresh(conn, keep_id)
    if a["parent_id"]:
        _refresh(conn, a["parent_id"])
    conn.commit()
    return {"id": keep_id, "merged": len(gone), "undo": undo}


def unmerge(conn, undo: dict) -> dict:
    """Undo a merge, from what merge() returned: the pairs it added are taken out (so updates find the
    events apart again), and the events it joined come back with their ids, names and photos. Call
    run() afterwards to find events and trips again for what changed since."""
    try:
        kind = "trip" if undo.get("kind") == "trip" else "event"
        keep = int(undo["keep"])
        pairs = [(int(p["a"]), int(p["b"])) for p in undo.get("joins") or []]
        old = [dict(e) for e in undo.get("events") or [] if isinstance(e, dict) and int(e.get("id") or 0)]
    except (KeyError, TypeError, ValueError):
        raise ValueError("That merge can't be undone from here.") from None
    db.write_lock(conn)
    left = list(pairs)
    kept = []
    for j in db.get_setting(conn, JOINS_KEY, []) or []:
        pair = (j.get("a"), j.get("b")) if isinstance(j, dict) and j.get("kind") == kind else None
        if pair in left:
            left.remove(pair)                 # one of this merge's pairs: taken out (once each)
        else:
            kept.append(j)
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (JOINS_KEY, json.dumps(kept)))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    for e in old:
        row = {k: v for k, v in e.items() if k in cols}
        conn.execute(f"INSERT OR IGNORE INTO events({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    back = [e["id"] for e in old]
    if kind == "event":
        for eid, ids in (undo.get("photos") or {}).items():
            ids = [int(i) for i in ids]
            for k in range(0, len(ids), 900):
                chunk = ids[k : k + 900]
                conn.execute(f"UPDATE shots SET event_id=? WHERE event_id=? AND id IN ({','.join('?' * len(chunk))})",
                             (int(eid), keep, *chunk))
    else:
        for eid, parent in (undo.get("parents") or {}).items():
            conn.execute("UPDATE events SET parent_id=? WHERE id=? AND parent_id=?", (parent, int(eid), keep))
    for eid in [keep, *back]:
        if conn.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
            _refresh(conn, eid)
    conn.commit()
    return {"id": keep, "restored": len(back)}


def _refresh(conn, event_id: int) -> None:
    """Dates, count, cover, places and automatic name of an event or trip after a merge."""
    e = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    which = "s.event_id = ?" if e["kind"] == "event" else "s.event_id IN (SELECT id FROM events WHERE parent_id = ?)"
    rows = conn.execute(
        f"SELECT {_COLS} FROM shots s LEFT JOIN metrics m ON m.shot_id = s.id"
        f" WHERE s.status='present' AND s.eff_ts IS NOT NULL AND {which} ORDER BY s.eff_ts, s.id", (event_id,)).fetchall()
    if not rows:
        return
    g = _describe(conn, {"rows": rows})
    Rule(conn).place(conn, g, rows)
    if e["kind"] == "event":
        try:
            _name_by_kind(conn, [g])
        except Exception:      # naming by look is a nicety
            pass
    else:
        label = _trip_label(conn, g["place_counts"]) if g["place_counts"] else (g["where"] or {}).get("name")
        g["auto_name"] = f"{label} trip · {g['when']}" if label else f"Trip · {g['when']}"
    conn.execute("UPDATE events SET name=?, auto_name=?, start_ts=?, end_ts=?, n=?, cover_shot_id=?, places=?, place=?, alt_name=?, folder=?"
                 " WHERE id=?",
                 (e["name"] if e["user_named"] else g["auto_name"], g["auto_name"], g["start"], g["end"], g["n"], g["cover"],
                  json.dumps(g["places"]), json.dumps(g["place"]) if g["place"] else None,
                  g.get("alt_name") if e["kind"] == "event" else None, g.get("folder_label") if e["kind"] == "event" else None, event_id))


def listing(conn, kind: str | None = None, year: int | None = None, hidden: bool = False) -> list[dict]:
    """Events and trips, newest first; with hidden, only the ones you hid."""
    q = f"SELECT * FROM events WHERE hidden = {int(hidden)}"
    args: list = []
    if kind:
        q += " AND kind = ?"
        args.append(kind)
    if year:
        a = datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()
        b = datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp()
        q += " AND start_ts >= ? AND start_ts < ?"
        args += [a, b]
    out = []
    for r in conn.execute(q + " ORDER BY start_ts DESC", args):
        d = dict(r)
        d["places"] = json.loads(d["places"] or "[]")
        d["people"] = json.loads(d["people"] or "[]")
        d["place"] = json.loads(d["place"]) if d.get("place") else None
        out.append(d)
    return out
