"""JoeyLab: non-destructive photo culling and organisation. Made by Yaduraj Bhageria (MIT licence)."""

from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("joeylab")
except _metadata.PackageNotFoundError:          # running from a source tree that isn't installed
    __version__ = "0.9.0"
__author__ = "Yaduraj Bhageria"


UNSUPPORTED = "JoeyLab runs on macOS, and partly on Linux. Windows isn't supported."


def main() -> None:
    import sys

    if sys.platform == "win32":
        sys.exit(UNSUPPORTED)
    try:
        import cv2

        cv2.imdecode
    except (ImportError, AttributeError):
        # Two OpenCV packages once shared one cv2 folder; removing either one leaves it half there.
        sys.exit("OpenCV isn't installed properly. Reinstall it, for example: "
                 "uv sync --all-extras --reinstall-package opencv-contrib-python")
    from .cli import main as _main

    _main()
