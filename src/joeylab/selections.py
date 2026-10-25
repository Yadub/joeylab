"""Selection folders: a folder that is a hand-picked subset of another, stored as full copies
("2025-12-20 Alex and Sam's Wedding" picked from "SD Card 20251221").

Each selection is saved as an album whose items point at the ORIGINAL files, so the selection
survives even if you later delete the copies. Two folders that fully contain each other are
"mirrors" (a whole folder copied twice). Everything here is read-only on your photo folders,
except link folders, which are made only on this Mac's own disk.
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from . import db

MIN_FILES = 5          # ignore tiny overlaps
MIN_SHARE = 0.95       # a selection: at least this share of its photos are also in the parent
KIND_ORDER = {"exact": 0, "copy": 1}


def _folder_counts(conn) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for (p,) in conn.execute("SELECT path FROM files WHERE status='present'"):
        counts[os.path.dirname(p)] += 1
    return counts


def detect(conn) -> list[dict]:
    """Pairs (folder, parent) where `folder` is (almost) entirely contained in `parent`."""
    rows = conn.execute(
        "SELECT ds.id AS set_id, ds.kind, f.id AS file_id, f.path, f.size, f.shot_id FROM dup_members dm"
        " JOIN dup_sets ds ON ds.id = dm.set_id JOIN files f ON f.id = dm.file_id"
        " WHERE ds.kind IN ('exact', 'copy') AND f.status = 'present'").fetchall()
    sets: dict[int, list] = defaultdict(list)
    for r in rows:
        sets[r["set_id"]].append(r)
    # (folder, parent) -> {file in folder: (its twin in parent, kind)}; an exact twin beats a re-saved one
    pairs: dict[tuple, dict] = defaultdict(dict)
    for members in sets.values():
        for a in members:
            for b in members:
                fa, fb = os.path.dirname(a["path"]), os.path.dirname(b["path"])
                if fa == fb:
                    continue
                prev = pairs[(fa, fb)].get(a["file_id"])
                if prev is None or KIND_ORDER[a["kind"]] < KIND_ORDER[prev[2]]:
                    pairs[(fa, fb)][a["file_id"]] = (a, b, a["kind"])
    counts = _folder_counts(conn)
    out, seen_mirrors = [], set()
    for (folder, parent), m in pairs.items():
        n_folder, n_parent = counts.get(folder, 0), counts.get(parent, 0)
        if len(m) < MIN_FILES or not n_folder or len(m) / n_folder < MIN_SHARE:
            continue
        parent_hit = len({b["file_id"] for (_, b, _) in m.values()})
        mirror = n_parent and parent_hit / n_parent >= MIN_SHARE
        if mirror:
            key = tuple(sorted((folder, parent)))
            if key in seen_mirrors:
                continue
            seen_mirrors.add(key)
        identical = [(a, b) for (a, b, k) in m.values() if k == "exact"]
        out.append({
            "kind": "mirror" if mirror else "selection", "folder": folder, "parent": parent,
            "folder_files": n_folder, "parent_files": n_parent, "matched": len(m),
            "identical": len(identical), "identical_bytes": sum(a["size"] for a, _ in identical),
            "items": [(a, b, k) for (a, b, k) in m.values()],
        })
    # A folder picked from two overlapping dumps is listed once: under the parent it matches
    # best (then the bigger one), with the others noted.
    best: dict[str, dict] = {}
    for s in out:
        if s["kind"] != "selection":
            continue
        cur = best.get(s["folder"])
        if cur is None or (s["identical"], s["parent_files"]) > (cur["identical"], cur["parent_files"]):
            if cur:
                s.setdefault("also_in", []).extend([cur["parent"], *cur.get("also_in", [])])
            best[s["folder"]] = s
        else:
            cur.setdefault("also_in", []).append(s["parent"])
    out = [s for s in out if s["kind"] == "mirror"] + list(best.values())
    out.sort(key=lambda s: -s["identical_bytes"])
    return out


def update(conn) -> dict:
    """Recompute selections and keep their albums up to date. Albums are never removed
    automatically: a selection whose copies you deleted keeps its album."""
    found = detect(conn)
    now = time.time()
    conn.execute("DELETE FROM selections")
    for s in found:
        album_id = None
        if s["kind"] == "selection":
            row = conn.execute("SELECT id FROM albums WHERE folder = ?", (s["folder"],)).fetchone()
            if row:
                album_id = row[0]
                conn.execute("UPDATE albums SET parent = ?, updated = ? WHERE id = ?", (s["parent"], now, album_id))
            else:
                name = os.path.basename(s["folder"])
                if conn.execute("SELECT 1 FROM albums WHERE name = ?", (name,)).fetchone():
                    # two selections with one name (a sorted folder and its copy in a backup): say where
                    name = f"{name} ({os.path.basename(os.path.dirname(s['folder']))})"
                album_id = db.new_id(conn, "albums")
                conn.execute(
                    "INSERT INTO albums(id, name, kind, folder, parent, created, updated) VALUES (?, ?, 'selection', ?, ?, ?, ?)",
                    (album_id, name, s["folder"], s["parent"], now, now))
            conn.executemany(
                "INSERT INTO album_items(album_id, file_id, shot_id, name, source_path) VALUES (?,?,?,?,?)"
                " ON CONFLICT(album_id, name) DO UPDATE SET file_id = excluded.file_id, shot_id = excluded.shot_id,"
                " source_path = excluded.source_path",
                [(album_id, b["file_id"], b["shot_id"], os.path.basename(a["path"]), a["path"]) for a, b, _ in s["items"]])
        conn.execute(
            "INSERT INTO selections(kind, folder, parent, folder_files, parent_files, matched, identical, identical_bytes,"
            " album_id, also_in) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (s["kind"], s["folder"], s["parent"], s["folder_files"], s["parent_files"], s["matched"],
             s["identical"], s["identical_bytes"], album_id, json.dumps(s.get("also_in", []))))
    conn.commit()
    return {"selections": sum(s["kind"] == "selection" for s in found), "mirrors": sum(s["kind"] == "mirror" for s in found)}


def link_plan_csv(conn, selection_id: int) -> tuple[str, str]:
    """What linking this selection would do (read-only): one row per copy in the selection."""
    sel = conn.execute("SELECT * FROM selections WHERE id = ?", (selection_id,)).fetchone()
    if not sel:
        raise KeyError(selection_id)
    match = {a["file_id"]: (a, b, k) for a, b, k in
             next((s["items"] for s in detect(conn) if s["folder"] == sel["folder"] and s["parent"] == sel["parent"]), [])}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["copy_in_selection", "original", "size_bytes", "byte_identical", "would_become_link"])
    for a, b, k in sorted(match.values(), key=lambda x: x[0]["path"]):
        w.writerow([a["path"], b["path"], a["size"], k == "exact", k == "exact"])
    return buf.getvalue(), os.path.basename(sel["folder"])


def make_link_folder(conn, album_id: int, dest: str) -> dict:
    """Make a folder of symbolic links to the album's original files, named as they were in
    the selection. Only on this Mac's own disk: never on an external drive."""
    from . import config

    from .export import on_this_macs_disk, within

    if not dest or not (dest.strip().startswith("/") or dest.strip().startswith("~")):
        raise ValueError("Give a full folder path, for example ~/Pictures/Selections/Wedding.")
    dest_path = os.path.realpath(os.path.expanduser(dest.strip()))
    if not on_this_macs_disk(Path(dest_path)):
        raise ValueError("Link folders are made on this Mac's own disk only, so external drives are never written to.")
    if within(Path(dest_path), config.HOME):
        raise ValueError("Choose a folder outside JoeyLab's data folder.")
    items = conn.execute(
        "SELECT ai.name, f.path, f.status FROM album_items ai JOIN files f ON f.id = ai.file_id WHERE ai.album_id = ?"
        " ORDER BY ai.name", (album_id,)).fetchall()
    from .export import check_dest

    dest_path = str(check_dest(Path(dest_path)))
    os.makedirs(dest_path, exist_ok=True)
    made = skipped = missing = 0
    now = time.time()
    for it in items:
        if it["status"] != "present":
            missing += 1
            continue
        link = os.path.join(dest_path, it["name"])
        if os.path.lexists(link):
            skipped += 1          # already there, or a real file / someone else's link: never replaced
            continue
        os.symlink(it["path"], link)
        conn.execute("INSERT OR REPLACE INTO album_links(dest, name, target, created) VALUES (?,?,?,?)",
                     (dest_path, it["name"], it["path"], now))
        made += 1
    conn.commit()
    return {"dest": dest_path, "made": made, "skipped": skipped, "missing": missing}
