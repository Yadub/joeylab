"""Your keyword tree: "Activities › Climbing", "Family › Grandparents", kept in the catalog only.

Keywords nest; a photo tagged "Climbing" is found under "Activities" too. Tagging is logged in the
same history as stars and picks, so it can be undone. XMP export writes them the way Lightroom does
(dc:subject with every level, lr:hierarchicalSubject as "Activities|Climbing"). Nothing here touches
a photo file.
"""

from __future__ import annotations

import json
import re
import time
import uuid

SEP = re.compile(r"\s*(?:›|>|\||/)\s*")
SUBTREE = ("WITH RECURSIVE sub(id) AS (SELECT ? UNION SELECT k.id FROM keywords k JOIN sub ON k.parent_id = sub.id)"
           " SELECT id FROM sub")


def split_path(text: str) -> list[str]:
    """ "Activities › Climbing", "Activities > Climbing", "Activities|Climbing" or "Activities/Climbing"."""
    return [p.strip()[:80] for p in SEP.split(text or "") if p.strip()]


LIVE = "COALESCE(deleted, 0) = 0"          # removed keywords are kept (hidden) so Undo can bring them back


def _child(conn, parent_id: int | None, name: str, removed_too: bool = False):
    return conn.execute("SELECT id, COALESCE(deleted, 0) AS deleted FROM keywords WHERE parent_id IS ? AND name = ? COLLATE NOCASE"
                        + ("" if removed_too else f" AND {LIVE}") + " ORDER BY deleted LIMIT 1", (parent_id, name)).fetchone()


def ensure(conn, path: str | list[str], commit: bool = True) -> int:
    """The keyword at this path, made (with any missing parents) if needed."""
    parts = split_path(path) if isinstance(path, str) else [p.strip() for p in path if p.strip()]
    if not parts:
        raise ValueError("A keyword needs a name.")
    parent = None
    for name in parts:
        r = _child(conn, parent, name, removed_too=True)
        if r:
            parent = r[0]
            if r["deleted"]:                      # typed again after being removed: it comes back
                conn.execute("UPDATE keywords SET deleted=0 WHERE id=?", (parent,))
        else:
            from . import db

            kid = db.new_id(conn, "keywords")      # never an id used before: history points at it
            conn.execute("INSERT INTO keywords(id, name, parent_id, created) VALUES (?,?,?,?)", (kid, name, parent, time.time()))
            parent = kid
    if commit:
        conn.commit()
    return parent


def find(conn, text: str) -> int | None:
    """A keyword by its path or, failing that, by its own name (the most used one when names repeat)."""
    parts = split_path(text)
    if not parts:
        return None
    if len(parts) > 1:
        parent = None
        for name in parts:
            r = _child(conn, parent, name)
            if not r:
                return None
            parent = r[0]
        return parent
    r = conn.execute(f"SELECT k.id FROM keywords k LEFT JOIN shot_keywords sk ON sk.keyword_id = k.id"
                     f" WHERE k.name = ? COLLATE NOCASE AND COALESCE(k.deleted, 0) = 0 GROUP BY k.id"
                     " ORDER BY COUNT(sk.shot_id) DESC LIMIT 1",
                     (parts[0],)).fetchone()
    return r[0] if r else None


def path_of(conn, kid: int) -> list[str]:
    out, seen = [], set()
    while kid is not None and kid not in seen:
        seen.add(kid)
        r = conn.execute("SELECT name, parent_id FROM keywords WHERE id=?", (kid,)).fetchone()
        if not r:
            break
        out.append(r[0])
        kid = r[1]
    return out[::-1]


def paths(conn) -> dict[int, list[str]]:
    """Every keyword's path at once (one query), for exports that would otherwise walk up the tree
    for each photo's each keyword."""
    rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, name, parent_id FROM keywords")}
    out: dict[int, list[str]] = {}
    for k in rows:
        chain, cur, seen = [], k, set()
        while cur is not None and cur in rows and cur not in seen and cur not in out:
            seen.add(cur)
            chain.append(cur)
            cur = rows[cur][1]
        base = list(out.get(cur, [])) if cur in out else []
        for c in reversed(chain):
            base = base + [rows[c][0]]
            out[c] = base
    return out


def subtree(conn, kid: int) -> list[int]:
    return [r[0] for r in conn.execute(SUBTREE, (kid,))]


def natural_key(name: str) -> list:
    """Sort as people count: "Topic 2" before "Topic 10"."""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", name.lower()) if p]


