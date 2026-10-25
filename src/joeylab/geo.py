"""Places, offline: GPS coordinates to the nearest town, with no location ever leaving the Mac.

The town list is GeoNames' cities5000 (every place with 5,000+ people, CC BY 4.0), downloaded once
(about 5 MB) into ~/.joeylab/models/geonames. Photos without GPS borrow the place of a photo
with GPS taken within a few hours (often a phone shot at the same event), marked "inferred".
Folders that name a town, region or country ("2023-Cyprus", "Phuket Jan 2026") place the events in
them, as a guess (`Gazetteer.folder_place`).

Your homes and how far from them a trip starts are about you, not a library, so they're kept for
the whole Mac in settings.json (`homes`, `trip_km`). events.py decides trips from them.
"""

from __future__ import annotations

import bisect
import csv
import difflib
import io
import json
import math
import os
import re
import unicodedata
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

import numpy as np

from .config import MODELS_DIR

GEO_DIR = MODELS_DIR / "geonames"
BASE = "https://download.geonames.org/export/dump/"
FILES = {"cities5000.zip": "cities5000.txt", "admin1CodesASCII.txt": None, "countryInfo.txt": None}
MAX_KM = 60             # beyond this from any town, say only the country/region we can't name
INFER_SECONDS = 3 * 3600
EARTH_KM = 6371.0
TRIP_KM = 100           # events further than this from every home are away (or in another country)
MAX_HOMES = 10


def available() -> bool:
    return (GEO_DIR / "cities5000.txt").exists()


def ensure(log: Callable[[str], None] = print) -> bool:
    if available():
        return True
    if os.environ.get("JOEYLAB_OFFLINE"):
        return False
    GEO_DIR.mkdir(parents=True, exist_ok=True)
    log("Downloading the offline list of towns for places (one time, about 5 MB)")
    try:
        for name, inner in FILES.items():
            dest = GEO_DIR / (inner or name)
            if dest.exists():
                continue
            with urllib.request.urlopen(BASE + name, timeout=120) as r:
                data = r.read()
            if inner:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    data = z.read(inner)
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.rename(dest)
    except (OSError, zipfile.BadZipFile) as e:
        log(f"Couldn't download the town list ({e}); places will appear after the next update")
        return False
    return available()


