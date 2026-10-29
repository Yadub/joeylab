"""Turn raw signals into a 0-100 quality score, flags, and suggestions.

Suggestions are pick, reject, or check ("worth checking": something may be off, such as slightly
soft focus, a blink with no better frame, or exposure, but not enough to call it a reject). They
are only hints; accepting suggestions turns picks and rejects into decisions and never touches a
"check".
"""

from __future__ import annotations

import json
import math
from collections import defaultdict

import numpy as np

from .db import to_u64
from .dupes import backup_copy_shot_ids, duplicate_shot_ids
from .hashing import hamming

NEAR_IDENTICAL_HAM = 16
NEAR_IDENTICAL_COS = 0.95
SOFTER_THAN_TWIN = 0.6        # under this share of a near-identical frame's sharpness: suggest reject
SLIGHTLY_SOFTER = 0.8         # between the two: worth checking
VERY_DARK = 45                # average brightness (0-255) under this is underexposed, clipped shadows or not
# An exposure outlier in a burst: this many stops darker or brighter than its near-identical frames,
# and worse exposed than they are. It ranks below them, and isn't called soft for being dark (a dark
# frame measures less focus).
OUTLIER_STOPS = 0.75
# Soft warnings: a photo that would otherwise get no suggestion is "worth checking".
WORTH_CHECKING = ("soft", "eyes-closed", "overexposed", "underexposed", "slightly-softer", "damaged",
                  "darker-than-burst", "brighter-than-burst")
EXPOSURE_FLAGS = {"overexposed", "underexposed", "darker-than-burst", "brighter-than-burst"}


def _ramp(x: float | None, lo: float, hi: float) -> float | None:
    if x is None or x <= 0:
        return None if x is None else 0.0
    return float(min(1.0, max(0.0, (math.log10(x) - lo) / (hi - lo))))


# How well the faces are caught (Apple Vision's face capture quality, faces.measure_quality): most faces
# score between these (a real library's: 10% below 0.15, 10% above 0.51). It's this share of the faces
# part, so a turned head or a passing frown never outweighs closed eyes, nor decides on its own.
CAUGHT_LOW, CAUGHT_HIGH = 0.15, 0.55
CAUGHT_SHARE = 0.25
# Apple Vision's aesthetics score (vision.score, -1 to 1, stored with the search index): from a snapshot to
# a strong image (a real library's: 10% below 0.32, 10% above 0.67). It agrees only loosely with the LAION
# predictor (r = 0.34 there), so it's a modest share of the aesthetic part, and only beside it.
APPLE_LOW, APPLE_HIGH = 0.25, 0.85
APPLE_SHARE = 0.25


def caught(q: float | None) -> float | None:
    """A face capture quality as 0-1 over the range most faces fall in; None when not judged."""
    if q is None or q < 0:
        return None
    return min(1.0, max(0.0, (q - CAUGHT_LOW) / (CAUGHT_HIGH - CAUGHT_LOW)))


def _part(m, key: str):
    """A column the row may not have (rows read before it existed, or without it)."""
    try:
        return m[key]
    except (IndexError, KeyError):
        return None


def components(m) -> dict:
    subj = _ramp(m["sharp_subject"], 1.8, 3.7)          # ~63 .. ~5000 Laplacian variance
    face = _ramp(m["face_sharp"], 1.3, 2.3) if m["main_faces"] else None   # ~20 soft .. ~200 crisp
    if face is not None and (m["face_area"] or 0) >= 0.01:
        sharp = 0.65 * face + 0.35 * subj
    else:
        sharp = subj

    b = m["brightness"]
    exposure = 1.0
    exposure -= min(0.6, max(0.0, m["clip_high"] - 0.03) * 5)
    exposure -= min(0.4, max(0.0, m["clip_low"] - 0.10) * 2)
    if b < 70:
        exposure -= (70 - b) / 90
    if b > 195:
        exposure -= (b - 195) / 120
    exposure = max(0.0, exposure)

    faces = None
    if m["main_faces"]:
        faces = 1.0 - 0.8 * (m["eyes_closed"] / m["main_faces"]) + 0.15 * (m["smile"] or 0)
        faces = min(1.0, max(0.0, faces))
        how = caught(_part(m, "face_quality"))
        if how is not None:
            faces = (1 - CAUGHT_SHARE) * faces + CAUGHT_SHARE * how

    aesthetic = None
    if m["aesthetic"] is not None:
        # LAION predictor: ~4.6 snapshot .. ~6.2 strong image on real event photos
        aesthetic = min(1.0, max(0.0, (m["aesthetic"] - 4.6) / 1.6))
        apple = _part(m, "vision_score")
        if apple is not None:
            apple = min(1.0, max(0.0, (apple - APPLE_LOW) / (APPLE_HIGH - APPLE_LOW)))
            aesthetic = (1 - APPLE_SHARE) * aesthetic + APPLE_SHARE * apple
    return {"sharp": sharp, "exposure": exposure, "faces": faces, "aesthetic": aesthetic}


