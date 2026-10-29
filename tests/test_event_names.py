"""Events are named from their folders, never a drive's or the home folder's name; what their photos
show is their other name, and the folder they're in is kept beside it."""

from __future__ import annotations

import os

from conftest import add_root, jpeg_at, run_all


def test_drive_and_home_folders_never_name_an_event():
    from joeylab import events

    home = os.path.expanduser("~")
    assert events.folder_hint("/Volumes/Photo Drive/SD Card 20251110 (TO SORT)") is None
    assert events.folder_hint("/Volumes/Photo Drive/Backups/SD Card 20251110/DCIM/100_FUJI") is None
    assert events.folder_hint("/Volumes/Photo Drive/2026-01 Lisbon trip/DCIM/100_FUJI") == "Lisbon trip"
    assert events.folder_hint(f"{home}/Pictures/Imports") is None
    assert events.folder_label("/Volumes/Photo Drive/Backups/SD Card 20251110 (TO SORT)/DCIM/100_FUJI") == "SD Card 20251110 (TO SORT)"
    assert events.folder_label("/Volumes/Photo Drive") is None
    assert events.folder_label("/Volumes/Photo Drive/2025-08 Recital/raf") == "2025-08 Recital"
    assert events.folder_hint("/Volumes/Photo Drive/Unsure") is None
    assert events.folder_hint("/Volumes/Photo Drive/Random") is None
    assert events.folder_hint("/Volumes/Photo Drive/mov") is None
    assert events.folder_hint("/Volumes/Photo Drive/202308 Lisbon + Porto") == "Lisbon + Porto"
    assert events.folder_hint("/Volumes/Photo Drive/2026-06-01to02 Home") == "Home"
    assert events.folder_hint("/Volumes/Photo Drive/2023-09 Lisbon - Family Holiday") == "Lisbon - Family Holiday"


def _shoot(lib, folder, day, seed):
    for i in range(6):
        jpeg_at(lib / folder / f"IMG_{seed + i:04d}.JPG", seed + i, f"{day} 10:{i * 5:02d}:00", size=(300, 200))


def test_every_event_gets_what_its_photos_show_as_its_other_name(env, monkeypatch):
    from joeylab import events, scenes, search

    _, lib = env
    _shoot(lib, "2024-05 Lisbon trip", "2024:05:04", 0)
    _shoot(lib, "Backups/Camera/SD Card 20240601", "2024:06:01", 20)
    add_root(lib)
    conn, _ = run_all()
    monkeypatch.setattr(scenes, "event_kinds", lambda c, evs: ["Dance performance"] * len(evs))
    events.run(conn)
    rows = {r["folder"]: dict(r) for r in conn.execute("SELECT name, auto_name, alt_name, folder FROM events WHERE kind='event'")}
    lisbon, card = rows["2024-05 Lisbon trip"], rows["SD Card 20240601"]
    assert lisbon["name"].startswith("Lisbon trip") and lisbon["alt_name"] == "Dance performance"
    assert card["name"].startswith("Dance performance") and card["alt_name"] == "Dance performance"
    # Found by either name in search.
    from joeylab import query

    assert query.parse("event:dance", conn)["filter"].get("event") in {r[0] for r in conn.execute("SELECT id FROM events")}
    assert query.parse("event:SD Card", conn)["filter"].get("event")


def test_an_indian_weddings_occasions_are_named_or_called_a_wedding():
    """Haldi, mehendi, sangeet and the rest are kinds of their own; an event split between them is a Wedding
    when together they have most of its photos, and festivals are named too."""
    import numpy as np

    from joeylab import explore, scenes

    names = [k for k, _ in scenes.KINDS]
    for word in ("Haldi", "Mehendi", "Sangeet", "Baraat", "Varmala", "Pheras", "Vidaai", "Wedding reception",
                 "Sadya", "Pookalam", "Diwali"):
        assert word in names and word in explore.VOCAB and word in explore.DESCRIBED
    # One description per kind and per nothing-in-particular phrase, each on an axis of its own.
    n_desc = sum(len(p) for _, p in scenes.KINDS) + len(scenes.NOTHING_IN_PARTICULAR)
    M = np.eye(n_desc, dtype=np.float32)
    owner = np.array([i for i, (_, p) in enumerate(scenes.KINDS) for _ in p]
                     + [len(scenes.KINDS) + j for j in range(len(scenes.NOTHING_IN_PARTICULAR))])
    first = {name: int(np.flatnonzero(owner == i)[0]) for i, name in enumerate(names)}

    def event(*parts):
        X = [np.full(n_desc, 0.01, dtype=np.float32) for kind, n in parts for _ in range(n)]
        for row, kind in zip(X, [kind for kind, n in parts for _ in range(n)]):
            row[first[kind]] = 1.0
        return X

    plan = [event(("Haldi", 4), ("Sangeet", 4), ("Beach", 3)),       # no one occasion has 40%, the wedding most: a Wedding
            event(("Haldi", 9), ("Beach", 3)),                         # a haldi
            event(("Climbing", 10)),
            event(("Haldi", 2), ("Sangeet", 2), ("Beach", 3), ("Climbing", 3))]   # occasions, but not most of it: nothing
    rows, events = [], []
    for X in plan:
        events.append(list(range(len(rows), len(rows) + len(X))))
        rows += X
    got = scenes.kinds_of(np.arange(len(rows)), np.stack(rows).astype(np.float16), events, (M, owner))
    assert got == ["Wedding", "Haldi", "Climbing", None]


def test_described_words_must_fit_a_region_better_than_plain_ones():
    import numpy as np

    from joeylab import explore

    words = ["Sarees", "Vidaai"]
    W = np.eye(2, dtype=np.float32)
    level = np.array([[0.70, 0.70], [0.10, 0.10]], dtype=np.float32)     # as close to either: the plain word
    clear = np.array([[0.30, 0.90], [0.20, 0.10]], dtype=np.float32)     # clearly the occasion: it's named
    assert explore.name_clusters(level, W, words)[0].startswith("Sarees")
    assert explore.name_clusters(clear, W, words)[0].startswith("Vidaai")
