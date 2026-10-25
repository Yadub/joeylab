"""A person's other names ("Dad" is also "Papa" and "Vikram Rao"): kept, swapped and undone in
the Edit dialog, never on two people, merged, used to name faces, and understood by search, ⌘K,
assistants and the portable export, while files only ever get the person's main name."""

from __future__ import annotations

import threading
import time

import numpy as np
from fastapi.testclient import TestClient

from conftest import add_root, make_jpeg, run_all


def _family(env, n=6):
    """A small library with Dad in photos 0-2, Mum in 3-4 and an unnamed group of two faces in 5."""
    _, lib = env
    for i in range(n):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    sid = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    dad = conn.execute("INSERT INTO people(name, kind) VALUES ('Dad', 'person')").lastrowid
    mum = conn.execute("INSERT INTO people(name, kind) VALUES ('Mum', 'person')").lastrowid
    for s in sid[:3]:
        conn.execute("INSERT INTO faces(shot_id, x, y, w, h, person_id, person_src) VALUES (?, .1, .1, .1, .1, ?, 'user')", (s, dad))
    for s in sid[3:5]:
        conn.execute("INSERT INTO faces(shot_id, x, y, w, h, person_id, person_src) VALUES (?, .5, .1, .1, .1, ?, 'user')", (s, mum))
    rng = np.random.default_rng(1)
    for x in (.2, .6):
        conn.execute("INSERT INTO faces(shot_id, x, y, w, h, px, sharp, closed, vec, cluster) VALUES (?, ?, .1, .1, .1, 120, 300, 0, ?, 42)",
                     (sid[5], x, rng.normal(size=512).astype(np.float16).tobytes()))
    conn.commit()
    return conn, sid, dad, mum


def _person(c, pid):
    d = c.get("/api/p/t/people").json()
    return next((x for x in d["people"] + d["pets"] + d["hidden"] if x["id"] == pid), None)


def test_other_names_are_added_removed_swapped_and_undone(env):
    from joeylab import identity
    from joeylab.web.server import create_app

    conn, _, dad, _ = _family(env)
    c = TestClient(create_app())
    c.post("/api/p/t/me", json={"person_id": dad})

    r = c.post(f"/api/p/t/people/{dad}/names", json={"name": "Dad", "aliases": ["Papa", " Vikram  Rao ", "papa", ""]}).json()
    assert r["aliases"] == ["Papa", "Vikram Rao"]                     # tidied; one of each, whatever the case
    assert r["before"] == {"name": "Dad", "aliases": []}
    assert _person(c, dad)["aliases"] == ["Papa", "Vikram Rao"]

    # Remove one; then make another the main name: they swap.
    c.post(f"/api/p/t/people/{dad}/names", json={"name": "Dad", "aliases": ["Vikram Rao"]})
    assert _person(c, dad)["aliases"] == ["Vikram Rao"]
    r = c.post(f"/api/p/t/people/{dad}/names", json={"name": "Vikram Rao", "aliases": ["Dad", "Vikram Rao"]}).json()
    assert (r["name"], r["aliases"]) == ("Vikram Rao", ["Dad"])
    assert identity.name_of(None) == "Vikram Rao" and identity.person_id(conn, None) == dad   # still you

    # Undo puts back what it was.
    c.post(f"/api/p/t/people/{dad}/names", json=r["before"])
    p = _person(c, dad)
    assert (p["name"], p["aliases"]) == ("Dad", ["Vikram Rao"]) and identity.name_of(None) == "Dad"

    # Forgetting someone takes their other names too; Undo brings them back.
    gone = c.delete(f"/api/p/t/people/{dad}").json()
    assert gone["aliases"] == ["Vikram Rao"]
    assert conn.execute("SELECT COUNT(*) FROM person_aliases").fetchone()[0] == 0
    back = c.post("/api/p/t/people/name", json={"name": gone["name"], "kind": gone["kind"], "faces": gone["faces"],
                                                "aliases": gone["aliases"]}).json()
    assert _person(c, back["id"])["aliases"] == ["Vikram Rao"] and len(back["assigned"]) == 3


