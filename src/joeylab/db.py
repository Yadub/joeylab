"""SQLite catalog. Everything JoeyLab learns lives here, never beside the photos."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .config import private, project_dir, secure_home

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    id        INTEGER PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,
    added_at  REAL
);

-- One logical photo: a RAW and its JPEG twin share a shot.
CREATE TABLE IF NOT EXISTS shots (
    id              INTEGER PRIMARY KEY,
    dir             TEXT NOT NULL,
    stem            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'present',   -- present | missing
    capture_ts      REAL,
    capture_str     TEXT,
    camera          TEXT,
    body_serial     TEXT,
    lens            TEXT,
    iso             REAL,
    aperture        REAL,
    shutter         REAL,
    focal           REAL,
    width           INTEGER,
    height          INTEGER,
    orientation     INTEGER,
    preview_file_id INTEGER,
    analyzed_fp     TEXT,          -- fingerprint of the file the metrics came from
    thumb           TEXT,
    phash           INTEGER,
    dhash           INTEGER,
    group_id        INTEGER,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS shots_key ON shots(dir, stem);
CREATE INDEX IF NOT EXISTS shots_group ON shots(group_id);

CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY,
    root_id    INTEGER,
    path       TEXT UNIQUE NOT NULL,
    shot_id    INTEGER,
    kind       TEXT NOT NULL,          -- raw | jpeg | other
    ext        TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    fp         TEXT NOT NULL,          -- quick content fingerprint (size+head+tail)
    full_hash  TEXT,                   -- computed lazily for exact-duplicate checks
    status     TEXT NOT NULL DEFAULT 'present',
    exif_done  INTEGER NOT NULL DEFAULT 0,
    last_seen  INTEGER
);
CREATE INDEX IF NOT EXISTS files_shot ON files(shot_id);
CREATE INDEX IF NOT EXISTS files_fp ON files(fp);

CREATE TABLE IF NOT EXISTS metrics (
    shot_id       INTEGER PRIMARY KEY,
    sharpness     REAL,
    sharp_center  REAL,
    sharp_subject REAL,
    brightness    REAL,
    contrast      REAL,
    clip_high     REAL,
    clip_low      REAL,
    saturation    REAL,
    faces         INTEGER,
    main_faces    INTEGER,
    eyes_closed   INTEGER,
    face_sharp    REAL,
    face_area     REAL,
    smile         REAL,
    aesthetic     REAL,
    quality       REAL,
    flags         TEXT,
    suggestion    TEXT,
    group_rank    INTEGER
);

CREATE TABLE IF NOT EXISTS groups (
    id           INTEGER PRIMARY KEY,
    camera_key   TEXT,
    start_ts     REAL,
    end_ts       REAL,
    size         INTEGER,
    best_shot_id INTEGER
);

-- Your decisions. Kept separate from computed data so re-analysis never
-- touches them, and kept when a file goes missing in case it comes back.
CREATE TABLE IF NOT EXISTS decisions (
    shot_id    INTEGER PRIMARY KEY,
    rating     INTEGER,               -- 0-5
    pick       INTEGER NOT NULL DEFAULT 0,  -- 1 pick, -1 reject, 0 none
    label      TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS dup_sets (
    id    INTEGER PRIMARY KEY,
    kind  TEXT NOT NULL               -- exact | copy
);
CREATE TABLE IF NOT EXISTS dup_members (
    set_id   INTEGER NOT NULL,
    file_id  INTEGER NOT NULL,
    shot_id  INTEGER,
    keep     INTEGER NOT NULL DEFAULT 0
);

-- One row per detected face (the faces stage). person_src: auto (from clustering) | user.
CREATE TABLE IF NOT EXISTS faces (
    id          INTEGER PRIMARY KEY,
    shot_id     INTEGER NOT NULL,
    x REAL, y REAL, w REAL, h REAL,     -- box as fractions of the oriented preview
    px          INTEGER,                -- face width in pixels on the 2048 preview
    area        REAL,
    sharp       REAL,
    closed      INTEGER,
    smile       REAL,
    vec         BLOB,                   -- float16, L2-normalised identity embedding (512 or 128: see model)
    cluster     INTEGER,
    person_id   INTEGER,
    person_src  TEXT
);
CREATE INDEX IF NOT EXISTS faces_shot ON faces(shot_id);
CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id);
CREATE INDEX IF NOT EXISTS faces_cluster ON faces(cluster);

CREATE TABLE IF NOT EXISTS people (
    id    INTEGER PRIMARY KEY,
    name  TEXT UNIQUE NOT NULL
);

-- Folders you chose to keep when identical copies exist elsewhere (survives rescans).
CREATE TABLE IF NOT EXISTS keep_folders (
    folder TEXT PRIMARY KEY
);

-- Folders that are a hand-picked subset of another folder (recomputed with duplicates).
CREATE TABLE IF NOT EXISTS selections (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,          -- selection | mirror
    folder      TEXT NOT NULL,          -- the selection (or one of two mirrored folders)
    parent      TEXT NOT NULL,          -- the folder it was picked from (or its mirror)
    folder_files INTEGER, parent_files INTEGER,
    matched     INTEGER,                -- files in `folder` with a twin in `parent`
    identical   INTEGER,                -- of those, byte-identical (safe to link later)
    identical_bytes INTEGER,
    album_id    INTEGER,
    also_in     TEXT                    -- JSON: other folders it was also picked from
);

-- Albums survive the files they came from: items point at the ORIGINAL files, and keep
-- the name the photo had in the selection folder (used when making link folders).
CREATE TABLE IF NOT EXISTS albums (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    kind     TEXT NOT NULL DEFAULT 'manual',   -- selection | manual
    folder   TEXT UNIQUE,                       -- for selections: the selection folder
    parent   TEXT,
    created  REAL,
    updated  REAL
);
CREATE TABLE IF NOT EXISTS album_items (
    album_id    INTEGER NOT NULL,
    file_id     INTEGER NOT NULL,       -- the original file
    shot_id     INTEGER,
    name        TEXT NOT NULL,          -- file name inside the selection folder
    source_path TEXT,                   -- where the copy was found
    UNIQUE (album_id, name)
);
CREATE INDEX IF NOT EXISTS album_items_shot ON album_items(shot_id);

CREATE TABLE IF NOT EXISTS scans (
    id        INTEGER PRIMARY KEY,
    started   REAL,
    finished  REAL,
    summary   TEXT
);
"""


