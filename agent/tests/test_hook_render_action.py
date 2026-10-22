"""Task 5 — render action + dispatch_render wiring tests."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import render.artifacts as render_artifacts  # noqa: E402
from hooks.actions import _resolve_urls, get_action, render_action  # noqa: E402


def test_resolve_urls_dotted_path():
    assert _resolve_urls("result.urls", {"result": {"urls": ["https://a", "https://b"]}}) == ["https://a", "https://b"]


def test_resolve_urls_literal():
    assert _resolve_urls("https://x/y.wav", {}) == ["https://x/y.wav"]


def test_resolve_urls_list_mixed():
    ctx = {"result": {"urls": ["https://a"]}}
    assert _resolve_urls(["https://b", "result.urls"], ctx) == ["https://b", "https://a"]


def test_resolve_urls_missing_is_empty():
    assert _resolve_urls("result.urls", {"result": {}}) == []


def test_render_registered():
    assert get_action("render") is not None


def test_render_action_calls_dispatch_with_resolved_urls(monkeypatch):
    seen = {}

    def fake_dispatch(urls, source_urls=None, label=None):
        seen["urls"] = urls
        seen["source_urls"] = source_urls
        return "artifacts/sess/render/abc.json"

    monkeypatch.setattr(render_artifacts, "dispatch_render", fake_dispatch)
    res = render_action({"urls": "result.urls"}, {"result": {"urls": ["https://a", "https://b"]}})
    assert res.status == "ok"
    assert res.extra["render_manifest_key"] == "artifacts/sess/render/abc.json"
    assert seen["urls"] == ["https://a", "https://b"]
    assert seen["source_urls"] is None


def test_render_action_forwards_source_media_as_context(monkeypatch):
    seen = {}

    def fake_dispatch(urls, source_urls=None, label=None):
        seen["urls"] = urls
        seen["source_urls"] = source_urls
        return "artifacts/sess/render/def.json"

    monkeypatch.setattr(render_artifacts, "dispatch_render", fake_dispatch)
    res = render_action(
        {"urls": "result.urls", "source_urls": "result.source_urls"},
        {"result": {"urls": ["https://a/boxes.json"], "source_urls": ["https://a/source.mp4"]}},
    )
    assert res.status == "ok"
    assert seen["urls"] == ["https://a/boxes.json"]
    assert seen["source_urls"] == ["https://a/source.mp4"]


def test_render_action_skipped_when_no_urls(monkeypatch):
    monkeypatch.setattr(render_artifacts, "dispatch_render", lambda urls, source_urls=None, label=None: None)
    res = render_action({"urls": "result.urls"}, {"result": {"urls": []}})
    assert res.status == "skipped"


def test_render_action_skipped_when_dispatch_returns_none(monkeypatch):
    monkeypatch.setattr(render_artifacts, "dispatch_render", lambda urls, source_urls=None, label=None: None)
    res = render_action({"urls": "result.urls"}, {"result": {"urls": ["https://a"]}})
    assert res.status == "skipped"


def test_task_agent_drain_converts_render_to_output_ref():
    import json
    from task_agent import _drain_hook_lines
    from orchestration.progress_channel import ProgressChannel

    ch = ProgressChannel()
    ch.push("done", "↳ Rendered artifacts", agent="render", extra={"render_manifest_key": "artifacts/s/render/x.json"})
    ch.push("done", "⚙ hi", agent="hook")
    lines = _drain_hook_lines(ch)
    parsed = [json.loads(l) for l in lines]
    assert parsed[0] == {"type": "output_ref", "s3_key": "artifacts/s/render/x.json"}
    assert parsed[1]["type"] == "step" and parsed[1]["agent"] == "hook"


def test_expand_s3_prefixes_replaces_prefix_with_renderable_artifacts(monkeypatch):
    class FakeS3:
        def list_objects_v2(self, **kwargs):
            assert kwargs == {"Bucket": "proxy", "Prefix": "run/results/", "MaxKeys": 200}
            return {"Contents": [
                {"Key": "run/results/preview.jpg"},
                {"Key": "run/results/timeline.vtt"},
                {"Key": "run/results/data.csv"},
                {"Key": "run/results/binary.tmp"},
            ]}

    monkeypatch.setattr(render_artifacts, "_s3_client", lambda: FakeS3())
    assert render_artifacts._expand_s3_prefixes([
        "s3://proxy/run/primary.json",
        "s3://proxy/run/results/",
    ]) == [
        "s3://proxy/run/primary.json",
        "s3://proxy/run/results/preview.jpg",
        "s3://proxy/run/results/timeline.vtt",
        "s3://proxy/run/results/data.csv",
    ]


def test_render_action_recovers_registered_source_when_result_omits_it(monkeypatch):
    """A standalone analysis result carries no source_urls (M2C omits it), so the render
    recovers the job's REGISTERED input asset (source_media) as the source canvas — this is
    what lets build_render_manifest stamp a source-traced sourceKey so analyses of one video
    merge onto ONE composite timeline instead of separate tabs."""
    seen = {}

    def fake_dispatch(urls, source_urls=None, label=None):
        seen["urls"] = urls
        seen["source_urls"] = source_urls
        return "artifacts/s/render/x.json"

    monkeypatch.setattr(render_artifacts, "dispatch_render", fake_dispatch)
    import hooks.provider as provider
    monkeypatch.setattr(provider, "_recorded_source_media",
                        lambda jid: ["https://a/source.mp4"] if jid == "job-1" else [])
    res = render_action(
        {"urls": "result.urls"},  # no source_urls declared / in result
        {"result": {"urls": ["https://a/shots.json"], "job_id": "job-1"}},
    )
    assert res.status == "ok"
    assert seen["source_urls"] == ["https://a/source.mp4"]  # recovered the job's registered source


def test_render_action_source_recovery_is_noop_when_nothing_recorded(monkeypatch):
    seen = {}
    monkeypatch.setattr(render_artifacts, "dispatch_render",
                        lambda urls, source_urls=None, label=None: (seen.update(source_urls=source_urls), "k")[1])
    import hooks.provider as provider
    monkeypatch.setattr(provider, "_recorded_source_media", lambda jid: [])
    render_action({"urls": "result.urls"}, {"result": {"urls": ["https://a/x.mp4"], "job_id": "j"}})
    assert seen["source_urls"] is None  # additive: no recorded source → unchanged behavior



# ---------------------------------------------------------------------------
# Plan-node per-clip suppression (#4): a DAG fan-out renders each node in its OWN
# invocation, so each sibling clip flushed its own manifest → one LOOSE tab per node
# beside the ONE combined gallery get_plan_results builds. render_action now suppresses
# a bare per-node video/audio clip (run_id bound, no render_groups); the combined-gallery
# render_groups path (main invocation, no run_id) is untouched. Mirrors the UI's
# autoRenderOnComplete `job.runId` skip.
# ---------------------------------------------------------------------------

def _bind_run(monkeypatch, run_id="plan-abc"):
    monkeypatch.setattr("jobs.context.current_run_id", lambda: run_id)


def test_plan_node_video_clip_is_suppressed(monkeypatch):
    _bind_run(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(render_artifacts, "dispatch_render",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "k")
    res = render_action({"urls": "result.urls"},
                        {"result": {"urls": ["https://cdn.example/seg1.mp4"], "job_id": "j1"}})
    assert res.status == "skipped"
    assert "plan-run node clip" in res.detail
    assert called["n"] == 0                       # nothing rendered/buffered


def test_plan_node_audio_clip_is_suppressed(monkeypatch):
    _bind_run(monkeypatch)
    monkeypatch.setattr(render_artifacts, "dispatch_render", lambda *a, **k: "k")
    res = render_action({"urls": "result.urls"},
                        {"result": {"urls": ["https://cdn.example/take1.wav"], "job_id": "j2"}})
    assert res.status == "skipped"


def test_standalone_video_still_renders_when_no_run_id(monkeypatch):
    monkeypatch.setattr("jobs.context.current_run_id", lambda: None)  # not a plan node
    seen = {}
    monkeypatch.setattr(render_artifacts, "dispatch_render",
                        lambda urls, source_urls=None, label=None: (seen.update(urls=urls), "k")[1])
    res = render_action({"urls": "result.urls"},
                        {"result": {"urls": ["https://cdn.example/clip.mp4"], "job_id": "j3"}})
    assert res.status == "ok"                      # standalone dispatch is unaffected
    assert seen["urls"] == ["https://cdn.example/clip.mp4"]


def test_plan_node_image_is_not_suppressed(monkeypatch):
    """Images already coalesce into one gallery slot, so a per-node image is the harmless
    case and must still render — only video/audio clips are suppressed."""
    _bind_run(monkeypatch)
    seen = {}
    monkeypatch.setattr(render_artifacts, "dispatch_render",
                        lambda urls, source_urls=None, label=None: (seen.update(urls=urls), "k")[1])
    res = render_action({"urls": "result.urls"},
                        {"result": {"urls": ["https://cdn.example/frame.png"], "job_id": "j4"}})
    assert res.status == "ok"
    assert seen["urls"] == ["https://cdn.example/frame.png"]


def test_suppression_fails_open_when_run_context_raises(monkeypatch):
    """If the run context can't be read, a video clip must still render (never suppress on
    absence of evidence)."""
    def boom():
        raise RuntimeError("contextvar unbound")
    monkeypatch.setattr("jobs.context.current_run_id", boom)
    monkeypatch.setattr(render_artifacts, "dispatch_render", lambda *a, **k: "k")
    res = render_action({"urls": "result.urls"},
                        {"result": {"urls": ["https://cdn.example/clip.mp4"]}})
    assert res.status == "ok"


def test_get_plan_results_video_fanout_still_coalesces_to_one_gallery(monkeypatch):
    """The combined-gallery path is unaffected: a get_plan_results result with render_groups
    (same video tool in >1 group) still collapses into ONE seggallery--<tool> slot — even with
    a run_id bound, because the guard only fires when there are NO groups. This is the review
    gallery the suppression relies on existing."""
    from hooks.actions import open_render_batch, flush_render_batch, _render_batch
    _bind_run(monkeypatch)  # even if a run_id were bound, the groups path must not be suppressed
    monkeypatch.setattr(render_artifacts, "dispatch_render", lambda *a, **k: "k")
    groups = [
        {"label": "Segment 1", "node_id": "n1", "tool": "sync-lipsync___submit",
         "urls": ["https://cdn.example/seg1.mp4"], "source_urls": []},
        {"label": "Segment 2", "node_id": "n2", "tool": "sync-lipsync___submit",
         "urls": ["https://cdn.example/seg2.mp4"], "source_urls": []},
    ]
    open_render_batch()
    try:
        res = render_action({"urls": "result.urls"},
                            {"result": {"urls": ["https://cdn.example/seg1.mp4",
                                                 "https://cdn.example/seg2.mp4"],
                                        "render_groups": groups}})
        assert res.status == "batched"             # NOT skipped by the plan-node guard
        seg_keys = [k for k in _render_batch if str(k[0]).startswith("seggallery--")]
        assert len(seg_keys) == 1                  # both siblings collapsed into ONE gallery slot
        slot = _render_batch[seg_keys[0]]
        assert set(slot["urls"]) == {"https://cdn.example/seg1.mp4", "https://cdn.example/seg2.mp4"}
    finally:
        flush_render_batch()


def test_one_tool_over_different_sources_is_per_video_work_not_a_fanout(monkeypatch):
    """Session b8a59e30: guided moderation ran shot segmentation on three videos (one node each).
    Counting by tool alone merged their segment clips into one "Segment 1..3" gallery spanning all
    three, so no video's shots folded onto its own source tab. Different sources -> per-node keys;
    the same source (a genuine fan-out, above) still coalesces."""
    from hooks.actions import open_render_batch, flush_render_batch, _render_batch
    _bind_run(monkeypatch)
    monkeypatch.setattr(render_artifacts, "dispatch_render", lambda *a, **k: "k")
    tool = "shot-segmentation___segment_shots"
    groups = [
        {"label": f"Segment Shots {v}", "node_id": f"sg1_segment_shots__{v}", "plan_id": "665da27b",
         "tool": tool, "urls": [f"https://cdn.example/{v}/segment_0000.mp4"],
         "source_urls": [f"https://cdn.example/src/{v}.mp4?X-Amz-Signature=abc"]}
        for v in ("12214339", "06dcaa9a", "5023ba9f")
    ]
    open_render_batch()
    try:
        render_action({"urls": "result.urls"},
                      {"result": {"urls": [u for g in groups for u in g["urls"]], "render_groups": groups}})
        keys = sorted(str(k[0]) for k in _render_batch)
        assert not any(k.startswith("seggallery--") for k in keys), keys
        assert keys == sorted(f"665da27b--sg1_segment_shots__{v}" for v in ("12214339", "06dcaa9a", "5023ba9f"))
    finally:
        flush_render_batch()



# ---------------------------------------------------------------------------
# Dead-tab guard (#2, session d9c9286c): a batch's N crop URLs reached a render as a
# SINGLE string joined by newlines, so the manifest's <video src> was the whole blob —
# an unplayable tab. _resolve_urls now splits a whitespace-joined multi-url string.
# ---------------------------------------------------------------------------

def test_resolve_urls_splits_a_whitespace_joined_blob():
    blob = "https://cdn/a.mp4\nhttps://cdn/b.mp4\nhttps://cdn/c.mp4"
    want = ["https://cdn/a.mp4", "https://cdn/b.mp4", "https://cdn/c.mp4"]
    assert _resolve_urls(blob, {}) == want                       # literal blob string
    assert _resolve_urls([blob], {}) == want                     # blob inside a list
    # a blob resolved from a dotted context path splits too
    assert _resolve_urls("result.urls", {"result": {"urls": [blob]}}) == want


def test_resolve_urls_leaves_a_single_url_untouched():
    assert _resolve_urls("https://cdn/x.mp4", {}) == ["https://cdn/x.mp4"]
    assert _resolve_urls(["https://cdn/x.mp4", "https://cdn/y.mp4"], {}) == \
        ["https://cdn/x.mp4", "https://cdn/y.mp4"]


def test_a_fanout_gallery_is_plan_scoped_and_titled_after_the_plan(monkeypatch):
    """Session be68313c: the lip-sync review gallery was keyed `seggallery--<tool>` (one slot per
    SESSION, so a second lip-sync plan would overwrite it) and titled "Usr 8ddf64af…", the user
    MCP server's opaque id."""
    from hooks.actions import open_render_batch, flush_render_batch, _render_batch
    _bind_run(monkeypatch)
    captured = []
    monkeypatch.setattr(render_artifacts, "dispatch_render", lambda *a, **k: captured.append((a, k)) or "k")
    tool = "usr-8ddf64af24be45c894a7a8c1f254c83f___submit_job"
    groups = [{"label": "", "node_id": f"seg{i}", "plan_id": "1bec78d5", "plan_label": "Lipsync8",
               "tool": tool, "urls": [f"https://cdn.example/seg{i}.mp4"], "source_urls": []} for i in (1, 2)]
    open_render_batch()
    try:
        render_action({"urls": "result.urls"},
                      {"result": {"urls": [g["urls"][0] for g in groups], "render_groups": groups}})
        keys = [str(k[0]) for k in _render_batch]
        assert keys == ["seggallery--1bec78d5--usr-8ddf64af24be45c894a7a8c1f254c83f___submit_job"]
        slot = next(iter(_render_batch.values()))
        assert "Lipsync8 — review" in str(slot)
    finally:
        flush_render_batch()