def tree(conn) -> list[dict]:
    """Every keyword with its path and counts: photos tagged with it, and with it or anything under it."""
    rows = [dict(r) for r in conn.execute(
        "SELECT k.id, k.name, k.parent_id, COUNT(s.id) AS n FROM keywords k"
        " LEFT JOIN shot_keywords sk ON sk.keyword_id = k.id LEFT JOIN shots s ON s.id = sk.shot_id AND s.status='present'"
        " WHERE COALESCE(k.deleted, 0) = 0 GROUP BY k.id")]
    by_id = {r["id"]: r for r in rows}
    kids: dict = {}
    for r in rows:
        kids.setdefault(r["parent_id"], []).append(r["id"])
    totals = {r["kid"]: r["n"] for r in conn.execute(
        "WITH RECURSIVE up(kid, anc) AS (SELECT id, id FROM keywords UNION ALL"
        " SELECT up.kid, k.parent_id FROM up JOIN keywords k ON k.id = up.anc WHERE k.parent_id IS NOT NULL)"
        " SELECT up.anc AS kid, COUNT(DISTINCT sk.shot_id) AS n FROM up JOIN shot_keywords sk ON sk.keyword_id = up.kid"
        " JOIN shots s ON s.id = sk.shot_id AND s.status='present' GROUP BY up.anc")}
    out: list[dict] = []

    def walk(parent, depth, prefix):
        for kid in sorted(kids.get(parent, []), key=lambda i: natural_key(by_id[i]["name"])):
            r = by_id[kid]
            path = prefix + [r["name"]]
            out.append({"id": kid, "name": r["name"], "parent_id": parent, "depth": depth, "path": " › ".join(path),
                        "n": r["n"], "total": totals.get(kid, 0), "children": len(kids.get(kid, []))})
            walk(kid, depth + 1, path)
    walk(None, 0, [])
    return out


def of_shot(conn, sid: int) -> list[dict]:
    return [{"id": r[0], "name": r[1], "path": " › ".join(path_of(conn, r[0]))} for r in conn.execute(
        "SELECT k.id, k.name FROM shot_keywords sk JOIN keywords k ON k.id = sk.keyword_id WHERE sk.shot_id=?"
        " ORDER BY k.name COLLATE NOCASE", (sid,))]


def _ids_of(conn, sid: int) -> list[int]:
    return sorted(r[0] for r in conn.execute("SELECT keyword_id FROM shot_keywords WHERE shot_id=?", (sid,)))


def _set(conn, sid: int, ids: list[int], src: str) -> None:
    have = set(_ids_of(conn, sid))
    want = set(ids)
    for k in have - want:
        conn.execute("DELETE FROM shot_keywords WHERE shot_id=? AND keyword_id=?", (sid, k))
    for k in want - have:
        conn.execute("INSERT OR IGNORE INTO shot_keywords(shot_id, keyword_id, src) VALUES (?,?,?)", (sid, k, src))


def assign(conn, shot_ids: list[int], add: list[int] = (), remove: list[int] = (), what: str = "keywords",
           source: str = "web", viewer: str | None = None, batch: str | None = None) -> tuple[str, list[int]]:
    """Add and remove keywords on photos as one undoable step in the history. Returns (batch, changed ids)."""
    batch = batch or uuid.uuid4().hex[:12]
    now = time.time()
    add, remove = [int(k) for k in add], set(int(k) for k in remove)
    changed = []
    for sid in dict.fromkeys(int(s) for s in shot_ids):
        before = _ids_of(conn, sid)
        after = sorted((set(before) | set(add)) - remove)
        if after == before:
            continue
        _set(conn, sid, after, source)
        conn.execute("INSERT INTO decision_log(ts, batch, label, source, viewer, shot_id, before, after) VALUES (?,?,?,?,?,?,?,?)",
                     (now, batch, what, source, viewer, sid, json.dumps({"keywords": before}), json.dumps({"keywords": after})))
        changed.append(sid)
    conn.commit()
    return batch, changed


