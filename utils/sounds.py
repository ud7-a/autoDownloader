"""Shared paths and choices for desktop notification sounds."""

import os
import sys

# Distinct from an unset value, which uses the bundled default.
SOUND_NONE = "__none__"


def builtin_sound_path():
    """Absolute path to the app's bundled default finish sound (frozen or source)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "assets", "finishingDownloadSound.wav")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets", "finishingDownloadSound.wav")