WEIGHTS = {"sharp": 0.40, "exposure": 0.20, "faces": 0.20, "aesthetic": 0.25}


def quality(c: dict) -> float:
    num = den = 0.0
    for k, w in WEIGHTS.items():
        if c[k] is not None:
            num += w * c[k]
            den += w
    return 100.0 * num / den if den else 0.0


def flags_for(m) -> list[str]:
    f = []
    subj, face = m["sharp_subject"], m["face_sharp"]
    has_face = bool(m["main_faces"]) and (m["face_area"] or 0) >= 0.01
    # With a real face in frame, focus is judged on the face: shallow-focus portraits have soft
    # surroundings on purpose.
    if has_face:
        if (face or 0) < 20 and subj < 150:
            f.append("blurry")
        elif (face or 0) < 28:
            f.append("soft")
    elif subj < 60:
        f.append("blurry")
    elif subj < 150:
        f.append("soft")
    if m["clip_high"] > 0.12:
        f.append("overexposed")
    # Dark on average (a frame a few stops under keeps its shadows above black), or dark with its
    # shadows crushed.
    if m["brightness"] < VERY_DARK or (m["brightness"] < 60 and m["clip_low"] > 0.15):
        f.append("underexposed")
    if m["eyes_closed"]:
        f.append("eyes-closed")
    return f


TASTE_WEIGHT = 0.4


