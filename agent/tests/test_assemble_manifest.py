"""Unification of the render agent's two manifest orchestrators.

`assemble_manifest` is the ONE canonical assembler (selection + build + identity), called by both
the `build_render_manifest` tool AND the deterministic entrypoint. `derive_source_key` is the ONE
authority for a manifest's `sourceKey`. These pin the behavior that was broken: real manifests had
`sourceKey: null` because the entrypoint path never stamped it, so `composeTimeline` never merged
analyses of one source onto one timeline. NOTE: the live end-to-end merge (real M2C job → render →
tab merge) is only verifiable on staging; these lock the deterministic assembly + identity.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import render_agent as ra  # noqa: E402
from render.review import _norm_url  # noqa: E402


def test_derive_source_key_prefers_source_then_media_then_candidate():
    # source video is the identity when present (analysis renders → merge onto the source)
    assert ra.derive_source_key(["s3://b/movie.mp4"], ["s3://b/out.wav"], ["s3://b/x.json"]) == _norm_url("s3://b/movie.mp4")
    # else the media deliverable (a clip is its own timeline)
    assert ra.derive_source_key([], ["s3://b/out.wav"], ["s3://b/x.json"]) == _norm_url("s3://b/out.wav")
    # else the first candidate
    assert ra.derive_source_key([], [], ["s3://b/x.json"]) == _norm_url("s3://b/x.json")
    # nothing → empty (never blocks the write)
    assert ra.derive_source_key([], [], []) == ""


def test_derive_source_key_clip_with_source_urls_keys_to_itself_not_the_original():
    """THE adc293de regression: a clip/transcode is dispatched WITH the original as source_urls,
    but it is a NEW source — its OWN video must win (has_analysis=False) so later analyses of the
    CLIP group onto it, and it does NOT wrongly merge with the original it was cut from."""
    key = ra.derive_source_key(["s3://b/original.mp4"], ["s3://b/c6/proxy/transcoded.mp4"], [],
                               has_analysis=False)
    assert key == _norm_url("s3://b/c6/proxy/transcoded.mp4")
    assert key != _norm_url("s3://b/original.mp4")


def test_derive_source_key_analysis_byproduct_video_never_beats_the_source():
    """A shot render's own segment_0000.mp4 is an analysis byproduct, not the identity — an
    analysis (has_analysis=True) must key to the source video it projects onto (the clip), so
    shots/faces still merge onto the source rather than fragmenting per segment."""
    key = ra.derive_source_key(["s3://b/c6/proxy/transcoded.mp4"], ["s3://b/segment_0000.mp4"], [],
                               has_analysis=True)
    assert key == _norm_url("s3://b/c6/proxy/transcoded.mp4")


def test_same_clip_at_two_storage_roots_gets_ONE_source_key():
    """THE cf3c0ffe regression: the trimmed clip lives at a durable artifacts path (the clip's own
    video render) AND an ephemeral micro-workflow path (what a chained shot/stem analysis ran on).
    Different key strings, SAME exec UUID + basename -> canonicalised to one identity so the clip
    video + its analyses merge into ONE composite tab instead of a solo tab each."""
    ex = "0da9b6c6-1756-5c88-bae6-4d6cea04bf10"
    # clip's own video render (plain media -> own video wins)
    clip = ra.derive_source_key(
        [], [f"https://cdn/artifacts/cf3c0ffe-46d9-4e96-a619-041c3d024382/output/{ex}/meridian_trim.mp4"], [],
        has_analysis=False)
    # shots analysis of that clip (analysis -> keys to its source video, the ephemeral copy)
    shots = ra.derive_source_key(
        [f"https://cdn/micro-workflow/media-processing/{ex}/trim_clip/meridian_trim.mp4"], ["https://cdn/shots.json"], [],
        has_analysis=True)
    assert clip == shots, (clip, shots)
    assert clip == f"{ex}/meridian_trim.mp4"
    # a DIFFERENT clip (different exec UUID) stays separate
    other = ra.derive_source_key(
        [], ["https://cdn/artifacts/s/output/99999999-1756-5c88-bae6-4d6cea04bf10/meridian_trim.mp4"], [],
        has_analysis=False)
    assert other != clip


def test_canonical_identity_leaves_plain_urls_unchanged():
    # no UUID in the path -> normalised url unchanged, so unrelated CDN media never collide
    assert ra.derive_source_key([], ["https://cdn/movie.mp4"], []) == _norm_url("https://cdn/movie.mp4")


def test_assemble_stamps_source_key_for_a_plain_media_render():
    m, _projected, _skill = ra.assemble_manifest(
        artifact_urls=["https://cdn/clip.mp4"], source_urls=[], tool=None, manifest_id="m1")
    assert m.get("sourceKey") == _norm_url("https://cdn/clip.mp4")  # was null before the unification


def test_two_analyses_of_the_same_source_get_the_SAME_source_key():
    """The actual lane-merge fix: shots + faces on ONE video must carry the same sourceKey so the
    UI (composeTimeline) folds them onto ONE composite timeline instead of separate tabs."""
    src = ["https://cdn/movie.mp4"]
    shots, _, _ = ra.assemble_manifest(
        artifact_urls=["https://cdn/shots.json"], source_urls=src, tool=None, manifest_id="a",
        fetched={"https://cdn/shots.json": {"shots": [{"start": 0.0, "end": 1.0}]}})
    faces, _, _ = ra.assemble_manifest(
        artifact_urls=["https://cdn/faces.json"], source_urls=src, tool=None, manifest_id="b",
        fetched={"https://cdn/faces.json": {"faces": [{"id": 0}]}})
    assert shots.get("sourceKey") == _norm_url("https://cdn/movie.mp4")
    assert shots["sourceKey"] == faces["sourceKey"]  # → ONE merged timeline tab


def test_a_derived_clip_gets_its_OWN_source_key_not_the_source():
    """A clip/transcode (its own deliverable, no source_urls) is its own timeline — a DIFFERENT
    sourceKey from the original — so it becomes its own tab, and later analyses of IT group back."""
    clip, _, _ = ra.assemble_manifest(
        artifact_urls=["https://cdn/meridian_trim.mp4"], source_urls=[], tool=None, manifest_id="c")
    assert clip.get("sourceKey") == _norm_url("https://cdn/meridian_trim.mp4")
    assert clip["sourceKey"] != _norm_url("https://cdn/movie.mp4")


def test_assemble_selects_document_for_a_pdf():
    m, _, _ = ra.assemble_manifest(
        artifact_urls=["https://cdn/report.pdf"], source_urls=[], tool=None, manifest_id="d")
    # a document deliverable renders as a document, not the analysed media
    assert m.get("template") == "document-viewer"
    assert m.get("sourceKey")  # still identity-stamped


def test_descriptive_only_sidecar_does_not_render_the_source(monkeypatch):
    """probe.json is the source SPEC — a probe-only render must NOT turn the source into a track
    (the job's INPUT rendered as its output). Ported guard now applies in the unified assembler."""
    monkeypatch.setattr(ra, "_is_descriptive_sidecar", lambda u: u.endswith("probe.json") or u.endswith("metadata.json"))
    m, _, _ = ra.assemble_manifest(
        artifact_urls=["https://cdn/probe.json"], source_urls=["https://cdn/movie.mp4"],
        tool=None, manifest_id="p", fetched={"https://cdn/probe.json": {"format": {}}})
    # no source-video track: the only track (if any) must not be the source movie
    srcs = [t.get("src") for t in (m.get("tracks") or []) if isinstance(t, dict)]
    assert "https://cdn/movie.mp4" not in srcs


def test_build_render_manifest_tool_now_stamps_source_key_via_the_shared_assembler():
    import json
    out = json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps(["https://cdn/clip.mp4"]), manifest_id="t1", session_id="s1"))
    assert out.get("sourceKey")  # the tool path delegates to assemble_manifest → identity stamped
