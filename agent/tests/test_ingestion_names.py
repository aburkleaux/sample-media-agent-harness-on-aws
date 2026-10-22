"""``ingestion.names`` — object-key sanitization and Display_Name derivation (design Properties 4, 5)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion.names import (  # noqa: E402
    DISPLAY_NAME_MAX,
    SANITIZE_MAX,
    derive_display_name,
    sanitize,
    with_suffix,
)


def test_sanitize_allowed_charset_and_idempotent():
    out = sanitize("my file (final)!.mp4")
    assert all(c.isalnum() or c in "._-" for c in out)
    assert sanitize(out) == out  # idempotent


def test_sanitize_keeps_extension():
    assert sanitize("clip.mp4").endswith(".mp4")
    assert sanitize("weird name@#.JPG").endswith(".JPG")


def test_sanitize_length_cap_preserves_extension():
    long_name = "a" * 500 + ".mp4"
    out = sanitize(long_name)
    assert len(out) <= SANITIZE_MAX
    assert out.endswith(".mp4")


def test_sanitize_never_empty():
    assert sanitize("") == "_"
    assert sanitize("///") != ""
    assert sanitize(None) == "_"


def test_derive_display_name_strips_forbidden_and_whitespace():
    name = "  [draft]  @final#  clip.mp4 "
    out = derive_display_name(name, "video")
    for ch in "[]@#":
        assert ch not in out
    assert "  " not in out
    assert out == out.strip()
    assert out.endswith("clip.mp4")


def test_derive_display_name_fallback_untitled():
    assert derive_display_name("[]@#", "image") == "Untitled image"
    assert derive_display_name("", "audio") == "Untitled audio"
    assert derive_display_name("", "document") == "Untitled document"
    assert derive_display_name("   ", "video") == "Untitled video"


def test_derive_display_name_code_point_cap():
    out = derive_display_name("x" * 400 + ".mp4", "video")
    assert len(out) <= DISPLAY_NAME_MAX
    assert out.endswith(".mp4")


def test_with_suffix():
    assert with_suffix("clip.mp4", 1) == "clip.mp4"
    assert with_suffix("clip.mp4", 2) == "clip.mp4 (2)"
    assert with_suffix("clip.mp4", 20) == "clip.mp4 (20)"
    long_base = "y" * 300
    out = with_suffix(long_base, 5)
    assert len(out) <= DISPLAY_NAME_MAX
    assert out.endswith(" (5)")