class CatalogDamaged(RuntimeError):
    """The catalog file can't be read. `joeylab restore` brings back a backup."""


class UpgradeFailed(CatalogDamaged):
    """A catalog upgrade stopped; it was rolled back, so the catalog is as it was."""


def _script(conn, sql: str) -> None:
    """Run several statements inside the current transaction. (executescript() would commit first,
    so an interrupted upgrade could be left half done.)"""
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                conn.execute(buf)
            buf = ""
    if buf.strip() and buf.strip() != ";":
        conn.execute(buf)


# Library names that would collide with the web app's own addresses (/img/share/..., /s/...).
RESERVED_NAMES = {"share", "s", "api", "static", "img"}


def connect(project: str, create: bool = True) -> sqlite3.Connection:
    pdir = project_dir(project)
    db_path = pdir / "catalog.db"
    if not db_path.exists() and not create:
        raise FileNotFoundError(f"There's no library called {project!r}. To make one: joeylab run PATH -p {project}")
    if not db_path.exists() and project.lower() in RESERVED_NAMES:
        raise ValueError(f"{project!r} is a word JoeyLab's web addresses use; choose another library name.")
    return connect_path(db_path, project)


def connect_path(db_path: Path, project: str) -> sqlite3.Connection:
    """The catalog at db_path, made (and brought up to date) if needed. connect() names it by library;
    a library being made by moving folders is written in a folder of its own first (split.py)."""
    pdir = db_path.parent
    secure_home()
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "thumbs").mkdir(exist_ok=True)
    if not db_path.exists():
        # A new catalog is this account's alone; SQLite gives its -wal and -shm files the same permissions.
        os.close(os.open(db_path, os.O_WRONLY | os.O_CREAT, 0o600))
    # check_same_thread off: the web server may open and use a connection on different pool threads.
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            conn.executescript(SCHEMA)        # tables any catalog has (IF NOT EXISTS), before upgrading
            try:
                _migrate(conn, project, db_path)
            except sqlite3.Error as e:
                conn.close()
                raise UpgradeFailed(
                    f"JoeyLab couldn't upgrade the catalog for {project!r} ({e}). Nothing was changed: the "
                    "upgrade was rolled back, and a copy taken just before it is in ~/.joeylab/backups. "
                    "Your photos are fine.") from e
    except sqlite3.DatabaseError as e:
        conn.close()
        raise CatalogDamaged(
            f"The catalog for {project!r} can't be read ({e}). Your photos are fine. "
            f"Bring back the latest backup with: joeylab restore -p {project}") from e
    return conn


