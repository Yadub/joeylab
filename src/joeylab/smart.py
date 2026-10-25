"""Choosing sets of photos, all from what's already in the catalog (no network):

* personal concepts: a few examples of "my blue bike" become a saved search;
* a balanced delivery set: N photos spread over the day, the people and the scenes;
* "best of" a year or a trip, one or a few per week or month;
* the photos the taste model is least sure about, to learn faster;
* shot-list coverage: which must-have moments a pick already covers.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import numpy as np

from . import db, embeddings, library, taste
from .library import Filter

NOT_JUNK = "tiny,duplicate,utility"


# ---------------------------------------------------------------- personal concepts

def _fit_weighted(X: np.ndarray, y: np.ndarray, w: np.ndarray, lam: float = 1.0, iters: int = 30) -> np.ndarray:
    """L2-regularised logistic regression with per-example weights (Newton's method)."""
    n, d = X.shape
    Xb = np.hstack([X, np.ones((n, 1))])
    beta = np.zeros(d + 1)
    reg = np.full(d + 1, lam)
    reg[-1] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Xb @ beta, -30, 30)))
        g = Xb.T @ (w * (p - y)) + reg * beta
        H = (Xb * (w * p * (1 - p))[:, None]).T @ Xb + np.diag(reg + 1e-8)
        step = np.linalg.solve(H, g)
        beta -= step
        if np.abs(step).max() < 1e-6:
            break
    return beta


def train_concept(conn, name: str, positives: list[int], negatives: list[int] | None = None) -> int:
    m = embeddings.matrix(conn)
    if m is None:
        raise ValueError("Concepts need the AI index; it builds during updates.")
    ids, mat, model = m
    index = {int(i): k for k, i in enumerate(ids)}
    pos = [index[i] for i in positives if i in index]
    if len(pos) < 3:
        raise ValueError("Give at least 3 example photos (they need to be indexed).")
    rng = np.random.default_rng(len(pos))
    neg_set = {index[i] for i in negatives or [] if i in index}
    pool = np.setdiff1d(np.arange(len(ids)), np.array(pos + list(neg_set)))
    sample = rng.choice(pool, size=min(len(pool), max(400, 40 * len(pos))), replace=False) if len(pool) else np.array([], int)
    neg = list(neg_set) + list(sample)
    X = np.vstack([mat[pos], mat[neg]]).astype(np.float64)
    y = np.concatenate([np.ones(len(pos)), np.zeros(len(neg))])
    w = np.full(len(y), 1.0)
    w[: len(pos)] = len(neg) / max(1, len(pos))      # balance the classes
    coef = _fit_weighted(X, y, w, lam=1.0)
    s = taste._predict(mat.astype(np.float32), coef)
    thr = float(np.percentile(s[pos], 20))
    cid = conn.execute("INSERT INTO concepts(name, positives, negatives, w, b, threshold, model, created) VALUES (?,?,?,?,?,?,?,?)",
                       (name.strip()[:80], json.dumps(positives), json.dumps(negatives or []), coef[:-1].astype(np.float32).tobytes(),
                        float(coef[-1]), thr, model, time.time())).lastrowid
    conn.commit()
    return cid


def concept_matches(conn, cid: int, limit: int = 300) -> list[dict]:
    c = conn.execute("SELECT * FROM concepts WHERE id=?", (cid,)).fetchone()
    if not c:
        return []
    m = embeddings.matrix(conn)
    if m is None:
        return []
    ids, mat, model = m
    if model != c["model"]:
        return []
    w = np.frombuffer(c["w"], dtype=np.float32)
    s = 1 / (1 + np.exp(-np.clip(mat.astype(np.float32) @ w + c["b"], -30, 30)))
    order = np.argsort(-s)[:limit]
    return [{"id": int(ids[k]), "score": round(float(s[k]), 4), "level": "strong" if s[k] >= c["threshold"] else "possible"}
            for k in order if s[k] >= 0.5 * c["threshold"]]