def test_an_opaque_user_server_id_is_never_a_tab_title():
    import hooks.provider as pv
    assert pv._label_from_tool("usr-8ddf64af24be45c894a7a8c1f254c83f___submit_job") == ""
    assert pv._label_from_tool("face-recognition___recognize_faces") == "Face Recognition"
    assert pv._humanize_plan("plan-lipsync8") == "Lipsync8"
    assert pv._humanize_plan("plan-trim-tts-8clips") == "Trim Tts 8clips"
    assert pv._humanize_plan(None) == ""


def test_a_voice_preview_lookup_is_never_auto_rendered(monkeypatch):
    """Staging 2d841a23: get_voice preview clips opened a "Text To Speech" timeline beside the audition."""
    monkeypatch.setattr(render_artifacts, "dispatch_render", lambda *a, **k: "k")
    res = render_action({"urls": "result.urls"},
                        {"tool": {"name": "m2c___text-to-speech___get_voice"},
                         "result": {"urls": ["s3://proxy/micro-workflow/tts-on-aws/voices/85bc/preview.wav"]}})
    assert res.status == "skipped" and "catalog lookup" in res.detail


def test_a_turn_that_opened_a_gallery_drops_its_auto_render_of_that_media_class(monkeypatch):
    """The same preview reaches a turn as an s3:// uri, a CloudFront mirror and a re-mirrored presign,
    so URL matching cannot tie them (4 of 12 matched). The gallery's media class is dropped for the
    turn, and the slot's composing tab is resolved with render_failed so nothing is left composing."""
    from hooks import actions as A
    pushed = []

    class _Ch:
        def push(self, status, msg, agent=None, extra=None):
            pushed.append((status, msg, extra))
    monkeypatch.setattr("orchestration.progress_channel.get_active_channel", lambda: _Ch())
    dispatched = []
    monkeypatch.setattr(render_artifacts, "dispatch_render", lambda *a, **k: dispatched.append(a) or "k")
    A.open_render_batch()
    try:
        render_action({"urls": "result.urls"},
                      {"tool": {"name": "media-outputs___get_download_urls"},
                       "result": {"urls": ["https://bucket.s3.amazonaws.com/a/preview.wav?X-Amz-Signature=1"]}})
        render_action({"urls": "result.urls"},
                      {"tool": {"name": "fal___submit_job"},
                       "result": {"urls": ["https://cdn.example/new_image.png"]}})
        A.note_gallery_classes(["https://d12.cloudfront.net/artifacts/external/8c85.wav"])
    finally:
        A.flush_render_batch()
    assert len(dispatched) == 1                                   # the image still renders
    failed = [p for p in pushed if p[0] == "failed"]
    assert len(failed) == 1 and failed[0][2]["render_failed"] is True
    assert not A._gallery_classes                                 # turn-scoped


