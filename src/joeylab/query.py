"""One search box for everything.

Field queries, like Lightroom's but over JoeyLab's own knowledge:
    person:"Alex" place:Lisbon is:pick rating:>3 iso:>800 q:>70 date:2024-06 #climbing "first dance"
and plain language, read locally (no network):
    "Mom in Cornwall in 2023 that I haven't picked"  ->  person Mom · place Cornwall · 2023 · undecided
Whatever isn't a filter is what the photo should look like, for the image search.
"""

from __future__ import annotations

import re
import shlex
from datetime import date

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December"]
MONTH_WORD = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?"
              r"|nov(?:ember)?|dec(?:ember)?)")
# Camera makers as people say them -> what the camera names in a catalog contain.
BRANDS = {"fuji": "fujifilm", "fujifilm": "fujifilm", "canon": "canon", "nikon": "nikon", "sony": "sony", "apple": "apple",
          "iphone": "iphone", "ipad": "ipad", "olympus": "olympus", "panasonic": "panasonic", "lumix": "panasonic",
          "leica": "leica", "ricoh": "ricoh", "pentax": "pentax", "hasselblad": "hasselblad", "gopro": "gopro", "dji": "dji",
          "samsung": "samsung", "galaxy": "samsung", "pixel": "pixel", "google": "google", "huawei": "huawei",
          "xiaomi": "xiaomi", "sigma": "sigma", "kodak": "kodak", "minolta": "minolta", "insta360": "insta360"}
GENERIC_MODEL = {"pro", "max", "mini", "plus", "ultra", "mark", "ii", "iii", "iv", "digital", "camera", "zoom"}
STOP = {"in", "of", "at", "from", "that", "i", "i've", "ive", "haven't", "havent", "have", "not", "photos", "photo", "pictures",
        "picture", "pics", "pic", "shots", "with", "and", "the", "a", "an", "my", "me", "show", "find", "all", "during", "on",
        "taken", "where", "which", "who", "is", "are", "were", "was", "yet", "any", "some", "by", "to", "for", "only", "just",
        "or"}
FIELD = re.compile(r"^(-?)([a-z_]+):(.*)$")
CMP = re.compile(r"^(>=|<=|>|<|=)?(.+)$")


def _cmp(v: str) -> tuple[str, str]:
    m = CMP.match(v)
    return (m.group(1) or "="), m.group(2)


def _date_range(v: str) -> tuple[str | None, str | None]:
    if ".." in v:
        a, b = v.split("..", 1)
        return (a or None), (b or None)
    if re.fullmatch(r"\d{4}", v):
        return f"{v}-01-01", f"{v}-12-31"
    if re.fullmatch(r"\d{4}-\d{2}", v):
        y, m = int(v[:4]), int(v[5:])
        end = date(y + (m == 12), (m % 12) + 1, 1).toordinal() - 1
        return f"{v}-01", date.fromordinal(end).isoformat()
    return v, v


