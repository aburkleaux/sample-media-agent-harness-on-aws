"""Plan-node fast path: a fully specified single call runs WITHOUT a model loop — same outcome.

Staging plan 81d9198e: every shot node made two Opus calls around ONE fal submit whose endpoint
and arguments the plan had already written down. A ``kind: "tool"`` node now reaches the Task
Agent as ``payload["tool_call"]`` and is executed directly through the tool's own stream.

The equivalence test drives ``_execute_plan_node`` twice against moto — once on the fast path, once
on the model path with the model "calling" the same tool and restating its result — and asserts the
two leave the SAME plan-state row, job registration and durable output. The fallback tests pin that
anything short of a clean direct call is today's model turn.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import plan_node_fast_path as fp  # noqa: E402
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool  # noqa: E402
from strands.types._events import ToolResultEvent  # noqa: E402

REGION = "us-west-2"
SID = "0a1b2c3d-1c4d-4a2e-9f31-6b0d5e2a77c1"
PID = "81d9198e-0000-4000-8000-000000000001"
BUCKET = "skills-bkt"
SUBMIT = "fal___submit_job"
ARGS = {"endpoint_id": "fal-ai/anime/restyle", "input": {"image_url": "https://c/1.png"}}
HANDLE = {"request_id": "req-123", "status_url": "https://queue.fal.run/fal-ai/anime/requests/req-123/status"}


class FakeMCPTool(MCPAgentTool):
    """An MCPAgentTool whose MCP round trip is a recorded fake."""

    def __init__(self, name=SUBMIT, result=None, schema=None, raises=None):  # noqa: D107 - no super()
        self._name, self.calls, self._raises = name, [], raises
        self._result = result or {"status": "success", "content": [{"text": json.dumps(HANDLE)}]}
        self._schema = schema or {"type": "object", "properties": {"endpoint_id": {}, "input": {}},
                                  "required": ["endpoint_id", "input"]}

    @property
    def tool_name(self):
        return self._name

    @property
    def tool_spec(self):
        return {"name": self._name, "description": "d", "inputSchema": {"json": self._schema}}

    async def stream(self, tool_use, invocation_state, **kwargs):
        self.calls.append(tool_use["input"])
        if self._raises:
            raise self._raises
        yield ToolResultEvent({"toolUseId": tool_use["toolUseId"], **self._result})


@pytest.fixture
def ta(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"),
                 ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(TableName="plan-state", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"},
                                    {"AttributeName": "node_id", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"},
                                               {"AttributeName": "node_id", "AttributeType": "S"}])
        ddb.create_table(TableName="job-registry", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}])
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        import task_agent

        async def _no_gateways(**kw):
            return None

        monkeypatch.setattr(task_agent, "_ensure_gateways_loaded", _no_gateways)
        monkeypatch.setattr(task_agent, "JOB_REGISTRY_TABLE", "job-registry")
        monkeypatch.setattr(task_agent, "build_model", lambda **kw: object())

        class _StubAgent:
            def __init__(self, **kw):
                self.messages = []

        monkeypatch.setattr(task_agent, "Agent", _StubAgent)
        monkeypatch.setattr(task_agent, "_queue_trio_for", lambda *a, **k: None)
        yield task_agent, ddb


def _payload(node, tool_call=None):
    p = {"prompt": "Restyle https://c/1.png as anime with fal-ai/anime/restyle.", "tools": [SUBMIT],
         "session_id": SID, "__plan_session_id": SID, "actor_id": "USER#a", "node_id": node,
         "plan_id": PID, "output_type": "json", "s3_bucket": BUCKET, "ddb_table": "plan-state",
         "__plan_node": True}
    if tool_call:
        p["tool_call"] = tool_call
    return p


def _run(task_agent, payload):
    async def go():
        return [e async for e in task_agent._execute_plan_node(payload)]
    return asyncio.run(go())


def _model_path(monkeypatch, task_agent, tool):
    """A model turn that calls ``tool`` once with ARGS and restates its result as the json block."""
    seen = {"model_turns": 0}

    async def fake_generate(node_agent, prompt, node_id=""):
        seen["model_turns"] += 1
        result = await fp.run_tool_call(tool, ARGS, node_id)
        node_agent.messages = [{"role": "user", "content": [{"toolResult": result}]}]
        return fp.fast_path_output(result), "end_turn", 0

    monkeypatch.setattr(task_agent, "_generate_with_continuation", fake_generate)
    return seen


def _state(ddb, node):
    row = ddb.Table("plan-state").get_item(Key={"session_id": SID, "node_id": f"{PID}#{node}"})["Item"]
    return {k: row.get(k) for k in ("status", "job_id", "plan_id", "node", "error")}


def _durable(node):
    obj = boto3.client("s3", region_name=REGION).get_object(
        Bucket=BUCKET, Key=f"plans/{SID}/{PID}/{node}/output")
    return obj["Body"].read().decode()


def test_fast_path_is_equivalent_to_the_model_path(ta, monkeypatch):
    task_agent, ddb = ta
    fast_tool, slow_tool = FakeMCPTool(), FakeMCPTool()

    seen_fast = _model_path(monkeypatch, task_agent, fast_tool)
    monkeypatch.setattr(task_agent, "_all_gateway_tools_by_name", lambda: {SUBMIT: fast_tool})
    _run(task_agent, _payload("shot1", {"name": SUBMIT, "arguments": ARGS}))

    seen_slow = _model_path(monkeypatch, task_agent, slow_tool)
    monkeypatch.setattr(task_agent, "_all_gateway_tools_by_name", lambda: {SUBMIT: slow_tool})
    _run(task_agent, _payload("shot2"))

    assert seen_fast["model_turns"] == 0, "the fast path must not run a model turn"
    assert seen_slow["model_turns"] == 1
    assert fast_tool.calls == [ARGS] and slow_tool.calls == [ARGS]
    fast, slow = _state(ddb, "shot1"), _state(ddb, "shot2")
    assert fast.pop("node") == "shot1" and slow.pop("node") == "shot2"
    assert fast == slow
    assert fast["job_id"] == "req-123", "the queue handle is registered on the node row as before"
    assert _durable("shot1") == _durable("shot2")


def test_an_error_result_falls_back_to_the_model_turn(ta, monkeypatch):
    task_agent, ddb = ta
    bad = FakeMCPTool(result={"status": "success", "content": [{"text": json.dumps(
        {"error": True, "errorCode": "INVALID_PARAMETER", "message": "image_url unreachable"})}]})
    good = FakeMCPTool()
    seen = _model_path(monkeypatch, task_agent, good)
    monkeypatch.setattr(task_agent, "_all_gateway_tools_by_name", lambda: {SUBMIT: bad})
    _run(task_agent, _payload("shot1", {"name": SUBMIT, "arguments": ARGS}))
    assert len(bad.calls) == 1 and seen["model_turns"] == 1
    assert _state(ddb, "shot1")["job_id"] == "req-123"


def test_a_raising_call_falls_back_to_the_model_turn(ta, monkeypatch):
    task_agent, _ = ta
    boom = FakeMCPTool(raises=RuntimeError("connection reset"))
    seen = _model_path(monkeypatch, task_agent, FakeMCPTool())
    monkeypatch.setattr(task_agent, "_all_gateway_tools_by_name", lambda: {SUBMIT: boom})
    _run(task_agent, _payload("shot1", {"name": SUBMIT, "arguments": ARGS}))
    assert seen["model_turns"] == 1


def test_a_hung_direct_call_fails_the_node_like_a_hung_model_turn(ta, monkeypatch):
    task_agent, ddb = ta

    class Hung(FakeMCPTool):
        async def stream(self, tool_use, invocation_state, **kwargs):
            await asyncio.sleep(5)
            yield None

    seen = _model_path(monkeypatch, task_agent, FakeMCPTool())
    monkeypatch.setattr(task_agent, "PLAN_NODE_EXEC_TIMEOUT", 0.2)
    monkeypatch.setattr(task_agent, "_all_gateway_tools_by_name", lambda: {SUBMIT: Hung()})
    _run(task_agent, _payload("shot1", {"name": SUBMIT, "arguments": ARGS}))
    row = _state(ddb, "shot1")
    assert row["status"] == "failed" and "node_timeout" in row["error"]
    assert seen["model_turns"] == 0, "a spent budget is not retried in the same turn"


@pytest.mark.parametrize("tool_call", [
    {"name": SUBMIT, "arguments": {"input": {}}},                # a required argument is missing
    {"name": "fal___other", "arguments": ARGS},                  # not one of the node's tools
    {"name": SUBMIT, "arguments": "not-an-object"},
])
def test_an_unusable_call_never_reaches_the_tool_and_uses_the_model_turn(ta, monkeypatch, tool_call):
    task_agent, _ = ta
    tool = FakeMCPTool()
    seen = _model_path(monkeypatch, task_agent, FakeMCPTool())
    monkeypatch.setattr(task_agent, "_all_gateway_tools_by_name", lambda: {SUBMIT: tool})
    _run(task_agent, _payload("shot1", tool_call))
    assert tool.calls == [] and seen["model_turns"] == 1


def test_resolution_matches_bare_names_and_refuses_non_gateway_tools():
    tool = FakeMCPTool(name="usr-abc___submit_job")
    got = fp.resolve_fast_path_call({"name": "submit_job", "arguments": ARGS}, ["submit_job"], [tool])
    assert got and got[0] is tool

    class Builtin:
        tool_name = "artifact"

    assert fp.resolve_fast_path_call({"name": "artifact", "arguments": {}}, ["artifact"], [Builtin()]) is None
    # Two gateways exposing the same bare tool: ambiguous, so the model turn decides.
    twins = [FakeMCPTool(name="a___submit_job"), FakeMCPTool(name="b___submit_job")]
    assert fp.resolve_fast_path_call({"name": "submit_job", "arguments": ARGS}, ["submit_job"], twins) is None


def test_schema_fit_is_shallow_and_respects_additional_properties():
    closed = FakeMCPTool(schema={"type": "object", "properties": {"a": {}}, "additionalProperties": False})
    assert fp.arguments_fit_schema(closed, {"a": 1}) == ""
    assert "unknown" in fp.arguments_fit_schema(closed, {"a": 1, "b": 2})
    open_ = FakeMCPTool(schema={"type": "object", "properties": {"a": {}}})
    assert fp.arguments_fit_schema(open_, {"a": 1, "b": 2}) == ""


def test_result_value_matches_what_the_model_restates():
    assert fp.result_value({"content": [{"text": json.dumps(HANDLE)}]}) == HANDLE
    assert fp.result_value({"content": [{"json": {"x": 1}}]}) == {"x": 1}
    assert fp.result_value({"content": [{"text": "plain words"}]}) == {"text": "plain words"}
    assert fp.fast_path_output({"content": [{"json": [1, 2]}]}) == "```json\n[1, 2]\n```"
