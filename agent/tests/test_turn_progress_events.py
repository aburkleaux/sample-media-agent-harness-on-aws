"""A chat turn shows progress at once, and a finished answer is released when the model stops.

* The first frame of a chat turn is a ``running`` step, emitted before any gateway, memory or
  model work; it is closed IN PLACE (same ``node_id`` -- the UI and the proxy transcript dedupe
  steps by it) with a ``done`` frame when real progress arrives.
* ``ProseStream.finish_answer`` releases the held answer text on the model's ``messageStop`` (no
  tool requested) instead of after the turn's post-processing, with the same text ``end`` gives.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stream_text import LEAD_IN_MAX, NOTE_MAX, ProseStream  # noqa: E402

MAIN = (Path(__file__).resolve().parents[1] / "src" / "main.py").read_text()


def test_turn_start_step_opens_and_closes_in_place():
    import main

    step = main._TurnStartStep(enabled=True)
    opened = json.loads(step.open())
    assert opened["type"] == "step" and opened["status"] == "running"
    assert opened["label"] == main.TURN_START_LABEL and opened["node_id"].startswith("turn-start-")
    closed = json.loads(step.close())
    assert closed["status"] == "done" and closed["node_id"] == opened["node_id"]
    assert isinstance(closed["duration_ms"], int)
    # Exactly once.
    assert step.close() == "" and step.open() == ""


def test_management_actions_get_no_start_step():
    import main

    step = main._TurnStartStep(enabled=False)
    assert step.open() == "" and step.close() == ""


def test_the_start_step_is_the_first_thing_invoke_yields():
    src = MAIN[MAIN.index("async def invoke(payload, context):"):]
    assert src.index("yield _opening") < src.index("_init_agent_subsystems(")
    assert src.index("yield _opening") < src.index("_ensure_gateway_connected(")


def test_message_stop_reason():
    import main

    assert main._message_stop_reason({"event": {"messageStop": {"stopReason": "end_turn"}}}) == "end_turn"
    assert main._message_stop_reason({"event": {"messageStop": {"stopReason": "tool_use"}}}) == "tool_use"
    assert main._message_stop_reason({"event": {"contentBlockDelta": {}}}) == ""
    assert main._message_stop_reason({"data": "x"}) == ""
    assert main._message_stop_reason("nope") == ""


def test_finish_answer_releases_a_short_answer_with_the_same_text_as_end():
    a, b = ProseStream(), ProseStream()
    for p in (a, b):
        assert p.text("Here is ") == []
        assert p.text("your clip.") == []          # short: held (could be a pre-tool note)
    assert a.finish_answer() == b.end() == [("text", "Here is your clip.")]
    assert a.end() == []                            # nothing is emitted twice


def test_finish_answer_applies_the_lead_in_rule_like_end():
    text = "All set. Here's step 1.\n\n### Step 1\n\nBody"
    a, b = ProseStream(), ProseStream()
    a.text(text)
    b.text(text)
    assert a.finish_answer() == b.end()
    assert a.finish_answer() == []


def test_text_after_finish_answer_streams_without_holding():
    p = ProseStream()
    p.text("Done.")
    assert p.finish_answer() == [("text", "Done.")]
    assert p.text(" More") == [("text", " More")]


def test_tool_after_finish_answer_still_resets_cleanly():
    p = ProseStream()
    p.text("Done.")
    p.finish_answer()
    assert p.tool_started() == []
    assert p.text("Short note") == []               # held again: a new piece
    assert p.tool_started() == [("note", "Short note")]


def test_long_answers_are_unchanged():
    p = ProseStream()
    long = "x" * (LEAD_IN_MAX + 1)
    assert p.text(long) == [("text", long)]
    assert p.finish_answer() == []
    assert NOTE_MAX < LEAD_IN_MAX


def test_invoke_releases_the_answer_on_message_stop():
    assert "_prose.finish_answer()" in MAIN
    loop = MAIN[MAIN.index("_stop_reason = _message_stop_reason(event)"):]
    assert loop.index('_stop_reason != "tool_use"') < loop.index("_prose.finish_answer()")
