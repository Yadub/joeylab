"""Outputs that never modify originals: symlink/copy albums, path lists, CSV, and opt-in XMP sidecars."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from . import config, library
from .library import Filter

FILE_CHOICES = ("preferred", "raw", "jpeg", "all")


def select_files(conn, s: dict, which: str) -> list[dict]:
    files = [f for f in library.files_of(conn, s["id"]) if f["kind"] != "video" or s.get("media") == "video"]
    if which == "all":
        return files
    if which == "raw":
        return [f for f in files if f["kind"] == "raw"] or files[:1]
    if which == "jpeg":
        return [f for f in files if f["kind"] in ("jpeg", "other")] or files[:1]
    # preferred: RAW if present (best for editing), else whatever exists
    raws = [f for f in files if f["kind"] == "raw"]
    return raws[:1] or files[:1]


def _album_name(s: dict, f: dict) -> str:
    ts = (s.get("capture_str") or "").replace("-", "").replace(":", "").replace(" ", "-")
    base = os.path.basename(f["path"])
    return f"{ts}_{base}" if ts else base


def tracked_roots() -> list[str]:
    """Every folder any library tracks: album folders may never go inside one."""
    from . import db

    roots = []
    for name in config.list_projects():
        try:
            c = db.connect(name, create=False)
        except Exception:
            continue
        try:
            roots += [r[0] for r in c.execute("SELECT path FROM roots")]
        finally:
            c.close()
    return roots


def within(dest: Path, folder: str | Path) -> bool:
    """Is dest the folder or inside it? Judged by the file system, not by spelling: macOS paths
    ignore case ("…/photos" is "…/Photos"), and links can lead in from elsewhere. A folder that
    isn't there right now (an unplugged drive) is compared by its path, ignoring case."""
    d = str(dest).rstrip(os.sep).lower()
    f = os.path.realpath(str(folder)).rstrip(os.sep).lower()
    if d == f or d.startswith(f + os.sep):
        return True
    try:
        target = os.stat(folder)
    except OSError:
        return False
    p = Path(dest)
    while True:
        try:
            st = os.stat(p)
            if (st.st_dev, st.st_ino) == (target.st_dev, target.st_ino):
                return True
        except OSError:
            pass
        if p.parent == p:
            return False
        p = p.parent


def check_dest(dest: Path) -> Path:
    """Where an album folder may go: anywhere except JoeyLab's own data and the photo folders
    it tracks (so no file is ever added to a folder you asked it only to read)."""
    raw = str(dest).strip()
    if not raw or not (raw.startswith("/") or raw.startswith("~")):
        raise ValueError("Give a full folder path, for example ~/Pictures/JoeyLab/Wedding-Best.")
    dest = Path(os.path.realpath(Path(raw).expanduser()))
    if within(dest, config.HOME):
        raise ValueError("Albums can't go inside JoeyLab's own data folder.")
    for r in tracked_roots():
        if within(dest, r):
            raise ValueError(f"That's inside {r}, a folder JoeyLab only reads. Choose a folder outside it, "
                             "for example ~/Pictures/JoeyLab/.")
    return dest


FILE_KINDS = {"list": (".txt", ".tsv", ".list"), "csv": (".csv",), "json": (".json",)}


def check_file_dest(out: Path, kind: str) -> Path:
    """Where an exported list, spreadsheet or JSON file may be written: never inside JoeyLab's own
    data or a folder it tracks, and never over a file that isn't an earlier export of the same kind
    (one mistyped name must not replace a photo)."""
    raw = str(out).strip()
    if not raw:
        raise ValueError("Give a file name, for example ~/Downloads/rejects.txt.")
    dest = Path(os.path.realpath(Path(raw).expanduser()))
    if dest.is_dir():
        raise ValueError(f"{dest} is a folder; give a file name inside it.")
    if within(dest, config.HOME):
        raise ValueError("That's inside JoeyLab's own data folder. Choose somewhere else, for example ~/Downloads/.")
    for r in tracked_roots():
        if within(dest, r):
            raise ValueError(f"That's inside {r}, a folder JoeyLab only reads. Choose somewhere else, for example ~/Downloads/.")
    if dest.exists() and dest.suffix.lower() not in FILE_KINDS[kind]:
        raise ValueError(f"{dest} already exists and isn't a {' or '.join(FILE_KINDS[kind])} file; choose another name.")
    return dest


