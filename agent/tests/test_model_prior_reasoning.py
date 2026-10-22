"""Earlier turns' reasoning is never resent (model/factory.py drop_prior_turn_reasoning).

Staging session 3d82d059 ran three turns on GPT-6.1-Sol, then switched to Claude: every Claude turn failed with
``Invalid data in redacted_thinking block`` because GPT's provider-encrypted reasoning was replayed to Claude.
"""
from __future__ import annotations

import os
import sys

from strands.models import BedrockModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from model.factory import (  # noqa: E402
    HarnessBedrockModel, StablePrefixBedrockModel, ToolResultImageHoistBedrockModel, build_model,
    drop_prior_turn_reasoning,
)

GPT_REASONING = {"reasoningContent": {"redactedContent": b"gAAAAA-openai-encrypted"}}
CLAUDE_THINKING = {"reasoningContent": {"reasoningText": {"text": "plan", "signature": "sig"}}}


def user(text):
    return {"role": "user", "content": [{"text": text}]}


def assistant(*blocks):
    return {"role": "assistant", "content": list(blocks)}


def tool_result(tid):
    return {"role": "user", "content": [{"toolResult": {"toolUseId": tid, "content": [{"text": "ok"}]}}]}


def tool_use(tid):
    return {"toolUse": {"toolUseId": tid, "name": "t", "input": {}}}


def test_reasoning_before_the_current_turn_is_dropped():
    msgs = [user("one"), assistant(GPT_REASONING, {"text": "a1"}), user("two"), assistant(GPT_REASONING, {"text": "a2"}),
            user("three")]
    out = drop_prior_turn_reasoning(msgs)
    assert out[1]["content"] == [{"text": "a1"}] and out[3]["content"] == [{"text": "a2"}]
    assert out[4] is msgs[4]


def test_the_current_tool_cycle_keeps_its_thinking():
    msgs = [user("old"), assistant(CLAUDE_THINKING, {"text": "x"}),
            user("now"), assistant(CLAUDE_THINKING, tool_use("t1")), tool_result("t1")]
    out = drop_prior_turn_reasoning(msgs)
    assert out[1]["content"] == [{"text": "x"}]          # previous turn: dropped
    assert out[3]["content"][0] == CLAUDE_THINKING        # current turn's tool cycle: kept
    assert out[4] is msgs[4]


def test_reasoning_only_message_keeps_valid_alternation():
    out = drop_prior_turn_reasoning([user("a"), assistant(GPT_REASONING), user("b")])
    assert out[1]["content"] == [{"text": "(done)"}]


def test_no_reasoning_is_identity_and_input_unmutated():
    msgs = [user("a"), assistant({"text": "b"}), user("c")]
    assert drop_prior_turn_reasoning(msgs) == msgs
    withr = [user("a"), assistant(GPT_REASONING, {"text": "b"}), user("c")]
    snap = repr(withr)
    drop_prior_turn_reasoning(withr)
    assert repr(withr) == snap


def test_every_model_class_strips_it():
    assert issubclass(StablePrefixBedrockModel, HarnessBedrockModel)
    assert issubclass(ToolResultImageHoistBedrockModel, HarnessBedrockModel)
    assert hasattr(BedrockModel, HarnessBedrockModel.SEAM_METHOD)
    for mid in ("global.anthropic.claude-opus-5", "global.openai.gpt-6.1-sol", "us.amazon.nova-pro-v1:0"):
        m = build_model(mid)
        assert isinstance(m, HarnessBedrockModel), mid
        req = m._format_bedrock_messages([user("q1"), assistant(GPT_REASONING, {"text": "a"}), user("q2")])
        assert not any("reasoningContent" in b for b in req[1]["content"]), mid
