"""Burst / similar-scene grouping along each camera body's timeline.

Group ids are stable: a re-run reuses the id of the group most of a new group's frames were in,
so links, tie-breaks and anything else that refers to a group keep pointing at the same frames.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from . import db
from .config import GROUPING_DEFAULT, GROUPING_LEVELS, GROUPING_NAMES
from .db import to_u64
from .hashing import hamming


def camera_key(camera: str | None, serial: str | None) -> str:
    return f"{camera or 'unknown camera'}|{serial or ''}"


def timeline(conn) -> tuple[dict[str, list], list[int]]:
    """Analysed, present shots per camera body, in shooting order, plus undated shots (no
    capture date at all: they are never grouped, since file times say nothing about bursts).
    Extra copies of a photo kept elsewhere, and its backups on another drive, are left out: a copy
    is not another frame."""
    from .dupes import backup_copy_shot_ids, duplicate_shot_ids

    extras = duplicate_shot_ids(conn) | backup_copy_shot_ids(conn)
    rows = conn.execute(
        "SELECT s.id, s.eff_ts, s.time_src, s.camera, s.body_serial, s.phash, s.dir, s.stem"
        " FROM shots s WHERE s.status='present' AND s.phash IS NOT NULL"
    ).fetchall()
    lines: dict[str, list] = defaultdict(list)
    undated = []
    for r in rows:
        if r["id"] in extras:
            continue
        if r["eff_ts"] is None or r["time_src"] == "file":
            undated.append(r["id"])
            continue
        lines[camera_key(r["camera"], r["body_serial"])].append((r["eff_ts"], r["dir"], r["stem"], r["id"], to_u64(r["phash"])))
    for v in lines.values():
        v.sort()
    return lines, undated


def level_of(conn) -> int:
    try:
        lv = int(db.get_setting(conn, "grouping", GROUPING_DEFAULT) or GROUPING_DEFAULT)
    except (TypeError, ValueError):
        lv = GROUPING_DEFAULT
    return lv if lv in GROUPING_LEVELS else GROUPING_DEFAULT


def _linked(fr, g, embs, t: dict) -> bool:
    dt = fr[0] - g[-1][0]
    ham = min(hamming(fr[4], x[4]) for x in g[-3:])
    if (dt <= t["burst_gap"] and ham <= t["burst_ham"]) or (dt <= t["similar_gap"] and ham <= t["similar_ham"]):
        return True
    e = embs.get(fr[3])
    if e is None or dt > max(t["scene_gap"], t["long_gap"]):
        return False
    cos = max((float(e @ embs[x[3]]) for x in g[-3:] if x[3] in embs), default=0.0)
    return (dt <= t["scene_gap"] and cos >= t["scene_cos"]) or (dt <= t["long_gap"] and cos >= t["long_cos"])


def _split(lines: dict, embs: dict, t: dict) -> list[tuple[str, list]]:
    out: list[tuple[str, list]] = []
    for key, frames in lines.items():
        groups: list[list] = []
        for fr in frames:
            if groups and _linked(fr, groups[-1], embs, t):
                groups[-1].append(fr)
            else:
                groups.append([fr])
        out += [(key, g) for g in groups]
    return out


_PREVIEW: dict[str, tuple] = {}


def preview(conn) -> dict:
    """What every strictness level would give, without changing anything: for the slider. Kept
    until the photos' times, cameras, hashes or vectors change (not worked out on every visit)."""
    from .ai import load_embeddings

    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    stamp = (db.counter(conn, "emb_gen"), *conn.execute(
        "SELECT COUNT(*), TOTAL(eff_ts), TOTAL(id * (time_src = 'file')), TOTAL(length(camera) + length(body_serial)),"
        " TOTAL(CAST(substr(phash, 1, 6) AS INTEGER)) FROM shots WHERE status='present' AND phash IS NOT NULL").fetchone(),
        conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='embeddings'").fetchone() else 0)
    hit = _PREVIEW.get(db_file)
    if hit and hit[0] == stamp:
        return hit[1]
    lines, _ = timeline(conn)
    embs = load_embeddings(conn)
    out = {}
    for lv, t in GROUPING_LEVELS.items():
        sizes = [len(g) for _, g in _split(lines, embs, t)]
        multi = [n for n in sizes if n > 1]
        out[lv] = {"name": GROUPING_NAMES[lv], "stacks": len(sizes), "multi": len(multi),
                   "in_multi": sum(multi), "largest": max(sizes, default=0)}
    _PREVIEW[db_file] = (stamp, out)
    return out


def run(conn) -> dict:
    from .ai import load_embeddings

    lines, undated = timeline(conn)
    embs = load_embeddings(conn)
    level = level_of(conn)
    old = {r[0]: r[1] for r in conn.execute("SELECT id, group_id FROM shots WHERE group_id IS NOT NULL")}
    new_groups = _split(lines, embs, GROUPING_LEVELS[level])

    # Reuse the old id that most of a group's frames had, biggest groups first. New groups get ids
    # above every id ever used.
    hi_old = conn.execute("SELECT COALESCE(MAX(id), 0) FROM groups").fetchone()[0]
    conn.execute("INSERT INTO meta(key, value) VALUES ('hwm:groups', ?) ON CONFLICT(key) DO UPDATE SET"
                 " value = MAX(CAST(value AS INTEGER), excluded.value)", (hi_old,))
    used: set[int] = set()
    order = sorted(range(len(new_groups)), key=lambda i: -len(new_groups[i][1]))
    chosen: dict[int, int] = {}
    for i in order:
        votes = Counter(old[fr[3]] for fr in new_groups[i][1] if fr[3] in old)
        for gid, _ in votes.most_common():
            if gid not in used:
                chosen[i] = gid
                used.add(gid)
                break
    # Write only what changed: a regrouping after one new photo touches a handful of rows, not every
    # photo in the library.
    before = {r[0]: tuple(r[1:]) for r in conn.execute("SELECT id, camera_key, start_ts, end_ts, size FROM groups")}
    member_of: dict[int, int] = {}
    rows: dict[int, tuple] = {}
    n_groups = n_multi = 0
    for i, (key, g) in enumerate(new_groups):
        gid = chosen.get(i) or db.new_id(conn, "groups")
        rows[gid] = (key, g[0][0], g[-1][0], len(g))
        for fr in g:
            member_of[fr[3]] = gid
        n_groups += 1
        n_multi += len(g) > 1
    gone = [gid for gid in before if gid not in rows]
    for k in range(0, len(gone), 900):
        chunk = gone[k : k + 900]
        conn.execute(f"DELETE FROM groups WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    conn.executemany("INSERT INTO groups(id, camera_key, start_ts, end_ts, size) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE"
                     " SET camera_key=excluded.camera_key, start_ts=excluded.start_ts, end_ts=excluded.end_ts, size=excluded.size",
                     [(gid, *r) for gid, r in rows.items() if before.get(gid) != r])
    conn.executemany("UPDATE shots SET group_id=NULL WHERE id=?", [(sid,) for sid in old if sid not in member_of])
    conn.executemany("UPDATE shots SET group_id=? WHERE id=?",
                     [(gid, sid) for sid, gid in member_of.items() if old.get(sid) != gid])
    conn.commit()
    return {"groups": n_groups, "multi_frame_groups": n_multi, "cameras": len(lines), "semantic": bool(embs),
            "undated": len(undated), "level": level}
