"""LOCAL REPRODUCTION — author-node max_tokens continuation (session 36c99c2d).

The live research->PDF failure: the author node truncated at the output-token cap MID-EMISSION.
Strands raises "Model stopped generating due to maximum token limit" rather than returning a clean
stop_reason, and _generate_with_continuation has no guard around the stream — so the exception
propagates, the partial already accumulated is LOST, the persisted file holds only the error
(observed: 1313 bytes, no content), and there is nothing to continue from. format_pdf then blocks.

These tests drive the GENERATION path (a scripted fake agent), not a pre-baked file, so they
actually exercise what broke. Scenario A is RED on current code (it raises + loses the partial).
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("PLAN_STATE_TABLE", "t")
os.environ.setdefault("JOB_REGISTRY_TABLE", "t")
os.environ.setdefault("DDB_TABLE", "t")
os.environ.setdefault("S3_BUCKET", "t")
os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncio  # noqa: E402
import pytest  # noqa: E402
import task_agent  # noqa: E402


class _Result:
    def __init__(self, stop_reason):
        self.stop_reason = stop_reason


class _Truncate:
    """A scripted turn that emits `partial` then raises a truncation error with `message`."""
    def __init__(self, message, partial=""):
        self.message = message
        self.partial = partial


class _ScriptedAgent:
    """Fake node agent: each entry in `turns` is either a list of stream events or a _Truncate.
    Events are dicts: {"data": str} chunk, or {"result": _Result(stop_reason)}."""
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0

    async def stream_async(self, prompt):
        turn = self.turns[self.calls]
        self.calls += 1
        if isinstance(turn, _Truncate):
            if turn.partial:
                yield {"data": turn.partial}
            raise RuntimeError(turn.message)
        for ev in turn:
            yield ev


def _run(agent):
    return asyncio.run(
        task_agent._generate_with_continuation(agent, "write the report", "author_report"))


def test_A_maxtokens_exception_keeps_partial_and_continues():
    # The real 36c99c2d shape: partial emitted, then Strands RAISES the max-tokens error.
    agent = _ScriptedAgent([
        _Truncate("Model stopped generating due to maximum token limit", "# Report\n## Section 1\nintro "),
        [{"data": "## Section 2\nbody"}, {"result": _Result("end_turn")}],
    ])
    output, stop_reason, conts = _run(agent)
    # The pre-raise partial must NOT be lost, and the node must complete.
    assert "## Section 1" in output, "partial before the max_tokens exception was lost (the bug)"
    assert "## Section 2" in output, "did not continue after the max_tokens exception"
    assert stop_reason == "end_turn"
    assert conts >= 1


def test_B_clean_maxtokens_stops_are_stitched():
    agent = _ScriptedAgent([
        [{"data": "A"}, {"result": _Result("max_tokens")}],
        [{"data": "B"}, {"result": _Result("max_tokens")}],
        [{"data": "C"}, {"result": _Result("end_turn")}],
    ])
    output, stop_reason, conts = _run(agent)
    assert output == "ABC" and stop_reason == "end_turn" and conts == 2


def test_C_non_maxtokens_exception_propagates():
    agent = _ScriptedAgent([_Truncate("ValidationException: bad input", "partial")])
    with pytest.raises(Exception) as ei:
        _run(agent)
    assert "ValidationException" in str(ei.value)


def test_D_budget_exhausted_still_returns_the_partial():
    # Always truncating: after the budget, must return the accumulated partial (not empty) so the
    # persisted file has real content, not just the error.
    turns = [[{"data": f"chunk{i} "}, {"result": _Result("max_tokens")}]
             for i in range(task_agent.MAX_NODE_CONTINUATIONS + 1)]
    agent = _ScriptedAgent(turns)
    output, stop_reason, conts = _run(agent)
    assert stop_reason == "max_tokens"
    assert "chunk0" in output and output != "(no output)"
    assert conts == task_agent.MAX_NODE_CONTINUATIONS
