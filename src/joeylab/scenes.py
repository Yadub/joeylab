"""What an event looks like, in a word or two ("Dance performance", "Climbing", "Wedding"), from the
photos themselves, on this Mac: the picture-search model compares each photo with a short list of
kinds of occasion (and with ordinary subjects that are no occasion at all), and an event gets a kind
only when most of its photos agree.

The descriptions' text vectors are computed once and kept in ~/.joeylab/models, so naming events
during an update doesn't need the model loaded. No AI service is involved.
"""

from __future__ import annotations

import hashlib
import json
import os

import numpy as np

from . import config

# Each kind is described as what a photo of it shows (the picture-search model matches captions, not names):
# an Indian wedding's occasions and Indian festivals are told by what's in the frame (turmeric, henna, a
# fire, garlands, banana leaves, oil lamps).
KINDS: list[tuple[str, list[str]]] = [
    ("Wedding", ["a wedding ceremony", "a bride and groom at their wedding"]),
    ("Haldi", ["a haldi ceremony: turmeric paste smeared on an indian bride or groom",
               "people in yellow clothes covered in turmeric at a haldi"]),
    ("Mehendi", ["a mehendi ceremony: henna patterns being drawn on a bride's hands",
                 "close-up of hands covered in intricate dark henna designs"]),
    ("Sangeet", ["a sangeet: wedding guests dancing to bollywood music in front of a big led screen",
                 "an indian family performing a choreographed dance at a wedding sangeet"]),
    ("Baraat", ["a baraat: the groom's wedding procession dancing in the street with a band",
                "an indian groom in a sherwani and turban riding a white horse"]),
    ("Varmala", ["a varmala: an indian bride and groom exchanging flower garlands on a stage",
                 "an indian bride putting a flower garland around the groom's neck"]),
    ("Pheras", ["pheras: an indian bride and groom walking around a sacred fire",
                "a hindu wedding ceremony around a fire under a decorated mandap"]),
    ("Vidaai", ["a vidaai: an indian bride throwing rice over her head as she leaves her parents' home",
                "an indian bride waving goodbye from a car decorated with flowers"]),
    ("Wedding reception", ["a wedding reception: the bride and groom on a decorated stage greeting guests",
                           "a bride and groom in evening clothes posing with guests on a reception stage"]),
    ("Dance performance", ["a classical dance performance on stage", "a dancer in costume performing on a stage",
                           "a bharatanatyam dancer on stage"]),
    ("Concert", ["musicians playing guitars and drums at a concert", "a band playing music with microphones and instruments"]),
    ("Birthday", ["a birthday party with a cake and candles"]),
    ("Climbing", ["rock climbing on a cliff", "a climber on a rock wall"]),
    ("Hike", ["hiking on a mountain trail", "a mountain landscape on a hike"]),
    ("Beach", ["a beach by the sea", "people on a sandy beach"]),
    ("Snow", ["snow and skiing in the mountains"]),
    ("Diving", ["underwater scuba diving with fish"]),
    ("City walk", ["walking through city streets", "old buildings in a city"]),
    ("Museum", ["paintings in an art museum"]),
    ("Temple visit", ["a hindu temple with carved stone towers", "a buddhist temple with a golden statue",
                      "a japanese shrine with a red torii gate", "an ancient greek temple with columns", "visiting a temple"]),
    ("Church", ["inside a church or cathedral with stained glass", "a gothic cathedral"]),
    ("Festival", ["a festival celebration with decorations and lights"]),
    ("Dinner", ["friends having dinner at a restaurant table"]),
    ("Graduation", ["a graduation ceremony with gowns"]),
    ("New baby", ["a newborn baby"]),
    ("Family gathering", ["a family gathering at home"]),
    ("Pool day", ["swimming in a pool"]),
    ("Match", ["a sports match"]),
    ("Garden", ["a garden with flowers and plants"]),
    ("Safari", ["wild animals on a safari"]),
    ("Road trip", ["a road trip with a car on the road"]),
    ("Night out", ["people at a bar at night"]),
    ("Puja", ["a hindu puja ceremony with offerings"]),
    ("Sadya", ["a sadya: a kerala feast served on a banana leaf", "people eating rice and many curries on banana leaves"]),
    ("Pookalam", ["a pookalam: a circular kerala flower carpet made of petals on the floor",
                  "a colourful floral rangoli of flower petals"]),
    ("Diwali", ["diwali: rows of lit diyas, small clay oil lamps", "diwali night with fireworks, sparklers and rangoli"]),
    # (Without it, Holi's colours read as Haldi's turmeric.)
    ("Holi", ["holi: people covered in bright coloured powder", "friends throwing coloured powder at each other at holi"]),
]
# Ordinary photos (a portrait, a street, a room) lean to one of these instead of to the nearest
# kind above: without them, any vaguely described kind ("visiting a temple") wins half the library.
NOTHING_IN_PARTICULAR = [
    "a portrait photo of a person", "a group of friends posing for a photo", "a selfie", "people talking indoors",
    "a city street with buildings", "a building exterior", "a landscape with sky", "a room inside a house",
    "a close-up of an object", "a plate of food", "a car", "a dog", "a cat", "a screenshot or document",
    "people walking outdoors", "a tourist posing in front of a landmark", "a garden with plants and trees",
    "an empty hallway or staircase", "a palace or a historic building", "a hotel room or lobby",
]
MIN_SHARE = 0.4          # this share of an event's photos must lean to one kind
MIN_PHOTOS = 4
# An Indian wedding's occasions are each a wedding too: an event split between them (and photos that are
# just "a wedding") is a Wedding when together they have most of its photos. (Most, not MIN_SHARE: photos of
# people in Indian clothes lean a little to one occasion or another at any family gathering.)
PART_OF = {k: "Wedding" for k in ("Haldi", "Mehendi", "Sangeet", "Baraat", "Varmala", "Pheras", "Vidaai", "Wedding reception")}
FAMILY_SHARE = 0.5


