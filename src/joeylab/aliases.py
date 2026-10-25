"""A person's other names: "Dad" is also "Papa" and "Vikram Rao". One name is shown (the
person's name); search, naming faces, ⌘K and assistants find them by any of them. A name, whatever
its case, belongs to one person only. Other names stay in the catalog: the metadata JoeyLab writes
into files uses the shown name alone."""

from __future__ import annotations

import json

MAX_LEN = 80


class Clash(ValueError):
    """That name already belongs to someone else."""

    def __init__(self, name: str, other: int, other_name: str, alias: bool):
        self.name, self.other, self.other_name, self.alias = name, other, other_name, alias
        whose = f"another name for {other_name}" if alias else "someone else's name"
        super().__init__(f"“{name}” is already {whose}. A name can belong to only one person: "
                         f"if they're the same person, merge them instead.")

    def info(self) -> dict:
        return {"name": self.name, "id": self.other, "person": self.other_name, "alias": self.alias}


def clean(name) -> str:
    name = " ".join(str(name or "").split())
    if not name or len(name) > MAX_LEN:
        raise ValueError(f"Give each name up to {MAX_LEN} characters.")
    return name


def owner(conn, name: str) -> tuple[int, str, bool] | None:
    """Who is called this: (person id, the name they're shown by, whether it's one of their other
    names), or None."""
    r = conn.execute("SELECT id, name FROM people WHERE name = ? COLLATE NOCASE ORDER BY id LIMIT 1", (name,)).fetchone()
    if r:
        return r[0], r[1], False
    r = conn.execute("SELECT p.id, p.name FROM person_aliases a JOIN people p ON p.id = a.person_id"
                     " WHERE a.name = ? COLLATE NOCASE LIMIT 1", (name,)).fetchone()
    return (r[0], r[1], True) if r else None


def find(conn, name: str, prefix: bool = False) -> tuple[int, str] | None:
    """The person called this, by their name or another name: (id, the name they're shown by). With
    prefix, a name that starts with it counts too ("person:Vik" finds Vikram)."""
    name = " ".join(str(name or "").split())
    if not name:
        return None
    o = owner(conn, name)
    if o:
        return o[0], o[1]
    if prefix:
        r = conn.execute(
            "SELECT id, name FROM (SELECT id, name, name AS said FROM people UNION ALL SELECT p.id, p.name, a.name"
            " FROM person_aliases a JOIN people p ON p.id = a.person_id) WHERE said LIKE ? COLLATE NOCASE"
            " ORDER BY LENGTH(said), id LIMIT 1", (f"{name}%",)).fetchone()
        if r:
            return r[0], r[1]
    return None


def of(conn, pid: int) -> list[str]:
    """One person's other names, in the order they were given."""
    return [r[0] for r in conn.execute("SELECT name FROM person_aliases WHERE person_id=? ORDER BY rowid", (pid,))]


def everyone(conn) -> dict[int, list[str]]:
    """Everyone's other names, by person id."""
    out: dict[int, list[str]] = {}
    for pid, name in conn.execute("SELECT person_id, name FROM person_aliases ORDER BY rowid"):
        out.setdefault(pid, []).append(name)
    return out


def for_search(conn) -> list[tuple[int, str, str]]:
    """Every way a search can say who someone is, longest first: (person id, the words, the name
    they're shown by). Names, other names, and the first name of a longer one when nobody else has
    it ("photos of Vikram" finds Vikram Rao). Hidden people are left out."""
    from .query import STOP

    shown = {pid: name for pid, name in conn.execute("SELECT id, name FROM people WHERE COALESCE(hidden,0)=0")}
    said = [(pid, name, name) for pid, name in shown.items()]
    said += [(pid, a, shown[pid]) for pid, a in conn.execute("SELECT person_id, name FROM person_aliases") if pid in shown]
    whole = {s.lower() for _, s, _ in said}
    firsts: dict[str, dict] = {}
    for pid, s, _ in said:
        words = s.split()
        if len(words) > 1 and len(words[0]) >= 3:
            firsts.setdefault(words[0].lower(), {"word": words[0], "pids": set()})["pids"].add(pid)
    for low, f in firsts.items():
        if len(f["pids"]) == 1 and low not in whole and low not in STOP:
            pid = next(iter(f["pids"]))
            said.append((pid, f["word"], shown[pid]))
    return sorted(said, key=lambda x: -len(x[1]))


def _check(conn, pid: int | None, names: list[str]) -> None:
    for n in names:
        o = owner(conn, n)
        if o and o[0] != pid:
            raise Clash(n, *o)


def _distinct(names, skip=()) -> list[str]:
    seen = {s.lower() for s in skip}
    out = []
    for n in names:
        if not str(n or "").strip():
            continue
        n = clean(n)
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def set_names(conn, pid: int, name: str, others: list[str]) -> dict:
    """Give a person their name and their other names in one go (a name among the others becomes
    the main one: they swap). Returns what they were, for Undo. Raises Clash, changing nothing,
    when one belongs to someone else."""
    from . import identity

    row = conn.execute("SELECT name FROM people WHERE id=?", (pid,)).fetchone()
    if not row:
        raise ValueError("No such person.")
    before = {"name": row[0], "aliases": of(conn, pid)}
    if not str(name or "").strip():
        raise ValueError("Give the person a name.")
    name = clean(name)
    keep = _distinct(others, skip=[name])
    _check(conn, pid, [name, *keep])
    conn.execute("DELETE FROM person_aliases WHERE person_id=?", (pid,))
    conn.execute("UPDATE people SET name=? WHERE id=?", (name, pid))
    conn.executemany("INSERT INTO person_aliases(person_id, name) VALUES (?, ?)", [(pid, a) for a in keep])
    if row[0] != name:
        identity.renamed(row[0], name)
    return before


