"""Exact duplicates (identical bytes) and copies (same photo re-saved/resized).

Duplicates are copies on one drive. A byte-identical copy on another drive is a backup, not a
duplicate: sets are formed per drive, so it is never an extra, never rejected or moved to the Trash
for being a copy. Inside one library, identical copies on different drives are linked as a "backup"
set (one copy shown, the others left out of culling, stacks and events), never removable."""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from typing import Callable


from .config import DUP_MAX_HAM, DUP_NOEXIF_MAX_HAM
from .db import to_u64
from .grouping import camera_key
from .hashing import full_hash, hamming


def _hash_candidates(conn, progress) -> dict[str, list]:
    """Identical files share size and quick fingerprint (first + last 256 KB), so only those are
    read in full. Hashes are saved in small commits, never inside a long write: decisions you make
    meanwhile don't wait, and an interrupted run keeps what it hashed."""
    by_fp: dict[str, list] = defaultdict(list)
    for f in conn.execute("SELECT id, path, size, shot_id, full_hash, fp, root_id FROM files WHERE status='present'"):
        by_fp[f["fp"]].append(dict(f))
    candidates = [f for fs in by_fp.values() if len(fs) > 1 for f in fs]
    by_hash: dict[str, list] = defaultdict(list)
    pending: list[tuple] = []
    for i, f in enumerate(candidates):
        h = f["full_hash"]
        if h is None:
            try:
                h = full_hash(f["path"])
            except OSError:
                continue
            pending.append((h, f["id"]))
            if len(pending) >= 200:
                conn.executemany("UPDATE files SET full_hash=? WHERE id=?", pending)
                conn.commit()
                pending = []
        by_hash[h].append(f)
        if progress:
            progress(i + 1, len(candidates))
    if pending:
        conn.executemany("UPDATE files SET full_hash=? WHERE id=?", pending)
        conn.commit()
    return by_hash


def _by_drive(members: list, drive_of) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for m in members:
        out[drive_of(m)].append(m)
    return out


