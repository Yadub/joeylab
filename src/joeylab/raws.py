"""RAW + JPEG pairs: which RAWs to keep, and moving the others to the Trash.

A RAW and the JPEG (or HEIC) the camera saved beside it are one photo with two files. Many people
want the RAW only of the photos they may edit again. For each photo the choice is Keep RAW, RAW not
needed, or none yet; rules decide the photos with no choice (by default the RAW of picks, of ★3 or
more, of favourites, of photos in an album and of edited photos stays). Your own choice comes first.
Choices are steps in the history, undone like a rating.

Only pairs count: a RAW with no other picture of the same photo beside it is the photo itself, and
is never a candidate. Removing a RAW takes one file away; the photo stays, shown from its JPEG, with
its stars, picks, albums, keywords, faces and the choice itself.

Moving the RAWs that aren't needed goes the way of every move to the Trash (cleanup.py): a read-only
check of each RAW and of the JPEG that stays (there, not empty, and it decodes), a typed count, and,
under the one-move-at-a-time lock, the same checks again just before each RAW goes, with the RAW's
own sidecar going along with it. Each run is written to the Trash log, so Put Back (in History, or
Finder's) returns them.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
import unicodedata
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable

from . import cleanup, config, db, imaging

RENDITIONS = ("jpeg", "other")           # files that show the photo without its RAW (JPEG, HEIC, TIFF...)
KEEP, REMOVE = "keep", "remove"
CHOICE_LABELS = {KEEP: "Keep RAW", REMOVE: "Reject RAW", "": "RAW back to the rules"}
RULES = {"pick": True, "stars": True, "min_stars": 3, "fav": True, "album": True, "edited": True}
EDIT_SLACK_NS = 3600 * 10**9             # a RAW saved more than an hour after its JPEG was changed since

# What decides a photo's RAW, in this order: your choice, then the rules. The first that applies names it.
KEPT = ("you", "pick", "stars", "fav", "album", "edited")
GOES = ("notneeded", "unkept")
WHY = {
    "you": "you chose Keep RAW",
    "pick": "picked",
    "stars": "rated ★{n} or more",
    "fav": "a favourite",
    "album": "in an album",
    "edited": "edited",
    "notneeded": "you rejected the RAW",
    "unkept": "no rule keeps it",
}

# ---------------------------------------------------------------- the state of each photo's RAW, in SQL
#
# For a query over `shots s LEFT JOIN decisions d` (every Library listing). The rules are read from
# the catalog's settings in the query itself, so the Library, the Tidy page, the command line and the
# check before a move all agree on which RAWs stay.

_RULES = "(SELECT value FROM settings WHERE key = 'raw_rules')"


def _on(rule: str) -> str:
    return f"COALESCE(json_extract({_RULES}, '$.{rule}'), {int(RULES[rule])})"


HAS_RAW = "EXISTS (SELECT 1 FROM files fr WHERE fr.shot_id = s.id AND fr.kind = 'raw' AND fr.status = 'present')"
HAS_OTHER = ("EXISTS (SELECT 1 FROM files fo WHERE fo.shot_id = s.id AND fo.kind IN ('jpeg', 'other')"
             " AND fo.status = 'present')")
CHOICE = "(SELECT rc.keep FROM raw_choices rc WHERE rc.shot_id = s.id)"
_NEWER = ("EXISTS (SELECT 1 FROM files fr JOIN files fo ON fo.shot_id = fr.shot_id WHERE fr.shot_id = s.id"
          " AND fr.kind = 'raw' AND fr.status = 'present' AND fo.kind IN ('jpeg', 'other') AND fo.status = 'present'"
          f" AND fr.mtime_ns > fo.mtime_ns + {EDIT_SLACK_NS})")
STATE = f"""(CASE WHEN NOT ({HAS_RAW} AND {HAS_OTHER}) THEN NULL
  WHEN {CHOICE} = 1 THEN 'you'
  WHEN {CHOICE} = 0 THEN 'notneeded'
  WHEN {_on('pick')} AND COALESCE(d.pick, 0) = 1 THEN 'pick'
  WHEN {_on('stars')} AND COALESCE(d.rating, 0) >= MAX(1, {_on('min_stars')}) THEN 'stars'
  WHEN {_on('fav')} AND EXISTS (SELECT 1 FROM viewer_decisions vd WHERE vd.shot_id = s.id AND vd.fav = 1) THEN 'fav'
  WHEN {_on('album')} AND EXISTS (SELECT 1 FROM album_items ai WHERE ai.shot_id = s.id) THEN 'album'
  WHEN {_on('edited')} AND (COALESCE(s.edited, 0) = 1 OR {_NEWER}) THEN 'edited'
  ELSE 'unkept' END)"""

_GROUPS = {
    "kept": f"{STATE} IN ({', '.join(repr(x) for x in KEPT)})",
    "removable": f"{STATE} IN ({', '.join(repr(x) for x in GOES)})",
    "pair": f"({HAS_RAW} AND {HAS_OTHER})",
    "only": f"({HAS_RAW} AND NOT {HAS_OTHER})",
}


def filter_sql(value: str) -> str | None:
    """The Library's `raw` filter: kept (by you or a rule), removable (what the Trash step would take),
    pair, only (a RAW with nothing beside it), or one state ("notneeded": you rejected the RAW)."""
    if value in _GROUPS:
        return _GROUPS[value]
    if value in KEPT + GOES:
        return f"{STATE} = '{value}'"
    return None


# ---------------------------------------------------------------- rules

def rules(conn) -> dict:
    saved = db.get_setting(conn, "raw_rules", {}) or {}
    out = dict(RULES)
    for k, v in saved.items():
        if k == "min_stars" and isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5:
            out[k] = v
        elif k in RULES and k != "min_stars" and isinstance(v, bool):
            out[k] = v
    return out


def set_rules(conn, changes: dict) -> dict:
    """Turn rules on or off ({pick: false}), or set the stars (min_stars: 1-5). Returns all of them."""
    for k, v in changes.items():
        if k not in RULES:
            raise ValueError(f"There's no rule called {k!r}.")
        if k == "min_stars":
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 5:
                raise ValueError("Stars are a whole number from 1 to 5.")
        elif not isinstance(v, bool):
            raise ValueError("A rule is on (true) or off (false).")
    db.write_lock(conn)                   # read, change and save as one: two toggles at once both count
    cur = {**rules(conn), **changes}
    db.set_setting(conn, "raw_rules", cur)
    return cur


def why(state: str | None, r: dict) -> str:
    return WHY.get(state or "", "").format(n=r["min_stars"])


# ---------------------------------------------------------------- choices

def _chunks(ids: list[int], k: int = 500):
    for i in range(0, len(ids), k):
        yield ids[i : i + k]


def choices(conn, shot_ids: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for chunk in _chunks([int(x) for x in shot_ids]):
        for sid, keep in conn.execute(f"SELECT shot_id, keep FROM raw_choices WHERE shot_id IN ({','.join('?' * len(chunk))})", chunk):
            out[sid] = KEEP if keep else REMOVE
    return out


def put_choice(conn, sid: int, choice: str) -> None:
    """Record one photo's choice ("" takes it away, so the rules decide)."""
    if choice in (KEEP, REMOVE):
        conn.execute("INSERT INTO raw_choices(shot_id, keep, updated) VALUES (?,?,?) ON CONFLICT(shot_id)"
                     " DO UPDATE SET keep=excluded.keep, updated=excluded.updated", (sid, int(choice == KEEP), time.time()))
    else:
        conn.execute("DELETE FROM raw_choices WHERE shot_id=?", (sid,))


