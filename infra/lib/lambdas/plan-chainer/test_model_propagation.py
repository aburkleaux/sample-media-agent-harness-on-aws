"""The orchestrator's picked model_id is forwarded onto each node's dispatch, so plan-node
execution runs on the model the user selected (task_agent honors payload.model_id, falls back
to its default when absent).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE", "SKILLS_S3_BUCKET"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_model", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_model"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()


class _FakeS3:
    def get_object(self, **k):
        raise RuntimeError("no manifest")  # dep-injection block is best-effort; skip it


class _FakeAC:
    class exceptions:
        class ClientError(Exception):
            ...

    def __init__(self):
        self.calls = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return {"response": b""}


class _FakeTable:
    def __init__(self):
        self.writes = []

    def put_item(self, **k):
        self.writes.append(k)

    def update_item(self, **k):
        self.writes.append(k)


def _setup(monkeypatch):
    ac = _FakeAC()
    monkeypatch.setattr(pc, "_agentcore", ac)
    monkeypatch.setattr(pc, "_s3", _FakeS3())
    monkeypatch.setattr(pc, "_plan_table", _FakeTable())
    monkeypatch.setattr(pc, "TASK_AGENT_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")
    return ac


def test_dispatch_node_includes_model_id(monkeypatch):
    ac = _setup(monkeypatch)
    pc._dispatch_node("s", "p1", "a", {"id": "t1", "prompt": "do"}, [{"id": "t1", "prompt": "do"}], {},
                      model_id="global.anthropic.claude-sonnet-5")
    body = json.loads(ac.calls[0]["payload"].decode())
    assert body["model_id"] == "global.anthropic.claude-sonnet-5"


def test_dispatch_node_omits_empty_model_id(monkeypatch):
    ac = _setup(monkeypatch)
    pc._dispatch_node("s", "p1", "a", {"id": "t1", "prompt": "do"}, [{"id": "t1", "prompt": "do"}], {})
    body = json.loads(ac.calls[0]["payload"].decode())
    assert "model_id" not in body


def test_advance_plan_forwards_plan_record_model_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: captured.update(k))
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda *a, **k: captured.update(k))
    nodes = [{"id": "t1", "depends_on": []}]
    plan_record = {"node_id": "PLAN", "plan_id": "p1", "actor_id": "a", "turn_id": "",
                   "delegation_grant": "", "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                   "nodes_json": json.dumps(nodes)}
    items = [plan_record, {"node_id": "p1#t1", "plan_id": "p1", "status": "pending"}]
    pc._advance_plan("s", plan_record, items)
    assert captured.get("model_id") == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_dispatch_node_forwards_planned_endpoint_ids(monkeypatch):
    """The task agent holds a node's fal submit to these ids (session c3447913)."""
    ac = _setup(monkeypatch)
    node = {"id": "t1", "prompt": "do", "endpoint_ids": ["minimax/h3-max/image-to-video"]}
    pc._dispatch_node("s", "p1", "a", node, [node], {})
    body = json.loads(ac.calls[0]["payload"].decode())
    assert body["endpoint_ids"] == ["minimax/h3-max/image-to-video"]


def test_dispatch_node_omits_absent_or_malformed_endpoint_ids(monkeypatch):
    ac = _setup(monkeypatch)
    for node in ({"id": "t1", "prompt": "do"}, {"id": "t1", "prompt": "do", "endpoint_ids": []},
                 {"id": "t1", "prompt": "do", "endpoint_ids": "minimax/h3-max"}):
        pc._dispatch_node("s", "p1", "a", node, [node], {})
    assert all("endpoint_ids" not in json.loads(c["payload"].decode()) for c in ac.calls)
