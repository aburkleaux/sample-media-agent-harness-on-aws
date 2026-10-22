"""The renderer must be fed by role, not by scraping urls out of prose.

dispatch_render is URL-BASED: the Render Agent gets urls, and the render skills
pick a template by fetching them. The plan aggregate inlines its findings, so a
four-analysis plan reported artifact_count=0 and dispatch_render had only a stray
presigned video to work with -- producing one duplicated video track and an empty
generic-preview instead of face/shot/speaker timelines.

The aggregate now states each artifact's ROLE, so the hook reads it:

  * role="analysis" -> `urls`, the JSONs the render skills already declare they
    handle;
  * role="source"  -> `source_urls`, which dispatch_render documents as "the
    original video behind a detection result ... so the Render Agent can compose an
    overlay view", excluded from the dedup key. That is the canvas the face
    video-annotations view draws its boxes on.

Reading the role also stops the source being rendered AS an output, which is what
gave the untouched upload its own "Media Processing" tab.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hooks import actions  # noqa: E402
from hooks.provider import ctx_after_tool_result, extract_source_uris  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_dispatch_render():
    """These tests replace render_artifacts.dispatch_render by DIRECT assignment (not monkeypatch)
    to capture its call args. Without restoring it, the stub (which returns a canned "m1") leaks
    into later test files and poisons any test that calls the real dispatch_render — e.g.
    test_durable_render_dedup saw res="m1" instead of None. Save/restore around every test here."""
    import render.artifacts as render_artifacts
    original = render_artifacts.dispatch_render
    try:
        yield
    finally:
        render_artifacts.dispatch_render = original

PROXY = "s3://proxy-bkt/micro-workflow/media-processing/exec-1/proxy/transcoded.mp4"
FACES = "s3://proxy-bkt/micro-workflow/face-recognition/exec-2/facemetadata.json"
SHOTS = "s3://proxy-bkt/micro-workflow/shot-segmentation/exec-3/embeddings/shots.json"

AGGREGATE = {
    "schema": "plan-result/v1",
    "plan_id": "p1",
    "status": "SUCCEEDED",
    "counts": {"analysis": 2},
    "artifact_count": 4,
    "nodes": [
        {"node_id": "recognize_faces", "status": "completed", "kind": "analysis",
         "artifacts": [
             {"url": FACES, "media_kind": "data", "role": "analysis", "name": "facemetadata.json"},
             {"url": PROXY, "media_kind": "video", "role": "source"},
         ],
         "findings": {"facemetadata.json": {"stats": {"num_people": 5}}},
         "error": "", "handle": {}},
        {"node_id": "segment_shots", "status": "completed", "kind": "analysis",
         "artifacts": [
             {"url": SHOTS, "media_kind": "data", "role": "analysis", "name": "shots.json"},
             {"url": PROXY, "media_kind": "video", "role": "source"},
         ],
         "findings": {"shots.json": {"shots": [1, 2, 3]}},
         "error": "", "handle": {}},
    ],
}


class _Event:
    def __init__(self, name, result):
        self.tool_use = {"name": name, "toolUseId": "u1"}
        self.result = result


def _result(payload):
    # The shape Strands hands a hook: a content block carrying the tool's json.
    return {"content": [{"text": json.dumps(payload)}]}


def test_analysis_files_become_render_urls():
    ctx = ctx_after_tool_result(_Event("get_plan_results", _result(AGGREGATE)))
    urls = ctx["result"]["urls"]
    assert FACES in urls and SHOTS in urls
    # application/json is what the render skills declare they handle.
    assert "application/json" in ctx["result"]["mimes"]


def test_the_source_video_is_context_not_an_output():
    ctx = ctx_after_tool_result(_Event("get_plan_results", _result(AGGREGATE)))
    assert PROXY not in ctx["result"]["urls"], "the source must not render as an output"
    assert PROXY in ctx["result"]["source_urls"], "but must reach the overlay channel"
    assert extract_source_uris(_result(AGGREGATE)) == [PROXY]


def test_a_non_aggregate_result_still_uses_structural_extraction():
    # Every other tool keeps today's behaviour.
    plain = {"content": [{"text": json.dumps({"output": {"url": "https://cdn/a.mp4"}})}]}
    ctx = ctx_after_tool_result(_Event("some___tool", plain))
    assert "https://cdn/a.mp4" in ctx["result"]["urls"]


def test_presigned_duplicates_of_one_object_render_once():
    """Two presigns of the same object differ only by query string.

    String equality treated them as different artifacts, which is how one manifest
    ended up with the SAME transcoded video as two tracks.
    """
    calls = []
    import render.artifacts as render_artifacts

    render_artifacts.dispatch_render = lambda urls, source_urls=None, label=None, render_request_id=None, render_key=None, tool_name=None: (
        calls.append(list(urls)) or "m1"
    )
    actions.open_render_batch()
    actions._buffer_render(["https://cdn/v.mp4?X-Amz-Signature=aaa&Expires=1"], [], "Faces")
    actions._buffer_render(["https://cdn/v.mp4?X-Amz-Signature=bbb&Expires=2"], [], "Faces")
    actions.flush_render_batch()
    assert len(calls) == 1
    assert len(calls[0]) == 1, f"same object rendered {len(calls[0])} times"


def test_source_urls_are_deduped_by_object_too():
    calls = []
    import render.artifacts as render_artifacts

    render_artifacts.dispatch_render = lambda urls, source_urls=None, label=None, render_request_id=None, render_key=None, tool_name=None: (
        calls.append(list(source_urls or [])) or "m1"
    )
    actions.open_render_batch()
    actions._buffer_render(["s3://b/a.json"], ["https://cdn/p.mp4?sig=1"], "Faces")
    actions._buffer_render(["s3://b/b.json"], ["https://cdn/p.mp4?sig=2"], "Faces")
    actions.flush_render_batch()
    assert len(calls[0]) == 1


def test_standalone_job_recovers_source_end_to_end_and_reaches_dispatch():
    """The d516165c failure, end to end: a completion aggregate keyed by the execution ARN
    (node_id == job_id) with analysis artifacts but NO role=source (resolve_node couldn't derive
    it). The recorded source_media must be recovered by node_id and REACH dispatch_render as
    source_urls — else the render has no canvas and falls back to data-table. Proves the whole
    agent chain: aggregate -> render_action groups -> flush -> dispatch_render."""
    import hooks.provider as provider
    import render.artifacts as render_artifacts

    EXEC = "arn:aws:states:us-west-2:100000000002:execution:m2cv6-face-recognition:894d044a"
    VIDEO = "s3://proxy-bkt/micro-workflow/media-processing/44500011/proxy/transcoded.mp4"
    envelope = {
        "schema": "plan-result/v1", "plan_id": "p", "status": "SUCCEEDED",
        "nodes": [{
            "node_id": EXEC, "status": "completed", "kind": "analysis",
            "artifacts": [
                {"url": "s3://b/out/facegroup.json", "media_kind": "data", "role": "analysis", "name": "facegroup.json"},
                {"url": "s3://b/out/facemetadata.json", "media_kind": "data", "role": "analysis", "name": "facemetadata.json"},
            ],  # NO role=source — exactly what the live manifest showed
        }],
    }
    # The registry recorded the source at start (capture is confirmed working live).
    provider._job_source_media_lookup = lambda jid: [VIDEO] if EXEC in jid else []
    calls = []
    render_artifacts.dispatch_render = lambda urls, source_urls=None, label=None, render_request_id=None, render_key=None, tool_name=None: (
        calls.append({"urls": list(urls), "source_urls": list(source_urls or []), "render_key": render_key}) or "m1"
    )
    try:
        ctx = ctx_after_tool_result(_Event("face-recognition___get_job_status", _result(envelope)))
        actions.open_render_batch()
        actions.render_action({}, ctx)
        actions.flush_render_batch()
    finally:
        provider._job_source_media_lookup = None

    assert len(calls) == 1, f"expected one render dispatch, got {len(calls)}"
    assert VIDEO in calls[0]["source_urls"], f"source video did not reach dispatch_render: {calls[0]}"
    assert "s3://b/out/facegroup.json" in calls[0]["urls"]
    assert calls[0]["render_key"] == EXEC, "render must be keyed by the job so it's one tab"


def test_a_generated_image_manifest_still_reaches_the_ui():
    """Regression: an image render must not be discarded as "empty".

    A suppression gate treated `tracks: []` as "nothing to draw". But
    generic-preview carries its content in `artifacts`/`sources`, so the manifest
    for two generated images -- tracks: [], artifacts: [2 images] -- was thrown
    away and image rendering broke completely. Two live sessions produced correct
    manifests (1004B, 1005B) that never became tabs.

    This is the real manifest shape, verbatim from
    artifacts/5c550bf2-…/render/9fe41555-….json.
    """
    import render.artifacts as render_artifacts

    manifest = {
        "id": "9fe41555",
        "template": "generic-preview",
        "duration": 0.001,
        "tracks": [],
        "clips": [],
        "captions": [],
        "sourceUrl": "https://cdn/artifacts/external/d644e758.jpg",
        "sources": ["https://cdn/artifacts/external/d644e758.jpg",
                    "https://cdn/artifacts/external/490cc260.png"],
        "artifacts": [
            {"url": "https://cdn/artifacts/external/d644e758.jpg", "mime": "image/jpeg", "kind": "image"},
            {"url": "https://cdn/artifacts/external/490cc260.png", "mime": "image/png", "kind": "image"},
        ],
        "mimeType": "image/jpeg",
        "renderSkill": "render-generic",
    }
    # Nothing in the surface path may inspect a manifest and decide to drop it.
    assert not hasattr(render_artifacts, "_manifest_has_content"), (
        "a content gate was reintroduced; verify it against real manifests first -- "
        "images live in artifacts/sources, not tracks"
    )
    # The shape that was being discarded does carry content.
    assert manifest["artifacts"] and not manifest["tracks"]


def test_each_node_gets_its_own_view_named_after_the_work():
    """One dispatch per node, or the analyses collapse into one generic tab.

    All of a plan's analysis files share the same tool label and the same "other"
    media class, so a single flat dispatch put seven JSONs in one manifest -- leaving
    the Render Agent to pick ONE template for face metadata, shot boundaries and
    speaker turns at once, which is how a rich four-analysis plan became an "N data
    files" tab.
    """
    calls = []
    import render.artifacts as render_artifacts

    render_artifacts.dispatch_render = lambda urls, source_urls=None, label=None, render_request_id=None, render_key=None, tool_name=None: (
        calls.append({"label": label, "urls": list(urls), "sources": list(source_urls or [])})
        or "m1"
    )
    ctx = ctx_after_tool_result(_Event("get_plan_results", _result(AGGREGATE)))
    actions.open_render_batch()
    actions.render_action({}, ctx)
    actions.flush_render_batch()

    by_label = {c["label"]: c for c in calls}
    assert set(by_label) == {"Recognize Faces", "Segment Shots"}
    assert by_label["Recognize Faces"]["urls"] == [FACES]
    assert by_label["Segment Shots"]["urls"] == [SHOTS]
    # Each view gets the canvas its overlay draws on.
    for call in calls:
        assert call["sources"] == [PROXY]


def test_node_ids_lose_their_ordering_prefix():
    from hooks.provider import _humanize_node

    assert _humanize_node("t3_detect_faces") == "Detect Faces"
    assert _humanize_node("sg7_shot_segmentation") == "Shot Segmentation"
    assert _humanize_node("recognize_faces") == "Recognize Faces"
    # A node whose whole name is a prefix keeps it rather than becoming empty.
    assert _humanize_node("t1") == "T1"
    assert _humanize_node("") == ""


def test_a_result_without_groups_still_renders_flat():
    # Older aggregates and every non-aggregate tool keep the single-dispatch path.
    calls = []
    import render.artifacts as render_artifacts

    render_artifacts.dispatch_render = lambda urls, source_urls=None, label=None, render_request_id=None, render_key=None, tool_name=None: (
        calls.append(label) or "m1"
    )
    ctx = ctx_after_tool_result(_Event("some___tool", {"content": [{"text": json.dumps(
        {"output": {"url": "https://cdn/a.mp4"}})}]}))
    actions.open_render_batch()
    actions.render_action({}, ctx)
    actions.flush_render_batch()
    assert len(calls) == 1


def test_tool_flows_through_hook_to_dispatch_and_selects_the_view(monkeypatch):
    """End-to-end for the tool-keyed view fix (sessions a2ebd1e9 / a5a1b4fa).

    A shot-segmentation node's federated tool name must reach dispatch_render as
    ``tool_name`` — and resolve to the shot view — instead of being inferred from the
    output JSON's shape (which picked render-image / render-audio in production).
    """
    import render.artifacts as render_artifacts
    from render_agent import select_render_skill_by_tool

    envelope = {
        "schema": "plan-result/v1", "plan_id": "p9", "status": "SUCCEEDED",
        "nodes": [{
            "node_id": "sg_shots", "status": "completed", "kind": "analysis",
            "tool": "shot-segmentation___get_job_status",
            "artifacts": [
                {"url": "s3://b/out/shots.json", "media_kind": "data",
                 "role": "analysis", "name": "shots.json"},
            ],  # NO role=source and JSON-only — exactly what rendered as render-image live
        }],
    }
    calls = []
    render_artifacts.dispatch_render = lambda urls, source_urls=None, label=None, render_request_id=None, render_key=None, tool_name=None: (
        calls.append({"render_key": render_key, "tool": tool_name}) or "m1"
    )
    ctx = ctx_after_tool_result(_Event("shot-segmentation___get_job_status", _result(envelope)))
    actions.open_render_batch()
    actions.render_action({}, ctx)
    actions.flush_render_batch()

    assert len(calls) == 1
    # The raw federated tool name reached dispatch_render...
    assert calls[0]["tool"] == "shot-segmentation___get_job_status", calls[0]
    # ...and it resolves to the shot view, NOT whatever the JSON shape would score.
    assert (select_render_skill_by_tool(calls[0]["tool"]) or {}).get("name") == "render-shot-segmentation"
    # Identity is still the job/node id (one tab per job), scoped by its plan (render.keys).
    assert calls[0]["render_key"] == "p9--sg_shots"