# ---------------------------------------------------------------- choosing a set

def _vecs(conn, ids: list[int], allv=None) -> dict[int, np.ndarray]:
    allv = embeddings.whole_frame(conn) if allv is None else allv
    return {i: allv[i] for i in ids if i in allv}


def _mmr(cands: list[int], quality: dict[int, float], vecs: dict[int, np.ndarray], n: int, lam: float = 0.72,
         bonus=None) -> list[int]:
    """Maximal marginal relevance: good photos that don't repeat each other."""
    chosen: list[int] = []
    sims_max = {c: 0.0 for c in cands}
    left = set(cands)
    while left and len(chosen) < n:
        best, best_s = None, -1e9
        for c in left:
            s = lam * quality[c] - (1 - lam) * sims_max[c] + (bonus(c, chosen) if bonus else 0.0)
            if s > best_s:
                best, best_s = c, s
        chosen.append(best)
        left.discard(best)
        v = vecs.get(best)
        if v is not None:
            for c in left:
                u = vecs.get(c)
                if u is not None:
                    sims_max[c] = max(sims_max[c], float(u @ v))
    return chosen


def balance(conn, target: int, flt: Filter | None = None) -> dict:
    """A delivery-sized set from your picks (topped up with the best suggestions): spread over
    events and time, every named person included, near-repeats avoided. Suggestions only."""
    base = flt or Filter()
    base.no_flag = ",".join(filter(None, [base.no_flag, NOT_JUNK]))
    rows = [r for r in library.shots(conn, base) if r["pick"] != -1]
    picks = [r for r in rows if r["pick"] == 1 or (r["rating"] or 0) >= 3]
    pool = picks if len(picks) >= target else picks + sorted(
        [r for r in rows if r not in picks and r.get("suggestion") == "pick"], key=lambda r: -(r["quality"] or 0)
    )[: max(0, 2 * target - len(picks))]
    if not pool:
        return {"ids": [], "from_picks": 0, "suggested": [], "events": 0, "people": {}, "missing_people": []}
    ids = [r["id"] for r in pool]
    q = {r["id"]: (r["quality"] or 50) / 100 + (0.15 if r["pick"] == 1 else 0) + 0.05 * (r["rating"] or 0) for r in pool}
    vecs = _vecs(conn, ids)
    event_of = {r["id"]: r.get("event_id") or r.get("group_id") for r in pool}
    people_of: dict[int, set] = defaultdict(set)
    for sid, pid in conn.execute(
            f"SELECT shot_id, person_id FROM faces WHERE person_id > 0 AND shot_id IN ({','.join(str(i) for i in ids)})"):
        people_of[sid].add(pid)
    want_people = Counter(p for s in ids for p in people_of[s])

    def bonus(c, chosen):
        have_e = {event_of[x] for x in chosen}
        have_p = Counter(p for x in chosen for p in people_of[x])
        b = 0.12 if event_of[c] not in have_e else 0.0
        b += 0.1 * sum(1 for p in people_of[c] if have_p[p] < 2)
        return b

    chosen = _mmr(ids, q, vecs, target, bonus=bonus)
    covered = Counter(p for x in chosen for p in people_of[x])
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM people")}
    pick_ids = {r["id"] for r in picks}
    return {"ids": chosen, "from_picks": sum(1 for c in chosen if c in pick_ids),
            "suggested": [c for c in chosen if c not in pick_ids],          # topped up: not reviewed yet
            "events": len({event_of[c] for c in chosen}),
            "people": {names.get(p, str(p)): covered[p] for p in want_people},
            "missing_people": [names.get(p, str(p)) for p in want_people if not covered[p]]}


