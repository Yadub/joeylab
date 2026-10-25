"""JoeyLab's own files are private; what it writes for you elsewhere isn't. Exported galleries can't be
broken out of; sidecar restores trust nothing but their own backups; an assistant's (or someone
else's) rejects don't go to the owner's Trash; one assistant call can't change a whole library."""

from __future__ import annotations

import json
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient

from conftest import add_root, make_jpeg, run_all


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_its_folder_and_files_are_this_accounts_alone(env, monkeypatch):
    tmp, lib = env
    from joeylab import backup, cleanup, config, db

    assert not config.HOME.exists()
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    conn, _ = run_all()
    conn.execute("INSERT INTO settings(key, value) VALUES ('x', '1')")
    conn.commit()
    assert mode(config.HOME) == 0o700
    cat = config.project_dir("t") / "catalog.db"
    for f in (cat, f"{cat}-wal", f"{cat}-shm"):
        assert mode(f) == 0o600, f
    assert mode(backup.backup("t", force=True)) == 0o600
    assert mode(cleanup._log_path("t-files")) == 0o600
    from joeylab import service

    monkeypatch.setattr(service, "LOG_DIR", config.HOME / "logs")      # read at import, before this test's home
    service.prepare_logs()
    for name in ("serve.log", "serve.err.log"):
        assert mode(service.LOG_DIR / name) == 0o600
    # The process's umask is left alone: what JoeyLab writes for you elsewhere keeps its usual permissions.
    um = os.umask(0o022)
    os.umask(um)
    assert um != 0o077


def test_an_older_folder_is_tightened_at_start_up(env):
    tmp, lib = env
    from joeylab import config, db

    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    run_all()[0].close()
    old = [config.project_dir("t") / "catalog.db", config.HOME / "backups" / "t" / "catalog-20240101.db",
           config.HOME / "logs" / "serve.log", config.HOME / "trash-log" / "t-files-20240101.tsv", config.HOME / "taste.json"]
    for f in old:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()
        os.chmod(f, 0o644)
    os.chmod(config.HOME, 0o755)
    config._SECURED.clear()                            # as in a new process
    db.connect("t", create=False).close()
    assert mode(config.HOME) == 0o700
    for f in old:
        assert mode(f) == 0o600, f


def test_gallery_titles_and_names_cant_break_out_of_the_script(env, tmp_path):
    _, lib = env
    make_jpeg(lib / "IMG_<!--&>.JPG", seed=1)     # a file name can't hold "/", but these it can
    add_root(lib)
    conn, _ = run_all()
    from joeylab import library, portable

    title = '</script><script>alert("hi")</script><!--'
    rows = library.shots(conn)
    out = tmp_path / "gallery"
    portable.export_gallery(conn, library.Filter(), out, title, lambda r: conn.execute(
        "SELECT thumb FROM shots WHERE id=?", (r["id"],)).fetchone()[0])
    page = (out / "index.html").read_text()
    script = page.split("<script>", 1)[1]
    assert script.count("</script>") == 1 and "<!--" not in script and "<script" not in script
    photos = json.loads(script.split("const PHOTOS=", 1)[1].split(";const KEY=", 1)[0])
    assert [p["name"] for p in photos] == [rows[0]["stem"]] == ["IMG_<!--&>"]
    key = json.loads(script.split('const KEY="fav-"+', 1)[1].split(";let fav", 1)[0])
    assert key.startswith(title)
    assert mode(out / "index.html") == 0o666 & ~_umask()             # a gallery isn't made private


def _umask() -> int:
    um = os.umask(0o022)
    os.umask(um)
    return um


def test_sidecar_restore_survives_odd_names_and_trusts_only_its_own_backups(env, tmp_path):
    from joeylab import config, export

    config.secure_home()
    photos = tmp_path / "photos"
    photos.mkdir()
    odd = photos / "IMG\t1\nsecond line.xmp"
    plain = photos / "IMG_2.xmp"
    for f in (odd, plain):
        f.write_text("before")
    backup = config.HOME / "xmp-backups" / "t" / "20260101-000000"
    export._backup_sidecar(str(odd), backup)
    export._backup_sidecar(str(plain), backup)
    assert mode(backup / "index.jsonl") == 0o600
    for f in (odd, plain):
        f.write_text("after")
    # Entries no backup of JoeyLab's would hold: a file that isn't a sidecar, a name that doesn't
    # match its path, a relative path.
    victim = tmp_path / "notes.txt"
    victim.write_text("mine")
    (backup / "evil.txt").write_text("overwritten")
    with open(backup / "index.jsonl", "a") as fh:
        for entry in ({"backup": "evil.txt", "sidecar": str(victim)},
                      {"backup": export._backup_name(str(plain)), "sidecar": str(photos / "other.xmp")},
                      {"backup": "evil.txt", "sidecar": "relative.xmp"}):
            fh.write(json.dumps(entry) + "\n")
        fh.write("not json\n")
    assert export.restore_sidecars(backup) == 2
    assert odd.read_text() == "before" and plain.read_text() == "before"
    assert victim.read_text() == "mine" and not (photos / "other.xmp").exists()