def add(conn, pid: int, names: list[str]) -> list[str]:
    """Add other names to a person, skipping any they have or someone else has. Returns those added."""
    have = conn.execute("SELECT name FROM people WHERE id=?", (pid,)).fetchone()
    if not have:
        return []
    added = [n for n in _distinct(names, skip=[have[0], *of(conn, pid)]) if not owner(conn, n)]
    conn.executemany("INSERT INTO person_aliases(person_id, name) VALUES (?, ?)", [(pid, a) for a in added])
    return added


def merge(conn, src: int, dst: int) -> dict:
    """src and dst are one person: src's faces go to dst, src's name and other names become dst's
    other names, and src is gone. Returns what Undo (unmerge) needs."""
    from . import identity

    if src == dst:
        raise ValueError("That's the same person.")
    a = conn.execute("SELECT id, name, kind, hidden FROM people WHERE id=?", (src,)).fetchone()
    b = conn.execute("SELECT id, name FROM people WHERE id=?", (dst,)).fetchone()
    if not a or not b:
        raise ValueError("No such person.")
    was_me = identity.person_id(conn, None) == src
    theirs = of(conn, src)
    faces = [r[0] for r in conn.execute("SELECT id FROM faces WHERE person_id=?", (src,))]
    conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (dst, src))
    # "That group isn't them" holds for the one person they now are.
    not_same = [r[0] for r in conn.execute("SELECT cluster FROM face_not_same WHERE person_id=?", (src,))]
    had = {r[0] for r in conn.execute("SELECT cluster FROM face_not_same WHERE person_id=?", (dst,))}
    conn.execute("UPDATE OR IGNORE face_not_same SET person_id=? WHERE person_id=?", (dst, src))
    conn.execute("DELETE FROM face_not_same WHERE person_id=?", (src,))
    shotlist = {}
    for sid, people in conn.execute("SELECT id, people FROM shotlist").fetchall():
        ids = json.loads(people or "[]")
        if src in ids:
            shotlist[sid] = people
            conn.execute("UPDATE shotlist SET people=? WHERE id=?",
                         (json.dumps(list(dict.fromkeys(dst if x == src else x for x in ids))), sid))
    conn.execute("DELETE FROM people WHERE id=?", (src,))          # their other names go with them
    added = add(conn, dst, [a["name"], *theirs])
    if was_me:
        identity.set_name(None, b["name"])
    return {"person": {"id": src, "name": a["name"], "kind": a["kind"], "hidden": a["hidden"], "aliases": theirs},
            "into": dst, "faces": faces, "added": added, "not_same": not_same,
            "not_same_added": [c for c in not_same if c not in had], "shotlist": shotlist, "me": was_me}


def unmerge(conn, undo: dict) -> int:
    """Undo a merge: the person who went comes back, with their names and the faces they had (those
    still with the person they joined). Returns their id."""
    from . import identity

    p, dst = undo.get("person") or {}, undo.get("into")
    src = int(p.get("id") or 0)
    if not src or conn.execute("SELECT 1 FROM people WHERE id=?", (src,)).fetchone():
        raise ValueError("That merge has already been undone.")
    if not conn.execute("SELECT 1 FROM people WHERE id=?", (dst,)).fetchone():
        raise ValueError("The person they were merged into is gone, so this can't be undone.")
    name = clean(p.get("name"))
    added = [str(n) for n in undo.get("added") or []]
    o = owner(conn, name)
    if o and not (o[0] == dst and o[2] and name.lower() in {n.lower() for n in added}):
        raise Clash(name, *o)
    for n in added:                       # their names come off the person they joined
        conn.execute("DELETE FROM person_aliases WHERE person_id=? AND name=?", (dst, n))
    conn.execute("INSERT INTO people(id, name, kind, hidden) VALUES (?, ?, ?, ?)",
                 (src, name, p.get("kind") or "person", int(p.get("hidden") or 0)))
    add(conn, src, p.get("aliases") or [])
    ids = [int(x) for x in undo.get("faces") or []]
    for i in range(0, len(ids), 900):
        chunk = ids[i : i + 900]
        conn.execute(f"UPDATE faces SET person_id=? WHERE person_id=? AND id IN ({','.join('?' * len(chunk))})",
                     (src, dst, *chunk))
    for c in undo.get("not_same_added") or []:
        conn.execute("DELETE FROM face_not_same WHERE person_id=? AND cluster=?", (dst, int(c)))
    conn.executemany("INSERT OR IGNORE INTO face_not_same(person_id, cluster) VALUES (?, ?)",
                     [(src, int(c)) for c in undo.get("not_same") or []])
    for sid, people in (undo.get("shotlist") or {}).items():
        conn.execute("UPDATE shotlist SET people=? WHERE id=?", (people, int(sid)))
    if undo.get("me"):
        identity.set_name(None, name)
    return src