def parse(text: str, conn=None, me: int | None = None) -> dict:
    """Returns {"filter": {...query params for filter_from...}, "text": str, "chips": [str], "sort": str|None}."""
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    flt: dict = {}
    chips: list[str] = []
    words: list[str] = []
    people: list[int] = []
    sort = None
    for tok in tokens:
        if tok.startswith("#") and len(tok) > 1 and conn is not None:     # #Climbing: a keyword
            from .keywords import find, path_of

            kid = find(conn, tok[1:])
            if kid:
                flt["keyword"] = kid
                chips.append("#" + " › ".join(path_of(conn, kid)))
                continue
        m = FIELD.match(tok.lower()) if ":" in tok else None
        if not m:
            words.append(tok)
            continue
        neg, key, raw = m.group(1), m.group(2), tok.split(":", 1)[1]
        op, val = _cmp(raw)
        if key in ("rating", "stars") and val.isdigit():
            flt["min_rating"] = int(val) + (1 if op == ">" else 0)
            chips.append(f"★ {flt['min_rating']}+")
        elif key == "is":
            v = val.lower()
            if v in ("pick", "picked", "picks"):
                flt["status"] = "picks"
            elif v in ("reject", "rejected", "rejects"):
                flt["status"] = "rejects"
            elif v in ("undecided", "new"):
                flt["status"] = "undecided"
            elif v == "rated":
                flt["status"] = "rated"
            elif v in ("video", "photo"):
                flt["media"] = v
            elif v == "edited":
                flt["edited"] = "1"
            elif v == "undated":
                flt["undated"] = "1"
            elif v in ("fav", "favourite", "favorite", "loved"):
                flt["favs"] = "me"
            chips.append(f"is {v}")
        elif key == "flag":
            flt["no_flag" if neg else "flag"] = val
            chips.append(("not " if neg else "") + val)
        elif key in ("q", "quality"):
            try:
                flt["max_q" if op.startswith("<") else "min_q"] = float(val)
                chips.append(f"quality {op}{val}")
            except ValueError:
                pass
        elif key == "iso":
            flt["max_iso" if op.startswith("<") else "min_iso"] = val
            chips.append(f"ISO {op}{val}")
        elif key in ("focal", "mm"):
            flt["max_focal" if op.startswith("<") else "min_focal"] = val
            chips.append(f"{op}{val} mm")
        elif key in ("f", "aperture"):
            flt["max_f"] = val
            chips.append(f"f/{val} or wider")
        elif key in ("camera", "lens", "label", "country"):
            flt[key] = val
            chips.append(f"{key} {val}")
        elif key in ("folder", "in"):
            flt["folder_like"] = val
            chips.append(f"folder {val}")
        elif key in ("date", "year", "when"):
            a, b = _date_range(val)
            if a:
                flt["from"] = a
            if b:
                flt["to"] = b
            chips.append(f"date {val}")
        elif key == "has" and val.lower() == "gps":
            flt["gps"] = "1"
            chips.append("has GPS")
        elif key in ("person", "who") and conn is not None:
            if val.lower() in ("me", "myself") and me is not None:
                people.append(me)
                chips.append("you")
                continue
            from .aliases import find as person_called

            r = person_called(conn, val, prefix=True)          # by their name or another name ("Papa")
            if r:
                people.append(r[0])
                chips.append(r[1])
        elif key == "place" and conn is not None:
            r = conn.execute("SELECT id, name FROM places WHERE name = ? COLLATE NOCASE", (val,)).fetchone()
            if r:
                flt["place"] = r[0]
                chips.append(r[1])
        elif key == "event" and conn is not None:
            r = conn.execute("SELECT id, name FROM events WHERE name LIKE ? COLLATE NOCASE OR alt_name LIKE ? COLLATE NOCASE"
                             " OR folder LIKE ? COLLATE NOCASE ORDER BY name LIKE ? COLLATE NOCASE DESC, n DESC",
                             (f"%{val}%",) * 4).fetchone()
            if r:
                flt["event"] = r[0]
                chips.append(r[1])
        elif key == "album" and conn is not None:
            r = conn.execute("SELECT id, name FROM albums WHERE name LIKE ? COLLATE NOCASE", (f"%{val}%",)).fetchone()
            if r:
                flt["album"] = r[0]
                chips.append(f"album {r[1]}")
        elif key in ("keyword", "kw", "tag") and conn is not None:
            from .keywords import find, path_of

            if val.lower() == "none":                       # keyword:none: nothing tagged yet
                flt["untagged"] = "1"
                chips.append("no keywords")
            else:
                kid = find(conn, val)
                if kid:
                    flt["keyword"] = kid
                    chips.append("#" + " › ".join(path_of(conn, kid)))
        elif key == "sort":
            sort = val
        else:
            words.append(tok)
    rest = " ".join(words)
    if conn is not None and rest:
        rest, extra = natural(rest, conn, me)
        for k, v in extra["filter"].items():
            flt.setdefault(k, v)
        people += extra["people"]
        chips += extra["chips"]
        sort = sort or extra.get("sort")
    if people:
        flt["people"] = ",".join(str(p) for p in dict.fromkeys(people))
    return {"filter": flt, "text": rest.strip(), "chips": chips, "sort": sort}


def _cameras(conn, low: str) -> list[tuple[str, str, str]]:
    """Cameras named in plain language: a model ("x-t4", "eos r5", "iphone 13 pro") or a maker
    ("fuji", "iphone"), when this library has photos from it. Returns (filter value, the words
    used, how to show it)."""
    names = [r[0] for r in conn.execute("SELECT DISTINCT camera FROM shots WHERE camera IS NOT NULL AND status='present'")]
    found: dict[str, tuple[str, str, str]] = {}
    for cam in sorted(names, key=len, reverse=True):
        parts = cam.lower().split()
        for k in range(len(parts)):                  # "fujifilm x-t4", then "x-t4"
            said = " ".join(parts[k:])
            specific = len(parts) - k >= 2 or (re.search(r"[a-z]", said) and re.search(r"\d", said))
            if len(said) > 2 and specific and said not in BRANDS and said not in GENERIC_MODEL \
                    and re.search(rf"(?<![\w-]){re.escape(said)}(?![\w-])", low):
                found.setdefault(cam, (cam, said, cam))
                break
    for word, brand in BRANDS.items():
        if not re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", low):
            continue
        theirs = [c for c in names if brand in c.lower()]
        if not theirs or any(brand in c.lower() for c in found):
            continue                                 # not in this library, or a model of it was named
        found[brand] = (brand, word, theirs[0] if len(theirs) == 1 else word.capitalize())
    return list(found.values())


