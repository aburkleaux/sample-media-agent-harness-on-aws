"""The chainer forwards a ``kind: "tool"`` node's planned call — only when nothing can make it stale.

The Task Agent executes ``payload["tool_call"]`` directly (no model loop). A dependency or an
external input is injected into the PROMPT, which literal arguments cannot see, and a retry exists
so a model can read the previous error and correct the call — in all three cases the node must run
as an ordinary model turn, so no tool_call is sent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _k in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE", "SKILLS_S3_BUCKET",
           "TASK_AGENT_RUNTIME_ARN"):
    os.environ.setdefault(_k, "t")
_spec = importlib.util.spec_from_file_location("_ch_tool_node", HERE / "handler.py")
pc = importlib.util.module_from_spec(_spec)
sys.modules["_ch_tool_node"] = pc
_spec.loader.exec_module(pc)

SUBMIT = "fal___submit_job"
ARGS = {"endpoint_id": "fal-ai/x", "input": {"image_url": "https://c/1.png"}}


def _node(**over):
    n = {"id": "restyle__shot1", "kind": "tool", "depends_on": [], "tools": [SUBMIT],
         "prompt": "Restyle shot 1", "output_type": "json",
         "config": {"tool": SUBMIT, "arguments": ARGS}}
    n.update(over)
    return n


class _Table:
    def __init__(self):
        self.puts = []

    def put_item(self, **k):
        self.puts.append(k)

    def update_item(self, **k):
        pass


def _payload(monkeypatch, node, all_nodes=None, **kw):
    sent = []
    monkeypatch.setattr(pc, "_plan_table", _Table())
    monkeypatch.setattr(pc, "_job_table", _Table())
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "")
    monkeypatch.setattr(pc, "_invoke_task_agent",
                        lambda sid, body: sent.append(json.loads(body)) or {"response": b""})
    pc._dispatch_node("sess", "plan1", "USER#a", node, all_nodes or [node], {}, **kw)
    assert len(sent) == 1
    return sent[0]


def test_a_complete_independent_tool_node_carries_its_call(monkeypatch):
    p = _payload(monkeypatch, _node())
    assert p["tool_call"] == {"name": SUBMIT, "arguments": ARGS}
    assert p["prompt"] == "Restyle shot 1", "the prompt stays — it is the model-turn fallback"
    assert p["session_id"] == "sess" and p["__plan_session_id"] == "sess"


def test_an_empty_prompt_gets_a_fallback_describing_the_call(monkeypatch):
    p = _payload(monkeypatch, _node(prompt=""))
    assert SUBMIT in p["prompt"] and "fal-ai/x" in p["prompt"]


def test_a_dependent_tool_node_runs_as_a_model_turn(monkeypatch):
    up = {"id": "up", "tools": [], "depends_on": []}
    p = _payload(monkeypatch, _node(depends_on=["up"]), [up, _node(depends_on=["up"])])
    assert "tool_call" not in p


def test_external_inputs_disable_the_fast_path():
    node = _node(external_inputs=[{"plan": "phase1", "node": "n"}])
    assert pc._fast_path_tool_call(node, [SUBMIT]) is None


def test_a_retry_runs_as_a_model_turn_with_its_error(monkeypatch):
    p = _payload(monkeypatch, _node(), retry_error="422 image_url unreachable", claimed=True, attempt=1)
    assert "tool_call" not in p
    assert "422 image_url unreachable" in p["prompt"]


def test_incomplete_or_unbound_calls_are_not_forwarded():
    assert pc._fast_path_tool_call(_node(kind="task"), [SUBMIT]) is None
    assert pc._fast_path_tool_call(_node(config={"tool": SUBMIT}), [SUBMIT]) is None
    assert pc._fast_path_tool_call(_node(config={"tool": SUBMIT, "arguments": "x"}), [SUBMIT]) is None
    assert pc._fast_path_tool_call(_node(), ["other___tool"]) is None
