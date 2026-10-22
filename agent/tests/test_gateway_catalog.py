"""Tests for the pure gateway tool-catalog normalizer (Wave 2)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.catalog import normalize_tool_metadata  # noqa: E402


def test_maps_each_tool_to_name_description_annotations():
    raw = [
        {"n": "a", "d": "does a", "ann": {"readOnlyHint": True}},
        {"n": "b", "d": "", "ann": None},
    ]
    out = normalize_tool_metadata(
        raw,
        extract_name=lambda t: t["n"],
        extract_description=lambda t: t["d"],
        extract_annotations=lambda t: t["ann"],
    )
    assert out == [
        {"name": "a", "description": "does a", "annotations": {"readOnlyHint": True}},
        {"name": "b", "description": "", "annotations": None},
    ]


def test_empty_tool_list():
    assert normalize_tool_metadata(
        [], extract_name=str, extract_description=str, extract_annotations=lambda t: None
    ) == []
