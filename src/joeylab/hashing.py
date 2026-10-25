"""Content fingerprints and perceptual hashes."""

from __future__ import annotations

import hashlib
import os

import cv2
import numpy as np

_CHUNK = 256 * 1024


def quick_fp(path: str, size: int) -> str:
    """Cheap identity: size + first and last 256 KB. Survives moves and renames."""
    h = hashlib.blake2b(digest_size=16)
    h.update(size.to_bytes(8, "little"))
    with open(path, "rb") as f:
        h.update(f.read(_CHUNK))
        if size > 2 * _CHUNK:
            f.seek(-_CHUNK, os.SEEK_END)
            h.update(f.read(_CHUNK))
    return h.hexdigest()


def full_hash(path: str) -> str:
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _bits_to_int(bits: np.ndarray) -> int:
    v = 0
    for b in bits.flatten():
        v = (v << 1) | int(b)
    return v


def phash(gray: np.ndarray) -> int:
    """64-bit DCT perceptual hash of a grayscale uint8 image."""
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(small)[:8, :8]
    med = np.median(dct.flatten()[1:])
    return _bits_to_int(dct > med)


def dhash(gray: np.ndarray) -> int:
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    return _bits_to_int(small[:, 1:] > small[:, :-1])


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def hamming_many(a: int, arr: np.ndarray) -> np.ndarray:
    """Distances from one hash to an array of uint64 hashes."""
    return np.bitwise_count(arr ^ np.uint64(a))
