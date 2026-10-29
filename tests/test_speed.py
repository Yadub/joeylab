"""Big libraries: planner statistics kept current, the Library's counts in one pass, the AI models and
vectors held only while needed, and picture search scoring only what a filter leaves."""

from __future__ import annotations

import pytest

from conftest import add_root, jpeg_at, run_all


def _library(lib, n=6):
    for i in range(n):
        jpeg_at(lib / f"IMG_{i}.JPG", i, f"2024:05:0{i + 1} 10:00:00")
    add_root(lib)
    return run_all()[0]


def _shots_stat(conn) -> int | None:
    row = conn.execute("SELECT stat FROM sqlite_stat1 WHERE tbl='shots' LIMIT 1").fetchone()
    return int(row[0].split()[0]) if row else None


# ---------------------------------------------------------------- planner statistics

def test_the_upgrade_takes_the_photo_tables_statistics_in_full(env):
    from joeylab import db

    _, lib = env
    conn = _library(lib)
    # As PRAGMA optimize leaves them on a big library: a few thousand rows sampled.
    conn.execute("DELETE FROM sqlite_stat1 WHERE tbl IN ('shots', 'metrics', 'decisions', 'groups')")
    conn.execute("INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES ('shots', 'shots_status_time', '75093 2001 4')")
    conn.commit()
    db._m18_statistics(conn)
    conn.commit()
    stats = {(r[0], r[1]): r[2] for r in conn.execute("SELECT tbl, idx, stat FROM sqlite_stat1")}
    assert stats[("shots", "shots_status_time")].split()[:2] == ["6", "6"]        # every photo is present
    assert {t for t, _ in stats} >= {"shots", "metrics", "groups"}


def test_statistics_are_taken_again_only_when_the_photo_count_moves(env):
    from joeylab import db

    tmp, lib = env
    conn = _library(lib)
    db.analyze_counted(conn)
    conn.commit()
    assert _shots_stat(conn) == 6 and not db.refresh_stats(conn)
    jpeg_at(lib / "IMG_9.JPG", 9, "2024:05:09 10:00:00")              # one more photo of 6: past 10%
    conn, _ = run_all()
    assert _shots_stat(conn) == 6
    assert db.refresh_stats(conn) and _shots_stat(conn) == 7
    assert not db.refresh_stats(conn)
    # A catalog never analysed gets its statistics at the end of its first update.
    conn.execute("DROP TABLE sqlite_stat1")
    conn.commit()
    assert db.refresh_stats(conn) and _shots_stat(conn) == 7


def test_an_update_ends_with_current_statistics(env):
    from joeylab import pipeline

    _, lib = env
    conn = _library(lib)
    conn.execute("DELETE FROM sqlite_stat1 WHERE tbl='shots'")
    conn.execute("INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES ('shots', 'shots_status_time', '2 2 1')")
    conn.commit()

    pipeline.refine(conn, pipeline.Reporter(), regroup=False)
    assert _shots_stat(conn) == 6


# ---------------------------------------------------------------- the AI models and vectors

def test_checking_for_the_ai_extra_does_not_import_it():
    import subprocess
    import sys

    code = ("import sys; from joeylab import ai, search; ai.available(); search.available();"
            " print(sorted(m for m in ('torch', 'open_clip', 'transformers') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def _with_vectors(conn, ids, dim=8, seed=0):
    import numpy as np

    from joeylab import ai, db

    ai.ensure_schema(conn)
    rng = np.random.default_rng(seed)
    vecs = {}
    for sid in ids:
        v = rng.normal(size=dim).astype(np.float32)
        v = (v / np.linalg.norm(v)).astype(np.float16)
        fp = conn.execute("SELECT analyzed_fp FROM shots WHERE id=?", (sid,)).fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO embeddings(shot_id, model, src, vec) VALUES (?,?,?,?)",
                     (sid, ai.MODEL_TAG, fp, v.tobytes()))
        vecs[sid] = v.astype(np.float32)
    db.bump(conn, "emb_gen")
    conn.commit()
    return vecs