# ---------------------------------------------------------------- migrations
#
# Steps only ever add columns, tables and indexes, run once per catalog in order (recorded in
# PRAGMA user_version), and the catalog is backed up first, so an upgrade can always be undone.

def _add_columns(conn, table: str, cols: list[tuple[str, str]]) -> None:
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, typ in cols:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def _m1_columns(conn) -> None:
    """Columns from earlier rounds, kept so very old catalogs still upgrade."""
    _add_columns(conn, "metrics", [("taste", "REAL")])
    _add_columns(conn, "shots", [("faces_fp", "TEXT")])
    _add_columns(conn, "selections", [("also_in", "TEXT")])


def _m2_library(conn) -> None:
    """Time, places, events, health, history, albums, sharing and the AI extras."""
    _add_columns(conn, "shots", [
        ("eff_ts", "REAL"),          # capture time on one shared timeline (camera clock + correction)
        ("time_src", "TEXT"),        # exif | file (no capture date: the file's modified time, local)
        ("tz_offset", "INTEGER"),    # minutes east of UTC, when the camera recorded it
        ("gps_lat", "REAL"), ("gps_lon", "REAL"), ("gps_alt", "REAL"),
        ("place_id", "INTEGER"), ("place_src", "TEXT"),   # gps | inferred
        ("event_id", "INTEGER"),
        ("error_fp", "TEXT"),        # analysis failed on this version of the file: wait until it changes
        ("media", "TEXT"),           # photo | video
        ("duration", "REAL"),
        ("cam_rating", "INTEGER"),   # stars set in camera or by another app (EXIF/XMP Rating)
        ("xmp_label", "TEXT"),
        ("edited", "INTEGER"),       # 1: develop settings in a sidecar
        ("c2pa", "TEXT"),            # Content Credentials found in the file
    ])
    _add_columns(conn, "files", [("meta", "TEXT"), ("f_capture", "REAL")])
    _add_columns(conn, "faces", [("kind", "TEXT")])                       # person | pet
    _add_columns(conn, "people", [("kind", "TEXT"), ("hidden", "INTEGER")])
    _add_columns(conn, "albums", [("query", "TEXT"), ("description", "TEXT")])
    _script(conn, LIBRARY_SCHEMA)


def _m3_indexes(conn) -> None:
    _script(conn, """
CREATE INDEX IF NOT EXISTS shots_status_time ON shots(status, capture_ts);
CREATE INDEX IF NOT EXISTS shots_status_eff ON shots(status, eff_ts);
CREATE INDEX IF NOT EXISTS shots_dir ON shots(dir);
CREATE INDEX IF NOT EXISTS shots_event ON shots(event_id);
CREATE INDEX IF NOT EXISTS shots_place ON shots(place_id);
CREATE INDEX IF NOT EXISTS metrics_quality ON metrics(quality);
CREATE INDEX IF NOT EXISTS dup_members_set ON dup_members(set_id);
CREATE INDEX IF NOT EXISTS dup_members_shot ON dup_members(shot_id);
CREATE INDEX IF NOT EXISTS album_items_album ON album_items(album_id);
""")


def _m4_backfill(conn) -> None:
    conn.execute("UPDATE shots SET eff_ts = capture_ts, time_src = 'exif' WHERE capture_ts IS NOT NULL AND eff_ts IS NULL")
    conn.execute("UPDATE shots SET media = 'photo' WHERE media IS NULL")
    # Read metadata once more so GPS, time zones and in-camera ratings fill in (read-only, exiftool).
    conn.execute("UPDATE files SET exif_done = 0 WHERE meta IS NULL")
    # Ids must never be handed out twice: remember the highest ever used.
    for t in ID_TABLES:
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, (SELECT COALESCE(MAX(id), 0) FROM " + t + "))",
                     (f"hwm:{t}",))
    # Real statistics for the query planner, so a new index is only used where it helps.
    conn.execute("ANALYZE")


