"""Shared pieces of the HTTP API: catalog connections, filters from query strings, request
guards (who may connect, who may write, same-origin checks) and the viewer's identity."""

from __future__ import annotations

import ipaddress
from typing import Any, Mapping

from fastapi import HTTPException, Request

from .. import db
from ..library import Filter
from . import net


def _int(v, default=None):
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _float(v, default=None):
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _bool(v):
    if v in (None, ""):
        return None
    return str(v).lower() in ("1", "true", "yes", "on")


def ids_of(v) -> list[int] | None:
    """Ids from "1,2,3" or a JSON list. Anything that isn't a number is a 400, not a 500."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, list):
            return [int(x) for x in v]
        return [int(x) for x in str(v).split(",") if x.strip()]
    except (TypeError, ValueError):
        raise HTTPException(400, "Photo ids must be numbers.")


FILTER_KEYS = ("status", "min_rating", "suggested", "flag", "no_flag", "min_q", "max_q", "camera", "lens", "min_iso",
               "max_iso", "min_focal", "max_focal", "max_f", "folder", "folder_like", "from", "to", "ids", "person", "people", "cluster",
               "album", "event", "trip", "place", "country", "gps", "media", "label", "edited", "undated", "favs", "c2pa",
               "keyword", "not_keyword", "untagged", "no_album", "filetype", "month", "step", "raw")


def filter_from(q: Mapping[str, Any], viewer: str | None = None) -> Filter:
    status = q.get("status") or "all"
    min_rating = _int(q.get("min_rating"))
    if status == "rated" and not min_rating:
        min_rating = 1
    favs = q.get("favs")
    return Filter(
        picks=status == "picks", rejects=status == "rejects", undecided=status == "undecided",
        min_rating=min_rating, suggestion=q.get("suggested") or None, flag=q.get("flag") or None,
        no_flag=q.get("no_flag") or None,
        min_quality=_float(q.get("min_q")), max_quality=_float(q.get("max_q")),
        camera=q.get("camera") or None, lens=q.get("lens") or None,
        min_iso=_float(q.get("min_iso")), max_iso=_float(q.get("max_iso")),
        min_focal=_float(q.get("min_focal")), max_focal=_float(q.get("max_focal")), max_aperture=_float(q.get("max_f")),
        dir_prefix=q.get("folder") or None, path_contains=q.get("folder_like") or None,
        date_from=q.get("from") or None, date_to=q.get("to") or None, ids=ids_of(q.get("ids")),
        person_id=_int(q.get("person")), people_all=ids_of(q.get("people")), cluster=_int(q.get("cluster")),
        album_id=_int(q.get("album")), event_id=_int(q.get("event")), trip_id=_int(q.get("trip")), place_id=_int(q.get("place")),
        country=q.get("country") or None, has_gps=_bool(q.get("gps")), media=q.get("media") or None,
        label=q.get("label") or None, edited=_bool(q.get("edited")), undated=_bool(q.get("undated")),
        favs_of=(viewer or "owner") if favs in ("me", "1", "true") else (favs or None), c2pa=_bool(q.get("c2pa")),
        keyword=_keywords(q.get("keyword")), not_keyword=ids_of(q.get("not_keyword")), untagged=_bool(q.get("untagged")),
        no_album=_bool(q.get("no_album")), file_kind=q.get("filetype") or None, month=_int(q.get("month")),
        step=q.get("step") or None, raw=q.get("raw") or None, backup=q.get("backup") or None,
    )


def _keywords(v) -> int | list[int] | None:
    """One keyword ("12"), or several that must all be there ("12,40")."""
    ids = ids_of(v)
    return None if not ids else ids[0] if len(ids) == 1 else ids


def conn_dep(p: str):
    try:
        conn = db.connect(p, create=False)
    except FileNotFoundError:
        raise HTTPException(404, f"There's no library called {p!r}.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except db.CatalogDamaged as e:
        raise HTTPException(503, str(e))
    try:
        yield conn
    finally:
        conn.close()


def one(conn, q: str, args=()) -> Any:
    return conn.execute(q, args).fetchone()[0]


def big_json(content: dict):
    """A large answer (every photo of a big library) sent as it is: FastAPI otherwise walks every value
    first (jsonable_encoder), which took 160 ms for Explore's 75,000 photos. Only for plain JSON values."""
    from fastapi.responses import JSONResponse

    return JSONResponse(content)


def cap(value, hi: int, lo: int = 1) -> int:
    """A count from a request, kept between lo and hi (a negative limit would mean "everything")."""
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return lo


# ---------------------------------------------------------------- who is asking
#
# JoeyLab has one user: its owner, at the Mac itself or on a device signed in to Tailscale with the
# Mac's own account. Nobody else can open it; other people see photos only through share links.

_PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "tailscale-user-login")
PRIVATE = ("This JoeyLab is private to its owner. Ask them for a share link to see their photos.")


