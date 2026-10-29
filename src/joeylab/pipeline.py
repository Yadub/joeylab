"""One update of a library, shared by the CLI and the web server: scan → analyse → stacks,
duplicates and scores → faces → picture vectors → search and pets → places, events and refined
scores → the untouched check.

Progress goes through a Reporter so each front end can show it its own way.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import analysis, config, dupes, grouping, scanner, scoring


class Reporter:
    """Receives progress. Subclass and override what you need."""

    def step(self, key: str) -> None:
        """The update moves on to a step (STEPS), so a page waiting for one can say how far it is."""
        pass

    def stage(self, name: str, total: int = 0) -> None:
        pass

    def advance(self, done: int, total: int | None = None) -> None:
        pass

    def log(self, msg: str, level: str = "info") -> None:  # level: info | ok | warn | error
        pass

    def finished(self, step: str) -> None:
        """A step of the update is done ("analyse": every photo has its thumbnail and measurements;
        "group": stacks, duplicates and suggestions are in)."""
        pass


# An update's steps, in order. The useful ones come first: once "group" is done, Cull, Duplicates
# and the suggestions work, while faces, picture vectors, search and places carry on after.
STEPS = ("scan", "analyse", "group", "faces", "ai", "search", "places", "check")


def _n(k: int, word: str, many: str | None = None) -> str:
    return f"{k:,} {word if k == 1 else many or word + 's'}"


def ensure_face_model(rep: Reporter) -> bool:
    from importlib.util import find_spec

    if config.FACE_MODEL.exists():
        return True
    if config.offline() or find_spec("mediapipe") is None:
        return False
    from . import downloads

    rep.log("Downloading face model (3.7 MB, one time)")
    try:
        downloads.fetch(config.FACE_MODEL_URL, config.FACE_MODEL_SHA256, config.FACE_MODEL)
        return True
    except OSError as e:
        rep.log(f"Face model download failed ({e}); continuing without face checks", "warn")
        return False


def add_roots(conn, paths: list[Path], rep: Reporter, project: str | None = None) -> None:
    """Raises ValueError (with the reason, in plain words) for a folder that can't be used, such as
    one another library includes. One with no photos in it yet is added, with a note."""
    from . import libraries

    for p in paths:
        check = libraries.check_folder(str(p), library=project)
        if not check["ok"] and not check["empty"]:
            raise ValueError(check["reason"])
        if check["empty"]:
            rep.log(f"{check['reason']} Added anyway: photos copied into it later are found by the next update.", "warn")
        _, note = scanner.add_root(conn, Path(p))
        if note:
            rep.log(f"{p}: {note}")


def scan(conn, rep: Reporter) -> scanner.ScanReport:
    from . import exif

    if not exif.available():
        rep.log(exif.MISSING, "warn")
    rep.step("scan")
    rep.stage("Looking for new and changed photos")
    r = scanner.scan(conn, progress=lambda msg, i, n: rep.advance(i, n or None))
    parts = [_n(r.seen, "file"), _n(r.shots_present, "photo")]
    for k, word in (("added", "new"), ("changed", "changed"), ("moved", "moved"), ("returned", "back again"),
                    ("missing", "missing")):
        v = getattr(r, k)
        if v:
            parts.append(f"{v:,} {word}")
    rep.log("Folders: " + " · ".join(parts), "ok")
    for mv in r.roots_moved:
        rep.log(f"{'Folder' if mv.get('kind') == 'folder' else 'Drive'} renamed: {mv['from']} is now {mv['to']}; "
                f"{_n(mv['paths'], 'path')} updated in the catalog"
                + ("" if mv.get("kind") == "folder" else " (nothing on the drive changed)")
                + (f". {_n(mv['stale_album_links'], 'album link')} still point at the old name;"
                " export those albums again to refresh them" if mv["stale_album_links"] else ""), "ok")
    for root in r.roots_wrong_drive:
        rep.log(f"A different drive is mounted under the name {root.split('/')[2]!r}; {root} is held back until "
                "the original drive is plugged in", "warn")
    for root in r.roots_unavailable:
        if root not in r.roots_wrong_drive:
            rep.log(f"Not available (skipped, nothing marked missing): {root}", "warn")
    if r.settling:
        rep.log(f"{_n(len(r.settling), 'file')} still being written; the next update picks them up", "warn")
    for e in r.errors[:5]:
        rep.log(e, "error")
    return r


def scan_changed(r: scanner.ScanReport) -> bool:
    return bool(r.added or r.changed or r.moved or r.returned or r.missing or r.roots_moved)


def analyze(conn, project: str, rep: Reporter, force: bool = False, faces: bool = True,
            workers: int | None = None) -> int:
    rep.step("analyse")
    use_faces = faces and ensure_face_model(rep)
    pending = len(analysis.pending_tasks(conn, project, force))
    if not pending:
        rep.log("Analyse: everything up to date", "ok")
        return 0
    rep.stage("Analysing photos", pending)
    n, errors = analysis.run(conn, project, force=force, workers=workers, use_faces=use_faces,
                             progress=lambda d, tot: rep.advance(d, tot))
    rep.log(f"Analyse: {_n(n, 'photo')}" + ("" if use_faces else " (eyes and smiles not checked)"), "ok")
    for e in errors[:5]:
        rep.log(e, "error")
    if len(errors) > 5:
        rep.log(f"…and {_n(len(errors) - 5, 'more error')}", "error")
    return n


def group(conn, rep: Reporter) -> dict:
    """Stacks, duplicates and scores, straight after analysis, from capture times, camera bodies
    and how alike frames look (plus any picture vectors from earlier updates). Cull, Duplicates and
    the suggestions work from here on; refine() later adds what faces, vectors and search know."""
    from . import selections

    rep.step("group")
    rep.stage("Stacking and finding duplicates")
    d = dupes.run(conn)          # first: stacks and events are made from kept copies only
    g = grouping.run(conn)
    sel = selections.update(conn)
    rep.stage("Scoring")
    s = scoring.run(conn)
    rep.log(f"Stacks: {_n(g['groups'], 'stack')} ({g['multi_frame_groups']:,} with more than one frame) from "
            f"{_n(g['cameras'], 'camera body', 'camera bodies')}"
            + ("" if g.get("semantic") else " (by time and look)")
            + (f"; {_n(g['undated'], 'photo')} without a capture date stay on their own" if g.get("undated") else ""), "ok")
    rep.log(f"Duplicates: {_n(d['exact_sets'], 'set')} of identical files · {_n(d['copy_sets'], 'set')} of re-saved copies"
            + (f" · {_n(d['exports'], 'edited or renamed version')}" if d.get("exports") else "")
            + (f" · {_n(d['split_pairs'], 'RAW + JPEG pair')} kept in different folders" if d.get("split_pairs") else ""), "ok")
    if sel["selections"] or sel["mirrors"]:
        rep.log(f"Selections: {_n(sel['selections'], 'folder')} picked from another folder · "
                f"{_n(sel['mirrors'], 'folder')} copied whole (saved as albums; nothing changed on disk)", "ok")
    if s.get("scored"):
        rep.log(f"Suggestions: {_n(s['suggest_pick'], 'pick')}, {_n(s['suggest_reject'], 'reject')}, "
                f"{s.get('suggest_check', 0):,} worth checking (only hints; nothing is decided for you)", "ok")
    return {"groups": g, "dupes": d, "score": s}


def index_copies(conn, project: str, rep: Reporter) -> dict | None:
    """This library's files in the index every library shares (copies.py), so each knows which of
    its photos have a backup on another drive. Catalog reads and index writes only, a second or two;
    a problem with the index is reported and never stops the update."""
    from . import copies

    try:
        r = copies.refresh(conn, project)
        copies.sync(conn, project)
    except Exception as e:                     # the index is derived: the next update tries again
        rep.log(f"Backups: the index of copies couldn't be updated ({e}); the next update tries again", "warn")
        return None
    c = copies.counts(conn)
    if c["elsewhere"] or r["files"]:
        rep.log(f"Backups: {_n(c['elsewhere'], 'photo')} with a copy on another drive"
                + (f" ({c['checked']:,} checked byte for byte)" if c["elsewhere"] else "")
                + f" · {c['only']:,} only on this drive", "ok")
    return r | {"backed_up": c["elsewhere"], "checked": c["checked"], "only": c["only"]}


def measure_colours(conn, rep: Reporter) -> int:
    """Each new photo's colours, from its thumbnail (a few milliseconds a photo), for Explore."""
    from . import colours

    todo = len(colours.pending(conn))
    if not todo:
        return 0
    rep.stage("Measuring colours", todo)
    n = colours.run(conn, progress=lambda d, tot: rep.advance(d, tot))
    rep.log(f"Colours: {_n(n, 'photo')} measured", "ok")
    return n


