"""People: find every face, recognise it, check eyes/smile per face (MediaPipe on the crop), and
cluster faces into people.

Face recognition is off until you turn it on, and you choose its models then (MODELS):

* InsightFace's buffalo_s pack (SCRFD finds faces, ArcFace recognises them): the most accurate,
  free for non-commercial research use only;
* OpenCV's YuNet and SFace (MIT and Apache 2.0): free for any use, including paid work.

Either is downloaded once. Their face vectors can't be compared (512 and 128 numbers), so every
face records which models found it and only the chosen models' faces are ever compared (active()).
Switching finds every face again, keeping names on the faces found in the same place (_carry).
"""

from __future__ import annotations

import atexit
import io
import itertools
import os
import threading
import time
import traceback
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from multiprocessing import get_context
from typing import Callable

import cv2
import numpy as np

from . import config, db
from .config import FACE_MODEL, MODELS_DIR

FACES_VERSION = "3"
DET_PATH = MODELS_DIR / "scrfd_500m.onnx"
REC_PATH = MODELS_DIR / "arcface_mbf.onnx"
PACK_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_s.zip"
PACK_SHA256 = "d85a87f503f691807cd8bb97128bdf7a0660326cd9cd02657127fa978bab8b5e"
PACK_FILES = {"det_500m.onnx": DET_PATH, "w600k_mbf.onnx": REC_PATH}

# OpenCV Zoo's models, from OpenCV's own copies on Hugging Face at a fixed commit (the GitHub
# repository keeps them in Git LFS).
YUNET_PATH = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_PATH = MODELS_DIR / "face_recognition_sface_2021dec.onnx"
OPENCV_FILES = {
    YUNET_PATH.name: (YUNET_PATH, "https://huggingface.co/opencv/face_detection_yunet/resolve/"
                      "3cc26e7f1014a5ee5d74a42acee58bafc9d0a310/face_detection_yunet_2023mar.onnx",
                      "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    SFACE_PATH.name: (SFACE_PATH, "https://huggingface.co/opencv/face_recognition_sface/resolve/"
                      "3d7082438a6e4551e840c9b2bb60b71e8da4b524/face_recognition_sface_2021dec.onnx",
                      "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
}

SOURCE_SIDE = 2048       # faces are found on the 2048 preview
DET_SIDE = 1600          # detector input, long side
DET_THRESH = 0.5
NMS_THRESH = 0.4
YUNET_SIDES = (1600, 640)  # YuNet finds faces of about 10 to 300 px: a second, smaller pass finds close-ups
YUNET_THRESH = 0.7
MIN_PX = 36              # smaller faces can't be recognised reliably
EYES_MIN_PX = 56         # smaller faces can't be judged for closed eyes
MAIN_FACE_MIN_AREA = 0.004
CLOSED_BLINK = 0.6
LAUGH_SMILE = 0.85

ARC_TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                         [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)

LICENCE_URL = "https://github.com/deepinsight/insightface/tree/master/python-package#license"
YUNET_LICENCE_URL = "https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/LICENSE"
SFACE_LICENCE_URL = "https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/LICENSE"


@dataclass(frozen=True)
class Models:
    """A set of face models, as the choice between them shows it."""
    key: str
    label: str               # the choice, in full: what it is and who may use it
    title: str               # the choice, short ("Most accurate")
    use: str                 # who may use it
    good: str                # what it's good at
    licence: str             # its licence in one sentence
    links: tuple             # ((label, url), ...): the full licences
    download_mb: int
    kept_mb: int             # what stays on this Mac
    credit: str              # the models, by name
    version: str             # in each photo's faces stamp: a change finds its faces again
    # Similarity bars, measured for each set's vectors (ArcFace's and SFace's spread differently):
    # cluster: a face joins a group; merge: two groups are one person (also naming a group pulls
    # in others); unclear, unclear_soft: a group's median likeness to its centre below which it's
    # a mix of faces (see UNCLEAR_SOFT_SHARP); edge: the same person years apart; suggest: "Is
    # this …?"; track: one person across the frames of a burst.
    limits: dict


MODELS = {
    "insightface": Models(
        key="insightface", label="Most accurate · personal, non-commercial use only", title="Most accurate",
        use="Personal, non-commercial use only",
        good="The best at telling people apart, and at small, turned or poorly lit faces.",
        licence="InsightFace makes these models free for non-commercial research use only, so not for paid work.",
        links=(("InsightFace licence", LICENCE_URL),), download_mb=130, kept_mb=16,
        credit="InsightFace buffalo_s (SCRFD and ArcFace)", version=FACES_VERSION,
        limits={"cluster": 0.42, "merge": 0.50, "unclear": 0.50, "unclear_soft": 0.60, "edge": 0.34, "suggest": 0.30,
                "track": 0.45}),
    "opencv": Models(
        key="opencv", label="Free for any use, including paid work", title="Free for any use",
        use="Including paid work",
        good="Good with clear, front-facing faces. It splits one person into more groups and mixes people up more "
             "often, so expect more naming.",
        licence="OpenCV's YuNet (MIT licence) and SFace (Apache 2.0 licence) may be used for anything, including paid work.",
        links=(("MIT licence", YUNET_LICENCE_URL), ("Apache 2.0 licence", SFACE_LICENCE_URL)), download_mb=39, kept_mb=39,
        credit="OpenCV YuNet and SFace", version="opencv-1",
        # SFace's likenesses run higher than ArcFace's for the same false matches (OpenCV gives 0.363
        # for one face against another). Measured on a library of ~25,000 faces with 15 named people.
        limits={"cluster": 0.54, "merge": 0.62, "unclear": 0.55, "unclear_soft": 0.65, "edge": 0.48, "suggest": 0.45,
                "track": 0.55}),
}
# What a switch of models does, said before it's confirmed (the app, the command line).
SWITCH_NOTE = ("Every face is found again with the new models, in each library at its next update: a background "
               "step, like finding the faces in new photos. Names are kept: your people, their other names, who's you, "
               "hidden people and your \"not the same person\" answers. Each face you named passes its name to the face "
               "found in the same place; a face the new models don't find loses its name. People keeps working "
               "meanwhile, and its suggestions fill in as faces are found again.")
UNCLEAR_SOFT_SHARP = 250


def files(key: str) -> list:
    """The model files a set needs on this Mac."""
    return list(PACK_FILES.values()) if key == "insightface" else [p for p, _, _ in OPENCV_FILES.values()]


def downloaded(key: str) -> bool:
    return all(p.exists() for p in files(key))


def available(key: str | None = None) -> bool:
    """Whether the models can run here (the chosen ones, or any before a choice): InsightFace's need
    the faces extra (ONNX Runtime); OpenCV runs its own."""
    if (key or current()) == "insightface":
        from importlib.util import find_spec

        return find_spec("onnxruntime") is not None      # without importing it (slow, and it writes to ~/Library)
    return hasattr(cv2, "FaceDetectorYN")


def current() -> str | None:
    """The face models chosen on this Mac (a MODELS key), or None before any choice. An install from
    before there was a choice has InsightFace's: it agreed to their licence or has them downloaded."""
    from . import prefs

    choice = prefs.load().get("faces")
    if isinstance(choice, dict):
        if choice.get("model") in MODELS:
            return choice["model"]
        if choice.get("enabled"):
            return "insightface"
    return "insightface" if downloaded("insightface") else None


def kept_from_before() -> bool:
    """The models in use are InsightFace's only because this install had them before there was a choice."""
    from . import prefs

    choice = prefs.load().get("faces")
    return current() == "insightface" and not (isinstance(choice, dict) and choice.get("model"))


def active() -> str:
    """The models whose face vectors are compared: the chosen ones, else InsightFace's (the only ones
    there were, so any faces already in a catalog are theirs). Faces found with other models are
    left out of every comparison until they're found again."""
    return current() or "insightface"


def limits(key: str | None = None) -> dict:
    """Similarity bars for a set of face models' vectors, or for pets' (pets.MODEL)."""
    key = key or active()
    if key not in MODELS:
        from . import pets

        return pets.LIMITS
    return MODELS[key].limits


def enabled() -> bool:
    """Face recognition is on for this Mac: you turned it on with a set of models and agreed to their
    licence or, on an install from before that choice existed, InsightFace's were already downloaded."""
    from . import prefs

    choice = prefs.load().get("faces")
    if isinstance(choice, dict) and "enabled" in choice:
        return bool(choice["enabled"]) and current() is not None
    return downloaded("insightface")


def chosen() -> bool:
    """Whether face recognition was ever turned on or off here (not just the default)."""
    from . import prefs

    return isinstance(prefs.load().get("faces"), dict)


def set_enabled(on: bool, via: str, model: str | None = None) -> None:
    """Turn face recognition on with a set of models (having shown their licence and had it agreed
    to) or off, for every library. Turning it off keeps faces already found; forgetting them is per
    library. Turning it on with other models than before finds every face again (switch_progress)."""
    from . import prefs

    key = model or current()
    if key not in MODELS:
        if on:
            raise ValueError("Choose the face models first.")
        key = None
    prefs.update(faces={"enabled": bool(on), "model": key, "when": time.strftime("%Y-%m-%d %H:%M:%S"), "via": via,
                        "licence": MODELS[key].links[0][1] if on else None})


def ensure_models(log: Callable[[str], None] = print, key: str | None = None) -> bool:
    key = key or active()
    if downloaded(key):
        return True
    if config.offline():
        log("Face recognition models aren't downloaded, and downloads are off (JOEYLAB_OFFLINE)")
        return False
    from . import downloads

    m = MODELS[key]
    log(f"Downloading face recognition models ({m.credit}; one time, about {m.download_mb} MB"
        + (f"; {m.kept_mb} MB kept)" if m.kept_mb != m.download_mb else ")"))
    if key == "opencv":
        try:
            for dest, url, sha in OPENCV_FILES.values():
                if not dest.exists():
                    downloads.fetch(url, sha, dest)
        except OSError as e:
            log(f"Face model download failed: {e}")
            return False
        return downloaded(key)
    pack = MODELS_DIR / "buffalo_s.zip"
    try:
        downloads.fetch(PACK_URL, PACK_SHA256, pack)
        with zipfile.ZipFile(pack) as z:
            for name in z.namelist():
                base = os.path.basename(name)
                if base in PACK_FILES:
                    tmp = PACK_FILES[base].with_suffix(".part")
                    tmp.write_bytes(z.read(name))
                    tmp.rename(PACK_FILES[base])
    except (OSError, zipfile.BadZipFile) as e:
        log(f"Face model download failed: {e}")
        return False
    finally:
        pack.unlink(missing_ok=True)
    return downloaded(key)


def switch_progress(conn) -> dict | None:
    """While this library's faces are being found again with newly chosen models: {"from", "to",
    "done", "total"} (photos). None otherwise, and for a library where faces are off."""
    was, now = db.get_setting(conn, "faces_model", "insightface"), active()
    if was == now or not db.get_setting(conn, "faces_enabled", True) or not conn.execute(
            "SELECT 1 FROM faces LIMIT 1").fetchone():
        return None
    total = conn.execute("SELECT COUNT(*) FROM shots s JOIN files f ON f.id = s.preview_file_id WHERE s.status='present'"
                         " AND f.status='present' AND s.analyzed_fp IS NOT NULL").fetchone()[0]
    return {"from": was, "to": now, "done": total - len(pending(conn)), "total": total}


# ---------------------------------------------------------------- per-process models

_det = _rec = _lm = None
_backend = "insightface"  # the models this process loaded (MODELS key)
SMALL_BATCH = 4   # this few photos are handled in-process: starting workers costs more
_inproc = {"ready": None, "lock": __import__("threading").Lock()}     # ready: the models loaded in-process


def _close_inproc() -> None:
    """MediaPipe aborts the process if a landmarker is still open at interpreter exit."""
    global _lm
    if _lm is not None:
        try:
            _lm.close()
        except Exception:
            pass
        _lm = None
    _inproc["ready"] = None


def _init_worker(quiet: bool = True, key: str = "insightface") -> None:
    global _det, _rec, _lm, _backend
    if quiet:
        os.dup2(os.open(os.devnull, os.O_WRONLY), 2)   # silence native-library chatter
        cv2.setNumThreads(1)
    _backend = key
    if key == "opencv":
        _det = cv2.FaceDetectorYN.create(str(YUNET_PATH), "", (320, 320), YUNET_THRESH, NMS_THRESH, 5000)
        _rec = cv2.FaceRecognizerSF.create(str(SFACE_PATH), "")
    else:
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.intra_op_num_threads = 2
        so.log_severity_level = 3
        _det = ort.InferenceSession(str(DET_PATH), so, providers=["CPUExecutionProvider"])
        _rec = ort.InferenceSession(str(REC_PATH), so, providers=["CPUExecutionProvider"])
    if FACE_MODEL.exists():
        try:
            from mediapipe.tasks import python as mp_tasks
            from mediapipe.tasks.python import vision

            _lm = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
                base_options=mp_tasks.BaseOptions(model_asset_path=str(FACE_MODEL),
                                                  delegate=mp_tasks.BaseOptions.Delegate.CPU),
                running_mode=vision.RunningMode.IMAGE, num_faces=1, min_face_detection_confidence=0.3,
                output_face_blendshapes=True))
            atexit.register(_close_inproc)   # in pool workers too: MediaPipe aborts if left open at exit
        except Exception:
            _lm = None