def on_this_macs_disk(dest: Path) -> bool:
    """True when dest is on the same disk as your home folder (not an external drive)."""
    if str(dest).lower().startswith("/volumes/"):
        return False
    p = Path(dest)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return os.stat(p).st_dev == os.stat(os.path.expanduser("~")).st_dev
    except OSError:
        return False


@dataclass
class AlbumResult:
    created: int = 0
    skipped: int = 0
    removed: int = 0
    dest: str = ""


def _manifest(conn, dest: str) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT name, target FROM album_links WHERE dest=?", (dest,))}


def album(conn, flt: Filter, dest: Path, which: str = "preferred", copy: bool = False,
          clean: bool = False, sort: str = "time") -> AlbumResult:
    dest = check_dest(dest)
    dest.mkdir(parents=True, exist_ok=True)
    res = AlbumResult(dest=str(dest))
    wanted: dict[str, str] = {}
    for s in library.shots(conn, flt, sort=sort):
        for f in select_files(conn, s, which):
            name = _album_name(s, f)
            stem, ext = os.path.splitext(name)
            i = 1
            while name in wanted and wanted[name] != f["path"]:
                name = f"{stem}_{i}{ext}"
                i += 1
            wanted[name] = f["path"]
    made = _manifest(conn, str(dest))
    if clean:
        # Only links JoeyLab made here, still pointing where it pointed them, are ever removed.
        for name, target in made.items():
            p = dest / name
            if name not in wanted and p.is_symlink() and os.readlink(p) == target:
                os.unlink(p)
                res.removed += 1
                conn.execute("DELETE FROM album_links WHERE dest=? AND name=?", (str(dest), name))
    now = time.time()
    for name, src in wanted.items():
        target = dest / name
        if target.is_symlink() or target.exists():
            if target.is_symlink() and os.readlink(target) == src:
                res.skipped += 1
                continue
            if not target.is_symlink() or made.get(name) != os.readlink(target):
                res.skipped += 1  # never replace a real file, or a link someone else made
                continue
            os.unlink(target)
        if copy:
            shutil.copy2(src, target)
        else:
            os.symlink(src, target)
            conn.execute("INSERT INTO album_links(dest, name, target, created) VALUES (?,?,?,?)"
                         " ON CONFLICT(dest, name) DO UPDATE SET target=excluded.target, created=excluded.created",
                         (str(dest), name, src, now))
        res.created += 1
    conn.commit()
    return res


def paths(conn, flt: Filter, which: str = "all", sort: str = "time") -> list[str]:
    out = []
    for s in library.shots(conn, flt, sort=sort):
        out += [f["path"] for f in select_files(conn, s, which)]
    return out


CSV_COLS = ["id", "capture_str", "camera", "lens", "dir", "stem", "quality", "flags", "suggestion",
            "rating", "pick", "label", "group_id", "group_size", "group_rank", "sharp_subject", "face_sharp",
            "brightness", "main_faces", "eyes_closed", "aesthetic", "gps_lat", "gps_lon", "preview_path", "keywords"]


def csv_rows(conn, rows: list[dict]):
    """Header, then one row per photo (for the CLI file and the web download alike)."""
    from . import keywords as kw_mod

    kw: dict[int, list[str]] = {}
    all_paths = kw_mod.paths(conn)
    for sid, kid in conn.execute("SELECT shot_id, keyword_id FROM shot_keywords"):
        kw.setdefault(sid, []).append(" › ".join(all_paths.get(kid, [])))
    yield CSV_COLS
    for r in rows:
        r = {**r, "keywords": "; ".join(sorted(kw.get(r["id"], [])))}
        yield [";".join(r[c]) if c == "flags" else r.get(c) for c in CSV_COLS]


def to_csv(conn, flt: Filter, out: Path, sort: str = "time") -> int:
    rows = library.shots(conn, flt, sort=sort)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(csv_rows(conn, rows))
    return len(rows)


# ---------------------------------------------------------------- XMP sidecars (opt-in)

