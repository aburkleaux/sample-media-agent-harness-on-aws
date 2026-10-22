"""Knowledge-graph tools on plan nodes: the plan's graph-enabled Project id (`project_scope_id` on the PLAN row)
is forwarded onto every node dispatch as `project_scope: {project_id}` — an id only; the node's Task Agent mints
its own Project_Token from the delegation grant, so no token is stored or carried (test_no_project_token.py).
"""
from __future__ import annotations

import json

from test_model_propagation import _setup, pc

PID = "p" + "e" * 32
NODE = {"id": "t1", "prompt": "do"}


def test_dispatch_node_carries_the_project_scope(monkeypatch):
    ac = _setup(monkeypatch)
    pc._dispatch_node("s", "p1", "a", NODE, [NODE], {}, delegation_grant="g.r.ant", project_scope_id=PID)
    body = json.loads(ac.calls[0]["payload"].decode())
    assert body["project_scope"] == {"project_id": PID}
    assert "project_token" not in body


def test_dispatch_node_omits_it_without_a_graph_project(monkeypatch):
    ac = _setup(monkeypatch)
    pc._dispatch_node("s", "p1", "a", NODE, [NODE], {})
    assert "project_scope" not in json.loads(ac.calls[0]["payload"].decode())


def test_project_scope_is_not_a_secret_key():
    assert "project_scope" not in pc.SECRET_PAYLOAD_KEYS