def pairs_among(conn, shot_ids: list[int]) -> set[int]:
    out: set[int] = set()
    for chunk in _chunks([int(x) for x in shot_ids]):
        out |= {r[0] for r in conn.execute(
            f"SELECT s.id FROM shots s WHERE s.id IN ({','.join('?' * len(chunk))}) AND s.status = 'present'"
            f" AND {HAS_RAW} AND {HAS_OTHER}", chunk)}
    return out


def choose(conn, shot_ids: list[int], choice: str, source: str = "web", viewer: str | None = None,
           what: str | None = None) -> dict:
    """Keep RAW, Reject RAW, or "" (no choice: the rules decide) for these photos, as one undoable
    step in the history. Only photos with a RAW and a JPEG take one; the rest are counted, not changed.
    Returns {batch, changed, not_pairs}."""
    if choice not in (KEEP, REMOVE, ""):
        raise ValueError("The choice is keep, remove or none.")
    ids = list(dict.fromkeys(int(i) for i in shot_ids))
    db.write_lock(conn)                   # what each photo had, and the change, in one step
    pairs = pairs_among(conn, ids)
    current = choices(conn, list(pairs))
    batch, now, changed = uuid.uuid4().hex[:12], time.time(), []
    for sid in ids:
        if sid not in pairs:
            continue
        before = current.get(sid, "")
        if before == choice:
            continue
        put_choice(conn, sid, choice)
        conn.execute("INSERT INTO decision_log(ts, batch, label, source, viewer, shot_id, before, after) VALUES (?,?,?,?,?,?,?,?)",
                     (now, batch, (what or CHOICE_LABELS[choice])[:60], source, viewer, sid,
                      json.dumps({"raw": before}), json.dumps({"raw": choice})))
        changed.append(sid)
    conn.commit()
    return {"batch": batch if changed else None, "changed": changed, "not_pairs": len(ids) - len(pairs)}


