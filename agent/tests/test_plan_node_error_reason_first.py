"""A failed node's plan-state ``error`` leads with the framework's reason, not the agent's prose.

Staging dcd11946: nodes that refused in long prose and then declared
``{"error": "Missing required input ..."}`` were stored as ``output[:2000]`` — the prose first,
the ``[plan] node declared failure: ...`` line appended at the END and cut off. The plan-chainer's
fail-fast classifier (``_is_missing_input_error``) reads that field, so it never saw the reason.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
REASON = "[plan] node declared failure: Missing required input: approved keyframe for shot 6"


def test_declared_reason_leads_and_survives_a_long_refusal():
    from task_agent import _node_row_error

    prose = "I cannot animate this shot. " * 200  # ~5.6 KB, well past the 2000-char cut
    output = f"{prose}\n\n{REASON}"
    err = _node_row_error(output, [REASON])
    assert err.startswith(REASON)
    assert len(err) == 2000
    assert err.count(REASON) == 1  # stripped from the tail, not stored twice
    assert "I cannot animate this shot." in err


def test_all_reasons_lead_in_order_and_prose_follows():
    from task_agent import _node_row_error

    notes = [REASON, "[plan] the submitted job failed: 422 prompt_expansion_mode"]
    output = "short prose\n\n" + "\n\n".join(notes)
    assert _node_row_error(output, notes) == "\n".join(notes) + "\n\nshort prose"


def test_no_notes_keeps_the_old_bounded_prose():
    from task_agent import _node_row_error

    assert _node_row_error("x" * 3000, []) == "x" * 2000
    assert _node_row_error("", []) == "node failed"
    assert _node_row_error("", [REASON]) == REASON


def test_the_chainer_fail_fast_signature_survives_the_cut():
    """``_is_missing_input_error`` (plan-chainer) matches "missing required input" in this field."""
    from task_agent import _node_row_error

    output = ("I cannot proceed. " * 200) + f"\n\n{REASON}"
    assert "missing required input" not in output[:2000].lower()       # the old stored value
    assert "missing required input" in _node_row_error(output, [REASON]).lower()


def test_execute_plan_node_stores_the_reason_first_error():
    body = (SRC / "task_agent.py").read_text()
    body = body[body.index("async def _execute_plan_node"):body.index("async def _fetch_plan_node_results")]
    assert 'extra_attrs["error"] = _node_row_error(output, failure_notes)' in body
    assert "(output or \"node failed\")[:2000]" not in body
    for note in ("node declared failure", "node did not satisfy its declared output",
                 "durable output write failed", "the submitted job failed"):
        i = body.index(note)
        assert "failure_notes.append(" in body[i - 120:i], note
