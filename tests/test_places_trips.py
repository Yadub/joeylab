"""Places and trips: homes and the trip distance, another country always away, a day abroad, places
from folder names (and the words that only look like towns), a place you set, and re-detection
keeping what you named and merged."""

from __future__ import annotations

import pytest

from conftest import add_root, jpeg_at, run_all

# A small town list: id, name, lat, lon, country, region, people, time zone.
TOWNS = [
    (1, "Delhi", 28.652, 77.231, "IN", "07", 11034555, "Asia/Kolkata"),
    (2, "Gurugram", 28.460, 77.026, "IN", "10", 1000000, "Asia/Kolkata"),
    (3, "Mumbai", 19.073, 72.883, "IN", "16", 12691836, "Asia/Kolkata"),
    (4, "Amritsar", 31.622, 74.875, "IN", "23", 1092450, "Asia/Kolkata"),
    (5, "Lahore", 31.558, 74.351, "PK", "04", 6310888, "Asia/Karachi"),
    (6, "Dubai", 25.077, 55.309, "AE", "03", 3790000, "Asia/Dubai"),
    (7, "Phuket", 8.072, 98.910, "TH", "63", 31219, "Asia/Bangkok"),
    (8, "Larnaca", 34.923, 33.623, "CY", "04", 72000, "Asia/Nicosia"),
    (9, "Lisbon", 38.717, -9.133, "PT", "14", 517802, "Europe/Lisbon"),
    (10, "Nice", 43.703, 7.266, "FR", "B8", 342669, "Europe/Paris"),
    (11, "Bath", 51.380, -2.360, "GB", "ENG", 101557, "Europe/London"),
    (12, "Anna", 51.489, 40.418, "RU", "86", 19148, "Europe/Moscow"),
    (13, "Victoria", 48.433, -123.366, "CA", "02", 289625, "America/Vancouver"),
    (14, "March", 52.551, 0.088, "GB", "ENG", 21051, "Europe/London"),
    (15, "Nicosia", 35.175, 33.364, "CY", "04", 200452, "Asia/Nicosia"),
    (16, "Fuji", 35.167, 138.683, "JP", "19", 245000, "Asia/Tokyo"),
    (17, "Savannah", 32.081, -81.091, "US", "GA", 147780, "America/New_York"),
]
COUNTRIES = {"IN": "India", "PK": "Pakistan", "AE": "United Arab Emirates", "TH": "Thailand", "CY": "Cyprus",
             "PT": "Portugal", "FR": "France", "GB": "United Kingdom", "RU": "Russia", "CA": "Canada", "JP": "Japan", "US": "United States"}


@pytest.fixture()
def towns(env, tmp_path, monkeypatch):
    from joeylab import geo

    g = tmp_path / "geonames"
    g.mkdir()
    (g / "countryInfo.txt").write_text("#\n" + "".join(f"{cc}\tX\t0\tX\t{name}\tX\n" for cc, name in COUNTRIES.items()))
    (g / "admin1CodesASCII.txt").write_text("".join(f"{t[4]}.{t[5]}\tRegion {t[4]}{t[5]}\tRegion {t[4]}{t[5]}\t1\n" for t in TOWNS))
    (g / "cities5000.txt").write_text("".join(
        "\t".join([str(i), name, name, "", str(lat), str(lon), "P", "PPL", cc, "", a1, "", "", "", str(pop), "", "0", tz, "2026-01-01"]) + "\n"
        for i, name, lat, lon, cc, a1, pop, tz in TOWNS))
    monkeypatch.setattr(geo, "GEO_DIR", g)
    geo.Gazetteer._inst = None
    geo._COORDS.clear()
    yield env
    geo.Gazetteer._inst = None
    geo._COORDS.clear()


def town(name):
    return next(t for t in TOWNS if t[1] == name)


def shoot(lib, folder, day, n=4, hour=10, seed=0):
    """n photos a few minutes apart on one day ("2024:03:01")."""
    for i in range(n):
        jpeg_at(lib / folder / f"IMG_{seed + i:04d}.JPG", seed + i, f"{day} {hour:02d}:{i * 7:02d}:00", size=(300, 200))


def place_folder(conn, folder, name, src="gps"):
    """Give a folder's photos the GPS position of a town (as if the camera had recorded it)."""
    t = town(name)
    conn.execute("UPDATE shots SET gps_lat=?, gps_lon=? WHERE dir LIKE ?", (t[2], t[3], f"%/{folder}"))
    conn.commit()


def detect(conn):
    from joeylab import events, geo

    geo.assign(conn, download=False)
    events.run(conn)
    return {e["name"].split(" · ")[0]: e for e in events.listing(conn, "trip")}


