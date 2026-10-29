"""Decisions and their history: Undo and Redo of every kind of step, two changes at once, and the
decisions export (audit 5: M4, M13 and the export's catalog-internal settings)."""

from __future__ import annotations

import itertools
import json
import threading

from conftest import add_root, jpeg_at, make_jpeg


def _library(lib):
    """Two dated photos and a scan with no date of its own, read but not analysed (enough for decisions)."""
    from joeylab import db, scanner

    jpeg_at(lib / "IMG_0001.JPG", 1, "2025:10:01 10:00:00", size=(300, 200))
    jpeg_at(lib / "IMG_0002.JPG", 2, "2025:10:01 10:05:00", size=(300, 200))
    make_jpeg(lib / "SCAN_0001.JPG", seed=3, size=(300, 200))
    add_root(lib)
    conn = db.connect("t")
    scanner.scan(conn)
    ids = {r[0]: r[1] for r in conn.execute("SELECT stem, id FROM shots")}
    return conn, ids["IMG_0001"], ids["IMG_0002"], ids["SCAN_0001"]


def _stars(conn, sid):
    return conn.execute("SELECT COALESCE(rating, 0) FROM decisions WHERE shot_id=?", (sid,)).fetchone()[0]


# ---------------------------------------------------------------- M4: a redo is logged as a redo

def test_undo_after_redoing_a_date_reverses_the_date_not_the_step_before(env):
    from joeylab import dating, library

    _, lib = env
    conn, a, _, scan = _library(lib)
    library.decide(conn, [{"id": a, "rating": 3}], what="3★")
    dating.set_dates(conn, [scan], "1998-06-14")
    library.undo(conn)
    assert dating.manual(conn, [scan]) == {}
    redone = library.redo(conn)
    assert dating.manual(conn, [scan]) == {scan: "1998-06-14 12:00:00"}
    assert {r[0] for r in conn.execute("SELECT source FROM decision_log WHERE batch=?", (redone["batch"],))} == {"redo"}
    assert not library.can_redo(conn)                    # the redo is spent: nothing more to redo
    res = library.undo(conn)                             # Undo straight after Redo reverses the redo...
    assert res["label"] == "date: 1998-06-14"
    assert dating.manual(conn, [scan]) == {}
    assert _stars(conn, a) == 3                          # ...not the star before it
    assert library.redo(conn)["label"] == "date: 1998-06-14"
    assert dating.manual(conn, [scan]) == {scan: "1998-06-14 12:00:00"}


def test_undo_after_redoing_a_time_shift_reverses_the_shift_not_the_step_before(env):
    from joeylab import dating, library

    _, lib = env
    conn, a, b, _ = _library(lib)
    eff = conn.execute("SELECT eff_ts FROM shots WHERE id=?", (b,)).fetchone()[0]

    def eff_b():
        return conn.execute("SELECT eff_ts FROM shots WHERE id=?", (b,)).fetchone()[0]

    library.decide(conn, [{"id": a, "rating": 3}], what="3★")
    dating.shift_times(conn, [b], 3600)
    library.undo(conn)
    assert eff_b() == eff and dating.shifts(conn, [b]) == {}
    redone = library.redo(conn)
    assert eff_b() == eff + 3600
    assert {r[0] for r in conn.execute("SELECT source FROM decision_log WHERE batch=?", (redone["batch"],))} == {"redo"}
    assert not library.can_redo(conn)
    res = library.undo(conn)
    assert res["label"] == "time shifted +1 h"
    assert eff_b() == eff and dating.shifts(conn, [b]) == {}
    assert _stars(conn, a) == 3


# ---------------------------------------------------------------- M13: two changes at once

class _Rows(list):
    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class _Racing:
    """A connection on which, just after the first read starting with `at`, another change runs on a
    connection of its own (in another thread, as a second request would). If this one holds the write
    lock by then, the other waits for it: it's given half a second, then this one goes on and commits."""

    def __init__(self, conn, at: str, other):
        self.conn, self.at, self.other, self.thread, self.errors = conn, at, other, None, []

    def execute(self, sql, *args):
        cur = self.conn.execute(sql, *args)
        if self.thread is None and sql.startswith(self.at):
            rows = _Rows(cur.fetchall())                 # read in full first, as the code under test would
            self.thread = threading.Thread(target=self._run)
            self.thread.start()
            self.thread.join(0.5)
            return rows
        return cur

    def _run(self):
        try:
            self.other()
        except Exception as e:                           # noqa: BLE001 - reported by finish()
            self.errors.append(e)

    def finish(self):
        assert self.thread is not None, f"nothing read {self.at!r}"
        self.thread.join(60)
        assert not self.thread.is_alive() and not self.errors, self.errors

    def __getattr__(self, name):
        return getattr(self.conn, name)


def _chain(conn, sid, key):
    """Each step's "before" for this photo is the "after" of the step before it."""
    rows = [(json.loads(r[0]), json.loads(r[1])) for r in conn.execute(
        "SELECT before, after FROM decision_log WHERE shot_id=? AND json_type(before, ?) IS NOT NULL ORDER BY id",
        (sid, f"$.{key}"))]
    return all(b[key] == a0[key] for (_, a0), (b, _) in itertools.pairwise(rows)), rows