def explain(conn, shot_id: int) -> list[dict]:
    """Why JoeyLab suggests what it does for one photo, in plain words. Each item is
    {"kind": good|bad|info, "text": ...}."""
    from . import taste

    vs = _vision_score(conn)
    m = conn.execute(
        f"SELECT m.*, s.group_id, s.width, s.height, COALESCE(s.media, 'photo') AS media, g.size AS group_size, {vs} AS vision_score"
        " FROM metrics m JOIN shots s ON s.id = m.shot_id LEFT JOIN groups g ON g.id = s.group_id WHERE m.shot_id = ?",
        (shot_id,)).fetchone()
    if not m:
        return [{"kind": "info", "text": "Not analysed yet."}]
    flags = json.loads(m["flags"] or "[]")
    out: list[dict] = []
    c = components(m)
    trusted = bool(taste.meta().get("trusted"))
    mates = conn.execute(
        f"SELECT m.*, s.id, s.stem, s.eff_ts, {vs} AS vision_score FROM metrics m JOIN shots s ON s.id = m.shot_id WHERE s.group_id = ?"
        " AND s.status = 'present' ORDER BY COALESCE(m.group_rank, 999), s.id", (m["group_id"],)).fetchall() \
        if m["group_id"] is not None else []
    if len(mates) > 1:
        out.append(_rank_text(m, mates, trusted))
    elif m["suggestion"] == "pick":
        out.append({"kind": "good", "text": f"A strong single shot: quality {round(m['quality'] or 0)}, "
                                            "in the top 40% of this library."})
    if m["suggestion"] == "check":
        out.append({"kind": "info", "text": "Worth a second look: something below may be off, but not enough to "
                                            "suggest rejecting it. It may be intentional, or fixable."})
    if "blurry" in flags or "soft" in flags:
        if m["main_faces"] and (m["face_area"] or 0) >= 0.01:
            out.append({"kind": "bad", "text": f"Faces are {'blurry' if 'blurry' in flags else 'a little soft'} "
                        f"(face focus {round(m['face_sharp'] or 0)}; crisp faces score 100+)."})
        else:
            out.append({"kind": "bad", "text": f"The subject is {'blurry' if 'blurry' in flags else 'a little soft'} "
                        f"(focus {round(m['sharp_subject'] or 0)}; sharp photos score 300+)."})
    if "softer-than-twin" in flags and m["group_id"] is not None:
        best = conn.execute("SELECT s.stem, m2.sharp_subject FROM shots s JOIN metrics m2 ON m2.shot_id = s.id"
                            " WHERE s.group_id = ? AND s.id != ? ORDER BY m2.sharp_subject DESC LIMIT 1",
                            (m["group_id"], shot_id)).fetchone()
        if best:
            ratio = (best[1] or 1) / max(1.0, m["sharp_subject"] or 1)
            out.append({"kind": "bad", "text": f"{ratio:.1f}× softer than {best[0]}, a near-identical frame." if ratio < 10
                        else f"{ratio:.0f}× softer than {best[0]}, a near-identical frame."})
    if "slightly-softer" in flags and m["group_id"] is not None:
        out.append({"kind": "bad", "text": "A little softer than a near-identical frame in this burst."})
    if "eyes-closed" in flags:
        n, k = m["main_faces"] or 0, m["eyes_closed"] or 0
        out.append({"kind": "bad", "text": f"Eyes closed ({k} of {n} face{'s' if n != 1 else ''})"
                    + (", and another frame in this burst is better." if (m["group_rank"] or 1) > 1 else ".")})
    if "duplicate" in flags:
        out.append({"kind": "bad", "text": "A copy of another file in your library (the better copy is kept)."})
    if "backup-copy" in flags:
        out.append({"kind": "info", "text": "An identical copy of a photo shown from another drive: a backup, not a "
                    "duplicate. It's left out of culling, stacks and events."})
    if "overexposed" in flags:
        out.append({"kind": "bad", "text": f"{(m['clip_high'] or 0) * 100:.0f}% of the frame is blown-out white."})
    if "underexposed" in flags:
        out.append({"kind": "bad", "text": "Very dark, with deep shadows clipped to black." if (m["clip_low"] or 0) > 0.15
                    else f"Very dark: its average brightness is {round(m['brightness'] or 0)} of 255."})
    for flag, word in (("darker-than-burst", "darker"), ("brighter-than-burst", "brighter")):
        others = [x["brightness"] or 0 for x in mates if x["shot_id"] != shot_id]
        if flag in flags and others:
            off = abs(math.log2(max(m["brightness"] or 0, 1.0) / max(float(np.median(others)), 1.0)))
            out.append({"kind": "bad", "text": f"{off:.1f} stops {word} than the other frames in this burst."})
    if "tiny" in flags:
        out.append({"kind": "info", "text": f"Only {m['width']}×{m['height']} pixels: a thumbnail or preview, "
                                            "not a full photo."})
    if "damaged" in flags:
        out.append({"kind": "bad", "text": "The file is damaged (often from a recovered memory card): this is the part "
                                           "that could still be read. Check whether an intact copy exists elsewhere."})
    if "rescuable" in flags:
        out.append({"kind": "info", "text": "Exposure is off, but there's a RAW file: it can likely be fixed in editing."})
    if m["media"] == "video":
        out.append({"kind": "info", "text": "A video: JoeyLab doesn't judge videos by their poster frame."})
    if trusted and m["taste"] is not None:
        out.append({"kind": "good" if m["taste"] >= 0.5 else "bad",
                    "text": f"Your taste: {round(m['taste'] * 100)}% — you tend to {'keep' if m['taste'] >= 0.5 else 'skip'} photos like this."})
    scored = [(k, v) for k, v in c.items() if v is not None]
    # A badly exposed frame's exposure is never its strong point, whatever its score says.
    strong = [(k, v) for k, v in scored if not (k == "exposure" and EXPOSURE_FLAGS & set(flags))]
    if scored:
        best_k = max(strong or scored, key=lambda x: x[1])
        worst_k = min(scored, key=lambda x: x[1])
        if len({round(v * 100) for _, v in scored}) == 1 and len(scored) > 1:
            names = [PARTS[k] for k, _ in scored]
            both = "both" if len(names) == 2 else "all"
            text = f"{(', '.join(names[:-1]) + ' and ' + names[-1]).capitalize()} {both} score {round(best_k[1] * 100)}."
        elif best_k[0] == worst_k[0]:
            text = f"Weakest: {PARTS[worst_k[0]]} ({round(worst_k[1] * 100)})."
        else:
            text = f"Strongest: {PARTS[best_k[0]]} ({round(best_k[1] * 100)}). Weakest: {PARTS[worst_k[0]]} ({round(worst_k[1] * 100)})."
        out.append({"kind": "info", "text": text})
    return out