def set_homes(conn, *names):
    from joeylab import geo

    geo.set_homes(conn, [town(n)[0] for n in names])


# ---------------------------------------------------------------- homes and the distance

def test_homes_and_the_trip_distance(towns):
    from joeylab import geo

    _, lib = towns
    shoot(lib, "gurgaon", "2024:03:01", seed=0)            # 30 km from Delhi
    shoot(lib, "mumbai-1", "2024:04:10", seed=10)          # 1,150 km, over two days
    shoot(lib, "mumbai-2", "2024:04:11", seed=20)
    shoot(lib, "dubai-1", "2024:06:01", seed=30)
    shoot(lib, "dubai-2", "2024:06:02", seed=40)
    add_root(lib)
    conn, _ = run_all()
    for f, t in [("gurgaon", "Gurugram"), ("mumbai-1", "Mumbai"), ("mumbai-2", "Mumbai"), ("dubai-1", "Dubai"), ("dubai-2", "Dubai")]:
        place_folder(conn, f, t)
    set_homes(conn, "Delhi")
    trips = detect(conn)
    assert set(trips) == {"Mumbai trip", "Dubai trip"}
    why = trips["Mumbai trip"]["place"]
    assert why["name"] == "Mumbai" and why["away"]["home"] == "Delhi" and 1100 < why["away"]["km"] < 1200
    assert not why["away"]["abroad"] and trips["Dubai trip"]["place"]["away"]["abroad"]

    # Further than 2,000 km only: Mumbai is near enough, Dubai is another country.
    geo.set_trip_km(2000)
    assert set(detect(conn)) == {"Dubai trip"}
    # Dubai is a home too: away from every home is what counts.
    geo.set_trip_km(100)
    set_homes(conn, "Delhi", "Dubai")
    assert set(detect(conn)) == {"Mumbai trip"}
    assert geo.rule(conn)["homes"][1]["name"] == "Dubai" and geo.rule(conn)["guess"] is None


def test_with_no_home_set_the_guess_is_shown_and_used(towns):
    from joeylab import events, geo

    _, lib = towns
    for m in range(1, 5):                                   # Delhi in four different months
        shoot(lib, f"delhi-{m}", f"2024:0{m}:05", seed=m * 10)
    shoot(lib, "phuket-1", "2024:06:01", seed=100)
    shoot(lib, "phuket-2", "2024:06:02", seed=110)
    add_root(lib)
    conn, _ = run_all()
    for m in range(1, 5):
        place_folder(conn, f"delhi-{m}", "Delhi")
    place_folder(conn, "phuket-1", "Phuket")
    place_folder(conn, "phuket-2", "Phuket")
    trips = detect(conn)
    r = geo.rule(conn)
    assert r["homes"] == [] and r["guess"]["name"] == "Delhi"
    assert set(trips) == {"Phuket trip"} and trips["Phuket trip"]["place"]["away"]["home"] == "Delhi"
    assert not events.stale(conn)
    set_homes(conn, "Phuket")                     # homes changed: the trips found before are out of date
    assert events.stale(conn)


# ---------------------------------------------------------------- another country, a day abroad

def test_another_country_is_away_even_across_the_border(towns):
    _, lib = towns
    shoot(lib, "lahore-1", "2024:05:01", seed=0)            # 45 km from Amritsar, in Pakistan
    shoot(lib, "lahore-2", "2024:05:02", seed=10)
    add_root(lib)
    conn, _ = run_all()
    place_folder(conn, "lahore-1", "Lahore")
    place_folder(conn, "lahore-2", "Lahore")
    set_homes(conn, "Amritsar")
    trips = detect(conn)
    assert set(trips) == {"Lahore trip"}
    away = trips["Lahore trip"]["place"]["away"]
    assert away["abroad"] and away["km"] < 100


def test_a_single_day_abroad_is_a_trip_but_not_at_home(towns):
    _, lib = towns
    shoot(lib, "dubai", "2024:06:01", seed=0)              # one afternoon in Dubai
    shoot(lib, "mumbai", "2024:07:01", seed=10)             # one afternoon in Mumbai
    add_root(lib)
    conn, _ = run_all()
    place_folder(conn, "dubai", "Dubai")
    place_folder(conn, "mumbai", "Mumbai")
    set_homes(conn, "Delhi")
    assert set(detect(conn)) == {"Dubai trip"}


# ---------------------------------------------------------------- folder names

