"""Catalog backups (your ratings and names live only in the catalog), and restoring one.

Kept: the last 14 daily copies, plus one per week for 8 weeks and one per month for a year, so a
problem that goes unnoticed for a while never pushes out every good copy. A catalog that fails
SQLite's integrity check is never backed up over good copies.

Most daily copies are "slim": they leave out the picture-search index, which is two thirds of a
catalog and is rebuilt from the thumbnails after a restore. A full copy is made at least weekly
(and whenever you ask), and the weekly and monthly keepers are full ones. Upgrade and manual
copies are kept two at a time; the copies kept before a restore too, plus the first of them, which
holds the catalog as it was before any restore.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

from . import config, db

KEEP_DAILY, KEEP_WEEKLY, KEEP_MONTHLY = 14, 8, 12
FULL_EVERY_DAYS = 7
REBUILDABLE = ("search_vecs",)          # left out of slim copies; rebuilt after a restore
KEEP_OTHER = 2                          # pre-upgrade, before-restore and manual copies of each kind


def _dir(project: str) -> Path:
    return config.HOME / "backups" / project


def backup(project: str, force: bool = False) -> Path | None:
    """Copy one catalog with SQLite's online backup (safe while in use). Returns the file, or
    None when today's backup already exists. Raises db.CatalogDamaged if the catalog is damaged."""
    dest_dir = _dir(project)
    dest_dir.mkdir(parents=True, exist_ok=True)
    day = time.strftime('%Y%m%d')
    if not force and any(dest_dir.glob(f"catalog-{day}*.db")):
        return None
    full = force or not _recent_full(dest_dir)
    dest = dest_dir / (f"catalog-{day}.db" if full else f"catalog-{day}-slim.db")
    src = db.connect(project, create=False)
    tmp = dest.with_suffix(".part")
    try:
        ok = src.execute("PRAGMA quick_check").fetchone()[0]
        if ok != "ok":
            raise db.CatalogDamaged(f"{project}: integrity check failed ({ok}); existing backups were left as they are.")
        out = sqlite3.connect(tmp)
        with out:
            src.backup(out)
        if not full:
            for t in REBUILDABLE:
                if out.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone():
                    out.execute(f"DELETE FROM {t}")
            out.commit()
            out.execute("VACUUM")
        out.close()
        config.private(tmp)
        tmp.replace(dest)
    finally:
        src.close()
        tmp.unlink(missing_ok=True)
    prune(project)
    return dest


def _day(p: Path) -> date | None:
    try:
        return datetime.strptime(p.stem.split("-")[1], "%Y%m%d").date()
    except (IndexError, ValueError):
        return None


def _is_full(p: Path) -> bool:
    return not p.stem.endswith("-slim")


def _recent_full(d: Path) -> bool:
    today = date.today()
    return any(_is_full(p) and _day(p) and (today - _day(p)).days < FULL_EVERY_DAYS for p in d.glob("catalog-*.db"))


def prune(project: str) -> list[Path]:
    """Thin out daily copies: keep 14 days, and the newest full copy of each of the last 8 weeks
    and 12 months. Upgrade, restore and manual copies: the newest two of each kind, and the first
    copy kept before a restore."""
    files = sorted((p for p in _dir(project).glob("catalog-*.db") if _day(p)), key=lambda p: (_day(p), _is_full(p)))
    keep: set[Path] = set(files[-KEEP_DAILY:])
    weeks: dict[tuple, Path] = {}
    months: dict[tuple, Path] = {}
    for p in files:                     # later wins; a full copy wins over a slim one of the same period
        d = _day(p)
        for bucket, key in ((weeks, d.isocalendar()[:2]), (months, (d.year, d.month))):
            if key not in bucket or _is_full(p) or not _is_full(bucket[key]):
                bucket[key] = p
    keep |= set(list(weeks.values())[-KEEP_WEEKLY:]) | set(list(months.values())[-KEEP_MONTHLY:])
    removed = []
    for p in files:
        if p not in keep:
            p.unlink(missing_ok=True)
            removed.append(p)
    for prefix in ("pre-upgrade-", "before-restore-", "manual-"):
        others = sorted(_dir(project).glob(f"{prefix}*.db"), key=lambda x: x.stat().st_mtime)
        if prefix == "before-restore-":
            # By the time in the name: the first one holds the catalog as it was before any restore,
            # which a later restore (of the wrong copy, say) must never push out.
            others = sorted(others, key=_restore_order)[1:]
        for p in others[:-KEEP_OTHER]:
            p.unlink(missing_ok=True)
            removed.append(p)
    return removed


def _restore_order(p: Path) -> tuple:
    """before-restore-<time>.db, then -2, -3... for more restores in the same second."""
    m = re.match(r"before-restore-(\d{8}-\d{6})(?:-(\d+))?\.db$", p.name)
    return (m.group(1), int(m.group(2) or 1)) if m else (p.name, 0)


def backup_all(force: bool = False) -> list[Path]:
    out = []
    for name in config.list_projects():
        try:
            if p := backup(name, force):
                out.append(p)
        except db.CatalogDamaged:
            continue
    return out


def list_backups(project: str) -> list[dict]:
    d = _dir(project)
    out = []
    for p in sorted(d.glob("*.db"), key=lambda x: x.stat().st_mtime, reverse=True) if d.exists() else []:
        out.append({"file": str(p), "name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime,
                    "slim": p.name.startswith("catalog-") and not _is_full(p)})
    return out


def latest(project: str) -> Path | None:
    files = sorted((p for p in _dir(project).glob("catalog-*.db") if _day(p)), key=lambda p: (_day(p), _is_full(p)))
    return files[-1] if files else None


def restore(project: str, source: Path | None = None) -> tuple[Path, Path | None]:
    """Replace the catalog with a backup (the latest daily one by default). The current catalog
    is kept beside the backups first, so restoring can itself be undone. Only JoeyLab's own
    catalog changes; no photo is touched. Returns (restored from, where the old one was kept)."""
    source = Path(source) if source else latest(project)
    if not source or not source.exists():
        raise FileNotFoundError(f"No backup found for {project!r} in {_dir(project)}")
    check = sqlite3.connect(source)
    try:
        if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise db.CatalogDamaged(f"{source} fails the integrity check; choose another backup.")
    finally:
        check.close()
    catalog = config.project_dir(project) / "catalog.db"
    kept = None
    if catalog.exists():
        stamp = time.strftime('%Y%m%d-%H%M%S')
        kept = _dir(project) / f"before-restore-{stamp}.db"
        for k in range(2, 100):                       # a second restore in the same second never writes over the first's
            if not kept.exists():
                break
            kept = _dir(project) / f"before-restore-{stamp}-{k}.db"
        _dir(project).mkdir(parents=True, exist_ok=True)
        try:
            cur = sqlite3.connect(catalog)
            out = sqlite3.connect(kept)
            with out:
                cur.backup(out)
            out.close()
            cur.close()
        except sqlite3.DatabaseError:
            import shutil

            shutil.copy2(catalog, kept)      # too damaged to read: keep the raw file
        config.private(kept)
    src = sqlite3.connect(source)
    tmp = catalog.with_suffix(".restoring")
    out = sqlite3.connect(tmp)
    with out:
        src.backup(out)
    out.close()
    src.close()
    config.private(tmp)
    for side in ("-wal", "-shm"):
        Path(str(catalog) + side).unlink(missing_ok=True)
    tmp.replace(catalog)
    return source, kept
