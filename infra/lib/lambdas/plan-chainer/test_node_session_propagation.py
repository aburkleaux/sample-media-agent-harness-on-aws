"""A node's Task Agent invoke carries a runtimeSessionId of its OWN, while everything the node
attributes (payload session_id / __plan_session_id) stays the PARENT session.

History: nodes were invoked with the PARENT chat session as runtimeSessionId, which put every node
of a plan in ONE microVM -- when it was replaced after a 502 (plan 81d9198e) the nodes inside it
died together. Each node now runs under ``plan-session-<plan_id>-<node>[-r<attempt>]``
(``_node_runtime_session_id``); the "plan-session-" prefix is the harness's sub-session marker.

The chainer used to dispatch with the session id only inside the payload
(``__plan_session_id``), which observability cannot see — so every node's
sub-agent got a fresh, uncorrelated runtime session and everything from the
sub-agent invoke onward had to be inferred from DynamoDB. Propagation is
best-effort in both directions: a too-short id is dropped rather than sent, and a
rejection of the kwarg retries once without it, because a working dispatch
outranks correlation. The one thing that must NOT happen is a second dispatch of
the same node.

The agent-side half of this defect lives in
``agent/tests/test_runtime_session_propagation.py``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

CHAT_SESSION = "0a1b2c3d-1c4d-4a2e-9f31-6b0d5e2a77c1"  # 36 chars, uuid-shaped like a real chat id
PLAN_SESSION = "plan-session-1786817632-0f3a1c9d5e6b4a2f8c7d1e0b9a8f7e6d"  # lengthened fallback


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE",
                 "SKILLS_S3_BUCKET", "TASK_AGENT_RUNTIME_ARN"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_session", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_session"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()

# The runtime session the t1 node of plan p1 is invoked under (first attempt).
NODE_SID = pc._node_runtime_session_id("p1", "t1", 0, CHAT_SESSION)


class _FakeS3:
    def get_object(self, **k):
        raise RuntimeError("no manifest")  # dep-injection block is best-effort; skip it


class _ReadTimeout(Exception):
    """Stands in for urllib3's ReadTimeoutError (matched by class NAME)."""


class _FakeAC:
    class exceptions:
        class ClientError(Exception):
            ...

    def __init__(self, raise_on_session: Exception | None = None):
        self.calls: list = []
        self._raise_on_session = raise_on_session

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise_on_session is not None and "runtimeSessionId" in kwargs:
            raise self._raise_on_session
        return {"response": b""}


class _FakeTable:
    def __init__(self):
        self.writes = []

    def put_item(self, **k):
        self.writes.append(k)

    def update_item(self, **k):
        self.writes.append(k)


