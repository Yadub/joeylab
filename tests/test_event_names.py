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
