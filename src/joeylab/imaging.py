"""Loading previews quickly (JPEG DCT scaling, embedded RAW previews)."""

from __future__ import annotations

import io
import threading

from PIL import Image, ImageOps

try:
    import pi_heif          # HEIC/HEIF decoding (iPhone photos, Fujifilm .hif); read-only, BSD licence

    pi_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional
    pass

Image.MAX_IMAGE_PIXELS = 400_000_000

_TRANSPOSE = {
    2: [Image.Transpose.FLIP_LEFT_RIGHT],
    3: [Image.Transpose.ROTATE_180],
    4: [Image.Transpose.FLIP_TOP_BOTTOM],
    5: [Image.Transpose.TRANSPOSE],
    6: [Image.Transpose.ROTATE_270],
    7: [Image.Transpose.TRANSVERSE],
    8: [Image.Transpose.ROTATE_90],
}


def _orient(img: Image.Image, orientation: int | None) -> Image.Image:
    for op in _TRANSPOSE.get(orientation or 1, []):
        img = img.transpose(op)
    return img


def _fit(img: Image.Image, max_side: int) -> Image.Image:
    if max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return img


DAMAGED = "joeylab_damaged"   # set in img.info when the picture came from a damaged file
# Showing a damaged JPEG turns on PIL's "load what's there" switch for a moment; a check that a file
# decodes whole must never run while it's on.
_TRUNCATED = threading.Lock()


def load_preview(path: str, kind: str, orientation: int | None = 1, max_side: int = 2048) -> Image.Image:
    """RGB image, correctly rotated, long side <= max_side. Never writes anything.

    A damaged file (a recovered SD card often leaves cut-off JPEGs and RAWs) is shown from what can
    still be read: the intact part of a JPEG, or the preview a RAW carries inside it. Such an image
    has img.info[DAMAGED] set. A file that's missing or can't be read right now still raises."""
    try:
        if kind == "raw":
            return _load_raw(path, orientation, max_side)
        if kind == "video":
            return _fit(video_poster(path), max_side)
        img = Image.open(path)
        if img.format == "JPEG":
            img.draft("RGB", (max_side, max_side))  # decode at 1/2, 1/4, 1/8 scale
        img = ImageOps.exif_transpose(img)
        return _fit(img.convert("RGB"), max_side)
    except Exception as e:
        if kind == "video" or (isinstance(e, OSError) and e.errno is not None) or not _readable(path):
            raise                     # missing or unreadable right now: not damaged, try again later
        img = _salvage(path, kind, orientation, max_side)
        if img is None:
            raise
        img.info[DAMAGED] = f"{type(e).__name__}: {e}"[:200]
        return img


def _readable(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return bool(fh.read(1))
    except OSError:
        return False


def _salvage(path: str, kind: str, orientation: int | None, max_side: int) -> Image.Image | None:
    if kind == "raw":
        import subprocess

        from . import exif

        tool = exif.exiftool()
        if not tool:
            return None
        for tag in ("PreviewImage", "JpgFromRaw", "ThumbnailImage"):
            try:
                data = subprocess.run([tool, "-b", f"-{tag}", path], capture_output=True, timeout=60,
                                      env=exif.run_env(tool)).stdout
            except (OSError, subprocess.SubprocessError):
                return None
            if len(data) > 10_000:
                try:
                    img = Image.open(io.BytesIO(data))
                    img.draft("RGB", (max_side, max_side))
                    img = img.convert("RGB")
                    if min(img.size) >= 320:
                        return _fit(_orient(img, orientation), max_side)
                except Exception:
                    continue
        return None
    from PIL import ImageFile

    _TRUNCATED.acquire()
    old = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = True         # keep the part of a cut-off JPEG that's there
    try:
        img = Image.open(path)
        if img.format == "JPEG":
            img.draft("RGB", (max_side, max_side))
        img = ImageOps.exif_transpose(img).convert("RGB")
        return _fit(img, max_side)
    except Exception:
        return None
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = old
        _TRUNCATED.release()


def decodes(path: str) -> bool:
    """Whether the whole picture in this file can be read (a JPEG at 1/8 scale, which still reads
    every byte of it): a cut-off or damaged file says no. Read-only."""
    with _TRUNCATED:
        try:
            with Image.open(path) as img:
                if img.format == "JPEG":
                    img.draft("RGB", (256, 256))
                img.load()
            return True
        except Exception:
            return False


def video_poster(path: str) -> Image.Image:
    """A representative frame: 10% in (past fades from black), read with OpenCV (read-only)."""
    import cv2

    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            raise OSError("can't open this video")
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        if frames > 10:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frames * 0.1))
        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        if not ok or frame is None:
            raise OSError("no frame could be read from this video")
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    finally:
        cap.release()


def _load_raw(path: str, orientation: int | None, max_side: int) -> Image.Image:
    import rawpy

    with rawpy.imread(path) as raw:
        try:
            thumb = raw.extract_thumb()
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            thumb = None
        if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
            img = Image.open(io.BytesIO(thumb.data))
            img.draft("RGB", (max_side, max_side))
            # Embedded previews usually lack orientation; apply the RAW's.
            exif_orient = img.getexif().get(0x0112, 1)
            img = img.convert("RGB")
            img = _orient(img, orientation if exif_orient in (None, 1) else exif_orient)
            return _fit(img, max_side)
        if thumb is not None and thumb.format == rawpy.ThumbFormat.BITMAP:
            return _fit(_orient(Image.fromarray(thumb.data), orientation), max_side)
        rgb = raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8)
        return _fit(Image.fromarray(rgb), max_side)  # libraw already rotates
