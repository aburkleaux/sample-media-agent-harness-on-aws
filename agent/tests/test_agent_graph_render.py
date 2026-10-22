"""The turn's "↳ Graph" render — the knowledge-graph extension of the Moments gallery.

- `ProjectGraphTool` strips `subgraph` from the model's text and records it per turn — Main_Agent only.
- `render.graph_view.merge` / `build_graph_manifest`: focus nodes first, dedup, caps, no URL, deterministic id.
- `hooks.actions.graph_render` (locked built-in `render-graph`): buffers one `graph-view` tab per turn, the batch
  flush writes and announces it on the same rid; skipped without graph calls / turn context / identity.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hooks import actions
from hooks.loader import LOCKED_BUILTINS
from orchestration import progress_channel
from project_search import graph_ledger
from project_search import graph_tools as gt
from project_search.context import ActiveProject, TurnContext, current_turn_context
from project_search.errors import SearchOutcome
from render import graph_view

import render.artifacts as render_artifacts

PID = "p" + "a" * 32


def _sg(nodes, edges=(), focus=()):
    return {"nodes": [{"id": n, "type": n.split(":")[0].title(), "name": n, "sample": n.startswith("sample:")}
                      for n in nodes], "edges": [list(e) for e in edges], "focus": list(focus)}


@pytest.fixture(autouse=True)
def _reset():
    def reset():
        graph_ledger._reset_for_test()
        actions._graph_done.clear()
        actions._graph_slot = None
        actions._moments_slot = None
        actions._batch_open = False
        actions._render_batch.clear()
        progress_channel.set_active_channel(None)
    reset()
    yield
    reset()


@contextlib.contextmanager
def _bound(turn_id="t1", session="session-1", actor="actor-1"):
    from jobs.context import reset_request_context, set_request_context

    tc = TurnContext(turn_id=turn_id, active_project=ActiveProject(id=PID, name="Finals", collection_id=PID,
                                                                   personal=False, graph=True),
                     web_search=False, project_access="ok")
    t = current_turn_context.set(tc)
    j = set_request_context(actor_id=actor, session_id=session)
    try:
        yield
    finally:
        reset_request_context(j)
        current_turn_context.reset(t)


# ---- the tool wrapper ----------------------------------------------------------------------------------

class _Ref:
    async def current(self):
        return "TOKEN"


class _Client:
    url = "https://gw"

    def __init__(self, body):
        self.body = body

    async def call_tool(self, token, name, args):
        return SearchOutcome("call", "ok", result=json.dumps(self.body))


def _call(tool):
    async def go():
        return [e async for e in tool.stream({"toolUseId": "u", "name": tool.tool_name, "input": {}}, {})]
    ev = asyncio.run(go())[0]
    return (ev.tool_result if hasattr(ev, "tool_result") else ev["tool_result"])["content"][0]["text"]


def _tool(caller, body):
    spec = {"name": "graph_find", "gateway_name": "project-graph___graph_find", "description": "d",
            "inputSchema": {"json": {"type": "object"}}}
    return gt.ProjectGraphTool(_Ref(), spec, project_id=PID, client=_Client(body), caller=caller, turn_id="t1")


def test_main_agent_records_the_subgraph_and_the_model_never_sees_it():
    body = {"ok": True, "nodes": [{"id": "user:u1"}], "subgraph": _sg(["user:u1"], focus=["user:u1"])}
    text = _call(_tool("main", body))
    assert "subgraph" not in json.loads(text)
    tg = graph_ledger.take("t1", PID)
    assert tg and tg.calls[0].tool == "graph_find" and tg.calls[0].subgraph["focus"] == ["user:u1"]


@pytest.mark.parametrize("caller", ["task", "plan_node"])
def test_sub_agents_never_record_a_render(caller):
    _call(_tool(caller, {"ok": True, "subgraph": _sg(["user:u1"])}))
    assert graph_ledger.take("t1", PID) is None


# ---- merge + manifest ------------------------------------------------------------------------------------

def test_merge_puts_focus_first_dedups_and_drops_dangling_edges():
    a = _sg(["user:a", "project:p"], [("user:a", "project:p", "OWNS")], focus=["user:a"])
    b = _sg(["project:p", "asset:x"], [("project:p", "asset:x", "HAS_ASSET"), ("project:p", "asset:gone", "HAS_ASSET"),
                                       ("user:a", "project:p", "OWNS")], focus=["asset:x"])
    m = graph_view.merge([a, b])
    assert [n["id"] for n in m["nodes"]] == ["user:a", "asset:x", "project:p"]
    assert [n["focus"] for n in m["nodes"]] == [True, True, False]
    assert m["edges"] == [["user:a", "project:p", "OWNS"], ["project:p", "asset:x", "HAS_ASSET"]]


def test_merge_caps_and_flags_truncation(monkeypatch):
    monkeypatch.setattr(graph_view, "MAX_NODES", 3)
    m = graph_view.merge([_sg([f"sample:Person:{i}" for i in range(10)], focus=["sample:Person:9"])])
    assert len(m["nodes"]) == 3 and m["nodes"][0]["id"] == "sample:Person:9" and m["truncated"] is True


def test_manifest_has_no_url_and_a_deterministic_id():
    calls = [graph_ledger.GraphCall("graph_find", _sg(["user:a"], focus=["user:a"]))]
    m1 = graph_view.build_graph_manifest(calls, session_id="s", turn_id="t", project_id=PID, project_name="F", created_at="x")
    m2 = graph_view.build_graph_manifest(calls, session_id="s", turn_id="t", project_id=PID, project_name="F", created_at="y")
    assert m1["template"] == "graph-view" and m1["id"] == m2["id"] and m1["project_id"] == PID
    assert "http" not in json.dumps(m1) and "s3://" not in json.dumps(m1)
    assert graph_view.build_graph_manifest([], session_id="s", turn_id="t", project_id=PID, project_name="",
                                           created_at="x") is None


# ---- the render-graph rule -------------------------------------------------------------------------------

def test_render_graph_is_a_locked_builtin():
    assert "render-graph" in LOCKED_BUILTINS


def test_buffered_then_flushed_on_one_rid(monkeypatch):
    graph_ledger.record("t1", PID, "graph_find", _sg(["user:a"], focus=["user:a"]))
    channel = progress_channel.ProgressChannel()
    progress_channel.set_active_channel(channel)
    actions.open_render_batch()
    with _bound():
        res = actions.graph_render({}, {})
    assert res.status == "ok"
    rid = res.extra["render_request_id"]
    written = {}

    def fake_write(session_id, manifest_id, manifest):
        written.update(session_id=session_id, manifest=manifest)
        return f"artifacts/{session_id}/render/{manifest_id}.json"

    monkeypatch.setattr(render_artifacts, "write_session_manifest", fake_write)
    actions.flush_render_batch()
    assert written["session_id"] == "session-1" and written["manifest"]["template"] == "graph-view"
    done = [e for e in channel.drain() if e.label == "↳ Graph" and e.status == "done"]
    assert len(done) == 1 and done[0].extra["render_request_id"] == rid
    assert done[0].extra["render_manifest_key"].endswith(f"{rid}.json")


def test_once_per_turn_and_skips(monkeypatch):
    monkeypatch.setattr(render_artifacts, "write_session_manifest", lambda *a: "k")
    with _bound():
        assert actions.graph_render({}, {}).status == "skipped"           # no graph calls this turn
    graph_ledger.record("t2", PID, "graph_find", _sg(["user:a"]))
    with _bound(turn_id="t2"):
        assert actions.graph_render({}, {}).status == "ok"
        assert actions.graph_render({}, {}).status == "skipped"           # already handled
    graph_ledger.record("t3", PID, "graph_find", _sg(["user:a"]))
    assert actions.graph_render({}, {}).status == "skipped"               # no turn context