def run_faces(conn, rep: Reporter, enabled: bool = True, force: bool = False) -> int:
    from . import db, faces

    if not enabled or not faces.available() or not db.get_setting(conn, "faces_enabled", True):
        return 0
    if not faces.enabled():
        if not faces.chosen():
            rep.log("People: face recognition is off. To find and name people, turn it on: joeylab faces enable")
        return 0
    rep.step("faces")
    if not faces.ensure_models(lambda m: rep.log(m)):
        rep.log("Face recognition models unavailable; skipping people", "warn")
        return 0
    todo = len(faces.pending(conn, force))
    if not todo:
        return 0
    rep.stage("Finding faces", todo)
    n, errors = faces.run(conn, force=force, progress=lambda d, t: rep.advance(d, t))
    rep.log(f"Faces: looked at {_n(n, 'photo')}", "ok")
    for e in errors[:5]:
        rep.log(e, "error")
    return n


def run_face_quality(conn, rep: Reporter, enabled: bool = True) -> int:
    """How well each face is caught (Apple Vision's capture quality), judged once per face from its crop,
    for scoring's faces part and Cull's face compare. A pass of its own, so faces found before it existed
    are judged without being found again."""
    from . import faces, search

    if not enabled or not search.vision_available():
        return 0
    todo = len(faces.quality_pending(conn))
    if not todo:
        return 0
    rep.step("faces")
    rep.stage("Judging faces", todo)
    errors: list = []
    n = faces.measure_quality(conn, progress=lambda d, t: rep.advance(d, t), errors=errors)
    left = len(faces.quality_pending(conn))
    rep.log(f"Faces: judged how well {_n(n, 'face is', 'faces are')} caught" + (f"; {left:,} still to judge" if left else ""),
            "warn" if left else "ok")
    for sid, why in errors[:5]:
        rep.log(f"Photo {sid}: {why}", "error")
    return n


