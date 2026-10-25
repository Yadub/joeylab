"""XMP sidecar names: IMG_0001.xmp belongs to one photo. When another photo in the folder has the same
name, each gets IMG_0001.JPG.xmp instead, judged from the whole catalog, not only from the photos in
one export, so a sidecar never receives another photo's stars."""

from __future__ import annotations

import os
import time

from PIL import Image

from conftest import add_root, make_jpeg, run_all


def _photo(path, seed, when):
    """A synthetic photo taken at a given time (EXIF DateTimeOriginal, YYYY:MM:DD HH:MM:SS)."""
    make_jpeg(path, seed=seed)
    img = Image.open(path)
    exif = img.getexif()
    exif.get_ifd(0x8769)[0x9003] = when
    exif[0x0110] = "TestCam"
    img.save(path, "JPEG", quality=90, exif=exif.tobytes())
    old = time.time() - 60
    os.utime(path, (old, old))


def _two_photos_one_name(lib):
    """IMG_1.JPG and IMG_1.jpeg, taken months apart: two photos with one name, kept apart."""
    _photo(lib / "IMG_1.JPG", seed=1, when="2024:01:01 10:00:00")
    _photo(lib / "IMG_1.jpeg", seed=2, when="2024:06:01 10:00:00")
    add_root(lib)
    conn, _ = run_all()
    first, second = (conn.execute("SELECT shot_id FROM files WHERE path=?", (str(lib / n),)).fetchone()[0]
                     for n in ("IMG_1.JPG", "IMG_1.jpeg"))
    assert first != second
    return conn, first, second


def _rating(path):
    from joeylab import exif

    tags, _ = exif.read_many([str(path)], tags=["Rating"])
    return tags.get(str(path), {}).get("Rating")


def test_exporting_one_of_two_photos_with_one_name_uses_its_full_name(env):
    from joeylab import export, library
    from joeylab.library import Filter

    _, lib = env
    conn, first, second = _two_photos_one_name(lib)
    library.decide(conn, [{"id": first, "rating": 5}, {"id": second, "rating": 1}])
    export.write_sidecars(conn, Filter(ids=[first]), include=("rating",), project="t")
    assert sorted(p.name for p in lib.glob("*.xmp")) == ["IMG_1.JPG.xmp"]
    # The other photo, exported later (updating sidecars), doesn't land in the first one's sidecar.
    export.write_sidecars(conn, Filter(ids=[second]), include=("rating",), update_existing=True, project="t")
    assert sorted(p.name for p in lib.glob("*.xmp")) == ["IMG_1.JPG.xmp", "IMG_1.jpeg.xmp"]
    assert _rating(lib / "IMG_1.JPG.xmp") == 5 and _rating(lib / "IMG_1.jpeg.xmp") == 1


def test_a_shared_stem_sidecar_from_before_is_left_alone(env):
    from joeylab import export, library
    from joeylab.library import Filter

    _, lib = env
    conn, first, second = _two_photos_one_name(lib)
    # IMG_1.xmp as an earlier export of the first photo alone left it: there's no telling which photo
    # it's for now, so neither writes into it.
    library.decide(conn, [{"id": first, "rating": 5}, {"id": second, "rating": 1}])
    (lib / "IMG_1.xmp").write_text(export._new_sidecar({}, (5, None), {}, ("rating",)), encoding="utf-8")
    before = (lib / "IMG_1.xmp").read_bytes()
    r = export.write_sidecars(conn, Filter(ids=[second]), include=("rating",), update_existing=True, project="t")
    assert r.written == 1 and r.updated == 0
    assert (lib / "IMG_1.xmp").read_bytes() == before and _rating(lib / "IMG_1.jpeg.xmp") == 1


def test_a_photo_keeps_its_full_name_sidecar_when_the_other_photo_goes(env):
    from joeylab import export, library, scanner
    from joeylab.library import Filter

    _, lib = env
    conn, first, second = _two_photos_one_name(lib)
    library.decide(conn, [{"id": first, "rating": 3}, {"id": second, "rating": 1}])
    export.write_sidecars(conn, Filter(ids=[first, second]), include=("rating",), project="t")
    (lib / "IMG_1.jpeg").unlink()
    (lib / "IMG_1.jpeg.xmp").unlink()
    scanner.scan(conn)
    assert conn.execute("SELECT status FROM shots WHERE id=?", (second,)).fetchone()[0] != "present"
    library.decide(conn, [{"id": first, "rating": 4}])
    r = export.write_sidecars(conn, Filter(ids=[first]), include=("rating",), update_existing=True, project="t")
    assert r.updated == 1 and r.written == 0
    assert sorted(p.name for p in lib.glob("*.xmp")) == ["IMG_1.JPG.xmp"] and _rating(lib / "IMG_1.JPG.xmp") == 4


def test_a_name_of_its_own_still_gets_the_stem_sidecar(env):
    from joeylab import export, library
    from joeylab.library import Filter

    _, lib = env
    _photo(lib / "IMG_1.JPG", seed=1, when="2024:01:01 10:00:00")
    _photo(lib / "IMG_2.JPG", seed=2, when="2024:06:01 10:00:00")
    add_root(lib)
    conn, _ = run_all()
    ids = [r[0] for r in conn.execute("SELECT id FROM shots ORDER BY stem")]
    library.decide(conn, [{"id": ids[0], "rating": 2}])
    export.write_sidecars(conn, Filter(ids=ids[:1]), include=("rating",), project="t")
    assert sorted(p.name for p in lib.glob("*.xmp")) == ["IMG_1.xmp"]
