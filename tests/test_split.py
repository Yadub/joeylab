"""Moving folders to another library (split.py): every catalog row about their photos goes with them,
nothing is read again, both catalogs are backed up and checked, a stop half way leaves the source as
it was, ids never land on the wrong photo, the source's scans leave the folders out from then on, and
"Move them back" restores the original."""

from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from conftest import EVERYDAY, WEDDINGS, add_root, jpeg_at, make_jpeg, run_all, synthetic_library


def _state(conn) -> dict:
    """Everything a library knows about its photos, by file path (ids aside), for comparing."""
    from joeylab import keywords

    path_of = {r["id"]: r["path"] for r in conn.execute(
        "SELECT s.id, MIN(f.path) AS path FROM shots s JOIN files f ON f.shot_id = s.id GROUP BY s.id")}
    kw_path = {k: " › ".join(v) for k, v in keywords.paths(conn).items()}
    out = {
        "photos": sorted(path_of.values()),
        "decisions": sorted((path_of[r[0]], r[1], r[2], r[3]) for r in conn.execute(
            "SELECT shot_id, rating, pick, label FROM decisions") if r[0] in path_of),
        "history": sorted((path_of.get(r["shot_id"]), r["batch"], r["label"], r["source"], r["undone"], r["undoes"],
                           _kw(r["before"], kw_path), _kw(r["after"], kw_path))
                          for r in conn.execute("SELECT * FROM decision_log")),
        "faces": sorted((path_of[r[0]], round(r[1], 3), r[2], r[3]) for r in conn.execute(
            "SELECT f.shot_id, f.x, p.name, f.person_src FROM faces f LEFT JOIN people p ON p.id = f.person_id")),
        "aliases": sorted(tuple(r) for r in conn.execute("SELECT p.name, a.name FROM person_aliases a JOIN people p ON p.id = a.person_id")),
        "keywords": sorted((path_of[r[0]], kw_path[r[1]]) for r in conn.execute("SELECT shot_id, keyword_id FROM shot_keywords")),
        "albums": sorted((r[0], path_of.get(r[1]), r[2]) for r in conn.execute(
            "SELECT a.name, i.shot_id, i.name FROM album_items i JOIN albums a ON a.id = i.album_id")),
        "raw": sorted((path_of[r[0]], r[1]) for r in conn.execute("SELECT shot_id, keep FROM raw_choices")),
        "favs": sorted((path_of[r[0]], r[1]) for r in conn.execute("SELECT shot_id, fav FROM viewer_decisions")),
        "comments": sorted((path_of[r[0]], r[1]) for r in conn.execute("SELECT shot_id, text FROM comments")),
        "backups": sorted((path_of[r[0]], r[1], r[2]) for r in conn.execute("SELECT shot_id, checked, drives FROM backups")),
        "dates": sorted((path_of[r[0]], r[1]) for r in conn.execute("SELECT shot_id, capture_str FROM manual_dates")),
        "metrics": sorted((path_of[r[0]], round(r[1] or 0, 4)) for r in conn.execute("SELECT shot_id, sharpness FROM metrics")),
        "colours": conn.execute("SELECT COUNT(*) FROM colours").fetchone()[0],
    }
    return out


def _kw(side, kw_path):
    d = json.loads(side or "{}")
    if "keywords" in d:
        d["keywords"] = sorted(kw_path.get(k, k) for k in d["keywords"])
    return json.dumps(d, sort_keys=True)


def _only(state: dict, prefix: str) -> dict:
    """The part of a library's state about photos under one folder."""
    out = {}
    for k, v in state.items():
        if isinstance(v, list):
            out[k] = [x for x in v if (isinstance(x, str) and x.startswith(prefix))
                      or (isinstance(x, tuple) and any(isinstance(y, str) and y.startswith(prefix) for y in x[:2]))]
        else:
            out[k] = v
    return out


def _ids(conn, table="shots") -> set[int]:
    return {r[0] for r in conn.execute(f"SELECT id FROM {table}")}


