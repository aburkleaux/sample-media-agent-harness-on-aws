"""P0-hardening-A: a plan node must not report SUCCEEDED when its durable output write did not
land.

The durable file (``plans/<sid>/<plan>/<node>/output``) is the AUTHORITATIVE handoff to
downstream nodes — they are handed ``result_s3_key`` and told to read it for the COMPLETE
content. A node that "succeeds" with no persisted result hands the consumer a reference to a file
that does not exist: the exact silent failure that hid the boto3-shadowing bug (P0), where the
write raised UnboundLocalError, the ``except`` logged a warning, ``s3_key`` stayed empty, and the
node still reported completed.

``_execute_plan_node`` is a large async generator with many runtime dependencies, so — following
the same source-contract pattern its sibling tests use (see test_plan_node_loads_skills.py) —
this pins the two invariants that make the fix hold:
  1. ``s3_key`` is assigned ONLY after a confirmed ``put_object`` (never a bare path beforehand),
  2. a completed node with an empty ``s3_key`` is downgraded to failed.
"""
from __future__ import annotations

import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def _exec_node_src() -> str:
    body = (SRC / "task_agent.py").read_text()
    return body[body.index("async def _execute_plan_node"):body.index("async def _fetch_plan_node_results")]


def test_s3_key_is_set_only_after_a_confirmed_put():
    src = _exec_node_src()
    # The authoritative key is assigned only AFTER the write is confirmed to have landed.
    assert "s3_key = _durable_key" in src, "confirmed-write assignment missing"
    assert src.index("put_object(") < src.index("s3_key = _durable_key"), \
        "s3_key must be assigned only after put_object succeeds"
    # And it must NOT be pre-assigned to the path before the write (the old, silent-failure shape).
    assert 's3_key = f"plans/' not in src, \
        "s3_key must not be set to a path before the write lands (that is how an empty file looked written)"


def test_completed_node_with_no_durable_write_is_failed():
    src = _exec_node_src()
    assert "not s3_key" in src and 'status = "failed"' in src, \
        "a completed node whose durable write did not land must be downgraded to failed"



def test_node_execution_is_bounded_by_a_wallclock_timeout():
    """A hung tool — most sharply a code_interpreter cell that never returns (session 20f7315e's
    seg1_validate blocked ~15 min) — must not hang the node/plan forever. The node's generation
    is wrapped in a wall-clock timeout and a timed-out node terminalizes FAILED so the plan
    settles (the plan-chainer's skip-dependents logic then runs)."""
    src = _exec_node_src()
    assert "asyncio.wait_for(" in src and "PLAN_NODE_EXEC_TIMEOUT" in src, \
        "the node's generation must be bounded by a wall-clock timeout"
    assert "except asyncio.TimeoutError" in src, "the timeout must be handled, not propagated raw"
    # The timeout branch downgrades to a terminal failure (same shape as the max-tokens branch).
    ti = src.index("except asyncio.TimeoutError")
    assert 'status = "failed"' in src[ti:ti + 600], \
        "a timed-out node must set status=failed so it terminalizes"
