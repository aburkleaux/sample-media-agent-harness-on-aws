"""Intermediate plan outputs must not get their own render tab.

Layer 2 of the render simplification. A node whose output is CONSUMED by another node
in the same plan is an intermediate, not a deliverable: the proxy transcode that exists
only to feed three analyses should not become a "Media Processing" tab. Session
a2ebd1e9 shows exactly that — a media-processing manifest keyed by execution ARN
alongside the three analyses the user actually asked for.

This replaces guesswork with a GRAPH property. Today the equivalent decision is a
hard-coded basename allowlist (`_CANONICAL_BASENAMES` in result_resolution.py: ten
literal filenames), which cannot know anything about a tool it has not been taught.
`depends_on` already exists on `dag_schema.DAGNode` and `jobs.models.JobRecord`, and is
consulted NOWHERE in the render path.

Speed matters as much as correctness here: every dropped intermediate is one fewer
Render-Agent invocation (each carrying a 90s dispatch budget), so fewer tabs is also a
faster turn.

SAFETY: this only ever REMOVES a tab, so every ambiguous case must fail OPEN (render it).
The rules below are deliberately conservative:
  * no depends_on data at all  -> nothing is provably intermediate -> render everything
  * dropping would leave NOTHING -> render everything
  * an `analysis` node is never dropped, even when consumed: its findings are the answer
    the user wanted (diarize -> translate still owes the user the diarization view)
  * dangling depends_on (unknown node id) is ignored, not treated as a consumer
Both dispatch flows are covered: single-tool dispatch has no plan/DAG at all, so it has
no groups and is untouched by construction (asserted below).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _n(node_id, kind="media", depends_on=None, artifacts=1):
    return {
        "node_id": node_id,
        "kind": kind,
        "depends_on": list(depends_on or []),
        "artifacts": [{"url": f"s3://b/{node_id}/out{i}.json", "role": "analysis"}
                      for i in range(artifacts)],
    }


def _drop(nodes):
    from hooks.provider import intermediate_node_ids
    return intermediate_node_ids(nodes)


# --- the real bug -----------------------------------------------------------

def test_proxy_transcode_feeding_analyses_is_intermediate():
    """session a2ebd1e9: transcode -> 3 analyses. The transcode must not get a tab."""
    nodes = [
        _n("transcode", kind="media"),
        _n("sg_shots", kind="analysis", depends_on=["transcode"]),
        _n("sg_diarize", kind="analysis", depends_on=["transcode"]),
        _n("sg_stems", kind="analysis", depends_on=["transcode"]),
    ]
    assert _drop(nodes) == {"transcode"}


def test_fanout_leaves_all_render():
    """session a5a1b4fa: 3 parallel shot-seg jobs, none consumed -> 3 tabs."""
    nodes = [_n(f"segment_{i}", kind="analysis") for i in range(3)]
    assert _drop(nodes) == set()


# --- edge cases -------------------------------------------------------------

def test_single_node_plan_is_never_intermediate():
    assert _drop([_n("only", kind="media")]) == set()


def test_no_depends_on_data_anywhere_fails_open():
    """Older aggregates carry no depends_on: nothing is PROVABLY intermediate."""
    nodes = [{"node_id": "a", "kind": "media", "artifacts": [{"url": "s3://b/a"}]},
             {"node_id": "b", "kind": "analysis", "artifacts": [{"url": "s3://b/b"}]}]
    assert _drop(nodes) == set()


def test_dropping_everything_falls_open():
    """A cycle (or a malformed DAG) must never render NOTHING."""
    nodes = [_n("a", kind="media", depends_on=["b"]),
             _n("b", kind="media", depends_on=["a"])]
    assert _drop(nodes) == set()


def test_consumed_analysis_node_is_kept():
    """diarize -> translate: the diarization view is still the user's answer."""
    nodes = [_n("diarize", kind="analysis"),
             _n("translate", kind="analysis", depends_on=["diarize"])]
    assert _drop(nodes) == set()