def test_a_new_library_gets_the_folders_with_everything_known_about_them(env):
    from joeylab import config, db, split

    _, lib = env
    s = synthetic_library(lib)
    conn = s["conn"]
    before = _state(conn)
    wed_ids, day_ids = set(s["wed"]), set(s["day"])
    weddings = str(lib / "Weddings")

    p = split.plan("t", [weddings], "weddings", "shoot")
    assert p["photos"] == len(wed_ids) and p["new"] and p["people"] == 2 and p["faces"] == 12
    assert p["decisions"] == 4 and p["albums"] == 2 and p["keywords"] == 2
    assert p["what"].startswith(f"Move {len(wed_ids)} photos (") and "to a new shoot library “weddings”" in p["what"]
    assert "Their decisions, names and history go with them." in p["what"]

    op = split.run("t", [weddings], "weddings", "shoot")
    assert op["state"] == "done" and op["counts"]["photos"] == len(wed_ids)
    src, dst = db.connect("t"), db.connect("weddings")
    after_src, after_dst = _state(src), _state(dst)

    # Each library has its own photos, with everything about them as it was.
    assert after_dst["photos"] == [p for p in before["photos"] if p.startswith(weddings)]
    assert after_src["photos"] == [p for p in before["photos"] if not p.startswith(weddings)]
    assert after_dst == _only(before, weddings) | {"colours": len(wed_ids), "aliases": before["aliases"]}
    assert after_src == _only(before, str(lib / "Everyday")) | {"colours": len(day_ids), "aliases": []}

    # People: the bride (and her other names) went; Sam is in both; Grandad stayed.
    names = lambda c: {r[0] for r in c.execute("SELECT name FROM people")}  # noqa: E731
    assert names(dst) == {"Bride", "Sam"} and names(src) == {"Sam", "Grandad"}
    # A new library keeps every id, and each library has only its own photos.
    assert _ids(dst) == wed_ids and _ids(src) == day_ids and not _ids(src) & _ids(dst)
    assert _ids(dst, "faces").isdisjoint(_ids(src, "faces"))
    assert db.new_id(dst, "shots") > max(wed_ids | day_ids) and db.new_id(src, "shots") > max(wed_ids | day_ids)
    # Albums: one moved whole, the other is in both with its own photos.
    albums = lambda c: {r[0] for r in c.execute("SELECT name FROM albums")}  # noqa: E731
    assert albums(dst) == {"Best of", "Wedding album"} and albums(src) == {"Best of"}
    # Shot list, merged events and the rules kept by folder followed the photos.
    assert dst.execute("SELECT title FROM shotlist").fetchone()[0] == "First dance"
    assert db.get_setting(dst, "shotlist_tags") == {str(s["shotlist"]): [s["wed"][3]]}
    assert db.get_setting(src, "shotlist_tags") in ({}, None)
    assert db.get_setting(dst, "event_joins") == [{"kind": "event", "a": s["wed"][0], "b": s["wed"][4]}]
    assert db.get_setting(src, "event_joins") == []
    assert db.get_setting(dst, "folder_names") == {str(lib / WEDDINGS[0]): "The vows"}
    assert db.get_setting(src, "folder_names") == {str(lib / EVERYDAY[0]): "Spring"}
    assert db.get_setting(dst, "keep_under") == [str(lib / WEDDINGS[2])] and db.get_setting(src, "keep_under") == []
    # Its purpose, and the folders: the new library's own, left out of the old one.
    assert db.get_setting(dst, "purpose") == "shoot" and db.get_setting(dst, "grouping") == config.SHOOT_GROUPING
    assert [r[0] for r in dst.execute("SELECT path FROM roots")] == [weddings]
    assert db.excluded_folders(src) == [weddings]
    assert dst.execute("SELECT COUNT(*) FROM files WHERE root_id IS NULL").fetchone()[0] == 0
    # Thumbnails went with them (linked), and no longer sit in the old library's folder.
    for sid, thumb in dst.execute("SELECT id, thumb FROM shots WHERE thumb IS NOT NULL"):
        assert thumb.startswith(str(config.project_dir("weddings"))) and os.path.isfile(thumb)
    assert not any((config.project_dir("t") / "thumbs" / f"{i % 100:02d}" / f"{i}.jpg").exists() for i in wed_ids)
    # Both catalogs were backed up first, under the move's name, and the log says what happened.
    assert os.path.isfile(op["backups"]["t"]) and "before-move-" in op["backups"]["t"]
    assert split.history("t")[0]["can_move_back"]
    # Undo in the new library undoes the latest change made to those photos.
    from joeylab import library

    got = library.undo(dst)
    assert got and set(got["ids"]) <= wed_ids