def run_ai(conn, rep: Reporter, enabled: bool = True) -> int:
    from . import ai

    if not enabled:
        return 0
    if not ai.available():
        rep.log("AI layer not installed (scene stacks, similar photos, how good a photo looks): uv sync --extra ai")
        return 0
    pending = len(ai.pending(conn))
    if not pending:
        return 0
    rep.step("ai")
    rep.stage("Loading the picture model")
    try:
        ai.Models.get()
    except (config.ModelMissing, OSError) as e:
        rep.log(f"AI: skipped. {e}", "warn")
        return 0
    rep.stage("Comparing photos (scenes, similar photos, looks)", pending)
    try:
        n = ai.run(conn, progress=lambda d, tot: rep.advance(d, tot))
    except ai.GpuStuck as e:
        left = len(ai.pending(conn))
        rep.log(f"AI: stopped. {e}; the other {_n(left, 'photo')} are compared in the next update, on the CPU", "warn")
        return pending - left
    rep.log(f"AI: compared {_n(n, 'photo')}", "ok")
    return n


def run_search_index(conn, rep: Reporter, enabled: bool = True) -> int:
    from . import search

    if not enabled or not search.available():
        return 0
    todo = len(search.pending(conn))
    if not todo:
        return 0
    rep.step("search")
    rep.stage("Loading the search model")
    try:
        search.Model.get()
    except (config.ModelMissing, OSError) as e:
        rep.log(f"Search: not indexed. {e}", "warn")
        return 0
    rep.stage("Indexing for search", todo)
    errors: list = []
    try:
        n = search.run(conn, progress=lambda d, t: rep.advance(d, t), errors=errors)
    except search.GpuStuck as e:
        left = len(search.pending(conn))
        rep.log(f"Search: stopped. {e}; what's indexed can be searched now, and the other "
                f"{_n(left, 'photo')} are indexed in the next update, on the CPU", "warn")
        return todo - left
    left = len(search.pending(conn))
    stuck = [sid for sid, why in errors if why.startswith(search.VISION_STUCK)]
    rep.log(f"Search: indexed {_n(n, 'photo')}" + ("" if search.vision_available() else " (Apple Vision labels unavailable)")
            + (f"; {left:,} still not indexed" if left else ""), "warn" if left else "ok")
    if stuck:
        rep.log(f"Apple Vision stopped answering; labels and text for {_n(len(stuck), 'photo')} are left for the next update",
                "warn")
    errors = [(sid, why) for sid, why in errors if not why.startswith(search.VISION_STUCK)]
    for sid, why in errors[:5]:
        rep.log(f"Photo {sid}: {why}", "error")
    if len(errors) > 5:
        rep.log(f"…and {_n(len(errors) - 5, 'more indexing error')}", "error")
    return n