def status(conn, shot_ids: list[int]) -> dict[int, dict]:
    """Each photo's RAW: {state, why, choice, raws, bytes}. state is one of KEPT or GOES for a pair,
    "only" for a RAW with nothing beside it, "gone" when its RAW is missing and its JPEG isn't, or None
    (no RAW)."""
    r_ = rules(conn)
    out: dict[int, dict] = {}
    for chunk in _chunks([int(x) for x in shot_ids]):
        for r in conn.execute(
                f"SELECT s.id, {STATE} AS st, {HAS_RAW} AS has_raw, {CHOICE} AS choice,"
                " (SELECT COUNT(*) FROM files f WHERE f.shot_id = s.id AND f.kind = 'raw' AND f.status = 'present') AS n,"
                " (SELECT COALESCE(SUM(f.size), 0) FROM files f WHERE f.shot_id = s.id AND f.kind = 'raw' AND f.status = 'present') AS b,"
                " EXISTS (SELECT 1 FROM files f WHERE f.shot_id = s.id AND f.kind = 'raw' AND f.status = 'missing') AS gone,"
                f" {HAS_OTHER} AS other"
                f" FROM shots s LEFT JOIN decisions d ON d.shot_id = s.id WHERE s.id IN ({','.join('?' * len(chunk))})", chunk):
            state = r["st"] or ("only" if r["has_raw"] else "gone" if r["gone"] and r["other"] else None)
            out[r["id"]] = {"state": state, "why": why(state, r_), "raws": r["n"], "bytes": r["b"],
                            "choice": "" if r["choice"] is None else KEEP if r["choice"] else REMOVE}
    return out


def count(conn, shot_ids: list[int]) -> dict:
    """What becomes of the RAWs of these photos, by state: {state: {photos, bytes, ids}} for pairs
    only, and the totals. For acting on a whole event or selection at once."""
    by: dict[str, dict] = {}
    for sid, st in status(conn, shot_ids).items():
        if st["state"] in KEPT + GOES:
            g = by.setdefault(st["state"], {"photos": 0, "bytes": 0, "ids": []})
            g["photos"] += 1
            g["bytes"] += st["bytes"]
            g["ids"].append(sid)
    return {"states": by, "pairs": sum(g["photos"] for g in by.values()), "bytes": sum(g["bytes"] for g in by.values())}


def state_of(conn, sid: int) -> str | None:
    r = conn.execute(f"SELECT {STATE} FROM shots s LEFT JOIN decisions d ON d.shot_id = s.id WHERE s.id = ?", (sid,)).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------- the Tidy page