def test_a_split_never_touches_photo_files(env):
    from joeylab import split

    _, lib = env
    synthetic_library(lib)
    stats = {p: os.stat(p) for p in map(str, lib.rglob("*")) if os.path.isfile(p)}
    split.run("t", [str(lib / "Weddings")], "weddings", "shoot")
    for p, st in stats.items():
        now = os.stat(p)
        assert (now.st_size, now.st_mtime_ns, now.st_ino) == (st.st_size, st.st_mtime_ns, st.st_ino)


def test_move_them_back_restores_the_original(env):
    from joeylab import config, db, split

    _, lib = env
    s = synthetic_library(lib)
    conn = s["conn"]
    before = _state(conn)
    ids_before = {t: _ids(conn, t) for t in ("shots", "files", "faces", "decision_log", "people", "albums", "keywords")}
    roots_before = [r[0] for r in conn.execute("SELECT path FROM roots")]
    conn.close()

    op = split.run("t", [str(lib / "Weddings")], "weddings", "shoot")
    back = split.move_back(op["id"])
    conn = db.connect("t")
    assert _state(conn) == before
    for t, ids in ids_before.items():
        assert _ids(conn, t) == ids, t                      # the same ids, so links and history point where they did
    assert [r[0] for r in conn.execute("SELECT path FROM roots")] == roots_before
    assert db.excluded_folders(conn) == []
    # The library the move made was empty again, so it went (its catalog is kept).
    assert "weddings" not in config.list_projects() and os.path.isfile(back["removed_library"]["catalog"])
    assert split.load(op["id"])["moved_back"] == back["id"]
    with pytest.raises(split.Refused, match="moved back already"):
        split.move_back(op["id"])


def test_moving_into_a_library_with_its_own_photos_never_reuses_an_id(env, tmp_path):
    """The other library's own photos have the same ids as the moved ones: those get new ids there, the
    history and albums follow them, people and keywords join the ones of the same name, and the history
    that came along goes before the library's own (its Undo reaches its own changes first)."""
    from joeylab import db, keywords, library, split

    _, lib = env
    s = synthetic_library(lib)
    s["conn"].close()
    other = tmp_path / "other"
    for i in range(10):
        jpeg_at(other / f"DSC_{i:04d}.JPG", seed=500 + i, when=f"2023:03:0{i % 9 + 1} 10:00:00")
    add_root(other, "family")
    fam, _ = run_all("family")
    fam_ids = _ids(fam)
    assert fam_ids & set(s["wed"])                          # the same numbers, different photos
    sam = db.new_id(fam, "people")
    fam.execute("INSERT INTO people(id, name, kind) VALUES (?, 'Sam', 'person')", (sam,))
    fam.execute("INSERT INTO faces(id, shot_id, x, y, w, h, person_id, person_src) VALUES (?, ?, .1, .1, .1, .1, ?, 'user')",
                (db.new_id(fam, "faces"), min(fam_ids), sam))
    fam.commit()
    fam_kw = keywords.ensure(fam, "Family")
    keywords.assign(fam, [min(fam_ids)], add=[fam_kw], what="tag")
    library.decide(fam, [{"id": max(fam_ids), "pick": 1}], what="their own pick")
    fam_before = _state(fam)
    src_before = _state(db.connect("t"))
    fam.close()

    split.run("t", [str(lib / "Weddings")], "family")
    fam = db.connect("family")
    after = _state(fam)
    moved = _only(src_before, str(lib / "Weddings"))
    for k in ("decisions", "faces", "keywords", "raw", "favs", "comments", "backups", "dates", "history"):
        assert sorted(after[k]) == sorted(fam_before[k] + moved[k]), k
    # Its own photos kept their ids; the moved ones got new ones above everything used there.
    assert fam_ids <= _ids(fam)
    new_ids = _ids(fam) - fam_ids
    assert len(new_ids) == len(s["wed"]) and min(new_ids) > max(fam_ids)
    # One Sam, one "Family" keyword: joined, not doubled.
    assert fam.execute("SELECT COUNT(*) FROM people WHERE name='Sam'").fetchone()[0] == 1
    assert fam.execute("SELECT COUNT(*) FROM keywords WHERE name='Family'").fetchone()[0] == 1
    # Undo there reaches its own pick first.
    assert library.undo(fam)["label"] == "their own pick"
    library.redo(fam)
    fam.close()

    # Moving them back finds their old ids free (the source gave them away), so they come back as they were.
    split.move_back(split.history("family")[0]["id"])
    assert _state(db.connect("t")) == src_before
    assert _ids(db.connect("t")) == set(s["wed"]) | set(s["day"])
    assert _ids(db.connect("family")) == fam_ids
    assert _state(db.connect("family")) == fam_before | {"history": _state(db.connect("family"))["history"]}