def _nms(boxes: np.ndarray, scores: np.ndarray, thresh: float) -> list[int]:
    x1, y1, x2, y2 = boxes.T
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]]); yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]]); yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        order = order[1:][inter / (areas[i] + areas[order[1:]] - inter + 1e-9) <= thresh]
    return keep


def detect(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Faces in rgb, with the loaded models: boxes (n,4), keypoints (n,5,2: the eyes, the nose and the
    corners of the mouth, left to right as seen) in rgb's pixel space, and scores."""
    return _detect_yunet(rgb) if _backend == "opencv" else _detect_scrfd(rgb)


def _detect_scrfd(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    h, w = rgb.shape[:2]
    scale = DET_SIDE / max(h, w)
    nh, nw = int(round(h * scale)), int(round(w * scale))
    ih, iw = (nh + 31) // 32 * 32, (nw + 31) // 32 * 32
    canvas = np.zeros((ih, iw, 3), dtype=np.float32)
    canvas[:nh, :nw] = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
    blob = ((canvas - 127.5) / 128.0).transpose(2, 0, 1)[None]
    outs = _det.run(None, {_det.get_inputs()[0].name: blob})
    boxes, kpss, scores = [], [], []
    for i, stride in enumerate((8, 16, 32)):
        sc, bb, kp = outs[i][:, 0], outs[i + 3] * stride, outs[i + 6] * stride
        fh, fw = ih // stride, iw // stride
        centers = np.stack(np.mgrid[:fh, :fw][::-1], axis=-1).astype(np.float32).reshape(-1, 2) * stride
        centers = np.repeat(centers, 2, axis=0)
        pos = np.nonzero(sc >= DET_THRESH)[0]
        if not len(pos):
            continue
        c = centers[pos]
        b = bb[pos]
        boxes.append(np.stack([c[:, 0] - b[:, 0], c[:, 1] - b[:, 1], c[:, 0] + b[:, 2], c[:, 1] + b[:, 3]], axis=1))
        kpss.append((c[:, None, :] + kp[pos].reshape(-1, 5, 2)))
        scores.append(sc[pos])
    if not boxes:
        return np.zeros((0, 4)), np.zeros((0, 5, 2)), np.zeros(0)
    boxes, kpss, scores = np.concatenate(boxes) / scale, np.concatenate(kpss) / scale, np.concatenate(scores)
    keep = _nms(boxes, scores, NMS_THRESH)
    return boxes[keep], kpss[keep], scores[keep]


def _detect_yunet(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    bgr = np.ascontiguousarray(rgb[..., ::-1])
    h, w = rgb.shape[:2]
    found = []
    for side in sorted({min(s, max(h, w)) for s in YUNET_SIDES}, reverse=True):
        scale = side / max(h, w)
        nh, nw = int(round(h * scale)), int(round(w * scale))
        _det.setInputSize((nw, nh))
        _, rows = _det.detect(bgr if scale == 1 else cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA))
        if rows is not None:
            found.append(np.concatenate([rows[:, :14] / scale, rows[:, 14:15]], axis=1))
    if not found:
        return np.zeros((0, 4)), np.zeros((0, 5, 2)), np.zeros(0)
    rows = np.concatenate(found)
    boxes = np.stack([rows[:, 0], rows[:, 1], rows[:, 0] + rows[:, 2], rows[:, 1] + rows[:, 3]], axis=1)
    keep = _nms(boxes, rows[:, 14], NMS_THRESH)
    return boxes[keep], rows[keep, 4:14].reshape(-1, 5, 2), rows[keep, 14]


def embed(rgb: np.ndarray, kps: np.ndarray) -> np.ndarray:
    """The face's identity vector (unit length), from a 112 px crop of the face turned upright on its
    keypoints."""
    if _backend == "opencv":
        row = np.zeros((1, 15), dtype=np.float32)
        row[0, 4:14] = kps.reshape(-1)
        face = _rec.alignCrop(rgb, row)          # OpenCV's own alignment (a warp: colour order doesn't matter)
        v = _rec.feature(np.ascontiguousarray(face[..., ::-1]))[0]       # SFace reads BGR
    else:
        M, _ = cv2.estimateAffinePartial2D(kps.astype(np.float32), ARC_TEMPLATE, method=cv2.LMEDS)
        face = cv2.warpAffine(rgb, M, (112, 112), borderValue=0.0).astype(np.float32)
        blob = ((face - 127.5) / 127.5).transpose(2, 0, 1)[None]
        v = _rec.run(None, {_rec.get_inputs()[0].name: blob})[0][0]
    return v / (np.linalg.norm(v) + 1e-9)


def eyes_and_smile(rgb: np.ndarray, box: np.ndarray) -> tuple[int | None, float | None]:
    if _lm is None:
        return None, None
    import mediapipe as mp

    x1, y1, x2, y2 = box
    cx, cy, s = (x1 + x2) / 2, (y1 + y2) / 2, max(x2 - x1, y2 - y1) * 0.9
    h, w = rgb.shape[:2]
    a, b = int(max(0, cx - s)), int(max(0, cy - s))
    c, d = int(min(w, cx + s)), int(min(h, cy + s))
    crop = cv2.resize(rgb[b:d, a:c], (256, 256), interpolation=cv2.INTER_AREA)
    res = _lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(crop)))
    if not res.face_blendshapes:
        return None, None
    sc = {x.category_name: x.score for x in res.face_blendshapes[0]}
    blink = min(sc.get("eyeBlinkLeft", 0), sc.get("eyeBlinkRight", 0))
    squint = min(sc.get("eyeSquintLeft", 0), sc.get("eyeSquintRight", 0))
    smile = (sc.get("mouthSmileLeft", 0) + sc.get("mouthSmileRight", 0)) / 2
    closed = blink > CLOSED_BLINK and not (smile > LAUGH_SMILE and squint > 0.5)
    return int(closed), float(smile)


