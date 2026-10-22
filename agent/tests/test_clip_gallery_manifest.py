"""The render agent's independent-clip gallery manifest (clip-gallery template).

Pins the contract the UI ClipGallery reads: template 'clip-gallery' + a `galleryClips`
array (distinct from a timeline manifest's `clips`), https-only srcs, kind inference,
and the id/createdAt the parser requires.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import render_agent as ra  # noqa: E402


def test_builds_gallery_with_kind_inference_and_https_filter():
    items = [
        {"label": "Segment 1", "url": "https://cdn/seg1.wav", "text": "Hi there"},
        {"label": "Take 2", "url": "https://cdn/seg2.mp4", "durationHint": 6.2},
        {"label": "Bad", "url": "s3://bucket/x.wav"},  # not https → dropped
    ]
    m = ra._build_clip_gallery_manifest(items, "mid-1", title="Review", subtitle="Trim each")
    assert m["template"] == "clip-gallery"
    assert m["id"] == "mid-1"
    assert isinstance(m.get("createdAt"), str) and m["createdAt"]
    assert m["title"] == "Review" and m["subtitle"] == "Trim each"
    gc = m["galleryClips"]
    assert len(gc) == 2  # the s3:// entry is filtered out
    assert gc[0] == {"id": "clip1", "label": "Segment 1", "src": "https://cdn/seg1.wav",
                     "kind": "audio", "text": "Hi there"}
    assert gc[1]["kind"] == "video" and gc[1]["durationHint"] == 6.2


def test_explicit_kind_overrides_extension():
    m = ra._build_clip_gallery_manifest(
        [{"label": "X", "url": "https://cdn/a.bin", "kind": "audio"}], "m")
    assert m["galleryClips"][0]["kind"] == "audio"


def test_malformed_joined_url_is_cleaned():
    # The session-3445660b glued-filename shape: keep only the first valid url token.
    m = ra._build_clip_gallery_manifest(
        [{"label": "S1", "url": "https://cdn/seg1.wav\\nseg2.wav", "kind": "audio"}], "m")
    assert m["galleryClips"][0]["src"] == "https://cdn/seg1.wav"


def test_tool_rejects_non_array_and_empty():
    assert json.loads(ra.build_clip_gallery_manifest("{}", "m"))["s3_key"] is None
    assert json.loads(ra.build_clip_gallery_manifest("[]", "m"))["s3_key"] is None
    # a list with no https clips → nothing to render
    out = json.loads(ra.build_clip_gallery_manifest(json.dumps([{"label": "b", "url": "s3://x/y.wav"}]), "m"))
    assert out["s3_key"] is None



def test_multi_video_gallery_is_the_video_review_surface():
    """N independent video takes → ONE clip-gallery of kind:video cards (not N timelines).

    This is the video-review trigger for the core renderer capability: the review step
    assembles all takes into a single gallery the user can play-all, expand, and frame-extract.
    """
    items = [
        {"label": f"Segment {i}", "url": f"https://cdn/seg{i}.mp4",
         "text": f"line {i}", "durationHint": 5.0 + i,
         "status": ("flagged" if i == 2 else None)}
        for i in range(1, 5)
    ]
    m = ra._build_clip_gallery_manifest(items, "reel-1", title="Review lip-sync")
    assert m["template"] == "clip-gallery"
    gc = m["galleryClips"]
    assert len(gc) == 4
    assert all(c["kind"] == "video" for c in gc), "every .mp4 take is a video card"
    # distinct ids preserved in order, each with its own src
    assert [c["id"] for c in gc] == ["clip1", "clip2", "clip3", "clip4"]
    assert gc[0]["src"] == "https://cdn/seg1.mp4"
    # a per-segment gate result rides through as status (the UI flags it)
    assert gc[1]["status"] == "flagged"
    assert gc[0]["durationHint"] == 6.0


def test_mixed_audio_and_video_keep_their_kinds():
    items = [
        {"label": "VO", "url": "https://cdn/a.wav"},
        {"label": "Shot", "url": "https://cdn/v.mp4"},
    ]
    gc = ra._build_clip_gallery_manifest(items, "m")["galleryClips"]
    assert [c["kind"] for c in gc] == ["audio", "video"]