def _cache_path():
    from . import search

    key = hashlib.sha1(json.dumps([search.MODEL_TAG, KINDS, NOTHING_IN_PARTICULAR]).encode()).hexdigest()[:12]
    return config.MODELS_DIR / f"scene-kinds-{key}.npz"


def kind_vectors() -> tuple[np.ndarray, np.ndarray] | None:
    """(one unit vector per description, the kind each belongs to). Kinds are numbered as in KINDS;
    the nothing-in-particular descriptions come after, one number each. Computed once and cached."""
    path = _cache_path()
    if path.exists():
        try:
            with np.load(path) as f:
                return f["vectors"], f["owner"]
        except (OSError, ValueError, KeyError):
            pass
    from . import search

    if not search.available() or os.environ.get("JOEYLAB_OFFLINE"):
        return None
    try:
        model = search.Model.get()
    except Exception:
        return None
    rows, owner = [], []
    for i, (_, phrases) in enumerate(KINDS):
        for p in phrases:
            rows.append(model.embed_text(p))
            owner.append(i)
    for j, p in enumerate(NOTHING_IN_PARTICULAR):
        rows.append(model.embed_text(p))
        owner.append(len(KINDS) + j)
    m = np.stack([v / (np.linalg.norm(v) + 1e-9) for v in rows]).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}-{os.getpid()}.npz")
    np.savez(tmp, vectors=m, owner=np.array(owner))
    os.replace(tmp, path)
    for old in path.parent.glob("scene-kinds-*"):       # vectors for an earlier list of kinds
        if old != path:
            old.unlink(missing_ok=True)
    return m, np.array(owner)


def whole_frames(conn, ids: list[int]) -> tuple[np.ndarray, np.ndarray]:
    """(photo ids in order, their whole-frame search vectors as one float16 matrix: the first of each photo's
    crops). Those in the search store kept in memory (embeddings.matrix) come from there; only any others
    (indexed from an earlier version of the photo, say) are read from the catalog, which used to take
    seconds for a big library. Photos without a vector are left out."""
    from . import embeddings

    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='search_vecs'").fetchone():
        return np.empty(0, dtype=np.int64), np.empty((0, 0), dtype=np.float16)
    want = np.unique(np.asarray(ids, dtype=np.int64))
    keys, X = np.empty(0, dtype=np.int64), None
    m = embeddings.matrix(conn)
    if m is not None and m[2] == "siglip2":   # (CLIP's vectors, when there's no current index, aren't these)
        rows = np.nonzero(np.isin(m[0], want))[0]
        keys, X = m[0][rows].astype(np.int64), m[1][rows]
    rest = np.setdiff1d(want, keys).tolist()
    if rest:
        more, MX = _read_frames(conn, rest, None if X is None else X.shape[1])
        if X is None or not len(keys):
            keys, X = more, MX
        elif len(more):
            keys, X = np.concatenate([keys, more]), np.concatenate([X, MX])
    if X is None:
        return np.empty(0, dtype=np.int64), np.empty((0, 0), dtype=np.float16)
    order = np.argsort(keys, kind="stable")
    return keys[order], X[order]