def test_clip_vectors_are_held_once_and_let_go_when_idle(env):
    import numpy as np

    from joeylab import ai

    _, lib = env
    conn = _library(lib)
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    vecs = _with_vectors(conn, ids)
    embs = ai.load_embeddings(conn)
    got_ids, mat = ai._matrix(conn)
    assert set(embs) == set(ids) and mat.dtype == np.float32
    for k, sid in enumerate(got_ids):
        assert np.array_equal(embs[int(sid)], vecs[int(sid)])          # exactly the stored vectors
        assert np.shares_memory(embs[int(sid)], mat) and np.array_equal(mat[k], vecs[int(sid)])
    assert ai.load_embeddings(conn) is embs                             # kept until they change
    assert not ai.release_idle()                                        # just used
    assert ai.release_idle(0) and not ai._VECS
    assert ai.load_embeddings(conn) is not embs and set(ai.load_embeddings(conn)) == set(ids)


def test_clip_is_loaded_only_when_needed_and_let_go_when_idle(monkeypatch):
    import threading
    import time

    from joeylab import ai

    made = []

    def fake_init(self, text_only=False):
        self.text_only, self.lock, self.used = text_only, threading.RLock(), time.monotonic()
        made.append(text_only)

    monkeypatch.setattr(ai.Models, "__init__", fake_init)
    monkeypatch.setattr(ai.Models, "_inst", None)
    monkeypatch.setattr(ai, "_VECS", {})
    text = ai.Models.get(text_only=True)
    assert text.text_only and ai.Models.get(text_only=True) is text
    full = ai.Models.get()                       # a photo to embed: the whole model in place of the text half
    assert not full.text_only and ai.Models._inst is full and made == [True, False]
    assert ai.Models.get(text_only=True) is full  # the whole model answers text too
    with pytest.raises(RuntimeError):
        text.embed_images([])
    # In use (another thread holds it): kept, however long since it was last asked for.
    held = threading.Event()
    done = threading.Event()

    def use():
        with full.lock:
            held.set()
            done.wait(5)

    t = threading.Thread(target=use)
    t.start()
    held.wait(5)
    assert not ai.release_idle(0) and ai.Models.loaded()
    done.set()
    t.join()
    assert not ai.release_idle()                 # idle, but not for IDLE_S yet
    assert ai.release_idle(0) and not ai.Models.loaded()


def test_the_service_warms_clip_only_for_a_library_without_a_search_index(env, monkeypatch):
    from joeylab import ai, db, search

    _, lib = env
    conn = _library(lib)
    monkeypatch.setattr(search, "available", lambda: True)
    assert not ai.search_needs_model()                                  # no vectors at all: nothing to search
    ids = [r[0] for r in conn.execute("SELECT id FROM shots")]
    _with_vectors(conn, ids)
    assert ai.search_needs_model()                                      # its searches would use CLIP
    search.ensure_schema(conn)
    conn.execute("INSERT INTO search_vecs(shot_id, src, vecs) VALUES (?, 'x', x'00')", (ids[0],))
    conn.commit()
    assert not ai.search_needs_model()                                  # picture search answers instead
    monkeypatch.setattr(search, "available", lambda: False)
    assert ai.search_needs_model()
    loads = []
    monkeypatch.setattr(ai.Models, "get", classmethod(lambda cls, text_only=False: loads.append(text_only)))
    monkeypatch.setattr(search, "available", lambda: True)
    ai.warm_up()
    assert loads == []
    db.connect("t").execute("DELETE FROM search_vecs").connection.commit()
    ai.warm_up()                                 # (the fake model can't embed: the warm-up keeps quiet)
    assert loads == [True]


# ---------------------------------------------------------------- the Library's counts

def _old_counts(conn, q: dict) -> dict:
    """The Library's first-page counts as three separate counts (before they were one pass)."""
    import dataclasses

    from joeylab import library
    from joeylab.web.deps import filter_for
    from joeylab.web.routes.library import (browse_filter, duplicates_note, hidden_by_browsing, settings_of,
                                            without_extras)

    raw = filter_for(conn, q)
    flt = raw if q.get("all") in ("1", "true") else browse_filter(conn, dataclasses.replace(raw))
    stack = q.get("stack") in ("1", "true")
    offset = int(q.get("offset") or 0)
    _, total = library.page(conn, flt, offset=offset, stack=stack, with_total=offset == 0 or q.get("total") == "1")
    if settings_of(conn)["hide_duplicates"] and flt is not raw:
        raw = without_extras(dataclasses.replace(raw))
    return {"total": total, "hidden": hidden_by_browsing(conn, raw, flt, total, stack),
            "duplicates": duplicates_note(conn, q, None) if offset == 0 else None}