def test_the_source_leaves_the_folders_out_from_then_on(env):
    from joeylab import db, scanner, split
    from joeylab.web import watch

    _, lib = env
    s = synthetic_library(lib)
    s["conn"].close()
    split.run("t", [str(lib / "Weddings")], "weddings", "shoot")
    new_photo = jpeg_at(lib / WEDDINGS[1] / "IMG_9999.JPG", seed=999, when="2024:05:05 10:00:00")
    src = db.connect("t")
    n = src.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
    rep = scanner.scan(src)
    assert rep.added == 0 and rep.missing == 0 and src.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == n
    assert not any(p.startswith(str(lib / "Weddings")) for p in rep.stats)        # not even walked
    dst = db.connect("weddings")
    rep = scanner.scan(dst)
    assert rep.added == 1 and rep.missing == 0
    assert dst.execute("SELECT 1 FROM files WHERE path=?", (str(new_photo),)).fetchone()
    # Adding the folder back to the source is refused with the reason.
    with pytest.raises(ValueError, match="moved to another library"):
        scanner.add_root(src, lib / "Weddings" / "Our wedding")
    # The folder watcher doesn't wake the source for changes there.
    touched = []
    w = watch.FolderWatcher.__new__(watch.FolderWatcher)
    w.excluded, w.touch = {"t": db.excluded_folders(src)}, touched.append
    h = watch._Handler(w, "t")
    ev = type("E", (), {"src_path": str(new_photo), "dest_path": "", "is_directory": False, "event_type": "created"})()
    h.dispatch(ev)
    assert touched == []
    ev.src_path = str(lib / EVERYDAY[0] / "IMG_new.JPG")
    h.dispatch(ev)
    assert touched == ["t"]


