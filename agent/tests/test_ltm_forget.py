"""`remember` error reporting and the `forget` tool (session 2f376186).

`remember` never worked: the main runtime role lacked BatchCreateMemoryRecords, and the tool
answered every failure with "Could not save to long-term memory.", so the model treated a
permanent AccessDeniedException as transient. `forget` deletes the actor's OWN preferences
(long-term facts were removed in 2026-10, so `/facts/` is never queried); namespaces come from
the authenticated actor id only, so another actor's record cannot be deleted by construction.
"""

from __future__ import annotations

import sys
from pathlib import Path

import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import ltm  # noqa: E402

MEM = "memory-0123456789abcdef"
ME = "user-abc"
OTHER = "user-abcd"  # shares a string prefix with ME on purpose
MAIN = (SRC / "main.py").read_text()
INFRA = (SRC.parents[1] / "infra" / "lib" / "agent-infra-stack.ts").read_text()


def _validate(operation: str, params: dict):
    model = botocore.session.get_session().get_service_model("bedrock-agentcore")
    report = ParamValidator().validate(params, model.operation_model(operation).input_shape)
    assert not report.has_errors(), report.generate_report()


def _id(name: str) -> str:
    """A record id of the length the real API requires (memoryRecordId min length 40)."""
    return name.ljust(40, "0")


def _rec(rid, actor, kind, text, score=0.9):
    ns = {"facts": f"/facts/{actor}/", "preferences": f"/preferences/{actor}/"}[kind]
    return {"memoryRecordId": _id(rid), "content": {"text": text}, "namespaces": [ns],
            "memoryStrategyId": "s", "createdAt": "2026-09-16T00:00:00Z", "score": score}


class FakeMemory:
    """A memory store holding several actors' records. Retrieval honours AgentCore's PREFIX
    semantics for `namespace`, so a namespace without its trailing slash would leak OTHER's
    records into ME's results — the fake is as loose as the real service, not looser."""

    def __init__(self, records, *, leak_foreign=False):
        self.records = {r["memoryRecordId"]: r for r in records}
        self.retrieved: list[dict] = []
        self.deleted_calls: list[dict] = []
        self.leak_foreign = leak_foreign

    def retrieve_memory_records(self, **kwargs):
        self.retrieved.append(kwargs)
        ns = kwargs["namespace"]
        hits = [r for r in self.records.values()
                if self.leak_foreign or any(n.startswith(ns) for n in r["namespaces"])]
        return {"memoryRecordSummaries": hits[: kwargs["searchCriteria"]["topK"]]}

    def batch_delete_memory_records(self, **kwargs):
        self.deleted_calls.append(kwargs)
        ok = []
        for r in kwargs["records"]:
            self.records.pop(r["memoryRecordId"], None)
            ok.append({"memoryRecordId": r["memoryRecordId"], "status": "SUCCEEDED"})
        return {"successfulRecords": ok, "failedRecords": []}


def _store():
    return FakeMemory([
        _rec("mine-pref", ME, "preferences", "prefers end-to-end moderation runs"),
        _rec("mine-pref2", ME, "preferences", "prefers moderation reports in English", score=0.8),
        # A legacy record from the removed facts namespace: never queried, never deleted.
        _rec("mine-fact", ME, "facts", "olive reel lives at s3://b/olive.mp4", score=0.95),
        _rec("theirs-pref", OTHER, "preferences", "prefers end-to-end moderation runs"),
    ])


# --- namespaces ----------------------------------------------------------------------------------

def test_namespaces_are_built_from_the_actor_only():
    assert ltm.own_forgettable_namespaces(ME) == ["/preferences/user-abc/"]


@pytest.mark.parametrize("bad", ["", "  ", None, "a/b", "{actorId}", "*", " user-abc"])
def test_unsafe_actor_ids_get_no_namespaces(bad):
    assert ltm.own_forgettable_namespaces(bad) == []
    assert ltm.forget(memory_id=MEM, actor_id=bad, query="q", client=_store()).removed == []


def test_forget_never_touches_session_summaries():
    store = _store()
    ltm.forget(memory_id=MEM, actor_id=ME, query="moderation", client=store)
    assert all("/summaries/" not in c["namespace"] for c in store.retrieved)