def test_dangling_depends_on_is_ignored():
    """A dep on a node not in this aggregate must not make anything intermediate."""
    nodes = [_n("a", kind="media"), _n("b", kind="analysis", depends_on=["ghost"])]
    assert _drop(nodes) == set()


def test_node_with_no_artifacts_is_not_reported():
    """Nothing to drop if it was never going to render."""
    nodes = [_n("empty", kind="media", artifacts=0),
             _n("real", kind="analysis", depends_on=["empty"])]
    assert _drop(nodes) == set()


def test_chain_of_three_drops_only_the_middle_and_first_media():
    """a(media) -> b(media) -> c(analysis): a and b are both consumed media."""
    nodes = [_n("a", kind="media"),
             _n("b", kind="media", depends_on=["a"]),
             _n("c", kind="analysis", depends_on=["b"])]
    assert _drop(nodes) == {"a", "b"}


def test_pending_or_error_nodes_are_not_dropped():
    """A failed/pending node's tab may be the only signal the user gets."""
    nodes = [_n("bad", kind="error"),
             _n("good", kind="analysis", depends_on=["bad"])]
    assert _drop(nodes) == set()


# --- flow coverage ----------------------------------------------------------

def test_single_dispatch_flow_has_no_groups_so_is_untouched():
    """dispatch_tool (one tool, no plan) produces no render_groups at all.

    `_aggregate_artifacts` returns None for a non-plan-result payload, so the filter
    never runs on that flow — the single-dispatch path is unaffected by construction.
    """
    from hooks.provider import _aggregate_artifacts
    assert _aggregate_artifacts({"content": [{"text": '{"some":"tool output"}'}]}) is None
    assert _aggregate_artifacts("just prose, no envelope") is None


# --- end-to-end through the hook (both flows) -------------------------------

def test_plan_flow_drops_the_plumbing_tab_end_to_end():
    """The real a2ebd1e9 shape: transcode feeds 3 analyses -> 3 dispatches, not 4."""
    import json
    import render.artifacts as render_artifacts
    from hooks import actions
    from hooks.provider import ctx_after_tool_result

    class _Ev:
        def __init__(self, name, result):
            self.tool_use = {"name": name, "toolUseId": "u1"}
            self.result = result

    envelope = {
        "schema": "plan-result/v1", "plan_id": "p1", "status": "SUCCEEDED",
        "nodes": [
            {"node_id": "transcode", "kind": "media", "status": "completed",
             "tool": "media-processing___transcode", "depends_on": [],
             "artifacts": [{"url": "s3://b/proxy/transcoded.mp4", "role": "output"}]},
            {"node_id": "sg_shots", "kind": "analysis", "status": "completed",
             "tool": "shot-segmentation___segment_shots", "depends_on": ["transcode"],
             "artifacts": [{"url": "s3://b/sg_shots/shots.json", "role": "analysis"}]},
            {"node_id": "sg_diarize", "kind": "analysis", "status": "completed",
             "tool": "speech-diarization___diarize_speech", "depends_on": ["transcode"],
             "artifacts": [{"url": "s3://b/sg_diarize/speaker_manifest.json", "role": "analysis"}]},
        ],
    }

    original = render_artifacts.dispatch_render
    calls = []
    render_artifacts.dispatch_render = lambda urls, source_urls=None, label=None, render_request_id=None, render_key=None, tool_name=None: (
        calls.append({"key": render_key, "tool": tool_name}) or "m"
    )
    try:
        ctx = ctx_after_tool_result(_Ev("get_plan_results", {"content": [{"text": json.dumps(envelope)}]}))
        actions.open_render_batch()
        actions.render_action({}, ctx)
        actions.flush_render_batch()
    finally:
        render_artifacts.dispatch_render = original

    keys = {c["key"] for c in calls}
    assert "transcode" not in keys, f"the proxy transcode must not get its own tab: {keys}"
    assert keys == {"p1--sg_shots", "p1--sg_diarize"}, keys  # plan-scoped (render.keys)
    # ...and the tool still rides along for deterministic view selection (layer 1).
    assert {c["tool"] for c in calls} == {
        "shot-segmentation___segment_shots", "speech-diarization___diarize_speech"}