def _m5_round5(conn) -> None:
    """The keyword tree, which drive each external folder is on (so a renamed drive is followed), and
    the face groups you said aren't someone at another age."""
    _script(conn, """
CREATE TABLE IF NOT EXISTS keywords (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    parent_id INTEGER,                 -- NULL at the top of the tree
    created   REAL
);
CREATE INDEX IF NOT EXISTS keywords_parent ON keywords(parent_id);
CREATE TABLE IF NOT EXISTS shot_keywords (
    shot_id    INTEGER NOT NULL,
    keyword_id INTEGER NOT NULL,
    src        TEXT,                   -- web | cli | mcp | sidecar | lightroom | merge | undo
    PRIMARY KEY (shot_id, keyword_id)
);
CREATE INDEX IF NOT EXISTS shot_keywords_kw ON shot_keywords(keyword_id);
-- "That group isn't this person at another age": never suggested again.
CREATE TABLE IF NOT EXISTS face_not_same (
    person_id INTEGER NOT NULL,
    cluster   INTEGER NOT NULL,
    ts        REAL,
    PRIMARY KEY (person_id, cluster)
);
""")
    _add_columns(conn, "roots", [("volume_uuid", "TEXT"), ("volume_name", "TEXT"), ("rel_path", "TEXT")])


def _m6_round6(conn) -> None:
    """Redo (which undo a step reversed), and a second look at photos whose Apple Vision pass failed
    without saying so: no labels, no text and no aesthetics at all mean it never ran."""
    _add_columns(conn, "decision_log", [("undoes", "TEXT")])
    _add_columns(conn, "dup_sets", [("key", "TEXT")])          # the same set keeps its id across updates
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='keywords'").fetchone():
        _add_columns(conn, "keywords", [("deleted", "INTEGER")])   # removed keywords stay, hidden, so Undo can bring them back
    # Files that failed to open get one more try: damaged ones can now be shown from what's readable.
    conn.execute("UPDATE shots SET error_fp = NULL WHERE analyzed_fp IS NULL AND error IS NOT NULL")
    conn.execute("CREATE TABLE IF NOT EXISTS face_groups (cluster INTEGER PRIMARY KEY, faces INTEGER, cohesion REAL,"
                 " shared_photos INTEGER, sharp REAL)")       # how well each face group agrees (unclear groups are set aside)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='vision'").fetchone():
        conn.execute("UPDATE vision SET src = '' WHERE (labels IS NULL OR labels = '[]') AND utility IS NULL"
                     " AND score IS NULL AND (text IS NULL OR text = '')")
    _sort_columns(conn)
    # Faces by photo with who they are, answered from the index alone (People pages, per-photo faces).
    conn.execute("CREATE INDEX IF NOT EXISTS faces_shot_person ON faces(shot_id, person_id, cluster)")


# The grid's time order (undated photos last) as one indexed number per photo, kept by triggers,
# so a page is read in index order instead of sorting the whole library for every page.
# Dated photos sort by time; then those with no time at all; then those dated only by their file
# (each part after the one before, whatever the times: they all lie within ±1e10 s of 1970).
_SORT_KEY = "(CASE WHEN COALESCE({t}.time_src, 'exif') = 'file' THEN 2e10 ELSE 0 END) + COALESCE({sign}{t}.eff_ts, 1e10)"


def _sort_columns(conn) -> None:
    _add_columns(conn, "shots", [("sort_ts", "REAL"), ("sort_desc", "REAL")])
    up, down = _SORT_KEY.format(t="shots", sign=""), _SORT_KEY.format(t="shots", sign="-")
    conn.execute(f"UPDATE shots SET sort_ts = {up}, sort_desc = {down}")
    nu, nd = _SORT_KEY.format(t="NEW", sign=""), _SORT_KEY.format(t="NEW", sign="-")
    for name, when in (("shots_sort_ins", "AFTER INSERT ON shots"), ("shots_sort_upd", "AFTER UPDATE OF eff_ts, time_src ON shots")):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {name} {when} BEGIN"
                     f" UPDATE shots SET sort_ts = {nu}, sort_desc = {nd} WHERE id = NEW.id; END")
    conn.execute("CREATE INDEX IF NOT EXISTS shots_sort ON shots(status, sort_ts, dir, stem)")
    conn.execute("CREATE INDEX IF NOT EXISTS shots_sort_desc ON shots(status, sort_desc, dir, stem)")


