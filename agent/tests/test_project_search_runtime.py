"""Unit tests for agent/src/project_search (media-harness-projects-access tasks 1.1, 4.1, 11.1–11.5, 11.11 arm A).

The AgentCore error shapes asserted here were observed on a live CUSTOM_JWT + Cedar gateway
(docs/prototypes/media-harness-projects/validation/spike_v10_gateway.py).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time

import pytest

from project_search import errors, ledger
from project_search.binding import SearchDescriptor, resolve_search_binding
from project_search.context import TurnContext, header_value
from project_search.errors import SearchOutcome, classify
from project_search.token_ref import (REFRESH_GRACE_S, AccessEnded, NotEstablished, ProjectTokenRef,
                                      RefreshOutcome)
from project_search.tool import ProjectSearchTool
from projects.ids import is_project_id

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "..", "infra", "lib", "projects", "fixtures",
                       "project-id-cases.json")
PID = "p" + "a" * 32


def _tok(*, sub="u1", project_id=PID, iat=None, life=3600, jti="j1"):
    iat = int(time.time()) if iat is None else iat
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'RS256', 'typ': 'JWT'})}.{seg({'sub': sub, 'project_id': project_id, 'iat': iat, 'exp': iat + life, 'jti': jti})}.sig"


# ---- task 1.1: ids ---------------------------------------------------------------------------
def test_project_id_fixture_cases():
    cases = json.load(open(FIXTURE))
    assert all(is_project_id(v) for v in cases["valid"])
    assert not any(is_project_id(v) for v in cases["invalid"])
    assert not any(is_project_id(v) for v in cases["invalidNonStrings"])


# ---- task 11.2: error contract ------------------------------------------------------------------
@pytest.mark.parametrize("outcome,category", [
    (SearchOutcome("initialize", "rpc_error", -32001, "Invalid Bearer token"), errors.PROJECT_ACCESS_UNAVAILABLE),
    (SearchOutcome("initialize", "rpc_error", -32002, "insufficient_scope - The request requires higher privileges"), errors.PROJECT_ACCESS_UNAVAILABLE),
    (SearchOutcome("initialize", "http", 401), errors.PROJECT_ACCESS_UNAVAILABLE),
    (SearchOutcome("call", "rpc_error", -20001, "Tool Execution Denied: Tool call not allowed due to policy enforcement"), errors.POLICY_DENIED),
    (SearchOutcome("call", "http", 403), errors.POLICY_DENIED),
    (SearchOutcome("initialize", "transport", None, "ConnectError"), errors.GATEWAY_UNREACHABLE),
    (SearchOutcome("initialize", "http", 502), errors.GATEWAY_UNREACHABLE),
    (SearchOutcome("call", "timeout"), errors.TIMEOUT),
    (SearchOutcome("call", "ok", result='{"error":true,"errorCode":"SEARCH_FAILED","message":"Lambda.AWSLambdaException"}'), errors.SEARCH_FAILED),
    (SearchOutcome("call", "ok", result='{"errorCode":"SEARCH_TIMEOUT"}'), errors.TIMEOUT),
])
def test_classify(outcome, category):
    got = classify(outcome)
    assert got and got[0] == category
    assert "expire" not in got[1].lower()


def test_classify_success_and_redaction():
    assert classify(SearchOutcome("call", "ok", result='{"results":[]}')) is None
    msg = errors.compose_reason("failed with Bearer eyJabc.def.ghi and eyJhbGciOiJSUzI1.eyJzdWIiOiJ1MSJ9.c2lnbmF0dXJl")
    assert "eyJ" not in msg


# ---- task 4.1: ProjectTokenRef ------------------------------------------------------------------
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


async def _nosleep(_):
    return None


def _run(c):
    return asyncio.run(c)


def test_ref_returns_token_outside_threshold_and_redacts_repr():
    ref = ProjectTokenRef(_tok(iat=1000), clock=Clock(1100), sleep=_nosleep)
    assert _run(ref.current()) == _tok(iat=1000)
    assert "sig" not in repr(ref) and "jti=j1" in repr(ref)


def test_ref_refreshes_inside_threshold_and_after_long_quiet_stretch():
    calls = []

    async def refresher(raw):
        calls.append(raw)
        return RefreshOutcome.ok(_tok(iat=int(clock.t), jti=f"j{len(calls) + 1}"))
    clock = Clock(1000 + 3500)
    ref = ProjectTokenRef(_tok(iat=1000), refresher=refresher, clock=clock, sleep=_nosleep)
    assert ref.jti == "j1"
    _run(ref.current())
    assert ref.jti == "j2"
    clock.t += 70 * 60 + 3600  # quiet for longer than a token lifetime, inside the 8 h grace
    _run(ref.current())
    assert ref.jti == "j3" and len(calls) == 2


def test_ref_access_ended_is_sticky():
    async def refresher(raw):
        return RefreshOutcome.access_ended()
    ref = ProjectTokenRef(_tok(iat=1000), refresher=refresher, clock=Clock(1000 + 3550), sleep=_nosleep)
    for _ in range(2):
        with pytest.raises(AccessEnded):
            _run(ref.current())


def test_ref_rejects_refresh_for_other_project_and_past_grace():
    async def refresher(raw):
        return RefreshOutcome.ok(_tok(project_id="p" + "b" * 32, iat=1000 + 3550))
    ref = ProjectTokenRef(_tok(iat=1000), refresher=refresher, clock=Clock(1000 + 3550), sleep=_nosleep)
    assert _run(ref.current())  # mismatched refresh ignored; still-valid token used (Req 4.25)
    assert ref.project_id == PID
    late = ProjectTokenRef(_tok(iat=1000), refresher=refresher, clock=Clock(1000 + 3600 + REFRESH_GRACE_S + 1))
    with pytest.raises(NotEstablished):
        _run(late.current())


def test_ref_single_flight():
    n = {"c": 0}

    async def refresher(raw):
        n["c"] += 1
        await asyncio.sleep(0)
        return RefreshOutcome.ok(_tok(iat=1000 + 3550, jti="j2"))

    async def go():
        ref = ProjectTokenRef(_tok(iat=1000), refresher=refresher, clock=Clock(1000 + 3550), sleep=_nosleep)
        await asyncio.gather(*[ref.current() for _ in range(8)])
    _run(go())
    assert n["c"] == 1


# ---- task 11.1: turn context and header --------------------------------------------------------
def test_turn_context_parse_is_strict():
    good = {"turn_id": "t1", "active_project": {"id": PID, "name": "X", "collection_id": PID, "personal": False},
            "web_search": False, "project_access": "ok"}
    assert TurnContext.parse(good).active_project.collection_id == PID
    # A stray search_arm the proxy may still send is ignored, not fatal (search is arm-A only).
    assert TurnContext.parse({**good, "search_arm": "C"}) is not None
    for bad in ({**good, "project_access": "maybe"},
                {**good, "active_project": {**good["active_project"], "collection_id": "default"}},
                {**good, "active_project": {**good["active_project"], "id": "default"}}, None, "x"):
        assert TurnContext.parse(bad) is None


def test_header_value_case_insensitive():
    assert header_value({"x-amzn-bedrock-agentcore-runtime-custom-projecttoken": "Bearer abc"}) == "abc"
    assert header_value({}) == "" and header_value(None) == ""


# ---- task 11.11: binding -------------------------------------------------------------------------
@pytest.mark.parametrize("d,want", [
    (SearchDescriptor("main", True, "ok", has_token=True), "search_tool"),
    (SearchDescriptor("main", True, "unavailable"), "search_tool_unavailable"),
    (SearchDescriptor("main", True, "ok", has_token=False), "search_tool_unavailable"),
    (SearchDescriptor("main", False), None),
    (SearchDescriptor("main", True, "ok", has_token=True, agent_scoped=True), None),
    (SearchDescriptor("task"), None),
])
def test_binding(d, want):
    assert resolve_search_binding(d) == want


# ---- task 11.5: ProjectSearchTool ----------------------------------------------------------------
class FakeClient:
    url, tool_name = "http://gw/mcp", "tgt___hybrid-search___search_content"

    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    async def call(self, token, args):
        self.calls.append((token, dict(args)))
        return self.outcome


SPEC = {"name": FakeClient.tool_name, "description": "d", "inputSchema": {"json": {"type": "object"}}}


def _stream(tool, args, tuid="tu1"):
    async def go():
        return [ev async for ev in tool.stream({"toolUseId": tuid, "name": SPEC["name"], "input": args}, {})]
    return _run(go())[-1].tool_result


def test_tool_success_passes_args_through_and_records_ledger():
    hits = [{"asset_id": "video:1", "start_ms": 0, "end_ms": 1000}]
    c = FakeClient(SearchOutcome("call", "ok", result=json.dumps({"results": hits})))
    raw = _tok()
    t = ProjectSearchTool(ProjectTokenRef(raw), SPEC, project_id=PID, turn_id="turn-x", client=c)
    r = _stream(t, {"collection_id": PID, "search_term": "boat", "asset_kinds": ["video"]})
    assert r["status"] == "success"
    # asset_kinds never sent; k is raised to TAB_FETCH_K so the Moments tab can show more than the model reads.
    assert c.calls == [(raw, {"collection_id": PID, "search_term": "boat", "k": 50})]
    assert [e.hits for e in ledger.results_for("turn-x")] == [hits]
    # The call's search term rides on the entry: the Moments tab's search box starts from it.
    assert [e.search_term for e in ledger.results_for("turn-x")] == ["boat"]


def test_tool_policy_denied_no_hits_no_ledger():
    c = FakeClient(SearchOutcome("call", "rpc_error", -20001, "Tool Execution Denied: ... policy enforcement"))
    t = ProjectSearchTool(ProjectTokenRef(_tok()), SPEC, project_id=PID, turn_id="turn-deny", client=c)
    r = _stream(t, {"collection_id": "p" + "b" * 32})
    assert r["status"] == "error"
    assert json.loads(r["content"][0]["text"])["error"]["category"] == errors.POLICY_DENIED
    assert ledger.results_for("turn-deny") == []


def test_tool_unavailable_makes_no_request():
    c = FakeClient(SearchOutcome("call", "ok", result="{}"))
    t = ProjectSearchTool(None, SPEC, project_id=PID, turn_id="t", client=c, unavailable=True)
    r = _stream(t, {"collection_id": PID})
    assert json.loads(r["content"][0]["text"])["error"]["message"] == errors.ACCESS_NOT_ESTABLISHED_TEXT
    assert c.calls == []


def test_tool_dispatch_frame_sink():
    frames = []
    c = FakeClient(SearchOutcome("call", "ok", result=json.dumps({"results": [{"asset_id": "audio:1"}]})))
    t = ProjectSearchTool(ProjectTokenRef(_tok()), SPEC, project_id=PID, turn_id="t", client=c,
                          dispatch_nonce="n1", frame_sink=frames.append)
    _stream(t, {"collection_id": PID})
    assert frames == [{"type": "search_result", "nonce": "n1", "tool_use_id": "tu1", "project_id": PID,
                       "ok": True, "category": None, "hits": [{"asset_id": "audio:1"}]}]
