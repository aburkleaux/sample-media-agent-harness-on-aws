"""M2C media-workflow tool-shape contract tests (resilience P1, task 7).

These lock the exact I/O shape the harness depends on from the M2C
media-workflow-gateway tools (see docs/gateway-integration-notes.md §3). The
GOLDEN payloads below ARE the contract:

  * ``diarize_speech`` (start) returns ``job_id`` (an SFN execution ARN).
  * ``get_job_status`` (SUCCEEDED) returns ``output_s3_prefix`` (s3://...) +
    bucket-relative ``*_key`` fields; the harness reconstructs full s3:// URIs
    for renderable extensions only (``.json`` manifests are deliberately
    excluded from rendering).

If the harness extraction code drifts, these fail in CI. If M2C changes its
payload shape, re-capture the golden via ``tests/contract_check_m2c.py``
(live), update these fixtures + the extraction code together, and bump
docs/gateway-integration-notes.md §3.
"""
import json

from hooks.provider import extract_output_uris
from jobs.strands_tool_wrap import _extract_job_id_from_strands_result

# --- GOLDEN: diarize_speech start response (verified shape) ---
GOLDEN_START = {
    "status": "success",
    "content": [{"text": json.dumps({
        "job_id": "arn:aws:states:us-west-2:100000000002:execution:mediaworks-speech-diarization:abc-123",
        "status": "RUNNING",
        "uuid": "19ac7df1-73ee-534b-a068-5229890ea983",
        "asset_s3uri": "s3://mediaworks-ingest-100000000002-us-west-2/test/speech.wav",
    })}],
}

# --- GOLDEN: get_job_status SUCCEEDED response (verified shape) ---
GOLDEN_STATUS = {
    "status": "success",
    "content": [{"text": json.dumps({
        "status": "SUCCEEDED",
        "execution_arn": "arn:aws:states:us-west-2:100000000002:execution:mediaworks-speech-diarization:abc-123",
        "output_s3_prefix": "s3://mediaworks-proxy-100000000002-us-west-2/test-out",
        "transcript_vtt_key": "test-out/transcripts/final_transcript.vtt",
        "transcript_json_key": "test-out/transcripts/final_transcript.json",
        "speaker_manifest_key": "test-out/speakers/speaker_manifest.json",
    })}],
}

_BUCKET = "s3://mediaworks-proxy-100000000002-us-west-2"


def test_start_response_carries_job_id():
    """The wrap extracts job_id (an SFN ARN) from the start response."""
    job_id = _extract_job_id_from_strands_result(GOLDEN_START)
    assert job_id == "arn:aws:states:us-west-2:100000000002:execution:mediaworks-speech-diarization:abc-123"
    assert job_id.startswith("arn:aws:states:")  # SFN-direct polling depends on this


def test_status_response_reconstructs_renderable_uris():
    """Renderable outputs (VTT) become full s3:// URIs; .json manifests excluded."""
    uris = extract_output_uris(GOLDEN_STATUS)
    assert f"{_BUCKET}/test-out/transcripts/final_transcript.vtt" in uris
    # .json keys are intentionally NOT rendered (manifests, not media/captions).
    assert not any(u.endswith(".json") for u in uris)


def test_contract_keys_present_in_golden():
    """Guard the contract itself: the fields the harness reads must exist.

    If a future edit to the golden drops one of these, the maintainer is forced
    to acknowledge the harness dependency (and update the extraction code).
    """
    start = json.loads(GOLDEN_START["content"][0]["text"])
    status = json.loads(GOLDEN_STATUS["content"][0]["text"])
    assert "job_id" in start, "wrap._extract_job_id_from_strands_result depends on job_id"
    assert status.get("output_s3_prefix", "").startswith("s3://"), \
        "provider.extract_output_uris depends on output_s3_prefix (s3://)"
    assert any(k.endswith("_key") for k in status), \
        "provider.extract_output_uris depends on *_key fields"


def test_missing_prefix_yields_no_uris():
    """No output_s3_prefix → no reconstructed URIs (graceful, not a crash)."""
    broken = {"status": "success", "content": [{"text": json.dumps({
        "status": "SUCCEEDED", "transcript_vtt_key": "x/y.vtt",
    })}]}
    assert extract_output_uris(broken) == []
