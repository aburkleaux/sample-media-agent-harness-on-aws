"""Search telemetry tests (media-harness-search-qa-rendering design Component 9, Req 4.1-4.5).

Covers the per-searching-turn telemetry record built by ``project_search/telemetry.py``:

- **Req 4.1** — the record carries the ids, latencies, per-call outcomes, token usage and citation
  counts; one :class:`CallSample` is noted per Search_Tool call, including rejected calls and timeouts,
  with ``call_count`` exact and ``calls[]`` capped at 100.
- **Req 4.2** — the record holds no Project_Token, User_Token, answer text or ``search_term``; asserted
  over a real pipeline turn and over Hypothesis-generated answers and search terms.
- **Req 4.3** — ``emit`` never raises, even on an unserializable record.
- **Req 4.4 / 4.5** — the operator report's aggregation computes the window figures and counts a
  ``timeout`` call as a fixed 60,000 ms in the latency percentiles.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
import time
import types
import uuid

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jobs.context import reset_request_context, set_request_context  # noqa: E402

from project_search import call_checks, ledger, telemetry, turn_end  # noqa: E402
from project_search.assets import ResolvedAsset  # noqa: E402
from project_search.citations import format_citation  # noqa: E402
from project_search.context import ActiveProject, TurnContext, current_turn_context  # noqa: E402
from project_search.errors import SearchOutcome  # noqa: E402
from project_search.session_hits import MemorySessionHitStore  # noqa: E402
from project_search.token_ref import ProjectTokenRef  # noqa: E402
from project_search.tool import ProjectSearchTool  # noqa: E402
from project_search.verify import whole_second_range  # noqa: E402

_PID = "p" + "a" * 32
_SPEC = {
    "name": "tgt___hybrid-search___search_content",
    "description": "d",
    "inputSchema": {"json": {"type": "object"}},
}
_USER_TOKEN = "USER-TOKEN-" + "u" * 40
_MODEL_ID = "us.anthropic.claude-test-v1"
_SKILL_SHA = "deadbeef" * 8


def _tok() -> str:
    """A JWT-shaped Project_Token that ProjectTokenRef accepts and returns verbatim (iat now, 1 h life)."""
    iat = int(time.time())
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    header = seg({"alg": "RS256", "typ": "JWT"})
    body = seg({"sub": "u1", "project_id": _PID, "iat": iat, "exp": iat + 3600, "jti": "j1"})
    return f"{header}.{body}.sig"


class _FakeClient:
    tool_name = _SPEC["name"]

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def call(self, token, args):
        self.calls.append((token, dict(args)))
        return self.outcome


class _FakeResolver:
    def __init__(self, by_id):
        self.by_id = by_id

    def resolve(self, project_id, asset_ids):
        return {aid: self.by_id[aid] for aid in asset_ids if aid in self.by_id}


def _scope() -> call_checks.CallScope:
    return call_checks.CallScope(
        session_id="sess-scope",
        project_id=_PID,
        ingest_bucket="ingest-bkt",
        proxy_bucket="proxy-bkt",
        frame_keys=lambda: frozenset(),
    )


def _stream_last(tool, args, tuid):
    async def go():
        return [ev async for ev in tool.stream({"toolUseId": tuid, "name": _SPEC["name"], "input": args}, {})]

    return asyncio.run(go())[-1].tool_result


def _tool(turn_id, client, token=None):
    return ProjectSearchTool(
        ProjectTokenRef(token or _tok()),
        _SPEC,
        project_id=_PID,
        turn_id=turn_id,
        client=client,
        scope=_scope(),
    )


def _hit(asset_id, start_ms, end_ms, title):
    return {
        "asset_id": asset_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "final_score": 1,
        "source_index": "scene",
        "title": title,
        "text": "a scene",
    }


# ===========================================================================
# Req 4.1 — one sample per call (success, reject, timeout), exact count, cap
# ===========================================================================
def test_sample_per_call_success_reject_timeout():
    telemetry._reset_for_test()
    turn_id = "tel-samples-" + uuid.uuid4().hex
    hit = _hit("video:a", 1_000, 3_000, "Clip A")

    # 1. A success call.
    ok_tool = _tool(turn_id, _FakeClient(SearchOutcome("call", "ok", result=json.dumps({"results": [hit]}))))
    assert _stream_last(ok_tool, {"collection_id": _PID, "search_term": "q"}, "tu-ok")["status"] == "success"

    # 2. A rejected call (neither search_term nor search_image_s3uri): the Component 2 checks reject it
    #    before any gateway call, and it is STILL sampled (outcome "other").
    reject_tool = _tool(turn_id, _FakeClient(SearchOutcome("call", "ok", result=json.dumps({"results": []}))))
    assert _stream_last(reject_tool, {"collection_id": _PID}, "tu-reject")["status"] == "error"

    # 3. A timeout call.
    timeout_tool = _tool(turn_id, _FakeClient(SearchOutcome("call", "timeout")))
    assert _stream_last(timeout_tool, {"collection_id": _PID, "search_term": "q"}, "tu-to")["status"] == "error"

    record = telemetry.build_record(
        tc=types.SimpleNamespace(turn_id=turn_id),
        session_id="sess-1",
        model_id=_MODEL_ID,
        ttft_ms=120,
        total_ms=5_000,
        usage={"inputTokens": 100, "outputTokens": 20},
        outcome=None,
        skill_sha256=_SKILL_SHA,
    )
    assert record["call_count"] == 3
    assert [c["outcome"] for c in record["calls"]] == ["success", "other", "timeout"]
    assert all(isinstance(c["latency_ms"], int) for c in record["calls"])
    assert record["calls_truncated"] is False
    telemetry.release(turn_id)


def test_calls_cap_at_100_but_count_exact():
    telemetry._reset_for_test()
    turn_id = "tel-cap-" + uuid.uuid4().hex
    for _ in range(105):
        telemetry.note_call(turn_id, telemetry.CallSample(latency_ms=1, outcome="success"))
    record = telemetry.build_record(
        tc=types.SimpleNamespace(turn_id=turn_id),
        session_id="s",
        model_id=_MODEL_ID,
        ttft_ms=None,
        total_ms=1,
        usage={},
        outcome=None,
        skill_sha256=None,
    )
    assert record["call_count"] == 105
    assert len(record["calls"]) == 100
    assert record["calls_truncated"] is True
    telemetry.release(turn_id)


def test_note_call_ignores_empty_turn_and_bad_sample():
    telemetry._reset_for_test()
    telemetry.note_call("", telemetry.CallSample(1, "success"))
    telemetry.note_call("t", "not-a-sample")  # type: ignore[arg-type]
    record = telemetry.build_record(
        tc=types.SimpleNamespace(turn_id="t"),
        session_id="s", model_id=None, ttft_ms=None, total_ms=0, usage={},
        outcome=None, skill_sha256=None,
    )
    assert record["call_count"] == 0 and record["calls"] == []


# ===========================================================================
# Req 4.1 — citation counts from the TurnOutcome; shape and token mapping
# ===========================================================================
class _Rec:
    def __init__(self, verified):
        self.verified = verified


class _Outcome:
    def __init__(self, citations, truncated):
        self.citations = citations
        self.truncated = truncated


def test_citation_counts_from_outcome():
    telemetry._reset_for_test()
    outcome = _Outcome([_Rec(True), _Rec(False), _Rec(False)], truncated=True)
    record = telemetry.build_record(
        tc=types.SimpleNamespace(turn_id="t"),
        session_id="s", model_id=_MODEL_ID, ttft_ms=9, total_ms=9, usage={}, outcome=outcome,
        skill_sha256=None,
    )
    assert record["citations"] == {"total": 3, "unverified": 2, "any_unverified": True, "truncated": True}


def test_citation_counts_none_outcome_all_zero():
    record = telemetry.build_record(
        tc=types.SimpleNamespace(turn_id="t"),
        session_id="s", model_id=_MODEL_ID, ttft_ms=None, total_ms=1, usage={}, outcome=None,
        skill_sha256=None,
    )
    assert record["citations"] == {"total": 0, "unverified": 0, "any_unverified": False, "truncated": False}


def test_record_shape_and_fields():
    telemetry._reset_for_test()
    record = telemetry.build_record(
        tc=types.SimpleNamespace(turn_id="t-123"),
        session_id="s-456",
        model_id=_MODEL_ID,
        ttft_ms=9_120,
        total_ms=24_400,
        usage={"inputTokens": 21_400, "outputTokens": 380, "totalTokens": 21_780},
        outcome=None,
        skill_sha256=_SKILL_SHA,
    )
    assert record["v"] == 1 and record["kind"] == "search_turn"
    assert record["session_id"] == "s-456" and record["turn_id"] == "t-123"
    assert record["model_id"] == _MODEL_ID and record["skill_sha256"] == _SKILL_SHA
    assert record["ttft_ms"] == 9_120 and record["total_ms"] == 24_400
    assert record["tokens"] == {"in": 21_400, "out": 380}
    assert record["ts"].endswith("Z")
    # Arm-A-only: no search_arm / search_execution_mode dimension (second arm removed).
    assert "search_arm" not in record and "search_execution_mode" not in record


# ===========================================================================
# Req 4.2 — no secrets, no content
# ===========================================================================
def _run_search_turn(search_term: str, answer: str):
    """Drive one real pipeline turn (tool call → finish_turn), returning (outcome, project_token)."""
    turn_id = "tel-42-" + uuid.uuid4().hex
    session_id = "sess-" + uuid.uuid4().hex
    hit = _hit("video:z", 1_000, 3_000, "Clip Z")
    client = _FakeClient(SearchOutcome("call", "ok", result=json.dumps({"results": [hit]})))
    project_token = _tok()
    tool = _tool(turn_id, client, token=project_token)

    tc = TurnContext(
        turn_id=turn_id,
        active_project=ActiveProject(id=_PID, name="Secret Name", collection_id=_PID, personal=False),
        web_search=False,
        project_access="ok",
    )
    ctx_token = current_turn_context.set(tc)
    sess_tokens = set_request_context(actor_id="u1", session_id=session_id)
    try:
        assert _stream_last(tool, {"collection_id": _PID, "search_term": search_term}, "tu-42")["status"] == "success"
        turn_end.note_answer_text(turn_id, answer)
        resolver = _FakeResolver(
            {"video:z": ResolvedAsset(asset_id="video:z", kind="video", display_name="Clip Z", status="indexed")}
        )
        outcome = turn_end.finish_turn(store=MemorySessionHitStore(), resolver=resolver)
        record = telemetry.build_record(
            tc=tc, session_id=session_id, model_id=_MODEL_ID, ttft_ms=42, total_ms=1_000,
            usage={"inputTokens": 10, "outputTokens": 2}, outcome=outcome, skill_sha256=_SKILL_SHA,
        )
        return record, project_token  # the Project_Token the tool used
    finally:
        reset_request_context(sess_tokens)
        current_turn_context.reset(ctx_token)
        turn_end.release(turn_id)
        telemetry.release(turn_id)


def _assert_no_leak(record: dict, project_token: str, answer: str, search_term: str):
    serialized = json.dumps(record, sort_keys=True)
    assert project_token not in serialized, "the Project_Token leaked into the telemetry record"
    assert _USER_TOKEN not in serialized, "the User_Token leaked into the telemetry record"
    for secret in (answer, search_term):
        for i in range(0, max(0, len(secret) - 19)):
            window = secret[i : i + 20]
            assert window not in serialized, f"a 20-char substring leaked: {window!r}"


def test_req_4_2_no_token_answer_or_search_term_in_record():
    telemetry._reset_for_test()
    search_term = "find the dunk by Kawhi Leonard in the fourth quarter"
    answer = f"The clip shows a dunk {format_citation('Clip Z', 1_000, 3_000)} near the end of the game."
    record, project_token = _run_search_turn(search_term, answer)
    # The pipeline really saw the search term and the answer.
    assert record["call_count"] == 1
    _assert_no_leak(record, project_token, answer, search_term)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    # Uppercase letters and spaces only: such a 20-char window cannot appear in the record's fixed
    # fields (lowercase hex ids, a dotted model id, digit timestamps), so a match proves a real leak,
    # never an accidental collision with runtime values the record legitimately carries.
    search_term=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ ", min_size=20, max_size=200),
    answer_body=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ ", min_size=20, max_size=200),
)
def test_req_4_2_no_leak_property(search_term, answer_body):
    telemetry._reset_for_test()
    answer = f"{answer_body} {format_citation('Clip Z', 1_000, 3_000)}"
    record, project_token = _run_search_turn(search_term, answer)
    _assert_no_leak(record, project_token, answer, search_term)


# ===========================================================================
# Req 4.3 — emit never raises
# ===========================================================================
def test_emit_logs_marker(caplog):
    telemetry._reset_for_test()
    record = telemetry.build_record(
        tc=types.SimpleNamespace(turn_id="t"),
        session_id="s", model_id=_MODEL_ID, ttft_ms=1, total_ms=1, usage={}, outcome=None,
        skill_sha256=None,
    )
    with caplog.at_level("INFO"):
        telemetry.emit(record)
    assert any("MH_SEARCH_TELEMETRY" in r.getMessage() for r in caplog.records)


def test_emit_never_raises_on_unserializable_record():
    # A set is not JSON-serializable; emit must swallow the error (Req 4.3).
    telemetry.emit({"session_id": "s", "turn_id": "t", "bad": {1, 2, 3}})


# ===========================================================================
# Req 4.4 / 4.5 — the operator report aggregation
# ===========================================================================
def _load_report_module():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "search_telemetry_report.py")
    spec = importlib.util.spec_from_file_location("search_telemetry_report", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_report_aggregate_percentiles_and_means():
    report = _load_report_module()
    records = [
        {"ttft_ms": 100, "total_ms": 1000, "call_count": 1,
         "calls": [{"latency_ms": 100, "outcome": "success"}],
         "tokens": {"in": 10, "out": 1}, "citations": {"any_unverified": False}},
        {"ttft_ms": 200, "total_ms": 2000, "call_count": 3,
         "calls": [{"latency_ms": 200, "outcome": "success"}, {"latency_ms": 300, "outcome": "success"}],
         "tokens": {"in": 30, "out": 3}, "citations": {"any_unverified": True}},
    ]
    stats = report.aggregate(records)
    assert stats["turns"] == 2
    assert stats["ttft_ms"]["p50"] == 150.0
    assert stats["mean_calls"] == 2.0
    assert stats["mean_tokens_in"] == 20.0
    assert stats["mean_tokens_out"] == 2.0
    assert stats["share_any_unverified"] == 0.5


def test_report_counts_timeout_as_60s(monkeypatch):
    report = _load_report_module()
    # A single timeout call: its reported latency (whatever it measured) is replaced by 60,000 ms.
    records = [
        {"ttft_ms": 10, "total_ms": 60100, "call_count": 1,
         "calls": [{"latency_ms": 60012, "outcome": "timeout"}],
         "tokens": {"in": 5, "out": 0}, "citations": {"any_unverified": False}},
    ]
    stats = report.aggregate(records)
    assert stats["call_latency_ms"]["p50"] == float(report.TIMEOUT_SUBSTITUTE_MS) == 60000.0
    assert stats["call_latency_ms"]["p95"] == 60000.0


def test_report_extract_records_dedup_and_marker():
    report = _load_report_module()
    rec = {"v": 1, "kind": "search_turn", "session_id": "s1", "turn_id": "t1",
           "ttft_ms": 1, "total_ms": 2, "call_count": 0, "calls": [],
           "tokens": {"in": 0, "out": 0}, "citations": {"any_unverified": False}}
    marker_line = f"2026-10-07 INFO MH_SEARCH_TELEMETRY {json.dumps(rec, separators=(',', ':'))}"
    # OTEL-style wrapper carrying the same record in a "body" field (de-duped on session/turn).
    otel_line = json.dumps({"body": f"MH_SEARCH_TELEMETRY {json.dumps(rec, separators=(',', ':'))}"})
    noise = "2026-10-07 INFO some unrelated log line"
    out = report.extract_records([marker_line, otel_line, noise])
    assert len(out) == 1 and out[0]["turn_id"] == "t1"


def test_report_empty_window():
    report = _load_report_module()
    stats = report.aggregate([])
    assert stats["turns"] == 0
    assert stats["ttft_ms"]["p50"] is None
    assert stats["share_any_unverified"] is None
    text = report.format_report(stats, env="staging", since="0", until="1")
    assert "turns: 0" in text