def run(conn, progress: Callable[[int, int], None] | None = None) -> dict:
    from . import volumes

    by_hash = _hash_candidates(conn, progress)
    drives = volumes.drive_keys(conn)
    drive = lambda root_id: drives.get(root_id, volumes.LOCAL)
    sets: list[tuple[str, list[dict]]] = []      # (kind, members in quality order); the keeper is chosen below

    # --- exact: identical files on one drive. Identical files on different drives back each other up.
    exact_sets = 0
    in_exact: set[frozenset] = set()
    across: list[list[dict]] = []                # identical files on more than one drive (this library's backups)
    for fs in by_hash.values():
        if len(fs) < 2:
            continue
        in_exact.add(frozenset(f["shot_id"] for f in fs))
        per = _by_drive(sorted(fs, key=lambda f: f["id"]), lambda f: drive(f["root_id"]))
        for ms in per.values():
            if len(ms) > 1:
                sets.append(("exact", [{"file_id": f["id"], "shot_id": f["shot_id"], "path": f["path"], "q": None} for f in ms]))
                in_exact.add(frozenset(f["shot_id"] for f in ms))
                exact_sets += 1
        if len(per) > 1:
            across.append(sorted(fs, key=lambda f: f["id"]))

    # --- copies: same capture instant + same body + near-identical pixels.
    shots = conn.execute(
        "SELECT s.id, s.stem, s.capture_ts, s.camera, s.body_serial, s.phash, s.width, s.height, s.preview_file_id,"
        " (SELECT meta FROM files f WHERE f.id = s.preview_file_id) AS pmeta,"
        " (SELECT path FROM files f WHERE f.id = s.preview_file_id) AS ppath,"
        " (SELECT root_id FROM files f WHERE f.id = s.preview_file_id) AS proot,"
        " (SELECT MAX(kind='raw') FROM files f WHERE f.shot_id=s.id AND f.status='present') AS has_raw,"
        " (SELECT MAX(kind!='raw') FROM files f WHERE f.shot_id=s.id AND f.status='present') AS has_other,"
        " (SELECT SUM(size) FROM files f WHERE f.shot_id=s.id AND f.status='present') AS bytes"
        " FROM shots s WHERE s.status='present' AND s.phash IS NOT NULL"
    ).fetchall()
    buckets: dict[tuple, list] = defaultdict(list)
    no_exif = []
    for s in shots:
        if s["capture_ts"] is None:
            if _distinctive(to_u64(s["phash"])):
                no_exif.append(s)
        else:
            buckets[(camera_key(s["camera"], s["body_serial"]), round(s["capture_ts"], 2))].append(s)

    parent = {s["id"]: s["id"] for s in shots}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for group in buckets.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if hamming(to_u64(group[i]["phash"]), to_u64(group[j]["phash"])) <= DUP_MAX_HAM:
                    union(group[i]["id"], group[j]["id"])
    if no_exif:
        # Photos without a capture time can only be matched on pixels, so the bar is stricter: the
        # same shape, and close to the set's first photo (not just to some member: chains of
        # "close to close to close" would join blank frames and screenshots into one huge set).
        # Candidates come from slices of the hash (any two within 4 bits share a whole slice), so
        # this stays fast with tens of thousands of undated photos.
        by_id_ = {s["id"]: s for s in shots}
        slices: list[dict[int, list[int]]] = [defaultdict(list) for _ in _SLICES]
        for s in shots:
            h = to_u64(s["phash"])
            for k, (shift, mask) in enumerate(_SLICES):
                slices[k][(h >> shift) & mask].append(s["id"])
        for s in no_exif:
            h = to_u64(s["phash"])
            near = set()
            for k, (shift, mask) in enumerate(_SLICES):
                near.update(slices[k].get((h >> shift) & mask, ()))
            near.discard(s["id"])
            for other in sorted(near):
                o = by_id_[other]
                if hamming(h, to_u64(o["phash"])) > DUP_NOEXIF_MAX_HAM or not _same_shape(s, o):
                    continue
                root = by_id_[find(other)]
                if hamming(h, to_u64(root["phash"])) <= DUP_NOEXIF_MAX_HAM:
                    union(s["id"], other)

    clusters: dict[int, list] = defaultdict(list)
    by_id = {s["id"]: s for s in shots}
    for sid in parent:
        clusters[find(sid)].append(by_id[sid])

    def add_set(kind: str, ms: list) -> None:
        sets.append((kind, [{"file_id": m["preview_file_id"], "shot_id": m["id"], "path": m["ppath"] or "",
                             "q": (m["has_raw"] or 0, (m["width"] or 0) * (m["height"] or 0), m["bytes"] or 0)} for m in ms]))

    for members in clusters.values():
        if len(members) < 2 or frozenset(m["id"] for m in members) in in_exact:
            continue
        members.sort(key=lambda m: (-(m["has_raw"] or 0), -((m["width"] or 0) * (m["height"] or 0)), -(m["bytes"] or 0), m["id"]))
        # One RAW-only shot plus the camera's JPEG of it, kept in another folder: that's a split
        # RAW+JPEG pair, not a redundant copy, so it is reported but never suggested for rejection.
        raw_only = [m for m in members if m["has_raw"] and not m["has_other"]]
        other_only = [m for m in members if m["has_other"] and not m["has_raw"]]
        if len(raw_only) == 1 and len(raw_only) + len(other_only) == len(members) == 2:
            add_set("pair", members)
            continue
        # Same file name, same size in pixels and not touched by an editor = the same file saved
        # again: a redundant copy. Anything else (DSCF6148.JPG vs 2026-Lisbon-88.jpg, a resized
        # web version, a Lightroom export that kept the camera's name) is a version of the
        # original, which is your work: linked for review, never suggested for rejection.
        by_name: dict[tuple, list] = defaultdict(list)
        for m in members:
            by_name[((m["stem"] or "").lower(), m["width"], m["height"], _edited_by(m))].append(m)
        for ms in by_name.values():
            # A re-saved copy on another drive is neither a duplicate (it's on another drive) nor a
            # backup (it isn't the same file): only copies on one drive make a set.
            for part in _by_drive(ms, lambda m: drive(m["proot"])).values():
                if len(part) > 1:
                    add_set("copy", part)
        if len(by_name) > 1:
            add_set("export", [ms[0] for ms in by_name.values()])

    rank = _Ranker(conn, sets)
    chosen = choose_keepers(conn, sets, rank)
    # One "backup" set per photo file held on several drives: every identical copy of it, the one
    # shown chosen below (from the copies each drive keeps). Never removable.
    backups = [("backup", [(f["id"], f["shot_id"], 0) for f in fs]) for fs in across]
    _write_sets(conn, chosen + backups)
    if backups:
        choose_shown(conn, rank)
    n = Counter(kind for kind, _ in sets)
    return {"exact_sets": exact_sets, "copy_sets": n.get("copy", 0), "split_pairs": n.get("pair", 0),
            "exports": n.get("export", 0), "backup_sets": len(backups)}


