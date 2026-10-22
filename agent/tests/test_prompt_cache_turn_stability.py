"""The request prefix up to the previous turn must be byte-identical from one turn to the next.

Measured in staging: every later turn's first model call read only the ~45.5k tokens of tools +
system from cache and re-WROTE the whole history (29k -> 95k tokens, TTFT 6.5-9.7s vs 1.5-4s).
Two things made turn N's history differ from what turn N-1 sent:

* AgentCore Memory spliced the retrieved ``<user_context>`` into the in-memory user message
  AFTER that message had been persisted, so turn N restored a turn N-1 prompt without it;
* Strands caches only at the end of the LAST user message, and the provider looks back only ~20
  blocks for an earlier entry, so a tool-heavy previous turn put every earlier entry out of reach.

These tests build consecutive turns' REAL Bedrock requests (the harness model's own request
formatting, no network) the way the main agent now sends them -- remembered context folded in
per call after the cache point (``ContextInjector``), history restored exactly as persisted --
and assert the prefix is identical and that a cache point marks it.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from model.factory import build_model, is_turn_start, place_history_cache_point  # noqa: E402

MODEL_ID = "global.anthropic.claude-opus-5"
MEMORY = '<remembered_context source="past sessions" authority="suggestion">\nprefers mp4\n</remembered_context>'


def _model():
    return build_model(MODEL_ID)


def _request(model, messages, *, injected: str = ""):
    """Format one call the way Strands does, with optional per-call context folded in.

    The fold is the ContextInjector's: a trailing text block on the latest user message, with
    ``dynamic_trailing_blocks=1`` so Strands puts its cache point AHEAD of it.
    """
    msgs = copy.deepcopy(messages)
    trailing = 0
    if injected:
        msgs[-1] = {**msgs[-1], "content": [*msgs[-1]["content"], {"text": f"\n\n{injected}"}]}
        trailing = 1
    req = model.format_request(
        msgs,
        tool_specs=[{"name": "dispatch_tool", "description": "d",
                     "inputSchema": {"json": {"type": "object", "properties": {}}}}],
        system_prompt_content=[{"text": "SYSTEM"}],
        dynamic_trailing_blocks=trailing,
    )
    return req["messages"]


def _strip_points(messages):
    return [
        {**m, "content": [b for b in m["content"] if "cachePoint" not in b]} for m in messages
    ]


def _prefix(messages, pos):
    """The messages a cache point at ``pos`` = (message index, block index) covers."""
    i, j = pos
    return [*messages[:i], {**messages[i], "content": messages[i]["content"][:j]}]


def _json(x):
    import json

    return json.dumps(x, sort_keys=True, default=str)


def _point_positions(messages):
    return [(i, j) for i, m in enumerate(messages) for j, b in enumerate(m["content"]) if "cachePoint" in b]


def _reasoning(text):
    return {"reasoningContent": {"reasoningText": {"text": text, "signature": "sig-" + text}}}


U1 = {"role": "user", "content": [{"text": "Trim the clip to 0:10-0:20\n\n[Current date: 2026-10-16]"}]}
A1 = {"role": "assistant", "content": [_reasoning("plan"), {"text": "Let me trim it."},
                                       {"toolUse": {"toolUseId": "t1", "name": "dispatch_tool", "input": {"a": 1}}}]}
R1 = {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "status": "success",
                                                  "content": [{"text": "done: clip.mp4"}]}}]}
A2 = {"role": "assistant", "content": [_reasoning("answer"), {"text": "Here is your clip."}]}
U2 = {"role": "user", "content": [{"text": "Now make it black and white\n\n[Current date: 2026-10-16]"}]}
A3 = {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t2", "name": "dispatch_tool", "input": {"b": 2}}}]}
R2 = {"role": "user", "content": [{"toolResult": {"toolUseId": "t2", "status": "success",
                                                  "content": [{"text": "done: bw.mp4"}]}}]}
A4 = {"role": "assistant", "content": [{"text": "Done."}]}
U3 = {"role": "user", "content": [{"text": "Thanks"}]}


def test_two_consecutive_turns_share_the_prefix_up_to_the_previous_turn():
    m = _model()
    # Turn 1, first call: the prompt with this turn's remembered context folded in per call.
    t1_first = _request(m, [U1], injected=MEMORY)
    # Turn 2, first call: history restored exactly as persisted (no memory block), new prompt.
    t2_first = _request(m, [U1, A1, R1, A2, U2], injected=MEMORY)

    # Turn 1 wrote its first entry at the end of U1's own content (ahead of the injected block);
    # turn 2 reads it back with a point at exactly that position...
    assert (0, 1) in _point_positions(t1_first)
    assert (0, 1) in _point_positions(t2_first)
    # ...over a byte-identical prefix.
    assert _json(_strip_points(_prefix(t2_first, (0, 1)))) == _json(_strip_points(_prefix(t1_first, (0, 1))))
    # The memory block never enters the prefix: it is the LAST block of the last message only.
    assert t2_first[-1]["content"][-1]["text"].strip() == MEMORY
    assert all(MEMORY not in str(msg) for msg in t2_first[:-1])


def test_prior_turns_format_identically_on_every_later_turn():
    """Turn 1's messages (reasoning dropped) are the same bytes in turn 2's and turn 3's requests."""
    m = _model()
    t2_first = _request(m, [U1, A1, R1, A2, U2], injected=MEMORY)
    t3_first = _request(m, [U1, A1, R1, A2, U2, A3, R2, A4, U3], injected=MEMORY)
    assert _strip_points(t3_first)[:4] == _strip_points(t2_first)[:4]
    # Turn 3 reads turn 2's first entry exactly: a point at the end of U2's content...
    assert (4, 1) in _point_positions(t3_first)
    # ...which turn 2 wrote at the same place (the automatic last-message point)...
    assert (4, 1) in _point_positions(t2_first)
    # ...over the same bytes.
    assert _json(_strip_points(_prefix(t3_first, (4, 1)))) == _json(_strip_points(_prefix(t2_first, (4, 1))))