def _loopback_peer(request: Request) -> bool:
    client = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(client.split("%")[0])
    except ValueError:
        return client in ("testclient", "localhost")       # FastAPI's TestClient
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def forwarded(request: Request) -> bool:
    """Came through `tailscale serve`: a connection from this Mac carrying X-Forwarded-For."""
    return _loopback_peer(request) and bool(request.headers.get("x-forwarded-for"))


def tailnet_login(request: Request) -> str | None:
    """The Tailscale account a request came from, from the identity header `tailscale serve` adds, or
    None. The header is believed only on a connection that `tailscale serve` forwarded from this Mac:
    anyone connecting directly could write it. A server that also listens on other addresses (`--host`)
    believes it never."""
    if not getattr(request.app.state, "trust_identity", True) or not forwarded(request):
        return None
    login = request.headers.get("tailscale-user-login")
    return login.strip().lower() if login else None


def viewer_of(request: Request) -> str | None:
    """Whose favourites, notes and "me" these are. One user, so always the owner's (None)."""
    return None


def at_the_mac(request: Request) -> bool:
    """A request made on this Mac itself: from a loopback address and not forwarded by a proxy
    (`tailscale serve` adds X-Forwarded-For, and a Tailscale identity for tailnet users)."""
    if any(request.headers.get(h) for h in _PROXY_HEADERS):
        return False
    return _loopback_peer(request)


def is_owner(request: Request) -> bool:
    """Someone at the Mac itself, on a device signed in to Tailscale as the Mac's own account, or (with
    `--host` and `--token`) someone who opened the link carrying the access token."""
    if at_the_mac(request) or getattr(request.state, "token_ok", False):
        return True
    owner = getattr(request.app.state, "owner", None)
    who = tailnet_login(request)
    return bool(owner and who and who == owner)


def is_writer(request: Request) -> bool:
    return is_owner(request)


def require_writer(request: Request) -> None:
    if not is_owner(request):
        raise HTTPException(403, PRIVATE)


def require_owner(request: Request, doing: str = "move files to the Trash") -> None:
    if not is_owner(request):
        raise HTTPException(403, PRIVATE)


# ---------------------------------------------------------------- request guard

SAFE = {"GET", "HEAD", "OPTIONS"}


def host_name(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v.startswith("["):
        return v[1 : v.find("]")]
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def host_allowed(host: str, extra: set[str]) -> bool:
    """Only names that can really mean this Mac (localhost, its own tailnet name, its addresses):
    blocks DNS rebinding, where a web page on some other site points its own name at 127.0.0.1 to
    read the library. Not any *.ts.net name: another tailnet's machine isn't this Mac."""
    if host in ("localhost", "127.0.0.1", "::1", "testserver") or host in extra:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip in net.TAILSCALE_V4 or ip in net.TAILSCALE_V6


def guard_problem(request: Request, extra_hosts: set[str], public: bool = False) -> tuple[int, str] | None:
    """Why this request must be refused, or None. Writes must come from JoeyLab's own pages:
    a page on another site can send a form or a "simple" fetch to localhost, but its Origin
    (or Sec-Fetch-Site) gives it away, and it can't send application/json without asking first.
    `public`: the share-link server, which may be published under any name, even to the internet."""
    host = host_name(request.headers.get("host"))
    if not public and not host_allowed(host, extra_hosts):
        return 421, "JoeyLab only answers to this Mac's own names."
    if not public and request.headers.get("tailscale-funnel-request"):
        return 403, "JoeyLab isn't for the open internet: it only answers on your tailnet."
    if request.method in SAFE:
        return None
    origin = request.headers.get("origin")
    if origin and origin != "null":
        o = host_name(origin.split("://", 1)[-1].split("/", 1)[0])
        fwd = host_name(request.headers.get("x-forwarded-host"))
        if o not in (host, fwd):
            return 403, "Changes can only be made from JoeyLab's own pages."
    elif request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        return 403, "Changes can only be made from JoeyLab's own pages."
    length = request.headers.get("content-length")
    has_body = (length and length != "0") or request.headers.get("transfer-encoding")
    is_json = (request.headers.get("content-type") or "").startswith("application/json")
    if has_body and not is_json:
        return 415, "JoeyLab's API only accepts JSON."
    # From a browser, a change must be JSON or carry JoeyLab's header. Both make a browser ask
    # before sending from any other page (another site, or another port of this Mac), and JoeyLab
    # never agrees, so only its own pages can change anything. Tools that aren't browsers (curl,
    # the CLI) send neither Origin nor Sec-Fetch headers and aren't affected.
    from_browser = bool(origin) or request.headers.get("sec-fetch-site") is not None or request.headers.get("sec-fetch-mode") is not None
    if from_browser and not is_json and request.headers.get("x-joeylab") != "1":
        return 403, "Changes can only be made from JoeyLab's own pages."
    return None