def _blank() -> dict:
    return {"photos": 0, "raws": 0, "bytes": 0, "kept": 0, "kept_bytes": 0, "removable": 0, "removable_raws": 0,
            "removable_bytes": 0, "rejected": 0, "rejected_raws": 0, "rejected_bytes": 0}


def summary(conn, max_folders: int = 300) -> dict:
    """The pairs and the space their RAWs take; how many RAWs stay and why (each photo under the first
    reason that applies), how many aren't needed and the space they'd free; the same by year and by
    folder; and the RAWs with nothing beside them, which always stay."""
    pairs, only = _blank(), {"photos": 0, "raws": 0, "bytes": 0}
    states = {s: {"photos": 0, "raws": 0, "bytes": 0} for s in KEPT + GOES}
    years: dict[str, dict] = defaultdict(_blank)
    folders: dict[str, dict] = defaultdict(_blank)
    for r in conn.execute(
            f"SELECT s.dir, substr(s.capture_str, 1, 4) AS y, COALESCE(s.time_src, 'exif') AS src, {STATE} AS st,"
            " (SELECT COUNT(*) FROM files f WHERE f.shot_id = s.id AND f.kind = 'raw' AND f.status = 'present') AS n,"
            " (SELECT COALESCE(SUM(f.size), 0) FROM files f WHERE f.shot_id = s.id AND f.kind = 'raw' AND f.status = 'present') AS b"
            f" FROM shots s LEFT JOIN decisions d ON d.shot_id = s.id WHERE s.status = 'present' AND {HAS_RAW}"):
        n, b, st = r["n"], r["b"], r["st"]
        if st is None:
            only["photos"] += 1
            only["raws"] += n
            only["bytes"] += b
            continue
        year = r["y"] if r["y"] and r["src"] != "file" else ""
        for agg in (pairs, years[year], folders[r["dir"]]):
            agg["photos"] += 1
            agg["raws"] += n
            agg["bytes"] += b
            if st == "notneeded":
                agg["rejected"] += 1
                agg["rejected_raws"] += n
                agg["rejected_bytes"] += b
            if st in GOES:
                agg["removable"] += 1
                agg["removable_raws"] += n
                agg["removable_bytes"] += b
            else:
                agg["kept"] += 1
                agg["kept_bytes"] += b
        states[st]["photos"] += 1
        states[st]["raws"] += n
        states[st]["bytes"] += b
    by_year = sorted(({"year": y, **v} for y, v in years.items() if y), key=lambda x: x["year"], reverse=True) \
        + ([{"year": "", **years[""]}] if "" in years else [])          # newest first, undated last
    by_folder = sorted(({"path": d, **v} for d, v in folders.items()),
                       key=lambda x: (-x["rejected_bytes"], -x["removable_bytes"], -x["bytes"], x["path"]))
    return {"pairs": pairs, "raw_only": only, "states": states, "rules": rules(conn),
            "years": by_year, "folders": by_folder[:max_folders], "folder_count": len(by_folder)}


# ---------------------------------------------------------------- what moving them would do

@dataclass
class RawItem(cleanup.Item):
    state: str = ""                                       # why it goes: notneeded (you rejected it) | unkept (no rule keeps it)
    allowed: tuple = GOES                                 # the states it may still go in, checked again before it moves
    sidecars: list = field(default_factory=list)          # a cleanup.Item for each of its own sidecars
    owns_stem: bool = False                               # IMG_1.xmp is this RAW's (see own_sidecars)


def _name_key(name: str) -> str:
    """A file name as macOS compares it: case and Unicode spelling ignored."""
    return unicodedata.normalize("NFC", name).lower()