def analyze_one(task: dict) -> dict:
    from .imaging import load_preview

    try:
        img = load_preview(task["path"], task["kind"], task["orientation"], SOURCE_SIDE)
        rgb = np.asarray(img)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        H, W = gray.shape
        boxes, kpss, _ = detect(rgb)
        faces = []
        for box, kps in zip(boxes, kpss):
            x1, y1, x2, y2 = box
            px = x2 - x1
            if px < MIN_PX:
                continue
            crop = gray[int(max(0, y1)):int(min(H, y2)), int(max(0, x1)):int(min(W, x2))]
            if crop.size < 64:
                continue
            # Measure focus at the face's own size (only shrinking big faces): enlarging a small
            # face would blur it and make every group shot look soft.
            if crop.shape[1] > 256:
                crop = cv2.resize(crop, (256, max(8, int(256 * crop.shape[0] / crop.shape[1]))), interpolation=cv2.INTER_AREA)
            closed, smile = eyes_and_smile(rgb, box) if px >= EYES_MIN_PX else (None, None)
            # A ready-made 200 px crop for the People page (same framing as the server's).
            cx, cy, s = (x1 + x2) / 2, (y1 + y2) / 2, max(px, y2 - y1) * 0.8
            thumb = img.crop((int(cx - s), int(cy - s), int(cx + s), int(cy + s))).resize((200, 200))
            buf = io.BytesIO()
            thumb.save(buf, "JPEG", quality=85)
            faces.append({
                "x": float(x1 / W), "y": float(y1 / H), "w": float(px / W), "h": float((y2 - y1) / H),
                "px": int(px), "area": float(px * (y2 - y1) / (W * H)),
                "sharp": float(cv2.Laplacian(crop, cv2.CV_64F).var()), "closed": closed, "smile": smile,
                "vec": embed(rgb, kps).astype(np.float16).tobytes(), "crop": buf.getvalue(),
            })
        return {"shot_id": task["shot_id"], "stamp": task["stamp"], "faces": faces, "error": None}
    except Exception as e:
        return {"shot_id": task["shot_id"], "stamp": task["stamp"], "faces": [], "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc(limit=3)}


# ---------------------------------------------------------------- stage

def pending(conn, force: bool = False) -> list[dict]:
    """Photos whose faces are still to be found with the chosen models (new, changed, or found with
    other models)."""
    version = MODELS[active()].version
    rows = conn.execute(
        "SELECT s.id, s.orientation, s.analyzed_fp, s.faces_fp, f.path, f.kind FROM shots s"
        " JOIN files f ON f.id = s.preview_file_id WHERE s.status='present' AND f.status='present'"
        " AND s.analyzed_fp IS NOT NULL").fetchall()
    out = []
    for r in rows:
        stamp = f"{r['analyzed_fp']}:{version}"
        if force or r["faces_fp"] != stamp:
            out.append({"shot_id": r["id"], "path": r["path"], "kind": r["kind"],
                        "orientation": r["orientation"] or 1, "stamp": stamp})
    return out


def same_place(old: list, new: list[dict]) -> list[int | None]:
    """For each face found again in a photo, the earlier face in the same place (its index in `old`), if
    any: centres within half a face of each other, each earlier face going to its nearest."""
    pairs = []
    for i, f in enumerate(new):
        cx, cy = f["x"] + f["w"] / 2, f["y"] + f["h"] / 2
        for j, o in enumerate(old):
            ow, oh = o["w"] or 0, o["h"] or o["w"] or 0
            dx = abs(o["x"] + ow / 2 - cx) / (max(ow, f["w"]) or 1)
            dy = abs(o["y"] + oh / 2 - cy) / (max(oh, f["h"]) or 1)
            if dx < 0.5 and dy < 0.5:
                pairs.append((dx + dy, i, j))
    out: list[int | None] = [None] * len(new)
    taken_old = set()
    for _, i, j in sorted(pairs):
        if out[i] is not None or j in taken_old:
            continue
        out[i] = j
        taken_old.add(j)
    return out


def _carry(old: list, new: list[dict], key: str) -> list[tuple]:
    """What each face found again in a photo keeps from the face in the same place before (same_place):
    a person you named, or said it isn't, always. Faces found before with other models (a switch) also
    hand on their group and the person the grouping gave them, so groups keep their ids, and named people
    their photos, until the new models' grouping. Returns (cluster, person_id, person_src) per new face."""
    out: list[tuple] = [(None, None, None)] * len(new)
    for i, j in enumerate(same_place(old, new)):
        if j is None:
            continue
        o = old[j]
        switched = o["model"] != key
        if o["person_src"] == "user":
            out[i] = (o["cluster"] if switched else None, o["person_id"], "user")
        elif switched:
            out[i] = (o["cluster"], o["person_id"], o["person_src"])
    return out


def _update_metrics(conn, shot_id: int, faces: list[dict]) -> None:
    """Face fields in metrics come from this stage once it has run: it sees far more faces
    (group shots) than the whole-image check in the analysis stage."""
    main = [f for f in faces if f["area"] >= MAIN_FACE_MIN_AREA]
    judged = [f for f in main if f["closed"] is not None]
    if main:
        # "Is the subject in focus?": the sharpest of the three largest faces, so a blurred
        # foreground guest doesn't mark a photo of a sharp subject as soft.
        face_sharp = float(max(f["sharp"] for f in sorted(main, key=lambda f: -f["area"])[:3]))
        smiles = [(f["smile"], f["area"]) for f in main if f["smile"] is not None]
        smile = float(np.average([s for s, _ in smiles], weights=[a for _, a in smiles])) if smiles else None
        area = max(f["area"] for f in main)
    else:
        face_sharp = smile = area = None
    conn.execute(
        "UPDATE metrics SET faces=?, main_faces=?, eyes_closed=?, face_sharp=?, face_area=?, smile=?, face_quality=?"
        " WHERE shot_id=?", (len(faces), len(main), sum(f["closed"] for f in judged), face_sharp, area, smile,
                             _photo_quality(main), shot_id))


def _photo_quality(main: list[dict]) -> float | None:
    """The main faces' capture quality, the bigger faces counting for more; None until they're judged."""
    judged = [(f["quality"], f["area"]) for f in main if f.get("quality") is not None and f["quality"] >= 0]
    return float(np.average([q for q, _ in judged], weights=[a for _, a in judged])) if judged else None


def refresh_metrics(conn, shot_ids) -> None:
    """The face fields in these photos' metrics again, from their faces in the catalog: after faces were
    found to be an animal's, or put back among people's (pets.py). The caller commits."""
    for sid in set(shot_ids):
        rows = conn.execute("SELECT area, closed, smile, sharp, quality FROM faces WHERE shot_id=? AND model=?"
                            " AND COALESCE(kind, '') != 'pet'", (sid, active())).fetchall()
        _update_metrics(conn, sid, [{**dict(r), "area": r["area"] or 0.0, "sharp": r["sharp"] or 0.0} for r in rows])


# ---------------------------------------------------------------- how well each face is caught
#
# Apple Vision's face capture quality (VNDetectFaceCaptureQualityRequest): 0-1 for focus, light,
# expression and how the head is turned, made for comparing captures of the same person. Judged once
# per face, from its 200 px crop (cut from the 2048 px preview, so small faces in group shots are
# judged at a useful size), in a pass of its own after faces are found; faces still to judge are found
# from an index of only those (faces_unjudged). Scoring's faces part and Cull's face compare use it,
# weighted so that it never decides on its own (scoring.caught).


def vision_quality(items: list) -> list[float]:
    """Apple Vision's capture quality of each face, given as (image path, (x, y, w, h) of the face in it,
    fractions from the top left); -1 for one it couldn't judge."""
    import Vision
    from Foundation import NSURL

    revision = getattr(Vision, "VNDetectFaceRectanglesRequestRevision3", 3)
    out = []
    for path, (x, y, w, h) in items:
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(path), None)
        req = Vision.VNDetectFaceCaptureQualityRequest.alloc().init()
        face = Vision.VNFaceObservation.faceObservationWithRequestRevision_boundingBox_roll_yaw_pitch_(
            revision, ((x, 1 - y - h), (w, h)), None, None, None)     # (Vision's origin is the bottom left)
        req.setInputFaceObservations_([face])
        res = handler.performRequests_error_([req], None)
        ok = res[0] if isinstance(res, tuple) else res
        got = (req.results() or []) if ok else []
        q = got[0].faceCaptureQuality() if got else None
        out.append(round(float(q), 4) if q is not None else -1.0)
    return out


def quality_pending(conn) -> list:
    """Faces not judged yet, in photos in the library (never pets: they're stored as -1)."""
    from . import pets

    return conn.execute(
        "SELECT f.id, f.shot_id, f.x, f.y, f.w, f.h, s.thumb, s.analyzed_fp FROM faces f INDEXED BY faces_unjudged"
        " JOIN shots s ON s.id = f.shot_id WHERE f.quality IS NULL AND f.model != ? AND s.status = 'present'"
        " ORDER BY f.shot_id, f.id", (pets.MODEL,)).fetchall()


def _judge(rows: list, crop_dir) -> list[tuple[int, float]]:
    """One photo's faces judged: from each face's crop, or the thumbnail when there's no crop."""
    from PIL import Image

    from .library import version_tag

    try:
        with Image.open(rows[0]["thumb"]) as im:
            W, H = im.size
    except Exception:
        W = H = 0
    items, ids, out = [], [], []
    for r in rows:
        w, h = r["w"] or 0, r["h"] or r["w"] or 0
        crop = crop_dir / f"{r['id'] % 100:02d}" / f"{r['id']}_{version_tag(r['analyzed_fp'])}.jpg" if crop_dir else None
        if not (w and W):
            out.append((r["id"], -1.0))
            continue
        if crop is not None and crop.exists():
            # The crop is a square 1.6 times the face's longer side, centred on it (as _write_crop's are cut).
            fw, fh = w * W, h * H
            bw, bh = fw / (1.6 * max(fw, fh)), fh / (1.6 * max(fw, fh))
            items.append((str(crop), (0.5 - bw / 2, 0.5 - bh / 2, bw, bh)))
        else:
            items.append((r["thumb"], (r["x"], r["y"], w, h)))
        ids.append(r["id"])
    return out + list(zip(ids, vision_quality(items)))