def _m7_person_aliases(conn) -> None:
    """A person's other names ("Dad" is also "Papa" and "Vikram"): each name belongs to one person,
    whatever its case, and goes when they do."""
    _script(conn, """
CREATE TABLE IF NOT EXISTS person_aliases (
    person_id INTEGER NOT NULL,
    name      TEXT NOT NULL COLLATE NOCASE UNIQUE
);
CREATE INDEX IF NOT EXISTS person_aliases_person ON person_aliases(person_id);
CREATE TRIGGER IF NOT EXISTS people_aliases_gone AFTER DELETE ON people BEGIN
    DELETE FROM person_aliases WHERE person_id = OLD.id;
END;
""")


def _m8_raw_choices(conn) -> None:
    """Keep RAW / RAW not needed for photos with a RAW and a JPEG (raws.py), and favourites looked up
    by photo (the rules that keep RAWs ask whether a photo is anyone's favourite)."""
    _script(conn, """
CREATE TABLE IF NOT EXISTS raw_choices (
    shot_id INTEGER PRIMARY KEY,
    keep    INTEGER NOT NULL,          -- 1 Keep RAW, 0 RAW not needed
    updated REAL
);
CREATE INDEX IF NOT EXISTS viewer_decisions_shot ON viewer_decisions(shot_id);
""")


def _m9_moves(conn) -> None:
    """Which file each path is (its inode): a file or library folder moved or renamed in Finder on
    the same drive is recognised at once, without reading it again."""
    _add_columns(conn, "files", [("ino", "INTEGER")])
    _add_columns(conn, "roots", [("ino", "INTEGER")])


def _m10_trips(conn) -> None:
    """Where each event and trip was and why a trip is one (JSON: the place, how it's known, and how
    far it is from home)."""
    _add_columns(conn, "events", [("place", "TEXT")])


def _m11_face_models(conn) -> None:
    """Which face models found each face (faces.MODELS): vectors from different models can't be
    compared. Every face found before there was a choice is InsightFace's."""
    _add_columns(conn, "faces", [("model", "TEXT NOT NULL DEFAULT 'insightface'")])


def _m12_colours(conn) -> None:
    """Each photo's colours, measured from its thumbnail (colours.py): the main hue, how colourful
    and how light it is, the colour most of it is and a three-colour palette."""
    _script(conn, """
CREATE TABLE IF NOT EXISTS colours (
    shot_id INTEGER PRIMARY KEY,
    src     TEXT NOT NULL,          -- analyzed_fp:colours.VERSION it was measured from
    hue     REAL,                   -- degrees around OKLab's hue circle; NULL: no colour to speak of
    chroma  REAL,                   -- how colourful: OKLab's mean chroma (0 for black and white)
    light   REAL,                   -- mean lightness, 0-1
    main    INTEGER,                -- the colour most of the photo is, 0xRRGGBB
    palette TEXT                    -- up to three colours, the most common first: #rrggbb,#rrggbb,#rrggbb
);
""")


def _m13_event_names(conn) -> None:
    """Each event's other name, from what its photos show ("Dance performance in Delhi"), and the folder
    its photos are in ("SD Card 20251110"): both shown beside its name, and searched."""
    _add_columns(conn, "events", [("alt_name", "TEXT"), ("folder", "TEXT")])


def _m14_backups(conn) -> None:
    """Which of this library's files have a byte-identical copy on another drive (copies.py keeps it
    from the index all libraries share), so the Library can filter and count by it. A file with no
    row has no copy on any other drive JoeyLab knows."""
    _script(conn, """
CREATE TABLE IF NOT EXISTS backups (
    file_id INTEGER PRIMARY KEY,
    shot_id INTEGER,
    checked INTEGER NOT NULL,       -- 1: the same full content hash (checked byte for byte); 0: same size and fingerprint, not checked yet
    drives  TEXT NOT NULL           -- the other drives holding it (keys in copies.db), comma-separated
);
CREATE INDEX IF NOT EXISTS backups_shot ON backups(shot_id);
""")


def _m15_excluded_folders(conn) -> None:
    """Folders inside the library's folders that it leaves out, with everything in them: a folder
    moved to another library (split.py) belongs to that one, so this library's scans skip it."""
    _script(conn, """
CREATE TABLE IF NOT EXISTS excluded_folders (
    folder  TEXT PRIMARY KEY,
    library TEXT,                   -- the library it was moved to, if any
    added   REAL
);
""")


