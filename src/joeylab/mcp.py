"""JoeyLab as an MCP server (stdio), so an AI assistant (any MCP client, e.g. Codex or Claude Code)
can work with your library. Settings › AI assistants shows the exact command; with JoeyLab on your
PATH it is:

    codex mcp add joeylab -- joeylab mcp
    claude mcp add joeylab -- joeylab mcp

Tools read the catalog (search, people, events, places, health, one photo's details and a
thumbnail to look at). With changes allowed (the default), the assistant can also set picks, stars
and color labels and make albums. Every change is logged under the assistant's name as one step
you can undo in the app or with `joeylab undo`. Nothing here ever touches your photo files.
JoeyLab itself never calls an AI service; the assistant you choose does the thinking.
"""

from __future__ import annotations

import base64
import json
import sys

from . import __version__, config, db, health, library

PROTOCOL = "2025-06-18"
CLIENT = {"name": "Assistant"}     # who is connected, from the initialize handshake; labels its changes


def client_label(info: dict | None) -> str:
    name = str((info or {}).get("name") or "").strip()
    low = name.lower()
    if "codex" in low:
        return "Codex"
    if "claude" in low:
        return "Claude"
    return name[:40] or "Assistant"


MAX_DECISIONS = 500      # photos one set_decisions call may change: a slip can't reject a whole library


