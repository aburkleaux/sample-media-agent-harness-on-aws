"""Regression: a generic octet-stream declared type must never beat the extension.

A boto3 upload with no ContentType stores application/octet-stream. When that generic
type was treated as authoritative, a .pdf classified as non-renderable and the render
fell back to a stale video. mime_for now ignores generic types in favour of the
extension (mirroring the artifact-serving proxy).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from render.types import mime_for, is_renderable  # noqa: E402


def test_octet_stream_declared_falls_back_to_extension():
    assert mime_for("nba_summary.pdf", "application/octet-stream") == "application/pdf"
    assert mime_for("nba_summary.pdf", "binary/octet-stream") == "application/pdf"
    assert mime_for("clip.mp4", "application/octet-stream") == "video/mp4"


def test_octet_stream_pdf_is_renderable():
    assert is_renderable("nba_summary.pdf", "application/octet-stream") is True


def test_specific_declared_mime_still_wins():
    # a real producer content-type is trusted over the extension
    assert mime_for("track.wav", "audio/mpeg") == "audio/mpeg"
    assert mime_for("data.bin", "application/pdf") == "application/pdf"


def test_generic_declared_with_params_is_ignored():
    assert mime_for("doc.pdf", "application/octet-stream; charset=binary") == "application/pdf"


def test_unknown_extension_keeps_generic_declared():
    # no extension signal → the generic declared type is the best we have
    assert mime_for("mystery", "application/octet-stream") == "application/octet-stream"


def test_extension_only_unchanged():
    assert mime_for("x.pdf") == "application/pdf"
    assert mime_for("x.wav") == "audio/wav"
