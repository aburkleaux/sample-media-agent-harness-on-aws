"""Pure-logic tests for the two-tier judge's parsing + thresholding — no LLM.

Verifies the response-extraction and score-parsing logic that wraps the real
Bedrock call, using recorded response shapes (incl. the reasoning-only block that
crashed an earlier version).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve.contract import Outcome  # noqa: E402
from improve.judge import _extract_text, _parse_two_tier, _to_result  # noqa: E402


def test_extract_text_skips_reasoning_only_block():
    # Sonnet5 can emit a reasoningContent block first; if that's all there is
    # (truncated on reasoning), extraction returns None rather than crashing.
    resp = {"output": {"message": {"content": [{"reasoningContent": {"reasoningText": {"text": ""}}}]}}}
    assert _extract_text(resp) is None


def test_extract_text_finds_text_block_after_reasoning():
    resp = {"output": {"message": {"content": [
        {"reasoningContent": {"reasoningText": {"text": "thinking..."}}},
        {"text": '{"dispatch_correct": 1.0, "results_delivered": 0.0, "reason": "x"}'},
    ]}}}
    assert '"dispatch_correct"' in _extract_text(resp)


def test_parse_two_tier_extracts_both_scores():
    d, dl, reason = _parse_two_tier('{"dispatch_correct": 0.9, "results_delivered": 0.2, "reason": "ok"}')
    assert d == 0.9 and dl == 0.2 and reason == "ok"


def test_parse_two_tier_tolerates_surrounding_text():
    d, dl, _ = _parse_two_tier('Here: {"dispatch_correct": 1.0, "results_delivered": 1.0} done')
    assert d == 1.0 and dl == 1.0


def test_parse_two_tier_missing_score_is_none():
    d, dl, _ = _parse_two_tier('{"dispatch_correct": 0.8, "reason": "no delivery field"}')
    assert d == 0.8 and dl is None


def test_to_result_threshold_boundary():
    assert _to_result("t", 0.7, "r", 0.7).outcome is Outcome.PASS   # >= threshold
    assert _to_result("t", 0.69, "r", 0.7).outcome is Outcome.FAIL
    assert _to_result("t", None, "r", 0.7).outcome is Outcome.NO_DATA  # missing -> RED