def run_pets(conn, rep: Reporter, enabled: bool = True) -> int:
    """Dogs and cats (pets.py): Apple Vision on each photo's thumbnail, then the search model on each
    animal it finds, for People to group and name them. A step of its own, with its own stamp: photos
    indexed for search before are only looked at for animals. Then faces the face models found on an
    animal, where nobody is, are set aside from people's (quick: only photos with an animal)."""
    from . import pets, search

    if not enabled or not pets.available():
        return 0
    todo = len(pets.pending(conn))
    n = 0
    if todo:
        rep.step("search")
        rep.stage("Loading the search model")
        try:
            search.Model.get()
        except (config.ModelMissing, OSError) as e:
            rep.log(f"Pets: not looked for. {e}", "warn")
            todo = 0
    if todo:
        rep.stage("Looking for pets", todo)
        errors: list = []
        try:
            n = pets.run(conn, progress=lambda d, t: rep.advance(d, t), errors=errors)
        except search.GpuStuck as e:
            rep.log(f"Pets: stopped. {e}; the other photos are looked at in the next update, on the CPU", "warn")
            n = todo - len(pets.pending(conn))
        else:
            left = len(pets.pending(conn))
            rep.log(f"Pets: looked at {_n(n, 'photo')}; {_n(pets.counts(conn)['pets'], 'dog or cat', 'dogs and cats')}"
                    " in the library so far" + (f"; {left:,} photos still to look at" if left else ""), "warn" if left else "ok")
            for sid, why in errors[:5]:
                rep.log(f"Photo {sid}: {why}", "error")
    moved = pets.animal_faces(conn)
    if moved:
        rep.log(f"People: {_n(moved, 'face')} on a dog or cat, with no one there, set aside from people's faces"
                " (People › Name faces › Animals' faces)", "ok")
    return n


def refine(conn, rep: Reporter, regroup: bool = True) -> dict:
    """After faces, picture vectors and search: people, taste, places and events, then the stacks
    (when there are new picture vectors) and scores again with what those add."""
    from . import events, faces, geo, pets, taste

    rep.step("places")
    rep.stage("Finding places and events")
    out: dict = {}
    if faces.available():
        fc = faces.cluster(conn)
        if fc["faces"]:
            rep.log(f"People: {_n(fc['faces'], 'face')} in {_n(fc['clusters'], 'group')}", "ok")
    pc = pets.cluster(conn)
    if pc["groups"]:
        rep.log(f"Pets: {_n(pc['pets'], 'dog or cat', 'dogs and cats')} in {_n(pc['groups'], 'group')}", "ok")
    t = taste.train_all()
    if t.get("auc") is not None:
        rep.log(f"Taste: learned from {_n(t['positives'], 'keeper')} and {_n(t['negatives'], 'reject')} "
                f"(ranking accuracy {t['auc']:.0%}{'' if t['trusted'] else ', not trusted yet'})", "ok")
    pl = geo.assign(conn, log=lambda m: rep.log(m))
    ev = events.run(conn)
    if pl.get("available") and (pl["gps"] or pl["inferred"]):
        rep.log(f"Places: {_n(pl['gps'], 'photo')} with GPS, {pl['inferred']:,} placed by the photos around them", "ok")
    rep.log(f"Events: {_n(ev['events'], 'event')}, {_n(ev['trips'], 'trip')}", "ok")
    rep.stage("Refining stacks and scores")
    if regroup:
        g = out["groups"] = grouping.run(conn)
        rep.log(f"Stacks: {_n(g['groups'], 'stack')} ({g['multi_frame_groups']:,} with more than one frame), "
                "now with scene similarity", "ok")
    out["score"] = scoring.run(conn)
    from . import db

    db.refresh_stats(conn)               # the photo tables' statistics in full, when the photo count has moved
    conn.execute("PRAGMA optimize")      # keeps the query planner's statistics current as the library grows
    return out


def organize(conn, rep: Reporter, project: str | None = None) -> dict:
    """Every catalog-only step at once (`joeylab organize`): stacks, duplicates and scores, then
    people, taste, places and events (and, given the library's name, its backups)."""
    out = group(conn, rep)
    if project:
        index_copies(conn, project, rep)
    rep.finished("group")
    later = refine(conn, rep, regroup=False)
    rep.finished("places")
    return out | {"score": later["score"]}