XMP_HEAD = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="JoeyLab">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
   xmlns:xmp="http://ns.adobe.com/xap/1.0/"
   xmlns:dc="http://purl.org/dc/elements/1.1/"
   xmlns:lr="http://ns.adobe.com/lightroom/1.0/"
   xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/"
   xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/"
   xmlns:stDim="http://ns.adobe.com/xap/1.0/sType/Dimensions#"
   xmlns:stArea="http://ns.adobe.com/xmp/sType/Area#"{attrs}>{body}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""
INCLUDE = ("rating", "label", "keywords", "people", "regions")


def xmp_values(s: dict) -> tuple[int | None, str | None] | None:
    """(Rating, Label) JoeyLab would record. Rating None: leave stars alone (a pick without
    stars says nothing about stars)."""
    pick, rating = s.get("pick") or 0, s.get("rating") or 0
    label = s.get("label") or None
    if pick == -1:
        return -1, label           # Lightroom/Bridge convention for "rejected"
    if pick == 1 or rating or label:
        return (rating or None), (label or ("Green" if pick == 1 else None))
    return None


def _bag(tag: str, items: list[str]) -> str:
    if not items:
        return ""
    lis = "".join(f"\n     <rdf:li>{escape(x)}</rdf:li>" for x in items)
    return f"\n   <{tag}>\n    <rdf:Bag>{lis}\n    </rdf:Bag>\n   </{tag}>"


def _regions(s: dict, faces: list[dict]) -> str:
    """MWG face regions, written only for photos stored upright (Orientation 1), where the
    coordinates are unambiguous for every app."""
    named = [f for f in faces if f.get("name")]
    if not named or (s.get("orientation") or 1) != 1 or not s.get("width") or not s.get("height"):
        return ""
    lis = "".join(
        f"""\n      <rdf:li rdf:parseType="Resource">
       <mwg-rs:Name>{escape(f['name'])}</mwg-rs:Name>
       <mwg-rs:Type>Face</mwg-rs:Type>
       <mwg-rs:Area stArea:x="{f['x'] + f['w'] / 2:.4f}" stArea:y="{f['y'] + f['h'] / 2:.4f}" stArea:w="{f['w']:.4f}" stArea:h="{f['h']:.4f}" stArea:unit="normalized"/>
      </rdf:li>""" for f in named)
    return (f"""\n   <mwg-rs:Regions rdf:parseType="Resource">
    <mwg-rs:AppliedToDimensions stDim:w="{s['width']}" stDim:h="{s['height']}" stDim:unit="pixel"/>
    <mwg-rs:RegionList>
     <rdf:Bag>{lis}
     </rdf:Bag>
    </mwg-rs:RegionList>
   </mwg-rs:Regions>""")


@dataclass
class XmpResult:
    written: int = 0
    updated: int = 0
    skipped_existing: int = 0
    kept_theirs: int = 0          # an existing sidecar already had a higher rating, or another label
    errors: list[str] = field(default_factory=list)
    backup_dir: str | None = None


def _identity(conn, s: dict, kw_paths: dict | None = None) -> dict:
    """Keywords and people JoeyLab knows for a photo."""
    faces = [dict(r) for r in conn.execute(
        "SELECT f.x, f.y, f.w, f.h, p.name FROM faces f LEFT JOIN people p ON p.id = f.person_id"
        " WHERE f.shot_id=? AND f.person_id > 0 AND COALESCE(p.hidden, 0) = 0", (s["id"],))]
    people = sorted({f["name"] for f in faces if f["name"]})
    keywords = list(people)
    ev = conn.execute("SELECT name FROM events WHERE id=?", (s.get("event_id"),)).fetchone() if s.get("event_id") else None
    if ev and ev[0]:
        keywords.append(ev[0])
    pl = conn.execute("SELECT name, country FROM places WHERE id=?", (s.get("place_id"),)).fetchone() if s.get("place_id") else None
    if pl:
        keywords += [x for x in (pl[0], pl[1]) if x]
    from . import keywords as kw_mod

    flat, hier = kw_mod.for_xmp(conn, s["id"], kw_paths)   # your keyword tree, the way Lightroom writes it
    keywords = list(dict.fromkeys(keywords + flat))
    return {"faces": faces, "people": people, "keywords": keywords, "hierarchy": hier}