def test_a_name_belongs_to_one_person(env):
    from joeylab.web.server import create_app

    conn, _, dad, mum = _family(env)
    c = TestClient(create_app())
    c.post(f"/api/p/t/people/{mum}/names", json={"name": "Mum", "aliases": ["Mummy"]})

    # Another person's name, or one of their other names, can't be Dad's too: it says whose it is.
    for taken, alias in (("mum", False), ("MUMMY", True)):
        r = c.post(f"/api/p/t/people/{dad}/names", json={"name": "Dad", "aliases": ["Papa", taken]})
        assert r.status_code == 409
        assert r.json()["clash"] == {"name": taken, "id": mum, "person": "Mum", "alias": alias}
        assert "only one person" in r.json()["error"] and "merge" in r.json()["error"]
        assert _person(c, dad)["aliases"] == []                             # nothing changed
    # A main name someone else has is refused too, unless merging is what's asked for.
    r = c.post(f"/api/p/t/people/{dad}/names", json={"name": "Mummy", "aliases": []})
    assert r.status_code == 409 and r.json()["clash"]["person"] == "Mum"
    assert {x["name"] for x in c.get("/api/p/t/people").json()["people"]} == {"Dad", "Mum"}
    # Renaming keeps the old name only when asked, and can't take someone else's other name by accident.
    c.post(f"/api/p/t/people/{dad}/names", json={"name": "Dad", "aliases": ["Papa"]})
    r = c.post(f"/api/p/t/people/{mum}/names", json={"name": "Mum", "aliases": ["Mummy", "papa"]})
    assert r.status_code == 409 and r.json()["clash"]["person"] == "Dad"


def test_rename_can_keep_the_old_name(env):
    from joeylab.web.server import create_app

    conn, _, dad, _ = _family(env)
    c = TestClient(create_app())
    r = c.post(f"/api/p/t/people/{dad}/rename", json={"name": "Vikram Rao", "keep_old": True}).json()
    assert (r["name"], r["aliases"], r["before"]) == ("Vikram Rao", ["Dad"], {"name": "Dad", "aliases": []})
    r = c.post(f"/api/p/t/people/{dad}/rename", json={"name": "Vikram"}).json()      # not kept unless asked
    assert (r["name"], r["aliases"]) == ("Vikram", ["Dad"])
    r = c.post(f"/api/p/t/people/{dad}/rename", json={"name": "dad", "keep_old": True}).json()   # one of theirs: a swap
    assert (r["name"], r["aliases"]) == ("dad", ["Vikram"])
    r = c.post(f"/api/p/t/people/{dad}/rename", json={"name": "Dad", "keep_old": True}).json()   # only the case changed
    assert (r["name"], r["aliases"]) == ("Dad", ["Vikram"])


