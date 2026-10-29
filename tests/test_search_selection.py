"""The Library showing a picture search's matches (`q`), and its summary of whatever it shows."""

from conftest import synthetic_library


def test_library_shows_a_picture_search_s_matches_and_sums_them_up(env, monkeypatch):
    from fastapi.testclient import TestClient

    from joeylab.library import Filter
    from joeylab.web.routes import ai as ai_routes
    from joeylab.web.server import create_app

    tmp, lib = env
    found = synthetic_library(lib)
    wed = sorted(found["wed"])
    asked = []
    monkeypatch.setattr(ai_routes, "picture_match_ids", lambda conn, text: asked.append(text) or (wed if text == "dance" else []))
    c = TestClient(create_app())

    def ids(params):
        r = c.get("/api/p/t/shots", params={"limit": 1000, **params}).json()
        return {x["id"] for x in r["rows"]}, r["total"]

    by_ids, n = ids({"ids": ",".join(map(str, wed))})
    shown, total = ids({"q": "dance"})
    assert shown == by_ids and total == n and n > 0          # exactly the search's matches, as the browsing rules show them
    assert ids({"q": "nothing like it"}) == (set(), 0)
    folder = c.get("/api/p/t/rows", params={"ids": str(wed[0])}).json()["rows"][0]["dir"]
    inside, _ = ids({"q": "dance", "folder": folder})
    assert inside and inside <= shown                          # with other filters: both apply

    s = c.get("/api/p/t/summary", params={"q": "dance"}).json()
    assert s["n"] == total and sum(r["n"] for r in s["when"]) + s["undated"] == total
    assert s["unit"] in ("year", "month") and s["folders"] and s["folders"][0]["n"] <= total
    everything = c.get("/api/p/t/summary").json()
    assert everything["n"] > s["n"]

    # The other places a Library filter goes: the months, Explore and a smart album's count.
    months = c.get("/api/p/t/timeline", params={"q": "dance"}).json()["months"]
    assert sum(m["n"] for m in months) == total
    assert set(c.get("/api/p/t/explore", params={"q": "dance"}).json()["shown"]) == shown

    # A search the server hasn't turned into its matches shows nothing, never everything.
    w, a = Filter(search="dance").sql()
    from joeylab import db, library
    conn = db.connect("t")
    assert conn.execute(f"SELECT COUNT(*) {library.LIGHT_FROM}{w}", a).fetchone()[0] == 0
    # The search page's own `q` is its query, not a Library filter on top of it.
    asked.clear()
    c.get("/api/p/t/search", params={"q": "status:picks"})
    assert asked == []
