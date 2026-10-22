"""The data-aware reviewer (F8) is the correctness guarantee for agent-driven renders.

Reproducibility is not required; "the right elements are on screen for the data" is. With the
render decision agent-driven, nothing structurally forces that -- this reviewer does. It catches
the two failures that shipped: an empty-but-valid manifest (a blank tab), and source data with
boxes rendered without box annotations.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from render.review import review_manifest

# The real empty face manifest shipped in 8e38c66: video-annotations, every list empty.
EMPTY_MANIFEST = {
    "template": "video-annotations", "renderSkill": "render-face-people",
    "tracks": [], "annotations": [], "segments": [], "entities": [],
}

# Real face_tracks.json shape (job 2f5e2a74): boxes under tracks[].faces[].box {l,t,w,h}.
FACE_TRACKS = {"tracks": [
    {"track_id": 0, "faces": [
        {"timestamp": 30.03, "frame_key": "f900.jpg",
         "box": {"l": 0.30, "t": 0.17, "w": 0.14, "h": 0.36}, "confidence": 99.9},
    ]},
]}


def test_empty_manifest_is_flagged():
    problems = review_manifest(EMPTY_MANIFEST)
    assert any("renders nothing" in p for p in problems), problems


def test_boxes_in_data_but_not_in_manifest_is_flagged():
    # A manifest that has a track but NO annotations, while the source has boxes.
    manifest = {"template": "composite-timeline", "tracks": [{"type": "video", "src": "s3://b/v.mp4"}],
                "annotations": []}
    problems = review_manifest(manifest, {"face_tracks.json": FACE_TRACKS})
    assert any("bounding box" in p for p in problems), problems


def test_a_manifest_that_renders_the_boxes_passes():
    manifest = {
        "template": "video-annotations",
        "tracks": [{"type": "video", "src": "s3://b/v.mp4"}],
        "annotations": [{"entityId": "0", "start": 30.0, "end": 31.0,
                         "box": {"x": 0.30, "y": 0.17, "width": 0.14, "height": 0.36}}],
    }
    assert review_manifest(manifest, {"face_tracks.json": FACE_TRACKS}) == []


def test_a_valid_video_manifest_with_no_box_data_passes():
    manifest = {"template": "nle-timeline", "tracks": [{"type": "video", "src": "s3://b/proxy.mp4"}]}
    assert review_manifest(manifest, {"probe.json": {"duration": 60}}) == []


def test_denormalized_box_coords_are_flagged():
    manifest = {"template": "video-annotations", "tracks": [{"type": "video", "src": "s3://b/v.mp4"}],
                "annotations": [{"box": {"x": 304, "y": 173, "width": 140, "height": 355}}]}
    problems = review_manifest(manifest, {"face_tracks.json": FACE_TRACKS})
    assert any("normalized" in p for p in problems), problems


def test_an_error_manifest_is_not_a_review_failure():
    assert review_manifest({"template": "error", "error": "job failed"}) == []


def test_schema_valid_but_empty_is_NOT_saved_by_a_schema_check():
    """The whole point: a schema check passes EMPTY_MANIFEST; the data-aware check does not."""
    assert review_manifest(EMPTY_MANIFEST), "reviewer must reject the empty manifest a schema check passes"


# --- resolved-URL validator (F4 resolve-step guard) ---
from render.review import validate_resolved_urls

CANDIDATES = [
    "s3://proxy/wf/exec/proxy/transcoded.mp4",
    "s3://proxy/wf/exec/probe.json",
]
SOURCE = ["s3://skills/artifacts/1786-Netflix_Open_Content_Meridian.mp4"]


def test_a_real_output_is_valid_even_when_presigned():
    chosen = ["https://proxy.s3.amazonaws.com/wf/exec/proxy/transcoded.mp4?X-Amz-Signature=zz"]
    out = validate_resolved_urls(chosen, CANDIDATES, source_urls=SOURCE)
    assert out["valid"] == chosen and out["rejected"] == []


def test_the_input_is_rejected():
    chosen = ["https://cdn/1786-Netflix_Open_Content_Meridian.mp4"]
    out = validate_resolved_urls(chosen, CANDIDATES, source_urls=SOURCE)
    assert out["valid"] == []
    assert out["rejected"] and "input" in out["rejected"][0]["reason"]


def test_a_hallucinated_url_not_in_outputs_is_rejected():
    chosen = ["s3://proxy/wf/exec/final_output.mp4"]  # never produced
    out = validate_resolved_urls(chosen, CANDIDATES)
    assert out["valid"] == []
    assert "not among the job's actual outputs" in out["rejected"][0]["reason"]


def test_an_already_rendered_url_is_rejected():
    url = "s3://proxy/wf/exec/proxy/transcoded.mp4"
    out = validate_resolved_urls([url], CANDIDATES, rendered_urls=[url])
    assert out["valid"] == [] and "already rendered" in out["rejected"][0]["reason"]


def test_mixed_pick_splits_valid_and_rejected():
    chosen = [
        "s3://proxy/wf/exec/proxy/transcoded.mp4",                       # valid output
        "https://cdn/1786-Netflix_Open_Content_Meridian.mp4",           # input
        "s3://proxy/wf/exec/hallucinated.mp4",                          # not produced
    ]
    out = validate_resolved_urls(chosen, CANDIDATES, source_urls=SOURCE)
    assert len(out["valid"]) == 1 and len(out["rejected"]) == 2


# --- corner cases: validator ---
from render.review import validate_resolved_urls as _vru


def test_validator_empty_and_none_inputs():
    assert _vru([], []) == {"valid": [], "rejected": []}
    assert _vru(None, None) == {"valid": [], "rejected": []}
    # no candidates given: nothing to prove hallucination against -> accept (can't reject)
    assert _vru(["s3://b/x/out.mp4"], []) == {"valid": ["s3://b/x/out.mp4"], "rejected": []}


def test_validator_ignores_non_string_and_blank_entries():
    out = _vru(["", "  ", None, 42, "s3://proxy/wf/exec/proxy/transcoded.mp4"],
               ["s3://proxy/wf/exec/proxy/transcoded.mp4"])
    assert out["valid"] == ["s3://proxy/wf/exec/proxy/transcoded.mp4"]
    assert out["rejected"] == []


def test_validator_source_wins_over_candidate_when_url_is_both():
    # A url that is somehow in both sets is an input -> reject (safety first).
    url = "s3://skills/artifacts/in.mp4"
    out = _vru([url], [url], source_urls=[url])
    assert out["valid"] == [] and "input" in out["rejected"][0]["reason"]


def test_validator_presign_query_stripped_both_sides():
    chosen = ["https://h/wf/exec/proxy/transcoded.mp4?X-Amz-Signature=a&X-Amz-Expires=900"]
    cand = ["s3://proxy/wf/exec/proxy/transcoded.mp4"]
    assert _vru(chosen, cand)["valid"] == chosen


def test_validator_distinguishes_the_same_filename_across_jobs():
    # transcoded.mp4 repeats across jobs, but the exec-id prefix distinguishes them. A pick from
    # a DIFFERENT job's path is NOT this job's output -> rejected as not-among-outputs.
    chosen = ["s3://proxy/micro-workflow/media-processing/EXEC-OTHER/proxy/transcoded.mp4"]
    cand = ["s3://proxy/micro-workflow/media-processing/EXEC-THIS/proxy/transcoded.mp4"]
    out = _vru(chosen, cand)
    assert out["valid"] == []
    assert "not among the job's actual outputs" in out["rejected"][0]["reason"]


def test_dedup_does_not_confuse_two_jobs_same_filename():
    # Two jobs each produce transcoded.mp4 under different exec ids; rendering job B's must NOT be
    # skipped because job A's was rendered. Full-key normalization keeps the exec-id, so they
    # are distinct.
    a = "s3://proxy/micro-workflow/media-processing/EXEC-A/proxy/transcoded.mp4"
    b = "s3://proxy/micro-workflow/media-processing/EXEC-B/proxy/transcoded.mp4"
    out = _vru([b], [b], rendered_urls=[a])
    assert out["valid"] == [b] and out["rejected"] == []


def test_same_object_as_s3_and_presigned_https_compares_equal():
    s3 = "s3://proxy/micro-workflow/media-processing/EXEC/proxy/transcoded.mp4"
    https = "https://proxy.s3.us-west-2.amazonaws.com/micro-workflow/media-processing/EXEC/proxy/transcoded.mp4?X-Amz-Signature=z"
    # dedup: the presigned form of an already-rendered s3 object is recognized as the same.
    out = _vru([https], [https], rendered_urls=[s3])
    assert out["valid"] == [] and "already rendered" in out["rejected"][0]["reason"]


# --- corner cases: reviewer ---
def test_reviewer_non_dict_and_missing_keys():
    assert review_manifest(None) == ["manifest is not an object"]
    assert review_manifest("not a dict") == ["manifest is not an object"]
    assert review_manifest({}) and "renders nothing" in review_manifest({})[0]


def test_reviewer_track_with_src_counts_as_renderable():
    m = {"template": "nle-timeline", "tracks": [{"type": "video", "src": "s3://b/v.mp4"}]}
    assert review_manifest(m) == []


def test_reviewer_track_without_src_does_not_count():
    m = {"template": "nle-timeline", "tracks": [{"type": "video"}]}  # no src
    assert any("renders nothing" in p for p in review_manifest(m))


def test_reviewer_boxes_deeply_nested_are_detected():
    # boxes several levels down (tracks[].faces[].box) still count as data boxes.
    sidecars = {"ft.json": {"a": {"tracks": [{"faces": [{"box": {"l": .1, "t": .1, "w": .2, "h": .2}}]}]}}}
    m = {"template": "composite-timeline", "tracks": [{"src": "s3://b/v.mp4"}], "annotations": []}
    assert any("bounding box" in p for p in review_manifest(m, sidecars))


def test_reviewer_boxes_present_and_rendered_passes_even_with_extra_findings():
    sidecars = {"ft.json": {"tracks": [{"faces": [{"box": {"l": .1, "t": .1, "w": .2, "h": .2}}]}]}}
    m = {"template": "video-annotations", "tracks": [{"src": "s3://b/v.mp4"}],
         "annotations": [{"box": {"x": .1, "y": .1, "width": .2, "height": .2}}]}
    assert review_manifest(m, sidecars) == []


# --- corner cases: the @tool JSON wrappers survive malformed input ---
def test_tool_wrappers_guard_json_parsing():
    """Both @tool wrappers must not crash on malformed JSON from the model."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "render_agent.py").read_text()
    # review tool guards manifest parse; validate tool guards each array parse.
    assert "manifest_json was not valid JSON" in src
    assert "except (ValueError, TypeError)" in src  # validate_render_urls' _arr guard