def test_merging_two_people_combines_their_names_and_can_be_undone(env):
    from joeylab import identity
    from joeylab.web.server import create_app

    conn, sid, dad, mum = _family(env)
    c = TestClient(create_app())
    c.post(f"/api/p/t/people/{dad}/names", json={"name": "Dad", "aliases": ["Papa"]})
    c.post(f"/api/p/t/people/{mum}/names", json={"name": "Vikram", "aliases": ["Vikram Rao", "Papa ji"]})
    c.post("/api/p/t/me", json={"person_id": mum})
    conn.execute("INSERT INTO face_not_same(person_id, cluster) VALUES (?, 42)", (mum,))
    conn.execute("INSERT INTO shotlist(title, people) VALUES ('Portrait', ?)", (f"[{mum}]",))
    conn.commit()

    # "Vikram" is Dad: the one that goes keeps its names as Dad's other names.
    r = c.post(f"/api/p/t/people/{dad}/merge", json={"other": mum}).json()
    assert r["merged"] and r["id"] == dad and r["aliases"] == ["Papa", "Vikram", "Vikram Rao", "Papa ji"]
    assert _person(c, mum) is None and _person(c, dad)["photos"] == 5
    assert identity.person_id(conn, None) == dad                             # you were Vikram: you're Dad now
    assert [r[0] for r in conn.execute("SELECT person_id FROM face_not_same")] == [dad]
    assert conn.execute("SELECT people FROM shotlist").fetchone()[0] == f"[{dad}]"

    # Undo: Vikram is back as he was, with his photos, names, "not them" answers and shot list.
    back = c.post("/api/p/t/people/unmerge", json={"undo": r["undo"]}).json()
    assert back["id"] == mum
    p = _person(c, mum)
    assert (p["name"], p["aliases"], p["photos"]) == ("Vikram", ["Vikram Rao", "Papa ji"], 2)
    assert _person(c, dad)["aliases"] == ["Papa"] and _person(c, dad)["photos"] == 3
    assert identity.person_id(conn, None) == mum
    assert [r[0] for r in conn.execute("SELECT person_id FROM face_not_same")] == [mum]
    assert conn.execute("SELECT people FROM shotlist").fetchone()[0] == f"[{mum}]"
    assert c.post("/api/p/t/people/unmerge", json={"undo": r["undo"]}).status_code == 400   # only once

    # Renaming to someone else's other name, or saving with merge, merges into them too.
    r = c.post(f"/api/p/t/people/{mum}/rename", json={"name": "papa"}).json()
    assert r["merged"] and r["id"] == dad and "Vikram" in r["aliases"]
    c.post("/api/p/t/people/unmerge", json={"undo": r["undo"]})
    r = c.post(f"/api/p/t/people/{mum}/names", json={"name": "Dad", "aliases": ["Vikram Rao", "Vikramji"], "merge": True}).json()
    assert r["merged"] and set(r["aliases"]) == {"Papa", "Vikram", "Vikram Rao", "Vikramji"}   # as the dialog had them
    c.post("/api/p/t/people/unmerge", json={"undo": r["undo"]})
    assert _person(c, mum)["aliases"] == ["Vikram Rao", "Papa ji"]     # as before that save
    assert c.post(f"/api/p/t/people/{dad}/merge", json={"other": dad}).status_code == 400


def test_naming_faces_by_another_name_names_that_person(env):
    from joeylab.web.server import create_app

    conn, sid, dad, _ = _family(env)
    c = TestClient(create_app())
    c.post(f"/api/p/t/people/{dad}/names", json={"name": "Dad", "aliases": ["Vikram Rao"]})
    r = c.post("/api/p/t/people/name", json={"cluster": 42, "name": "  vikram   rao "}).json()
    assert r["id"] == dad and r["name"] == "Dad" and len(r["assigned"]) == 2
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 2               # nobody new
    one = conn.execute("SELECT id FROM faces WHERE cluster=42 LIMIT 1").fetchone()[0]
    c.post(f"/api/p/t/people/{dad}/unname", json={"faces": [one]})
    assert c.post("/api/p/t/people/name", json={"faces": [one], "name": "VIKRAM RAO"}).json()["id"] == dad