@pytest.mark.parametrize("path,expected", [
    ("Trips/2023-Cyprus", ("country", "Cyprus")),
    ("Photos/Phuket Jan 2026", ("town", "Phuket")),
    ("Lisbon", ("town", "Lisbon")),
    ("Travel/Cyprus 2023/Larnaca beach", ("town", "Larnaca")),
    ("Phuket Jan 2026/DCIM/100MSDCF", ("town", "Phuket")),
    ("Dad", None), ("Home", None), ("Nice", None), ("Nice day out", None), ("Bath time", None),
    ("March 2023", None), ("Victoria's party", None), ("Anna", None), ("anna birthday", None),
    ("phuket", None), ("lisbon", None),                     # not written as a name
    ("Dad's 60th", None), ("Mum and Dad", None), ("DCIM", None), ("SD Card 20240101", None),
    # Named after the camera, not the place (Fuji is a town in Japan)
    ("Fujifilm XT-5:X100VI Image Backups/202306 Fuji Backup (TO SORT)/DCIM/100_FUJI", None),
    ("Fuji 2023", None), ("2025-12-20 Savannah and Rohan's Wedding", None), ("Savannah 2025", ("town", "Savannah")), ("Fujifilm XT-5:X100VI Image Backups/2026-01 Phuket Climbing", ("town", "Phuket")),
])
def test_folder_names_that_name_a_place(towns, path, expected):
    from joeylab import geo

    hit = geo.Gazetteer.get().folder_place(path.split("/"), {"IN"})
    assert (hit["kind"], hit["name"]) == expected if expected else hit is None


def test_a_folder_name_places_its_event_as_a_guess(towns):
    _, lib = towns
    shoot(lib, "Phuket Jan 2026", "2026:01:24", seed=0)
    shoot(lib, "Phuket Jan 2026", "2026:01:25", seed=10)
    shoot(lib, "Nice day out", "2026:02:01", seed=20)
    add_root(lib)
    conn, _ = run_all()
    set_homes(conn, "Delhi")
    trips = detect(conn)
    assert list(trips) == ["Phuket trip"]
    place = trips["Phuket trip"]["place"]
    assert place["src"] == "folder" and place["name"] == "Phuket" and place["away"]["abroad"]
    srcs = {r[0]: r[1] for r in conn.execute("SELECT dir, place_src FROM shots")}
    assert {v for k, v in srcs.items() if k.endswith("Phuket Jan 2026")} == {"folder"}
    assert {v for k, v in srcs.items() if k.endswith("Nice day out")} == {None}
    # Guessed again each time: the folder's photos don't keep it as if it were known.
    assert set(detect(conn)) == {"Phuket trip"}


def test_a_camera_clock_on_another_time_zone_is_away(towns):
    from joeylab import events

    _, lib = towns
    shoot(lib, "a", "2024:01:05", seed=0)
    shoot(lib, "b", "2024:02:05", seed=10)
    shoot(lib, "c", "2024:03:05", seed=20)
    add_root(lib)
    conn, _ = run_all()
    conn.execute("UPDATE shots SET tz_offset = 330")                         # Delhi time...
    conn.execute("UPDATE shots SET tz_offset = 420 WHERE dir LIKE '%/b'")    # ...but Bangkok time in February
    conn.commit()
    set_homes(conn, "Delhi")
    detect(conn)
    feb = next(e for e in events.listing(conn, "event") if e["place"])
    assert feb["place"]["src"] == "clock" and feb["place"]["offset"] == 420 and feb["place"]["away"]["home_offset"] == 330
    assert len(events.listing(conn, "trip")) == 1
    # A camera that's never on home's time (never set) says nothing.
    conn.execute("UPDATE shots SET tz_offset = 0")
    conn.commit()
    detect(conn)
    assert not events.listing(conn, "trip") and not any(e["place"] for e in events.listing(conn, "event"))


# ---------------------------------------------------------------- what you set and named stays

def test_a_place_you_set_beats_guesses_and_survives_redetection(towns):
    from joeylab import events

    _, lib = towns
    shoot(lib, "Lisbon", "2024:05:01", seed=0)
    shoot(lib, "Lisbon", "2024:05:02", seed=10)
    add_root(lib)
    conn, _ = run_all()
    set_homes(conn, "Delhi")
    detect(conn)
    trip = events.listing(conn, "trip")[0]
    assert trip["place"]["name"] == "Lisbon" and trip["place"]["src"] == "folder"
    r = events.set_place(conn, trip["id"], town("Larnaca")[0])              # it was Larnaca, really
    assert r["n"] == 8 and r["previous"] is None
    for _ in range(2):
        trips = detect(conn)
        assert list(trips) == ["Larnaca trip"] and trips["Larnaca trip"]["place"]["src"] == "manual"
    assert {r[0] for r in conn.execute("SELECT place_src FROM shots")} == {"manual"}
    # Taken away again, the folder's guess comes back.
    events.set_place(conn, trips["Larnaca trip"]["id"], None)
    assert list(detect(conn)) == ["Lisbon trip"]


