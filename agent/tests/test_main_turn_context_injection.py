"""End to end through a real Strands Agent: what the main agent sends for two consecutive turns.

A real ``Agent`` + the harness Bedrock model + Strands' ``ContextInjector`` (as ``main`` wires
them), with only ``converse_stream`` faked, so the captured requests are exactly what Bedrock
would receive. Pins:

* per-turn context (remembered context, the Lab inventory hint) arrives as the LAST block of the
  latest user message, after the cache point, on every call -- and never enters history;
* the system prompt is ``[stable, cachePoint, session tail]`` and stays identical across turns;
* turn 2's request repeats turn 1's prefix byte for byte and marks it with a cache point.
"""
from __future__ import annotations

import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strands import Agent, AgentSkills, Skill  # noqa: E402
from strands.vended_plugins.context_injector import ContextInjector  # noqa: E402

import main  # noqa: E402
from model.factory import build_model  # noqa: E402

TURN_CONTEXT = '<remembered_context source="past sessions" authority="suggestion">\nprefers mp4\n</remembered_context>'


def _fake_stream(captured):
    def converse_stream(**request):
        captured.append(copy.deepcopy(request))
        return {"stream": [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "ok"}, "contentBlockIndex": 0}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                          "metrics": {"latencyMs": 1}}},
        ]}
    return converse_stream


def _agent(history, captured):
    model = build_model("global.anthropic.claude-opus-5")
    model.client.converse_stream = _fake_stream(captured)
    system = main._request_system_prompt("STABLE INSTRUCTIONS + CATALOG", "SESSION GUIDANCE", "",
                                         cacheable=main._model_caches_prompts(model))
    injector = ContextInjector(lambda ctx, **kw: main._compose_turn_context(TURN_CONTEXT),
                               name="harness:turn-context", trigger="everyTurn")
    return Agent(model=model, system_prompt=system, messages=copy.deepcopy(history),
                 plugins=[AgentSkills(skills=[Skill(name="demo", description="d", instructions="i")]), injector],
                 callback_handler=None)


def _points(messages):
    return [(i, j) for i, m in enumerate(messages) for j, b in enumerate(m["content"]) if "cachePoint" in b]


def _no_points(msgs):
    return [{**m, "content": [b for b in m["content"] if "cachePoint" not in b]} for m in msgs]


def test_two_turns_through_a_real_agent():
    captured: list = []
    a1 = _agent([], captured)
    a1("Trim the clip")
    history_after_t1 = copy.deepcopy(a1.messages)
    # The per-call context never reached durable history.
    assert TURN_CONTEXT not in json.dumps(history_after_t1)

    a2 = _agent(history_after_t1, captured)          # a fresh Agent per request, as in main
    a2("Now black and white")
    t1, t2 = captured

    # System prompt: stable part, one cache point, then the session tail -- the same both turns.
    assert t1["system"][0] == {"text": "STABLE INSTRUCTIONS + CATALOG"}
    assert "cachePoint" in t1["system"][1]
    assert t1["system"][2] == {"text": "SESSION GUIDANCE"}
    assert "available_skills" in t1["system"][-1]["text"]   # the skills plugin appends after the tail
    assert t1["system"] == t2["system"]

    # Per-call context is the LAST block of the latest user message, after the cache point.
    for req in (t1, t2):
        last = req["messages"][-1]["content"]
        assert last[-1]["text"].strip() == TURN_CONTEXT
        assert "cachePoint" in last[-2]

    # Turn 2 repeats turn 1's prefix byte for byte and marks it where turn 1 wrote it.
    i, j = (0, 1)
    assert (i, j) in _points(t1["messages"]) and (i, j) in _points(t2["messages"])
    p1 = _no_points([{**t1["messages"][0], "content": t1["messages"][0]["content"][:j]}])
    p2 = _no_points([{**t2["messages"][0], "content": t2["messages"][0]["content"][:j]}])
    assert json.dumps(p1, sort_keys=True) == json.dumps(p2, sort_keys=True)
    assert t1.get("toolConfig") == t2.get("toolConfig")

    # Four cache points at most in any request (Bedrock's limit).
    for req in (t1, t2):
        n = sum("cachePoint" in b for b in req.get("system", []))
        n += sum("cachePoint" in b for m in req["messages"] for b in m["content"])
        n += sum("cachePoint" in t for t in req.get("toolConfig", {}).get("tools", []))
        assert n <= 4


def test_turn_prompt_parts_keep_the_inventory_out_of_the_system_prompt(monkeypatch):
    monkeypatch.setattr(main, "_format_catalog_text", lambda *a, **k: "<CATALOG>")
    monkeypatch.setattr(main, "_build_catalog", lambda *a, **k: {})
    monkeypatch.setattr(main.main_lab_tools, "render_inventory_section", lambda snap: "- skill: Foo (draft)")
    stable, session, turn = main._build_turn_prompt(
        inventory_snapshot=object(), prompt="edit my skill", session_id="sess-1234")
    assert "<CATALOG>" in stable and "Foo (draft)" not in stable and "sess-1234" not in stable
    assert "sess-1234" in session
    assert turn == f"{main.LAB_INVENTORY_HEADING}\n- skill: Foo (draft)"
    # A prompt that does not mention assets gets no inventory -- and the SAME system prompt.
    stable2, session2, turn2 = main._build_turn_prompt(
        inventory_snapshot=object(), prompt="trim the clip", session_id="sess-1234")
    assert (stable2, session2, turn2) == (stable, session, "")
    # Same text as the single-string prompt, minus the inventory and with the guidance moved last.
    whole = main._build_system_prompt(prompt="trim the clip", session_id="sess-1234")
    assert sorted(whole) == sorted(stable + session)


def test_request_system_prompt_shapes():
    assert main._request_system_prompt("S", "G", "P", cacheable=False) == "SGP"
    assert main._request_system_prompt("S", "", "", cacheable=True) == [
        {"text": "S"}, {"cachePoint": {"type": "default"}}]
    assert main._request_system_prompt("S", "G", "P", cacheable=True)[2] == {"text": "GP"}