def test_without_a_gallery_nothing_is_dropped(monkeypatch):
    from hooks import actions as A
    dispatched = []
    monkeypatch.setattr(render_artifacts, "dispatch_render", lambda *a, **k: dispatched.append(a) or "k")
    A.open_render_batch()
    try:
        render_action({"urls": "result.urls"},
                      {"tool": {"name": "media-outputs___get_download_urls"},
                       "result": {"urls": ["https://bucket.s3.amazonaws.com/a/take.wav?X-Amz-Signature=1"]}})
    finally:
        A.flush_render_batch()
    assert len(dispatched) == 1


def test_a_code_cell_renders_only_the_files_it_wrote(monkeypatch):
    """Staging c4a4f142: the stitch's manifest cell printed the 8 presigned INPUT clips and they opened as a
    'Code Interpreter' gallery; the reel was the only output."""
    import sandbox_outputs
    import hooks.provider as pv
    monkeypatch.setattr(sandbox_outputs, "output_roots", lambda: ["projects/p/sessions/s/output/"])
    inputs = "https://b.s3.amazonaws.com/artifacts/external/seg1.mp4?X-Amz-Signature=1"
    reel_url = "https://d12.cloudfront.net/projects/p/sessions/s/output/olive_reel_final.mp4"
    printed_only = {"content": [{"text": f'{{"clips": [{{"path": "{inputs}"}}]}}'}]}
    assert pv._code_cell_outputs_only("code_interpreter", printed_only, [inputs]) == []
    wrote = {"content": [{"text": f"report {inputs}"},
                         {"text": f"Files this cell wrote to the session output folder:\n- {reel_url}"}]}
    assert pv._code_cell_outputs_only("code_interpreter", wrote, [inputs, reel_url]) == [reel_url]
    assert pv._code_cell_outputs_only("fal___submit_job", printed_only, [inputs]) == [inputs]
    monkeypatch.setattr(sandbox_outputs, "output_roots", lambda: [])
    assert pv._code_cell_outputs_only("code_interpreter", printed_only, [inputs]) == [inputs]  # no reporting
