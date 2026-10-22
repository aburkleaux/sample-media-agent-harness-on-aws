"""Search_Tool binding wired into the Main_Agent (media-harness-projects-access task 11.11).

Design Component 7 "Binding rules", "Turn-context prompt block", "Dispatch and plans carry no token".
Search is arm-A only: the Main_Agent runs the Search_Tool itself. Covers:

- the Main_Agent's `prepare_turn`: a turn context binds the `ProjectSearchTool` (normal or
  `unavailable` mode), the prompt block, the spec-fetch-failure fallback, and the no-binding cases
  (agent-scoped invoke, no turn context);
- `dispatch_tool`, `custom_agent_tool`, `plan_and_execute` (direct and the plan record) never carry a
  `project_token`, and the chainer's `SECRET_PAYLOAD_KEYS` equals the agent's.
"""
from __future__ import annotations

import ast
import base64
import json
import logging
import time
from pathlib import Path

import pytest

from orchestration import dispatch_tool
from project_search import context as ps_context
from project_search import tool as ps_tool
from project_search import turn as ps_turn
from project_search.context import PROJECT_TOKEN_HEADER, current_project_token, current_turn_context
from project_search.scrub import SECRET_PAYLOAD_KEYS, payload_summary, scrub_payload
from project_search.token_ref import ProjectTokenRef
from project_search.tool import ProjectSearchTool

PID = "p" + "c" * 32
TOOL = "search_content"
SPEC = {"name": TOOL, "description": "Search.", "inputSchema": {"json": {"type": "object"}}}
HITS = [{"asset_id": "video:1", "start_ms": 1000, "end_ms": 4000}]
REPO = Path(__file__).resolve().parents[2]


def _tok(*, project_id=PID, life=3600, jti="SENTINELJTI"):
    iat = int(time.time())
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return (f"{seg({'alg': 'RS256'})}."
            f"{seg({'sub': 'u1', 'project_id': project_id, 'iat': iat, 'exp': iat + life, 'jti': jti})}"
            ".SENTINEL-SIGNATURE")


def _turn_ctx(access="ok", pid=PID):
    return {"turn_id": "turn-w", "active_project": {"id": pid, "name": "Match day", "collection_id": pid},
            "web_search": True, "project_access": access}


class _Ctx:
    def __init__(self, token=None):
        self.request_headers = {PROJECT_TOKEN_HEADER: f"Bearer {token}"} if token else {}


@pytest.fixture(autouse=True)
def _search_env(monkeypatch):
    monkeypatch.setenv("SEARCH_GATEWAY_URL", "https://search.example/mcp")
    monkeypatch.setenv("SEARCH_TOOL_NAME", TOOL)
    monkeypatch.delenv("PROJECT_TOKEN_REFRESH_FN", raising=False)
    ps_tool._spec_cache.clear()
    yield
    ps_tool._spec_cache.clear()


async def _spec_ok(client, ref):
    await ref.current()
    return dict(SPEC)


# ---------------------------------------------------------------------------
# Main_Agent: prepare_turn
# ---------------------------------------------------------------------------


def _assert_common_prompt(block: str, token: str):
    assert "Custom agents cannot search Projects in this version" in block
    assert "project_access_ended" in block and "no longer have access" in block
    assert "Never explain a search error as an expired token" in block
    assert token not in block and "SENTINEL" not in block
    assert "Web search: on" in block and "Match day" in block


@pytest.mark.asyncio
async def test_binds_search_tool_only(monkeypatch):
    monkeypatch.setattr(ps_turn, "fetch_spec", _spec_ok)
    token = _tok()
    tools, block = await ps_turn.prepare_turn(_Ctx(token), {"__turn_context": _turn_ctx()})
    assert len(tools) == 1 and isinstance(tools[0], ProjectSearchTool)
    assert tools[0].tool_name == TOOL
    assert "search_dispatch" not in block
    assert f'Active project: "Match day" — search with collection_id="{PID}"' in block
    _assert_common_prompt(block, token)
    assert isinstance(current_project_token.get(), ProjectTokenRef)


@pytest.mark.asyncio
async def test_spec_fetch_failure_binds_nothing_and_says_search_is_unavailable(monkeypatch, caplog):
    async def _boom(*_a):
        raise ConnectionError("down")

    monkeypatch.setattr(ps_turn, "fetch_spec", _boom)
    with caplog.at_level(logging.WARNING):
        tools, block = await ps_turn.prepare_turn(_Ctx(_tok()), {"__turn_context": _turn_ctx()})
    assert tools == []
    assert "Media search is unavailable this turn" in block and TOOL in block
    assert "spec fetch failed" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["principal", "agent_test", "delegated"])
async def test_agent_scoped_main_invoke_binds_nothing(monkeypatch, marker):
    monkeypatch.setattr(ps_turn, "fetch_spec", _spec_ok)
    tools, block = await ps_turn.prepare_turn(
        _Ctx(_tok()), {"__turn_context": _turn_ctx(), "agent_id": "ag", marker: {"sub": "s"}})
    assert tools == [] and block == ""


@pytest.mark.asyncio
async def test_no_turn_context_binds_nothing():
    assert await ps_turn.prepare_turn(_Ctx(_tok()), {"prompt": "hi"}) == ([], "")


# ---------------------------------------------------------------------------
# Scrubbing and the payload builders that must never carry a token
# ---------------------------------------------------------------------------


