"""D-02: the Design Agent's one pre-dispatch self-correction was a no-op.

When a produced DAG plan fails validation, the orchestrator re-invokes the Design Agent with the
validation errors (the sender in plan_and_execute_tool._call_design_agent formats them into a
"- type: message" STRING and ships it as payload["validation_errors"]). But invoke() never read
that key, so the retry prompt was BYTE-IDENTICAL to the first attempt — and on the pinned reasoning
model an identical prompt reproduces the same invalid plan and the plan aborts.

These tests capture the user_prompt actually handed to stream_async (no model / AWS call) and
assert: the retry carries the errors + correction framing and DIFFERS from attempt #1; attempt #1
is unchanged; and an empty / whitespace validation_errors is treated as absent.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import design_agent  # noqa: E402


class _CapturingAgent:
    """Fake Strands Agent: records the prompt passed to stream_async and emits no text."""

    last_prompt = None

    def __init__(self, *args, **kwargs):
        pass

    async def stream_async(self, prompt):
        type(self).last_prompt = prompt
        # An empty async generator — no model call, no chunks.
        return
        yield  # pragma: no cover — makes this a generator


def _capture_prompt(monkeypatch, payload):
    """Run invoke() to exhaustion with the fake agent and return the captured user_prompt."""
    _CapturingAgent.last_prompt = None
    monkeypatch.setattr(design_agent, "Agent", _CapturingAgent)
    # build_model must not touch Bedrock/AWS; the fake agent ignores the model anyway.
    monkeypatch.setattr(design_agent, "build_model", lambda **kwargs: object())

    async def _drain():
        # No session_id / bucket -> invoke takes the "stream text directly" branch (no boto3).
        async for _ in design_agent.invoke(payload, context=None):
            pass

    asyncio.run(_drain())
    return _CapturingAgent.last_prompt


_HLP = {"goal": "make a report", "execution_mode": "dag", "sub_goals": [{"id": "s1"}]}
_CATALOG = {"tools": [{"name": "search", "description": "web search"}]}
_ERRORS = "- missing_output_key: node t1 has no output_key\n- unknown_tool: node t2 references 'foo'"

_CORRECTION_MARKER = "failed validation with these errors"


def test_first_attempt_prompt_has_no_correction_framing(monkeypatch):
    prompt = _capture_prompt(monkeypatch, {"high_level_plan": _HLP, "catalog": _CATALOG})
    assert prompt is not None
    assert prompt.endswith("Produce the executable DAG Plan JSON.")
    assert _CORRECTION_MARKER not in prompt


def test_retry_prompt_contains_errors_and_correction_framing_and_differs(monkeypatch):
    first = _capture_prompt(monkeypatch, {"high_level_plan": _HLP, "catalog": _CATALOG})
    retry = _capture_prompt(
        monkeypatch,
        {"high_level_plan": _HLP, "catalog": _CATALOG, "validation_errors": _ERRORS},
    )
    # The retry frames the errors as feedback on the model's own prior output ...
    assert _CORRECTION_MARKER in retry
    assert "Produce a corrected plan that fixes them." in retry
    # ... carries the exact error text verbatim ...
    assert _ERRORS in retry
    # ... and is genuinely different from the first attempt (not a byte-identical reflection).
    assert retry != first
    # The retry is the first-attempt prompt with the correction block appended.
    assert retry.startswith(first)


def test_empty_and_whitespace_validation_errors_treated_as_absent(monkeypatch):
    baseline = _capture_prompt(monkeypatch, {"high_level_plan": _HLP, "catalog": _CATALOG})
    for empty in ("", "   \n\t "):
        prompt = _capture_prompt(
            monkeypatch,
            {"high_level_plan": _HLP, "catalog": _CATALOG, "validation_errors": empty},
        )
        assert prompt == baseline
        assert _CORRECTION_MARKER not in prompt