# Five slices of a 64-bit hash: (shift, mask).
_SLICES = [(0, (1 << 13) - 1), (13, (1 << 13) - 1), (26, (1 << 13) - 1), (39, (1 << 13) - 1), (52, (1 << 12) - 1)]


def _set_key(kind: str, members: list[tuple[int, int, int]]) -> str:
    return kind + ":" + ",".join(str(f) for f in sorted(m[0] for m in members))


def _write_sets(conn, sets: list) -> None:
    """Replace the sets in one short write. A set with the same files as before keeps its id, so a
    page loaded before an update still acts on the sets it shows; new sets get ids never used."""
    from . import db

    old = {r[0]: r[1] for r in conn.execute("SELECT key, id FROM dup_sets WHERE key IS NOT NULL")}
    rows, members = [], []
    used: set[int] = set()
    for kind, ms in sets:
        key = _set_key(kind, ms)
        sid = old.get(key)
        if sid is None or sid in used:
            sid = None
        rows.append([sid, kind, key, ms])
        if sid is not None:
            used.add(sid)
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            if r[0] is None:
                r[0] = db.new_id(conn, "dup_sets")
        conn.execute("DELETE FROM dup_members")
        conn.execute("DELETE FROM dup_sets")
        conn.executemany("INSERT INTO dup_sets(id, kind, key) VALUES (?,?,?)", [(r[0], r[1], r[2]) for r in rows])
        conn.executemany("INSERT INTO dup_members(set_id, file_id, shot_id, keep) VALUES (?,?,?,?)",
                         [(r[0], f, sh, k) for r in rows for (f, sh, k) in r[3]])
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _distinctive(h: int) -> bool:
    """Blank or nearly uniform frames hash to almost all 0s or 1s and match each other by chance."""
    return 6 <= h.bit_count() <= 58


def _same_shape(a, b) -> bool:
    if not (a["width"] and a["height"] and b["width"] and b["height"]):
        return True
    return abs(a["width"] / a["height"] - b["width"] / b["height"]) < 0.01


def _edited_by(m) -> str:
    """The editing app that saved this file, if any (Lightroom, Capture One, ...)."""
    from .exif import EDITORS

    try:
        sw = (json.loads(m["pmeta"] or "{}").get("software") or "").lower()
    except ValueError:
        return ""
    return next((e for e in EDITORS if e in sw), "")


# Only these kinds are redundant copies. "export" (a version: your edit, a resize) and "pair" (a
# RAW and its camera JPEG kept apart) are never offered as files to remove.
REMOVABLE = ("exact", "copy")


# ---------------------------------------------------------------- which copy is kept

# Folder names that say "a copy of something kept elsewhere": their copies are the last ones kept.
_AVOID = re.compile(r"\b(backups?|back up|bak|copy|copies|exports?|exported|dumps?|desktop|old|shared|selected|selects|"
                    r"selections?|duplicates?|dupes|temp|tmp|to sort|unsorted|recovered|downloads?|whatsapp)\b", re.I)
_DATED = re.compile(r"^\s*(19|20)\d\d(?!\d)|^\s*(19|20)\d{6}")          # "2024", "2024-05 Lisbon", "20240501"
_CAMERA = re.compile(r"^(dcim|\d{3}[a-z0-9_]{5})$", re.I)                # DCIM, 100_FUJI, 100CANON, 101MSDCF
_COPY_NAME = re.compile(r"(\bcopy( \d+)?|\(\d+\))$", re.I)               # "IMG_0001 copy", "IMG_0001 (1)"
NEAR_SIZE = 0.95        # re-saved copies this close in size are as good as each other: the folder decides
RULES_KEY = "keep_under"


def keep_rules(conn) -> list[str]:
    """Folders whose copies are always the ones kept ("always keep copies under ..."), with everything
    inside them. Kept per library."""
    from .db import get_setting

    return [r for r in (get_setting(conn, RULES_KEY, []) or []) if isinstance(r, str) and r]


def set_keep_rule(conn, folder: str, on: bool = True) -> int:
    """Add (or remove) an "always keep copies under this folder" rule, then choose every set's
    kept copy again. Returns how many sets now keep a different copy."""
    from .db import set_setting

    folder = folder.rstrip("/") or "/"
    rules = [r for r in keep_rules(conn) if r != folder]
    if on:
        rules.append(folder)
    set_setting(conn, RULES_KEY, sorted(rules))
    return rekeep(conn)


