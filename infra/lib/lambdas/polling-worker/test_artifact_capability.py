"""Unit tests for tool-name-agnostic renderability discovery."""
from artifact_capability import has_renderable_artifacts, renderable_artifact_urls


def test_explicit_outputs_support_objects_and_prefixes():
    payload = {
        "status": "SUCCEEDED",
        "outputs": [
            {"uri": "s3://bucket/run/analysis_metadata.json", "mime": "application/json"},
            {"uri": "s3://bucket/run/images/", "mime": "image/jpeg"},
        ],
    }
    assert has_renderable_artifacts(payload) is True


def test_explicit_operational_outputs_do_not_trigger_rendering():
    payload = {
        "status": "SUCCEEDED",
        "outputs": [
            {"uri": "s3://bucket/run/usage_tracking.json", "label": "usage"},
            {"uri": "s3://bucket/run/pipeline.log", "label": "log"},
        ],
    }
    assert has_renderable_artifacts(payload) is False


def test_direct_sfn_output_prefix_is_renderable():
    payload = {
        "status": "SUCCEEDED",
        "executionOutput": {
            "operation": "future_operation",
            "output": {"s3uri": "s3://bucket/runs/123"},
            "input": {"s3uri": "s3://bucket/source.mp4"},
        },
    }
    assert has_renderable_artifacts(payload) is True


def test_input_artifact_alone_does_not_trigger_rendering():
    payload = {
        "status": "SUCCEEDED",
        "asset_s3uri": "s3://bucket/source.mp4",
        "input": {"s3uri": "s3://bucket/other-source.wav"},
    }
    assert has_renderable_artifacts(payload) is False


def test_relative_result_keys_are_detected_without_tool_names():
    payload = {
        "status": "SUCCEEDED",
        "data": {
            "primary_result_key": "runs/123/final.csv",
            "preview_prefix": "runs/123/previews/",
        },
    }
    assert has_renderable_artifacts(payload) is True


def test_operational_relative_keys_are_ignored():
    payload = {
        "status": "SUCCEEDED",
        "data": {
            "usage_key": "runs/123/usage_tracking.json",
            "manifest_key": "runs/123/frame_manifest.json",
            "probe_key": "runs/123/probe.json",
        },
    }
    assert has_renderable_artifacts(payload) is False


def test_render_false_opt_out_is_respected():
    payload = {
        "status": "SUCCEEDED",
        "outputs": [
            {"uri": "s3://bucket/run/private.json", "render": False},
        ],
    }
    assert has_renderable_artifacts(payload) is False


def test_collect_explicit_output_urls_skip_operational():
    payload = {
        "status": "SUCCEEDED",
        "outputs": [
            {"uri": "s3://bucket/run/clip.mp4", "mime": "video/mp4"},
            {"uri": "s3://bucket/run/usage_tracking.json", "label": "usage"},
            {"uri": "s3://bucket/run/private.json", "render": False},
        ],
    }
    assert renderable_artifact_urls(payload) == ["s3://bucket/run/clip.mp4"]


def test_collect_reference_urls_ignore_inputs():
    payload = {
        "status": "SUCCEEDED",
        "result": {"output_url": "https://cdn.example/out.wav"},
        "input": {"source_uri": "s3://bucket/in.mp4"},
    }
    assert renderable_artifact_urls(payload) == ["https://cdn.example/out.wav"]


def test_collect_urls_empty_when_not_renderable():
    assert renderable_artifact_urls({"status": "SUCCEEDED", "message": "done"}) == []


def test_collect_urls_deduped_in_order():
    payload = {
        "outputs": [
            {"uri": "s3://b/a.mp4", "mime": "video/mp4"},
            {"uri": "s3://b/a.mp4", "mime": "video/mp4"},
            {"uri": "s3://b/b.png", "mime": "image/png"},
        ],
    }
    assert renderable_artifact_urls(payload) == ["s3://b/a.mp4", "s3://b/b.png"]


