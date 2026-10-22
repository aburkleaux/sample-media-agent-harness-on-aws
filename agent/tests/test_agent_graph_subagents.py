"""Knowledge-graph tools for dispatched sub-agents (Task Agent, plan nodes) — privacy exactly like search.

- `DelegatedTokenRef`: mints the sub-agent's own Project_Token from its delegation grant (DelegatedTokenIssuerFn),
  caches it, re-mints shortly before `exp`, and maps issuer answers onto the search access errors.
- `subagent_graph_binding`: binds only with `project_scope` AND a delegation grant; never raises.
- dispatch: `project_scope` (an id) rides the payload only for a graph-enabled active Project — never a token.
"""
from __future__ import annotations

import asyncio

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestration import dispatch_tool
from project_search import delegated_token as dt
from project_search import graph_tools as gt
from project_search.context import ActiveProject, TurnContext, current_turn_context, graph_scope_project_id
from project_search.token_ref import AccessEnded, NotEstablished

PID = "p" + "f" * 32


class _Issuer:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, fn, event):
        self.calls.append((fn, dict(event)))
        a = self.answers.pop(0) if self.answers else {"status": "error"}
        if isinstance(a, Exception):
            raise a
        return a


def _ref(issuer, now=1000.0, fn="fn-delegated", grant="g.r.ant"):
    clock = {"t": now}
    ref = dt.DelegatedTokenRef(PID, grant, fn=fn, invoke=issuer, clock=lambda: clock["t"])
    return ref, clock


def test_mints_from_the_grant_and_caches_until_near_expiry():
    issuer = _Issuer({"status": "ok", "token": "T1", "exp": 1000 + 3600}, {"status": "ok", "token": "T2", "exp": 9e9})
    ref, clock = _ref(issuer)
    assert asyncio.run(ref.current()) == "T1"
    assert asyncio.run(ref.current()) == "T1" and len(issuer.calls) == 1
    assert issuer.calls[0] == ("fn-delegated", {"grant": "g.r.ant", "projectId": PID})
    clock["t"] = 1000 + 3600 - dt.REISSUE_BEFORE_EXP_S + 1          # inside the re-mint window
    assert asyncio.run(ref.current()) == "T2" and len(issuer.calls) == 2


@pytest.mark.parametrize("answer,exc", [({"status": "not_found"}, AccessEnded), ({"status": "unauthenticated"}, NotEstablished),
                                        ({"status": "error"}, NotEstablished), (RuntimeError("down"), NotEstablished)])
def test_issuer_answers_map_onto_the_search_access_errors(answer, exc):
    ref, _ = _ref(_Issuer(answer))
    with pytest.raises(exc):
        asyncio.run(ref.current())


@pytest.mark.parametrize("fn,grant", [("", "g"), ("fn", "")])
def test_without_an_issuer_or_a_grant_access_is_not_established(fn, grant):
    ref, _ = _ref(_Issuer({"status": "ok", "token": "T", "exp": 9e9}), fn=fn, grant=grant)
    with pytest.raises(NotEstablished):
        asyncio.run(ref.current())


# ---- binding -----------------------------------------------------------------------------------------

@pytest.fixture
def fake_tools(monkeypatch):
    made = []

    async def make(ref, pid, *, caller="main", client=None):
        made.append((type(ref).__name__, pid, caller))
        return ["graph_tool_sentinel"]
    monkeypatch.setattr(gt, "make_graph_tools", make)
    return made


@pytest.mark.parametrize("payload", [
    {},
    {"delegation_grant": "g.r.ant"},                                   # no scope
    {"project_scope": {"project_id": PID}},                            # no grant (only a user token)
    {"project_scope": {"project_id": PID}, "user_token": "u"},
    {"project_scope": {"project_id": "nope"}, "delegation_grant": "g"},
])
def test_no_graph_tools_without_scope_and_grant(fake_tools, payload):
    assert asyncio.run(dt.subagent_graph_binding(payload)) == ([], "")
    assert fake_tools == []


def test_scope_and_grant_bind_graph_tools_with_a_delegated_token(fake_tools):
    tools, prompt = asyncio.run(dt.subagent_graph_binding(
        {"project_scope": {"project_id": PID}, "delegation_grant": "g.r.ant"}, caller="plan_node"))
    assert tools == ["graph_tool_sentinel"] and "knowledge graph" in prompt.lower()
    assert fake_tools == [("DelegatedTokenRef", PID, "plan_node")]


def test_binding_never_raises(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(gt, "make_graph_tools", boom)
    assert asyncio.run(dt.subagent_graph_binding({"project_scope": {"project_id": PID}, "delegation_grant": "g"})) == ([], "")


# ---- dispatch carries the scope, never a token --------------------------------------------------------

def _tc(graph=True, access="ok"):
    return TurnContext(turn_id="t", active_project=ActiveProject(id=PID, name="P", collection_id=PID, personal=False,
                                                                 graph=graph), web_search=False, project_access=access)


@pytest.mark.parametrize("tc,expected", [(_tc(), PID), (_tc(graph=False), None), (_tc(access="unavailable"), None), (None, None)])
def test_graph_scope_follows_the_active_project(tc, expected):
    tok = current_turn_context.set(tc)
    try:
        assert graph_scope_project_id() == expected
    finally:
        current_turn_context.reset(tok)


def test_dispatch_payload_gets_project_scope_but_no_token():
    tok = current_turn_context.set(_tc())
    try:
        payload: dict = {"prompt": "x"}
        dispatch_tool._forward_request_context(payload)
    finally:
        current_turn_context.reset(tok)
    assert payload.get("project_scope") == {"project_id": PID}
    assert "project_token" not in payload


def test_dispatch_payload_without_a_graph_project_has_no_scope():
    tok = current_turn_context.set(_tc(graph=False))
    try:
        payload: dict = {"prompt": "x"}
        dispatch_tool._forward_request_context(payload)
    finally:
        current_turn_context.reset(tok)
    assert "project_scope" not in payload