def _vision_score(conn) -> str:
    """SQL for a photo's Apple Vision aesthetics score (s: its row in shots), if the catalog has them."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='vision'").fetchone():
        return "(SELECT score FROM vision v WHERE v.shot_id = s.id)"
    return "NULL"


PARTS = {"sharp": "focus", "exposure": "exposure", "faces": "faces and eyes", "aesthetic": "composition and light",
         "taste": "your taste"}


def ordinal(k: int) -> str:
    return f"{k}{'th' if 10 <= k % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(k % 10, 'th')}"


def points(m, trusted: bool) -> dict:
    """What each part adds to a photo's quality score (together they make it up)."""
    c = components(m)
    den = sum(w for k, w in WEIGHTS.items() if c[k] is not None)
    if not den:
        return {}
    share = 1 - TASTE_WEIGHT if trusted and m["taste"] is not None else 1.0
    out = {k: 100 * share * w * c[k] / den for k, w in WEIGHTS.items() if c[k] is not None}
    if share < 1:
        out["taste"] = 100 * TASTE_WEIGHT * m["taste"]
    return out


def _rank_text(m, mates: list, trusted: bool) -> dict:
    """Where a frame stands in its stack, and why: "3rd of 5: 4 points behind IMG_0005, mostly on
    focus". `mates` are the stack's frames, best first."""
    n = len(mates)
    rank = next((i for i, x in enumerate(mates, 1) if x["shot_id"] == m["shot_id"]), m["group_rank"] or 1)
    me = mates[rank - 1]
    other = mates[1] if rank == 1 else mates[0]      # the runner-up, or the best frame
    q, oq = me["quality"] or 0, other["quality"] or 0
    lead = q - oq if rank == 1 else oq - q
    a, b = (points(me, trusted), points(other, trusted)) if rank == 1 else (points(other, trusted), points(me, trusted))
    diffs = {k: a[k] - b[k] for k in a if k in b}
    part = max(diffs, key=diffs.get) if diffs and max(diffs.values()) > 0.5 else None
    on = f", mostly on {PARTS[part]}" if part else ""
    outliers = {"darker-than-burst", "brighter-than-burst"}
    off = rank > 1 and outliers & set(json.loads(me["flags"] or "[]"))
    if off and lead > 0:
        # A dark frame also measures less focus: exposure is the reason it ranks where it does.
        return {"kind": "info", "text": f"{ordinal(rank)} of {n}: ranked below the frames exposed like the rest of the "
                                        f"burst ({round(lead)} points behind {other['stem']})."}
    if (rank == 1 and lead <= -0.05) or (off and lead <= 0):
        if rank == 1:
            return {"kind": "good", "text": f"Best of {n}: {other['stem']} scores higher, but it's exposed unlike the "
                                            "rest of the burst."}
        return {"kind": "info", "text": f"{ordinal(rank)} of {n}: it scores as well as {other['stem']}, but it's "
                                        "exposed unlike the rest of the burst."}
    if abs(lead) < 0.05:
        first, second = (me, other) if rank == 1 else (other, me)
        why = "as the sharper frame" if (first["sharp_subject"] or 0) != (second["sharp_subject"] or 0) else "as the earlier frame"
        return {"kind": "good" if rank == 1 else "info",
                "text": f"{'Best' if rank == 1 else ordinal(rank)} of {n}: level with {other['stem']} on quality ({round(q)}); "
                        f"{first['stem']} ranks first {why}."}
    gap = "less than a point" if lead < 0.5 else f"{round(lead)} point{'s' if round(lead) != 1 else ''}"
    if rank == 1:
        return {"kind": "good", "text": f"Best of {n}: {gap} ahead of {other['stem']}{on}."}
    return {"kind": "info", "text": f"{ordinal(rank)} of {n}: {gap} behind {other['stem']}{on}."}