def fold(s: str) -> str:
    """Lower case, accents and punctuation gone: "Bāli" and "bali", "Dibba Al-Hisn" and "dibba al hisn"."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[-‐–'’.,()]+", " ", s).split())


# ---------------------------------------------------------------- folder names that say where
# Words that are also the names of towns (Nice, Bath, Split, Reading, Wedding in Berlin...) but in a
# folder name almost always mean something else: never read as a place. With the possessive rule
# ("Victoria's party") and the size rule below, a folder only places its event when it clearly names
# somewhere.
_COMMON = set("""
acre airport alliance along alpine altar amble anchorage annex anthem apex aroma ascension atlantic august
aurora bacon badger bake baker band bank bare barking bath battle battlefield beach beacon bear bell bend bent
best bhandara birthday bright brick bride brush buffalo butler butterfly camp canyon carnival carpenter carver
cascade casino cathedral cave celebration center central centre champion chamber change christmas citadel
citrus clay clover cocoa coin college come commerce commonwealth concert concord condor corona corral
countryside cove crane crest crystal cypress date deal defiance delta derby diwali dome downtown drama eagle
east eastern easter economy emerald engineer enter enterprise eureka evergreen falcon family fate federal
festival fleet flint flora forest fortune fountain freedom friendly friends fully garden gateway give glad glen
globe golden gondola grace grand grange grave gray green grove hamlet happy hard harmony harvest hazard heath
hermitage highland hillside holi holiday holidays holly home homeland homestead hope horn humble hurricane
imperial independence isle jade jubilee justice karma kids king kissing lakeland lakeside landing laurel
lavender lazy leader legacy lens liberal liberation liberty lively long love lucky magnolia mahogany male manage
mango manor marathon march marina maritime mascot media meridian mesa metropolis midway mile mill mine mission
mobile mocha mole monster monument moss most moth mound much naval nice nine noble none normal north northeast
northern northwest oasis office officer opera opportunity oral orange oriental overland pace pacific page pale
panorama paper papa paradise paramount parkway parole party pearl peel petal plantation plateau plum plunge
police pool post price pride progress promontory prospect prosper punch race rail rain reading real reception
republic reserve reservoir retreat reunion rich ridge rifle riverside rodeo rouge rugby rustic sake sale salon
salt same sandwich sandy savage save school seaside sedan send shaft side silly smile snowflake song south
sparks speedway split spring star sterling stone store strand strawberry street success summit sunrise sunset
sunshine superior surprise talent tank temple terrace time tire toss trail triangle trim trinity tune twist
union unity university uptown valentine valley varsity vice victory vista wake walker walnut warden wedding
welcome west western wick wilderness winter woodland worth yoga young
""".split())

# Cameras, phones and the folders they make ("Fujifilm X-T5 Backups", "202306 Fuji Backup", "100_FUJI",
# "Canon EOS", "Nikon D750"): brand names that are also towns (Fuji in Japan, Sony, Canon in France...).
# A folder named after the camera says nothing about where its photos were taken.
_GEAR = set("""
apple canon casio dji fuji fujifilm fujinon garmin google gopro hasselblad huawei insta360 iphone kodak konica
leica lumix minolta nikon nokia olympus oneplus oppo panasonic pentax pixel polaroid ricoh samsung sigma sony
tamron vivo xiaomi yashica zeiss
""".split())
# First names that are also towns: "Anna", "Leo", "Charlotte" name a person far more often. A city
# of a million people or more ("Sydney", "Sofia") still counts.
_NAMES = set("""
adam adrian albert alex allen alma alvin amelia amos anderson andreas angus anita anna anthony anton antony
arnold axel bailey barry bella ben benson beverly blaine blair brad bradley brandon brent brooke bruce
bryan byron camilla carney cary casey casper chad charlotte chester clark claudia claudio clayton clive clyde
craig cristina darby darling david devon dexter dion donna dori douglas dudley duncan edmond eliot elizabeth
elsa elwood emma emmett erwin eugene everett florence franklin frederick gary george gilbert gordon graham
granville griffin hamilton harper harvey herve holden homer howard irving isla jasper jeannette jerome jones
jordan joshua kamala kari kathleen kent kenton kimberly kyle lana lata lawrence leigh leo leon leslie lily linda
lindsay lorraine lowell lucas luna lyndon lynn mack mamta marco marion marshall martin marvin mary mason maya
mayo meena mila miki mitchell moore morton murray nancy nanda nandi nelson nigel nina nora norman oliver olivia
paola parker patrick pedro perry pierre pratt priya raja raman ramon ramsey randolph rani rees rekha roman
rosario rupert russell sam sanford santa sara scott seymour shankar shannon sharan sharon sherman shirley
sidney sofia spencer stanley stella stuart sundar sylvester teresa theodore tracy tucker turner tyler tyrone
vernon victoria vincent viola vladimir warren wayne webster wendell wilson winston wright xenia
""".split())
# Particles inside a multi-word name ("Rio de Janeiro", "Stratford upon Avon").
_PARTICLES = set("de di da do du del della der den la le les el al of on upon sur am an im y e".split())
# Folder words that make a smaller town in the same folder name likely to be a destination.
_TRAVEL = set("trip trips travel travels holiday holidays vacation vacances tour visit journey getaway honeymoon "
              "weekend roadtrip voyage expedition trek".split())
_YEAR = re.compile(r"(?<!\d)(19|20)\d\d(?!\d)")
_TOKEN = re.compile(r"[^\W\d_]+", re.U)
_COUNTRY_ALIASES = {"usa": "US", "america": "US", "united states of america": "US", "uk": "GB", "britain": "GB",
                    "great britain": "GB", "uae": "AE", "emirates": "AE", "holland": "NL", "netherlands": "NL",
                    "czech republic": "CZ", "burma": "MM", "korea": "KR", "turkiye": "TR", "vatican city": "VA"}
# Names a city went by that folders still use, and what the town list calls it now.
_EXONYMS = {"bombay": ("Mumbai", "IN"), "calcutta": ("Kolkata", "IN"), "madras": ("Chennai", "IN"),
            "bangalore": ("Bengaluru", "IN"), "pondicherry": ("Puducherry", "IN"), "benares": ("Varanasi", "IN"),
            "poona": ("Pune", "IN"), "cochin": ("Kochi", "IN"), "trivandrum": ("Thiruvananthapuram", "IN"),
            "simla": ("Shimla", "IN"), "baroda": ("Vadodara", "IN"), "gurgaon": ("Gurugram", "IN"),
            "peking": ("Beijing", "CN"), "saigon": ("Ho Chi Minh City", "VN"), "rangoon": ("Yangon", "MM")}
_REGION_SUFFIX = re.compile(r"\s+(province|region|governorate|prefecture|district|county|state|oblast|department)$")
SMALL_TOWN = 15000       # smaller towns need a reason (a year or "trip" in the folder name) to count


class Gazetteer:
    _inst: "Gazetteer | None" = None

    def __init__(self):
        countries, admin1 = {}, {}
        for line in (GEO_DIR / "countryInfo.txt").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                p = line.split("\t")
                countries[p[0]] = p[4]
        for line in (GEO_DIR / "admin1CodesASCII.txt").read_text(encoding="utf-8").splitlines():
            p = line.split("\t")
            if len(p) > 1:
                admin1[p[0]] = p[1]
        self.countries, self.admin1 = countries, admin1
        rows = []
        alternates: dict[int, str] = {}
        with open(GEO_DIR / "cities5000.txt", encoding="utf-8") as fh:
            for p in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
                if len(p) < 15:
                    continue
                cc = p[8]
                pop = int(p[14] or 0)
                # id, name, region, country, cc, lat, lon, people, ASCII name, time zone, region code
                rows.append((int(p[0]), p[1], admin1.get(f"{cc}.{p[10]}"), countries.get(cc, cc), cc,
                             float(p[4]), float(p[5]), pop, p[2], p[17] if len(p) > 17 else "", f"{cc}.{p[10]}"))
                if pop >= 100000 and p[3]:
                    alternates[len(rows) - 1] = p[3]          # "Bombay", "Lisboa", "Firenze": for cities only
        self.rows = rows
        self.alternates = alternates
        lat = np.radians([r[5] for r in rows])
        lon = np.radians([r[6] for r in rows])
        self.xyz = np.stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)], axis=1).astype(np.float32)
        self.pop = np.array([r[7] for r in rows], dtype=np.float32)
        self.cells: dict[tuple, list] = defaultdict(list)
        for i, r in enumerate(rows):
            self.cells[(math.floor(r[5]), math.floor(r[6]))].append(i)
        self._ids: dict[int, int] | None = None
        self._keys: tuple[list[str], list[int]] | None = None
        self._folder: dict[str, list[tuple]] | None = None
        self._zones: dict[str, set[str]] | None = None

    @classmethod
    def get(cls) -> "Gazetteer":
        if cls._inst is None:
            cls._inst = Gazetteer()
        return cls._inst

    def nearest(self, lat: float, lon: float) -> tuple[tuple, float] | None:
        """The town to name a spot by: the closest, but a much bigger town only slightly further
        wins (a photo at a beach hotel is "Biarritz", not the hamlet next door)."""
        cand = []
        la, lo = math.floor(lat), math.floor(lon)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                cand += self.cells.get((la + dy, ((lo + dx + 180) % 360) - 180), [])
        if not cand:
            return None
        rl, ro = math.radians(lat), math.radians(lon)
        v = np.array([math.cos(rl) * math.cos(ro), math.cos(rl) * math.sin(ro), math.sin(rl)], dtype=np.float32)
        idx = np.array(cand)
        km = EARTH_KM * np.arccos(np.clip(self.xyz[idx] @ v, -1, 1))
        score = km - 4.0 * np.log10(np.maximum(self.pop[idx], 1000))     # ~12 km per 10x population
        k = int(np.argmin(score))
        if km[k] > MAX_KM:
            return None
        return self.rows[idx[k]], float(km[k])

    # ------------------------------------------------------------ by id, by name
    def row(self, place_id: int) -> tuple | None:
        if self._ids is None:
            self._ids = {r[0]: i for i, r in enumerate(self.rows)}
        i = self._ids.get(int(place_id))
        return self.rows[i] if i is not None else None

    @staticmethod
    def town(r: tuple) -> dict:
        return {"id": r[0], "name": r[1], "admin1": r[2], "country": r[3], "cc": r[4], "lat": r[5], "lon": r[6],
                "people": r[7], "tz": r[9] or None}

    def search(self, q: str, limit: int = 10) -> list[dict]:
        """Towns whose name (or any word of it, or a big city's other name) starts with q, the exact
        name first and then the biggest."""
        key = fold(q)
        if len(key) < 2:
            return []
        if self._keys is None:
            pairs = []
            for i, r in enumerate(self.rows):
                own = {fold(r[8]), fold(r[1])}
                other = {fold(a) for a in self.alternates.get(i, "").split(",") if a.isascii() and len(a) > 2} - own
                # how good a match on it is: its own name 3, another name 2, a later word of either 1 or 0
                for names, q in ((own, 3), (other, 2)):
                    for name in names:
                        words = name.split()
                        pairs += [(" ".join(words[k:]), i, q if k == 0 else q - 2) for k in range(len(words))]
            pairs.sort()
            self._keys = ([p[0] for p in pairs], [(p[1], p[2]) for p in pairs])
        keys, idx = self._keys
        best: dict[int, tuple] = {}
        for j in range(bisect.bisect_left(keys, key), len(keys)):
            if not keys[j].startswith(key):
                break
            i, q = idx[j]
            # Its own name exactly; a big city's other name exactly ("Lisboa" is Lisbon); a word of its
            # own name ("New Delhi"); its own name starting so; anything else. Then the biggest.
            word = keys[j] == key or keys[j].startswith(key + " ")
            pop = self.rows[i][7]
            tier = (5 if q == 3 and keys[j] == key else 4 if q == 2 and keys[j] == key and pop >= 500000
                    else 3 if q in (1, 3) and word else 2 if q in (1, 3) else 1)
            rank = (tier, pop)
            if rank > best.get(i, ()):
                best[i] = rank
        top = sorted(best, key=lambda i: best[i], reverse=True)[:limit]
        return [self.town(self.rows[i]) for i in top]

    # ------------------------------------------------------------ time zones
    def zones_of(self, cc: str) -> set[str]:
        """The time zones a country's towns keep."""
        if self._zones is None:
            self._zones = defaultdict(set)
            for r in self.rows:
                if r[9]:
                    self._zones[r[4]].add(r[9])
        return self._zones.get(cc, set())

    # ------------------------------------------------------------ folder names
    def _folder_index(self) -> dict[str, list[tuple]]:
        """Every name a folder might use, each with what it could be: ("country", cc),
        ("region", code) or ("town", row index)."""
        if self._folder is not None:
            return self._folder
        index: dict[str, list[tuple]] = defaultdict(list)
        region_pop: dict[str, int] = defaultdict(int)
        region_top: dict[str, int] = {}
        top: dict[str, int] = {}
        for i, r in enumerate(self.rows):
            names = {fold(r[8]), fold(r[1])}
            if r[4] == "TH" and r[8].startswith("Ban "):
                names.add(fold(r[8][4:]))                       # "Ban Ao Nang" (Ao Nang village)
            # Another spelling of a big city's name ("Darjeeling", "Marrakech", "Lisboa"), when close to
            # its own: other languages' names for it are too often English words ("Lion" is Lyon).
            own = fold(r[8])
            for a in self.alternates.get(i, "").split(","):
                k = fold(a)
                if (a.isascii() and len(k) >= 6 and k[0] == own[:1] and abs(len(k) - len(own)) <= 2
                        and difflib.SequenceMatcher(None, k, own).ratio() >= 0.8):
                    names.add(k)
            for name in names:
                if len(name) >= 3:
                    index[name].append(("town", i))
            region_pop[r[10]] += r[7]
            if r[10] not in region_top or r[7] > self.rows[region_top[r[10]]][7]:
                region_top[r[10]] = i
            if r[4] not in top or r[7] > self.rows[top[r[4]]][7]:
                top[r[4]] = i
        for old, (name, cc) in _EXONYMS.items():
            for k, i in index.get(fold(name), []):
                if self.rows[i][4] == cc:
                    index[old].append(("town", i))
                    break
        for line in (GEO_DIR / "admin1CodesASCII.txt").read_text(encoding="utf-8").splitlines():
            p = line.split("\t")
            if len(p) > 2 and p[0] in region_top:
                for name in {fold(p[1]), fold(p[2])}:
                    name = _REGION_SUFFIX.sub("", name)
                    # "Jammu and Kashmir", "Andaman and Nicobar": either half names it too
                    for n in [name] + (name.split(" and ") if " and " in name else []):
                        if len(n) >= 3:
                            index[n].append(("region", p[0]))
        names = {fold(n): cc for cc, n in self.countries.items()}
        names.update({k: v for k, v in _COUNTRY_ALIASES.items() if v in self.countries})
        for name, cc in names.items():
            if cc in top:
                index[name].insert(0, ("country", cc))
        self._region_pop, self._region_top, self._country_top = region_pop, region_top, top
        self._folder = dict(index)
        return self._folder

    def _what(self, kind: str, ref) -> dict:
        if kind == "town":
            r = self.rows[ref]
            return {"kind": "town", "id": r[0], "name": r[1], "admin1": r[2], "country": r[3], "cc": r[4],
                    "lat": r[5], "lon": r[6], "people": r[7]}
        if kind == "region":
            r = self.rows[self._region_top[ref]]
            return {"kind": "region", "id": None, "name": self.admin1.get(ref, ref), "admin1": None, "country": r[3],
                    "cc": r[4], "lat": r[5], "lon": r[6], "people": self._region_pop[ref]}
        r = self.rows[self._country_top[ref]]
        return {"kind": "country", "id": None, "name": self.countries[ref], "admin1": None, "country": self.countries[ref],
                "cc": ref, "lat": r[5], "lon": r[6], "people": None}

    def _pick(self, cands: list[tuple], homes_cc: set[str]) -> list[dict]:
        """What a name most likely means, best first: a country (unless a region or city of a
        million people at home has the name: "Georgia" from Atlanta is the state); else a town over
        the region of the same name it's in ("Phuket", "Dubai"); else the biggest, unless one in a
        home country is at least a tenth its size ("Punjab" from Delhi is in India)."""
        in_region = {self.rows[ref][10] for k, ref in cands if k == "town"}
        out = sorted([self._what(k, ref) for k, ref in cands if k == "town" or (k == "region" and ref not in in_region)],
                     key=lambda g: -(g["people"] or 0))
        countries = [self._what(k, ref) for k, ref in cands if k == "country"]
        if countries:
            home = [g for g in out if g["cc"] in homes_cc and (g["people"] or 0) >= 1000000]
            return home[:1] + countries[:1]
        if out and homes_cc:
            near = [g for g in out if g["cc"] in homes_cc and g["kind"] == out[0]["kind"]
                    and (g["people"] or 0) * 10 >= (out[0]["people"] or 0)]
            if near:
                out = near + [g for g in out if g not in near]
        return out

    def folder_place(self, parts: list[str], homes_cc: set[str] = frozenset()) -> dict | None:
        """The place a folder path names, if it clearly names one: parts are its folder names,
        outermost first. A capitalised word (or words) that is a town, region or country, at least 4
        letters, not a common word, a first name or someone's ("Victoria's"); a town of under 15,000
        people only with a year or "trip" beside it. The deepest folder that names a place wins, and a
        town beats the country it's in ("Thailand 2026/Phuket" is Phuket)."""
        index = self._folder_index()
        found: list[dict] = []
        for part in reversed(parts):
            words = []
            for m in _TOKEN.finditer(part):
                after = part[m.end():m.end() + 2].lower()
                words.append((m.group(), after in ("'s", "’s") or (m.group().endswith("s") and after[:1] in ("'", "’"))))
            for k in range(len(words) - 3, -1, -1):       # "Savannah and Rohan's wedding": both are people
                if words[k + 1][0].lower() == "and" and words[k + 2][1]:
                    words[k] = (words[k][0], True)
            context = bool(_YEAR.search(part)) or any(fold(w) in _TRAVEL for w, _ in words)
            i = 0
            while i < len(words):
                hit = None
                for n in (4, 3, 2, 1):
                    gram = words[i:i + n]
                    if len(gram) < n or not self._plausible(gram):
                        continue
                    key = " ".join(fold(w) for w, _ in gram)
                    if key not in index:
                        continue
                    for g in self._pick(index[key], homes_cc):
                        if self._counts(g, gram, context):
                            hit = (g, n)
                            break
                    if hit:
                        break
                if hit:
                    found.append(hit[0])
                    i += hit[1]
                else:
                    i += 1
            if any(g["kind"] != "country" for g in found):
                break               # the deepest folder naming a town or region says where
        if not found:
            return None
        countries = {g["cc"] for g in found if g["kind"] == "country"}
        for g in found:
            if g["kind"] != "country" and (not countries or g["cc"] in countries):
                return g
        return next((g for g in found if g["kind"] == "country"), found[0])

    @staticmethod
    def _plausible(gram: list[tuple[str, bool]]) -> bool:
        words = [w for w, _ in gram]
        if gram[-1][1] or any(own for _, own in gram):       # "Victoria's party", "Parents' house"
            return False
        if not (words[0][0].isupper() and words[-1][0].isupper()):
            return False
        return all(w[0].isupper() or w.lower() in _PARTICLES for w in words)

    @staticmethod
    def _counts(g: dict, gram: list[tuple[str, bool]], context: bool) -> bool:
        words = [fold(w) for w, _ in gram]
        letters = sum(len(w) for w in words)
        if any(w in _GEAR for w in words):
            return False                                       # the camera, not the place
        if g["kind"] == "country":
            return letters >= 4 or (len(words) == 1 and gram[0][0].isupper() and letters >= 2)    # "UK", "USA"
        if len(words) == 1:
            w = words[0]
            if w in _COMMON or letters < (3 if g["kind"] == "region" else 4):
                return False
            if w in _NAMES and (g["kind"] != "town" or (g["people"] or 0) < 1000000):
                return False
        elif all(w in _COMMON or w in _PARTICLES for w in words) and (g["people"] or 0) < 100000:
            return False                                       # "Garden City", "Spring Valley"
        if g["kind"] == "town" and len(words) == 1 and (g["people"] or 0) < SMALL_TOWN and not context:
            return False
        return True


def _store_place(conn, row: tuple) -> int:
    conn.execute("INSERT OR IGNORE INTO places(id, name, admin1, country, cc, lat, lon) VALUES (?,?,?,?,?,?,?)", row[:7])
    return row[0]


def store_town(conn, place_id: int) -> bool:
    """Put a town from the town list into this library's places (to set it on photos)."""
    if conn.execute("SELECT 1 FROM places WHERE id=?", (place_id,)).fetchone():
        return True
    if not available():
        return False
    r = Gazetteer.get().row(place_id)
    if r:
        _store_place(conn, r)
    return bool(r)


def assign(conn, log: Callable[[str], None] = print, download: bool = True) -> dict:
    """Name the place of every photo with GPS, then infer it for photos taken nearby in time.
    A place you set yourself is kept."""
    if not (available() or (download and ensure(log))):
        return {"gps": 0, "inferred": 0, "available": False}
    g = Gazetteer.get()
    gps = conn.execute("SELECT id, gps_lat, gps_lon, eff_ts FROM shots WHERE status='present' AND gps_lat IS NOT NULL").fetchall()
    n_gps = 0
    timeline: list[tuple[float, int]] = []
    updates = []
    for r in gps:
        hit = g.nearest(r["gps_lat"], r["gps_lon"])
        pid = _store_place(conn, hit[0]) if hit else None
        updates.append((pid, "gps" if pid else None, r["id"]))
        if pid:
            n_gps += 1
            if r["eff_ts"] is not None:
                timeline.append((r["eff_ts"], pid))
    conn.executemany("UPDATE shots SET place_id=?, place_src=? WHERE id=?", updates)
    timeline.sort()
    times = [t for t, _ in timeline]
    inferred = []
    for r in conn.execute("SELECT id, eff_ts FROM shots WHERE status='present' AND gps_lat IS NULL AND eff_ts IS NOT NULL"
                          " AND COALESCE(time_src, 'exif') = 'exif' AND COALESCE(place_src, '') != 'manual'"):
        pid = None
        if times:
            i = bisect.bisect_left(times, r["eff_ts"])
            near = [timeline[j] for j in (i - 1, i) if 0 <= j < len(timeline) and abs(timeline[j][0] - r["eff_ts"]) <= INFER_SECONDS]
            places = {p for _, p in near}
            if len(places) == 1:
                pid = places.pop()
        inferred.append((pid, "inferred" if pid else None, r["id"]))
    conn.executemany("UPDATE shots SET place_id=?, place_src=? WHERE id=?", inferred)
    conn.commit()
    return {"gps": n_gps, "inferred": sum(1 for p, _, _ in inferred if p), "available": True}


def summary(conn) -> list[dict]:
    """Countries and places with photo counts, for the Places page."""
    rows = conn.execute(
        "SELECT p.id, p.name, p.admin1, p.country, p.cc, p.lat, p.lon, COUNT(*) AS n,"
        " SUM(s.place_src = 'gps') AS gps, SUM(s.place_src = 'folder') AS folder, SUM(s.place_src = 'manual') AS manual,"
        " MIN(s.eff_ts) AS first, MAX(s.eff_ts) AS last"
        " FROM shots s JOIN places p ON p.id = s.place_id WHERE s.status='present' GROUP BY p.id ORDER BY n DESC").fetchall()
    return [dict(r) for r in rows]


def points(conn, limit: int = 20000) -> list[list]:
    """GPS points for the map: [lat, lon, shot id], thinned to one per ~100 m cell."""
    seen = set()
    out = []
    for r in conn.execute("SELECT id, gps_lat, gps_lon FROM shots WHERE status='present' AND gps_lat IS NOT NULL"
                          " ORDER BY eff_ts"):
        key = (round(r[1], 3), round(r[2], 3))
        if key in seen:
            continue
        seen.add(key)
        out.append([round(r[1], 5), round(r[2], 5), r[0]])
        if len(out) >= limit:
            break
    return out


def place_label(row) -> str:
    if not row:
        return ""
    bits = [row["name"]]
    if row["country"]:
        bits.append(row["country"])
    return ", ".join(bits)


def home_place(conn) -> int | None:
    """Home: the place photographed across the most different months (at least 3). A place with
    many photos from one week is a trip, not home. None when no place qualifies (common when
    only travel photos carry GPS): then every multi-day run of placed photos can be a trip.
    Places guessed from folder names don't count."""
    r = conn.execute("SELECT place_id, COUNT(DISTINCT substr(capture_str, 1, 7)) AS months FROM shots"
                     " WHERE status='present' AND place_id IS NOT NULL AND capture_str IS NOT NULL"
                     " AND COALESCE(place_src, '') != 'folder'"
                     " GROUP BY place_id ORDER BY months DESC, COUNT(*) DESC LIMIT 1").fetchone()
    return r[0] if r and r[1] >= 3 else None


_COORDS: dict[int, tuple[float, float]] = {}


def _coords(conn, pid: int):
    if pid not in _COORDS:
        r = conn.execute("SELECT lat, lon FROM places WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        _COORDS[pid] = (r[0], r[1])     # GeoNames ids are global, so this cache is safe across libraries
    return _COORDS[pid]


def km_between(la1: float, lo1: float, la2: float, lo2: float) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (la1, lo1, la2, lo2))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def distance_km(conn, a: int | None, b: int | None) -> float | None:
    if not a or not b:
        return None
    pa, pb = _coords(conn, a), _coords(conn, b)
    if not pa or not pb:
        return None
    return km_between(pa[0], pa[1], pb[0], pb[1])


# ---------------------------------------------------------------- homes (for the whole Mac)
def homes() -> list[dict]:
    """The homes you set, each {id, name, admin1, country, cc, lat, lon, tz}; [] if you set none."""
    from . import prefs

    out = []
    for h in prefs.load().get("homes") or []:
        if isinstance(h, dict) and isinstance(h.get("lat"), (int, float)) and isinstance(h.get("lon"), (int, float)) and h.get("name"):
            out.append(h)
    return out[:MAX_HOMES]


def trip_km() -> int:
    """How far from every home an event has to be to count as away."""
    from . import prefs

    v = prefs.load().get("trip_km")
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 20000 else TRIP_KM


def map_tiles(conn) -> bool:
    """Whether the Places map loads OpenStreetMap's street map (for the whole Mac; a library where it
    was turned on before this was app-wide keeps it on until it's changed)."""
    from . import db, prefs

    v = prefs.load().get("map_tiles")
    return bool(v) if isinstance(v, bool) else bool(db.get_setting(conn, "map_tiles", False))


def set_map_tiles(on: bool) -> None:
    from . import prefs

    prefs.update(map_tiles=bool(on))


def home_record(conn, place_id: int) -> dict | None:
    """A town as a home: from this library's places or the town list, with its time zone when known."""
    r = conn.execute("SELECT id, name, admin1, country, cc, lat, lon FROM places WHERE id=?", (place_id,)).fetchone()
    g = Gazetteer.get().row(place_id) if available() else None
    if r:
        return {**dict(r), "tz": (g[9] or None) if g else None}
    return {k: v for k, v in Gazetteer.town(g).items() if k != "people"} if g else None


def set_homes(conn, place_ids: list[int]) -> list[dict]:
    """Make these towns your homes (in this order), for every library."""
    from . import prefs

    out = []
    for pid in place_ids[:MAX_HOMES]:
        h = home_record(conn, pid)
        if h is None:
            raise ValueError("That town isn't in the town list.")
        if all(x["id"] != h["id"] for x in out):
            out.append(h)
    prefs.update(homes=out)
    return out


def set_trip_km(km: int) -> int:
    from . import prefs

    prefs.update(trip_km=int(km))
    return trip_km()


def rule(conn) -> dict:
    """What decides trips now: your homes, or JoeyLab's guess when you set none, and the distance."""
    mine = homes()
    guess = None
    if not mine:
        pid = home_place(conn)
        guess = home_record(conn, pid) if pid else None
    return {"homes": mine, "guess": guess, "km": trip_km()}


def rule_key(r: dict) -> str:
    """The rule as a string, kept with a library's trips: when it differs, they're found again."""
    hs = r["homes"] or ([r["guess"]] if r["guess"] else [])
    return json.dumps({"homes": [[h["id"], round(h["lat"], 4), round(h["lon"], 4)] for h in hs], "km": r["km"], "v": 1})


def offset_at(tz: str | None, ts: float) -> int | None:
    """Minutes east of UTC in this time zone at the time ts (a camera's wall-clock time)."""
    if not tz:
        return None
    try:
        from zoneinfo import ZoneInfo

        d = datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=ZoneInfo(tz))
        return int(d.utcoffset().total_seconds() // 60)
    except Exception:
        return None


_OFFSETS: dict[tuple[str, int], frozenset[int]] = {}


def utc_offsets(tz: str | None, ts: float) -> frozenset[int]:
    """Minutes east of UTC that clocks in this time zone show in the year of ts, in winter and in
    summer (a camera whose summer time wasn't switched still reads as home)."""
    if not tz:
        return frozenset()
    year = datetime.fromtimestamp(ts, timezone.utc).year
    key = (tz, year)
    if key not in _OFFSETS:
        try:
            from zoneinfo import ZoneInfo

            z = ZoneInfo(tz)
            _OFFSETS[key] = frozenset(int(datetime(year, m, 15, 12, tzinfo=z).utcoffset().total_seconds() // 60) for m in (1, 7))
        except Exception:
            _OFFSETS[key] = frozenset()
    return _OFFSETS[key]