def test_forget_never_queries_or_deletes_facts():
    store = _store()
    ltm.forget(memory_id=MEM, actor_id=ME, query="olive", max_records=10, client=store)
    assert store.retrieved and all("/facts/" not in c["namespace"] for c in store.retrieved)
    assert _id("mine-fact") in store.records
    # Even when the service returns it, a /facts/ record is outside the actor's own namespaces.
    store.leak_foreign = True
    ltm.forget(memory_id=MEM, actor_id=ME, query="olive", max_records=10, client=store)
    deleted = {r["memoryRecordId"] for c in store.deleted_calls for r in c["records"]}
    assert _id("mine-fact") not in deleted


# --- forget --------------------------------------------------------------------------------------

def test_forget_deletes_own_matches_and_reports_them():
    store = _store()
    res = ltm.forget(memory_id=MEM, actor_id=ME, query="moderation", client=store)
    assert {r["id"] for r in res.removed} == {_id("mine-pref"), _id("mine-pref2")}
    assert "prefers end-to-end moderation runs" in [r["text"] for r in res.removed]
    assert _id("theirs-pref") in store.records, "another actor's record must survive"
    assert res.error == ""
    _validate("BatchDeleteMemoryRecords", store.deleted_calls[0])
    for call in store.retrieved:
        _validate("RetrieveMemoryRecords", call)


def test_foreign_record_returned_by_the_service_is_never_deleted():
    # Even if retrieval returned everything (a service bug or a broader prefix), ownership is
    # re-checked per record before the delete.
    store = _store()
    store.leak_foreign = True
    res = ltm.forget(memory_id=MEM, actor_id=ME, query="moderation", max_records=10, client=store)
    deleted = {r["memoryRecordId"] for c in store.deleted_calls for r in c["records"]}
    assert _id("theirs-pref") not in deleted
    assert _id("theirs-pref") in store.records
    assert {r["id"] for r in res.removed} <= {_id("mine-pref"), _id("mine-pref2")}
    assert _id("mine-fact") not in deleted


def test_record_without_namespaces_is_not_deleted():
    rec = _rec("x", ME, "preferences", "t")
    rec["namespaces"] = []
    store = FakeMemory([rec])
    res = ltm.forget(memory_id=MEM, actor_id=ME, query="t", client=store)
    assert res.removed == [] and store.deleted_calls == []


def test_forget_respects_max_records_and_score_floor():
    store = FakeMemory([
        _rec("a", ME, "preferences", "a", score=0.95),
        _rec("b", ME, "preferences", "b", score=0.7),
        _rec("c", ME, "preferences", "c", score=0.1),  # below the floor: never deleted
    ])
    res = ltm.forget(memory_id=MEM, actor_id=ME, query="q", max_records=1, client=store)
    assert [r["id"] for r in res.removed] == [_id("a")]
    res = ltm.forget(memory_id=MEM, actor_id=ME, query="q", max_records=5, client=store)
    assert [r["id"] for r in res.removed] == [_id("b")]
    assert _id("c") in store.records


def test_forget_reports_the_error_class():
    class Denied:
        def retrieve_memory_records(self, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                              "RetrieveMemoryRecords")

    res = ltm.forget(memory_id=MEM, actor_id=ME, query="q", client=Denied())
    assert res.removed == [] and res.error == "AccessDeniedException"


def test_forget_with_no_matches_deletes_nothing():
    store = FakeMemory([])
    res = ltm.forget(memory_id=MEM, actor_id=ME, query="q", client=store)
    assert res.removed == [] and res.error == "" and store.deleted_calls == []


# --- remember error class ------------------------------------------------------------------------

def test_write_preference_reports_access_denied():
    class Denied:
        def batch_create_memory_records(self, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
                              "BatchCreateMemoryRecords")

    res = ltm.write_preference(memory_id=MEM, actor_id=ME, text="x", client=Denied())
    assert res.ok is False and res.error == "AccessDeniedException"
    assert "AccessDeniedException" in ltm.NON_RETRYABLE_ERRORS


