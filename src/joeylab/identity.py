"""Who "me" is: the person in your libraries who is the one using JoeyLab (it has one user), kept by
name in JoeyLab's settings.json, so the same person in another library is "me" there too."""

from __future__ import annotations

from . import prefs


def _key(viewer: str | None = None) -> str:
    return "owner"                      # one user per install


def name_of(viewer: str | None) -> str | None:
    return ((prefs.load().get("me") or {}).get(_key(viewer))) or None


def set_name(viewer: str | None, name: str | None) -> None:
    me = dict(prefs.load().get("me") or {})
    if name:
        me[_key(viewer)] = name.strip()
    else:
        me.pop(_key(viewer), None)
    prefs.update(me=me)


def person_id(conn, viewer: str | None) -> int | None:
    """This library's person who is you, if you've said so (and they're in this library, by that
    name or as one of their other names)."""
    from . import aliases

    name = name_of(viewer)
    if not name:
        return None
    r = aliases.find(conn, name)
    return r[0] if r else None


def renamed(old: str, new: str) -> None:
    """A person was renamed: whoever had them as "me" keeps them."""
    me = dict(prefs.load().get("me") or {})
    changed = {k: new for k, v in me.items() if v and v.lower() == (old or "").lower()}
    if changed:
        me.update(changed)
        prefs.update(me=me)


def fold_into_owner(login: str | None) -> int:
    """Earlier versions kept favourites (and "me") per tailnet account, so the owner's own phone had a
    set apart from the Mac's. Fold the owner's account into the one set. Returns rows moved."""
    if not login:
        return 0
    from . import config, db

    login = login.strip().lower()
    me = dict(prefs.load().get("me") or {})
    if login in me and "owner" not in me:
        me["owner"] = me.pop(login)
        prefs.update(me=me)
    moved = 0
    for name in config.list_projects():
        try:
            conn = db.connect(name, create=False)
        except Exception:
            continue
        try:
            rows = conn.execute("SELECT shot_id, fav, updated FROM viewer_decisions WHERE viewer=?", (login,)).fetchall()
            for sid, fav, updated in rows:
                conn.execute("INSERT INTO viewer_decisions(viewer, shot_id, fav, updated) VALUES ('owner', ?, ?, ?)"
                             " ON CONFLICT(viewer, shot_id) DO UPDATE SET fav=MAX(fav, excluded.fav)", (sid, fav, updated))
            conn.execute("DELETE FROM viewer_decisions WHERE viewer=?", (login,))
            conn.commit()
            moved += len(rows)
        finally:
            conn.close()
    return moved