def test_single_dispatch_flow_still_renders_its_one_output():
    """dispatch_tool flow: no envelope, no groups -> the single output still renders."""
    import render.artifacts as render_artifacts
    from hooks import actions
    from hooks.provider import ctx_after_tool_result

    class _Ev:
        def __init__(self, name, result):
            self.tool_use = {"name": name, "toolUseId": "u1"}
            self.result = result

    original = render_artifacts.dispatch_render
    calls = []
    render_artifacts.dispatch_render = lambda urls, source_urls=None, label=None, render_request_id=None, render_key=None, tool_name=None: (
        calls.append({"urls": list(urls), "tool": tool_name}) or "m"
    )
    try:
        # A real single-dispatch status payload: SUCCEEDED + a fetchable https url
        # (the hook ignores bare s3:// refs, which are not browser-reachable).
        ctx = ctx_after_tool_result(_Ev(
            "shot-segmentation___get_job_status",
            {"content": [{"text": '{"status":"SUCCEEDED","output":'
                                  '{"s3uri":"https://cdn.example.net/one/shots.json"}}'}]}))
        actions.open_render_batch()
        actions.render_action({}, ctx)
        actions.flush_render_batch()
    finally:
        render_artifacts.dispatch_render = original

    assert len(calls) == 1, f"single dispatch must still produce exactly one render: {calls}"
    # The ctx tool name is threaded on this flow too (no groups involved).
    assert calls[0]["tool"] == "shot-segmentation___get_job_status"


# --- one node = one tab (media-class split) ----------------------------------

def test_an_analysis_nodes_thumbnails_do_not_become_a_second_tab():
    """Found by the local E2E harness, missed by every unit test before it.

    Image slots are keyed on media class ALONE so that image-GENERATION jobs coalesce
    (12 storyboard panels -> 1 gallery, session ccbc9559). But an ANALYSIS node also ships
    stills — shot_*.jpg thumbnails, speaker avatars — and those took the shared image slot,
    so one shot-segmentation node produced TWO manifests: its marker view AND a stray
    gallery. That is the duplicate-tab symptom, reproduced end to end.

    Rule: images are SECONDARY when the same node also produced non-image artifacts, and
    ride along in that node's slot. A node emitting ONLY images is a generation job and
    still coalesces.
    """
    from hooks import actions
    actions._render_batch.clear()
    try:
        actions.open_render_batch()
        actions._buffer_render(
            ["https://cdn/x/shots.json", "https://cdn/x/shot_000000.jpg",
             "https://cdn/x/shot_000001.jpg"],
            [], "Shot Segmentation", render_key="sg_shots",
            tool_name="shot-segmentation___segment_shots")
        slots = dict(actions._render_batch)
    finally:
        actions._render_batch.clear()
    assert len(slots) == 1, (
        f"one node must produce ONE slot; got {len(slots)}: {list(slots)}")
    (urls,) = [s["urls"] for s in slots.values()]
    assert len(urls) == 3, urls


def test_a_pure_image_generation_job_still_coalesces():
    """The behaviour the media-class key exists for must survive."""
    from hooks import actions
    actions._render_batch.clear()
    try:
        actions.open_render_batch()
        # Two separate image-gen jobs, distinct labels/keys, images only.
        actions._buffer_render(["https://cdn/g/a.png"], [], "Job A", render_key="job-a")
        actions._buffer_render(["https://cdn/g/b.png"], [], "Job B", render_key="job-b")
        slots = dict(actions._render_batch)
    finally:
        actions._render_batch.clear()
    assert len(slots) == 1, f"image-only jobs must share one gallery; got {list(slots)}"
    (urls,) = [s["urls"] for s in slots.values()]
    assert sorted(u.rsplit("/", 1)[-1] for u in urls) == ["a.png", "b.png"]