def test_a_stop_half_way_leaves_the_source_as_it_was(env, monkeypatch):
    from joeylab import config, db, split

    _, lib = env
    s = synthetic_library(lib)
    s["conn"].close()
    before = _state(db.connect("t"))

    # While the other library is being written: nothing anywhere changes, and no library is made.
    real = split._verify

    def broken(ct):
        raise RuntimeError("power cut")

    monkeypatch.setattr(split, "_verify", broken)
    with pytest.raises(RuntimeError, match="power cut"):
        split.run("t", [str(lib / "Weddings")], "weddings", "shoot")
    assert _state(db.connect("t")) == before
    assert "weddings" not in config.list_projects() and not any((config.HOME / "staging").iterdir())
    assert split.history("t")[0]["state"] == "failed"
    monkeypatch.setattr(split, "_verify", real)

    # After the other library has its copy but before the photos leave: the source is as it was, both
    # have them, and finishing it takes them out of the source.
    real_remove = split._remove_from_source

    def stop(cs, chk, moved, maps):
        real_remove(cs, chk, moved, maps)
        raise KeyboardInterrupt

    monkeypatch.setattr(split, "_remove_from_source", stop)
    with pytest.raises(KeyboardInterrupt):
        split.run("t", [str(lib / "Weddings")], "weddings", "shoot")
    monkeypatch.setattr(split, "_remove_from_source", real_remove)
    assert _state(db.connect("t")) == before
    op = split.history("t")[0]
    assert op["state"] == "copied" and "weddings" in config.list_projects()
    with pytest.raises(split.Refused, match="stopped half way"):
        split.plan("t", [str(lib / "Everyday")], "everyday")
    split.finish(op["id"])
    src = db.connect("t")
    assert _state(src)["photos"] == [p for p in before["photos"] if "/Weddings/" not in p]
    assert db.excluded_folders(src) == [str(lib / "Weddings")]


def test_folders_belong_to_one_library(env, tmp_path):
    from joeylab import db, libraries, split

    _, lib = env
    s = synthetic_library(lib)
    s["conn"].close()
    # A folder another library includes can't be a new library's, nor be added to another one.
    check = libraries.check_folder(str(lib / "Weddings"))
    assert not check["ok"] and "already includes" in check["reason"] and "Move to another library" in check["reason"]
    assert libraries.check_folder(str(lib / "Weddings"), library="t")["ok"]       # its own: fine
    other = tmp_path / "elsewhere"
    make_jpeg(other / "a.jpg", seed=1)
    add_root(other, "third")
    run_all("third")[0].close()
    assert "holds" in libraries.check_folder(str(tmp_path))["reason"]
    # Refusals, each with its reason.
    with pytest.raises(split.Refused, match="has no folder"):
        split.plan("t", [str(lib / "Nope")], "weddings")
    with pytest.raises(split.Refused, match="in “t” already"):
        split.plan("t", [str(lib / "Weddings")], "t")
    with pytest.raises(split.Refused, match="has no folder"):
        split.plan("t", [str(other)], "weddings")
    with pytest.raises(split.Refused, match="library already"):
        split.plan("t", [str(lib / "Weddings")], "third", "shoot")
    split.run("t", [str(lib / "Weddings")], "weddings", "shoot")
    with pytest.raises(split.Refused, match="moved to the library “weddings”"):
        split.plan("t", [str(lib / WEDDINGS[0])], "other")
    # Including it again is refused while another library has it; once that one's gone, it's allowed.
    with pytest.raises(split.Refused, match="in the library “weddings” now"):
        split.include_again("t", str(lib / "Weddings"))
    libraries.remove("weddings", "weddings")
    assert split.include_again("t", str(lib / "Weddings"))["library"] == "weddings"
    assert db.excluded_folders(db.connect("t")) == []