def test_the_library_counts_in_one_pass_exactly_as_before(env):
    import itertools

    from fastapi.testclient import TestClient

    from joeylab import db, library
    from joeylab.web.routes import library as routes
    from joeylab.web.server import create_app
    from conftest import synthetic_library

    tmp, lib = env
    s = synthetic_library(lib)
    conn = s["conn"]
    wed, day = s["wed"], s["day"]
    # Something of every kind browsing hides or notes: a thumbnail, extra copies, a utility shot, a photo
    # that couldn't be read, and a backup of another drive's photo.
    for sid, flags in ((wed[5], '["tiny"]'), (wed[6], '["duplicate"]'), (day[4], '["duplicate", "blurry"]'),
                       (day[5], '["utility"]'), (wed[7], '["backup-copy"]')):
        conn.execute("UPDATE metrics SET flags=? WHERE shot_id=?", (flags, sid))
    conn.execute("UPDATE shots SET error='unreadable', analyzed_fp=NULL WHERE id=?", (day[6],))
    conn.commit()
    c = TestClient(create_app())
    queries = [{}, {"status": "picks"}, {"status": "undecided"}, {"min_rating": "2"}, {"flag": "duplicate"},
               {"flag": "blurry"}, {"no_flag": "duplicate"}, {"all": "1"}, {"media": "photo"},
               {"person": str(s["people"]["Sam"])}, {"folder": str(lib / "Weddings")}, {"keyword": str(s["keywords"][1])},
               {"offset": "2", "total": "1"}, {"offset": "2"}]
    seen = 0
    for hide_dups, hide_utility in itertools.product((False, True), (False, True)):
        db.set_setting(conn, "hide_duplicates", hide_dups)
        db.set_setting(conn, "hide_utility", hide_utility)
        conn.commit()
        for q, stack in itertools.product(queries, ("", "1")):
            q = {**q, "stack": stack} if stack else q
            want = _old_counts(conn, q)
            got = c.get("/api/p/t/shots", params={**q, "limit": 2}).json()
            assert {k: got[k] for k in want} == want, (q, hide_dups, hide_utility)
            seen += bool(want["hidden"]) + bool(want["duplicates"] and want["duplicates"]["n"])
    assert seen > 20                            # (the cases count something)
    # Kept until the counts change: a decision changes the picks' count at once.
    picks = c.get("/api/p/t/shots", params={"status": "picks"}).json()["total"]
    assert routes._COUNTS
    library.decide(conn, [{"id": day[1], "pick": 1}], what="pick")
    assert c.get("/api/p/t/shots", params={"status": "picks"}).json()["total"] == picks + 1


# ---------------------------------------------------------------- picture vectors read from the kept store

def _with_search_vectors(conn, ids, dim=16, seed=1, stale=()):
    """Search vectors (every crop) for these photos; `stale` ones from an earlier version of the photo."""
    import numpy as np

    from joeylab import db, search

    search.ensure_schema(conn)
    rng = np.random.default_rng(seed)
    out = {}
    for sid in ids:
        v = rng.normal(size=(search.N_CROPS, dim)).astype(np.float32)
        v = (v / np.linalg.norm(v, axis=1, keepdims=True)).astype(np.float16)
        fp = conn.execute("SELECT analyzed_fp FROM shots WHERE id=?", (sid,)).fetchone()[0]
        src = f"old:{search.INDEX_VERSION}" if sid in stale else f"{fp}:{search.INDEX_VERSION}"
        conn.execute("INSERT OR REPLACE INTO search_vecs(shot_id, src, vecs) VALUES (?,?,?)", (sid, src, v.tobytes()))
        out[sid] = v
    db.bump(conn, "search_gen")
    conn.commit()
    return out