def own_sidecars(raw: str, owns_stem: bool, listing: dict | None = None) -> list[str]:
    """The RAW's own .xmp files beside it: IMG_1.CR3.xmp (darktable) always, and IMG_1.xmp (Lightroom,
    Capture One: one per photo, kept by its RAW) when it owns that name. A JPEG's own sidecar
    (IMG_1.JPG.xmp) is never among them. Names as they are on disk, each file once."""
    folder, name = os.path.split(raw)
    if listing is None or folder not in listing:
        try:
            names = os.listdir(folder)
        except OSError:
            names = []
        if listing is not None:
            listing[folder] = names
    else:
        names = listing[folder]
    want = {_name_key(name + ".xmp")}
    if owns_stem:
        want.add(_name_key(os.path.splitext(name)[0] + ".xmp"))
    out = []
    for n in sorted(names):
        if _name_key(n) in want:
            p = os.path.join(folder, n)
            try:
                if stat.S_ISREG(os.lstat(p).st_mode):
                    out.append(p)
            except OSError:
                continue
    return out


_DEVELOP = re.compile(rb'crs:HasSettings\s*=\s*"True"|<crs:HasSettings>\s*True|crs:ProcessVersion|darktable:history', re.I)
_AGENT = re.compile(rb'softwareAgent\s*(?:=\s*"|>)([^"<]{1,200})')


def has_develop_settings(path: str) -> bool:
    """A sidecar holding develop settings, as the scan reads them (exif.summarize_sidecar): Adobe's
    settings or process version, darktable's history, or a history written by an editing app. One
    that can't be read counts as edited: the RAW stays."""
    from .exif import is_editor

    try:
        with open(path, "rb") as fh:
            data = fh.read(8 << 20)
    except OSError:
        return True
    return bool(_DEVELOP.search(data)) or any(is_editor(m.decode("utf-8", "replace")) for m in _AGENT.findall(data))


def _edited_now(raw: str, stays: str, sidecars: list[str]) -> str:
    """Why a RAW no rule keeps is edited after all, looking at the disk now, or ""."""
    if any(has_develop_settings(s) for s in sidecars):
        return "it has develop settings in its sidecar"
    try:
        if os.stat(raw).st_mtime_ns > os.stat(stays).st_mtime_ns + EDIT_SLACK_NS:
            return "the RAW was saved after its JPEG"
    except OSError:
        pass
    return ""


