"""Tests for ltm — long-term memory preference write/recall.

Logic is exercised with a fake bedrock-agentcore client (no creds/network), and
the exact call shapes are validated against botocore's own model via
ParamValidator (mirrors tests/test_capability_registry_param_shapes.py — a fake
looser than the real API would hide breakage).

Long-term FACTS were removed (2026-10, staging session 07703a35): nothing here may
read or write a `/facts/` namespace.
"""
from __future__ import annotations

import botocore.session
import pytest
from botocore.validate import ParamValidator

import ltm
from security import identity

MEM = "memory-0123456789abcdef"
ACTOR = "user-abc"


class FakeAgentCore:
    def __init__(self, retrieve_response=None):
        self.created = []
        self.retrieved = []
        self._resp = retrieve_response or {"memoryRecordSummaries": []}

    def batch_create_memory_records(self, **kwargs):
        self.created.append(kwargs)
        return {"successfulRecords": [], "failedRecords": []}

    def retrieve_memory_records(self, **kwargs):
        self.retrieved.append(kwargs)
        return self._resp


def _validate(operation: str, params: dict):
    model = botocore.session.get_session().get_service_model("bedrock-agentcore")
    op = model.operation_model(operation)
    report = ParamValidator().validate(params, op.input_shape)
    assert not report.has_errors(), report.generate_report()


# --------------------------------------------------------------------------
# write_preference
# --------------------------------------------------------------------------

def test_write_preference_goes_to_preferences_namespace():
    fake = FakeAgentCore()
    res = ltm.write_preference(memory_id=MEM, actor_id=ACTOR, text="always 16:9", client=fake)
    assert res.ok is True
    (call,) = fake.created
    rec = call["records"][0]
    assert call["memoryId"] == MEM
    assert rec["namespaces"] == ["/preferences/user-abc/"]  # actor-scoped, resolved
    assert rec["content"]["text"] == "always 16:9"
    assert rec["requestIdentifier"]
    assert rec["timestamp"] is not None


def test_write_preference_is_idempotent_per_actor_and_text():
    fake = FakeAgentCore()
    kw = dict(memory_id=MEM, actor_id=ACTOR, text="answer briefly", client=fake)
    ltm.write_preference(**kw)
    ltm.write_preference(**kw)
    ids = [c["records"][0]["requestIdentifier"] for c in fake.created]
    assert ids[0] == ids[1]  # same dedup id → idempotent upsert


def test_write_preference_rejects_empty_inputs():
    fake = FakeAgentCore()
    for kw in (dict(memory_id="", actor_id=ACTOR, text="x"),
               dict(memory_id=MEM, actor_id="", text="x"),
               dict(memory_id=MEM, actor_id=ACTOR, text=""),
               dict(memory_id=MEM, actor_id=ACTOR, text="   ")):
        res = ltm.write_preference(client=fake, **kw)
        assert res.ok is False and res.error == "InvalidInput"
    assert fake.created == []


@pytest.mark.parametrize("text", [
    "the reel is at s3://bucket/olive.mp4",
    "S3://BUCKET/KEY",
    "use https://cdn.example/artifacts/x.mp4",
    "see http://example.com/a.png",
    "input lives at artifacts/0b2c7a10-1111-4222-8333-444455556666/input/clip.mp4",
    "projects/p-123/source.mov is the master cut",
])
def test_write_preference_refuses_locations(text):
    fake = FakeAgentCore()
    res = ltm.write_preference(memory_id=MEM, actor_id=ACTOR, text=text, client=fake)
    assert res.ok is False and res.error == "NotAPreference"
    assert "project_status" in res.message
    assert fake.created == []


@pytest.mark.parametrize("text", [
    "always 16:9", "answer briefly", "captions in Spanish by default",
    "prefers end-to-end moderation runs", "likes warm artifacts-free color grading",
])
def test_preference_rejection_allows_plain_preferences(text):
    assert ltm.preference_rejection(text) == ""


def test_write_preference_swallows_client_errors():
    class Boom:
        def batch_create_memory_records(self, **k):
            raise RuntimeError("service down")
    res = ltm.write_preference(memory_id=MEM, actor_id=ACTOR, text="x", client=Boom())
    assert res.ok is False and res.error == "RuntimeError"


def test_write_preference_call_shape_is_valid_botocore():
    fake = FakeAgentCore()
    ltm.write_preference(memory_id=MEM, actor_id=ACTOR, text="answer briefly", client=fake)
    _validate("BatchCreateMemoryRecords", fake.created[0])


def test_no_fact_writer_remains():
    assert not hasattr(ltm, "write_fact")
    assert not hasattr(ltm, "remember_fact")
    assert not hasattr(identity, "SEMANTIC_FACTS_NAMESPACE")


# --------------------------------------------------------------------------
# recall
# --------------------------------------------------------------------------

def test_recall_searches_preferences_and_session_summary_only():
    resp = {"memoryRecordSummaries": [
        {"content": {"text": "pref one"}},
        {"content": {"text": "pref two"}},
    ]}
    fake = FakeAgentCore(retrieve_response=resp)
    out = ltm.recall(memory_id=MEM, actor_id=ACTOR, query="olive", session_id="sess-1", client=fake)
    queried = [c["namespace"] for c in fake.retrieved]
    assert queried == ["/preferences/user-abc/", "/summaries/user-abc/sess-1/"]
    assert all("/facts/" not in ns for ns in queried)
    # Same canned response across namespaces → de-duplicated, order preserved.
    assert out == ["pref one", "pref two"]


def test_recall_without_session_skips_summary_namespace():
    fake = FakeAgentCore()
    ltm.recall(memory_id=MEM, actor_id=ACTOR, query="q", client=fake)
    queried = [c["namespace"] for c in fake.retrieved]
    assert queried == ["/preferences/user-abc/"]


def test_recall_rejects_empty_inputs():
    fake = FakeAgentCore()
    assert ltm.recall(memory_id="", actor_id=ACTOR, query="q", client=fake) == []
    assert ltm.recall(memory_id=MEM, actor_id=ACTOR, query="", client=fake) == []
    assert fake.retrieved == []


def test_recall_swallows_client_errors():
    class Boom:
        def retrieve_memory_records(self, **k):
            raise RuntimeError("down")
    assert ltm.recall(memory_id=MEM, actor_id=ACTOR, query="q", client=Boom()) == []


def test_recall_call_shape_is_valid_botocore():
    fake = FakeAgentCore()
    ltm.recall(memory_id=MEM, actor_id=ACTOR, query="q", client=fake)
    _validate("RetrieveMemoryRecords", fake.retrieved[0])


def test_namespaces_resolve_with_no_wildcards():
    # AgentCore rejects wildcard namespaces; every placeholder must resolve.
    for template in (identity.USER_PREFERENCE_NAMESPACE, identity.SESSION_SUMMARY_NAMESPACE):
        ns = template.format(actorId=ACTOR, sessionId="sess-1")
        assert "{" not in ns and "*" not in ns
