"""A marker/segment skill must win over the images-only gallery branch.

Second root cause of the "wrong view" reports (sessions a2ebd1e9 / a5a1b4fa),
independent of skill selection and found by replaying the real artifacts against a
locally-run render agent:

`_render_deterministic` evaluates `elif image_urls and not media_urls and not doc_urls`
BEFORE `elif timeline_media`. `media_urls` counts only DELIVERABLES; the source video
arrives in `source_urls`. Shot segmentation ships 5 shot_*.jpg thumbnails as
deliverables, so the images-only branch wins and the marker timeline — which needs
`timeline_media = media_urls or source_video_urls` — is never reached.

Observed locally with the real session-a2ebd1e9 artifacts: skill resolved correctly to
render-shot-segmentation (after the tool-keyed fix) yet the manifest was still
template=image-annotated with 0 markers.

The rule: when the selected skill declares a TIMELINE projection (markers/segments/lanes)
and a timeline canvas exists (deliverable media OR a source video), the timeline view wins.
Images become gallery content inside it, not a competing view.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CF = "https://cdn.example.net"
SHOTS = f"{CF}/out/shots.json"
THUMBS = [f"{CF}/out/shot_{i:06d}.jpg" for i in range(5)]
SOURCE = f"{CF}/in/source.mp4"


def _classify(deliverables, sources):
    """Mirror _render_deterministic's url classification."""
    import render_agent as r
    media = [u for u in deliverables
             if r._ext_from_url(u) in r._VIDEO_EXTS or r._ext_from_url(u) in r._AUDIO_EXTS]
    images = [u for u in deliverables if r._ext_from_url(u) in r._IMAGE_EXTS]
    docs = [u for u in deliverables if r._ext_from_url(u) in ("pdf", "html")]
    source_video = [u for u in sources if r._ext_from_url(u) in r._VIDEO_EXTS]
    return media, images, docs, source_video


def test_source_video_provides_a_timeline_canvas_even_with_image_deliverables():
    """The precondition the ordering bug ignores: a timeline canvas DOES exist."""
    media, images, docs, source_video = _classify([SHOTS] + THUMBS, [SOURCE])
    assert media == [], "shot segmentation ships no deliverable video"
    assert len(images) == 5, "it ships thumbnails, which is what hijacks the view"
    timeline_media = media or source_video
    assert timeline_media == [SOURCE], "the source video IS available as the canvas"


def test_marker_skill_must_not_lose_to_the_images_only_branch():
    """The regression: images-only must not pre-empt a declared marker view.

    Exercises the SAME predicate the render path uses (`images_only_view_wins`), so the
    test cannot drift from the branch it guards.
    """
    import render_agent as r
    skill = r.select_render_skill_by_tool("shot-segmentation___get_job_status")
    assert skill and skill["name"] == "render-shot-segmentation"
    decl = skill.get("declaration") or {}
    assert "markers" in decl and skill.get("template") == "nle-timeline"

    media, images, docs, source_video = _classify([SHOTS] + THUMBS, [SOURCE])
    timeline_media = media or source_video

    assert not r.images_only_view_wins(images, media, docs, timeline_media, skill), (
        "images-only branch must not pre-empt a declared timeline view when a source "
        "video canvas is available"
    )


def test_plain_image_generation_still_gets_the_gallery():
    """Guard the case the images-only branch exists for: no timeline skill, no canvas.

    A two-option text-to-image result must still render as a gallery.
    """
    import render_agent as r
    imgs = [f"{CF}/gen/a.png", f"{CF}/gen/b.png"]
    assert r.images_only_view_wins(imgs, [], [], [], None) is True


def test_gallery_still_wins_for_a_timeline_skill_with_no_canvas():
    """A marker skill with NO media canvas must not be pushed into an empty timeline."""
    import render_agent as r
    skill = r.select_render_skill_by_tool("shot-segmentation")
    assert r.images_only_view_wins(THUMBS, [], [], [], skill) is True
