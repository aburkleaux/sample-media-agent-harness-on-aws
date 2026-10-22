"""``ingestion.indexer`` / ``index_errors`` / ``collection`` (Req 4.1–4.3, 6.1–6.8, 7.5–7.9)."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import chunking, collection, index_errors, indexer  # noqa: E402
from ingestion.indexer import Done, Failed, IndexRun, Poll, Submit, Wait  # noqa: E402
from ingestion.m2c import ToolError, Transient, Validation  # noqa: E402


def _docs(n):
    return [{"asset_id": "video:x", "doc_type": "shot" if i < n // 2 else "scene", "start_ms": i, "end_ms": i + 1,
             "content": []} for i in range(n)]


# ------------------------------------------------------------------------------------- classifier
@pytest.mark.parametrize("err,code,p,i,kind", [
    ("Lambda.AWSLambdaException", "", None, None, index_errors.COLD_START_OR_THROTTLE),  # measured in staging
    ("Lambda is initializing your function", "", 0, 0, index_errors.COLD_START_OR_THROTTLE),
    ("ThrottlingException: Rate exceeded", "", None, 0, index_errors.COLD_START_OR_THROTTLE),
    ("Lambda.AWSLambdaException", "", 5, None, index_errors.OTHER),  # rows were written: not a cold start
    ("ClientError: ModelTimeoutException calling InvokeModel", "", None, None, index_errors.EMBEDDING_SERVICE),
    ("anything", "INVALID_PARAMETER", None, None, index_errors.CONTRACT),
    ("ValueError: content[0].field 'x' not in schema mappings", "", None, None, index_errors.CONTRACT),
    ("States.Timeout", "", None, None, index_errors.OTHER),
])
def test_classify(err, code, p, i, kind):
    assert index_errors.classify(err, code=code, prepared=p, indexed=i).kind == kind


def test_embedding_reason_text():
    assert index_errors.classify("ServiceUnavailableException").reason_message == "search backend unavailable"


# ---------------------------------------------------------------------------------- state machine
def test_happy_path_sequential_chunks_and_totals():
    run = IndexRun(n_chunks=2)
    assert run.next(0) == Submit(0)
    run = run.submitted("j1")
    assert run.next(0) == Poll("j1") and run.accepted
    run = run.status({"status": "RUNNING"}, 0)
    assert run.next(0) == Poll("j1")
    run = run.status({"status": "SUCCEEDED", "prepared": 100, "skipped": 0, "indexed": 100}, 0)
    assert run.next(0) == Submit(1)  # only after the previous chunk SUCCEEDED (Req 6.3)
    run = run.submitted("j2").status({"status": "SUCCEEDED", "prepared": 3, "skipped": 2, "indexed": 3}, 0)
    assert run.next(0) == Done(103, 2, 103)


def test_cold_start_resubmits_same_chunk_twice_then_fails():
    run = IndexRun(n_chunks=3, chunk=1).submitted("j")
    for attempt in range(2):
        run = run.status({"status": "FAILED", "error": "Lambda.AWSLambdaException"}, 100.0)
        assert run.next(100.0) == Wait(130.0)            # ≥ 30 s after the failure
        assert run.next(130.0) == Submit(1)              # same chunk, not chunk 1 (Req 7.7)
        run = run.submitted(f"j{attempt}")
    run = run.status({"status": "FAILED", "error": "Lambda.AWSLambdaException"}, 200.0)
    assert isinstance(run.next(200.0), Failed)


def test_failed_chunk_other_error_ends_run():
    run = IndexRun(n_chunks=2).submitted("j").status({"status": "FAILED", "error": "States.Runtime: x"}, 0)
    assert run.next(0) == Failed("States.Runtime: x", index_errors.OTHER)


def test_three_failed_calls_30s_apart_fail_and_success_resets():
    run = IndexRun(n_chunks=1).submitted("j")
    run = run.call_failed(Transient("t"), 0)
    assert run.next(10) == Wait(30)
    run = run.status({"status": "RUNNING"}, 30)  # a success in between resets (Req 7.9)
    assert run.call_failures == 0
    for t in (40, 70):
        run = run.call_failed(Transient("t"), t)
        assert run.outcome is None
    run = run.call_failed(Transient("t"), 100)
    assert isinstance(run.next(100), Failed)


def test_tool_refusal_and_validation_fail_immediately():
    run = IndexRun(n_chunks=1).call_failed(ToolError("documents bad", "INVALID_PARAMETER"), 0)
    assert run.next(0) == Failed("documents bad", index_errors.CONTRACT)
    run = IndexRun(n_chunks=1).call_failed(Validation("Unknown tool"), 0)
    assert isinstance(run.next(0), Failed)


# --------------------------------------------------------------------------------- blocking driver
class FakeHS:
    def __init__(self, statuses):
        self.calls, self.statuses = [], list(statuses)

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == indexer.SUBMIT_TOOL:
            return {"job_id": f"j{len(self.calls)}", "status": "RUNNING"}
        nxt = self.statuses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_run_blocking_chunks_title_and_refresh():
    docs = _docs(150)
    prepared = indexer.prepare(docs, chunking.History(True, "older", frozenset({"shot"})))
    assert prepared.refresh and [len(c) for c in prepared.chunks] == [100, 50]
    fake = FakeHS([{"status": "RUNNING"}, {"status": "SUCCEEDED", "prepared": 100, "indexed": 100},
                   {"status": "SUCCEEDED", "prepared": 50, "indexed": 50}])
    t = [0.0]
    run = indexer.run_blocking(fake, "p1", prepared, "My clip", sleep=lambda s: t.__setitem__(0, t[0] + s),
                               clock=lambda: t[0])
    assert run.outcome == Done(150, 0, 150)
    submits = [a for n, a in fake.calls if n == indexer.SUBMIT_TOOL]
    assert [len(a["documents"]) for a in submits] == [100, 50]
    assert all(a["asset_metadata"] == {"title": "My clip"} and a["collection_id"] == "p1" for a in submits)
    assert {d["asset_id"] for a in submits for d in a["documents"]} == {"video:x"}  # Req 6.1
    # Req 6.6: shot refreshes in chunk 1 only; scene first appears in chunk 1 (doc 75) → true there, false after.
    assert all(d["refresh"] for d in submits[0]["documents"])
    assert not any(d["refresh"] for d in submits[1]["documents"])


def test_run_blocking_identical_record_submits_no_refresh():
    docs = _docs(4)
    first = indexer.prepare(docs, chunking.History())
    again = indexer.prepare(docs, chunking.History(True, first.digest))
    assert not first.refresh and not again.refresh  # Req 6.4, 6.5


# ------------------------------------------------------------------------------------- collection
class FakeColl:
    """Answers create_collection with ``result``; every configure call succeeds and is recorded."""

    def __init__(self, result, stored=None, list_error=None, configure_error=None):
        self.result, self.args, self.configured = result, None, []
        self.stored, self.list_error, self.configure_error = stored, list_error, configure_error

    def call_tool(self, name, args):
        if name == collection.LIST_TOOL:
            if self.list_error:
                raise self.list_error
            return {"collections": [] if self.stored is None else
                    [{"collection_id": "other", "retrieval_paths": {"visual": {"shot_ann": 9}}},
                     {"collection_id": "p1", "retrieval_paths": self.stored}]}
        if name == collection.CONFIGURE_TOOL:
            if self.configure_error:
                raise self.configure_error
            self.configured.append(args)
            return {"updated": True}
        self.args = (name, args)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_collection_ensure_four_scopes_and_exists_is_success():
    f = FakeColl({"created": True})
    assert collection.ensure(f, "p1", "Trailer cuts") is True
    assert f.args == (collection.CREATE_TOOL, {"collection_id": "p1",
                                               "active_scopes": ["shot", "scene", "utterance", "topic"],
                                               "name": "Trailer cuts"})
    assert f.configured == [collection.configure_arguments("p1")]
    # An existing collection (a retried create) is still configured; a real create failure configures nothing.
    exists = FakeColl(ToolError("collection already exists"))
    assert collection.ensure(exists, "p1", "x") is False and exists.configured == [collection.configure_arguments("p1")]
    assert collection.ensure(FakeColl({"created": False}), "p1", "x") is False
    bad = FakeColl(ToolError("collection_id must match", "INVALID_PARAMETER"))
    with pytest.raises(ToolError):
        collection.ensure(bad, "p1", "x")
    assert bad.configured == []


# alpha.20261017a: configure_search_backend REFUSES a write that omits a stored sub-query (PARTIAL_REPLACEMENT_PAYLOAD),
# and new collections inherit dialogue.topic_ann -- the harness's fixed set made every customer install fail indexing.
TOOLKIT_DEFAULTS = {
    "visual": {"shot_ann": 0.7, "scene_ann": 0.5},
    "text_overlay": {"scene_text_fts": 0.7},
    "entity_fts": {"scene_named_entities_fts": 0.9},
    "dialogue": {"utterance_text_fts": 0.8, "utterance_ann": 0.4, "topic_ann": 0.4},
}


def test_collection_weights_are_merged_with_what_is_stored():
    f = FakeColl({"created": True}, stored=TOOLKIT_DEFAULTS)
    collection.ensure(f, "p1", "x")
    (sent,) = f.configured
    paths = sent["retrieval_paths"]
    assert paths["dialogue"]["topic_ann"] == 0.4                 # stored-only sub-query kept
    for path, subs in collection.RETRIEVAL_PATHS.items():        # every harness weight sent
        for k, v in subs.items():
            assert paths[path][k] == v
    assert "replace_all" not in sent


def test_the_harness_weight_wins_where_both_name_a_sub_query():
    stored = {**TOOLKIT_DEFAULTS, "visual": {"shot_ann": 0.2, "scene_ann": 0.5, "topic_ann": 0.9}}
    f = FakeColl({"created": False}, stored=stored)
    collection.ensure(f, "p1", "x")
    assert f.configured[0]["retrieval_paths"]["visual"] == collection.RETRIEVAL_PATHS["visual"]


def test_a_read_failure_falls_back_to_the_full_write():
    f = FakeColl({"created": True}, list_error=ToolError("Unknown tool: hybrid-search___list_collections"))
    collection.ensure(f, "p1", "x")
    assert f.configured == [collection.configure_arguments("p1")]
    old = FakeColl({"created": True}, stored=None)               # an older toolkit: no retrieval_paths returned
    collection.ensure(old, "p1", "x")
    assert old.configured == [collection.configure_arguments("p1")]


def test_nothing_is_written_when_the_stored_weights_already_match():
    f = FakeColl({"created": False}, stored=collection.merged_paths(TOOLKIT_DEFAULTS))
    collection.ensure(f, "p1", "x")
    assert f.configured == []


def test_a_configure_refusal_is_reported_as_configure_not_create():
    f = FakeColl({"created": False}, stored=None, configure_error=ToolError("PARTIAL_REPLACEMENT_PAYLOAD", "PARTIAL"))
    with pytest.raises(ToolError) as e:
        collection.ensure(f, "p1", "x")
    assert getattr(e.value, "step", "") == "configure_search_backend"