# Tables of vectors and labels that other modules create when first needed (ai.SCHEMA, search.SCHEMA),
# each with an index on its photo column: counting their rows then reads the small index rather than
# every row's vectors (a library of 75,000 photos keeps over a gigabyte of them).
COUNT_INDEXES = {"embeddings": "embeddings_shot", "search_vecs": "search_vecs_shot", "vision": "vision_shot"}

# A counter in `meta` that changes, in the same transaction, with every change to what the top bar
# and Home count from these tables (decisions, photos and files coming and going, analysis, stacks,
# suggestions, duplicate sets, the settings that hide photos from browsing). Kept by triggers, so a
# change made by anything (the app, the command line, an assistant) moves it. The web app keeps those
# counts until it moves (web/overview.py).
COUNTS_GEN = "counts:gen"
COUNTED = {
    "decisions": ("pick", "rating"),
    "shots": ("status", "error", "analyzed_fp", "media", "group_id", "cam_rating"),
    "files": ("status", "shot_id", "kind", "size"),
    "metrics": ("flags", "suggestion"),
    "dup_sets": ("kind",),
    "dup_members": ("set_id", "file_id", "keep"),
    "groups": ("size",),
}
COUNTED_SETTINGS = ("purpose", "hide_tiny", "hide_utility", "hide_duplicates")


def _counts_triggers(conn) -> None:
    bump = (f"INSERT INTO meta(key, value) VALUES ('{COUNTS_GEN}', 1)"
            " ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;")
    for table, cols in COUNTED.items():
        changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in cols)
        for name, when in ((f"{table}_counted_ins", f"AFTER INSERT ON {table}"),
                           (f"{table}_counted_del", f"AFTER DELETE ON {table}"),
                           (f"{table}_counted_upd", f"AFTER UPDATE OF {', '.join(cols)} ON {table} WHEN {changed}")):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {name} {when} BEGIN {bump} END")
    keys = ", ".join(f"'{k}'" for k in COUNTED_SETTINGS)
    for name, ev in (("settings_counted_ins", "INSERT"), ("settings_counted_upd", "UPDATE")):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {name} AFTER {ev} ON settings WHEN NEW.key IN ({keys}) BEGIN {bump} END")
    conn.execute(f"INSERT OR IGNORE INTO meta(key, value) VALUES ('{COUNTS_GEN}', '1')")


def _m16_counts(conn) -> None:
    """Counting without reading the big rows: indexes on the photo column of the tables of vectors and
    labels, so their counts read only the index; two indexes that hold what People and the
    Library ask of faces (by group, and by person), so neither reads the faces' vectors; files by photo
    with their status and kind; and the counter the top bar's counts are kept by (COUNTS_GEN)."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, index in COUNT_INDEXES.items():
        if table in have:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {table}(shot_id)")
    _script(conn, """
CREATE INDEX IF NOT EXISTS faces_by_group ON faces(cluster, person_id, person_src, shot_id, closed, px, sharp);
CREATE INDEX IF NOT EXISTS faces_by_person ON faces(person_id, shot_id, closed, px, sharp, person_src);
CREATE INDEX IF NOT EXISTS files_shot_status ON files(shot_id, status, kind);
""")
    # The planner learns the new indexes at once, from statistics taken together with the older ones on the
    # same columns (stale statistics for those made it pick an index that has to read every face's row).
    conn.execute("ANALYZE faces")
    conn.execute("ANALYZE files")
    _counts_triggers(conn)


MIGRATIONS = [_m1_columns, _m2_library, _m3_indexes, _m4_backfill, _m5_round5, _m6_round6, _m7_person_aliases,
              _m8_raw_choices, _m9_moves, _m10_trips, _m11_face_models, _m12_colours, _m13_event_names, _m14_backups,
              _m15_excluded_folders, _m16_counts]
SCHEMA_VERSION = len(MIGRATIONS)


def _backup_before_migrating(conn, project: str, db_path: Path, version: int) -> None:
    import time

    if not conn.execute("SELECT 1 FROM shots LIMIT 1").fetchone():
        return  # a new, empty catalog: nothing to protect
    dest_dir = db_path.parent.parent.parent / "backups" / project
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"pre-upgrade-v{version}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    out = sqlite3.connect(dest)
    with out:
        conn.backup(out)
    out.close()
    private(dest)


def _migrate(conn: sqlite3.Connection, project: str = "", db_path: Path | None = None) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    if db_path is not None:
        _backup_before_migrating(conn, project, db_path, conn.execute("PRAGMA user_version").fetchone()[0])
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Another connection may have finished the upgrade while this one waited for the lock.
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for step in MIGRATIONS[version:]:
            step(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


LIBRARY_SCHEMA = """
-- Counters that tell in-memory caches to reload, and per-library settings.
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