def test_event_kinds_read_whole_frames_from_the_kept_store(env, monkeypatch):
    import numpy as np

    from joeylab import scenes

    _, lib = env
    conn = _library(lib)
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    assert len(scenes.whole_frames(conn, ids)[0]) == 0                  # no index yet
    _with_vectors(conn, ids)
    assert len(scenes.whole_frames(conn, ids)[0]) == 0                  # CLIP's vectors are not the kinds' space
    vecs = _with_search_vectors(conn, ids, dim=768, stale={ids[1]})
    read = []
    real = scenes._read_frames
    monkeypatch.setattr(scenes, "_read_frames", lambda conn, rest, dim: read.append(rest) or real(conn, rest, dim))
    want = list(reversed(ids))[:4] + [ids[1]]
    keys, X = scenes.whole_frames(conn, want + [999999])
    assert keys.tolist() == sorted(want) and X.dtype == np.float16
    for k, sid in enumerate(keys.tolist()):
        assert np.array_equal(X[k], vecs[sid][0])                       # each photo's whole frame
    assert read == [[ids[1], 999999]]          # only what the kept store lacks is read from the catalog


def test_explore_asks_whether_there_are_vectors_without_reading_them(env, monkeypatch):
    from joeylab import embeddings, explore

    _, lib = env
    conn = _library(lib)
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    monkeypatch.setattr(embeddings, "matrix", lambda conn: (_ for _ in ()).throw(AssertionError("read them")))
    assert not embeddings.has_vectors(conn) and explore.available(conn)["content"] is not True
    _with_search_vectors(conn, ids[:2], stale=set(ids[:2]))
    assert not embeddings.has_vectors(conn)                             # only out of date ones
    _with_search_vectors(conn, ids[2:3])
    assert embeddings.has_vectors(conn) and explore.available(conn)["content"] is True
    conn.execute("DELETE FROM search_vecs")
    _with_vectors(conn, ids[:1])
    assert embeddings.has_vectors(conn)                                 # CLIP's, when there's no search index


# ---------------------------------------------------------------- picture search scoring

class _Words:
    """Stands in for SigLIP's text half: a fixed unit vector per text, and its calibration."""

    scale, bias = 12.0, -6.0

    def embed_text(self, text):
        import zlib

        import numpy as np

        v = np.random.default_rng(zlib.crc32(text.encode())).normal(size=16).astype(np.float32)
        return v / np.linalg.norm(v)

    def probability(self, cos):
        import numpy as np

        return 1 / (1 + np.exp(-(self.scale * cos + self.bias)))


def test_scoring_on_several_threads_gives_exactly_the_same_numbers():
    import numpy as np

    from joeylab import search

    rng = np.random.default_rng(3)
    mat = rng.normal(size=(1000, 24)).astype(np.float16)
    v = rng.normal(size=24).astype(np.float32)
    one_at_a_time = np.concatenate([mat[i : i + 64].astype(np.float32) @ v for i in range(0, len(mat), 64)])
    assert np.array_equal(search._dot(mat, v, chunk=64), one_at_a_time)
    rows = np.array([5, 6, 7, 400, 401, 999])
    assert np.allclose(search._dot(mat, v, chunk=4, rows=rows), one_at_a_time[rows], rtol=0, atol=1e-6)
    assert search._dot(mat[:0], v).shape == (0,)


def test_a_filtered_search_scores_only_the_filtered_photos(env, monkeypatch):
    import json

    import numpy as np

    from joeylab import search

    _, lib = env
    conn = _library(lib, n=8)
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    _with_search_vectors(conn, ids)
    for k, sid in enumerate(ids):                     # Apple's labels: some photos are "a dog"
        labels = [["dog", 0.9 - k / 20], ["grass", 0.4]] if k % 2 else [["people", 0.7]]
        conn.execute("INSERT INTO vision(shot_id, src, labels, text) VALUES (?, 'x', ?, ?)",
                     (sid, json.dumps(labels), "dog show" if k == 3 else ""))
        conn.execute("INSERT INTO search_fts(rowid, labels, text) VALUES (?, ?, ?)",
                     (sid, " ".join(lbl for lbl, _ in labels), "dog show" if k == 3 else ""))
    conn.commit()
    monkeypatch.setattr(search.Model, "get", classmethod(lambda cls: _Words()))
    scored = []
    real = search._dot
    monkeypatch.setattr(search, "_dot", lambda mat, v, chunk=32768, rows=None: scored.append(
        len(mat) if rows is None else len(rows)) or real(mat, v, chunk, rows))
    everything = {h["id"]: h for h in search.search(conn, "dog show", 100)}
    assert set(everything) == set(ids) and scored == [len(ids) * search.N_CROPS]
    some = set(ids[1:6:2]) | {ids[6]}
    scored.clear()
    hits = search.search(conn, "dog show", 100, restrict=some)
    assert scored == [len(some) * search.N_CROPS]                 # only their vectors
    assert {h["id"] for h in hits} == some
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    for h in hits:                                                # the same match as unfiltered, photo by photo
        assert {k: v for k, v in h.items() if k != "level"} == {k: v for k, v in everything[h["id"]].items() if k != "level"}
    # "Strong" is judged against the best match among the filtered photos.
    bar = max(search.STRONG_PROB, search.STRONG_OF_TOP * max(h["prob"] for h in hits))
    for h in hits:
        if h["prob"] >= bar + 1e-4:
            assert h["level"] == "strong"
    # Apple's labels and text are read only for the filtered photos, and give what they gave before.
    assert search._keyword_hits(conn, "dog show", some) == {k: v for k, v in search._keyword_hits(conn, "dog show").items() if k in some}
    assert search.search(conn, "dog show", 100, restrict=set()) == []
    two = search.search(conn, "dog on grass", 100, restrict=some)  # several parts: each scored on the filter only
    assert {h["id"] for h in two} == some and len(scored) == 1 + 3
    assert all(n == len(some) * search.N_CROPS for n in scored)
    assert np.isfinite([h["score"] for h in two]).all()


