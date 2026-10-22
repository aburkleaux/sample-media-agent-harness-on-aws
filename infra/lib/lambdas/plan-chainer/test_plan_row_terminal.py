"""Regression: a completed run must leave the plan-state PLAN row terminal, not `pending`.

Session 2221f53d: all nodes completed and the run was marked SUCCEEDED in the Job Registry,
but the plan-state PLAN row (session_id, node_id="PLAN") stayed `pending` — so the flow view,
which reads that row, showed the plan/flow still "running" after it had finished. The completion
path must write the terminal status to BOTH stores, exactly as _mark_node_failed /
_mark_node_skipped do per node.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("PLAN_STATE_TABLE", "test-plan-state")
os.environ.setdefault("JOB_REGISTRY_TABLE", "test-job-registry")
os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")

sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402
import handler  # noqa: E402


class _CaptureTable:
    """DDB table double that records every update_item's Key + :s status value."""

    def __init__(self):
        self.writes = []

    def update_item(self, Key=None, UpdateExpression="", ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, **kw):
        self.writes.append({"Key": Key or {}, "vals": ExpressionAttributeValues or {}})

    def status_for(self, pred):
        for w in self.writes:
            if pred(w["Key"]):
                return w["vals"].get(":s")
        return None


def _run(monkeypatch, all_succeeded):
    plan_tbl, job_tbl = _CaptureTable(), _CaptureTable()
    monkeypatch.setattr(handler, "_plan_table", plan_tbl)
    monkeypatch.setattr(handler, "_job_table", job_tbl)
    monkeypatch.setattr(handler, "_publish_plan_result", lambda *a, **k: "")  # no S3
    nodes = [{"id": "a"}, {"id": "b"}]
    statuses = ({"a": "completed", "b": "completed"} if all_succeeded
                else {"a": "completed", "b": "failed"})
    handler._mark_run_complete("sess-1", "plan-1", nodes, statuses)
    return plan_tbl, job_tbl


def test_success_marks_both_job_registry_and_plan_row_terminal(monkeypatch):
    plan_tbl, job_tbl = _run(monkeypatch, all_succeeded=True)
    assert job_tbl.status_for(lambda k: k.get("jobId") == "plan-1") == "SUCCEEDED"
    # The bug: this PLAN row was left "pending". It must now be terminal.
    plan_status = plan_tbl.status_for(
        lambda k: k.get("node_id") == "PLAN" and k.get("session_id") == "sess-1")
    assert plan_status == "completed", f"PLAN row not marked terminal: {plan_status!r}"


def test_failure_marks_plan_row_failed(monkeypatch):
    plan_tbl, job_tbl = _run(monkeypatch, all_succeeded=False)
    assert job_tbl.status_for(lambda k: k.get("jobId") == "plan-1") == "FAILED"
    assert plan_tbl.status_for(lambda k: k.get("node_id") == "PLAN") == "failed"
