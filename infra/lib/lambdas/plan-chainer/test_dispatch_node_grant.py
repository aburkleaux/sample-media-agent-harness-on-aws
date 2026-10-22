"""Tests that the Plan Chainer PRESENTS a signed delegation grant (option E).

`_dispatch_node` must forward the grant (persisted on the PLAN row and read in
`_process_plan`) into the Task Agent invoke payload, beside — but distinct from
— the untrusted `actor_id`. When no grant was minted (plan needs no user_jwt
tool), the field must be absent rather than empty.

Importing `handler` requires its env + builds boto3 clients at import, so env is
stubbed before import and the AgentCore client is replaced with a capturing fake.
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("PLAN_STATE_TABLE", "test-plan-state")
os.environ.setdefault("JOB_REGISTRY_TABLE", "test-job-registry")
os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")
os.environ.setdefault("SKILLS_S3_BUCKET", "")  # skip manifest injection path

sys.path.insert(0, os.path.dirname(__file__))

import handler  # noqa: E402


class _CapturingAgentCore:
    """Captures the invoke payload; mimics a fire-and-forget accepted invoke."""

    def __init__(self):
        self.payloads = []

        class _Exc:
            class ClientError(Exception):
                pass

        self.exceptions = _Exc()

    def invoke_agent_runtime(self, *, agentRuntimeArn, qualifier, payload, contentType):  # noqa: N803
        self.payloads.append(json.loads(payload.decode("utf-8")))
        return {"response": b""}  # bytes has no .read(); dispatch treats as accepted


class _ConditionalCheckFailedException(Exception):
    pass


class _StubClient:
    class exceptions:  # noqa: N801
        ConditionalCheckFailedException = _ConditionalCheckFailedException


class _StubMeta:
    client = _StubClient()


class _NoopTable:
    """Plan/Job table stub: writes succeed, nothing is asserted about them here."""

    meta = _StubMeta()

    def put_item(self, **kwargs):
        return {}

    def update_item(self, **kwargs):
        return {}


def _install(monkeypatch):
    fake = _CapturingAgentCore()
    monkeypatch.setattr(handler, "_agentcore", fake)
    monkeypatch.setattr(handler, "_plan_table", _NoopTable())
    monkeypatch.setattr(handler, "_job_table", _NoopTable())
    monkeypatch.setattr(handler, "SKILLS_S3_BUCKET", "")
    return fake


_NODE = {"id": "n1", "prompt": "do it", "tools": ["usr-abc123___run_model"], "depends_on": []}


def test_dispatch_node_presents_grant_in_payload(monkeypatch):
    fake = _install(monkeypatch)
    handler._dispatch_node(
        "sess-1", "plan-1", "USER#alice", dict(_NODE), [dict(_NODE)], {},
        turn_id="turn-1", delegation_grant="signed.grant.token",
    )
    assert len(fake.payloads) == 1
    payload = fake.payloads[0]
    assert payload["delegation_grant"] == "signed.grant.token"
    # actor_id still rides the payload (for logging/quota) but is the UNTRUSTED
    # value; the grant is the verifiable one.
    assert payload["actor_id"] == "USER#alice"


def test_dispatch_node_omits_grant_when_absent(monkeypatch):
    fake = _install(monkeypatch)
    handler._dispatch_node(
        "sess-1", "plan-1", "USER#alice", dict(_NODE), [dict(_NODE)], {},
        turn_id="turn-1", delegation_grant="",
    )
    assert len(fake.payloads) == 1
    # No empty-string decoration: the field is simply absent when no grant exists.
    assert "delegation_grant" not in fake.payloads[0]


def test_dispatch_node_grant_defaults_to_absent(monkeypatch):
    """Backwards-compatible default: callers that don't pass a grant send none."""
    fake = _install(monkeypatch)
    handler._dispatch_node(
        "sess-1", "plan-1", "USER#alice", dict(_NODE), [dict(_NODE)], {},
    )
    assert "delegation_grant" not in fake.payloads[0]


if __name__ == "__main__":
    print("run with pytest")