def best_of(conn, year: int | None = None, per: str = "month", n_per: int = 2, event_id: int | None = None) -> dict:
    """The strongest, least repetitive photos of each week or month (of a year, or of an event)."""
    flt = Filter(no_flag=NOT_JUNK, event_id=event_id)
    if year:
        flt.date_from, flt.date_to = f"{year}-01-01", f"{year}-12-31"
    # Only what's needed of each photo, in the Library's time order (a whole library's full rows took
    # seconds to read and turn into dicts).
    where, args = flt.sql(scan=not year and event_id is None)
    rows = conn.execute(
        f"SELECT s.id, s.eff_ts, m.quality, COALESCE(d.pick, 0) AS pick, d.rating, m.taste {library.LIGHT_FROM}{where}"
        " AND COALESCE(d.pick, 0) != -1 AND (s.media IS NULL OR s.media != 'video') AND s.eff_ts IS NOT NULL AND s.eff_ts != 0"
        f" ORDER BY {library.SORTS['time']}", args).fetchall()
    buckets: dict[str, list] = defaultdict(list)
    for r in rows:
        d = datetime.fromtimestamp(r["eff_ts"], timezone.utc)
        key = d.strftime("%Y-W%V") if per == "week" else d.strftime("%Y-%m") if per == "month" else d.strftime("%Y-%m-%d")
        buckets[key].append(r)
    out = []
    t = taste.meta()
    allv = embeddings.whole_frame(conn) if buckets else None      # (once, not once a period)
    for key in sorted(buckets):
        rs = buckets[key]
        q = {r["id"]: (r["quality"] or 0) / 100 + (0.3 if r["pick"] == 1 else 0) + 0.06 * (r["rating"] or 0)
             + (0.2 * (r["taste"] or 0) if t.get("trusted") else 0) for r in rs}
        top = sorted(rs, key=lambda r: -q[r["id"]])[:40]
        chosen = _mmr([r["id"] for r in top], q, _vecs(conn, [r["id"] for r in top], allv), n_per)
        out.append({"period": key, "ids": chosen, "of": len(rs)})
    return {"periods": out, "ids": [i for p in out for i in p["ids"]]}


def taste_questions(conn, n: int = 10) -> list[int]:
    """Undecided photos the taste model is least sure about (or, before it exists, a spread
    across quality), so each answer teaches it the most."""
    rows = conn.execute(
        "SELECT s.id, m.taste, m.quality FROM shots s JOIN metrics m ON m.shot_id = s.id LEFT JOIN decisions d ON d.shot_id = s.id"
        " WHERE s.status='present' AND d.shot_id IS NULL AND COALESCE(s.media,'photo')='photo'"
        " AND COALESCE(m.flags,'') NOT LIKE '%\"tiny\"%' AND COALESCE(m.flags,'') NOT LIKE '%\"duplicate\"%'"
        " AND COALESCE(m.group_rank, 1) = 1").fetchall()
    if not rows:
        return []
    if any(r["taste"] is not None for r in rows):
        rows = sorted((r for r in rows if r["taste"] is not None), key=lambda r: abs(r["taste"] - 0.5))
        cand = [r["id"] for r in rows[: n * 6]]
    else:
        rows = sorted(rows, key=lambda r: r["quality"] or 0)
        step = max(1, len(rows) // (n * 3))
        cand = [r["id"] for r in rows[::step]]
    vecs = _vecs(conn, cand)
    return _mmr(cand, {c: 1.0 for c in cand}, vecs, n, lam=0.3)


# ---------------------------------------------------------------- shot list

# Frames you said cover an item ("covers this shot"), whatever search makes of them: {item id: [shot
# ids]} in the library's settings. A tagged frame covers its item once it's picked.
SHOT_TAGS = "shotlist_tags"


def shot_tags(conn) -> dict[int, list[int]]:
    return {int(k): [int(i) for i in v] for k, v in (db.get_setting(conn, SHOT_TAGS) or {}).items()}


def _save_tags(conn, tags: dict[int, list[int]], commit: bool = True) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (SHOT_TAGS, json.dumps({str(k): v for k, v in sorted(tags.items()) if v})))
    if commit:
        conn.commit()