def _work_left(conn, project: str, faces_on: bool, ai_on: bool) -> bool:
    """Anything still to compute, e.g. faces and AI skipped by an earlier quick run."""
    from . import ai, faces

    if analysis.pending_tasks(conn, project):
        return True
    from . import db

    if (faces_on and faces.available() and faces.enabled() and db.get_setting(conn, "faces_enabled", True)
            and faces.pending(conn)):
        return True
    if faces_on and conn.execute("SELECT 1 FROM faces f INDEXED BY faces_unjudged JOIN shots s ON s.id = f.shot_id"
                                 " WHERE f.quality IS NULL AND s.status = 'present' LIMIT 1").fetchone():
        from . import search

        if search.vision_available():
            return True
    if ai_on and ai.available() and ai.pending(conn):
        return True
    from . import pets, search

    return bool(ai_on and search.available() and (search.pending(conn) or (pets.available() and pets.pending(conn))))


def run_all(conn, project: str, rep: Reporter, *, do_scan: bool = True, only_if_changed: bool = False,
            force: bool = False, faces: bool = True, ai_layer: bool = True, workers: int | None = None,
            lock_wait: float = 0.0) -> dict:
    """Full pipeline, one at a time per library (across the CLI and the service). With
    only_if_changed, stops after the scan when nothing on disk changed and nothing is waiting
    for analysis. Ends by checking every tracked file is exactly as the scan found it."""
    from . import locks

    with locks.project_lock(project, wait=lock_wait):
        return _run_all(conn, project, rep, do_scan=do_scan, only_if_changed=only_if_changed, force=force,
                        faces=faces, ai_layer=ai_layer, workers=workers)


def _run_all(conn, project, rep, *, do_scan, only_if_changed, force, faces, ai_layer, workers) -> dict:
    from . import moves

    summary: dict = {}
    before = None
    for r in moves.recover(conn, project):         # a move or rename cut short (a crash): finished from its log first
        rep.log(f"Finished a move that stopped half way: {r['label']} ({r['finished']} done, "
                f"{r['removed']} unfinished copies removed)", "warn")
    if do_scan:
        r = scan(conn, rep)
        summary["scan"] = r.as_dict()
        before = r.stats
        if only_if_changed and not scan_changed(r) and not _work_left(conn, project, faces, ai_layer):
            summary["skipped"] = True
            return summary
    summary["analysed"] = analyze(conn, project, rep, force=force, faces=faces, workers=workers)
    rep.finished("analyse")
    # What you came for first: stacks, duplicates and suggestions. The slow, optional steps after.
    summary.update(group(conn, rep))
    summary["copies"] = index_copies(conn, project, rep)
    rep.finished("group")
    summary["colours"] = measure_colours(conn, rep)
    summary["faces"] = run_faces(conn, rep, faces, force=force)
    summary["face_quality"] = run_face_quality(conn, rep, faces)
    rep.finished("faces")
    summary["embedded"] = run_ai(conn, rep, ai_layer)
    rep.finished("ai")
    summary["search_indexed"] = run_search_index(conn, rep, ai_layer)
    summary["pets"] = run_pets(conn, rep, ai_layer)
    rep.finished("search")
    later = refine(conn, rep, regroup=bool(summary["embedded"]))
    summary["groups"] = later.get("groups", summary["groups"])
    summary["score"] = later["score"]
    rep.finished("places")
    if before:
        summary["untouched"] = check_untouched(conn, before, rep)
    return summary


def check_untouched(conn, before: dict, rep: Reporter) -> dict:
    from . import integrity

    rep.step("check")
    rep.stage("Checking your folders are untouched")
    res = integrity.record(conn, integrity.compare(before))
    folders = {os.path.dirname(p) for p in before}
    files = sum(p not in folders for p in before)          # (the rest are the folders they're in)
    if res["ok"]:
        rep.log(f"Untouched: all {res['checked']:,} files and folders are exactly as before the update", "ok")
    else:
        n = len(res["changed"]) + len(res["gone"])
        rep.log(f"{_n(n, 'file or folder', 'files or folders')} changed during the update. JoeyLab doesn't write "
                "there, so another app (or a copy in progress) did; see Tidy › Health for the list", "warn")
    return {"ok": res["ok"], "checked": res["checked"], "files": files, "changed": len(res["changed"]), "gone": len(res["gone"])}
