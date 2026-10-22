"""Property 14: Search_Tool binding per arm (media-harness-projects-access task 11.12).

**Validates: Requirements 4.12, 4.16, 4.18, 4.19, 5.3, 5.5, 5.13, 9.10**

Hypothesis over invocation descriptors: whether a turn context is present, `project_access`, token
presence, agent scoping, and origin. Each descriptor is turned into the real Main_Agent payload (with
the real AgentCore context carrying the ProjectToken header) and run through the real wiring
`project_search.turn.prepare_turn`, cross-checked against the pure `resolve_search_binding`. Search is
arm-A only: the Main_Agent runs the Search_Tool itself. The payload builders of `dispatch_tool`,
`plan_and_execute` (direct dispatch and the plan record), and the plan chainer run against fake
runtimes while a turn's token is bound, and must never carry a `project_token`.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from orchestration import dispatch_tool
from project_search import tool as ps_tool
from project_search import turn as ps_turn
from project_search.binding import SearchDescriptor, resolve_search_binding
from project_search.context import PROJECT_TOKEN_HEADER, TurnContext, current_project_token, current_turn_context
from project_search.token_ref import ProjectTokenRef
from project_search.tool import ProjectSearchTool

PID = "p" + "e" * 32
TOOL = "search_content"
SPEC = {"name": TOOL, "description": "Search.", "inputSchema": {"json": {"type": "object"}}}
REPO = Path(__file__).resolve().parents[2]

ORIGINS = ("chat_turn", "search_dispatch", "dispatch_tool", "plan_node", "chainer_node", "custom_agent",
           "external_invoke", "slack")
SCOPES = (None, "principal", "agent_test", "delegated")
SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


def _tok(project_id=PID, jti="P14JTI"):
    iat = int(time.time())
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return (f"{seg({'alg': 'RS256'})}."
            f"{seg({'sub': 'u1', 'project_id': project_id, 'iat': iat, 'exp': iat + 3600, 'jti': jti})}.P14SIG")


TOKEN = _tok()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SEARCH_GATEWAY_URL", "https://search.example/mcp")
    monkeypatch.setenv("SEARCH_TOOL_NAME", TOOL)
    monkeypatch.delenv("PROJECT_TOKEN_REFRESH_FN", raising=False)

    async def _spec(client, ref):
        return dict(SPEC)

    monkeypatch.setattr(ps_turn, "fetch_spec", _spec)
    ps_tool._spec_cache.clear()


descriptors = st.fixed_dictionaries({
    "runtime": st.sampled_from(("main", "task")),
    "arm": st.sampled_from(("A", "B", None)),  # None = no __turn_context (Slack, session-less)
    "access": st.sampled_from(("ok", "unavailable")),
    "mode": st.sampled_from((None, "search_dispatch", "plan", "render")),
    "token": st.booleans(),
    "nonce": st.booleans(),
    "scope": st.sampled_from(SCOPES),
    "origin": st.sampled_from(ORIGINS),
})


def _scope_fields(scope):
    if scope is None:
        return {}
    return {"agent_id": "ag-1", scope: ({"issuer": "https://i", "sub": "s"} if scope == "principal" else True)}


# ---------------------------------------------------------------------------
# Main_Agent
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, token):
        self.request_headers = {PROJECT_TOKEN_HEADER: f"Bearer {token}"} if token else {}


def _main_payload(d):
    payload = {"prompt": "find the goal", **_scope_fields(d["scope"])}
    if d["arm"] is not None and d["origin"] != "slack":
        payload["__turn_context"] = {
            "turn_id": "t-p14", "active_project": {"id": PID, "name": "P", "collection_id": PID},
            "web_search": True, "project_access": d["access"], "search_arm": d["arm"]}
    return payload


def _main_token(d):
    # The turn gate mints a token exactly when access is `ok` (Req 4.1, 4.17).
    return TOKEN if d["access"] == "ok" else None


def _run_main(d):
    async def go():
        tools, block = await ps_turn.prepare_turn(_Ctx(_main_token(d)), _main_payload(d))
        return tools, block, current_turn_context.get()
    return asyncio.run(go())


@SETTINGS
@given(d=descriptors)
def test_main_agent_binding(d):
    tools, _block, _tc = _run_main(d)
    has_tc = d["arm"] is not None and d["origin"] != "slack"
    scoped = d["scope"] is not None
    search_tools = [t for t in tools if isinstance(t, ProjectSearchTool)]

    want_search_tool = has_tc and not scoped
    assert bool(search_tools) == want_search_tool
    assert len(search_tools) <= 1     # at most one tool reaching search_content
    if search_tools:
        assert search_tools[0]._unavailable == (d["access"] == "unavailable")
    # The pure function agrees with the wiring (search is arm-A only; any arm field is ignored).
    pure = resolve_search_binding(SearchDescriptor(
        runtime="main", has_turn_context=has_tc, project_access=d["access"],
        has_token=_main_token(d) is not None, agent_scoped=scoped))
    assert pure == (None if not want_search_tool else
                    "search_tool" + ("_unavailable" if d["access"] == "unavailable" else ""))


# ---------------------------------------------------------------------------
# Payload builders never carry a project_token
# ---------------------------------------------------------------------------


def _load_chainer():
    here = REPO / "infra/lib/lambdas/plan-chainer"
    layer = REPO / "infra/lib/lambdas/_layers/result-resolution/python"
    env = {"PLAN_STATE_TABLE": "t", "JOB_REGISTRY_TABLE": "j",
           "TASK_AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-west-2:1:runtime/t"}
    saved_env = {k: os.environ.get(k) for k in env}
    saved_path = list(sys.path)
    os.environ.update(env)
    sys.path[:0] = [str(here), str(layer)]
    try:
        spec = importlib.util.spec_from_file_location("_p14_plan_chainer_handler", here / "handler.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path[:] = saved_path
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(scope="module")
def chainer():
    return _load_chainer()


@pytest.fixture
def bound_turn():
    t1 = current_project_token.set(ProjectTokenRef(TOKEN))
    t2 = current_turn_context.set(TurnContext.parse({
        "turn_id": "t", "active_project": {"id": PID, "name": "P", "collection_id": PID},
        "web_search": False, "project_access": "ok"}))
    yield
    current_project_token.reset(t1)
    current_turn_context.reset(t2)


builder_inputs = st.fixed_dictionaries({
    "prompt": st.text(min_size=1, max_size=40),
    "tools": st.lists(st.sampled_from(["code_interpreter", "artifact", TOOL, "usr-x___run"]), max_size=3),
    "stray": st.booleans(),  # a model-supplied `project_token` argument
})


@SETTINGS
@given(inp=builder_inputs)
def test_payload_builders_never_carry_project_token(inp, bound_turn, chainer):
    from orchestration import plan_and_execute_tool as pae

    sent: list[dict] = []
    extra = {"project_token": TOKEN} if inp["stray"] else {}

    # dispatch_tool
    old = (dispatch_tool.TASK_AGENT_ARN, dispatch_tool._invoke_runtime_stream)
    dispatch_tool.TASK_AGENT_ARN = "arn:aws:bedrock-agentcore:us-west-2:1:runtime/t"
    dispatch_tool._invoke_runtime_stream = lambda arn, payload, **_: (sent.append(dict(payload)), ("ok", False, True))[1]
    try:
        dispatch_tool.dispatch_tool({"toolUseId": "t1", "input": {"prompt": inp["prompt"], "tools": inp["tools"],
                                                                  **extra}})
    finally:
        dispatch_tool.TASK_AGENT_ARN, dispatch_tool._invoke_runtime_stream = old

    # plan_and_execute direct dispatch
    old = (pae.TASK_AGENT_ARN, pae._invoke_runtime)
    pae.TASK_AGENT_ARN = "arn:aws:bedrock-agentcore:us-west-2:1:runtime/t"
    pae._invoke_runtime = lambda arn, payload: (sent.append(dict(payload)), ("ok", {}))[1]
    try:
        pae._dispatch_direct({"prompt": inp["prompt"], "tools": inp["tools"], **extra}, "tu")
    finally:
        pae.TASK_AGENT_ARN, pae._invoke_runtime = old

    # The chainer's node payload (built from the plan record), with a fake AgentCore client.
    class _AC:
        class exceptions:  # noqa: N801
            class ClientError(Exception):
                pass

        def invoke_agent_runtime(self, *, agentRuntimeArn, qualifier, payload, contentType, **_k):  # noqa: N803
            sent.append(json.loads(payload))
            return {"response": b""}

    class _T:
        class meta:  # noqa: N801
            class client:  # noqa: N801
                class exceptions:  # noqa: N801
                    class ConditionalCheckFailedException(Exception):
                        pass

        def put_item(self, **_k):
            return {}

        def update_item(self, **_k):
            return {}

    saved = (chainer._agentcore, chainer._plan_table, chainer._job_table, chainer.SKILLS_S3_BUCKET)
    chainer._agentcore, chainer._plan_table, chainer._job_table, chainer.SKILLS_S3_BUCKET = _AC(), _T(), _T(), ""
    try:
        node = {"id": "n1", "prompt": inp["prompt"], "tools": inp["tools"], "depends_on": [], **extra}
        chainer._dispatch_node("s", "plan", "USER#a", dict(node), [dict(node)], {}, turn_id="t")
    finally:
        chainer._agentcore, chainer._plan_table, chainer._job_table, chainer.SKILLS_S3_BUCKET = saved

    assert len(sent) == 3
    for p in sent:
        assert "project_token" not in p
        assert TOKEN not in json.dumps(p, default=str)


@SETTINGS
@given(goal=st.text(min_size=1, max_size=30), stray=st.booleans())
def test_plan_record_never_carries_project_token(goal, stray, bound_turn, monkeypatch):
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
        id, tools, depends_on, prompt, output_type, output_key, skills = "n1", ["code_interpreter"], [], goal, \
            "json", None, []

        def to_dict(self):
            d = {"id": self.id, "tools": self.tools, "depends_on": [], "prompt": self.prompt}
            return d

    class _Plan:
        nodes, plan_id = [_Node()], "plan-x"

    _Plan.goal = goal
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
    assert TOKEN not in json.dumps(items, default=str)