-- Every decision change, append-only: undo that survives reloads and works across devices,
-- a history, and better training data for taste.
CREATE TABLE IF NOT EXISTS decision_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    batch    TEXT,
    label    TEXT,                  -- what the change was, e.g. "keep only this"
    source   TEXT,                  -- web | cli | accept | import | undo | api
    viewer   TEXT,
    shot_id  INTEGER NOT NULL,
    before   TEXT,                  -- JSON {rating, pick, label}
    after    TEXT,
    undone   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS decision_log_batch ON decision_log(batch);
CREATE INDEX IF NOT EXISTS decision_log_ts ON decision_log(ts);

-- Links JoeyLab made in album folders, so tidying a folder never removes anyone else's links.
CREATE TABLE IF NOT EXISTS album_links (
    dest    TEXT NOT NULL,
    name    TEXT NOT NULL,
    target  TEXT NOT NULL,
    created REAL,
    PRIMARY KEY (dest, name)
);

-- Places from an offline list of cities (GeoNames): locations never leave the Mac.
CREATE TABLE IF NOT EXISTS places (
    id       INTEGER PRIMARY KEY,   -- GeoNames id
    name     TEXT NOT NULL,
    admin1   TEXT,
    country  TEXT,
    cc       TEXT,
    lat      REAL,
    lon      REAL
);

-- Events and trips, with stable ids so names you give survive re-detection.
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT,
    auto_name     TEXT,
    user_named    INTEGER NOT NULL DEFAULT 0,
    kind          TEXT,             -- event | trip
    parent_id     INTEGER,          -- the trip an event belongs to
    start_ts      REAL,
    end_ts        REAL,
    n             INTEGER,
    cover_shot_id INTEGER,
    places        TEXT,             -- JSON [[place, photos], ...]
    people        TEXT,             -- JSON [[person id, photos], ...]
    hidden        INTEGER NOT NULL DEFAULT 0
);

-- Seconds to add to a camera body's clock so several cameras share one timeline.
CREATE TABLE IF NOT EXISTS camera_offsets (
    camera_key TEXT PRIMARY KEY,
    offset_s   REAL NOT NULL,
    source     TEXT,
    updated    REAL
);

-- Other people on the tailnet: their own favourites (never the owner's picks), and comments.
CREATE TABLE IF NOT EXISTS viewer_decisions (
    viewer  TEXT NOT NULL,
    shot_id INTEGER NOT NULL,
    fav     INTEGER NOT NULL DEFAULT 0,
    updated REAL,
    PRIMARY KEY (viewer, shot_id)
);
CREATE TABLE IF NOT EXISTS comments (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id INTEGER NOT NULL,
    viewer  TEXT,
    text    TEXT NOT NULL,
    ts      REAL
);
CREATE INDEX IF NOT EXISTS comments_shot ON comments(shot_id);

-- A shot list for a shoot: moments that should be covered by a pick.
CREATE TABLE IF NOT EXISTS shotlist (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    title     TEXT NOT NULL,
    query     TEXT,
    people    TEXT,                 -- JSON [person ids] who should be in it
    min_count INTEGER NOT NULL DEFAULT 1,
    position  INTEGER
);

-- Personal concepts learned from a few examples ("my blue bike").
CREATE TABLE IF NOT EXISTS concepts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    positives TEXT,
    negatives TEXT,
    w         BLOB,
    b         REAL,
    threshold REAL,
    model     TEXT,
    created   REAL
);

-- tiebreaks and captions held answers from the tie-break and caption features, since removed (JoeyLab
-- calls no AI service); the empty tables stay so every catalog keeps the same schema (tables are never dropped).
CREATE TABLE IF NOT EXISTS tiebreaks (
    key      TEXT PRIMARY KEY,
    shot_ids TEXT,
    ranking  TEXT,
    reasons  TEXT,
    model    TEXT,
    created  REAL
);

CREATE TABLE IF NOT EXISTS captions (
    shot_id INTEGER PRIMARY KEY,
    src     TEXT,
    text    TEXT,
    model   TEXT
);

