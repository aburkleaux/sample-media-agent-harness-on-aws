"""Characterization tests for main._sanitize_tool_pairs.

Locks the CURRENT restore-time repair of toolUse/toolResult pairing before the
long-term-memory change touches the restore path. `_sanitize_tool_pairs` is the
backstop that keeps a corrupted session (orphaned/duplicate toolResult, or a
toolUse with no result — what pressing Stop mid-tool-call can persist) from
hard-failing every subsequent Bedrock turn. Pure, offline, in-memory.
"""
from __future__ import annotations

from main import _sanitize_tool_pairs

INTERRUPTED = {"text": "Tool was interrupted."}


def _assistant_tooluse(*ids):
    return {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": i, "name": "x", "input": {}}} for i in ids
    ]}


def _user_toolresults(*ids, status="success"):
    return {"role": "user", "content": [
        {"toolResult": {"toolUseId": i, "status": status, "content": [{"text": "ok"}]}}
        for i in ids
    ]}


def test_clean_matched_pairs_unchanged():
    messages = [_assistant_tooluse("t1"), _user_toolresults("t1")]
    before = [dict(m) for m in messages]
    changed = _sanitize_tool_pairs(messages)
    assert changed == 0
    assert messages == before


def test_missing_result_with_no_following_turn_injects_user_turn():
    messages = [_assistant_tooluse("t1")]
    changed = _sanitize_tool_pairs(messages)
    assert changed == 1
    assert len(messages) == 2
    injected = messages[1]
    assert injected["role"] == "user"
    tr = injected["content"][0]["toolResult"]
    assert tr["toolUseId"] == "t1"
    assert tr["status"] == "error"
    assert tr["content"] == [INTERRUPTED]


def test_missing_result_in_existing_user_turn_is_filled_and_results_lead():
    messages = [
        _assistant_tooluse("t1"),
        {"role": "user", "content": [{"text": "hello"}]},
    ]
    changed = _sanitize_tool_pairs(messages)
    assert changed == 1
    content = messages[1]["content"]
    # Synthesized toolResult must LEAD the user turn, then non-result content.
    assert "toolResult" in content[0]
    assert content[0]["toolResult"]["toolUseId"] == "t1"
    assert content[0]["toolResult"]["status"] == "error"
    assert content[-1] == {"text": "hello"}


def test_duplicate_results_are_deduped_to_one():
    messages = [
        _assistant_tooluse("t1"),
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"text": "a"}]}},
            {"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"text": "b"}]}},
        ]},
    ]
    changed = _sanitize_tool_pairs(messages)
    assert changed == 1
    results = [b for b in messages[1]["content"] if "toolResult" in b]
    assert len(results) == 1


def test_orphan_result_after_text_assistant_is_dropped_second_pass():
    messages = [
        {"role": "assistant", "content": [{"text": "hi"}]},  # no toolUse
        _user_toolresults("orphan"),
    ]
    changed = _sanitize_tool_pairs(messages)
    assert changed == 1
    # keep becomes empty -> truthful placeholder (Bedrock rejects empty content).
    assert messages[1]["content"] == [{"text": "(interrupted tool results removed)"}]


def test_multiple_tooluses_partial_results_synthesizes_only_missing():
    messages = [
        _assistant_tooluse("t1", "t2"),
        _user_toolresults("t1"),  # t2 missing
    ]
    changed = _sanitize_tool_pairs(messages)
    assert changed == 1
    ids = [b["toolResult"]["toolUseId"] for b in messages[1]["content"] if "toolResult" in b]
    assert ids == ["t1", "t2"]  # ordered by the assistant's toolUse order
    t2 = next(b["toolResult"] for b in messages[1]["content"]
              if b.get("toolResult", {}).get("toolUseId") == "t2")
    assert t2["status"] == "error" and t2["content"] == [INTERRUPTED]


def test_non_dict_and_empty_inputs_are_safe():
    assert _sanitize_tool_pairs([]) == 0
    assert _sanitize_tool_pairs(["not-a-dict", {"role": "assistant"}]) == 0
    assert _sanitize_tool_pairs([{"role": "user", "content": [{"text": "hi"}]}]) == 0
