"""Library health: one to-do list for tidying, all read-only.

Duplicates, photos that exist on only one drive (not backed up), files JoeyLab couldn't read,
undated photos, thumbnails and fragments, files whose contents don't match their name, sidecars
whose photo is gone, "to sort" folders, drives and when they were last seen, the "untouched"
check, and catalog backups.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict

from . import backup, config, exif, integrity

TO_SORT = re.compile(r"to[ _-]?sort|unsorted|misc|new folder|untitled|temp|recovered", re.I)


def last_seen(conn) -> dict[str, float]:
    """When each tracked folder was last actually scanned (an unplugged drive keeps its old date)."""
    out: dict[str, float] = {}
    for summary, finished in conn.execute(
            "SELECT summary, finished FROM scans WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 400"):
        try:
            for r in json.loads(summary or "{}").get("roots_scanned", []):
                out.setdefault(r, finished)
        except ValueError:
            continue
    return out


def backup_coverage(conn, project: str) -> dict:
    """Which files exist on only one drive, from the index every library shares (copies.py). A copy
    in another folder of the same drive is not a backup; a byte-identical copy on another drive, in
    this library or any other, is (`checked` counts those compared byte for byte)."""
    from . import copies, volumes

    is_known = copies.ensure(conn, project)
    drives = volumes.root_drives(conn)
    single: dict[str, int] = defaultdict(int)
    single_bytes: dict[str, int] = defaultdict(int)
    covered = checked = total = 0
    examples = []
    for size, root_id, sid, ck in conn.execute(
            "SELECT f.size, f.root_id, f.shot_id, b.checked FROM files f LEFT JOIN backups b ON b.file_id = f.id"
            " WHERE f.status='present'"):
        total += 1
        if ck is not None:
            covered += 1
            checked += ck
        else:
            d = drives.get(root_id)
            v = d["name"] if d else "?"
            single[v] += 1
            single_bytes[v] += size
            if len(examples) < 12:
                examples.append(sid)
    return {"files": total, "backed_up": covered, "checked": checked, "only_one_copy": total - covered, "known": is_known,
            "by_drive": [{"drive": v, "files": n, "bytes": single_bytes[v]} for v, n in sorted(single.items(), key=lambda x: -x[1])],
            "examples": examples, "libraries": len(config.list_projects())}


def orphan_sidecars(conn, limit: int = 200) -> list[str]:
    """.xmp files whose photo is no longer next to them (only folders that hold photos are looked at)."""
    out = []
    dirs = [r[0] for r in conn.execute("SELECT DISTINCT dir FROM shots WHERE status='present'")]
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        stems = {os.path.splitext(n)[0].lower() for n in names if not n.lower().endswith(".xmp")}
        full = {n.lower() for n in names}
        for n in names:
            if n.lower().endswith(".xmp") and not n.startswith("."):
                base = n[:-4]
                if base.lower() not in stems and base.lower() not in full:
                    out.append(os.path.join(d, n))
                    if len(out) >= limit:
                        return out
    return out


def report(conn, project: str, deep: bool = False) -> dict:
    one = lambda q, a=(): conn.execute(q, a).fetchone()[0]  # noqa: E731
    present = "status='present'"
    dup = conn.execute(
        "SELECT COUNT(DISTINCT ds.id), COUNT(*), COALESCE(SUM(f.size),0) FROM dup_members dm JOIN dup_sets ds ON ds.id=dm.set_id"
        " JOIN files f ON f.id=dm.file_id WHERE dm.keep=0 AND ds.kind IN ('exact','copy') AND f.status='present'").fetchone()
    failed = [dict(r) for r in conn.execute(
        f"SELECT s.id, s.dir, s.stem, s.error FROM shots s WHERE s.{present} AND s.error IS NOT NULL AND s.analyzed_fp IS NULL LIMIT 50")]
    flags = {r[0]: r[1] for r in conn.execute(
        "SELECT j.value, COUNT(*) FROM metrics m JOIN shots s ON s.id=m.shot_id, json_each(m.flags) j"
        f" WHERE s.{present} AND j.value IN ('tiny','utility','rescuable') GROUP BY j.value")}
    # Files whose contents aren't what their names say: each kind of file and name checked once, then
    # the files of the kinds that are wrong (reading every file's metadata here took a large share).
    has_type = f"{present} AND meta IS NOT NULL AND json_valid(meta)"
    bad = [(ft, ext) for ft, ext in conn.execute(
        f"SELECT DISTINCT json_extract(meta, '$.filetype'), ext FROM files WHERE {has_type}") if exif.wrong_extension(ft, ext)]
    wrong = [{"path": path, "is": ft} for path, ft in conn.execute(
        f"SELECT path, json_extract(meta, '$.filetype') FROM files WHERE {has_type} AND ("
        + " OR ".join(["(json_extract(meta, '$.filetype') IS ? AND ext = ?)"] * len(bad)) + ") ORDER BY id",
        [x for b in bad for x in b])] if bad else []
    to_sort = [{"dir": r[0], "n": r[1]} for r in conn.execute(
        f"SELECT dir, COUNT(*) FROM shots WHERE {present} GROUP BY dir ORDER BY 2 DESC") if TO_SORT.search(r[0])]
    last_scan = conn.execute("SELECT summary, finished FROM scans WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    scan = json.loads(last_scan[0]) if last_scan and last_scan[0] else {}
    # The photos' own counts, in one pass over them (each was a pass of its own).
    c = conn.execute(
        "SELECT SUM(error IS NOT NULL AND analyzed_fp IS NULL), SUM(error LIKE 'Damaged file%'), SUM(capture_ts IS NULL),"
        " SUM(media='video'), SUM(c2pa IS NOT NULL), SUM(edited=1) FROM shots WHERE +status='present'").fetchone()
    c = [x or 0 for x in c]
    seen = last_seen(conn)
    drives = []
    for r, vname, vuuid in conn.execute("SELECT path, volume_name, volume_uuid FROM roots"):
        drives.append({"path": r, "available": os.path.isdir(r), "last_seen": seen.get(r),
                       "mount": integrity.mount_info(r) if os.path.isdir(r) else {},
                       "drive": vname, "known_by_id": bool(vuuid)})
    out = {
        "duplicates": {"sets": dup[0], "extra_files": dup[1], "bytes": dup[2]},
        "versions": one("SELECT COUNT(*) FROM dup_sets WHERE kind='export'"),
        "pairs_apart": one("SELECT COUNT(*) FROM dup_sets WHERE kind='pair'"),
        "missing": one(f"SELECT COUNT(*) FROM shots WHERE status='missing'"),
        "failed": {"n": c[0], "examples": failed},
        # Damaged files shown from what could be read (often a recovered card), and how many of them
        # have an intact copy with the same name elsewhere.
        "damaged": {"n": c[1],
                    "with_copy": one(f"SELECT COUNT(*) FROM shots s WHERE s.status='present' AND s.error LIKE 'Damaged file%'"
                                     " AND EXISTS (SELECT 1 FROM shots o WHERE o.id != s.id AND o.status='present'"
                                     " AND lower(o.stem) = lower(s.stem) AND o.analyzed_fp IS NOT NULL AND o.error IS NULL)")
                    if c[1] else 0},
        "undated": c[2],
        "tiny": flags.get("tiny", 0), "utility": flags.get("utility", 0), "rescuable": flags.get("rescuable", 0),
        "wrong_extension": {"n": len(wrong), "examples": wrong[:50]},
        "to_sort": to_sort,
        "scan": {"finished": last_scan[1] if last_scan else None, "errors": scan.get("errors", 0),
                 "unreadable": scan.get("unreadable", 0), "settling": scan.get("settling", 0)},
        "drives": drives,
        "untouched": integrity.latest(conn),
        "backups": backup.list_backups(project)[:10],
        "videos": c[3], "c2pa": c[4], "edited": c[5],
    }
    from . import copies

    copies.ensure(conn, project)
    out["copies"] = copies.counts(conn)          # photos with a backup on another drive, and only on this one
    if deep:
        out["coverage"] = backup_coverage(conn, project)
        out["orphan_sidecars"] = orphan_sidecars(conn)
    return out


# ---------------------------------------------------------------- a plan you run yourself

def verify_extras(conn, paths: list[str]) -> list[dict]:
    """Right before you delete a list of duplicate extras: check each one still has an identical
    kept copy on disk (full content hash, read-only). Anything that isn't is reported."""
    from .hashing import full_hash

    out = []
    for p in paths:
        p = p.strip()
        if not p:
            continue
        row = conn.execute("SELECT id, fp, size FROM files WHERE path=?", (p,)).fetchone()
        if not row:
            out.append({"path": p, "ok": False, "why": "not in the catalog"})
            continue
        twin = conn.execute(
            "SELECT f2.path FROM dup_members dm JOIN dup_members dk ON dk.set_id = dm.set_id AND dk.keep = 1"
            " JOIN dup_sets ds ON ds.id = dm.set_id JOIN files f2 ON f2.id = dk.file_id"
            " WHERE dm.file_id = ? AND dm.keep = 0 AND ds.kind IN ('exact','copy')", (row["id"],)).fetchone()
        if not twin:
            out.append({"path": p, "ok": False, "why": "not a duplicate extra any more"})
            continue
        try:
            same = os.path.exists(twin[0]) and os.path.getsize(p) == os.path.getsize(twin[0]) and full_hash(p) == full_hash(twin[0])
        except OSError as e:
            out.append({"path": p, "ok": False, "why": str(e)})
            continue
        ds_kind = conn.execute("SELECT ds.kind FROM dup_members dm JOIN dup_sets ds ON ds.id=dm.set_id WHERE dm.file_id=?",
                               (row["id"],)).fetchone()[0]
        if ds_kind == "copy":
            out.append({"path": p, "ok": os.path.exists(twin[0]), "kept": twin[0],
                        "why": "re-saved copy; the kept version is on disk" if os.path.exists(twin[0]) else "the kept copy is gone"})
        else:
            out.append({"path": p, "ok": same, "kept": twin[0], "why": "identical copy verified" if same else "the kept copy differs or is gone"})
    return out


def reorganise_plan(conn, scheme: str = "year/event") -> list[dict]:
    """A proposed tidy layout ("2023/2023-09 Cornwall - Family Holiday/"), for you to review and apply
    yourself. JoeyLab only writes the plan; it never moves a file."""
    rows = conn.execute(
        "SELECT f.path, s.capture_str, e.name AS event FROM files f JOIN shots s ON s.id = f.shot_id"
        " LEFT JOIN events e ON e.id = s.event_id WHERE f.status='present' ORDER BY s.eff_ts").fetchall()
    out = []
    for r in rows:
        year = (r["capture_str"] or "undated")[:4]
        ev = (r["event"] or "").replace("/", "-").replace(":", "-").strip() or "Unsorted"
        ym = (r["capture_str"] or "")[:7]
        folder = f"{year}/{ym} {ev.split(' · ')[0]}".strip() if scheme == "year/event" else year
        out.append({"from": r["path"], "to_folder": folder, "name": os.path.basename(r["path"])})
    return out