def measure_quality(conn, progress: Callable[[int, int], None] | None = None, batch: int = 64,
                    errors: list | None = None) -> int:
    """Judge the faces waiting for it (quality_pending), a batch of photos at a time, each batch saved in
    one short write with its photos' quality (metrics.face_quality); a stop keeps what was saved. Apple
    Vision not answering in VISION_TIMEOUT_S ends the pass for this update. Returns how many faces."""
    from collections import defaultdict
    from concurrent.futures import TimeoutError as FutureTimeout
    from pathlib import Path

    from . import deadline

    errors = errors if errors is not None else []
    rows = quality_pending(conn)
    if not rows:
        return 0
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    crop_dir = Path(db_file).parent / "previews" / "faces" if db_file else None
    by_shot: dict[int, list] = defaultdict(list)
    for r in rows:
        by_shot[r["shot_id"]].append(r)
    shots = list(by_shot)
    done = 0
    with deadline.Pool(2, "vision") as pool:
        for i in range(0, len(shots), batch):
            chunk = shots[i : i + batch]
            futs = {sid: pool.submit(_judge, by_shot[sid], crop_dir) for sid in chunk}
            got, stuck = {}, False
            for sid in chunk:
                try:
                    got[sid] = futs[sid].result(timeout=0 if stuck else config.VISION_TIMEOUT_S)
                except FutureTimeout:
                    if not stuck:
                        errors.append((sid, f"Apple Vision didn't answer in {config.VISION_TIMEOUT_S:.0f} s; the rest"
                                            " of the faces are judged in the next update"))
                    stuck = True
                except Exception as e:
                    errors.append((sid, f"Apple Vision failed: {type(e).__name__}: {e}"))
            for sid, judged in got.items():
                conn.executemany("UPDATE faces SET quality=? WHERE id=?", [(q, fid) for fid, q in judged])
                main = conn.execute("SELECT area, quality FROM faces WHERE shot_id=? AND model=? AND COALESCE(kind, '') != 'pet'"
                                    " AND area >= ?", (sid, active(), MAIN_FACE_MIN_AREA)).fetchall()
                conn.execute("UPDATE metrics SET face_quality=? WHERE shot_id=?", (_photo_quality([dict(m) for m in main]), sid))
                done += len(judged)
            conn.commit()
            if progress:
                progress(min(i + len(chunk), len(shots)), len(shots))
            if stuck:
                break
    return done


def _write_crop(crop_dir, fid: int, analyzed_fp: str, data: bytes) -> None:
    from .library import version_tag

    dest = crop_dir / f"{fid % 100:02d}" / f"{fid}_{version_tag(analyzed_fp)}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def run(conn, force: bool = False, workers: int | None = None,
        progress: Callable[[int, int], None] | None = None) -> tuple[int, list[str]]:
    from pathlib import Path

    tasks = pending(conn, force)
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    crop_dir = Path(db_file).parent / "previews" / "faces" if db_file else None
    if not tasks:
        return 0, []
    workers = workers or max(1, min(6, (os.cpu_count() or 4) - 2))
    key = active()
    errors, done = [], 0
    def results():
        if len(tasks) <= SMALL_BATCH:
            with _inproc["lock"]:
                if _inproc["ready"] != key:
                    _close_inproc()
                    _init_worker(quiet=False, key=key)
                    _inproc["ready"] = key
                    atexit.register(_close_inproc)
                for t in tasks:
                    yield analyze_one(t)
            return
        with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn"), initializer=_init_worker,
                                 initargs=(True, key)) as pool:
            for fut in as_completed([pool.submit(analyze_one, t) for t in tasks]):
                yield fut.result()

    from . import pets

    for r in results():
        done += 1
        if r["error"]:
            errors.append(f"shot {r['shot_id']}: {r['error']}")
        else:
            # Keep your naming: faces are replaced, but what you said about a face stays with the
            # new face in the same place (and on a switch of models, its group too), as does your
            # word that it's a person's, not an animal's (pets.put_back). The photo's pets (pets.py)
            # aren't faces found here, and stay.
            old = conn.execute("SELECT id, x, y, w, h, cluster, person_id, person_src, model, kind FROM faces WHERE shot_id=?"
                               " AND model != ?", (r["shot_id"], pets.MODEL)).fetchall()
            conn.execute("DELETE FROM faces WHERE shot_id=? AND model != ?", (r["shot_id"], pets.MODEL))
            fp = r["stamp"].rsplit(":", 1)[0]
            said = [j is not None and old[j]["kind"] == "person" for j in same_place(old, r["faces"])]
            for f, (cl, pid, src), person in zip(r["faces"], _carry(old, r["faces"], key), said):
                crop = f.pop("crop", None)
                fid = db.new_id(conn, "faces")
                conn.execute(
                    "INSERT INTO faces(id, shot_id, x, y, w, h, px, area, sharp, closed, smile, vec, model, cluster,"
                    " person_id, person_src, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fid, r["shot_id"], f["x"], f["y"], f["w"], f["h"], f["px"], f["area"], f["sharp"], f["closed"],
                     f["smile"], f["vec"], key, cl, pid, src, "person" if person else None))
                if crop and crop_dir:
                    _write_crop(crop_dir, fid, fp, crop)
            if crop_dir:
                from .library import version_tag

                for o in old:                    # the replaced faces' crops (JoeyLab's own cache)
                    (crop_dir / f"{o['id'] % 100:02d}" / f"{o['id']}_{version_tag(fp)}.jpg").unlink(missing_ok=True)
            _update_metrics(conn, r["shot_id"], r["faces"])
            conn.execute("UPDATE shots SET faces_fp=? WHERE id=?", (r["stamp"], r["shot_id"]))
        if done % 20 == 0:
            conn.commit()
        if progress:
            progress(done, len(tasks))
    conn.commit()
    return done, errors


# ---------------------------------------------------------------- clustering

MIN_CLUSTER_PX = 48       # only reasonably sized faces seed clusters
# The face's photo is in the library: photos that went are few, so this is a short list, and the faces
# are read from an index instead of being joined to their photos one by one.
PRESENT = "f.shot_id NOT IN (SELECT id FROM shots WHERE status < 'present' OR status > 'present')"


FULL_EVERY_S = 24 * 3600     # a complete re-grouping at most daily; in between, new faces are placed
# "Unclear faces": groups that are a mix of blurred, turned and partial faces rather than one person.
# Their faces agree less (with ArcFace, one person scores 0.6-0.8) and are soft; small but sharp
# faces (stage make-up, a crowd) that agree reasonably are kept as people. The bars are per set of
# models (limits()); UNCLEAR_SOFT_SHARP is the softness below which a group must agree better.


def _unit(vecs: np.ndarray, at: np.ndarray) -> np.ndarray:
    """Rows `at` of vectors() as float32 of unit length (each row on its own, so a chunk at a time gives
    the same numbers as all at once)."""
    V = vecs[at].astype(np.float32)
    return V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)


def _best(sims: np.ndarray, bar: float, k: int = 8) -> np.ndarray:
    """The k highest of sims at or above bar, highest first: the ones np.argsort(-sims)[:k] gives before
    its first below the bar, without sorting them all (sorting every group for every face took most of
    a complete regrouping's time)."""
    top = np.flatnonzero(sims >= bar)
    if len(top) > k:
        top = top[np.argpartition(-sims[top], k - 1)[:k]]
    return top[np.argsort(-sims[top], kind="stable")]


# ---------------------------------------------------------------- every face's vector, kept in memory
#
# Naming a group, "Is this …?" and the same person across the years each compare thousands of groups,
# which means every face's vector: on a library of 300,000 faces, reading them from the catalog took
# seconds each time. A face's vector never changes (a face found again is a new row with a higher id),
# so they're kept in memory per catalog and chosen models, brought up to date by reading only the new
# faces, and let go when unused for VECTORS_IDLE_S. Who a face is (its group, its name) is always read
# fresh, from the faces_by_group index rather than the faces' rows.

VECTORS_IDLE_S = 600.0
_VECTORS: dict[tuple, dict] = {}
_VECTORS_LOCK = threading.Lock()
NO_GROUP = -(1 << 62)            # a face in no group, in assignments()