def test_write_preference_reports_a_rejected_record():
    class Rejects:
        def batch_create_memory_records(self, **kwargs):
            return {"successfulRecords": [],
                    "failedRecords": [{"requestIdentifier": "r", "status": "FAILED",
                                       "errorCode": "ValidationException"}]}

    res = ltm.write_preference(memory_id=MEM, actor_id=ME, text="x", client=Rejects())
    assert res.ok is False and res.error == "ValidationException"


def test_write_preference_succeeds():
    class Ok:
        def batch_create_memory_records(self, **kwargs):
            return {"successfulRecords": [], "failedRecords": []}

    assert ltm.write_preference(memory_id=MEM, actor_id=ME, text="x", client=Ok()).ok is True


# --- wiring --------------------------------------------------------------------------------------

def test_forget_is_registered_like_remember():
    assert "def forget(query: str, max_records: int = 3)" in MAIN
    assert '"forget": forget' in MAIN
    assert '"remember", "recall", "forget"' in MAIN


def test_tools_report_error_class_not_could_not_save():
    assert 'else "Could not save to long-term memory."' not in MAIN
    assert '_ltm_error_text("Not saved to long-term memory", result.error' in MAIN


def test_forget_tool_takes_no_actor_or_namespace_input():
    start = MAIN.index("def forget(query: str")
    sig = MAIN[start:MAIN.index(")", start)]
    assert "actor" not in sig and "namespace" not in sig
    body = MAIN[start:MAIN.index("\n@tool", start) if "\n@tool" in MAIN[start:] else None]
    assert "actor_id=_current_memory_actor()" in body


def test_main_runtime_role_can_write_and_delete_memory_records():
    stmt_at = INFRA.index('"bedrock-agentcore:RetrieveMemoryRecords",\n          "bedrock-agentcore:BatchCreateMemoryRecords"')
    stmt = INFRA[stmt_at:stmt_at + 400]
    assert "bedrock-agentcore:BatchDeleteMemoryRecords" in stmt
    assert "conversationMemory.attrMemoryArn" in stmt


# --- remember: preferences only ------------------------------------------------------------------

class _CapturingMemory:
    def __init__(self):
        self.created: list[dict] = []

    def batch_create_memory_records(self, **kwargs):
        self.created.append(kwargs)
        return {"successfulRecords": [], "failedRecords": []}


@pytest.fixture
def remember_env(monkeypatch):
    import main

    store = _CapturingMemory()
    monkeypatch.setattr(main, "MEMORY_ID", MEM)
    monkeypatch.setattr(main, "_current_memory_actor", lambda: ME)
    monkeypatch.setattr(ltm, "_client", lambda region=None, client=None: store)
    return main, store


@pytest.mark.parametrize("text", [
    "the stitched reel is s3://bucket/olive_stitched.mp4",
    "reference video: https://d123.cloudfront.net/artifacts/x/output/reel.mp4",
    "source lives at artifacts/0b2c7a10-1111-4222-8333-444455556666/input/clip.mp4",
    "use projects/p-123/assets/source.mov as the master",
])
def test_remember_rejects_locations_with_a_clear_error(remember_env, text):
    main, store = remember_env
    out = main.remember(text=text)
    assert out.startswith("Not saved:")
    assert "project_status" in out and "artifacts/" in out
    assert store.created == []


def test_remember_writes_to_preferences(remember_env):
    main, store = remember_env
    out = main.remember(text="always 16:9")
    assert out == "Saved to long-term memory as a preference."
    (call,) = store.created
    assert call["records"][0]["namespaces"] == ["/preferences/user-abc/"]
    assert call["records"][0]["content"]["text"] == "always 16:9"


def test_remember_schema_has_only_text():
    import main

    props = main.remember.tool_spec["inputSchema"]["json"]["properties"]
    assert set(props) == {"text"}  # no `kind` (it only chose facts vs preferences), no `location`
    desc = main.remember.tool_spec["description"]
    assert "PREFERENCE" in desc and "NEVER" in desc
    for word in ("file locations", "URLs", "asset ids", "media"):
        assert word in desc, word


def test_recall_tool_does_not_offer_file_locations():
    import main

    desc = main.recall.tool_spec["description"]
    assert "FILE LOCATIONS" not in desc and "facts" not in desc