def _noun(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return "JPEG" if ext in (".jpg", ".jpeg") else "HEIC" if ext in (".heic", ".heif", ".hif") else ext[1:].upper() or "picture"


def _rendition(path: str, not_ident: tuple | None) -> tuple[int | None, str]:
    """(size, "") when the picture that stays is there, not empty and decodes; else (None, why)."""
    noun = _noun(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, f"its {noun} is missing"
    except OSError as e:
        return None, f"its {noun} can't be read ({e.strerror})"
    if not stat.S_ISREG(st.st_mode) or (not_ident and (st.st_dev, st.st_ino) == not_ident):
        return None, f"its {noun} isn't a file of its own"
    if not st.st_size:
        return None, f"its {noun} is empty"
    if not imaging.decodes(path):
        return None, f"its {noun} can't be read whole (damaged or cut off)"
    return st.st_size, ""


def _side_item(path: str, raw: str) -> cleanup.Item:
    st = os.lstat(path)
    return cleanup.Item(path, category=cleanup.SIDECAR, size=st.st_size, mtime_ns=st.st_mtime_ns, stays=None,
                        ident=(st.st_dev, st.st_ino), why=f"goes with {os.path.basename(raw)}")


def _namesakes(conn, folder: str, stem: str, sid: int, cache: dict) -> bool:
    """Another photo in this folder has this name (IMG_1.TIF taken at another time), so IMG_1.xmp
    isn't this RAW's alone."""
    if folder not in cache:
        names: dict[str, set] = defaultdict(set)
        for other, s in conn.execute("SELECT id, stem FROM shots WHERE dir=?", (folder,)):
            names[_name_key(s)].add(other)
        cache[folder] = names
    return bool(cache[folder].get(_name_key(stem), set()) - {sid})


def plan(conn, shot_ids: list[int] | None = None, progress: Callable[[int, int], None] | None = None,
         include_unkept: bool = False) -> list[RawItem]:
    """What moving the RAWs that aren't needed would do, without changing anything: for each RAW of the
    photos whose RAW goes (all of them, or those among shot_ids), the JPEG that stays and the RAW's own
    sidecars (category TWIN), or why it's left alone (SKIP, with why). Only the RAWs you rejected go,
    unless include_unkept: then also those no rule keeps."""
    from .scanner import PREVIEW_PREFERENCE

    home = os.path.realpath(config.HOME)
    goes = GOES if include_unkept else ("notneeded",)
    rows = conn.execute(
        f"SELECT * FROM (SELECT s.id, s.dir, s.stem, {STATE} AS st FROM shots s LEFT JOIN decisions d ON d.shot_id = s.id"
        f" WHERE s.status = 'present') WHERE st IN ({', '.join(repr(x) for x in goes)}) ORDER BY dir, stem").fetchall()
    if shot_ids is not None:
        wanted = {int(x) for x in shot_ids}
        rows = [r for r in rows if r["id"] in wanted]
    files: dict[int, list] = defaultdict(list)
    for chunk in _chunks([r["id"] for r in rows]):
        for f in conn.execute("SELECT id, shot_id, path, kind, size, mtime_ns FROM files WHERE status = 'present'"
                              f" AND shot_id IN ({','.join('?' * len(chunk))}) ORDER BY path", chunk):
            files[f["shot_id"]].append(f)
    total = sum(1 for r in rows for f in files[r["id"]] if f["kind"] == "raw")
    live = rules(conn)["edited"]
    items: list[RawItem] = []
    listing: dict = {}
    names: dict = {}
    for r in rows:
        fs = files[r["id"]]
        raws = [f for f in fs if f["kind"] == "raw"]
        others = sorted((f for f in fs if f["kind"] in RENDITIONS), key=lambda f: (PREVIEW_PREFERENCE.get(f["kind"], 9), f["path"]))
        owns_stem = len(raws) == 1 and not _namesakes(conn, r["dir"], r["stem"], r["id"], names)
        for f in raws:
            if progress:
                progress(len(items) + 1, total)
            it = RawItem(f["path"], shot_id=r["id"], state=r["st"], owns_stem=owns_stem, allowed=goes)
            items.append(it)
            it.why = _check_raw(it, f, home)
            if it.why:
                continue
            reasons = []
            for o in others:
                size, why_not = _rendition(o["path"], it.ident)
                if size:
                    it.stays, it.stays_size = o["path"], size
                    break
                reasons.append(why_not)
            if not it.stays:
                it.why = reasons[0] if reasons else "no JPEG beside it any more"
                continue
            sides = own_sidecars(it.path, owns_stem, listing)
            if r["st"] == "unkept" and live:
                edited = _edited_now(it.path, it.stays, own_sidecars(it.path, True, listing))
                if edited:
                    it.why = f"kept: {edited}"
                    continue
            try:
                it.sidecars = [_side_item(p, it.path) for p in sides]
            except OSError:
                it.why = "its sidecar can't be read"
                continue
            it.category = cleanup.TWIN
    return items


def _check_raw(it: RawItem, f, home: str) -> str:
    try:
        st = os.lstat(it.path)
    except FileNotFoundError:
        return "already gone"
    except OSError as e:
        return f"can't be read ({e.strerror})"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if os.path.realpath(it.path).startswith(home + os.sep):
        return "inside JoeyLab's own folder"
    it.size, it.mtime_ns, it.ident = st.st_size, st.st_mtime_ns, (st.st_dev, st.st_ino)
    if st.st_size != f["size"] or st.st_mtime_ns != f["mtime_ns"]:
        return "changed since JoeyLab last read it (update the library and check again)"
    return ""


def going(items: list[RawItem]) -> list[RawItem]:
    return [i for i in items if i.category == cleanup.TWIN]


def by_folder(items: list[RawItem]) -> list[dict]:
    """For each folder, most space first: the RAWs that would go and their space (sidecars included),
    and the ones left alone."""
    rows: dict[str, dict] = {}
    for i in items:
        r = rows.setdefault(os.path.dirname(i.path), {"raws": 0, "bytes": 0, "sidecars": 0, "skipped": 0})
        if i.category == cleanup.TWIN:
            r["raws"] += 1
            r["bytes"] += i.size + sum(s.size for s in i.sidecars)
            r["sidecars"] += len(i.sidecars)
        else:
            r["skipped"] += 1
    return sorted(({"path": d, **r} for d, r in rows.items()), key=lambda r: (-r["bytes"], -r["skipped"], r["path"]))


def skipped(items: list[RawItem]) -> list[tuple[str, int]]:
    return Counter(i.why for i in items if i.category != cleanup.TWIN).most_common()


# ---------------------------------------------------------------- moving them

_NOW = {
    "you": "kept now (you chose Keep RAW since the check)",
    "pick": "kept now (picked since the check)",
    "stars": "kept now (rated since the check)",
    "fav": "kept now (a favourite since the check)",
    "album": "kept now (in an album since the check)",
    "edited": "kept now (edited since the check)",
    "unkept": "kept now (you took back Reject RAW since the check)",
    None: "no JPEG beside it any more",
}


def still_goes(conn, it: RawItem) -> str | None:
    """Asked just before the RAW moves: None if it should still go, else why not. The photo's RAW
    must still not be needed (your choice and the rules, read again), and the picture that stays must
    still be there, unchanged and whole; the RAW's own sidecars must be the ones checked."""
    st = state_of(conn, it.shot_id)
    if st not in it.allowed:
        return _NOW.get(st, _NOW[None])
    if st == "unkept" and rules(conn)["edited"]:
        edited = _edited_now(it.path, it.stays, own_sidecars(it.path, True))
        if edited:
            return f"kept now ({edited})"
    size, why_not = _rendition(it.stays, it.ident)
    if not size:
        return why_not
    if size != it.stays_size:
        return f"its {_noun(it.stays)} changed since the check"
    if own_sidecars(it.path, it.owns_stem) != [s.path for s in it.sidecars]:
        return "its sidecars changed since the check"
    for s in it.sidecars:
        try:
            now = os.lstat(s.path)
        except OSError:
            return "its sidecar changed since the check"
        if (now.st_size, now.st_mtime_ns) != (s.size, s.mtime_ns):
            return "its sidecar changed since the check"
    return None


def _with_sidecars(conn, it: RawItem, mover) -> None:
    """The RAW has just gone: the catalog says so at once (the photo stays, shown from its JPEG), and
    its own sidecars follow it."""
    cleanup.mark_moved(conn, it)
    for s in it.sidecars:
        try:
            now = os.lstat(s.path)
        except OSError:
            s.result = "left alone: gone already"
            continue
        if (now.st_size, now.st_mtime_ns) != (s.size, s.mtime_ns):
            s.result = "left alone: changed since it was checked"
            continue
        try:
            s.trashed_to = mover(s.path)
            s.result = "moved to the Trash"
        except OSError as e:
            s.result = f"left alone: {e}"


def trash(conn, items: list[RawItem], log_name: str, mover: Callable[[str], str | None] | None = None,
          progress: Callable[[int, int], None] | None = None) -> str:
    """Move the RAWs the check found to the Trash, each checked again just before, with their own
    sidecars. One run at a time, across the app and the command line. Every RAW checked goes in the
    log, moved or not (and its sidecars after it), as other moves to the Trash do. Returns the log."""
    mover = mover or cleanup.move_to_trash
    go = going(items)
    seen = []

    def check(it: RawItem) -> str | None:
        seen.append(it)
        if progress:
            progress(len(seen), len(go))
        return still_goes(conn, it)

    with cleanup.trash_lock():
        cleanup._move_files(go, mover, check, on_moved=lambda it: _with_sidecars(conn, it, mover))
    rows: list = []
    for it in items:
        rows.append(it)
        for s in it.sidecars:
            s.result = s.result or "left alone: its RAW stayed"
            rows.append(s)
    return cleanup._log(rows, log_name)


def moved(items: list[RawItem]) -> tuple[list[RawItem], int]:
    """The RAWs moved, and the space they and their sidecars took."""
    done = [i for i in items if i.result == "moved to the Trash"]
    return done, sum(i.size for i in done) + sum(s.size for i in done for s in i.sidecars if s.result == "moved to the Trash")
