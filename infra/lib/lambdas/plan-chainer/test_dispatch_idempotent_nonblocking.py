"""Node dispatch must be NON-BLOCKING and IDEMPOTENT.

Regression for session 27c33c57 (a 7-clip trim fan-out that took 7m39s and ran 5 of its
7 trims twice):

  * `_dispatch_node` used to `body.read(100)` after the invoke to "confirm the stream
    opened". The Task Agent writes nothing until its turn ends, so that read blocked for
    the client's whole `read_timeout` (30s) every single time. Eight nodes = 240s of
    blocking inside a 60s Lambda, so the fan-out dispatched as a ~30s staircase (first
    trim 12:55:02, last 12:59:55) instead of in parallel.
  * Because the Lambda then died mid-loop, the DynamoDB stream re-delivered the SAME
    batch, and every node that had been invoked but not yet written to plan state was
    still `pending` — so it was dispatched a SECOND time. The node is now CLAIMED
    (conditional pending->running) BEFORE the invoke, so a re-delivered batch is a no-op.

The error-aware retry pass transitions the node out of `pending` itself (to record
`retries`), so it passes `claimed=True` and must still dispatch.
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
    spec = importlib.util.spec_from_file_location("_chainer_dispatch_idem", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_dispatch_idem"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()

NODE = {"id": "trim_shot3", "prompt": "trim shot 3"}


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


class _BlockingBody:
    """A response stream that behaves like the real one: `read` never returns.

    The real `read` blocks until the client's read timeout (30s). A test cannot wait
    that long, so reading here is an outright failure: the production path must never
    read this stream.
    """

    def __init__(self):
        self.closed = False

    def read(self, *_a, **_k):
        raise AssertionError(
            "dispatch must NOT read the response stream — the Task Agent writes nothing "
            "until its turn ends, so this read costs the full 30s read_timeout per node"
        )

    def close(self):
        self.closed = True


class _PlanTable:
    """Records claim attempts; `claim_fails` simulates a node another delivery claimed."""

    meta = _FakeMeta()

    def __init__(self, claim_fails=False):
        self.claim_fails = claim_fails
        self.puts: list = []

    def put_item(self, **k):
        self.puts.append(k)
        if self.claim_fails:
            raise _CondFailed("already claimed")

    def update_item(self, **k):
        pass


class _JobTable:
    meta = _FakeMeta()

    def update_item(self, **k):
        pass


def _wire(monkeypatch, plan_table):
    """Install fakes and return (invoked_sessions, body)."""
    invoked: list = []
    body = _BlockingBody()
    monkeypatch.setattr(pc, "_agentcore", _FakeAC, raising=False)
    monkeypatch.setattr(pc, "TASK_AGENT_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task", raising=False)
    monkeypatch.setattr(pc, "_plan_table", plan_table, raising=False)
    monkeypatch.setattr(pc, "_job_table", _JobTable(), raising=False)
    monkeypatch.setattr(pc, "_mark_node_failed", lambda *a, **k: None, raising=False)

    def _invoke(session_id, payload):
        invoked.append(session_id)
        return {"response": body}

    monkeypatch.setattr(pc, "_invoke_task_agent", _invoke, raising=False)
    return invoked, body


def test_dispatch_never_reads_the_response_stream(monkeypatch):
    """The 30s-per-node staircase: reading the stream is what caused it."""
    plan = _PlanTable()
    invoked, body = _wire(monkeypatch, plan)

    pc._dispatch_node("sess", "plan1", "USER#a", dict(NODE), [dict(NODE)], {})

    assert invoked == [pc._node_runtime_session_id("plan1", "trim_shot3", 0, "sess")], \
        "the node must still be invoked, under its OWN runtime session"
    assert body.closed, "the stream should be closed to release the connection, not read"


def test_node_is_claimed_before_the_invoke(monkeypatch):
    """Plan state must say `running` BEFORE the invoke, conditioned on `pending`."""
    plan = _PlanTable()
    _wire(monkeypatch, plan)

    pc._dispatch_node("sess", "plan1", "USER#a", dict(NODE), [dict(NODE)], {})

    assert len(plan.puts) == 1, "exactly one plan-state write (the pre-invoke claim)"
    put = plan.puts[0]
    assert put["Item"]["status"] == "running"
    assert put["Item"]["node_id"] == "trim_shot3"
    assert put["Item"]["plan_id"] == "plan1", "the claim must stamp plan_id (stale-row guard)"
    assert ":pending" in put["ExpressionAttributeValues"], "the claim must be conditional on pending"


def test_redelivered_batch_does_not_dispatch_the_node_twice(monkeypatch):
    """The duplicate-trim bug: a re-delivered stream batch must be a no-op per node."""
    plan = _PlanTable(claim_fails=True)
    invoked, _ = _wire(monkeypatch, plan)

    pc._dispatch_node("sess", "plan1", "USER#a", dict(NODE), [dict(NODE)], {})

    assert invoked == [], (
        "a node that is already claimed must NOT be invoked again — session 27c33c57 ran "
        "5 of 7 trims twice this way, both runs writing output and setting completed"
    )


def test_retry_pass_still_dispatches_a_node_it_already_claimed(monkeypatch):
    """`claimed=True` (the retry pass already moved the row out of pending) must dispatch."""
    plan = _PlanTable(claim_fails=True)  # would refuse a pending-conditioned claim
    invoked, _ = _wire(monkeypatch, plan)

    pc._dispatch_node("sess", "plan1", "USER#a", dict(NODE), [dict(NODE)], {}, claimed=True)

    assert len(invoked) == 1, "an error-aware retry must still reach the Task Agent"
    assert plan.puts == [], "the retry pass owns the status write; dispatch must not re-claim"