def test_scrub_and_summary_never_show_secret_values():
    token = _tok()
    p = {"prompt": "x" * 500, "project_token": token, "delegation_grant": "g.SENTINEL.g", "user_token": "u",
         "search_dispatch": {"nonce": "NONCE-SENTINEL"}, "skills": [{}, {}]}
    assert set(scrub_payload(p)) == set(p)
    s = payload_summary(p)
    assert "SENTINEL" not in s and token not in s and "<2 items>" in s


def test_chainer_secret_payload_keys_match_the_agent():
    src = (REPO / "infra/lib/lambdas/plan-chainer/handler.py").read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "SECRET_PAYLOAD_KEYS" for t in node.targets):
            assert set(ast.literal_eval(node.value.args[0])) == set(SECRET_PAYLOAD_KEYS)
            break
    else:
        pytest.fail("plan-chainer handler.py defines no SECRET_PAYLOAD_KEYS")


@pytest.fixture
def project_turn_bound():
    """A live turn: the project contextvars hold a token while other tools build payloads."""
    token = _tok()
    t1 = current_project_token.set(ProjectTokenRef(token))
    t2 = current_turn_context.set(ps_context.TurnContext.parse(_turn_ctx()))
    yield token
    current_project_token.reset(t1)
    current_turn_context.reset(t2)


def test_dispatch_tool_payload_never_carries_project_token(monkeypatch, project_turn_bound):
    sent = {}
    monkeypatch.setattr(dispatch_tool, "TASK_AGENT_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/t")
    monkeypatch.setattr(dispatch_tool, "_invoke_runtime_stream",
                        lambda arn, payload, **_: (sent.update(payload), ("ok", False, True))[1])
    dispatch_tool.dispatch_tool({"toolUseId": "t1", "input": {"prompt": "go", "tools": ["x"]}})
    assert sent and "project_token" not in sent and project_turn_bound not in json.dumps(sent)


@pytest.mark.asyncio
async def test_custom_agent_tool_payload_never_carries_project_token(monkeypatch, project_turn_bound):
    import custom_agent_tool

    sent = {}

    async def _lookup(agent_id, version=None):
        return {"assetId": agent_id, "name": "A", "version": 1}

    monkeypatch.setattr(custom_agent_tool, "_lookup_published_agent", _lookup)
    monkeypatch.setattr(custom_agent_tool, "_invoke_runtime_stream",
                        lambda arn, payload: (sent.update(payload), ("ok", False, True))[1])
    monkeypatch.setattr(custom_agent_tool, "TASK_AGENT_ARN", "arn:aws:bedrock:::r/x")
    await custom_agent_tool.invoke_custom_agent(agent_id="ag-1", prompt="go")
    assert sent and "project_token" not in sent and project_turn_bound not in json.dumps(sent)


def test_plan_direct_payload_never_carries_project_token(monkeypatch, project_turn_bound):
    from orchestration import plan_and_execute_tool as pae

    sent = {}
    monkeypatch.setattr(pae, "TASK_AGENT_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/t")
    monkeypatch.setattr(pae, "_invoke_runtime", lambda arn, payload: (sent.update(payload), ("ok", {}))[1])
    pae._dispatch_direct({"prompt": "go", "tools": ["x"]}, "tu-1")
    assert sent and "project_token" not in sent and project_turn_bound not in json.dumps(sent)


def test_plan_record_never_carries_project_token(monkeypatch, project_turn_bound):
    import planning.plan_manifest as plan_manifest
    import state_store
    from jobs import context as jobs_context
    from orchestration import plan_and_execute_tool as pae

    items: list = []

    class _Table:
        def put_item(self, Item):  # noqa: N803
            items.append(Item)

    class _Store:
        def __init__(self, *a, **k):
            self._table = _Table()

    class _Manifest:
        def __init__(self, *a, **k):
            self.s3_uri = "s3://b/m.json"

        def create(self, **k):
            return None

    class _Node:
        id, tools, depends_on, prompt, output_type, output_key, skills = "n1", ["code_interpreter"], [], "p", \
            "json", None, []

        def to_dict(self):
            return {"id": self.id, "tools": self.tools, "depends_on": [], "prompt": self.prompt}

    class _Plan:
        nodes, goal, plan_id = [_Node()], "g", "plan-x"

    monkeypatch.setattr(jobs_context, "current_actor_id", lambda: "USER#a")
    monkeypatch.setattr(jobs_context, "current_session_id", lambda: "sess-1")
    monkeypatch.setattr(jobs_context, "current_turn_id", lambda: "turn-1")
    monkeypatch.setattr(state_store, "StateStore", _Store)
    monkeypatch.setattr(plan_manifest, "PlanManifest", _Manifest)
    monkeypatch.setattr(pae, "PLAN_STATE_TABLE", "t")
    monkeypatch.setattr(pae, "JOB_REGISTRY_TABLE", "")
    monkeypatch.setattr(pae, "SKILLS_S3_BUCKET", "b")
    result = pae._dispatch_plan_async(tool_use_id="tu", plan=_Plan(), execution_mode="standard",
                                      cumulative_tokens={"inputTokens": 0, "outputTokens": 0}, session_id="s")
    assert result["status"] == "success"
    assert items and all("project_token" not in i for i in items)
    assert project_turn_bound not in json.dumps(items, default=str)
