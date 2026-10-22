"""Unit tests for main._sanitize_tool_pairs.

Reproduces the corrupted-session failure mode observed on staging: an
interrupted ``dispatch_tool`` call left the restored message history with
mismatched toolUse/toolResult pairing, which made Bedrock reject EVERY
subsequent turn with:

    ValidationException ... The number of toolResult blocks at messages.N.content
    exceeds the number of toolUse blocks of previous turn.

The sanitizer must normalize the in-memory message list so the session
self-heals: every assistant toolUse is answered by exactly one matching
toolResult, orphan/duplicate toolResults are dropped, and an unanswered
(interrupted) toolUse gets a synthesized error result.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import _sanitize_tool_pairs


def _tool_use(uid, name="dispatch_tool"):
    return {"toolUse": {"toolUseId": uid, "name": name, "input": {}}}


def _tool_result(uid, text="ok"):
    return {"toolResult": {"toolUseId": uid, "status": "success",
                           "content": [{"text": text}]}}


def _use_ids(msg):
    return [b["toolUse"]["toolUseId"] for b in msg["content"] if "toolUse" in b]


def _result_ids(msg):
    return [b["toolResult"]["toolUseId"] for b in msg["content"] if "toolResult" in b]


def test_orphan_and_duplicate_toolresult_are_repaired():
    # Assistant emitted ONE toolUse; the next turn carries a result for a
    # DIFFERENT (orphaned) id plus the correct one — the exact staging shape.
    messages = [
        {"role": "user", "content": [{"text": "list my jobs"}]},
        {"role": "assistant", "content": [_tool_use("use_A")]},
        {"role": "user", "content": [
            _tool_result("use_ORPHAN"),   # interrupted call's stray result
            _tool_result("use_A"),
        ]},
    ]
    changed = _sanitize_tool_pairs(messages)
    assert changed >= 1
    # The results turn now matches the assistant's toolUse set exactly.
    assert _result_ids(messages[2]) == ["use_A"]


def test_unanswered_tooluse_gets_synthetic_error_result():
    messages = [
        {"role": "assistant", "content": [_tool_use("use_A"), _tool_use("use_B")]},
        {"role": "user", "content": [_tool_result("use_A")]},
    ]
    changed = _sanitize_tool_pairs(messages)
    assert changed == 1
    rids = _result_ids(messages[1])
    assert rids == ["use_A", "use_B"]
    synth = [b for b in messages[1]["content"]
             if b.get("toolResult", {}).get("toolUseId") == "use_B"][0]
    assert synth["toolResult"]["status"] == "error"


def test_dangling_tooluse_with_no_following_turn_gets_results_turn():
    messages = [
        {"role": "user", "content": [{"text": "go"}]},
        {"role": "assistant", "content": [_tool_use("use_A")]},
    ]
    changed = _sanitize_tool_pairs(messages)
    assert changed == 1
    assert len(messages) == 3
    assert messages[2]["role"] == "user"
    assert _result_ids(messages[2]) == ["use_A"]


def test_trailing_text_after_results_is_preserved_after_results():
    messages = [
        {"role": "assistant", "content": [_tool_use("use_A")]},
        {"role": "user", "content": [_tool_result("use_A"), {"text": "and then?"}]},
    ]
    _sanitize_tool_pairs(messages)
    content = messages[1]["content"]
    # toolResult must lead the user turn; trailing text preserved after it.
    assert "toolResult" in content[0]
    assert content[-1] == {"text": "and then?"}


def test_healthy_history_is_left_unchanged():
    messages = [
        {"role": "user", "content": [{"text": "hi"}]},
        {"role": "assistant", "content": [_tool_use("use_A")]},
        {"role": "user", "content": [_tool_result("use_A")]},
        {"role": "assistant", "content": [{"text": "done"}]},
    ]
    changed = _sanitize_tool_pairs(messages)
    assert changed == 0
    assert _use_ids(messages[1]) == ["use_A"]
    assert _result_ids(messages[2]) == ["use_A"]


def test_orphan_result_after_text_only_assistant_is_dropped():
    """A toolResult whose predecessor has NO toolUse must be removed.

    This is the shape that bricked a live session. Pressing Stop mid-tool-call
    persisted a toolResult after a text-only assistant turn. The original pass only
    inspected messages FOLLOWING an assistant message that contained toolUse, so
    this orphan was never visited and every subsequent turn -- including "try
    again" -- failed with "The number of toolResult blocks at messages.N.content
    exceeds the number of toolUse blocks of previous turn". The corruption lived in
    the restored history, so no retry could clear it.
    """
    messages = [
        {"role": "user", "content": [{"text": "hi"}]},
        {"role": "assistant", "content": [{"text": "thinking out loud"}]},
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": "ghost", "status": "success",
                            "content": [{"text": "orphan"}]}},
            {"text": "carry on"},
        ]},
    ]
    changed = _sanitize_tool_pairs(messages)
    assert changed == 1
    assert _result_ids(messages[2]) == []
    # Non-result content is preserved.
    assert messages[2]["content"] == [{"text": "carry on"}]


def test_message_of_only_orphan_results_gets_a_placeholder():
    # Bedrock rejects empty content, and dropping the message would break role
    # alternation, so a truthful placeholder replaces it.
    messages = [
        {"role": "assistant", "content": [{"text": "no tools here"}]},
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": "ghost", "status": "success",
                            "content": [{"text": "orphan"}]}},
        ]},
    ]
    changed = _sanitize_tool_pairs(messages)
    assert changed == 1
    assert messages[1]["content"] == [{"text": "(interrupted tool results removed)"}]


def test_duplicate_results_after_a_valid_pair_are_dropped():
    messages = [
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "u1", "name": "t", "input": {}}}]},
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": "u1", "status": "success", "content": [{"text": "ok"}]}},
            {"toolResult": {"toolUseId": "u1", "status": "success", "content": [{"text": "dupe"}]}},
        ]},
    ]
    _sanitize_tool_pairs(messages)
    assert _result_ids(messages[1]) == ["u1"]


def test_a_valid_conversation_is_untouched_by_the_second_pass():
    messages = [
        {"role": "user", "content": [{"text": "go"}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "u1", "name": "t", "input": {}}}]},
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": "u1", "status": "success", "content": [{"text": "ok"}]}},
        ]},
        {"role": "assistant", "content": [{"text": "done"}]},
    ]
    assert _sanitize_tool_pairs(messages) == 0
    assert _result_ids(messages[2]) == ["u1"]