def _read_frames(conn, ids: list[int], dim: int | None) -> tuple[np.ndarray, np.ndarray]:
    """whole_frames() for these photos, read from their rows of vectors."""
    keys: list[int] = []
    X = None
    for i in range(0, len(ids), 900):
        chunk = ids[i : i + 900]
        for sid, blob in conn.execute(f"SELECT shot_id, vecs FROM search_vecs WHERE shot_id IN ({','.join('?' * len(chunk))})", chunk):
            v = np.frombuffer(blob, dtype=np.float16)
            d = 768 if v.size % 768 == 0 else v.size // 10
            if X is None:
                X = np.empty((len(ids), dim or d), dtype=np.float16)
            if v.size < X.shape[1] or d != X.shape[1]:
                continue
            X[len(keys)] = v[: X.shape[1]]
            keys.append(sid)
    if X is None:
        return np.empty(0, dtype=np.int64), np.empty((0, dim or 0), dtype=np.float16)
    return np.array(keys, dtype=np.int64), X[: len(keys)]


def event_kinds(conn, events: list[list[int]]) -> list[str | None]:
    """For each event (its photo ids), the kind of occasion most of its photos show, or None."""
    kv = kind_vectors()
    if kv is None:
        return [None] * len(events)
    all_ids = sorted({i for ids in events for i in ids})
    keys, X16 = whole_frames(conn, all_ids)
    return kinds_of(keys, X16, events, kv)


def kinds_of(keys: np.ndarray, X16: np.ndarray, events: list[list[int]], kv: tuple) -> list[str | None]:
    """event_kinds() from the photos' whole-frame vectors (keys: their ids) and kind_vectors()."""
    M, owner = kv
    if not len(keys) or X16.shape[1] != M.shape[1]:
        return [None] * len(events)
    n_kinds = int(owner.max()) + 1                # the kinds, then each nothing-in-particular description
    S = np.empty((len(keys), n_kinds), dtype=np.float32)
    for i in range(0, len(keys), 32768):           # float32 a slice at a time
        X = X16[i : i + 32768].astype(np.float32)
        X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
        P = X @ M.T
        # A kind is as close as its closest description ("a japanese shrine" for a shrine), not
        # the average of them all.
        for k in range(n_kinds):
            S[i : i + 32768, k] = P[:, owner == k].max(axis=1)
    # A photo votes for the kind it's closest to, but only if it shows more of that kind than an
    # average photo in the library does (so "city" or "family" can't win by being vaguely everywhere,
    # while what you photograph a lot, dance say, still counts).
    Z = (S - S.mean(axis=0)) / (S.std(axis=0) + 1e-6)
    best = S.argmax(axis=1)
    strong = Z[np.arange(len(best)), best] >= 0.0
    vote = {int(k): (int(best[i]) if strong[i] else -1) for i, k in enumerate(keys)}
    out: list[str | None] = []
    for ids in events:
        votes = [vote[i] for i in ids if i in vote]
        if len(votes) < MIN_PHOTOS:
            out.append(None)
            continue
        counts = np.bincount([v for v in votes if v >= 0], minlength=n_kinds) if any(v >= 0 for v in votes) else None
        if counts is None:
            out.append(None)
            continue
        k = int(counts.argmax())        # nothing in particular can win too: then the event has no kind
        ok = k < len(KINDS) and counts[k] >= MIN_SHARE * len(votes) and counts[k] >= MIN_PHOTOS
        if not ok and k < len(KINDS):
            whole = PART_OF.get(KINDS[k][0], KINDS[k][0])
            family = sum(counts[j] for j, (name, _) in enumerate(KINDS) if PART_OF.get(name, name) == whole)
            if family >= FAMILY_SHARE * len(votes) and family >= MIN_PHOTOS:
                out.append(whole)
                continue
        out.append(KINDS[k][0] if ok else None)
    return out
