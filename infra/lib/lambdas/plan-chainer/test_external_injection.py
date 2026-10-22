"""Cross-plan (guided-mode phase handoff) input injection.

A later-phase consumer references a producer that ran in a PRIOR phase's plan via
``external_inputs``. The chainer resolves that producer's EXACT deliverable files
(reusing result_resolution.canonical_result_refs — the same resolver in-plan
depends_on uses) and injects verbatim uris, so the model never reconstructs a
transcript path from a job uuid.

Resolution is by producer NODE id: the plan-state table is keyed (session_id,
node_id) and every plan writes its record under node_id="PLAN", so only the latest
phase's PLAN row survives — a producer plan cannot be found by display_name (session
cc09dea9: every cross-phase input came back UNAVAILABLE that way). The producer's own
node row is uniquely keyed and its result_s3_key embeds the plan_id, which points at
the manifest holding the value.
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("PLAN_STATE_TABLE", "test-plan-state")
os.environ.setdefault("JOB_REGISTRY_TABLE", "test-job-registry")
os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")

sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402
import handler  # noqa: E402

SID = "sess-1"


class _Body:
    def __init__(self, b): self._b = b
    def read(self): return self._b


def _install(monkeypatch, node_rows, manifests, plan_row=None):
    """node_rows: {node_id: {"result_s3_key": ...}} — the producer node rows (uniquely keyed).
    manifests: {plan_id: manifest_dict}. plan_row: the session's single (overwritten) PLAN row,
    included only to prove resolution does NOT depend on it."""
    class _FakeS3:
        def get_object(self, Bucket, Key):
            plan_id = Key.split("/")[2]  # plans/<session>/<plan_id>/manifest.json
            if plan_id not in manifests:
                raise KeyError(Key)
            return {"Body": _Body(json.dumps(manifests[plan_id]).encode("utf-8"))}

    class _FakePlanTable:
        def get_item(self, Key):
            nid = Key["node_id"]
            if nid == "PLAN":
                return {"Item": plan_row} if plan_row else {}
            item = node_rows.get(nid)
            return {"Item": item} if item else {}

    monkeypatch.setattr(handler, "_s3", _FakeS3())
    monkeypatch.setattr(handler, "_plan_table", _FakePlanTable())
    monkeypatch.setattr(handler, "SKILLS_S3_BUCKET", "skills-bkt")


def _node_row(plan_id, node_id):
    return {"result_s3_key": f"plans/{SID}/{plan_id}/{node_id}/output", "status": "completed"}


# Real M2C diarization value: output.s3uri is the PREFIX; named *_key fields under `data`
# are the deliverables that canonical_result_refs resolves.
_DIAR_VALUE = {
    "output": {"s3uri": "s3://proxy-bkt/micro-workflow/speech-diarization/021e360c/"},
    "data": {
        "final_transcript_key":
            "micro-workflow/speech-diarization/021e360c/transcripts/final_transcript.json",
    },
}
_SHOTS_VALUE = {
    "output": {"s3uri": "s3://proxy-bkt/micro-workflow/shot-segmentation/SHOTUUID/"},
    "data": {"shots_key": "micro-workflow/shot-segmentation/SHOTUUID/embeddings/shots.json"},
}


def test_external_input_injects_exact_transcript_uri(monkeypatch):
    _install(monkeypatch,
             node_rows={"sg2_diarize_speech": _node_row("P1", "sg2_diarize_speech")},
             manifests={"P1": {"outputs": {
                 "sg2_diarize_speech": {"status": "completed", "value": _DIAR_VALUE}}}})
    node_def = {"id": "sg4",
                "external_inputs": [{"plan": "plan-foundation", "node": "sg2_diarize_speech"}]}
    text = handler._external_injection_text(node_def, session_id=SID)
    assert "021e360c/transcripts/final_transcript.json" in text
    assert "do NOT guess" in text
    assert "speech-diarization/021e360c/final_transcript.json" not in text  # not the dropped-path form


def test_prefix_producer_surfaces_output_prefix_and_deliverable(monkeypatch):
    _install(monkeypatch,
             node_rows={"sg1_segment_shots": _node_row("P1", "sg1_segment_shots")},
             manifests={"P1": {"outputs": {
                 "sg1_segment_shots": {"status": "completed", "value": _SHOTS_VALUE}}}})
    node_def = {"id": "sg3",
                "external_inputs": [{"plan": "plan-foundation", "node": "sg1_segment_shots"}]}
    text = handler._external_injection_text(node_def, session_id=SID)
    assert "embeddings/shots.json" in text                 # deliverable file
    assert "shot-segmentation/SHOTUUID/" in text           # prefix, for shot_segmentation_s3uri
    assert "PRODUCER OUTPUT" in text


def test_resolves_via_node_row_even_when_plan_row_is_a_later_phase(monkeypatch):
    # Regression for cc09dea9: the session's single PLAN row is a LATER phase's (overwritten),
    # with a different name/plan_id. Resolution must still succeed via the producer node row.
    _install(monkeypatch,
             node_rows={"sg2_diarize_speech": _node_row("FOUNDATION", "sg2_diarize_speech")},
             manifests={"FOUNDATION": {"outputs": {
                 "sg2_diarize_speech": {"status": "completed", "value": _DIAR_VALUE}}}},
             plan_row={"node_id": "PLAN", "display_name": "plan-phase2-shot-conv",
                       "plan_id": "LATER", "status": "processing"})
    node_def = {"id": "sg4",
                "external_inputs": [{"plan": "plan-content-moderation-foundation",
                                     "node": "sg2_diarize_speech"}]}
    text = handler._external_injection_text(node_def, session_id=SID)
    assert "021e360c/transcripts/final_transcript.json" in text
    assert "UNAVAILABLE" not in text


def test_missing_producer_emits_unavailable_note_not_a_synthesized_path(monkeypatch):
    # Producer node row absent (never ran / no output) → unavailable, never a fabricated key.
    _install(monkeypatch, node_rows={}, manifests={})
    node_def = {"id": "sg4",
                "external_inputs": [{"plan": "plan-foundation", "node": "sg2_diarize_speech"}]}
    text = handler._external_injection_text(node_def, session_id=SID)
    assert "unavailable" in text.lower()
    assert "s3://" not in text


def test_producer_row_without_result_key_is_unavailable(monkeypatch):
    _install(monkeypatch,
             node_rows={"sg2_diarize_speech": {"status": "failed", "result_s3_key": ""}},
             manifests={})
    node_def = {"id": "sg4",
                "external_inputs": [{"plan": "plan-foundation", "node": "sg2_diarize_speech"}]}
    text = handler._external_injection_text(node_def, session_id=SID)
    assert "unavailable" in text.lower()
    assert "s3://" not in text


def test_no_external_inputs_is_empty(monkeypatch):
    _install(monkeypatch, node_rows={}, manifests={})
    assert handler._external_injection_text({"id": "n"}, session_id=SID) == ""


# --- Idempotent-absorption gap (session 28514e98) ------------------------------------------------
# Re-running an already-processed asset absorbs into an M2C execution that finished in an EARLIER
# session; no results-fetch fills THIS session's manifest (outputs == {}). The resolver must fall
# back to the SFN execution's own terminal output (DescribeExecution), not report UNAVAILABLE.

_ARN = ("arn:aws:states:us-west-2:100000000002:execution:"
        "m2cv6-0e0000000002-speech-diarization:9c88e6b4-1ec9-5dc4-a6e7-65ccd87426e3")


class _FakeSFN:
    def __init__(self, status="SUCCEEDED", output=None):
        self.status, self.output, self.calls = status, output, []

    def describe_execution(self, executionArn):
        self.calls.append(executionArn)
        return {"status": self.status, "output": json.dumps(self.output) if self.output is not None else ""}


def _diar_row():
    return {"result_s3_key": f"plans/{SID}/P1/sg2_diarize_speech/output",
            "status": "completed", "job_id": _ARN}


def test_empty_manifest_falls_back_to_sfn_output(monkeypatch):
    _install(monkeypatch, node_rows={"sg2_diarize_speech": _diar_row()}, manifests={"P1": {"outputs": {}}})
    sfn = _FakeSFN("SUCCEEDED", _DIAR_VALUE)
    monkeypatch.setattr(handler, "_sfn_client_for_arn", lambda arn: sfn)
    node_def = {"id": "sg4", "external_inputs": [{"plan": "p", "node": "sg2_diarize_speech"}]}
    text = handler._external_injection_text(node_def, session_id=SID)
    assert "021e360c/transcripts/final_transcript.json" in text
    assert "UNAVAILABLE" not in text
    assert sfn.calls == [_ARN]


def test_sfn_not_succeeded_stays_unavailable(monkeypatch):
    _install(monkeypatch, node_rows={"sg2_diarize_speech": _diar_row()}, manifests={"P1": {"outputs": {}}})
    monkeypatch.setattr(handler, "_sfn_client_for_arn", lambda arn: _FakeSFN("RUNNING", _DIAR_VALUE))
    node_def = {"id": "sg4", "external_inputs": [{"plan": "p", "node": "sg2_diarize_speech"}]}
    text = handler._external_injection_text(node_def, session_id=SID)
    assert "UNAVAILABLE" in text and "s3://" not in text


def test_sfn_error_payload_stays_unavailable(monkeypatch):
    _install(monkeypatch, node_rows={"sg2_diarize_speech": _diar_row()}, manifests={"P1": {"outputs": {}}})
    monkeypatch.setattr(handler, "_sfn_client_for_arn",
                        lambda arn: _FakeSFN("SUCCEEDED", {"status": "FAILED", "error": "boom"}))
    node_def = {"id": "sg4", "external_inputs": [{"plan": "p", "node": "sg2_diarize_speech"}]}
    assert "UNAVAILABLE" in handler._external_injection_text(node_def, session_id=SID)


def test_non_sfn_job_id_never_calls_describe(monkeypatch):
    _install(monkeypatch,
             node_rows={"sg2_diarize_speech": {**_diar_row(), "job_id": "req_fal_123"}},
             manifests={"P1": {"outputs": {}}})
    sfn = _FakeSFN("SUCCEEDED", _DIAR_VALUE)
    monkeypatch.setattr(handler, "_sfn_client_for_arn", lambda arn: sfn)
    node_def = {"id": "sg4", "external_inputs": [{"plan": "p", "node": "sg2_diarize_speech"}]}
    assert "UNAVAILABLE" in handler._external_injection_text(node_def, session_id=SID)
    assert sfn.calls == []


def test_in_plan_dependency_also_falls_back(monkeypatch):
    # Same gap for in-plan depends_on: dep_outputs (manifest) empty, dep state carries the SFN job.
    monkeypatch.setattr(handler, "SKILLS_S3_BUCKET", "skills-bkt")
    monkeypatch.setattr(handler, "_sfn_client_for_arn", lambda arn: _FakeSFN("SUCCEEDED", _DIAR_VALUE))
    text = handler._dependency_injection_text(
        {"id": "sg4", "depends_on": ["sg2_diarize_speech"]}, {}, {"sg2_diarize_speech": _diar_row()},
        SID, "P2")
    assert "021e360c/transcripts/final_transcript.json" in text


# --- Readiness gate in _dispatch_node (false-success fix, session 28514e98) ----------------------

def _gate_setup(monkeypatch, node_rows, manifests, job_status=None, sfn=None):
    _install(monkeypatch, node_rows=node_rows, manifests=manifests)
    if sfn is not None:
        monkeypatch.setattr(handler, "_sfn_client_for_arn", lambda arn: sfn)

    class _Jobs:
        def get_item(self, Key):
            return {"Item": {"jobId": Key["jobId"], "status": job_status}} if job_status else {}

    monkeypatch.setattr(handler, "_job_table", _Jobs())
    failed = []
    monkeypatch.setattr(handler, "_mark_node_failed", lambda s, p, n, r: failed.append((n, r)))
    reached = []

    def _past_gate(node_def, all_nodes):  # first call after the gate — proves dispatch proceeded
        reached.append(node_def["id"])
        raise RuntimeError("stop-after-gate")

    monkeypatch.setattr(handler, "_ensure_node_can_reach_its_inputs", _past_gate)
    return failed, reached


_NODE = {"id": "sg4", "external_inputs": [{"plan": "p", "node": "sg2_diarize_speech"}]}


def test_gate_fails_fast_when_unresolvable(monkeypatch):
    failed, reached = _gate_setup(monkeypatch, node_rows={}, manifests={})
    handler._dispatch_node(SID, "P2", "actor", _NODE, [_NODE], {})
    assert reached == []                                   # never dispatched the agent
    assert failed and failed[0][0] == "sg4"
    assert "unresolved reference" in failed[0][1]           # non-retryable per _is_missing_input_error
    assert handler._is_missing_input_error(failed[0][1])


def test_gate_defers_while_producer_job_running(monkeypatch):
    failed, reached = _gate_setup(
        monkeypatch, node_rows={"sg2_diarize_speech": _diar_row()}, manifests={"P1": {"outputs": {}}},
        job_status="RUNNING", sfn=_FakeSFN("RUNNING", _DIAR_VALUE))
    handler._dispatch_node(SID, "P2", "actor", _NODE, [_NODE], {})
    assert reached == [] and failed == []                  # deferred: left pending, not failed


def test_gate_passes_when_resolvable(monkeypatch):
    failed, reached = _gate_setup(
        monkeypatch, node_rows={"sg2_diarize_speech": _diar_row()}, manifests={"P1": {"outputs": {}}},
        job_status="SUCCEEDED", sfn=_FakeSFN("SUCCEEDED", _DIAR_VALUE))
    with pytest.raises(RuntimeError, match="stop-after-gate"):
        handler._dispatch_node(SID, "P2", "actor", _NODE, [_NODE], {})
    assert reached == ["sg4"] and failed == []