def _tools(allow_changes: bool) -> list[dict]:
    lib = {"type": "string", "description": "Library name (see list_libraries). Optional when there is only one."}
    ids = {"type": "array", "items": {"type": "integer"}, "description": "Photo ids from a search"}
    tools = [
        {"name": "list_libraries", "description": "The photo libraries JoeyLab knows, with photo counts and folders.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "search_photos", "description":
            "Find photos. Accepts plain language ('Alex laughing at the beach in 2025', 'best photos of Mom') and field "
            "filters: person:\"Name\" place:Name event:Name is:pick|reject|undecided|video rating:>3 q:>70 date:2024-06 "
            "camera:x100 label:red flag:blurry -flag:tiny. Returns ids, dates, people, places and scores, best match first.",
         "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "library": lib,
                                                          "limit": {"type": "integer", "default": 30}},
                         "required": ["query"]}},
        {"name": "photo_details", "description": "Everything known about one photo: file, date, camera, place, event, "
                                                  "people, quality, flags, why it is suggested or not, and its backups "
                                                  "(byte-identical copies on other drives, in any library).",
         "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "library": lib}, "required": ["id"]}},
        {"name": "view_photo", "description": "Look at a photo (a 768 px JPEG from the catalog; the original is never read).",
         "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "library": lib}, "required": ["id"]}},
        {"name": "people", "description": "Named people (and pets) with how many photos and picks each has, and the other "
                                          "names they go by (also_called: 'Dad' may also be 'Papa'; search finds them by any). "
                                          "The one marked is_the_user is the person you're talking to (\"me\" in their requests). "
                                          "Give a name to look up one person by any of their names.",
         "inputSchema": {"type": "object", "properties": {"library": lib, "name": {"type": "string"}}}},
        {"name": "events", "description": "Events and trips found from time and place, newest first.",
         "inputSchema": {"type": "object", "properties": {"library": lib, "kind": {"type": "string", "enum": ["event", "trip"]},
                                                          "year": {"type": "integer"}}}},
        {"name": "places", "description": "Places photos were taken, with counts.",
         "inputSchema": {"type": "object", "properties": {"library": lib}}},
        {"name": "library_health", "description": "Duplicates, photos only on one drive, unreadable, undated, and other tidying.",
         "inputSchema": {"type": "object", "properties": {"library": lib}}},
        {"name": "keywords", "description": "The user's keyword tree (e.g. 'Activities › Climbing') with photo counts. "
                                            "Search a keyword with #Name or keyword:\"A › B\" in search_photos.",
         "inputSchema": {"type": "object", "properties": {"library": lib}}},
    ]
    if allow_changes:
        tools += [
            {"name": "set_decisions", "description": f"Pick, reject, star (0-5) or color-label photos, up to {MAX_DECISIONS} "
                                                 "per call. One undoable step.",
             "inputSchema": {"type": "object", "properties": {
                 "library": lib, "ids": ids, "rating": {"type": "integer", "minimum": 0, "maximum": 5},
                 "pick": {"type": "integer", "enum": [-1, 0, 1], "description": "1 pick, -1 reject, 0 clear"},
                 "label": {"type": "string", "enum": ["Red", "Yellow", "Green", "Blue", "Purple", ""]},
                 "why": {"type": "string", "description": "A few words shown in the history"}}, "required": ["ids"]}},
            {"name": "create_album", "description": "Make a JoeyLab album (in the catalog only) from photo ids.",
             "inputSchema": {"type": "object", "properties": {"library": lib, "name": {"type": "string"}, "ids": ids},
                             "required": ["name", "ids"]}},
            {"name": "tag_photos", "description": "Add or remove keywords on photos. Keywords are paths like "
                                              "'Activities › Climbing'; new ones are made. One undoable step.",
             "inputSchema": {"type": "object", "properties": {
                 "library": lib, "ids": ids, "add": {"type": "array", "items": {"type": "string"}},
                 "remove": {"type": "array", "items": {"type": "string"}}}, "required": ["ids"]}},
            {"name": "undo_last", "description": "Undo your own most recent change (picks, stars, labels or keywords).",
             "inputSchema": {"type": "object", "properties": {"library": lib}}},
        ]
    return tools


def _project(args: dict, default: str | None) -> str:
    name = args.get("library") or default
    projects = config.list_projects()
    if not name:
        if len(projects) == 1:
            return projects[0]
        raise ValueError(f"Which library? One of: {', '.join(projects)}")
    if name not in projects:
        raise ValueError(f"No library called {name!r}. Libraries: {', '.join(projects)}")
    return name


def _brief(conn, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        people = [x[0] for x in conn.execute(
            "SELECT DISTINCT p.name FROM faces f JOIN people p ON p.id=f.person_id WHERE f.shot_id=? AND COALESCE(p.hidden,0)=0",
            (r["id"],))]
        place = conn.execute("SELECT name, country FROM places WHERE id=?", (r.get("place_id"),)).fetchone() if r.get("place_id") else None
        ev = conn.execute("SELECT name FROM events WHERE id=?", (r.get("event_id"),)).fetchone() if r.get("event_id") else None
        out.append({"id": r["id"], "name": r["stem"], "taken": r.get("capture_str"), "camera": r.get("camera"),
                    "quality": round(r["quality"]) if r.get("quality") is not None else None,
                    "rating": r.get("rating") or 0, "pick": r.get("pick") or 0, "label": r.get("label"),
                    "people": people, "place": ", ".join(x for x in place if x) if place else None,
                    "event": ev[0] if ev else None, "flags": r.get("flags") or [], "media": r.get("media") or "photo",
                    "match": r.get("level")})
    return out


def _backups(conn, project: str, shot_id: int) -> dict:
    """Where else this photo's files are: byte-identical copies on other drives (backups), checked by
    their full content hash or only probably the same (same size and fingerprint), or none."""
    import time as _time

    from . import copies

    try:
        b = copies.for_shot(conn, project, shot_id)
    except Exception:
        return {"note": "The index of copies on other drives can't be read right now."}
    if not b["known"]:
        return {"note": "Not known yet: this library's files go into the index of copies at its next update."}
    out = []
    for f in b["files"]:
        where = [{"drive": c["drive_name"], "path": c["rel"], "library": c["library"], "photo_id": c["shot_id"],
                  "checked_byte_for_byte": c["checked"], "connected": c["connected"],
                  "last_seen": _time.strftime("%Y-%m-%d", _time.localtime(c["last_seen"])) if c["last_seen"] else None}
                 for c in f["backups"]]
        out.append({"file": f["name"], "backed_up_on": where} if where else
                   {"file": f["name"], "backed_up_on": [], "note": "Only copy: not on any other drive JoeyLab knows."})
    return {"files": out, "all_backed_up": b["all_backed_up"]}


def call(name: str, args: dict, default: str | None, allow_changes: bool):
    if name == "list_libraries":
        out = []
        for p in config.list_projects():
            c = db.connect(p, create=False)
            try:
                out.append({"name": p, "photos": c.execute("SELECT COUNT(*) FROM shots WHERE status='present'").fetchone()[0],
                            "folders": [r[0] for r in c.execute("SELECT path FROM roots")]})
            finally:
                c.close()
        return out
    project = _project(args, default)
    conn = db.connect(project, create=False)
    try:
        if name == "search_photos":
            from . import query as q_mod
            from .web.deps import filter_from
            from .web.routes.library import browse_filter

            parsed = q_mod.parse(args["query"], conn)
            flt = browse_filter(conn, filter_from({k: str(v) for k, v in parsed["filter"].items()}))
            limit = max(1, min(int(args.get("limit") or 30), 200))
            if parsed["text"]:
                from . import search

                if not (search.available() and search.indexed_count(conn)):
                    raise ValueError("Picture search isn't available (the AI index hasn't been built).")
                restrict = None
                if parsed["filter"]:
                    w, a = flt.sql()
                    restrict = {r[0] for r in conn.execute(f"SELECT s.id {library.LIGHT_FROM}{w}", a)}
                hits = search.search(conn, parsed["text"], limit, restrict=restrict)
                rows = {r["id"]: r for r in library.rows_by_ids(conn, [h["id"] for h in hits])}
                res = [rows[h["id"]] | {"level": h["level"]} for h in hits if h["id"] in rows and h["level"] != "unlikely"]
            else:
                res, _ = library.page(conn, flt, sort=parsed["sort"] or "quality", limit=limit)
            return {"understood": parsed["chips"], "looking_for": parsed["text"], "photos": _brief(conn, res)}
        if name == "photo_details":
            from . import scoring

            s = library.shot(conn, int(args["id"]))
            if not s:
                raise ValueError("No such photo.")
            d = _brief(conn, library.rows_by_ids(conn, [s["id"]]))[0]
            d.update(files=[f["path"] for f in library.files_of(conn, s["id"])], lens=s.get("lens"),
                     exposure=library.describe(s), gps=[s["gps_lat"], s["gps_lon"]] if s.get("gps_lat") else None,
                     why=[w["text"] for w in scoring.explain(conn, s["id"])], backups=_backups(conn, project, s["id"]))
            return d
        if name == "view_photo":
            r = conn.execute("SELECT thumb, stem FROM shots WHERE id=?", (int(args["id"]),)).fetchone()
            if not r or not r[0]:
                raise ValueError("That photo hasn't been analysed yet.")
            with open(r[0], "rb") as f:
                return {"__image__": base64.standard_b64encode(f.read()).decode("ascii"), "text": r[1]}
        if name == "people":
            from . import aliases, faces, identity

            rows = [dict(r) for r in conn.execute(
                "SELECT p.id, p.name, COALESCE(p.kind,'person') AS kind, COUNT(DISTINCT f.shot_id) AS photos,"
                " COUNT(DISTINCT CASE WHEN d.pick=1 THEN f.shot_id END) AS picks FROM people p JOIN faces f ON f.person_id=p.id"
                " LEFT JOIN decisions d ON d.shot_id=f.shot_id WHERE COALESCE(p.hidden,0)=0 GROUP BY p.id ORDER BY photos DESC")]
            if not rows and not faces.enabled():
                return {"people": [], "note": "Face recognition is off on this Mac, so nobody is named yet. The owner "
                                              "can turn it on (choosing its models after reading their licences) with: "
                                              "joeylab faces enable"}
            if args.get("name"):                            # one person, by any of their names
                who = aliases.find(conn, str(args["name"]), prefix=True)
                rows = [r for r in rows if who and r["id"] == who[0]]
                if not rows:
                    return {"people": [], "note": f"Nobody in this library is called {args['name']!r}."}
            me = identity.person_id(conn, None)             # the person who is the user (the Mac's owner)
            others = aliases.everyone(conn)
            for r in rows:
                pid = r.pop("id")
                if others.get(pid):
                    r["also_called"] = others[pid]
                if me is not None and pid == me:
                    r["is_the_user"] = True
            return rows
        if name == "events":
            from . import events as ev_mod

            return [{"id": e["id"], "name": e["name"], "kind": e["kind"], "photos": e["n"],
                     "places": [p for p, _ in e["places"][:3]]} for e in ev_mod.listing(conn, args.get("kind"), args.get("year"))[:200]]
        if name == "places":
            from . import geo

            return [{"place": f"{p['name']}, {p['country']}", "photos": p["n"]} for p in geo.summary(conn)[:200]]
        if name == "library_health":
            h = health.report(conn, project)
            return {k: h[k] for k in ("duplicates", "missing", "undated", "tiny", "utility", "to_sort", "drives", "untouched")}
        if name == "keywords":
            from . import keywords

            return [{"path": k["path"], "photos": k["total"]} for k in keywords.tree(conn)]
        if not allow_changes:
            raise ValueError("This JoeyLab server is read-only.")
        if name == "tag_photos":
            from . import keywords

            add = [keywords.ensure(conn, k) for k in args.get("add") or [] if keywords.split_path(k)]
            remove = [k for k in (keywords.find(conn, x) for x in args.get("remove") or []) if k]
            label = ", ".join([keywords.path_of(conn, k)[-1] for k in add] + [f"−{keywords.path_of(conn, k)[-1]}" for k in remove])
            _, changed = keywords.assign(conn, [int(i) for i in args["ids"]], add=add, remove=remove,
                                         what=f"{CLIENT['name']}: keywords {label}"[:60], source="mcp")
            return {"changed": len(changed), "undo": "Use undo_last, the app's Undo, or `joeylab undo`."}
        if name == "set_decisions":
            if len(args["ids"]) > MAX_DECISIONS:
                raise ValueError(f"That's {len(args['ids'])} photos; set_decisions changes at most {MAX_DECISIONS} at a time. "
                                 "Ask the user before changing more, then send them in smaller groups.")
            items = [{"id": int(i), "rating": args.get("rating"), "pick": args.get("pick"),
                      "label": args.get("label")} for i in args["ids"]]
            _, changed = library.decide(conn, items, what=f"{CLIENT['name']}: {args.get('why') or 'change'}"[:60], source="mcp")
            return {"changed": len(changed), "undo": "Use undo_last, the app's Undo, or `joeylab undo`."}
        if name == "create_album":
            import time

            from . import portable

            aid = db.new_id(conn, "albums")
            conn.execute("INSERT INTO albums(id, name, kind, description, created, updated) VALUES (?,?,?,?,?,?)",
                         (aid, args["name"][:120], "manual", f"Made by {CLIENT['name']}", time.time(), time.time()))
            conn.commit()
            return {"album": aid, "added": portable.add_to_album(conn, aid, [int(i) for i in args["ids"]])}
        if name == "undo_last":
            res = library.undo(conn, source="mcp")      # only the assistant's own changes
            return {"undone": res["label"] if res else None,
                    "kept_newer_changes": res["skipped"] if res else 0}
        raise ValueError(f"Unknown tool {name}")
    finally:
        conn.close()


def _reply(id_, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": id_}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")


def handle(msg, default_project: str | None, allow_changes: bool) -> dict | None:
    """One JSON-RPC message in, the reply out (None for notifications)."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Each message must be one JSON-RPC object."}}
    method, id_ = msg.get("method"), msg.get("id")
    if id_ is None:
        return None                          # notifications (initialized, cancelled) need no answer
    ok = lambda result: {"jsonrpc": "2.0", "id": id_, "result": result}
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    try:
        if method == "initialize":
            asked = params.get("protocolVersion")
            CLIENT["name"] = client_label(params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else None)
            return ok({"protocolVersion": asked if asked in SUPPORTED else PROTOCOL, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "joeylab", "title": "JoeyLab by Yaduraj Bhageria", "version": __version__},
                       "instructions": "JoeyLab is the user's local photo library. Search before deciding; "
                                       "decisions are logged and undoable; photo files are never changed."})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": _tools(allow_changes)})
        if method == "tools/call":
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            try:
                res = call(params.get("name"), args, default_project, allow_changes)
            except Exception as e:           # any failure of a tool is reported to the assistant, not the protocol
                msg_ = str(e) if isinstance(e, (ValueError, KeyError, db.CatalogDamaged)) else f"{type(e).__name__}: {e}"
                return ok({"content": [{"type": "text", "text": msg_}], "isError": True})
            if isinstance(res, dict) and "__image__" in res:
                content = [{"type": "image", "data": res["__image__"], "mimeType": "image/jpeg"},
                           {"type": "text", "text": res.get("text") or ""}]
            else:
                content = [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=1)}]
            return ok({"content": content})
        return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}


def serve(default_project: str | None = None, allow_changes: bool = True) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _reply(None, error={"code": -32700, "message": "Parse error"})
            continue
        out = handle(msg, default_project, allow_changes)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