def test_collect_resolves_relative_key_and_prefers_file_over_prefix():
    # process_media shape: an output prefix (s3uri) + a concrete relative key
    # (output_keys.proxy) under destination.bucket. The concrete .mp4 must win;
    # the bare prefix (renders as duration 0) must be dropped.
    payload = {
        "output": {"s3uri": "s3://bkt/dir/run"},
        "data": {
            "destination": {"bucket": "bkt", "prefix": "dir/run"},
            "output_keys": {"proxy": "dir/run/proxy/transcoded.mp4"},
            "probe_s3_key": "dir/run/probe.json",
        },
    }
    assert renderable_artifact_urls(payload) == ["s3://bkt/dir/run/proxy/transcoded.mp4"]


def test_collect_qualifies_relative_key_from_s3_uri_bucket_when_no_explicit_bucket():
    payload = {
        "result": {"output_prefix": "s3://mybkt/out/", "video_key": "out/clip.mp4"},
    }
    assert renderable_artifact_urls(payload) == ["s3://mybkt/out/clip.mp4"]


def test_mcp_text_wrapped_outputs_are_detected():
    """M2C status tools reply as an MCP envelope: {content:[{type:text,text:<json>}]}.

    The renderable check must parse the inner JSON, or a finished text-to-speech
    job (outputs[].uri = a .wav, no label, no mime) is marked non-renderable and
    never auto-renders — the exact regression the unwrap fixes.
    """
    import json
    inner = {
        "status": "SUCCEEDED",
        "outputs": [
            {"transcript_index": 0,
             "uri": "s3://proxy/micro-workflow/tts-on-aws/output/j/j_0_0_c0.wav",
             "duration_s": 14.24, "qc_passed": False, "qc_similarity": 0.767},
        ],
        "batch_qc_summary": {"all_passed": False, "passed": 0, "failed": 1, "total": 1},
    }
    envelope = {"content": [{"type": "text", "text": json.dumps(inner)}]}
    assert has_renderable_artifacts(envelope) is True
    assert renderable_artifact_urls(envelope) == [
        "s3://proxy/micro-workflow/tts-on-aws/output/j/j_0_0_c0.wav"]


def test_mcp_structured_content_outputs_are_detected():
    payload = {"structuredContent": {
        "status": "SUCCEEDED",
        "outputs": [{"transcript_index": 0, "uri": "s3://proxy/out/clip_0.wav"}],
    }}
    assert has_renderable_artifacts(payload) is True
    assert renderable_artifact_urls(payload) == ["s3://proxy/out/clip_0.wav"]


def test_nested_wav_key_is_renderable_and_qualified():
    # TTS outputs[].wav_key (relative) nested in a list-of-dicts, with out_bucket
    # in a sibling. The capability gate must say True and the URL layer must
    # qualify it with the discovered bucket — else the audio never auto-renders.
    import artifact_capability as a
    pref = "micro-workflow/tts-on-aws/output/cc1a0b45"
    payload = {"data": {"iterators": [{"item_data": {
        "out_bucket": "buck", "out_prefix": pref,
        "voice": {"s3_conditioning_key": "x/conditioning.npz"},
        "outputs": [{"wav_key": f"{pref}/cc1a0b45_0_0.wav"}],
    }}], "usage_tracking": {"usage_key": f"{pref}/usage_tracking.json"}}}
    assert a.has_renderable_artifacts(payload) is True
    assert a.renderable_artifact_urls(payload) == [f"s3://buck/{pref}/cc1a0b45_0_0.wav"]


def test_metadata_probe_json_is_not_renderable():
    """A media-processing PROBE emits metadata.json/probe.json as the ANSWER (fast, in-turn),
    not a renderable deliverable. It must NOT flag the job renderable — that made the completion
    notification's click re-fetch and render the JSON in the generic renderer. A genuine
    analysis_metadata.json still renders (test_explicit_outputs_support_objects_and_prefixes)."""
    for name in ("metadata.json", "probe.json"):
        payload = {"outputs": [{"uri": f"s3://bucket/ff7/metadata/{name}", "mime": "application/json"}]}
        assert has_renderable_artifacts(payload) is False, name
        assert renderable_artifact_urls(payload) == []