def test_a_bulk_reject_and_a_star_at_the_same_moment_both_count(env):
    from joeylab import db, library

    _, lib = env
    conn, a, b, scan = _library(lib)
    other = db.connect("t")
    racing = _Racing(conn, "SELECT rating, pick, label FROM decisions", lambda: library.decide(other, [{"id": a, "rating": 5}], what="5★"))
    library.decide(racing, [{"id": a, "pick": -1}, {"id": b, "pick": -1}, {"id": scan, "pick": -1}], what="reject")
    racing.finish()
    assert library._current(conn, a) == {"rating": 5, "pick": -1, "label": ""}
    assert library._current(conn, b)["pick"] == -1 and library._current(conn, scan)["pick"] == -1
    ok, rows = _chain(conn, a, "rating")
    assert ok and len(rows) == 2, rows
    assert library.undo(conn)["label"] == "5★" and library._current(conn, a) == {"rating": 0, "pick": -1, "label": ""}


def test_undo_and_a_new_star_at_the_same_moment_keep_the_new_star(env):
    from joeylab import db, library

    _, lib = env
    conn, a, _, _ = _library(lib)
    library.decide(conn, [{"id": a, "rating": 3}], what="3★")
    other = db.connect("t")
    racing = _Racing(conn, "SELECT rating, pick, label FROM decisions", lambda: library.decide(other, [{"id": a, "rating": 5}], what="5★"))
    res = library.undo(racing)
    racing.finish()
    assert _stars(conn, a) == 5                          # changed again meanwhile: yours wins, as for any later change
    assert res["label"] == "3★"


def test_two_keyword_changes_at_the_same_moment_both_count(env):
    from joeylab import db, keywords

    _, lib = env
    conn, a, _, _ = _library(lib)
    k0, k1 = keywords.ensure(conn, "Family"), keywords.ensure(conn, "Trips")
    keywords.assign(conn, [a], add=[k0])
    other = db.connect("t")
    racing = _Racing(conn, "SELECT keyword_id FROM shot_keywords", lambda: keywords.assign(other, [a], remove=[k0]))
    keywords.assign(racing, [a], add=[k1])
    racing.finish()
    assert keywords._ids_of(conn, a) == [k1]             # one added it, the other took Family off
    ok, rows = _chain(conn, a, "keywords")
    assert ok and len(rows) == 3, rows


def test_two_time_shifts_at_the_same_moment_add_up(env):
    from joeylab import dating, db

    _, lib = env
    conn, _, b, _ = _library(lib)
    eff = conn.execute("SELECT eff_ts FROM shots WHERE id=?", (b,)).fetchone()[0]
    other = db.connect("t")
    racing = _Racing(conn, "SELECT shot_id, offset_s FROM shot_offsets", lambda: dating.shift_times(other, [b], 7200))
    dating.shift_times(racing, [b], 3600)
    racing.finish()
    assert dating.shifts(conn, [b]) == {b: 3 * 3600.0}
    assert conn.execute("SELECT eff_ts FROM shots WHERE id=?", (b,)).fetchone()[0] == eff + 3 * 3600
    ok, rows = _chain(conn, b, "shift")
    assert ok and len(rows) == 2, rows


def test_two_dates_given_at_the_same_moment_are_logged_one_after_the_other(env):
    from joeylab import dating, db, library

    _, lib = env
    conn, _, _, scan = _library(lib)
    other = db.connect("t")
    racing = _Racing(conn, "SELECT shot_id, capture_str FROM manual_dates", lambda: dating.set_dates(other, [scan], "1999"))
    dating.set_dates(racing, [scan], "1998")
    racing.finish()
    ok, rows = _chain(conn, scan, "date")
    assert ok and len(rows) == 2, rows
    library.undo(conn)
    library.undo(conn)
    assert dating.manual(conn, [scan]) == {}             # both undone: undated again


# ---------------------------------------------------------------- the decisions export

def test_the_decisions_export_leaves_out_the_catalogs_own_settings(env):
    """How far face finding got, the ids a library gave away by moves and the like describe that catalog,
    not your work: carried into another they'd make it skip faces or misnumber its own."""
    from joeylab import db, portable, scanner

    _, lib = env
    conn, a, _, _ = _library(lib)
    internal = {"moved_out": {"shots": [[1, 3]]}, "hwm_clusters": 40, "faces_seen_upto": 900, "faces_full_at": 1.0e9,
                "faces_model": "opencv", "trip_rule": "abc", "event_joins": [{"kind": "event", "a": 1, "b": 2}],
                "root_drives": {"1": {"name": "Photos SSD"}}, "explore_content": {"ids": [a]}, "shotlist_tags": {"1": [a]}}
    yours = {"folder_names": {str(lib): "Holiday"}, "keep_under": [str(lib)], "faces_enabled": False, "grouping": 2}
    for k, v in {**internal, **yours}.items():
        db.set_setting(conn, k, v)
    data = json.loads(json.dumps(portable.export_decisions(conn, "t")))
    assert data["settings"] == yours
    # An export made before they were left out: they're not taken in either.
    data["settings"].update(internal)
    fresh = db.connect("fresh")
    scanner.add_root(fresh, lib)
    scanner.scan(fresh)
    portable.import_decisions(fresh, data)
    assert {r[0] for r in fresh.execute("SELECT key FROM settings")} & set(internal) == set()
    assert {k: db.get_setting(fresh, k) for k in yours} == yours