def test_redetection_keeps_names_and_merges(towns):
    from joeylab import events

    _, lib = towns
    shoot(lib, "Morning climb", "2024:06:01", seed=0)
    shoot(lib, "Night market", "2024:06:01", hour=18, seed=10)     # the same day, eight hours later: two events
    shoot(lib, "l1", "2024:09:01", seed=20)
    shoot(lib, "l1", "2024:09:02", seed=30)
    add_root(lib)
    conn, _ = run_all()
    place_folder(conn, "Morning climb", "Phuket")
    place_folder(conn, "Night market", "Phuket")
    place_folder(conn, "l1", "Larnaca")
    set_homes(conn, "Delhi")
    detect(conn)
    a, b = sorted((e for e in events.listing(conn, "event") if e["start_ts"] < events.listing(conn, "trip")[0]["start_ts"]),
                  key=lambda e: e["start_ts"])
    events.merge(conn, a["id"], b["id"])
    larnaca = next(t for t in events.listing(conn, "trip") if "Larnaca" in t["name"])
    events.rename(conn, larnaca["id"], "Cyprus with Mum")
    set_homes(conn, "Delhi", "Mumbai")                      # homes changed: found again
    detect(conn)
    names = {t["name"] for t in events.listing(conn, "trip")}
    assert "Cyprus with Mum" in names
    phuket_events = [e for e in events.listing(conn, "event") if e["place"] and e["place"]["name"] == "Phuket"]
    assert len(phuket_events) == 1 and phuket_events[0]["id"] == a["id"] and phuket_events[0]["n"] == 8


# ---------------------------------------------------------------- the web app

def test_places_prefs_search_and_set_place_over_the_web(towns):
    import time

    from fastapi.testclient import TestClient

    from joeylab import prefs
    from joeylab.web.jobs import JobManager
    from joeylab.web.server import create_app

    _, lib = towns
    shoot(lib, "Phuket Jan 2026", "2026:01:24", seed=0)
    shoot(lib, "Phuket Jan 2026", "2026:01:25", seed=10)
    add_root(lib)
    run_all()
    client = TestClient(create_app(jobs=JobManager(watch_minutes=0, faces=False, ai_layer=False)))

    def settled():
        for _ in range(100):
            if not client.get("/api/p/t/places/prefs").json()["busy"]:
                return
            time.sleep(0.1)
        raise AssertionError("trips were never found again")

    found = client.get("/api/places/search", params={"q": "del"}).json()["towns"]
    assert found[0]["name"] == "Delhi" and found[0]["cc"] == "IN"
    r = client.post("/api/p/t/places/prefs", json={"homes": [1], "km": 150, "map_tiles": True}).json()
    assert [h["name"] for h in r["homes"]] == ["Delhi"] and r["km"] == 150 and r["map_tiles"] is True
    assert prefs.load()["homes"][0]["tz"] == "Asia/Kolkata"
    settled()
    d = client.get("/api/p/t/events").json()
    trip = next(e for e in d["events"] if e["kind"] == "trip")
    assert d["rule"]["homes"][0]["name"] == "Delhi" and trip["place"]["away"]["abroad"]
    assert client.post("/api/p/t/places/prefs", json={"km": 0}).status_code == 400
    assert client.post("/api/p/t/places/prefs", json={"homes": [999]}).status_code == 400
    # Set a place on the trip: its photos are Larnaca's now, and trips are found again.
    r = client.post(f"/api/p/t/events/{trip['id']}/place", json={"place_id": 8}).json()
    assert r["n"] == 8
    settled()
    one = client.get(f"/api/p/t/events/{trip['id']}").json()["event"]
    assert one["place"]["name"] == "Larnaca" and one["place"]["src"] == "manual"
    places = client.get("/api/p/t/places").json()
    assert places["manual"] == 8 and places["map_tiles"] is True


def test_a_trip_never_takes_an_event_s_id(towns):
    """New events and a new trip found in one go each get an id of their own (the trip used to be
    given one just handed to an event, and replaced it)."""
    from joeylab import events

    _, lib = towns
    shoot(lib, "Lisbon", "2024:05:01", seed=0)
    shoot(lib, "Lisbon", "2024:05:02", seed=10)
    add_root(lib)
    conn, _ = run_all()
    set_homes(conn, "Delhi")
    for _ in range(2):
        detect(conn)
        evs, trips = events.listing(conn, "event"), events.listing(conn, "trip")
        assert len(evs) == 1 and len(trips) == 1 and evs[0]["parent_id"] == trips[0]["id"] != evs[0]["id"]