def test_two_presses_at_once_make_one_person(env, monkeypatch):
    from joeylab import aliases, db
    from joeylab.web.routes import people

    _family(env)
    slow = aliases.find

    def find(*a, **k):                    # widen the gap between "is anyone called this?" and the insert
        r = slow(*a, **k)
        time.sleep(0.2)
        return r

    monkeypatch.setattr(aliases, "find", find)
    got, errors = [], []
    start = threading.Barrier(2)

    def press(name):
        conn = db.connect("t")
        start.wait()
        try:
            got.append(people._person_named(conn, name))
            conn.commit()
        except Exception as e:            # noqa: BLE001 - the test reports any failure
            errors.append(e)
        finally:
            conn.close()

    threads = [threading.Thread(target=press, args=(n,)) for n in ("Noor", "noor")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conn = db.connect("t")
    assert not errors and len(got) == 2 and got[0] == got[1]
    assert conn.execute("SELECT COUNT(*) FROM people WHERE name = 'noor' COLLATE NOCASE").fetchone()[0] == 1


def test_search_and_jump_find_people_by_any_name(env):
    from joeylab import query
    from joeylab.web.server import create_app

    conn, sid, dad, mum = _family(env)
    c = TestClient(create_app())
    c.post(f"/api/p/t/people/{dad}/names", json={"name": "Dad", "aliases": ["Papa", "Vikram Rao"]})

    for q in ("photos of Vikram Rao", "photos of vikram", "Papa's birthday", "person:papa", "person:Vikr", "who:\"Vikram Rao\""):
        parsed = query.parse(q, conn)
        assert parsed["filter"].get("people") == str(dad), q
        assert parsed["chips"] == ["Dad"], q                                 # shown by their main name
    assert query.parse("Papa's birthday", conn)["text"] == "birthday"
    assert query.parse("Dad and Papa at the beach", conn)["text"] == "beach"   # one person, said twice
    parsed = c.get("/api/p/t/query/parse", params={"q": "Mum with vikram"}).json()
    assert set(parsed["filter"]["people"].split(",")) == {str(dad), str(mum)}
    assert c.get("/api/p/t/shots", params={"people": parsed["filter"]["people"]}).json()["total"] == 0

    # A first name shared by two people isn't a guess at either.
    c.post(f"/api/p/t/people/{mum}/names", json={"name": "Mum", "aliases": ["Vikram Singh"]})
    assert "people" not in query.parse("photos of vikram", conn)["filter"]

    jump = c.get("/api/p/t/jump").json()
    assert next(x for x in jump["people"] if x["id"] == dad)["aliases"] == ["Papa", "Vikram Rao"]


def test_assistants_export_and_files(env):
    from joeylab import db, export, library, mcp, portable
    from joeylab.web.server import create_app

    conn, sid, dad, mum = _family(env)
    c = TestClient(create_app())
    c.post(f"/api/p/t/people/{dad}/names", json={"name": "Dad", "aliases": ["Papa", "Vikram Rao"]})
    c.post("/api/p/t/me", json={"person_id": dad})

    got = mcp.call("people", {"library": "t"}, "t", False)
    dad_row = next(x for x in got if x["name"] == "Dad")
    assert dad_row["also_called"] == ["Papa", "Vikram Rao"] and dad_row["is_the_user"] and dad_row["photos"] == 3
    assert "also_called" not in next(x for x in got if x["name"] == "Mum")
    assert [x["name"] for x in mcp.call("people", {"library": "t", "name": "vikram"}, "t", False)] == ["Dad"]
    assert mcp.call("people", {"library": "t", "name": "Nobody"}, "t", False)["people"] == []
    found = mcp.call("search_photos", {"library": "t", "query": "Papa"}, "t", False)
    assert found["understood"] == ["Dad"] and len(found["photos"]) == 3

    # Files get the main name only.
    s = library.shot(conn, sid[0])
    assert export._identity(conn, s)["people"] == ["Dad"] and "Papa" not in export._identity(conn, s)["keywords"]

    # The portable export carries the other names, and a fresh catalog takes them back.
    data = portable.export_decisions(conn, "t")
    assert {"name": "Dad", "aliases": ["Papa", "Vikram Rao"]} in data["other_names"]
    conn.execute("DELETE FROM people")
    conn.execute("UPDATE faces SET person_id=NULL, person_src=NULL")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM person_aliases").fetchone()[0] == 0     # they went with the people
    portable.import_decisions(conn, data)
    dad2 = conn.execute("SELECT id FROM people WHERE name='Dad'").fetchone()[0]
    assert [r[0] for r in conn.execute("SELECT name FROM person_aliases WHERE person_id=? ORDER BY rowid", (dad2,))] == \
        ["Papa", "Vikram Rao"]
    assert db.SCHEMA_VERSION == conn.execute("PRAGMA user_version").fetchone()[0]
