"""An OPERATIONAL output declared in an `outputs[]` envelope must not become a render tab.

The failure this pins: a media-processing job declares two outputs -- the trimmed video (label
"proxy") and probe.json (label "probe", the source SPEC that ffprobe wrote). The renderer opened
TWO tabs, both titled "Media Processing", and the second played the untouched 11:58 source video
that probe.json describes, with a metadata grid under it.

Nothing scraped a url out of the file body. The envelope was honored literally, including the
sidecar -- because the rule "probe.json is not a deliverable" was written in four places and the
one reader that classifies THIS payload was the place that lacked it. Hence
`test_operational_hints_match_poller`: the two readers of the same envelope are pinned equal, so
the next divergence fails the build instead of shipping a duplicate tab.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hooks.provider import (  # noqa: E402
    _OPERATIONAL_HINTS,
    _is_operational_name,
    extract_output_uris,
)

_PREFIX = ("s3://mediaworks-proxy-100000000002-us-west-2/micro-workflow/media-processing/"
           "95522d24-17ff-5cb8-8bca-c12ef76bc1e1")


def _status_result(outputs):
    """A `get_job_status` tool result carrying an `outputs[]` envelope."""
    return {"status": "success",
            "content": [{"text": json.dumps({"status": "SUCCEEDED", "outputs": outputs})}]}


def test_probe_sidecar_is_not_a_render_candidate():
    """The exact payload from the duplicate-tab report: two declared outputs, one deliverable."""
    uris = extract_output_uris(_status_result([
        {"uri": f"{_PREFIX}/proxy/transcoded.mp4", "label": "proxy", "mime": "video/mp4"},
        {"uri": f"{_PREFIX}/probe.json", "label": "probe", "mime": "application/json"},
    ]))
    assert uris == [f"{_PREFIX}/proxy/transcoded.mp4"], (
        "probe.json is the source spec, not a deliverable -- it must never open a render tab"
    )


def test_probe_excluded_by_basename_when_the_label_is_absent():
    """The label is a hint, not a requirement: an unlabeled probe.json is still operational."""
    uris = extract_output_uris(_status_result([
        {"uri": f"{_PREFIX}/proxy/transcoded.mp4", "mime": "video/mp4"},
        {"uri": f"{_PREFIX}/probe.json", "mime": "application/json"},
    ]))
    assert uris == [f"{_PREFIX}/proxy/transcoded.mp4"]


def test_real_json_deliverables_still_surface():
    """The guard against over-correcting.

    Matching is on whole TOKENS, so the canonical deliverables survive. A substring rule would
    read "metadata" inside facemetadata.json and "manifest" inside speaker_manifest.json and drop
    both -- and facemetadata.json is the FIRST entry on the result-resolution layer's canonical
    list, i.e. the answer itself.
    """
    uris = extract_output_uris(_status_result([
        {"uri": f"{_PREFIX}/facemetadata.json", "mime": "application/json"},
        {"uri": f"{_PREFIX}/face_tracks.json", "mime": "application/json"},
        {"uri": f"{_PREFIX}/final_transcript.json", "mime": "application/json"},
        {"uri": f"{_PREFIX}/shots.json", "mime": "application/json"},
    ]))
    assert uris == [
        f"{_PREFIX}/facemetadata.json",
        f"{_PREFIX}/face_tracks.json",
        f"{_PREFIX}/final_transcript.json",
        f"{_PREFIX}/shots.json",
    ]


def test_speaker_manifest_is_classified_operational_KNOWN_CONFLICT():
    """Pins a PRE-EXISTING conflict this file's drift pin surfaced -- not a behavior change.

    "manifest" is an operational hint in BOTH readers (it has always been in the hook's explicit
    list and the poller's), so speaker_manifest.json is dropped as operational. But it is the
    THIRD entry on the result-resolution layer's _CANONICAL_RESULT_FILES, i.e. that layer calls it
    a deliverable. So a speech-diarization job's speaker manifest surfaces through the aggregate
    path and is dropped on the envelope path.

    Left as-is deliberately: "manifest" also names genuinely operational files (render/job
    manifests), so re-classifying the word is a judgement call about diarization output, not a
    drift fix. Pinned here so the disagreement is visible and intentional rather than latent.
    """
    assert _is_operational_name("speaker_manifest.json") is True
    uris = extract_output_uris(_status_result([
        {"uri": f"{_PREFIX}/speaker_manifest.json", "mime": "application/json"},
    ]))
    assert uris == []


def test_operational_names_are_token_matched():
    assert _is_operational_name("probe") is True
    assert _is_operational_name("probe.json") is True
    assert _is_operational_name("usage_tracking.json") is True
    assert _is_operational_name("pipeline.log") is True
    # Deliverables whose names merely CONTAIN an operational word as a substring.
    assert _is_operational_name("facemetadata.json") is False
    assert _is_operational_name("final_transcript.json") is False
    assert _is_operational_name("transcoded.mp4") is False


def test_operational_hints_match_poller():
    """One rule, two readers of the same envelope, pinned equal.

    `hooks.provider` (agent runtime) and `artifact_capability` (polling-worker lambda) classify
    the SAME M2C `outputs[]` payload, and they are separate deploy units -- the agent cannot
    import a lambda bundle, so the constant cannot literally be shared. Drift is therefore
    prevented here rather than by construction: the poller carried "probe" while the hook did
    not, and that one-word gap is the whole bug this file exists for.
    """
    poller_dir = (Path(__file__).resolve().parents[2]
                  / "infra" / "lib" / "lambdas" / "polling-worker")
    if not (poller_dir / "artifact_capability.py").exists():
        # Packaged/agent-only checkouts have no infra tree; there is nothing to drift against.
        import pytest
        pytest.skip(f"poller bundle not present at {poller_dir}")
    sys.path.insert(0, str(poller_dir))
    try:
        from artifact_capability import _OPERATIONAL_HINTS as poller_hints
    finally:
        sys.path.remove(str(poller_dir))
    assert set(_OPERATIONAL_HINTS) == set(poller_hints), (
        "hooks.provider._OPERATIONAL_HINTS and artifact_capability._OPERATIONAL_HINTS classify "
        "the same outputs[] envelope and have drifted. Update both."
    )


def test_exact_metadata_probe_json_not_rendered_but_facemetadata_survives():
    """A media-processing PROBE's metadata.json is the ANSWER (shown in the chat turn) and must
    NOT render as a separate video+card tab even though it's a DECLARED output (Layer 1 forces
    .json renderable). Exact-basename so facemetadata.json (a real people-detection deliverable)
    still renders."""
    uris = extract_output_uris(_status_result([
        {"uri": f"{_PREFIX}/metadata.json", "mime": "application/json"},
        {"uri": f"{_PREFIX}/facemetadata.json", "mime": "application/json"},
    ]))
    assert f"{_PREFIX}/metadata.json" not in uris          # probe answer → no render tab
    assert f"{_PREFIX}/facemetadata.json" in uris          # real deliverable → still renders