# ---------------------------------------------------------------- kept across restarts

def test_clock_suggestions_are_kept_on_disk_for_after_a_restart(env, monkeypatch):
    import time

    from joeylab import clocksync, config

    _, lib = env
    conn = _library(lib, n=3)
    made = []
    monkeypatch.setattr(clocksync, "_suggest", lambda c: made.append(1) or [{"camera": "b", "offset_s": 3600.0 * len(made)}])
    clocksync._SUGGESTED.clear()
    assert clocksync.suggest(conn, "t") == [{"camera": "b", "offset_s": 3600.0}] and made == [1]
    assert (config.project_dir("t") / clocksync.FILE).exists()
    clocksync._SUGGESTED.clear()                                    # a restart
    assert clocksync.suggest(conn, "t") == [{"camera": "b", "offset_s": 3600.0}] and made == [1]
    # Out of date when the service starts: the kept ones at once, newer ones worked out in the background.
    clocksync._SUGGESTED.clear()
    conn.execute("UPDATE shots SET capture_ts = capture_ts + 600")
    conn.commit()
    assert clocksync.suggest(conn, "t")[0]["offset_s"] == 3600.0
    end = time.time() + 10
    while clocksync.suggest(conn, "t")[0]["offset_s"] != 7200.0:
        assert time.time() < end
        time.sleep(0.05)
    clocksync._SUGGESTED.clear()
    assert clocksync.suggest(conn, "t")[0]["offset_s"] == 7200.0 and made == [1, 1]
    (config.project_dir("t") / clocksync.FILE).write_text("{not json")
    clocksync._SUGGESTED.clear()
    assert clocksync.suggest(conn, "t")[0]["offset_s"] == 10800.0     # a damaged file: worked out again


def test_the_grouping_preview_is_kept_on_disk_for_after_a_restart(env, monkeypatch):
    from joeylab import config, grouping

    _, lib = env
    conn = _library(lib)
    first = grouping.preview(conn)
    assert set(first) == set(grouping.GROUPING_LEVELS) and (config.project_dir("t") / grouping.PREVIEW_FILE).exists()
    grouping._PREVIEW.clear()                                       # a restart
    real = grouping._split
    monkeypatch.setattr(grouping, "_split", lambda *a: (_ for _ in ()).throw(AssertionError("worked out again")))
    assert grouping.preview(conn) == first
    monkeypatch.setattr(grouping, "_split", real)
    conn.execute("UPDATE shots SET eff_ts = eff_ts + 86400 * (id % 2)")     # every other photo a day later
    conn.commit()
    grouping._PREVIEW.clear()
    again = grouping.preview(conn)
    assert again != first and again[1]["stacks"] >= first[1]["stacks"]