def test_sidecar_backups_from_before_still_restore(env, tmp_path):
    from joeylab import config, export

    side = tmp_path / "IMG_3.xmp"
    side.write_text("after")
    backup = config.HOME / "xmp-backups" / "t" / "20250101-000000"
    backup.mkdir(parents=True)
    name = export._backup_name(str(side))
    (backup / name).write_text("before")
    (backup / "index.tsv").write_text(f"{name}\t{side}\nbroken line without a tab\n")
    assert export.restore_sidecars(backup) == 1 and side.read_text() == "before"


def test_an_assistant_changes_at_most_500_photos_per_call(env):
    _, lib = env
    make_jpeg(lib / "IMG_1.JPG", seed=1)
    add_root(lib)
    conn, _ = run_all()
    sid = conn.execute("SELECT id FROM shots").fetchone()[0]
    from joeylab import mcp

    with pytest.raises(ValueError, match="at most 500"):
        mcp.call("set_decisions", {"ids": [sid] * 501, "pick": -1}, "t", True)
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert mcp.call("set_decisions", {"ids": [sid] * 500, "rating": 2}, "t", True)["changed"] == 1
    tool = next(t for t in mcp._tools(True) if t["name"] == "set_decisions")
    assert "500" in tool["description"]
    reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "set_decisions", "arguments": {"ids": list(range(600)), "pick": -1}}}, "t", True)
    assert reply["result"]["isError"] is True and "500" in reply["result"]["content"][0]["text"]


def _wait(c, url):
    for _ in range(200):
        j = c.get(url).json()
        if j["state"] != "checking":
            return j
        time.sleep(0.05)
    raise AssertionError("never finished")


def test_the_trash_takes_only_the_owners_own_rejects(env):
    _, lib = env
    for i in range(6):
        make_jpeg(lib / f"IMG_{i}.JPG", seed=i)
    add_root(lib)
    conn, _ = run_all()
    sid = {r["stem"]: r["id"] for r in conn.execute("SELECT id, stem FROM shots")}
    from joeylab import library
    from joeylab.web.server import create_app

    library.decide(conn, [{"id": sid["IMG_0"], "pick": -1}], source="web")                       # at the Mac
    library.decide(conn, [{"id": sid["IMG_1"], "pick": -1}], source="web", viewer="me@example.com")   # the owner's phone
    library.decide(conn, [{"id": sid["IMG_2"], "pick": -1}], source="mcp")                       # an assistant
    library.decide(conn, [{"id": sid["IMG_3"], "pick": -1}], source="web", viewer="friend@example.com")
    # An assistant's reject, undone and redone by the owner, is still the assistant's...
    library.decide(conn, [{"id": sid["IMG_4"], "pick": -1}], source="mcp")
    library.undo(conn)
    library.redo(conn)
    # ...but rejected again by the owner, it's theirs.
    library.decide(conn, [{"id": sid["IMG_5"], "pick": -1}], source="mcp")
    library.decide(conn, [{"id": sid["IMG_5"], "pick": 0}], source="web")
    library.decide(conn, [{"id": sid["IMG_5"], "pick": -1}], source="web")
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE pick=-1").fetchone()[0] == 6

    c = TestClient(create_app(owner="me@example.com"))
    j = c.post("/api/p/t/trash/plan", json={"what": "rejects", "filter": {"all": "1"}}).json()
    j = _wait(c, f"/api/p/t/trash/{j['token']}")
    assert j["state"] == "ready"
    skipped = {s["why"]: s["n"] for s in j["skipped"]}
    assert skipped == {"rejected by an assistant (open it in the Library and reject it yourself to include it)": 2,
                       "rejected by someone else (open it in the Library and reject it yourself to include it)": 1}
    assert j["go"]["all"] == 3                       # IMG_0, IMG_1 and IMG_5: the owner's own (all last copies)
    job = c.app.state.trash_jobs[j["token"]]
    going = {it.shot_id for it in job.items if it.category != "skip"}
    assert going == {sid["IMG_0"], sid["IMG_1"], sid["IMG_5"]}