def _conflict_error(code="RetryableConflictException", status=409):
    """A botocore-shaped retryable 409, as InvokeAgentRuntime models it."""
    err = _FakeAC.exceptions.ClientError("Session operation in progress, please retry")
    err.response = {
        "Error": {"Code": code, "Message": "Session operation in progress, please retry"},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return err


class _ConflictingAC(_FakeAC):
    """409s the first ``conflicts`` session-scoped invokes, then accepts them.

    Models the microVM-provisioning window: siblings dispatched into a session that
    is still coming up are rejected, and the SAME session works moments later.
    """

    def __init__(self, conflicts: int, error: Exception | None = None):
        super().__init__()
        self._left = conflicts
        self._error = error or _conflict_error()

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if "runtimeSessionId" in kwargs and self._left > 0:
            self._left -= 1
            raise self._error
        return {"response": b""}


def _setup(monkeypatch, ac=None):
    ac = ac or _FakeAC()
    monkeypatch.setattr(pc, "_agentcore", ac)
    monkeypatch.setattr(pc, "_s3", _FakeS3())
    monkeypatch.setattr(pc, "_plan_table", _FakeTable())
    monkeypatch.setattr(pc, "TASK_AGENT_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")
    # Module-global budget, so it must not leak between tests (handler() resets it
    # per invocation; these tests call _dispatch_node directly).
    pc._session_conflict_sleep_left = pc._SESSION_CONFLICT_SLEEP_BUDGET_S
    return ac


def _no_real_sleeping(monkeypatch) -> list:
    """Record backoff durations instead of spending them."""
    slept: list = []
    monkeypatch.setattr(pc.time, "sleep", lambda s: slept.append(s))
    return slept


def _dispatch(session_id):
    node = {"id": "t1", "prompt": "do"}
    pc._dispatch_node(session_id, "p1", "a", node, [node], {})


def test_dispatch_passes_the_nodes_own_runtime_session_id(monkeypatch):
    ac = _setup(monkeypatch)
    _dispatch(CHAT_SESSION)
    assert len(ac.calls) == 1
    assert ac.calls[0].get("runtimeSessionId") == NODE_SID
    assert NODE_SID != CHAT_SESSION and NODE_SID.startswith("plan-session-")
    # Attribution stays the PARENT: task_agent keys outputs, manifests, job rows and plan-state
    # rows on these two payload fields, never on the runtime session.
    payload = json.loads(ac.calls[0]["payload"].decode())
    assert payload["__plan_session_id"] == CHAT_SESSION
    assert payload["session_id"] == CHAT_SESSION


def test_node_session_ids_are_deterministic_distinct_and_valid():
    import re as _re

    a = pc._node_runtime_session_id("81d9198e-1c4d-4a2e-9f31-6b0d5e2a77c1", "shot1")
    assert a == pc._node_runtime_session_id("81d9198e-1c4d-4a2e-9f31-6b0d5e2a77c1", "shot1")
    others = {
        pc._node_runtime_session_id("81d9198e-1c4d-4a2e-9f31-6b0d5e2a77c1", "shot2"),
        pc._node_runtime_session_id("81d9198e-1c4d-4a2e-9f31-6b0d5e2a77c1", "shot1", 1),
        pc._node_runtime_session_id("other-plan-0000-0000-0000-000000000000", "shot1"),
        pc._node_runtime_session_id("p", "a.b"),
        pc._node_runtime_session_id("p", "a-b"),
    }
    assert a not in others and len(others) == 5
    for sid in others | {a, pc._node_runtime_session_id("p", "x"),
                         pc._node_runtime_session_id("p" * 300, "n" * 300, 3)}:
        assert pc._MIN_RUNTIME_SESSION_ID_LEN <= len(sid) <= 256, sid
        assert _re.fullmatch(r"[A-Za-z0-9_-]+", sid), sid
    # No plan/node to derive from: the caller's session is kept (legacy).
    assert pc._node_runtime_session_id("", "t1", 0, CHAT_SESSION) == CHAT_SESSION


def test_a_retry_runs_in_a_fresh_runtime_session(monkeypatch):
    # The failed attempt may have died with (or be hung in) its microVM; the retry must not be
    # routed back there.
    ac = _setup(monkeypatch)
    node = {"id": "t1", "prompt": "do"}
    pc._dispatch_node(CHAT_SESSION, "p1", "a", node, [node], {}, claimed=True, attempt=1)
    assert ac.calls[0]["runtimeSessionId"] == pc._node_runtime_session_id("p1", "t1", 1)
    assert ac.calls[0]["runtimeSessionId"] != NODE_SID


def test_plan_session_keyed_plan_still_attributes_to_its_plan_session(monkeypatch):
    # A plan started with no chat session bound is keyed by the plan-session id; its nodes run
    # under their own runtime sessions but still attribute to that plan-session id.
    assert len(PLAN_SESSION) >= pc._MIN_RUNTIME_SESSION_ID_LEN
    ac = _setup(monkeypatch)
    _dispatch(PLAN_SESSION)
    assert ac.calls[0].get("runtimeSessionId") == NODE_SID
    assert json.loads(ac.calls[0]["payload"].decode())["__plan_session_id"] == PLAN_SESSION


def test_short_session_id_is_omitted_not_sent(monkeypatch):
    # AgentCore rejects a shorter id outright, so the invoke must proceed without it rather than
    # fail. (Derived node ids are always long enough; the guard stays for any other caller.)
    ac = _setup(monkeypatch)
    pc._invoke_task_agent("plan-session-1786817632", b"{}")
    assert len(ac.calls) == 1
    assert "runtimeSessionId" not in ac.calls[0]
    assert ac.calls[0]["agentRuntimeArn"].endswith("runtime/task")


def test_dispatch_survives_rejected_session_kwarg(monkeypatch):
    # Regression guard: if the session-scoped call is rejected (an older bundled
    # botocore that does not model the param, a ValidationException, the SigV4
    # collision that once broke every dispatch), the node still gets dispatched.
    ac = _setup(monkeypatch, _FakeAC(raise_on_session=_FakeAC.exceptions.ClientError("ValidationException")))
    _dispatch(CHAT_SESSION)
    assert len(ac.calls) == 2
    assert "runtimeSessionId" in ac.calls[0]
    assert "runtimeSessionId" not in ac.calls[1]
    # And it was still recorded as dispatched (running write happened).
    assert pc._plan_table.writes


def test_read_timeout_is_not_retried(monkeypatch):
    # A read timeout means the invoke WAS accepted and the node is already
    # running, so retrying without the kwarg would dispatch the node twice —
    # two transcodes, two status writes. Exactly one invoke, and the caller's
    # read-timeout branch still treats it as dispatched.
    ac = _setup(monkeypatch, _FakeAC(raise_on_session=_ReadTimeout("read operation timed out")))
    _dispatch(CHAT_SESSION)
    assert len(ac.calls) == 1
    assert "runtimeSessionId" in ac.calls[0]
    assert pc._plan_table.writes  # not treated as a failed dispatch


def test_is_read_timeout_matches_both_shapes():
    class ReadTimeoutError(Exception):
        ...

    assert pc._is_read_timeout(ReadTimeoutError("whatever"))  # by class name
    assert pc._is_read_timeout(RuntimeError("read operation timed out"))  # by message
    assert not pc._is_read_timeout(RuntimeError("ValidationException"))


# --- retryable 409 on a session that is still being provisioned --------------
#
# Every node of a plan now shares one runtimeSessionId, so siblings dispatched
# together (initial PLAN insert, or a map expansion) can hit the microVM
# provisioning window. Falling through to the no-session fallback there would be
# silent: the node runs uncorrelated, exactly like the bug being fixed.


def test_session_conflict_retries_the_same_session(monkeypatch):
    slept = _no_real_sleeping(monkeypatch)
    ac = _setup(monkeypatch, _ConflictingAC(conflicts=1))
    _dispatch(CHAT_SESSION)
    assert len(ac.calls) == 2
    # SAME session id on the retry — the point of the whole exercise.
    assert [c.get("runtimeSessionId") for c in ac.calls] == [NODE_SID, NODE_SID]
    assert slept == [pc._SESSION_CONFLICT_BACKOFF_S[0]]


def test_session_conflict_is_detected_by_http_status_alone(monkeypatch):
    # Defensive: a 409 whose code is spelled differently must still be retried
    # rather than silently degraded.
    slept = _no_real_sleeping(monkeypatch)
    ac = _setup(monkeypatch, _ConflictingAC(conflicts=1, error=_conflict_error(code="ConflictException")))
    _dispatch(CHAT_SESSION)
    assert len(ac.calls) == 2
    assert ac.calls[1].get("runtimeSessionId") == NODE_SID
    assert slept == [pc._SESSION_CONFLICT_BACKOFF_S[0]]


def test_persistent_conflict_degrades_only_after_backoff(monkeypatch):
    # A session that never comes up must not wedge the node: after the bounded
    # backoff, dispatch proceeds without correlation.
    slept = _no_real_sleeping(monkeypatch)
    ac = _setup(monkeypatch, _ConflictingAC(conflicts=99))
    _dispatch(CHAT_SESSION)
    with_session = [c for c in ac.calls if "runtimeSessionId" in c]
    without = [c for c in ac.calls if "runtimeSessionId" not in c]
    assert len(with_session) == 1 + len(pc._SESSION_CONFLICT_BACKOFF_S)
    assert len(without) == 1  # dispatched, uncorrelated, exactly once
    assert slept == list(pc._SESSION_CONFLICT_BACKOFF_S)
    assert pc._plan_table.writes


def test_conflict_sleep_is_bounded_across_a_wide_fanout(monkeypatch):
    """The multiplier, not the per-node cost, is what threatens the 60s timeout.

    20 children of a map expansion are dispatched from one serial loop in a single
    Lambda invocation. Even if every one of them conflicts forever, the invocation
    may only spend the fixed budget sleeping — and every node still gets dispatched.
    """
    slept = _no_real_sleeping(monkeypatch)
    ac = _setup(monkeypatch, _ConflictingAC(conflicts=10_000))
    for _ in range(20):
        _dispatch(CHAT_SESSION)
    assert sum(slept) <= pc._SESSION_CONFLICT_SLEEP_BUDGET_S
    # All 20 nodes dispatched despite the conflicts (correlation is what degrades).
    assert len([c for c in ac.calls if "runtimeSessionId" not in c]) == 20


def test_handler_resets_the_conflict_budget_per_invocation(monkeypatch):
    # Warm containers: a budget spent by an earlier invocation must not leave this
    # one unable to retry a 409 at all.
    monkeypatch.setattr(pc, "_process_plan", lambda *a, **k: None)
    pc._session_conflict_sleep_left = 0.0
    pc.handler({"Records": []}, None)
    assert pc._session_conflict_sleep_left == pc._SESSION_CONFLICT_SLEEP_BUDGET_S


def test_consume_conflict_sleep_refuses_once_budget_is_spent(monkeypatch):
    slept = _no_real_sleeping(monkeypatch)
    pc._session_conflict_sleep_left = 0.3
    assert pc._consume_conflict_sleep(0.2) is True
    assert pc._consume_conflict_sleep(0.2) is False  # 0.1 left — not enough
    assert slept == [0.2]