def test_the_list_of_libraries_keeps_its_counts_until_they_change(env, monkeypatch):
    from fastapi.testclient import TestClient

    from joeylab import library
    from joeylab.web.routes import library as routes
    from joeylab.web.server import create_app

    _, lib = env
    conn = _library(lib)
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY id")]
    c = TestClient(create_app())
    first = c.get("/api/projects").json()[0]
    assert first["photos"] == 6 and first["picks"] == 0 and first["cover"]
    asked = []
    real = routes.one
    monkeypatch.setattr(routes, "one", lambda conn, q, *a: asked.append(q) or real(conn, q, *a))
    assert c.get("/api/projects").json()[0] == first and not asked              # kept
    library.decide(conn, [{"id": ids[2], "pick": 1, "rating": 5}], what="pick")
    now = c.get("/api/projects").json()[0]
    assert now["picks"] == 1 and now["cover"]["id"] == ids[2] and asked         # counted again at once


# ---------------------------------------------------------------- writing only what changed

def test_taste_scores_write_only_what_changed_a_chunk_at_a_time(env, monkeypatch):
    import numpy as np

    from joeylab import config, taste

    _, lib = env
    conn = _library(lib, n=7)
    config.HOME.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(5)
    np.savez(taste._model_path(), w=rng.normal(size=taste.N_SCALAR + 1), mu=np.zeros(taste.N_SCALAR),
             sd=np.ones(taste.N_SCALAR), use_emb=np.array([False]))
    monkeypatch.setattr(taste, "SCORE_CHUNK", 3)
    conn.execute("UPDATE metrics SET taste = NULL")
    conn.commit()
    assert taste.score(conn) == 7
    first = dict(conn.execute("SELECT shot_id, taste FROM metrics"))
    assert all(v is not None for v in first.values())
    before = conn.total_changes
    assert taste.score(conn) == 7 and conn.total_changes == before          # nothing changed: nothing written
    some = list(first)[:2]
    conn.executemany("UPDATE metrics SET taste = 0.5 WHERE shot_id = ?", [(i,) for i in some])
    conn.commit()
    before = conn.total_changes
    taste.score(conn)
    assert conn.total_changes - before == 2 and dict(conn.execute("SELECT shot_id, taste FROM metrics")) == first
    assert not conn.in_transaction


def test_places_are_written_only_for_photos_whose_place_changes(env, tmp_path, monkeypatch):
    from joeylab import geo

    g = tmp_path / "geonames"
    g.mkdir()
    (g / "countryInfo.txt").write_text("#\nIN\tIND\t356\tIN\tIndia\n")
    (g / "admin1CodesASCII.txt").write_text("IN.13\tKerala\tKerala\t1\n")
    rows = [("1", "Kochi", "9.9312", "76.2673", "IN", "13", "600000"), ("2", "Aluva", "10.1076", "76.3516", "IN", "13", "20000")]
    (g / "cities5000.txt").write_text("\n".join("\t".join([r[0], r[1], r[1], "", r[2], r[3], "P", "PPL", r[4], "", r[5], "", "", "", r[6]])
                                                for r in rows) + "\n")
    monkeypatch.setattr(geo, "GEO_DIR", g)
    monkeypatch.setattr(geo.Gazetteer, "_inst", None)
    _, lib = env
    conn = _library(lib, n=4)
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY eff_ts")]
    conn.execute("UPDATE shots SET eff_ts = ? + id * 60", (conn.execute("SELECT MIN(eff_ts) FROM shots").fetchone()[0],))
    conn.execute("UPDATE shots SET gps_lat=9.95, gps_lon=76.27 WHERE id=?", (ids[0],))
    conn.commit()
    first = geo.assign(conn, download=False)
    state = conn.execute("SELECT id, place_id, place_src FROM shots ORDER BY id").fetchall()
    assert first["gps"] == 1 and first["inferred"] == 3
    before = conn.total_changes
    assert geo.assign(conn, download=False) == first and conn.total_changes == before
    conn.execute("UPDATE shots SET gps_lat=10.11, gps_lon=76.35 WHERE id=?", (ids[0],))      # now at Aluva
    conn.commit()
    before = conn.total_changes
    # The photo, Aluva in the library's places, and the three photos placed by it.
    assert geo.assign(conn, download=False) == first and conn.total_changes - before == 1 + 1 + 3
    names = {r[0] for r in conn.execute("SELECT p.name FROM shots s JOIN places p ON p.id=s.place_id")}
    assert names == {"Aluva"} and len(state) == 4