def tag_shots(conn, item_id: int, ids: list[int], on: bool = True) -> list[int]:
    """Say these frames cover a shot-list item (on) or don't. Returns the item's tagged frames;
    ValueError if there's no such item."""
    db.write_lock(conn)
    if not conn.execute("SELECT 1 FROM shotlist WHERE id=?", (item_id,)).fetchone():
        conn.rollback()
        raise ValueError("That shot-list item has gone (removed on another page?). Reload and try again.")
    tags = shot_tags(conn)
    have = tags.get(item_id, [])
    if on:
        known = {r[0] for r in conn.execute(f"SELECT id FROM shots WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
        have = have + [i for i in dict.fromkeys(ids) if i in known and i not in have]
    else:
        drop = set(ids)
        have = [i for i in have if i not in drop]
    tags[item_id] = have
    _save_tags(conn, tags)
    return have


def untag_item(conn, item_id: int) -> None:
    tags = shot_tags(conn)
    if tags.pop(item_id, None) is not None:
        _save_tags(conn, tags)


def carry_shot_tags(conn, src: int, dst: int, keep: bool = False) -> None:
    """One photo's tags go to another (two files joined as one photo, or a copy that stays when the
    other goes to the Trash); with keep, the first photo keeps them too. Doesn't commit."""
    tags = shot_tags(conn)
    changed = False
    for ids in tags.values():
        if src in ids:
            if dst not in ids:
                ids.insert(ids.index(src) + 1, dst)
            if not keep:
                ids.remove(src)
            changed = True
    if changed:
        _save_tags(conn, tags, commit=False)


def shotlist_status(conn) -> list[dict]:
    """Each item, with the picks that cover it: the frames you tagged for it once picked, and the
    picks search finds for it. `tagged` lists every frame tagged for it, picked or not."""
    from . import search

    items = [dict(r) for r in conn.execute("SELECT * FROM shotlist ORDER BY COALESCE(position, id)")]
    if not items:
        return []
    picked = {r[0] for r in conn.execute("SELECT d.shot_id FROM decisions d JOIN shots s ON s.id=d.shot_id"
                                         " WHERE s.status='present' AND (d.pick=1 OR COALESCE(d.rating,0)>=4)")}
    tags = shot_tags(conn)
    every = sorted({i for ids in tags.values() for i in ids})
    present = {r[0] for k in range(0, len(every), 500) for r in conn.execute(
        f"SELECT id FROM shots WHERE status='present' AND id IN ({','.join('?' * len(every[k:k + 500]))})", every[k:k + 500])}
    can_search = search.available() and search.indexed_count(conn)
    for it in items:
        people = json.loads(it["people"] or "[]")
        scope = set(picked)
        for pid in people:
            scope &= {r[0] for r in conn.execute("SELECT DISTINCT shot_id FROM faces WHERE person_id=?", (pid,))}
        covered: list[int] = []
        if it["query"] and can_search and scope:
            hits = search.search(conn, it["query"], limit=24, restrict=scope)
            covered = [h["id"] for h in hits if h["level"] in ("strong", "possible")]
        elif not it["query"]:
            covered = sorted(scope)[:24]
        # What you tagged comes first and counts as it is (you know who's in it).
        tagged = [i for i in tags.get(it["id"], []) if i in present]
        yours = [i for i in tagged if i in picked]
        covered = yours + [i for i in covered if i not in yours]
        cands: list[int] = []
        if len(covered) < it["min_count"] and it["query"] and can_search:
            allow = None
            if people:
                allow = set.intersection(*[{r[0] for r in conn.execute("SELECT DISTINCT shot_id FROM faces WHERE person_id=?",
                                                                       (pid,))} for pid in people])
            cands = [h["id"] for h in search.search(conn, it["query"], limit=12, restrict=allow) if h["level"] != "unlikely"
                     and h["id"] not in picked and h["id"] not in tagged][:8]
        it.update(people=people, covered=covered, done=len(covered) >= it["min_count"], candidates=cands, tagged=tagged)
    return items