def test_thumbnails_and_previews_follow_under_their_new_ids(env, tmp_path, monkeypatch):
    """Previews, zoom tiles and face crops go with the photos (renamed, under their ids in the other
    library); a thumbnail kept anywhere but the library's own folder is left alone, and its photo is read
    again there; a stop while they move loses none of them."""
    from joeylab import config, db, split

    _, lib = env
    s = synthetic_library(lib)
    conn = s["conn"]
    other = tmp_path / "other"
    for i in range(10):
        jpeg_at(other / f"DSC_{i:04d}.JPG", seed=500 + i, when=f"2023:03:0{i % 9 + 1} 10:00:00")
    add_root(other, "family")
    run_all("family")[0].close()
    src_dir = config.project_dir("t")
    sid, fid = s["wed"][0], conn.execute("SELECT id FROM faces WHERE shot_id=?", (s["wed"][0],)).fetchone()[0]
    for rel in (f"previews/{sid % 100:02d}/{sid}_abc.jpg", f"thumbs_s/{sid % 100:02d}/{sid}_abc.jpg",
                f"previews/faces/{fid % 100:02d}/{fid}_abc.jpg", f"previews/{sid % 100:02d}/{sid}_abc_tiles/1_0_0.jpg"):
        (src_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (src_dir / rel).write_bytes(b"jpeg")
    outside = tmp_path / "elsewhere.jpg"                     # a thumbnail somewhere else: never taken
    outside.write_bytes(b"jpeg")
    away = s["wed"][1]
    conn.execute("UPDATE shots SET thumb=? WHERE id=?", (str(outside), away))
    (src_dir / "thumbs" / f"{away % 100:02d}" / f"{away}.jpg").unlink()
    conn.commit()
    conn.close()

    real = split._move_cached
    monkeypatch.setattr(split, "_move_cached", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        split.run("t", [str(lib / "Weddings")], "family")
    monkeypatch.setattr(split, "_move_cached", real)
    op = split.history("t")[0]
    assert op["state"] == "removed"                          # the catalogs are done; the images wait
    assert (src_dir / "thumbs" / f"{sid % 100:02d}" / f"{sid}.jpg").exists()
    split.finish(op["id"])

    fam = db.connect("family")
    new_sid = fam.execute("SELECT shot_id FROM files WHERE path=?", (_path_in_backup(op, sid),)).fetchone()[0]
    new_fid = fam.execute("SELECT id FROM faces WHERE shot_id=? ORDER BY id LIMIT 1", (new_sid,)).fetchone()[0]
    assert new_sid != sid                                    # (the family library had its own photo with that id)
    fam_dir = config.project_dir("family")
    for rel in (f"thumbs/{new_sid % 100:02d}/{new_sid}.jpg", f"previews/{new_sid % 100:02d}/{new_sid}_abc.jpg",
                f"thumbs_s/{new_sid % 100:02d}/{new_sid}_abc.jpg", f"previews/faces/{new_fid % 100:02d}/{new_fid}_abc.jpg",
                f"previews/{new_sid % 100:02d}/{new_sid}_abc_tiles/1_0_0.jpg"):
        assert (fam_dir / rel).exists(), rel
    assert not any((src_dir / "thumbs").rglob(f"{sid}.jpg"))
    assert fam.execute("SELECT thumb FROM shots WHERE id=?", (new_sid,)).fetchone()[0] == str(fam_dir / "thumbs" / f"{new_sid % 100:02d}" / f"{new_sid}.jpg")
    # The photo whose thumbnail was elsewhere is read again there; the file itself wasn't touched.
    lost = fam.execute("SELECT id FROM shots WHERE thumb IS NULL AND analyzed_fp IS NULL").fetchall()
    assert len(lost) == 1 and outside.read_bytes() == b"jpeg" and os.stat(outside).st_nlink == 1
    assert split.history("t")[0]["state"] == "done"


def _path_in_backup(op, sid):
    import sqlite3

    c = sqlite3.connect(op["backups"]["t"])
    try:
        return c.execute("SELECT MIN(path) FROM files WHERE shot_id=?", (sid,)).fetchone()[0]
    finally:
        c.close()


def _follow(c, job):
    for _ in range(300):
        if job["state"] not in ("moving", "waiting"):
            break
        time.sleep(0.1)
        job = c.get(f"/api/split/{job['token']}").json()
    return job


def test_the_app_shows_the_plan_moves_and_moves_back(env):
    from joeylab import config
    from joeylab.web.jobs import JobManager
    from joeylab.web.server import create_app

    _, lib = env
    s = synthetic_library(lib)
    s["conn"].close()
    c = TestClient(create_app(jobs=JobManager(watch_minutes=0, faces=False, ai_layer=False)))
    weddings = str(lib / "Weddings")
    body = {"folders": [weddings], "to": "Weddings", "purpose": "shoot"}
    p = c.post("/api/p/t/split/plan", json=body).json()
    assert p["dst"] == "weddings" and p["what"].startswith("Move ") and "a new shoot library “weddings”" in p["what"]
    bad = c.post("/api/p/t/split/plan", json={"folders": [str(lib / "Nope")], "to": "x"})
    assert bad.status_code == 400 and "has no folder" in bad.json()["error"]
    assert TestClient(create_app(), client=("100.101.102.103", 5000)).post("/api/p/t/split/plan", json=body).status_code == 403

    job = _follow(c, c.post("/api/p/t/split/run", json=body).json())
    assert job["state"] == "done", job
    assert "weddings" in config.list_projects()
    info = c.get("/api/p/t/split").json()
    assert [e["folder"] for e in info["excluded"]] == [weddings] and info["excluded"][0]["there"]
    assert info["moves"][0]["can_move_back"] and info["moves"][0]["dst"] == "weddings"
    no = c.post("/api/p/t/split/include", json={"folder": weddings})
    assert no.status_code == 400 and "Move it back from there" in no.json()["error"]
    for _ in range(300):              # the regroup in the background, then Move them back
        if not c.get(f"/api/split/{job['token']}").json()["regrouping"]:
            break
        time.sleep(0.1)
    back = _follow(c, c.post(f"/api/split/{info['moves'][0]['id']}/back").json())
    assert back["state"] == "done", back
    for _ in range(300):
        if not c.app.state.bg.running:
            break
        time.sleep(0.1)
    assert "weddings" not in config.list_projects() and c.get("/api/p/t/split").json()["excluded"] == []
    # A smart album can follow several folders (the lighter way to see them together).
    a = c.post("/api/p/t/albums", json={"name": "Weddings", "smart": {"folder": f"{lib / WEDDINGS[0]}\n{lib / WEDDINGS[2]}"}}).json()
    albums = {x["id"]: x for x in c.get("/api/p/t/albums").json()["albums"]}
    assert albums[a["id"]]["items"] == 9                     # 4 + 4 + the undated scan


def test_the_command_line_moves_folders_and_moves_them_back(env):
    from typer.testing import CliRunner

    from joeylab import config, split
    from joeylab.cli import app

    _, lib = env
    s = synthetic_library(lib)
    s["conn"].close()
    run = CliRunner().invoke
    r = run(app, ["library", "split", "t", "--folders", str(lib / WEDDINGS[0]), str(lib / WEDDINGS[1]), "--to", "weddings",
                  "--purpose", "shoot", "--yes", "--no-regroup"])
    assert r.exit_code == 0, r.output
    assert "Move 8 photos" in r.output and "Moved 8 photos" in r.output and "joeylab library move-back" in r.output
    assert "weddings" in config.list_projects()
    r = run(app, ["library", "split", "t", "--folders", "Weddings/Our wedding", "--to", "weddings", "--yes", "--no-regroup"])
    assert r.exit_code == 0, r.output                        # a folder by its path inside the library, to an existing one
    r = run(app, ["library", "moves", "t"])
    assert r.exit_code == 0 and r.output.count("t → weddings") == 2
    r = run(app, ["library", "split", "t", "--folders", str(lib / "Nope"), "--to", "x", "--yes"])
    assert r.exit_code == 1 and "has no folder" in r.output
    for op in split.history("t"):
        r = run(app, ["library", "move-back", op["id"], "--no-regroup"])
        assert r.exit_code == 0, r.output
    assert "weddings" not in config.list_projects()


def test_both_libraries_are_regrouped_afterwards(env):
    from joeylab import db, split

    _, lib = env
    s = synthetic_library(lib)
    s["conn"].close()
    split.run("t", [str(lib / "Weddings")], "weddings", "shoot")
    for name in ("t", "weddings"):
        conn = db.connect(name)
        took = split.recompute(conn, name)
        assert set(took) == {"organise", "faces", "search"}
        n = conn.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0]
        grouped = conn.execute("SELECT COUNT(*) FROM shots WHERE group_id IS NOT NULL AND status='present'").fetchone()[0]
        assert grouped == n                              # every photo is in a stack of this library's
        assert not conn.execute("SELECT 1 FROM groups g WHERE NOT EXISTS (SELECT 1 FROM shots s WHERE s.group_id = g.id)").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM face_groups").fetchone()[0] >= 1