-- Read-only links to one album or event for someone on the tailnet.
CREATE TABLE IF NOT EXISTS shares (
    token   TEXT PRIMARY KEY,
    kind    TEXT NOT NULL,          -- album | event
    ref     TEXT NOT NULL,
    label   TEXT,
    created REAL,
    expires REAL,
    revoked INTEGER NOT NULL DEFAULT 0
);

-- "Untouched" checks: tracked files compared before and after each update.
CREATE TABLE IF NOT EXISTS integrity (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL,
    checked INTEGER,
    changed INTEGER,
    details TEXT,
    ok      INTEGER
);
"""


# ---------------------------------------------------------------- small helpers

ID_TABLES = ("shots", "files", "faces")


def excluded_folders(conn) -> list[str]:
    """The folders this library leaves out (each with everything inside it)."""
    try:
        return [r[0] for r in conn.execute("SELECT folder FROM excluded_folders ORDER BY folder")]
    except sqlite3.OperationalError:          # a catalog read before its upgrade
        return []


def write_lock(conn) -> None:
    """Hold the catalog's write lock from here to the next commit, so a check ("is anyone called
    this?") and the write it leads to can't be split by another request doing the same."""
    if conn.in_transaction:
        conn.execute("UPDATE meta SET value = value WHERE 0")      # a write takes the lock
    else:
        conn.execute("BEGIN IMMEDIATE")


def new_id(conn, table: str) -> int:
    """An id never used before in this table. SQLite would hand out a deleted id again, and
    faces, album items or history left pointing at it would land on a different photo."""
    # One statement, so it runs under the write lock: two requests at once can't both read the
    # same high-water mark and hand out the same id.
    top = f"(SELECT COALESCE(MAX(id), 0) FROM {table})"
    return int(conn.execute(
        f"INSERT INTO meta(key, value) VALUES (?, {top} + 1)"
        f" ON CONFLICT(key) DO UPDATE SET value = MAX(CAST(value AS INTEGER), {top}) + 1 RETURNING value",
        (f"hwm:{table}",)).fetchone()[0])


def bump(conn, key: str) -> int:
    """Increment a counter (e.g. when embeddings change) so in-memory caches know to reload."""
    conn.execute("INSERT INTO meta(key, value) VALUES (?, '1')"
                 " ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1", (key,))
    return counter(conn, key)


def file_stamp(path: str) -> tuple:
    """Changes whenever anything (this process, the command line, an assistant) commits to the catalog
    at this path: every commit writes the WAL, and a checkpoint writes the file itself. An empty WAL holds
    nothing: SQLite makes a new one each time the catalog is opened after every connection closed (which
    also checkpoints and removes the last), so it counts as no WAL."""
    out: list = [path]
    for f in (path, path + "-wal"):
        try:
            st = os.stat(f)
            out += [st.st_mtime_ns, st.st_size] if st.st_size or f == path else [0, 0]
        except OSError:
            out += [0, 0]
    return tuple(out)


def counter(conn, key: str) -> int:
    try:
        r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(r[0]) if r else 0


def get_setting(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r[0])
    except ValueError:
        return default


def set_setting(conn, key: str, value) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
    conn.commit()


# Catalog rows computed about a photo (never the photo itself).
SHOT_TABLES = ("metrics", "embeddings", "search_vecs", "vision", "captions", "viewer_decisions", "comments", "colours",
               "backups")


def forget_computed(conn, ids: list[int]) -> None:
    """Remove what JoeyLab computed about these shots (faces, vectors, captions...).
    Used when shots leave the catalog; decisions are handled by the caller."""
    if not ids:
        return
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for i in range(0, len(ids), 500):
        chunk = [int(x) for x in ids[i : i + 500]]
        q = ",".join("?" * len(chunk))
        for t in SHOT_TABLES:
            if t in tables:
                conn.execute(f"DELETE FROM {t} WHERE shot_id IN ({q})", chunk)
        if "search_fts" in tables:
            conn.execute(f"DELETE FROM search_fts WHERE rowid IN ({q})", chunk)
        conn.execute(f"DELETE FROM faces WHERE shot_id IN ({q})", chunk)
        conn.execute(f"DELETE FROM dup_members WHERE shot_id IN ({q})", chunk)


def thumbs_dir(project: str) -> Path:
    return project_dir(project) / "thumbs"


def to_i64(u: int) -> int:
    return u - (1 << 64) if u >= (1 << 63) else u


def to_u64(i: int) -> int:
    return i + (1 << 64) if i < 0 else i
