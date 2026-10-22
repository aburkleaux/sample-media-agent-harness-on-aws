"""result.json must report a node failed when plan-state says it failed.

Staging dcd11946: seven animate nodes' fal jobs 422'd on ``prompt_expansion_mode``. Plan-state
recorded each as ``failed`` with the error, but the manifest still held the submit's
``{request_id}`` handle as ``status: completed``, and the aggregate read only the manifest -- so
result.json showed ``status: completed, kind: pending`` and the orchestrator diagnosed a 422 as
"timing". The plan-state row's terminal status and error are now overlaid onto the aggregate.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")

from test_plan_result_aggregate import _JobTable, _S3, _nodes, _wire, pc  # noqa: E402

_FAL_422 = ('{"isError": true, "content": [{"text": "HTTP 422 Model input failed schema validation. '
            'No generation was submitted. field_errors: [{\\"path\\": [\\"prompt_expansion_mode\\"], '
            '\\"code\\": \\"required\\"}]"}]}')


def _pending_handle():
    return {"status": "completed", "output_type": "json",
            "value": {"request_id": "req-7", "model": "minimax/h3-max/image-to-video"}}


def test_failed_in_plan_state_but_pending_handle_in_manifest_is_reported_failed(monkeypatch):
    s3 = _S3(manifest={"outputs": {"animate_1": _pending_handle(),
                                   "ok": {"status": "completed", "value": "https://cdn/a.mp4"}}})
    jobs = _JobTable(rows={"req-7": {"status": "RUNNING"}})  # unresolved -> would read as pending
    _wire(monkeypatch, s3, jobs)

    pc._mark_run_complete(
        "sess", "plan-dcd", _nodes("animate_1", "ok"),
        {"animate_1": "failed", "ok": "completed"},
        node_states={"animate_1": {"status": "failed", "error": _FAL_422},
                     "ok": {"status": "completed"}},
    )

    agg = json.loads(s3.puts[0]["body"].decode())
    assert agg["status"] == "FAILED"
    by_id = {n["node_id"]: n for n in agg["nodes"]}
    assert by_id["animate_1"]["status"] == "failed"
    assert by_id["animate_1"]["kind"] == "error"
    assert "prompt_expansion_mode" in by_id["animate_1"]["error"]
    # A successful sibling is untouched.
    assert by_id["ok"]["status"] == "completed" and by_id["ok"]["kind"] == "media"


def test_skip_decided_this_pass_is_reported_skipped(monkeypatch):
    """The settle pass marks dependents skipped in ``node_statuses``; their rows are still stale."""
    s3 = _S3(manifest={"outputs": {}})
    _wire(monkeypatch, s3, _JobTable())

    pc._mark_run_complete(
        "sess", "plan-skip", _nodes("animate_1", "stitch"),
        {"animate_1": "failed", "stitch": "skipped"},
        node_states={"animate_1": {"status": "failed", "error": "422 duration"},
                     "stitch": {"status": "pending"}},
    )

    by_id = {n["node_id"]: n for n in json.loads(s3.puts[0]["body"].decode())["nodes"]}
    assert by_id["stitch"]["status"] == "skipped" and by_id["stitch"]["kind"] == "error"
    assert by_id["stitch"]["error"] == "upstream dependency failed"
    assert by_id["animate_1"]["error"] == "422 duration"


def test_plan_state_error_wins_over_an_older_manifest_error():
    nodes = pc._assemble_aggregate_nodes(
        "sess", "P", _nodes("n1"),
        {"outputs": {"n1": {"status": "failed", "error": "first attempt: 422 duration"}}},
        node_statuses={"n1": "failed"},
        node_states={"n1": {"status": "failed", "error": "retry: 422 prompt_expansion_mode"}},
    )
    assert nodes["n1"]["status"] == "failed"
    assert nodes["n1"]["error"] == "retry: 422 prompt_expansion_mode"


def test_settle_pass_hands_node_states_to_the_aggregate(monkeypatch):
    seen = {}
    monkeypatch.setattr(pc, "_mark_run_complete",
                        lambda s, p, nj, ns, **k: seen.update(statuses=dict(ns), **k))
    monkeypatch.setattr(pc, "_mark_node_skipped", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_should_retry_node", lambda row: False)
    pr = {"node_id": "PLAN", "plan_id": "P", "actor_id": "", "turn_id": "", "delegation_grant": "",
          "model_id": "", "nodes_json": json.dumps([{"id": "a", "depends_on": []}])}
    pc._advance_plan("sess", pr, [pr, {"node_id": "a", "status": "failed", "error": "422 boom"}])
    assert seen["statuses"] == {"a": "failed"}
    assert seen["node_states"]["a"]["error"] == "422 boom"


def test_result_error_keeps_fal_field_errors():
    """A fal 422 arrives in an MCP envelope; a 500-char cut lost the field that was wrong."""
    padded = {"isError": True, "content": [{"text": "x" * 600 + " field_errors: prompt_expansion_mode"}]}
    err = pc._result_error_text(padded)
    assert err and "prompt_expansion_mode" in err and len(err) <= pc._RESULT_ERROR_MAX_CHARS
