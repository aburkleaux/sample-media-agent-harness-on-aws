"""Regression for the broken raw manifest tab (session 368f1233).

The Render Agent prompt schema is {id,duration,tracks,clips,captions,createdAt,errors} — no
`template`/`sourceKey`. When the model hand-builds that in its text instead of calling
build_render_manifest, the manifest is unroutable and the UI shows a broken raw tab. The pipeline
now repairs it DETERMINISTICALLY (no LLM, so no self-correction latency) before validation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import render_agent as ra  # noqa: E402


def _repair(m: dict) -> dict:
    return json.loads(ra._repair_untemplated_manifest(json.dumps(m)))


# The exact broken shape from session 368f1233.
_BROKEN = {
    "id": "4b4e2fdc19677147892b01d88198ab21",
    "duration": 47.2,
    "tracks": [{"id": "video-0", "type": "video",
                "src": "https://dexamplecdn002.cloudfront.net/artifacts/"
                       "368f1233-893c-45a9-ba80-5f5d682958a0/input/1790712027595-Meridian.mp4"}],
    "clips": [],
    "captions": [],
    "createdAt": "2025-01-03T02:23:00.000Z",
    "errors": [],
}


def test_repairs_a_template_less_media_manifest():
    out = _repair(_BROKEN)
    assert out["template"] == "nle-timeline"          # now routable → no broken tab
    assert isinstance(out.get("sourceKey"), str) and out["sourceKey"]  # folds with the real render
    # the media track is preserved (we don't recompute the box/geometry)
    assert out["tracks"] == _BROKEN["tracks"]


def test_leaves_a_templated_manifest_untouched():
    m = {**_BROKEN, "template": "nle-timeline", "sourceKey": "s/clip.mp4"}
    out = _repair(m)
    assert out["template"] == "nle-timeline"
    assert out["sourceKey"] == "s/clip.mp4"           # not overwritten


def test_never_touches_an_error_manifest():
    """An error manifest has NO template but carries errors[] — the UI routes it to the error
    view, so we must NOT stamp a template (that would hide the error)."""
    m = {"id": "e1", "duration": 0.001, "tracks": [], "clips": [], "captions": [],
         "createdAt": "2026-01-01T00:00:00.000Z",
         "errors": [{"code": "TIMEOUT", "message": "pipeline exceeded budget"}]}
    out = _repair(m)
    assert "template" not in out                      # untouched
    assert out["errors"][0]["code"] == "TIMEOUT"


def test_ignores_a_manifest_with_no_media_tracks():
    """No video/audio track → nothing renderable; leave it alone (don't invent an nle-timeline)."""
    m = {"id": "x", "duration": 1.0, "tracks": [], "clips": [], "captions": [],
         "createdAt": "2026-01-01T00:00:00.000Z", "errors": []}
    out = _repair(m)
    assert "template" not in out


def test_bad_json_passes_through():
    assert ra._repair_untemplated_manifest("not json at all") == "not json at all"
