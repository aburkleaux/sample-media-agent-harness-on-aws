"""Agent-side knowledge-graph tools: the graph extension of project search, bound and called exactly like search.

- `ProjectGraphTool`: `project_id` is removed from the model-visible schema and always set by the harness
  (a model value is overwritten), the call goes through the Search_Gateway client with the Project_Token, and
  errors map onto the search error contract (policy denial worded for the graph, access errors, Lambda errors).
- `make_graph_tools`: only `project-graph___` tools from `tools/list`, never raises.
- `prepare_turn`: graph tools bind only for a graph-enabled active Project with a Project_Token, project access,
  and a non-scoped invoke — plus the graph prompt line and skill; never otherwise.
- the bundled `project-knowledge-graph` skill loads and states its rules.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import skills_manager
from project_search import errors
from project_search import graph_tools as gt
from project_search import skill_text
from project_search import turn as ps_turn
from project_search.errors import SearchOutcome
from project_search.token_ref import AccessEnded, NotEstablished

from test_search_binding_wiring import PID, _Ctx, _spec_ok, _tok, _turn_ctx  # noqa: F401  (fixture reused below)
from test_search_binding_wiring import _search_env  # noqa: F401

OTHER = "p" + "d" * 32
SHIPPED_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"


def _gw_tool(name, props=None, required=None):
    schema = {"type": "object", "properties": {"project_id": {"type": "string"}, **(props or {})},
              "required": ["project_id", *(required or [])]}
    return SimpleNamespace(name=name, description=f"{name} desc", inputSchema=schema, input_schema=schema)


GW_TOOLS = [_gw_tool("project-graph___graph_find", {"text": {"type": "string"}}, ["text"]),
            _gw_tool("project-graph___graph_schema"),
            _gw_tool("mediaworks-harness-m2c-search___hybrid-search___search_content")]


class _Ref:
    def __init__(self, exc=None):
        self.exc, self.calls = exc, 0

    async def current(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return "TOKEN"


class _Client:
    url = "https://search.example/mcp"

    def __init__(self, outcome=None):
        self.outcome = outcome or SearchOutcome("call", "ok", result=json.dumps({"ok": True, "nodes": []}))
        self.calls = []

    async def list_tools(self, token):
        return GW_TOOLS

    async def call_tool(self, token, name, args):
        self.calls.append((token, name, dict(args)))
        return self.outcome


def _run(tool, args):
    async def go():
        return [e async for e in tool.stream({"toolUseId": "u1", "name": tool.tool_name, "input": args}, {})]
    ev = asyncio.run(go())[0]
    return ev.tool_result if hasattr(ev, "tool_result") else ev["tool_result"]


@pytest.fixture(autouse=True)
def _clear_cache():
    gt._spec_cache.clear()
    yield
    gt._spec_cache.clear()


def _tools(client=None, ref=None):
    return asyncio.run(gt.make_graph_tools(ref or _Ref(), PID, client=client or _Client()))


# ---- tool construction ------------------------------------------------------------------------------

def test_only_graph_tools_bind_and_project_id_is_hidden_from_the_model():
    tools = _tools()
    assert sorted(t.tool_name for t in tools) == ["graph_find", "graph_schema"]
    find = next(t for t in tools if t.tool_name == "graph_find")
    schema = find.tool_spec["inputSchema"]["json"]
    assert "project_id" not in schema["properties"] and schema["required"] == ["text"]


def test_make_graph_tools_never_raises_and_needs_a_gateway_and_token():
    class Boom(_Client):
        async def list_tools(self, token):
            raise RuntimeError("down")
    assert asyncio.run(gt.make_graph_tools(_Ref(), PID, client=Boom())) == []
    assert asyncio.run(gt.make_graph_tools(None, PID, client=_Client())) == []
    no_url = _Client()
    no_url.url = ""
    assert asyncio.run(gt.make_graph_tools(_Ref(), PID, client=no_url)) == []


# ---- calls ------------------------------------------------------------------------------------------

def test_harness_sets_project_id_even_when_the_model_supplies_another():
    client = _Client()
    find = next(t for t in _tools(client) if t.tool_name == "graph_find")
    res = _run(find, {"text": "uma", "project_id": OTHER})
    assert res["status"] == "success"
    token, name, args = client.calls[0]
    assert (token, name) == ("TOKEN", "project-graph___graph_find")
    assert args == {"text": "uma", "project_id": PID}


def test_policy_denial_is_worded_for_the_graph():
    out = SearchOutcome("call", "rpc_error", status=-32002, message="Tool Execution Denied by policy enforcement")
    find = next(t for t in _tools(_Client(out)) if t.tool_name == "graph_find")
    body = json.loads(_run(find, {"text": "x"})["content"][0]["text"])
    assert body["error"]["category"] == errors.POLICY_DENIED
    assert "only the active project's knowledge graph" in body["error"]["message"]


def test_lambda_errors_reach_the_model_as_their_code():
    out = SearchOutcome("call", "ok", result=json.dumps({"ok": False, "error": {"code": "unlabelled_node", "message": "label it"}}))
    find = next(t for t in _tools(_Client(out)) if t.tool_name == "graph_find")
    res = _run(find, {"text": "x"})
    assert res["status"] == "error"
    assert json.loads(res["content"][0]["text"])["error"]["category"] == "unlabelled_node"


@pytest.mark.parametrize("exc,category", [(AccessEnded(), errors.PROJECT_ACCESS_ENDED),
                                          (NotEstablished(), errors.PROJECT_ACCESS_UNAVAILABLE)])
def test_access_errors_never_call_the_gateway(exc, category):
    client = _Client()
    tools = _tools(client)                      # spec fetch uses a working ref
    find = next(t for t in tools if t.tool_name == "graph_find")
    find._ref = _Ref(exc)
    body = json.loads(_run(find, {"text": "x"})["content"][0]["text"])
    assert body["error"]["category"] == category and client.calls == []
    assert "token" not in body["error"]["message"].lower()


# ---- prepare_turn binding ---------------------------------------------------------------------------

@pytest.fixture
def graph_specs(monkeypatch):
    monkeypatch.setattr(ps_turn, "fetch_spec", _spec_ok)

    async def specs(client, ref):
        await ref.current()
        return [gt._model_spec(t) for t in GW_TOOLS[:2]]
    monkeypatch.setattr(gt, "fetch_graph_specs", specs)


def _turn(tc, token=None, **payload):
    return asyncio.run(ps_turn.prepare_turn(_Ctx(token), {"__turn_context": tc, **payload}))


def _graph_tc(**kw):
    tc = _turn_ctx(**kw)
    tc["active_project"]["graph"] = True
    return tc


def test_graph_enabled_turn_binds_graph_tools_and_rules(graph_specs):
    tools, block = _turn(_graph_tc(), _tok())
    names = [t.tool_name for t in tools]
    assert "graph_find" in names and "graph_schema" in names
    assert "Knowledge graph: this project has one" in block


@pytest.mark.parametrize("case", ["no_graph", "no_token", "unavailable", "scoped"])
def test_no_graph_tools_otherwise(graph_specs, case):
    tc = _turn_ctx() if case == "no_graph" else _graph_tc(access="unavailable" if case == "unavailable" else "ok")
    extra = {"agent_test": True} if case == "scoped" else {}
    tools, block = _turn(tc, None if case == "no_token" else _tok(), **extra)
    assert not [t for t in tools if t.tool_name.startswith("graph_")]
    assert "Knowledge graph: this project has one" not in block


def test_a_payload_without_turn_context_binds_nothing(graph_specs):
    _turn(_graph_tc(), _tok())                  # leaves contextvars set in this context
    tools, block = asyncio.run(ps_turn.prepare_turn(_Ctx(_tok()), {}))
    assert tools == [] and block == ""


# ---- skill ------------------------------------------------------------------------------------------

def test_bundled_skill_loads_with_its_rules(monkeypatch):
    monkeypatch.setattr(skills_manager, "SKILLS_DIR", SHIPPED_SKILLS_DIR)
    skill_text._reset_graph_cache_for_test()
    try:
        s = skill_text.load_project_knowledge_graph()
        assert s is not None and s.name == "project-knowledge-graph"
        for rule in ("KG-S1", "KG-S2", "KG-Q2", "KG-C1", "KG-A2"):
            assert f"**{rule}**" in s.instructions
        assert len(s.instructions.encode()) < 12_288
        assert skill_text.graph_prompt_section(s).startswith("\n## Knowledge graph rules")
        assert not (SHIPPED_SKILLS_DIR / "project-knowledge-graph" / "tools.json").exists(), "binds no tools itself"
    finally:
        skill_text._reset_graph_cache_for_test()
