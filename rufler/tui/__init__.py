"""Textual-based TUI for rufler.

All TUI modules do a lazy ``import textual`` so the rest of rufler
works fine when textual is not installed.
"""
from __future__ import annotations

import os
import sys


def has_textual() -> bool:
    """Return True if textual is importable AND stdout is a real terminal.

    Returns False when running under test harnesses (CliRunner), pipes,
    or when the RUFLER_NO_TUI env var is set — so commands fall back to
    Rich table output automatically.
    """
    if os.environ.get("RUFLER_NO_TUI"):
        return False
    if not sys.stdout.isatty():
        return False
    try:
        import textual  # noqa: F401
        return True
    except ImportError:
        return False