def natural(text: str, conn, me: int | None = None) -> tuple[str, dict]:
    """Find people, places, years, months and decision words in plain language."""
    s = " " + re.sub(r"\s+", " ", text.strip()) + " "
    low = s.lower()
    flt: dict = {}
    chips: list[str] = []
    people: list[int] = []
    sort = None

    def take(span: str):
        nonlocal s, low
        i = low.find(span.lower())
        if i >= 0:
            s = s[:i] + " " + s[i + len(span):]
            low = s.lower()

    from .aliases import for_search

    for pid, name, shown in for_search(conn):          # names, other names ("Papa") and first names
        if name.lower() in low and re.search(rf"(?<![\w]){re.escape(name.lower())}(?:'s)?(?![\w])", low):
            if pid not in people:
                people.append(pid)
                chips.append(shown)
            take(name + "'s") if (name.lower() + "'s") in low else take(name)
    # "me" and "myself": the person you've said is you.
    if me is not None and me not in people and re.search(r"(?<![\w])(me|myself)(?![\w])", low):
        people.append(me)
        chips.append("you")
        take(" myself ") if " myself " in low else take(" me ")
    for pid, name, country in conn.execute(
            "SELECT p.id, p.name, p.country FROM places p WHERE EXISTS (SELECT 1 FROM shots s WHERE s.place_id = p.id)"
            " ORDER BY LENGTH(p.name) DESC").fetchall():
        if re.search(rf"(?<![\w]){re.escape(name.lower())}(?![\w])", low):
            flt["place"] = pid
            chips.append(name)
            take(name)
            break
        if country and re.search(rf"(?<![\w]){re.escape(country.lower())}(?![\w])", low):
            r = conn.execute("SELECT cc FROM places WHERE id=?", (pid,)).fetchone()
            flt["country"] = r[0]
            chips.append(country)
            take(country)
            break
    # "may 2024"; or a month on its own ("in june", "december"): that month of the one year
    # mentioned, or of any year. "may" and "march" are verbs too, so alone they need "in" or "from".
    mon = year = None
    m = re.search(rf"\b{MONTH_WORD}\.?\s+((?:19|20)\d\d)\b", low)
    if m:
        mon, year = MONTHS[m.group(1)[:3]], m.group(2)
        take(m.group(0))
    else:
        for mm in re.finditer(rf"\b(?:(in|from|during|of)\s+)?{MONTH_WORD}\b", low):
            if mm.group(1) or (len(mm.group(2)) >= 4 and mm.group(2) not in ("may", "march")):
                mon = MONTHS[mm.group(2)[:3]]
                take(mm.group(0))
                break
    years = re.findall(r"\b((?:19|20)\d\d)\b", low) if not year else []
    if mon and len(years) == 1:
        year, years = years[0], []
        take(year)
    if mon and year:
        flt["from"], flt["to"] = _date_range(f"{year}-{mon:02d}")
        chips.append(f"{MONTH_NAMES[mon - 1]} {year}")
    else:
        if mon:
            flt["month"] = mon
            chips.append(MONTH_NAMES[mon - 1] if years else f"{MONTH_NAMES[mon - 1]}, any year")
        if len(years) >= 2:
            flt["from"], flt["to"] = f"{min(years)}-01-01", f"{max(years)}-12-31"
            chips.append(f"{min(years)}–{max(years)}")
            for y in years:
                take(y)
        elif years:
            flt["from"], flt["to"] = _date_range(years[0])
            chips.append(years[0])
            take(years[0])
    for pat, key, val, chip in (
        (r"\b(haven'?t|not|never) (yet )?(picked|chosen|decided)\b|\bundecided\b|\bunsorted\b", "status", "undecided", "undecided"),
        (r"\b(my )?(picks|picked|keepers|chosen ones)\b", "status", "picks", "picks"),
        (r"\b(rejects|rejected)\b", "status", "rejects", "rejects"),
        (r"\b(videos?|clips?|movies?)\b", "media", "video", "videos"),
        (r"\b(edited|edits)\b", "edited", "1", "edited"),
        (r"\bundated\b|\bwithout (a )?date\b", "undated", "1", "undated"),
        (r"\b(favou?rites?|loved)\b", "favs", "me", "favourites"),
    ):
        mm = re.search(pat, low)
        if mm and key not in flt:
            flt[key] = val
            chips.append(chip)
            take(mm.group(0))
    mm = re.search(r"\b(best|top|strongest|nicest)\b", low)
    if mm:
        sort = "quality"
        chips.append("best first")
        take(mm.group(0))
    cams = _cameras(conn, low)
    if cams:
        flt["camera"] = ",".join(c[0] for c in cams)
        for _, said, chip in cams:
            chips.append(chip)
            take(said)
    if not chips:
        return text.strip(), {"filter": flt, "people": people, "chips": chips, "sort": sort}
    # Keep the words as typed ("dog on a beach" stays whole: "on" tells the picture search to want
    # both); only trim the filler left at the edges where recognised filters were cut out.
    words = re.findall(r"[\w'-]+", s)
    while words and words[0].lower() in STOP:
        words.pop(0)
    while words and words[-1].lower() in STOP:
        words.pop()
    rest = re.sub(r"\b(in|from|at|of|on|with|and)\s+(in|from|at|of|on|with|and|that|which)\b", r"\1", " ".join(words))
    return rest, {"filter": flt, "people": people, "chips": chips, "sort": sort}