def test_metadata_only_probe_status_body_is_not_renderable():
    """The real 18665eaf regression: a media-processing PROBE returns NO outputs[] envelope, a bare
    output.s3uri DIRECTORY (no file/ext), and declares metadata_only + output_groups[format=metadata].
    The output-scoped catch-all wrongly collected that bare prefix -> hasRenderableArtifacts=true ->
    a bell, a generic-preview JSON tab, and a stuck render. Honouring the metadata-only signal makes
    it non-renderable so a fast probe is answered in-turn only."""
    payload = {
        "operation": "StateUsageTracking",
        "output": {
            "output_groups": [{"group_name": "probe", "format": "metadata", "detail": "full"}],
            "s3uri": "s3://proxy/micro-workflow/media-processing/ea831123-22eb-5330-a726-fc3b42fe5657",
        },
        "input": {"s3uri": "s3://skills/artifacts/18665eaf/input/Meridian.mp4"},
        "data": {
            "destination": {"bucket": "proxy", "prefix": "micro-workflow/media-processing/ea831123"},
            "output_groups": [{"group_name": "probe", "format": "metadata", "detail": "full", "name": "metadata"}],
            "probe_s3_key": "micro-workflow/media-processing/ea831123/probe.json",
            "metadata_only": True,
            "output_keys": {"probe": "micro-workflow/media-processing/ea831123/probe/metadata.json"},
            "usage_tracking": {"source_key": "artifacts/18665eaf/input/Meridian.mp4"},
        },
        "status": "COMPLETED",
    }
    assert has_renderable_artifacts(payload) is False
    assert renderable_artifact_urls(payload) == []


def test_metadata_only_flag_alone_suppresses_renderable():
    # even if a bare output prefix would otherwise be walked, the explicit flag wins
    payload = {"output": {"s3uri": "s3://bucket/runs/999"}, "metadata_only": True}
    assert has_renderable_artifacts(payload) is False


# --- _discover_bucket: OUTPUT-biased, input-aware (shot-segmentation wrong-bucket fix) ---
from artifact_capability import _discover_bucket


def test_discover_bucket_prefers_output_s3uri_over_source_bucket():
    # Real shot-segmentation shape: OUTPUT lives in the proxy bucket, the input/source in
    # skills-store. The old code returned the first `*bucket*` key (source_bucket) and qualified
    # the proxy output keys with skills-store -> NoSuchKey. It must pick the proxy (output) bucket.
    payload = {
        "status": "SUCCEEDED",
        "output": {"s3uri": "s3://proxy-bucket/micro-workflow/shot-segmentation/exec/"},
        "input": {"s3uri": "s3://skills-store/artifacts/sess/input.mp4"},
        "data": {"source_bucket": "skills-store", "proxy_bucket": "proxy-bucket"},
    }
    assert _discover_bucket(payload) == "proxy-bucket"


def test_discover_bucket_proxy_hint_beats_source_bucket_key():
    # Even with no s3uri, an output-named bucket hint must win over a source-named one,
    # regardless of dict order.
    payload = {"data": {"source_bucket": "skills-store", "proxy_bucket": "proxy-bucket"}}
    assert _discover_bucket(payload) == "proxy-bucket"
    payload_rev = {"data": {"proxy_bucket": "proxy-bucket", "source_bucket": "skills-store"}}
    assert _discover_bucket(payload_rev) == "proxy-bucket"


def test_discover_bucket_lone_bucket_still_returned():
    # No output signal at all -> fall back to the only bucket present (unchanged behavior).
    assert _discover_bucket({"data": {"source_bucket": "only-bucket"}}) == "only-bucket"
    assert _discover_bucket({"output": {"s3uri": "s3://out/x/"}}) == "out"


def test_discover_bucket_none_when_no_bucket():
    assert _discover_bucket({"status": "SUCCEEDED", "note": "nothing here"}) is None