def run(conn) -> dict:
    from . import taste
    from .ai import load_embeddings

    taste.score(conn)
    trusted = taste.meta().get("trusted", False)

    from .config import TINY_SIDE

    has_vision = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='vision'").fetchone())
    # (Apple Vision's utility flag and aesthetics score, read from one look-up of each photo's row)
    vision, join = (("v.utility AS utility, v.score AS vision_score", " LEFT JOIN vision v ON v.shot_id = s.id") if has_vision
                    else ("NULL AS utility, NULL AS vision_score", ""))
    rows = conn.execute(
        "SELECT m.*, s.group_id, s.phash, s.width, s.height, s.eff_ts, COALESCE(s.media, 'photo') AS media, s.error AS shot_error,"
        " (SELECT MAX(kind='raw') FROM files f WHERE f.shot_id = s.id AND f.status='present') AS has_raw,"
        f" {vision} FROM metrics m JOIN shots s ON s.id = m.shot_id{join}"
        " WHERE s.status='present'"
    ).fetchall()
    embs = load_embeddings(conn)
    phashes = {m["shot_id"]: to_u64(m["phash"]) for m in rows if m["phash"] is not None}

    def near_identical(a: int, b: int) -> bool:
        if a in embs and b in embs:
            return float(embs[a] @ embs[b]) >= NEAR_IDENTICAL_COS
        return a in phashes and b in phashes and hamming(phashes[a], phashes[b]) <= NEAR_IDENTICAL_HAM

    if not rows:
        return {"scored": 0}
    dups = duplicate_shot_ids(conn)
    backed = backup_copy_shot_ids(conn)       # flagged only: a backup is never suggested for rejection for being one
    scored = {}
    for m in rows:
        c = components(m)
        q = quality(c)
        if trusted and m["taste"] is not None:
            q = (1 - TASTE_WEIGHT) * q + TASTE_WEIGHT * 100 * m["taste"]
        # A video is judged by people, not by its poster frame's focus or exposure.
        flags = flags_for(m) if m["media"] != "video" else []
        if m["width"] and m["height"] and max(m["width"], m["height"]) < TINY_SIDE and m["media"] != "video":
            flags.append("tiny")
        if m["utility"]:
            flags.append("utility")      # Apple Vision: a screenshot, document, receipt or similar
        if m["has_raw"] and {"overexposed", "underexposed"} & set(flags) and "blurry" not in flags:
            flags.append("rescuable")
        if (m["shot_error"] or "").startswith("Damaged file"):
            flags.append("damaged")      # shown from what could be read; may have an intact copy elsewhere
        scored[m["shot_id"]] = {"q": q, "flags": flags, "sharp": c["sharp"] or 0, "group": m["group_id"],
                                "media": m["media"], "b": m["brightness"] or 0.0, "expo": c["exposure"],
                                "tie": (-(m["sharp_subject"] or 0), m["eff_ts"] or 0, m["shot_id"])}
        if m["shot_id"] in dups:
            scored[m["shot_id"]]["flags"].append("duplicate")
        elif m["shot_id"] in backed:
            scored[m["shot_id"]]["flags"].append("backup-copy")

    pick_bar = float(np.percentile([s["q"] for s in scored.values()], 60))
    by_group: dict = defaultdict(list)
    for sid, s in scored.items():
        # Undated photos aren't grouped: each stands alone rather than competing with every other
        # undated photo for "best in group".
        by_group[s["group"] if s["group"] is not None else ("alone", sid)].append(sid)

    counts = defaultdict(int)
    was = {r[0]: (r[1], r[2], r[3], r[4]) for r in conn.execute("SELECT shot_id, quality, flags, suggestion, group_rank FROM metrics")}
    best_was = {r[0]: r[1] for r in conn.execute("SELECT id, best_shot_id FROM groups")}
    updates: list[tuple] = []
    best_updates: list[tuple] = []
    def stops(a: float, b: float) -> float:
        return math.log2(max(a, 1.0) / max(b, 1.0))

    def outlier(sid) -> bool:
        return bool({"darker-than-burst", "brighter-than-burst"} & set(scored[sid]["flags"]))

    for gid, members in by_group.items():
        twins = {sid: [o for o in members if o != sid and near_identical(sid, o)] for sid in members} if len(members) > 1 else {}
        for sid, tw in twins.items():
            s = scored[sid]
            if not tw or s["media"] == "video":
                continue
            off = stops(s["b"], float(np.median([scored[o]["b"] for o in tw])))
            if abs(off) >= OUTLIER_STOPS and s["expo"] < float(np.median([scored[o]["expo"] for o in tw])) - 0.05:
                s["flags"].append("darker-than-burst" if off < 0 else "brighter-than-burst")
        # Best first, an exposure outlier after its burst-mates; a tie goes to the sharper frame, then
        # the earlier one.
        members.sort(key=lambda sid: (outlier(sid), -round(scored[sid]["q"], 1), scored[sid]["tie"]))
        for rank, sid in enumerate(members, 1):
            s = scored[sid]
            if len(members) > 1:
                if rank == 1:
                    s["flags"].append("best-in-group")
                # Only compare focus against near-identical frames (bursts) exposed alike: a tighter or
                # differently framed shot of the same scene isn't "softer", and a darker frame measures
                # less focus.
                alike = [o for o in twins[sid] if abs(stops(s["b"], scored[o]["b"])) < OUTLIER_STOPS]
                best_twin = max((scored[o]["sharp"] for o in alike), default=0.0)
                if alike and s["sharp"] < SOFTER_THAN_TWIN * best_twin:
                    s["flags"].append("softer-than-twin")
                elif alike and s["sharp"] < SLIGHTLY_SOFTER * best_twin:
                    s["flags"].append("slightly-softer")
            strong_reject = {"duplicate", "blurry"} & set(s["flags"])
            # Badly exposed next to near-identical frames that aren't (and no RAW to fix it from).
            badly_exposed = outlier(sid) and {"underexposed", "overexposed"} & set(s["flags"]) and "rescuable" not in s["flags"]
            if s["media"] == "video" or {"tiny", "utility"} & set(s["flags"]):
                sug = "reject" if "duplicate" in s["flags"] else None
            elif strong_reject or ("eyes-closed" in s["flags"] and rank > 1) or "softer-than-twin" in s["flags"] or badly_exposed:
                sug = "reject"
            elif rank == 1 and (len(members) > 1 or s["q"] >= pick_bar) and not {"eyes-closed", "damaged"} & set(s["flags"]):
                sug = "pick"
            elif set(WORTH_CHECKING) & set(s["flags"]):
                sug = "check"
            else:
                sug = None
            counts[sug or "none"] += 1
            row = (round(s["q"], 1), json.dumps(s["flags"]), sug, rank)
            if was.get(sid) != row:                  # only what changed is written
                updates.append((*row, sid))
        if isinstance(gid, int) and best_was.get(gid) != members[0]:
            best_updates.append((members[0], gid))
    conn.executemany("UPDATE metrics SET quality=?, flags=?, suggestion=?, group_rank=? WHERE shot_id=?", updates)
    conn.executemany("UPDATE groups SET best_shot_id=? WHERE id=?", best_updates)
    conn.commit()
    return {"scored": len(scored), "suggest_pick": counts["pick"], "suggest_reject": counts["reject"],
            "suggest_check": counts["check"]}