def _new_sidecar(s: dict, vals, ident: dict, include: tuple) -> str:
    attrs, body = "", ""
    rating, label = vals if vals else (None, None)
    if "rating" in include and rating is not None:
        attrs += f"\n   xmp:Rating={quoteattr(str(rating))}"
    if "label" in include and label:
        attrs += f"\n   xmp:Label={quoteattr(label)}"
    if "keywords" in include:
        body += _bag("dc:subject", ident["keywords"])
        body += _bag("lr:hierarchicalSubject", ident.get("hierarchy") or [])
    if "people" in include:
        body += _bag("Iptc4xmpExt:PersonInImage", ident["people"])
    if "regions" in include:
        body += _regions(s, ident["faces"])
    return XMP_HEAD.format(attrs=attrs, body=body)


def _backup_name(side: str) -> str:
    tag = hashlib.blake2b(side.encode(), digest_size=6).hexdigest()
    return f"{tag}-{os.path.basename(side)}"


def _backup_sidecar(side: str, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    name = _backup_name(side)
    shutil.copy2(side, backup_dir / name)
    # One JSON object per line: a tab or a line break in a file name can't split an entry.
    index = backup_dir / "index.jsonl"
    fd = os.open(index, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"backup": name, "sidecar": side}) + "\n")


def _name_key(stem: str) -> str:
    """A file name as macOS compares it: case and Unicode spelling ignored."""
    return unicodedata.normalize("NFC", stem).lower()


def _stem_sidecar(conn, shot_id: int, path: str, folders: dict) -> str:
    """IMG_0001.xmp, or IMG_0001.JPG.xmp when another photo in the folder has the same name
    (IMG_0001.JPG and IMG_0001.TIF shot at other times): other apps would read IMG_0001.xmp for
    both, so neither gets it. Every photo in the catalog counts, not just the ones being exported,
    so exporting the other photo later never writes into the first one's sidecar. A photo whose
    namesake has since gone keeps the IMG_0001.JPG.xmp it already has."""
    folder, name = os.path.split(path)
    if folder not in folders:
        present, gone = defaultdict(set), defaultdict(set)
        for sid, stem, status in conn.execute("SELECT id, stem, status FROM shots WHERE dir=?", (folder,)):
            (present if status == "present" else gone)[_name_key(stem)].add(sid)
        folders[folder] = (present, gone)
    present, gone = folders[folder]
    key = _name_key(os.path.splitext(name)[0])
    stem_side, own = os.path.splitext(path)[0] + ".xmp", path + ".xmp"
    if present[key] - {shot_id}:
        return own
    if gone[key] - {shot_id} and os.path.exists(own) and not os.path.exists(stem_side):
        return own
    return stem_side