def _under(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _words(part: str) -> str:
    return re.sub(r"[_.\-]+", " ", part)


class _Ranker:
    """Which folder's copy to keep, best first:
    1. a folder you chose ("Keep the copies in", then "Always keep copies under");
    2. not named like a copy (Backup, Old Laptop, Desktop dump, Export, Shared, Selected...);
    3. not a folder whose photos are (almost) all copies of photos in other folders, such as a
       selection picked from a card: the originals are kept, and the selection is saved as an album;
    4. a dated, named or camera folder ("2024-05 Lisbon", "DCIM/100_FUJI");
    5. the folder with the most photos that exist nowhere else;
    then the shallower path, a file name without "copy" or "(1)", and the oldest file."""

    def __init__(self, conn, sets: list[tuple[str, list[dict]]]):
        from .selections import MIN_SHARE

        self.manual = {r[0] for r in conn.execute("SELECT folder FROM keep_folders")}
        self.rules = keep_rules(conn)
        total: dict[str, int] = defaultdict(int)
        for (p,) in conn.execute("SELECT path FROM files WHERE status='present'"):
            total[os.path.dirname(p)] += 1
        twinned: dict[str, set] = defaultdict(set)       # files with a copy in another folder
        for kind, ms in sets:
            if kind in REMOVABLE and len({os.path.dirname(m["path"]) for m in ms}) > 1:
                for m in ms:
                    twinned[os.path.dirname(m["path"])].add(m["file_id"])
        selections = {r[0] for r in conn.execute("SELECT folder FROM selections WHERE kind='selection'")}
        self.contained = {d for d, fs in twinned.items() if d in selections or len(fs) >= MIN_SHARE * max(1, total.get(d, 0))}
        self.unique = {d: total.get(d, 0) - len(twinned.get(d, ())) for d in total}

    def user(self, d: str) -> int:
        return 0 if d in self.manual else 1 if any(_under(d, r) for r in self.rules) else 2

    @staticmethod
    def good(d: str) -> bool:
        from .events import generic_folder

        for part in d.rstrip("/").split("/")[-2:]:
            if _DATED.match(part) or _CAMERA.match(part):
                return True
            if not generic_folder(part) and sum(ch.isalpha() for ch in part) >= 3 and not _AVOID.search(_words(part)):
                return True
        return False

    def key(self, m: dict, base: str) -> tuple:
        d = os.path.dirname(m["path"])
        # Only the folders below the set's common folder tell its copies apart.
        below = d[len(base):].strip("/").split("/") if _under(d, base) else d.split("/")
        stem = os.path.splitext(os.path.basename(m["path"]))[0]
        return (self.user(d), any(_AVOID.search(_words(x)) for x in below if x), d in self.contained,
                not self.good(d), -self.unique.get(d, 0), d.count("/"), bool(_COPY_NAME.search(stem)), m["file_id"])


def choose_keepers(conn, sets: list[tuple[str, list[dict]]], rank: _Ranker | None = None) -> list[tuple[str, list[tuple[int, int, int]]]]:
    """Mark the copy to keep in each set: for identical files, the one in the best folder (see
    _Ranker); for re-saved copies, the best-quality file, and among files as good as each other the
    one in the best folder. Edited versions and RAW+JPEG pairs keep their first member (they're
    never removed). Members are {file_id, shot_id, path, q}; returns (kind, [(file_id, shot_id, keep)])."""
    rank = rank or _Ranker(conn, sets)
    out = []
    for kind, ms in sets:
        keeper = ms[0] if ms else None
        if kind in REMOVABLE and len(ms) > 1:
            pool = ms
            if kind == "copy":
                best = max(m["q"] for m in ms)
                pool = [m for m in ms if m["q"][:2] == best[:2] and m["q"][2] >= NEAR_SIZE * best[2]]
            base = os.path.commonpath([os.path.dirname(m["path"]) or "/" for m in ms])
            keeper = min(pool, key=lambda m: rank.key(m, base))
        out.append((kind, [(m["file_id"], m["shot_id"], int(m is keeper)) for m in ms]))
    return out


def rekeep(conn) -> int:
    """Choose the kept copy of every set again (after a rule changes), without looking for
    duplicates again. Returns how many sets now keep a different copy."""
    rows = conn.execute(
        "SELECT dm.rowid AS rid, ds.id AS set_id, ds.kind, dm.file_id, dm.shot_id, dm.keep, f.path, s.width, s.height,"
        " (SELECT MAX(f2.kind='raw') FROM files f2 WHERE f2.shot_id = dm.shot_id AND f2.status='present') AS has_raw,"
        " (SELECT SUM(f2.size) FROM files f2 WHERE f2.shot_id = dm.shot_id AND f2.status='present') AS bytes"
        " FROM dup_members dm JOIN dup_sets ds ON ds.id = dm.set_id JOIN files f ON f.id = dm.file_id"
        f" LEFT JOIN shots s ON s.id = dm.shot_id WHERE ds.kind IN ({','.join('?' * len(REMOVABLE))}) AND f.status='present'"
        " ORDER BY ds.id, dm.rowid", REMOVABLE).fetchall()
    by_set: dict[int, list] = defaultdict(list)
    for r in rows:
        by_set[r["set_id"]].append(r)
    ids = list(by_set)
    sets = [(by_set[i][0]["kind"], [{"file_id": r["file_id"], "shot_id": r["shot_id"], "path": r["path"], "rid": r["rid"],
                                     "q": (r["has_raw"] or 0, (r["width"] or 0) * (r["height"] or 0), r["bytes"] or 0)}
                                    for r in by_set[i]]) for i in ids]
    was = {r["rid"]: r["keep"] for r in rows}
    changed, updates = set(), []
    rank = _Ranker(conn, sets)
    for set_id, (_, ms), (_, chosen) in zip(ids, sets, choose_keepers(conn, sets, rank)):
        for m, (_, _, keep) in zip(ms, chosen):
            if was[m["rid"]] != keep:
                updates.append((keep, m["rid"]))
                changed.add(set_id)
    conn.executemany("UPDATE dup_members SET keep=? WHERE rowid=?", updates)
    conn.commit()
    choose_shown(conn, rank)
    return len(changed)


def choose_shown(conn, rank: _Ranker | None = None) -> int:
    """Which copy of each photo file held on several drives of this library is the one shown (the
    others are its backups, left out of culling, stacks and events): the best-placed of the copies
    the drives keep, never one that's an extra copy on its own drive. Run after the kept copies
    change. Returns how many backup sets now show a different copy."""
    rows = conn.execute(
        "SELECT dm.rowid AS rid, dm.set_id, dm.file_id, dm.keep, f.path FROM dup_members dm"
        " JOIN dup_sets ds ON ds.id = dm.set_id JOIN files f ON f.id = dm.file_id"
        " WHERE ds.kind = 'backup' AND f.status = 'present' ORDER BY dm.set_id, dm.rowid").fetchall()
    if not rows:
        return 0
    extra = {r[0] for r in conn.execute(
        "SELECT dm.file_id FROM dup_members dm JOIN dup_sets ds ON ds.id = dm.set_id WHERE ds.kind = 'exact' AND dm.keep = 0")}
    rank = rank or _Ranker(conn, [])
    by_set: dict[int, list] = defaultdict(list)
    for r in rows:
        by_set[r["set_id"]].append(r)
    updates, changed = [], 0
    for ms in by_set.values():
        pool = [m for m in ms if m["file_id"] not in extra] or ms
        base = os.path.commonpath([os.path.dirname(m["path"]) or "/" for m in ms])
        shown = min(pool, key=lambda m: rank.key({"path": m["path"], "file_id": m["file_id"]}, base))
        moved = False
        for m in ms:
            want = int(m is shown)
            if m["keep"] != want:
                updates.append((want, m["rid"]))
                moved = True
        changed += moved
    if updates:
        conn.executemany("UPDATE dup_members SET keep=? WHERE rowid=?", updates)
    conn.commit()
    return changed


def backup_copy_shot_ids(conn) -> set[int]:
    """Photos whose files are all backups of a copy shown elsewhere in this library, on another
    drive (and not extra copies on their own drive, which duplicate_shot_ids has). They make no
    stack, event or culling decision of their own, and are never suggested for rejection."""
    rows = conn.execute(
        "SELECT dm.shot_id, dm.file_id, dm.keep, ds.kind FROM dup_members dm JOIN dup_sets ds ON ds.id = dm.set_id"
        " WHERE ds.kind IN ('exact', 'backup') AND dm.shot_id IS NOT NULL").fetchall()
    behind = {r["file_id"] for r in rows if not r["keep"]}            # an extra on its drive, or a backup copy
    shown = {r["file_id"] for r in rows if r["kind"] == "backup" and r["keep"]}
    candidates = {r["shot_id"] for r in rows if r["kind"] == "backup" and not r["keep"]}
    if not candidates:
        return set()
    out = set()
    for sid in candidates:
        present = {f[0] for f in conn.execute("SELECT id FROM files WHERE shot_id=? AND status='present'", (sid,))}
        if present and present <= behind and not (present & shown):
            out.add(sid)
    return out - duplicate_shot_ids(conn)


def extras(conn, set_ids: list[int]) -> list[str]:
    """Paths of the extra (non-kept) files in these sets, only ever from removable kinds."""
    if not set_ids:
        return []
    out = []
    for i in range(0, len(set_ids), 900):
        chunk = [int(x) for x in set_ids[i : i + 900]]
        out += [r[0] for r in conn.execute(
            f"SELECT f.path FROM dup_members dm JOIN dup_sets ds ON ds.id = dm.set_id JOIN files f ON f.id = dm.file_id"
            f" WHERE dm.keep = 0 AND f.status = 'present' AND ds.kind IN ({','.join('?' * len(REMOVABLE))})"
            f" AND dm.set_id IN ({','.join('?' * len(chunk))})", (*REMOVABLE, *chunk))]
    return sorted(set(out))


def combine_decisions(conn, set_ids: list[int], viewer: str | None = None) -> tuple[str, list[int]]:
    """Give each kept copy the best decision any of its copies has (stars, pick), so nothing you
    decided on an extra is lost. Logged as one undoable step; never lowers a decision."""
    from . import library

    items = []
    for sid in set_ids:
        rows = conn.execute(
            "SELECT dm.shot_id, dm.keep, COALESCE(d.rating, 0) AS rating, COALESCE(d.pick, 0) AS pick FROM dup_members dm"
            " JOIN dup_sets ds ON ds.id = dm.set_id LEFT JOIN decisions d ON d.shot_id = dm.shot_id"
            f" WHERE dm.set_id = ? AND ds.kind IN ({','.join('?' * len(REMOVABLE))})", (int(sid), *REMOVABLE)).fetchall()
        keep = next((r for r in rows if r["keep"]), None)
        if not keep:
            continue
        best_rating = max(r["rating"] for r in rows)
        picked = any(r["pick"] == 1 for r in rows)
        change = {}
        if best_rating > keep["rating"]:
            change["rating"] = best_rating
        if picked and keep["pick"] != 1:
            change["pick"] = 1
        if change:
            items.append({"id": keep["shot_id"], **change})
    return library.decide(conn, items, what="combine duplicate ratings", source="web", viewer=viewer)


def extra_copies(conn, set_ids: list[int]) -> dict:
    """The extra copies in these sets that could go: identical files and re-saved copies only, and
    never a photo that is the kept copy of any set. {"files": {photo id: [(path, bytes)] of its extra
    files in these sets}, "sets": the sets they're extras in, "kept_skipped"/"unique_skipped": photos
    left out because they're a kept copy elsewhere, or have a file with no copy}.
    Rejecting extras and moving them to the Trash both count from this, so their numbers agree."""
    ids = sorted({int(x) for x in set_ids})
    files: dict[int, dict[str, int]] = defaultdict(dict)
    in_sets: dict[int, set] = defaultdict(set)
    kinds = ",".join("?" * len(REMOVABLE))
    for i in range(0, len(ids), 900):
        chunk = ids[i : i + 900]
        for set_id, shot, path, size in conn.execute(
                f"SELECT dm.set_id, dm.shot_id, f.path, f.size FROM dup_members dm JOIN dup_sets ds ON ds.id = dm.set_id"
                f" JOIN files f ON f.id = dm.file_id WHERE dm.keep = 0 AND dm.shot_id IS NOT NULL AND f.status = 'present'"
                f" AND ds.kind IN ({kinds}) AND dm.set_id IN ({','.join('?' * len(chunk))})", (*REMOVABLE, *chunk)):
            files[shot][path] = size or 0
            in_sets[shot].add(set_id)
    kept = {r[0] for r in conn.execute("SELECT DISTINCT shot_id FROM dup_members WHERE keep = 1 AND shot_id IS NOT NULL")}
    skipped = [s for s in files if s in kept]
    for s in skipped:
        files.pop(s)
        in_sets.pop(s)
    # A photo is rejected as a whole (its RAW and its JPEG together), so it's an extra only when every
    # file it has is an extra copy. A unique RAW beside a duplicated JPEG keeps the photo.
    extra_files = {r[0] for r in conn.execute(
        f"SELECT dm.file_id FROM dup_members dm JOIN dup_sets ds ON ds.id = dm.set_id"
        f" WHERE dm.keep = 0 AND ds.kind IN ({kinds})", REMOVABLE)}
    unique = []
    for s in list(files):
        own = [r[0] for r in conn.execute("SELECT id FROM files WHERE shot_id = ? AND status = 'present'", (s,))]
        if any(f not in extra_files for f in own):
            unique.append(s)
            files.pop(s)
            in_sets.pop(s)
    return {"files": {s: sorted(fs.items()) for s, fs in files.items()}, "sets": set().union(*in_sets.values()),
            "kept_skipped": len(skipped), "unique_skipped": len(unique)}


def rejected_of(conn, shot_ids: list[int]) -> set[int]:
    out: set[int] = set()
    for i in range(0, len(shot_ids), 900):
        chunk = shot_ids[i : i + 900]
        out |= {r[0] for r in conn.execute(
            f"SELECT shot_id FROM decisions WHERE pick = -1 AND shot_id IN ({','.join('?' * len(chunk))})", chunk)}
    return out


def files_in_folder(conn, folder: str) -> int:
    """How many of the library's files are directly in this folder."""
    return conn.execute("SELECT COUNT(*) FROM files WHERE status='present' AND path >= ? AND path < ?"
                        " AND instr(substr(path, ?), '/') = 0", (folder + "/", folder + "0", len(folder) + 2)).fetchone()[0]


def folder_losses(conn, paths: list[str]) -> list[dict]:
    """For each folder these files are in, most first: how many files it loses, and how many of the
    library's files stay in it (0: the folder is emptied)."""
    out = []
    for d, k in sorted(Counter(os.path.dirname(p) for p in paths).items(), key=lambda x: (-x[1], x[0])):
        total = max(files_in_folder(conn, d), k)
        out.append({"path": d, "loses": k, "keeps": total - k, "total": total})
    return out


def reject_extras(conn, set_ids: list[int], combine: bool = True, viewer: str | None = None,
                  dry: bool = False) -> dict:
    """Mark every extra copy in these sets rejected, whole sets at a time (not just the ones on
    screen): identical files and re-saved copies only, never edited versions or RAW+JPEG halves, and
    never a photo that is the kept copy of any set. With combine, each kept copy first gets the best
    stars and pick of its copies. One step in the history: one Undo puts everything back."""
    from . import library

    ids = sorted({int(x) for x in set_ids})
    ex = extra_copies(conn, ids)
    sets, extras = ex["sets"], ex["files"]
    already = rejected_of(conn, list(extras))
    todo = [s for s in extras if s not in already]
    paths = [p for fs in extras.values() for p, _ in fs]
    out = {"sets": len(sets), "extras": len(extras), "to_reject": len(todo), "already": len(already),
           "kept_skipped": ex["kept_skipped"], "unique_skipped": ex["unique_skipped"], "files": len(paths), "bytes": sum(b for fs in extras.values() for _, b in fs),
           "folders": folder_losses(conn, paths), "rejected": 0, "combined": 0, "batch": None}
    if dry:
        return out
    batch = None
    if combine:
        batch, combined = combine_decisions(conn, ids, viewer=viewer)
        out["combined"] = len(combined)
    if todo:
        batch, changed = library.decide(conn, [{"id": s, "pick": -1} for s in todo],
                                        what=f"reject duplicate extras ({len(sets)} sets)"[:60], source="web",
                                        viewer=viewer, batch=batch)
        out["rejected"] = len(changed)
    out["batch"] = batch if (out["rejected"] or out["combined"]) else None
    return out


def duplicate_shot_ids(conn) -> set[int]:
    """Shots that are a redundant copy of another (every file is a non-kept duplicate)."""
    rows = conn.execute(
        "SELECT dm.shot_id, ds.kind, dm.keep, dm.file_id FROM dup_members dm JOIN dup_sets ds ON ds.id = dm.set_id"
    ).fetchall()
    copies = {r["shot_id"] for r in rows if r["kind"] == "copy" and not r["keep"]}
    exact_nonkeep = defaultdict(set)
    for r in rows:
        if r["kind"] == "exact" and not r["keep"]:
            exact_nonkeep[r["shot_id"]].add(r["file_id"])
    for shot_id, fids in exact_nonkeep.items():
        present = {f[0] for f in conn.execute("SELECT id FROM files WHERE shot_id=? AND status='present'", (shot_id,))}
        if present and present <= fids:
            copies.add(shot_id)
    return copies


# ---------------------------------------------------------------- folder view

def folder_groups(conn, kinds: tuple[str, ...] = ("exact", "copy")) -> dict:
    """Duplicates grouped by the set of folders they occur in, with how much of each folder
    is duplicated. Answers "which folders duplicate each other, and how badly"."""
    if not kinds:
        return {"groups": [], "totals": {}, "rules": keep_rules(conn)}
    q = ("SELECT ds.id, ds.kind, dm.keep, f.path, f.size FROM dup_members dm JOIN dup_sets ds ON ds.id = dm.set_id"
         f" JOIN files f ON f.id = dm.file_id WHERE ds.kind IN ({','.join('?' * len(kinds))})")
    sets: dict[int, list] = defaultdict(list)
    kind_of: dict[int, str] = {}
    for r in conn.execute(q, kinds):
        sets[r["id"]].append(r)
        kind_of[r["id"]] = r["kind"]
    folder_total: dict[str, int] = defaultdict(int)
    for (p,) in conn.execute("SELECT path FROM files WHERE status='present'"):
        folder_total[os.path.dirname(p)] += 1
    groups: dict[tuple, dict] = {}
    for sid, ms in sets.items():
        folders = tuple(sorted({os.path.dirname(m["path"]) for m in ms}))
        g = groups.setdefault(folders, {"sets": 0, "set_ids": [], "kinds": defaultdict(int), "files": 0,
                                        "extra_files": 0, "extra_bytes": 0, "in_folder": defaultdict(set),
                                        "keep_in": defaultdict(int), "extra_in": defaultdict(int), "smaller": 0})
        g["sets"] += 1
        sizes = [m["size"] or 0 for m in ms]
        if kind_of[sid] == "copy" and min(sizes) and max(sizes) >= 1.5 * min(sizes):
            g["smaller"] += 1                 # the same edit saved again as a much smaller file
        g["set_ids"].append(sid)
        g["kinds"][kind_of[sid]] += 1
        g["files"] += len(ms)
        for m in ms:
            d = os.path.dirname(m["path"])
            g["in_folder"][d].add(m["path"])   # distinct files: one file can be in two sets
            if m["keep"]:
                g["keep_in"][d] += kind_of[sid] in REMOVABLE
            else:
                g["extra_files"] += 1
                g["extra_bytes"] += m["size"]
                g["extra_in"][d] += kind_of[sid] in REMOVABLE
    keep_pref = {r[0] for r in conn.execute("SELECT folder FROM keep_folders")}
    rules = keep_rules(conn)
    out = []
    for folders, g in groups.items():
        out.append({
            # kept_here / removed_here: the copies kept in this folder, and the extras in it
            # (identical files and re-saved copies only: versions and RAW+JPEG halves are neither).
            "folders": [{"path": f, "dup_files": len(g["in_folder"][f]), "total_files": folder_total.get(f, 0),
                         "pct": min(100, round(100 * len(g["in_folder"][f]) / max(1, folder_total.get(f, 0)))),
                         "kept_here": g["keep_in"][f], "removed_here": g["extra_in"][f], "preferred": f in keep_pref,
                         "rule": next((r for r in rules if _under(f, r)), None)} for f in folders],
            "sets": g["sets"], "set_ids": g["set_ids"], "kinds": dict(g["kinds"]), "files": g["files"],
            "extra_files": g["extra_files"], "extra_bytes": g["extra_bytes"], "smaller": g["smaller"],
        })
    out.sort(key=lambda g: -g["extra_bytes"])
    totals = {"groups": len(out), "sets": len(sets), "extra_files": sum(g["extra_files"] for g in out),
              "extra_bytes": sum(g["extra_bytes"] for g in out)}
    return {"groups": out, "totals": totals, "rules": rules}


def prefer_folder(conn, folder: str, set_ids: list[int]) -> int:
    """Keep the copies in `folder` for these sets (identical files only; for re-saved copies the
    best-quality file always stays the keeper). Remembered for future rescans."""
    sets = defaultdict(list)
    rows = conn.execute(
        f"SELECT dm.rowid AS rid, dm.set_id, f.path FROM dup_members dm JOIN dup_sets ds ON ds.id = dm.set_id"
        f" JOIN files f ON f.id = dm.file_id WHERE ds.kind = 'exact' AND dm.set_id IN ({','.join('?' * len(set_ids))})",
        set_ids).fetchall() if set_ids else []
    for r in rows:
        sets[r["set_id"]].append(r)
    others = {os.path.dirname(r["path"]) for r in rows} - {folder}
    conn.executemany("DELETE FROM keep_folders WHERE folder = ?", [(o,) for o in others])
    conn.execute("INSERT OR IGNORE INTO keep_folders(folder) VALUES (?)", (folder,))
    changed = 0
    for ms in sets.values():
        here = [m for m in ms if os.path.dirname(m["path"]) == folder]
        if not here:
            continue
        for m in ms:
            conn.execute("UPDATE dup_members SET keep=? WHERE rowid=?", (int(m["rid"] == here[0]["rid"]), m["rid"]))
        changed += 1
    conn.commit()
    if changed:
        choose_shown(conn)          # a copy shown from another drive may be an extra copy on its own drive now
    return changed
