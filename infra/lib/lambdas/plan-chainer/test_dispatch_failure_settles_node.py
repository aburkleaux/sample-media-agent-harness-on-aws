"""A dispatch INVOKE failure must FAIL the node loudly so the plan settles.

Regression for session 1a4b98f2: a ClientError / non-timeout exception on `_invoke_task_agent`
used to `return` SILENTLY, leaving the node `pending` with no plan-state write — so no DynamoDB
stream event fired, the chainer was never re-invoked, and the node plus every dependent (the
seg*_validate nodes, then gallery) sat PENDING forever. `_dispatch_node` now marks the node failed
in that case, which emits a stream event → the chainer re-runs → the retry pass / skip-propagation
settles the plan. A READ TIMEOUT is different (the invoke was accepted, the agent is running) and
must NOT fail the node.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE",
                 "SKILLS_S3_BUCKET", "TASK_AGENT_RUNTIME_ARN"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_dispatch_fail", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_dispatch_fail"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()


class _ClientError(Exception):
    pass


class _FakeAC:
    class exceptions:
        ClientError = _ClientError


class _CondFailed(Exception):
    pass


class _FakeClient:
    class exceptions:
        ConditionalCheckFailedException = _CondFailed


class _FakeMeta:
    client = _FakeClient()


class _FakeTable:
    """Enough of a DynamoDB table for the post-dispatch RUNNING write to not explode."""
    meta = _FakeMeta()

    def put_item(self, **k):
        pass

    def update_item(self, **k):
        pass


NODE = {"id": "seg1_validate", "prompt": "validate the segment"}


def _capture(monkeypatch):
    failed: list = []
    monkeypatch.setattr(pc, "_mark_node_failed", lambda s, p, n, r: failed.append((n, r)), raising=False)
    monkeypatch.setattr(pc, "_agentcore", _FakeAC, raising=False)
    monkeypatch.setattr(pc, "TASK_AGENT_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task", raising=False)
    monkeypatch.setattr(pc, "_plan_table", _FakeTable(), raising=False)
    monkeypatch.setattr(pc, "_job_table", _FakeTable(), raising=False)
    return failed


def test_client_error_on_dispatch_marks_node_failed(monkeypatch):
    failed = _capture(monkeypatch)
    monkeypatch.setattr(pc, "_invoke_task_agent",
                        lambda session_id, payload: (_ for _ in ()).throw(_ClientError("ThrottlingException")))
    pc._dispatch_node("sess", "plan1", "USER#a", dict(NODE), [dict(NODE)], {})
    assert failed, "a ClientError on dispatch must mark the node failed (not leave it PENDING)"
    assert failed[0][0] == "seg1_validate"
    assert "dispatch failed" in failed[0][1]


def test_generic_error_on_dispatch_marks_node_failed(monkeypatch):
    failed = _capture(monkeypatch)
    monkeypatch.setattr(pc, "_invoke_task_agent",
                        lambda session_id, payload: (_ for _ in ()).throw(RuntimeError("capacity exceeded")))
    pc._dispatch_node("sess", "plan1", "USER#a", dict(NODE), [dict(NODE)], {})
    assert failed, "a non-timeout dispatch error must mark the node failed"
    assert failed[0][0] == "seg1_validate"


def test_read_timeout_on_dispatch_does_not_fail_node(monkeypatch):
    """A read timeout = the invoke was accepted; the agent is running. Do NOT fail the node."""
    failed = _capture(monkeypatch)

    class ReadTimeoutError(Exception):
        pass

    monkeypatch.setattr(pc, "_invoke_task_agent",
                        lambda session_id, payload: (_ for _ in ()).throw(ReadTimeoutError("read operation timed out")))
    pc._dispatch_node("sess", "plan1", "USER#a", dict(NODE), [dict(NODE)], {})
    assert failed == [], "a read timeout means the invoke was accepted — must not fail the node"