def write_sidecars(conn, flt: Filter, style: str = "stem", update_existing: bool = False,
                   include: tuple = ("rating", "label"), project: str | None = None) -> XmpResult:
    """Writes <stem>.xmp (Lightroom/Capture One) or <file>.<ext>.xmp (darktable) next to originals.

    Originals are never opened for writing. Existing sidecars are left alone unless
    update_existing is set. Then only the fields JoeyLab owns are changed, never lowering a
    rating or replacing a label another app set, and each sidecar is copied to
    ~/.joeylab/xmp-backups first.
    """
    from . import exif

    res = XmpResult()
    include = tuple(i for i in include if i in INCLUDE)
    backup_dir = config.HOME / "xmp-backups" / (project or "default") / time.strftime("%Y%m%d-%H%M%S")
    plan: list[tuple[dict, str, tuple | None, dict]] = []
    from . import keywords as kw_mod

    kw_paths = kw_mod.paths(conn)
    folders: dict = {}
    for s in library.shots(conn, flt):
        vals = xmp_values(s) if {"rating", "label"} & set(include) else None
        ident = _identity(conn, s, kw_paths) if {"keywords", "people", "regions"} & set(include) else \
            {"faces": [], "people": [], "keywords": []}
        if vals is None and not (ident["keywords"] or ident["people"]):
            continue
        files = library.files_of(conn, s["id"])
        has_raw = any(x["kind"] == "raw" for x in files)
        for f in files:
            if f["kind"] == "video" or (style == "stem" and f["kind"] != "raw" and has_raw):
                continue  # one stem.xmp per shot, owned by the RAW
            side = _stem_sidecar(conn, s["id"], f["path"], folders) if style == "stem" else (f["path"] + ".xmp")
            plan.append((s, side, vals, ident, f["path"]))
            if style == "stem":
                break
    # A last check: two photos never share one sidecar.
    owners: dict[str, set] = defaultdict(set)
    for s, side, *_ in plan:
        owners[side.lower()].add(s["id"])
    plan = [(s, (fpath + ".xmp") if len(owners[side.lower()]) > 1 else side, vals, ident)
            for s, side, vals, ident, fpath in plan]
    existing = [side for _, side, _, _ in plan if os.path.exists(side)]
    theirs, _ = exif.read_many(existing, tags=["Rating", "Label"]) if (existing and update_existing) else ({}, set())
    for s, side, vals, ident in plan:
        if not os.path.exists(side):
            with open(side, "x", encoding="utf-8") as fh:
                fh.write(_new_sidecar(s, vals, ident, include))
            res.written += 1
            continue
        if not update_existing:
            res.skipped_existing += 1
            continue
        t = theirs.get(side, {})
        args = []
        rating, label = vals if vals else (None, None)
        their_rating = t.get("Rating") if isinstance(t.get("Rating"), (int, float)) else None
        if "rating" in include and rating is not None:
            if their_rating is None or (rating > their_rating and their_rating >= 0) or (rating == -1 and their_rating <= 0):
                if rating != their_rating:
                    args.append(f"-XMP-xmp:Rating={rating}")
            elif rating != their_rating:
                res.kept_theirs += 1
        if "label" in include and label:
            if not t.get("Label"):
                args.append(f"-XMP-xmp:Label={label}")
            elif str(t.get("Label")).lower() != label.lower():
                res.kept_theirs += 1
        for tag, items in (("XMP-dc:Subject", ident["keywords"] if "keywords" in include else []),
                           ("XMP-lr:HierarchicalSubject", ident.get("hierarchy") or [] if "keywords" in include else []),
                           ("XMP-iptcExt:PersonInImage", ident["people"] if "people" in include else [])):
            for kw in items:      # remove-then-add keeps each value once
                args += [f"-{tag}-={kw}", f"-{tag}+={kw}"]
        if not args:
            res.skipped_existing += 1
            continue
        _backup_sidecar(side, backup_dir)
        res.backup_dir = str(backup_dir)
        tool = exif.exiftool()
        if not tool:
            res.errors.append(f"{side}: {exif.MISSING}")
            continue
        proc = subprocess.run([tool, "-q", "-overwrite_original", *args, side],
                              env=exif.run_env(tool), capture_output=True, text=True)
        if proc.returncode != 0:
            res.errors.append(f"{side}: {(proc.stderr or proc.stdout).strip()[:200]}")
        else:
            res.updated += 1
    return res


def _backup_entries(backup_dir: Path) -> list[tuple[str, str]]:
    """(backup file name, sidecar it came from) for each sidecar in a backup folder: index.jsonl, or
    index.tsv in backups made before it."""
    out = []
    index = backup_dir / "index.jsonl"
    if index.exists():
        for line in index.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                out.append((str(d["backup"]), str(d["sidecar"])))
            except (ValueError, KeyError, TypeError):
                continue
    old = backup_dir / "index.tsv"
    if old.exists():
        for line in old.read_text(encoding="utf-8").splitlines():
            name, _, original = line.partition("\t")
            out.append((name, original))
    return out


def restore_sidecars(backup_dir: Path) -> int:
    """Put back the sidecars a previous XMP update changed (from its backup folder). Only what the
    index really describes: an .xmp file, copied back to the path its backup's name was made from."""
    n = 0
    for name, original in _backup_entries(backup_dir):
        if not original.lower().endswith(".xmp") or not os.path.isabs(original) or name != _backup_name(original):
            continue          # a damaged or altered entry: never written anywhere
        src = backup_dir / name
        if src.is_file():
            shutil.copy2(src, original)
            n += 1
    return n


def manifest_json(conn) -> str:
    """Every album folder JoeyLab has made, for the record."""
    return json.dumps([dict(r) for r in conn.execute("SELECT dest, COUNT(*) AS links FROM album_links GROUP BY dest")])