def test_mid_turn_calls_keep_the_turn_prompt_point():
    """Inside a tool cycle the point stays on THIS turn's prompt (the entry its first call wrote)."""
    m = _model()
    t2_cycle2 = _request(m, [U1, A1, R1, A2, U2, A3, R2], injected=MEMORY)
    positions = _point_positions(t2_cycle2)
    assert (4, 1) in positions               # end of U2
    assert positions[-1][0] == 6             # plus Strands' automatic point on the last message
    assert len(positions) == 2               # never more than one extra point (4-point budget)
    # The folded memory block sits after the tool result, last, and the tool result stays first.
    assert "toolResult" in t2_cycle2[-1]["content"][0]
    assert t2_cycle2[-1]["content"][-1]["text"].strip() == MEMORY
    # The in-progress cycle keeps nothing it should drop, and turn 1's reasoning is gone.
    assert not any("reasoningContent" in b for msg in t2_cycle2[:4] for b in msg["content"])


def test_current_turn_reasoning_is_kept_when_context_is_folded_into_a_tool_result():
    """A tool-result message carrying folded text is NOT a new turn: the cycle keeps its thinking."""
    m = _model()
    t1_cycle2 = _request(m, [U1, A1, R1], injected=MEMORY)
    assert any("reasoningContent" in b for b in t1_cycle2[1]["content"])


def test_is_turn_start():
    assert is_turn_start(U1)
    assert not is_turn_start(R1)
    assert not is_turn_start({"role": "user", "content": [*R1["content"], {"text": "ctx"}]})
    assert not is_turn_start(A1)


def test_no_point_without_caching():
    msgs = [copy.deepcopy(U1), copy.deepcopy(A2), copy.deepcopy(U2)]
    assert place_history_cache_point(msgs) == msgs
    assert not _point_positions(msgs)


def test_no_point_after_a_non_pdf_document():
    doc_prompt = {"role": "user", "content": [{"text": "see"}, {"document": {"format": "txt", "name": "a",
                                                                            "source": {"bytes": b"x"}}}]}
    msgs = [doc_prompt, copy.deepcopy(A2), {"role": "user", "content": [{"text": "q"}, {"cachePoint": {"type": "default"}}]}]
    place_history_cache_point(msgs)
    assert "cachePoint" not in msgs[0]["content"][-1]


def test_framed_memory_is_moved_out_of_the_message_when_ephemeral():
    """The session manager keeps the history message exactly as persisted and hands the block over."""
    import memory_context as mc
    from security import identity

    class _Client:
        def retrieve_memories(self, **kw):
            return [{"content": {"text": "prefers mp4"}, "score": 0.9}]

    mgr = mc.FramedMemorySessionManager.__new__(mc.FramedMemorySessionManager)
    mgr.config = identity.build_memory_config(memory_id="m", session_id="s", actor_id="a")
    mgr.memory_client = _Client()
    mgr.ephemeral = True
    msgs = [copy.deepcopy(U1)]
    mgr.retrieve_customer_context(SimpleNamespace(agent=SimpleNamespace(messages=msgs)))
    assert msgs == [U1]
    assert mgr.turn_context.startswith(mc.REMEMBERED_CONTEXT_OPEN)
    assert "prefers mp4" in mgr.turn_context