def vectors(conn, model: str | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(ids, vectors): every face the chosen models found (or, given a model, every face that model
    found: pets.MODEL for pets), ids ascending, one float16 row each."""
    model = model or active()
    key = (conn.execute("PRAGMA database_list").fetchone()[2], model)
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")                           # (one snapshot: the count and the faces read agree)
    try:
        n, top = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM faces").fetchone()
        with _VECTORS_LOCK:
            c = _VECTORS.get(key)
            if c is None or c["n"] != n or c["top"] != top:
                c = _VECTORS[key] = _vectors_since(conn, model, c, n, top)
                _let_go_later()
            c["used"] = time.time()
            return c["ids"], c["vecs"]
    finally:
        if own:
            conn.rollback()                             # only read


def _vectors_since(conn, model: str, c: dict | None, n: int, top: int) -> dict:
    """The kept vectors brought up to date: faces that went are dropped, and only faces added since
    (ids above the highest seen: ids only go up) are read."""
    after = c["top"] if c else 0
    # (+id: counted and listed from an index; a range of ids would read the faces' rows)
    added = conn.execute("SELECT COUNT(*) FROM faces WHERE +id > ?", (after,)).fetchone()[0]
    if c is not None and c["n"] + added != n:
        alive = np.fromiter((r[0] for r in conn.execute("SELECT id FROM faces WHERE +id <= ?", (after,))), dtype=np.int64)
        keep = np.isin(c["ids"], alive)                 # faces that went (their photos left the library)
        c = {**c, "ids": c["ids"][keep], "vecs": c["vecs"][keep], "inv": c["inv"][keep]}
    ids = np.empty(added, dtype=np.int64)
    new = None
    k = 0
    for fid, vec in conn.execute("SELECT id, vec FROM faces WHERE id > ? AND model = ? AND vec IS NOT NULL ORDER BY id",
                                 (after, model)):
        if new is None:                                 # filled in place: never two copies of them all
            new = np.empty((added, len(vec) // 2), dtype=np.float16)
        new[k] = np.frombuffer(vec, dtype=np.float16)
        ids[k] = fid
        k += 1
    dim = new.shape[1] if new is not None else c["vecs"].shape[1] if c is not None else 0
    new, ids = (new[:k], ids[:k]) if new is not None else (np.zeros((0, dim), dtype=np.float16), ids[:0])
    if k < added // 2:                                  # most were other models' faces: don't keep their room
        new, ids = new.copy(), ids.copy()
    inv = np.empty(len(new), dtype=np.float32)          # 1 / each vector's length (float16 rounds them)
    for a in range(0, len(new), 32768):
        inv[a : a + 32768] = 1.0 / (np.linalg.norm(new[a : a + 32768].astype(np.float32), axis=1) + 1e-9)
    if c is not None and len(c["ids"]):
        ids, new, inv = np.concatenate([c["ids"], ids]), np.concatenate([c["vecs"], new]), np.concatenate([c["inv"], inv])
    return {"n": n, "top": top, "ids": ids, "vecs": new, "inv": inv, "used": time.time()}


def inverse_norms(vecs: np.ndarray) -> np.ndarray:
    """1 / the length of each of these vectors (as vectors() gave them), kept with them."""
    with _VECTORS_LOCK:
        for c in _VECTORS.values():
            if c["vecs"] is vecs:
                return c["inv"]
    return 1.0 / (np.linalg.norm(vecs.astype(np.float32), axis=1) + 1e-9)


_LETTING_GO = False


def _let_go_later() -> None:
    """Vectors unused for VECTORS_IDLE_S are let go (a minute's check, in a thread of its own)."""
    global _LETTING_GO
    if _LETTING_GO:
        return
    _LETTING_GO = True

    def loop():
        while True:
            time.sleep(60)
            with _VECTORS_LOCK:
                for k in [k for k, c in _VECTORS.items() if time.time() - c["used"] > VECTORS_IDLE_S]:
                    del _VECTORS[k]

    threading.Thread(target=loop, name="face-vectors", daemon=True).start()


def assignments(conn) -> dict[str, np.ndarray]:
    """Who each face of a photo in the library is, as arrays: id, cluster (NO_GROUP: none), person
    (0: nobody; below 0: "not them"), user (named by you), shot."""
    rows = conn.execute(
        f"SELECT f.id, COALESCE(f.cluster, {NO_GROUP}), COALESCE(f.person_id, 0), COALESCE(f.person_src = 'user', 0), f.shot_id"
        " FROM faces f WHERE f.shot_id NOT IN (SELECT id FROM shots WHERE status < 'present' OR status > 'present')").fetchall()
    a = np.fromiter(itertools.chain.from_iterable(rows), dtype=np.int64, count=5 * len(rows)).reshape(len(rows), 5)
    return {"id": a[:, 0], "cluster": a[:, 1], "person": a[:, 2], "user": a[:, 3].astype(bool), "shot": a[:, 4]}


def rows_of(ids: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Where each face (by id) is in vectors(); -1 for faces it doesn't have (other models', no vector)."""
    if not len(ids):
        return np.full(len(faces), -1)
    pos = np.minimum(np.searchsorted(ids, faces), len(ids) - 1)
    return np.where(ids[pos] == faces, pos, -1)


def unit_centres(keys: np.ndarray, rows: np.ndarray, vecs: np.ndarray, chunk: int = 2048,
                 weights: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """The unit centre of each key's vectors: (keys ascending, centres [keys, dim] float32). rows: each
    item's row in vecs; weights: each item's weight (1 when not given). Sorted by key and summed a chunk
    at a time, as a product with a matrix saying which key each row is (the fastest way numpy has), so
    only a chunk is ever float32 at once."""
    order = np.argsort(keys, kind="stable")
    keys, rows = keys[order], rows[order]
    weights = weights[order].astype(np.float32) if weights is not None else None
    uniq = np.unique(keys)
    out = np.zeros((len(uniq), vecs.shape[1] if vecs.ndim == 2 else 0), dtype=np.float32)
    for a in range(0, len(keys), chunk):
        k = keys[a : a + chunk]
        new = np.r_[True, k[1:] != k[:-1]]
        which = np.cumsum(new) - 1
        onehot = np.zeros((which[-1] + 1, len(k)), dtype=np.float32)
        onehot[which, np.arange(len(k))] = 1.0 if weights is None else weights[a : a + chunk]
        out[np.searchsorted(uniq, k[new])] += onehot @ vecs[rows[a : a + chunk]].astype(np.float32)
    out /= np.linalg.norm(out, axis=1, keepdims=True) + 1e-9
    return uniq, out


def cluster(conn, full: bool | None = None) -> dict:
    """Group faces into people. Two faces in one photo are never put in the same group (they are
    two people), and groups that share a photo are never merged.

    Named people (your assignments) anchor the groups. A complete re-grouping runs when asked, when
    many faces are new, or at most once a day when faces were found or named since the last; otherwise
    only new faces are placed, in batches against the existing groups, so a new photo doesn't re-group
    the whole library. Group ids are stable (a group keeps the id most of its faces had) and never
    reused. What you say about a face while it runs (naming, "not this person") stands (_write_groups)."""
    key = active()
    # Faces found since the last grouping have higher ids (a re-detected face gets a new one).
    # Small faces that matched nobody stay ungrouped without counting as new forever.
    seen = int(db.get_setting(conn, "faces_seen_upto", 0) or 0)
    # Newly chosen models: everything is grouped afresh (each face found again kept its group as a
    # hint, so groups keep their ids) until every photo's faces are found with them.
    switched = db.get_setting(conn, "faces_model", "insightface") != key
    # Nothing new (after most updates, and after naming): told from the newest faces and an index, without
    # reading every face's vector (seconds and over a gigabyte on a library of 300,000 faces).
    if full is None and seen and not switched and not _fresh(conn, seen, key) and not _full_due(conn):
        return {"faces": conn.execute(f"SELECT COUNT(*) FROM faces f WHERE {PRESENT}").fetchone()[0],
                "clusters": conn.execute("SELECT COUNT(*) FROM face_groups").fetchone()[0], "placed": 0}
    names, top = _names_stamp(conn), conn.execute("SELECT COALESCE(MAX(id), 0) FROM faces").fetchone()[0]
    rows, at, vecs = _to_group(conn)
    if not rows:
        return {"faces": 0, "clusters": 0}

    fresh = [i for i, r in enumerate(rows) if r["id"] > seen and r["cluster"] is None]
    if full is None:
        full = not seen or switched or len(fresh) > 0.25 * len(rows) or _full_due(conn)
    if not full and not fresh:
        return {"faces": len(rows), "clusters": len({r["cluster"] for r in rows if r["cluster"] is not None}), "placed": 0}
    out = _cluster_full(conn, rows, at, vecs) if full else _place_new(conn, rows, at, vecs, fresh)
    if full:
        # What this grouping was made from: the next daily one waits for faces or names newer than these.
        db.set_setting(conn, "faces_full_at", time.time())
        db.set_setting(conn, "faces_full_upto", top)
        db.set_setting(conn, "faces_full_names", names)
        if switched and not pending(conn):
            db.set_setting(conn, "faces_model", key)
    db.set_setting(conn, "faces_seen_upto", max(r["id"] for r in rows))
    _cohesion(conn)
    return out


def animal_ids(conn) -> set[int]:
    """Faces of kind 'pet', from the faces_kind index: pets themselves, and faces the face models found
    that are an animal's (pets.py). Neither is grouped with people's faces."""
    return {r[0] for r in conn.execute("SELECT id FROM faces WHERE kind = 'pet'")}


def _to_group(conn) -> tuple[list, np.ndarray, np.ndarray]:
    """The faces to group, the chosen models' faces in photos in the library (not animals'), ids
    ascending: their rows (id, shot_id, px, sharp, person_id, person_src, cluster, from the faces_by_group
    index rather than the faces' own rows), each one's row in vectors(), and those vectors."""
    ids, vecs = vectors(conn)
    rows = conn.execute("SELECT f.id, f.shot_id, f.px, f.sharp, f.person_id, f.person_src, f.cluster FROM faces f"
                        f" WHERE {PRESENT}").fetchall()
    fid = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
    at = rows_of(ids, fid)
    animals = animal_ids(conn)
    if animals:
        at[np.isin(fid, np.fromiter(animals, dtype=np.int64, count=len(animals)))] = -1
    keep = np.flatnonzero(at >= 0)                       # (other models' faces, and faces with no vector, aren't)
    keep = keep[np.argsort(fid[keep], kind="stable")]
    return [rows[i] for i in keep], at[keep], vecs


def _fresh(conn, seen: int, key: str) -> bool:
    """Whether a face found since the last grouping waits for a group: read from the newest faces' rows
    only (ids only go up)."""
    return conn.execute(f"SELECT 1 FROM faces f WHERE f.id > ? AND f.cluster IS NULL AND f.model = ? AND f.vec IS NOT NULL"
                        f" AND COALESCE(f.kind, '') != 'pet' AND {PRESENT} LIMIT 1", (seen, key)).fetchone() is not None


def _names_stamp(conn) -> list:
    """What you've said about faces (names, and "not this person"), as two numbers that change when any
    of it does (from the faces_by_person index)."""
    n, s = conn.execute("SELECT COUNT(*), COALESCE(SUM(id * (person_id + 2)), 0) FROM faces"
                        " WHERE person_id IS NOT NULL AND person_src = 'user'").fetchone()
    return [n, s]


def _full_due(conn) -> bool:
    """The daily complete regrouping is due: a day since the last, and faces found or names given (or
    taken off) since. With neither it would give the same groups again, after minutes under the update
    lock on a big library."""
    if time.time() - float(db.get_setting(conn, "faces_full_at", 0) or 0) <= FULL_EVERY_S:
        return False
    upto = db.get_setting(conn, "faces_full_upto")
    if upto is None or conn.execute("SELECT COALESCE(MAX(id), 0) FROM faces").fetchone()[0] > upto:
        return True
    return db.get_setting(conn, "faces_full_names") != _names_stamp(conn)


def _write_groups(conn, updates: list[tuple], was: dict[int, tuple]) -> int:
    """Write a grouping: (cluster, person_id, person_src, face id) for each face, as it worked them out
    from the faces as they were (was: each face's (cluster, person_id, person_src) then). A complete
    regrouping takes minutes on a big library, and what you said about a face meanwhile (named it or
    its group, took a name off, "not this person", merged two people) stands: each face's name is read
    again under the write lock, and a face whose name changed keeps it. A person gone meanwhile (merged
    into someone, forgotten) is given no faces. Only faces whose group or name changes are written.
    Returns how many."""
    conn.commit()
    db.write_lock(conn)
    now = {fid: (pid, src) for fid, pid, src in conn.execute(
        "SELECT id, person_id, person_src FROM faces WHERE person_id IS NOT NULL")}
    people = {r[0] for r in conn.execute("SELECT id FROM people")}
    out = []
    for cid, pid, src, fid in updates:
        cl, *then = was[fid]
        cur = now.get(fid, (None, None))
        if cur != tuple(then):
            pid, src = cur                                   # yours, since the grouping read it
        elif src == "auto" and pid not in people:
            pid = src = None
        if (cid, pid, src) != (cl, *cur):
            out.append((cid, pid, src, fid))
    conn.executemany("UPDATE faces SET cluster=?, person_id=?, person_src=? WHERE id=?", out)
    conn.commit()
    return len(out)


def _new_cluster_id(conn) -> int:
    from . import db

    hi = conn.execute("SELECT COALESCE(MAX(cluster), 0) FROM faces").fetchone()[0]
    cur = int(db.get_setting(conn, "hwm_clusters", 0) or 0)
    nid = max(hi, cur) + 1
    db.set_setting(conn, "hwm_clusters", nid)
    return nid


def _cluster_full(conn, rows, at: np.ndarray, vecs: np.ndarray, lim: dict | None = None) -> dict:
    """Group every face afresh (rows: as _to_group gives them; at: each one's row in vecs; lim: the
    bars, the chosen models' unless given: pets have their own)."""
    lim = lim or limits()
    n = len(rows)
    dim = vecs.shape[1]
    ids = np.array([r["id"] for r in rows])
    shot = np.array([r["shot_id"] for r in rows])
    # Biggest and sharpest first; between equals, the face found first (rows are by id), so the groups
    # don't depend on the order the catalog happens to read the faces in.
    order = np.argsort([-(r["px"] * min(r["sharp"] or 0, 500)) for r in rows], kind="stable")
    labels = np.full(n, -1)
    cap = 1024
    C = np.zeros((cap, dim), dtype=np.float32)          # running sums
    norms = np.zeros(cap, dtype=np.float32)
    shots_in: list[set] = []
    k = 0
    person_of_cluster: dict[int, int] = {}
    cluster_of_person: dict[int, int] = {}

    def new_cluster(v) -> int:
        nonlocal C, norms, cap, k
        if k == cap:
            cap *= 2
            C = np.vstack([C, np.zeros_like(C)])
            norms = np.concatenate([norms, np.zeros_like(norms)])
        C[k] = v
        norms[k] = float(np.linalg.norm(v))
        shots_in.append(set())
        k += 1
        return k - 1

    # Faces you assigned seed one group per person.
    seeds = [i for i, r in enumerate(rows) if r["person_src"] == "user" and r["person_id"] and r["person_id"] > 0]
    for i, v in zip(seeds, _unit(vecs, at[seeds])):
        r = rows[i]
        c = cluster_of_person.get(r["person_id"])
        if c is None:
            c = new_cluster(np.zeros(dim, dtype=np.float32))
            cluster_of_person[r["person_id"]] = c
            person_of_cluster[c] = r["person_id"]
        labels[i] = c
        C[c] += v
        norms[c] = float(np.linalg.norm(C[c]))
        shots_in[c].add(int(shot[i]))
    locked_out = {r["id"] for r in rows if r["person_src"] == "user" and (r["person_id"] or 0) <= 0}

    for a0 in range(0, n, 4096):                         # (the vectors as float32 a chunk at a time)
        chunk = order[a0 : a0 + 4096]
        for i, v in zip(chunk, _unit(vecs, at[chunk])):
            if labels[i] >= 0:
                continue
            chosen = -1
            if k:
                sims = (C[:k] @ v) / (norms[:k] + 1e-9)
                for c in _best(sims, lim["cluster"]):
                    if int(shot[i]) in shots_in[c] or (ids[i] in locked_out and int(c) in person_of_cluster):
                        continue          # already someone else in this photo: a different person
                    chosen = int(c)
                    break
            if chosen >= 0:
                labels[i] = chosen
            elif rows[i]["px"] >= MIN_CLUSTER_PX or not k:
                chosen = labels[i] = new_cluster(np.zeros(dim, dtype=np.float32))
            else:
                continue
            C[chosen] += v
            norms[chosen] = float(np.linalg.norm(C[chosen]))
            shots_in[chosen].add(int(shot[i]))

    # Merge near-identical groups: never two different named people, never two groups that share
    # a photo. Only candidate pairs above the bar are visited.
    parent = list(range(k))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    if k > 1:
        Cn = C[:k] / (norms[:k, None] + 1e-9)
        pairs = []
        for a0 in range(0, k, 2048):
            S = Cn[a0 : a0 + 2048] @ Cn.T
            ii, jj = np.nonzero(S >= lim["merge"])
            pairs += [(float(S[a, b]), int(a + a0), int(b)) for a, b in zip(ii, jj) if b > a + a0]
        pairs.sort(reverse=True)                  # the most alike first
        for _, a, b in pairs:
            ra, rb = find(a), find(b)
            pa, pb = person_of_cluster.get(ra), person_of_cluster.get(rb)
            if ra == rb or (pa and pb and pa != pb) or shots_in[ra] & shots_in[rb]:
                continue
            parent[rb] = ra
            shots_in[ra] |= shots_in[rb]
            if pb and not pa:
                person_of_cluster[ra] = pb
    labels = np.array([find(int(l)) if l >= 0 else -1 for l in labels])

    # Stable ids: each group takes the old id most of its faces had; new groups get fresh ids.
    from collections import Counter

    votes: dict[int, Counter] = {}
    for i, r in enumerate(rows):
        if labels[i] >= 0 and r["cluster"] is not None:
            votes.setdefault(int(labels[i]), Counter())[r["cluster"]] += 1
    mapping: dict[int, int] = {}
    taken: set[int] = set()
    for lab in sorted(set(int(x) for x in labels if x >= 0), key=lambda l: -sum(votes.get(l, Counter()).values())):
        old = next((c for c, _ in votes.get(lab, Counter()).most_common() if c not in taken), None)
        mapping[lab] = old if old is not None else _new_cluster_id(conn)
        taken.add(mapping[lab])

    # Unnamed faces in a named group are auto-assigned to that person.
    updates = []
    for i, r in enumerate(rows):
        lab = int(labels[i])
        cid = mapping.get(lab) if lab >= 0 else None
        if r["person_src"] == "user":
            updates.append((cid, r["person_id"], "user", int(ids[i])))
        else:
            pid = person_of_cluster.get(lab) if lab >= 0 else None
            updates.append((cid, pid, "auto" if pid else None, int(ids[i])))
    _write_groups(conn, updates, {r["id"]: (r["cluster"], r["person_id"], r["person_src"]) for r in rows})
    return {"faces": n, "clusters": len(mapping), "full": True}


def _place_new(conn, rows, at: np.ndarray, vecs: np.ndarray, fresh: list[int]) -> dict:
    """Put new faces (fresh: their places in rows) into the existing groups, a batch at a time (one
    matrix product per batch); faces that fit nowhere start groups of their own."""
    olds = [i for i, r in enumerate(rows) if r["cluster"] is not None]
    if not olds:
        return _cluster_full(conn, rows, at, vecs)
    bar = limits()["cluster"]
    old = [rows[i] for i in olds]
    labels_old = np.array([r["cluster"] for r in old])
    groups = np.unique(labels_old)
    gi = {int(g): k for k, g in enumerate(groups)}
    which = np.array([gi[int(c)] for c in labels_old])
    C = np.zeros((len(groups), vecs.shape[1]), dtype=np.float32)
    for a in range(0, len(olds), 32768):                # (float32 a chunk at a time; added in the same order)
        np.add.at(C, which[a : a + 32768], _unit(vecs, at[olds[a : a + 32768]]))
    Cn = C / (np.linalg.norm(C, axis=1, keepdims=True) + 1e-9)
    shots_in: dict[int, set] = {}
    person: dict[int, tuple] = {}
    for r in old:
        shots_in.setdefault(r["cluster"], set()).add(r["shot_id"])
        if r["person_id"] and r["person_id"] > 0:
            person[r["cluster"]] = (r["person_id"], "auto")
    fresh = sorted(fresh, key=lambda i: -(rows[i]["px"] * min(rows[i]["sharp"] or 0, 500)))
    new = [rows[i] for i in fresh]
    locked_out = {r["id"] for r in new if r["person_src"] == "user" and (r["person_id"] or 0) <= 0}
    Vn = _unit(vecs, at[fresh])
    updates, leftovers = [], []
    for b0 in range(0, len(new), 2048):
        S = Vn[b0 : b0 + 2048] @ Cn.T if len(groups) else np.zeros((min(2048, len(new) - b0), 0))
        for j in range(S.shape[0]):
            r = new[b0 + j]
            chosen = None
            for k in _best(S[j], bar):
                g = int(groups[k])
                if r["shot_id"] in shots_in[g] or (r["id"] in locked_out and g in person):
                    continue
                chosen = g
                break
            if chosen is None:
                leftovers.append((b0 + j, r))
                continue
            shots_in[chosen].add(r["shot_id"])
            pid, src = person.get(chosen, (None, None))
            updates.append((chosen, pid if r["person_src"] != "user" else r["person_id"],
                            src if r["person_src"] != "user" else "user", r["id"]))
    # The rest group among themselves (few of them): greedy, with the same one-photo rule.
    made: list[tuple[np.ndarray, set, int]] = []
    for j, r in leftovers:
        v = Vn[j]
        target = None
        for idx, (cv, shots, cid) in enumerate(made):
            if float(cv @ v) / (np.linalg.norm(cv) + 1e-9) >= bar and r["shot_id"] not in shots:
                target = idx
                break
        if target is None:
            if r["px"] < MIN_CLUSTER_PX:
                continue
            made.append((v.copy(), {r["shot_id"]}, _new_cluster_id(conn)))
            target = len(made) - 1
        else:
            cv, shots, cid = made[target]
            made[target] = (cv + v, shots | {r["shot_id"]}, cid)
        updates.append((made[target][2], r["person_id"] if r["person_src"] == "user" else None,
                        "user" if r["person_src"] == "user" else None, r["id"]))
    _write_groups(conn, updates, {r["id"]: (r["cluster"], r["person_id"], r["person_src"]) for r in new})
    return {"faces": len(rows), "clusters": len(groups) + len(made), "placed": len(updates), "full": False}


def place(conn, face_ids) -> int:
    """Put these faces, if in no group, into the groups they fit (as new faces are placed, _place_new):
    faces put back among people's (pets.put_back). Returns how many found a group."""
    want = {int(x) for x in face_ids}
    rows, at, vecs = _to_group(conn)
    fresh = [i for i, r in enumerate(rows) if r["id"] in want and r["cluster"] is None]
    if not fresh:
        return 0
    return _place_new(conn, rows, at, vecs, fresh)["placed"]


def _cohesion(conn) -> None:
    """How well each group's faces agree, and how many photos have two of its faces (which would
    mean two people): People sets loose groups aside as "unclear faces". Who is in which group comes
    from the faces_by_group index, and their vectors from vectors()."""
    ids, vecs = vectors(conn)
    rows = conn.execute(f"SELECT f.id, f.cluster, f.shot_id, COALESCE(f.sharp, 0) FROM faces f"
                        f" WHERE f.cluster IS NOT NULL AND {PRESENT}").fetchall()
    a = np.fromiter(itertools.chain.from_iterable(rows), dtype=np.float64, count=4 * len(rows)).reshape(-1, 4)
    fid, cl, shot = a[:, 0].astype(np.int64), a[:, 1].astype(np.int64), a[:, 2].astype(np.int64)
    at = rows_of(ids, fid)
    keep = np.flatnonzero(at >= 0)                       # the chosen models' faces
    keep = keep[np.lexsort((fid[keep], cl[keep]))]       # by group
    cl, shot, sharp, at = cl[keep], shot[keep], a[keep, 3], at[keep]
    starts = np.flatnonzero(np.r_[True, cl[1:] != cl[:-1]]) if len(cl) else np.zeros(0, dtype=np.int64)
    out = []
    for i, j in zip(starts, np.r_[starts[1:], len(cl)].astype(np.int64)):
        V = _unit(vecs, at[i:j])
        cen = V.sum(0)
        cen /= np.linalg.norm(cen) + 1e-9
        _, per_shot = np.unique(shot[i:j], return_counts=True)
        out.append((int(cl[i]), int(j - i), float(np.median(V @ cen)), int((per_shot > 1).sum()),
                    float(np.median(sharp[i:j]))))
    # (Pets' groups are measured with pets' vectors, by pets.cluster, and kept: from the faces_kind index.)
    conn.execute("DELETE FROM face_groups WHERE cluster NOT IN (SELECT cluster FROM faces WHERE kind = 'pet'"
                 " AND cluster IS NOT NULL)")
    conn.executemany("INSERT OR REPLACE INTO face_groups(cluster, faces, cohesion, shared_photos, sharp) VALUES (?,?,?,?,?)", out)
    conn.commit()


def clear_group(cohesion: float | None, sharp: float | None) -> bool:
    """Whether a group's faces (their median likeness to its centre, and how sharp they are) are one
    person rather than a mix of blurred, turned or different faces."""
    lim = limits()
    return cohesion is not None and not (cohesion < lim["unclear"] or
                                         (cohesion < lim["unclear_soft"] and (sharp or 0) < UNCLEAR_SOFT_SHARP))


def unclear_groups(conn) -> set[int]:
    """Unnamed groups that are a mix of blurred, partial or different faces rather than one person."""
    lim = limits()
    return {r[0] for r in conn.execute(
        "SELECT g.cluster FROM face_groups g WHERE g.faces >= 8 AND (g.cohesion < ? OR (g.cohesion < ? AND g.sharp < ?))"
        " AND NOT EXISTS (SELECT 1 FROM faces f WHERE f.cluster = g.cluster AND f.person_src = 'user')",
        (lim["unclear"], lim["unclear_soft"], UNCLEAR_SOFT_SHARP))}


def name_cluster(conn, cluster_id: int, person_id: int) -> int:
    """Name an unnamed group right away, and pull in other groups that are clearly the same person
    (their centres as close as a merge would need). A group in a photo with the person already (the
    named group included) is someone else, as in the grouping: two faces in one photo are two
    people. The full re-clustering can follow later."""
    conn.execute("UPDATE faces SET person_id=?, person_src='user' WHERE cluster=? AND"
                 " (person_id IS NULL OR person_id <= 0 OR person_src='auto') AND"
                 " NOT (COALESCE(person_src, '') = 'user' AND COALESCE(person_id, 0) = -1)", (person_id, cluster_id))
    # The groups that could join (nobody's, or this person's by the grouping) and the named one, from the
    # faces_by_group index; their vectors from vectors() (the chosen models' faces only, or a pet group's
    # from pets.vectors: only other pets' groups can join it).
    from . import pets

    pet = pets.is_pet_group(conn, cluster_id)
    ids, vecs = pets.vectors(conn) if pet else vectors(conn)
    rows = conn.execute("SELECT id, cluster, shot_id FROM faces WHERE cluster IS NOT NULL AND"
                        " ((person_id IS NULL OR person_id <= 0 OR person_id = ?) AND COALESCE(person_src, '') != 'user'"
                        " OR cluster = ?)", (person_id, cluster_id)).fetchall()
    a = np.fromiter(itertools.chain.from_iterable(rows), dtype=np.int64, count=3 * len(rows)).reshape(-1, 3)
    at = rows_of(ids, a[:, 0])
    a, at = a[at >= 0], at[at >= 0]
    if not (a[:, 1] == cluster_id).any():
        conn.commit()
        return 0
    keys, centres = unit_centres(a[:, 1], at, vecs)
    i = int(np.searchsorted(keys, cluster_id))
    me = centres[i]
    sims = centres @ me
    sims[i] = -1.0
    # The photos the person is in, and which groups those faces are in (None: a face named on its own).
    # (For a pet, its own boxes only: a dog's face the face models found in the same photo is the same dog.)
    theirs: dict[int, set] = {}
    for sid, cl in conn.execute("SELECT shot_id, cluster FROM faces WHERE person_id=?" + (" AND model = ?" if pet else ""),
                                (person_id, pets.MODEL) if pet else (person_id,)):
        theirs.setdefault(sid, set()).add(cl)
    order = np.argsort(-sims, kind="stable")
    bar = limits(pets.MODEL if pet else None)["merge"]
    joined = 0
    for j in order:                              # the most alike first
        if sims[j] < bar:
            break
        cid = int(keys[j])
        shots = set(a[a[:, 1] == cid, 2].tolist())
        if any(theirs.get(sid, set()) - {cid} for sid in shots):
            continue                             # the person is already in one of its photos
        conn.execute("UPDATE faces SET person_id=?, person_src='auto' WHERE cluster=? AND"
                     " (person_id IS NULL OR person_id <= 0) AND COALESCE(person_src, '') != 'user'", (person_id, cid))
        for sid in shots:
            theirs.setdefault(sid, set()).add(cid)
        joined += 1
    conn.commit()
    return joined


# ---------------------------------------------------------------- the same person across the years
#
# A child's face changes year by year, so the grouping splits them into one group per age. Two
# groups are offered as the same person when they don't overlap in time, are never in the same
# photo, and the faces nearest in time on either side (the person's earliest year and the group's
# latest, say) look alike: a lower bar than for faces of the same age, because the years between
# are what changed them.

YEAR = 365.25 * 86400
# limits()["edge"]: how alike the faces nearest in time must be, across the gap (centre and face by face)
EDGE_YEARS = 1.5         # how much of each side counts as its edge
MAX_GAP_YEARS = 4.0      # beyond this, too much has changed to judge
MIN_PHOTOS = 3


def _units(conn) -> tuple[dict, dict]:
    """Each named person's and each unnamed group's faces in dated photos: their ids, rows in vectors(),
    years, clearness and photos, and the span of years most of them are in."""
    ids, vecs = vectors(conn)
    # When each photo was taken (photos in the library with a capture date), and each big enough face
    # (from the faces_by_group index), put together here rather than joined face by face.
    dated = conn.execute("SELECT id, eff_ts FROM shots WHERE status='present' AND eff_ts IS NOT NULL"
                         " AND COALESCE(time_src, 'exif') != 'file' ORDER BY id").fetchall()
    d = np.fromiter(itertools.chain.from_iterable(dated), dtype=np.float64, count=2 * len(dated)).reshape(-1, 2)
    rows = conn.execute(
        f"SELECT f.id, f.shot_id, COALESCE(f.cluster, {NO_GROUP}), COALESCE(f.person_id, 0), COALESCE(f.person_src = 'user', 0),"
        " COALESCE(f.px, 0) * MIN(COALESCE(f.sharp, 0), 400) * (1 - 0.5 * COALESCE(f.closed, 0)) FROM faces f"
        " WHERE COALESCE(f.px, 0) >= ?", (MIN_CLUSTER_PX,)).fetchall()
    a = np.fromiter(itertools.chain.from_iterable(rows), dtype=np.float64, count=6 * len(rows)).reshape(-1, 6)
    fid, shot, pid = a[:, 0].astype(np.int64), a[:, 1].astype(np.int64), a[:, 3].astype(np.int64)
    hidden = np.array([r[0] for r in conn.execute("SELECT id FROM people WHERE COALESCE(hidden, 0) != 0")], dtype=np.int64)
    when = rows_of(d[:, 0].astype(np.int64), shot)
    at = rows_of(ids, fid)
    keep = (at >= 0) & (when >= 0) & ~np.isin(pid, hidden)    # the chosen models' faces in dated photos, not hidden people's
    a, fid, at, shot, pid, when = a[keep], fid[keep], at[keep], shot[keep], pid[keep], when[keep]
    cl = a[:, 2].astype(np.int64)
    cols = (fid, at, shot, d[when, 1] / YEAR, a[:, 5])
    return (_units_of(pid, (pid > 0) & (a[:, 4] > 0), *cols),
            _units_of(cl, (pid <= 0) & (cl != NO_GROUP), *cols))


def _units_of(key, mask, fid, at, shot, ts, q) -> dict:
    """The units (people, or groups) with faces in at least MIN_PHOTOS photos, as arrays sorted by unit
    and then time: each unit is rows starts[i]:ends[i], with its span of years (lo and hi: the 5th and
    95th percentiles) and its middle year, worked out for all units at once, as numpy would one by one."""
    idx = np.flatnonzero(mask)
    idx = idx[np.lexsort((ts[idx], key[idx]))]
    k = key[idx]
    first = np.r_[True, k[1:] != k[:-1]] if len(k) else np.zeros(0, dtype=bool)
    unit = np.cumsum(first) - 1
    keys = k[first]
    pairs = np.unique(unit * (int(shot.max(initial=0)) + 1) + shot[idx])        # each unit's photos, once
    photos = np.bincount(pairs // (int(shot.max(initial=0)) + 1), minlength=len(keys))
    good = photos >= MIN_PHOTOS
    rows_kept = good[unit]
    idx, unit = idx[rows_kept], (np.cumsum(good) - 1)[unit[rows_kept]]
    keys, photos = keys[good], photos[good]
    starts = np.flatnonzero(np.r_[True, unit[1:] != unit[:-1]]) if len(unit) else np.zeros(0, dtype=np.int64)
    ends = np.r_[starts[1:], len(unit)].astype(np.int64) if len(unit) else np.zeros(0, dtype=np.int64)
    n = ends - starts
    t = ts[idx]
    half = starts + n // 2                              # (np.median: the middle one, or the two middle ones' mean)
    med = np.where(n % 2 == 1, t[half], (t[half - 1] + t[half]) / 2) if len(t) else np.zeros(0)
    return {"keys": keys, "pos": {int(x): i for i, x in enumerate(keys)}, "starts": starts, "ends": ends, "n": n,
            "fid": fid[idx], "rows": at[idx], "shot": shot[idx], "ts": t, "q": q[idx], "photos": photos,
            "lo": _percentile(t, starts, n, 5), "hi": _percentile(t, starts, n, 95), "med": med}


def _percentile(t, starts, n, p):
    """np.percentile(..., p) of each sorted run t[starts[i]:starts[i]+n[i]], computed the way numpy does."""
    q = p / 100
    virt = n * q + (1 + q * (1 - 1 - 1)) - 1
    prev = np.floor(virt)
    above, below = virt >= n - 1, virt < 0
    lo = np.where(above, n - 1, np.where(below, 0, prev)).astype(np.int64)
    hi = np.where(above, n - 1, np.where(below, 0, prev + 1)).astype(np.int64)
    gamma = virt - np.where(above, -1, np.where(below, 0, prev))
    if not len(t):
        return np.zeros(len(starts))
    a, b = t[starts + lo], t[starts + hi]
    d = b - a
    return np.where(gamma >= 0.5, b - d * (1 - gamma), a + d * gamma)


def _unit_vecs(vecs: np.ndarray, rows: np.ndarray) -> np.ndarray:
    v = vecs[rows].astype(np.float32)
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)


def _edge_mask(u: dict, i: int, side: str) -> slice | np.ndarray:
    s = slice(u["starts"][i], u["ends"][i])
    ts = u["ts"][s]
    return ts <= u["lo"][i] + EDGE_YEARS if side == "early" else ts >= u["hi"][i] - EDGE_YEARS


def _edge(u: dict, i: int, side: str) -> tuple[np.ndarray, np.ndarray]:
    """The faces at one end of unit i's span: (their rows in vectors(), at most 300; the clearest
    ones' ids)."""
    s = slice(u["starts"][i], u["ends"][i])
    m = _edge_mask(u, i, side)
    return u["rows"][s][m][:300], u["fid"][s][m][np.argsort(-u["q"][s][m], kind="stable")][:3]


def _edge_centres(u: dict, side: str, vecs: np.ndarray) -> np.ndarray:
    """The unit centre of each unit's faces at one end of its span (each face's vector made unit
    first), for all units in one pass: [units, dim]."""
    if not len(u["keys"]):
        return np.zeros((0, vecs.shape[1]), dtype=np.float32)
    unit = np.repeat(np.arange(len(u["keys"])), u["n"])
    lim = np.repeat(u["lo"] + EDGE_YEARS if side == "early" else u["hi"] - EDGE_YEARS, u["n"])
    m = u["ts"] <= lim if side == "early" else u["ts"] >= lim
    rows = u["rows"][m]
    keys, c = unit_centres(unit[m], rows, vecs, weights=inverse_norms(vecs)[rows])
    out = np.zeros((len(u["keys"]), vecs.shape[1]), dtype=np.float32)
    out[keys] = c
    return out


def across_years(conn, limit: int = 30) -> list[dict]:
    """Unnamed groups that may be someone you've named, younger or older: [{person, cluster,
    direction, gap_years, similarity, person_faces, group_faces, group_years}], likeliest first."""
    if not conn.execute("SELECT 1 FROM faces f JOIN people p ON p.id = f.person_id WHERE f.person_id > 0"
                        " AND f.person_src = 'user' AND COALESCE(p.hidden, 0) = 0 LIMIT 1").fetchone():
        return []                       # nobody named (from the faces_by_person index): no one to compare with
    P, G = _units(conn)
    if not len(P["keys"]) or not len(G["keys"]):
        return []
    vecs = vectors(conn)[1]
    bar = limits()["edge"]
    no = {(r[0], r[1]) for r in conn.execute("SELECT person_id, cluster FROM face_not_same")}
    # Everything cheap is decided for all pairs at once (every group against every person); only the
    # pairs whose centres are alike enough to pass get the face-by-face comparison.
    P_lo, P_hi = P["lo"], P["hi"]
    P_early, P_late = _edge_centres(P, "early", vecs), _edge_centres(P, "late", vecs)
    G_early, G_late = _edge_centres(G, "early", vecs), _edge_centres(G, "late", vecs)
    unit = np.repeat(np.arange(len(G["keys"])), G["n"])
    # How much of each group is inside each person's span: its faces up to the span's end, less those
    # before its start (the groups' times are sorted within each group).
    upto = np.stack([np.bincount(unit, weights=G["ts"] <= x, minlength=len(G["keys"])) for x in P_hi])
    before = np.stack([np.bincount(unit, weights=G["ts"] < x, minlength=len(G["keys"])) for x in P_lo])
    inside = (upto - before) / G["n"]
    young = G["med"][None, :] < P_lo[:, None]
    gaps = np.where(young, P_lo[:, None] - G["hi"][None, :], G["lo"][None, :] - P_hi[:, None])
    centre = np.where(young, P_early @ G_late.T, P_late @ G_early.T)
    ok = (inside <= 0.25) & (gaps <= MAX_GAP_YEARS) & (centre >= bar)
    best: dict[int, dict] = {}
    for i, j in zip(*np.nonzero(ok)):           # person by person, as before: the first best match wins
        pid, cid = int(P["keys"][i]), int(G["keys"][j])
        if (pid, cid) in no:
            continue
        if set(P["shot"][P["starts"][i]:P["ends"][i]].tolist()) & set(G["shot"][G["starts"][j]:G["ends"][j]].tolist()):
            continue            # in the same photo: two different people
        younger, gap = bool(young[i, j]), float(gaps[i, j])
        pe, pf = _edge(P, i, "early" if younger else "late")
        ce, cf = _edge(G, j, "late" if younger else "early")
        pv = P_early[i] if younger else P_late[i]
        cv = G_late[j] if younger else G_early[j]
        # Both the centres and the faces one by one (each of the group's faces against its closest
        # match on the other side) must agree: a centre alone can be pulled by a few odd faces.
        sim = min(float(pv @ cv), float(np.median((_unit_vecs(vecs, ce) @ _unit_vecs(vecs, pe).T).max(axis=1))))
        if sim < bar or (cid in best and best[cid]["similarity"] >= sim):
            continue
        best[cid] = {"person": pid, "cluster": cid, "direction": "younger" if younger else "older",
                     "gap_years": round(max(0.0, gap), 1), "similarity": round(sim, 3), "photos": int(G["photos"][j]),
                     "person_faces": [int(x) for x in pf[:3]], "group_faces": [int(x) for x in cf[:3]],
                     "group_span": [G["lo"][j] * YEAR, G["hi"][j] * YEAR], "person_span": [P_lo[i] * YEAR, P_hi[i] * YEAR]}
    return sorted(best.values(), key=lambda x: (-x["similarity"], -x["photos"]))[:limit]


def not_same(conn, person_id: int, cluster: int) -> None:
    """You said a group isn't this person at another age: don't suggest it again."""
    import time

    conn.execute("INSERT OR IGNORE INTO face_not_same(person_id, cluster, ts) VALUES (?,?,?)", (person_id, cluster, time.time()))
    conn.commit()