def restore(conn, sid: int, ids: list[int]) -> None:
    """Undo: put a photo's keywords back as they were. A keyword removed since comes back too."""
    ids = [int(k) for k in ids]
    known = {r[0]: r[1] for r in conn.execute(
        f"SELECT id, COALESCE(deleted, 0) FROM keywords WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    keep = [k for k in ids if k in known]
    revive = [(k,) for k in keep if known[k]]
    if revive:
        conn.executemany("UPDATE keywords SET deleted=0 WHERE id=?", revive)
    _set(conn, sid, keep, "undo")


def rename(conn, kid: int, name: str) -> None:
    name = (name or "").strip()[:80]
    if not name or SEP.search(name):
        raise ValueError("A keyword name can't be empty or contain › > | /.")
    r = conn.execute("SELECT parent_id FROM keywords WHERE id=?", (kid,)).fetchone()
    if not r:
        raise ValueError("No such keyword.")
    other = _child(conn, r[0], name)
    if other and other[0] != kid:
        raise ValueError(f"There's already a keyword called {name} here; merge them instead.")
    conn.execute("UPDATE keywords SET name=? WHERE id=?", (name, kid))
    conn.commit()


def move(conn, kid: int, parent_id: int | None) -> None:
    if parent_id is not None and parent_id in subtree(conn, kid):
        raise ValueError("A keyword can't go inside itself.")
    name = conn.execute("SELECT name FROM keywords WHERE id=?", (kid,)).fetchone()
    if not name:
        raise ValueError("No such keyword.")
    other = _child(conn, parent_id, name[0])
    if other and other[0] != kid:
        merge(conn, kid, other[0])
        return
    conn.execute("UPDATE keywords SET parent_id=? WHERE id=?", (parent_id, kid))
    conn.commit()


def merge(conn, src: int, dst: int, source: str = "web", viewer: str | None = None) -> dict:
    """Fold one keyword into another: its photos and sub-keywords move over. The photos' change is one
    step in the history (Undo puts them back under the old keyword). Returns {moved, batch}."""
    if src == dst:
        return {"moved": 0, "batch": None}
    if dst in subtree(conn, src):
        raise ValueError("Can't merge a keyword into one of its own sub-keywords.")
    names = (path_of(conn, src) or ["?"])[-1], (path_of(conn, dst) or ["?"])[-1]
    photos = [r[0] for r in conn.execute("SELECT shot_id FROM shot_keywords WHERE keyword_id=?", (src,))]
    batch, changed = assign(conn, photos, add=[dst], remove=[src], what=f"keywords: {names[0]} merged into {names[1]}"[:60],
                            source=source, viewer=viewer)
    for (child,) in conn.execute(f"SELECT id FROM keywords WHERE parent_id=? AND {LIVE}", (src,)).fetchall():
        move(conn, child, dst)
    conn.execute("UPDATE keywords SET deleted=1 WHERE id=?", (src,))
    conn.commit()
    return {"moved": len(changed), "batch": batch}


def delete(conn, kid: int, source: str = "web", viewer: str | None = None) -> dict:
    """Take a keyword off every photo (one step in the history: Undo brings it back) and out of the
    tree; its sub-keywords move up a level rather than vanish. Returns {removed_from, batch}."""
    r = conn.execute("SELECT parent_id FROM keywords WHERE id=?", (kid,)).fetchone()
    if not r:
        return {"removed_from": 0, "batch": None}
    photos = [x[0] for x in conn.execute("SELECT shot_id FROM shot_keywords WHERE keyword_id=?", (kid,))]
    batch, changed = assign(conn, photos, remove=[kid], what=f"keyword removed: {(path_of(conn, kid) or ['?'])[-1]}"[:60],
                            source=source, viewer=viewer)
    for (child,) in conn.execute(f"SELECT id FROM keywords WHERE parent_id=? AND {LIVE}", (kid,)).fetchall():
        move(conn, child, r[0])
    conn.execute("UPDATE keywords SET deleted=1 WHERE id=?", (kid,))
    conn.commit()
    return {"removed_from": len(changed), "batch": batch}


def for_xmp(conn, sid: int, all_paths: dict[int, list[str]] | None = None) -> tuple[list[str], list[str]]:
    """(dc:subject values: every keyword and its parents; lr:hierarchicalSubject: "A|B|C" paths)."""
    flat: list[str] = []
    hier: list[str] = []
    for (kid,) in conn.execute("SELECT keyword_id FROM shot_keywords WHERE shot_id=?", (sid,)):
        path = all_paths[kid] if all_paths is not None and kid in all_paths else path_of(conn, kid)
        hier.append("|".join(path))
        flat += path
    return list(dict.fromkeys(flat)), sorted(set(hier))


def import_names(conn, items: dict[int, list[list[str]]], source: str, what: str) -> dict:
    """Keywords found elsewhere (sidecars, Lightroom) per photo, each given as its path parts
    (["Activities", "Climbing"]), as one undoable step. Only adds, never removes."""
    batch = uuid.uuid4().hex[:12]
    made_before = conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]
    n = 0
    for sid, paths in items.items():
        ids = [ensure(conn, [x[:80] for x in parts], commit=False) for parts in paths if any(x.strip() for x in parts)]
        if ids:
            _, ch = assign(conn, [sid], add=ids, what=what, source=source, batch=batch)
            n += len(ch)
    made = conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] - made_before
    conn.commit()
    return {"photos": n, "new_keywords": made, "batch": batch}


def from_sidecars(conn) -> dict:
    """Keywords other apps wrote into .xmp sidecars (read during scanning), onto their photos.
    Lightroom's hierarchy ("Activities|Climbing") is kept as a tree; a flat keyword list is used
    otherwise, leaving out the people, places and event names JoeyLab itself writes there."""
    known = {r[0].lower() for r in conn.execute(
        "SELECT name FROM people UNION SELECT name FROM places UNION SELECT country FROM places WHERE country IS NOT NULL"
        " UNION SELECT name FROM events") if r[0]}
    items: dict[int, list[list[str]]] = {}
    for sid, kws, hier in conn.execute(
            "SELECT f.shot_id, json_extract(f.meta, '$.sidecar.keywords'), json_extract(f.meta, '$.sidecar.hierarchy')"
            " FROM files f JOIN shots s ON s.id = f.shot_id WHERE s.status='present'"
            " AND (json_array_length(json_extract(f.meta, '$.sidecar.keywords')) > 0"
            "      OR json_array_length(json_extract(f.meta, '$.sidecar.hierarchy')) > 0)"):
        paths = [[x for x in str(h).split("|") if x.strip()] for h in json.loads(hier or "[]")]
        if not paths:
            paths = [[str(k)] for k in json.loads(kws or "[]") if str(k).lower() not in known]
        for path in paths:
            if path:
                items.setdefault(sid, []).append(path)
    return import_names(conn, items, "sidecar", "keywords from .xmp sidecars")
