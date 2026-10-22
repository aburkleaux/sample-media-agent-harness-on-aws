"""Example/unit tests for per-invocation ceilings + usage attribution (task 14.2).

Covers the Task-Agent-side units of Requirement 14:
  * 14.3/14.4 — the per-invocation ceiling *selection* (``_select_ceilings``):
    external (principal-bearing) invocations pick the bounded EXT_* ceilings;
    internal invocations pick the general ceilings, which are now a GENEROUS
    RUNAWAY BACKSTOP (finite, set well above any legitimate in-node poll loop)
    after run 863816b4 looped ``submit_job`` 236× in one unbounded internal turn.
    The ceilings are what the invoke loop enforces mid-stream before it yields
    the terminal ``invocation_ceiling_exceeded`` SSE error frame.
  * 14.5 — usage attribution (``_usage_attribution``): a principal-bearing
    invocation attributes usage to ``(agentId, End_User)`` with the Owner as the
    quota owner, while an invocation with no principal attributes nothing (``{}``).

Full end-to-end mid-stream ceiling *enforcement* is driven by the streaming
invoke loop (real Strands agent + model), which is impractical to exercise as a
unit test; here we assert the pure selection + attribution units plus the bound
that keeps the external ceilings no looser than the finite internal backstop.

Validates: Requirements 14.3, 14.4, 14.5
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import task_agent  # noqa: E402
from task_agent import _select_ceilings, _usage_attribution  # noqa: E402


# ---------------------------------------------------------------------------
# 14.3 / 14.4 — ceiling selection: EXT (bounded) vs general (unbounded default)
# ---------------------------------------------------------------------------


def test_select_ceilings_external_uses_ext_bounds():
    """External invocations resolve the EXT_* per-invocation ceilings."""
    ceilings = _select_ceilings(is_external=True)

    assert set(ceilings) == {"cycles", "tokens", "wall_clock_sec"}
    # With default env these are the EXT_* module constants, each bounded (>0)
    # so the invoke loop can terminate a runaway external call.
    assert ceilings["cycles"] == task_agent.EXT_MAX_CYCLES
    assert ceilings["tokens"] == task_agent.EXT_MAX_TOKENS
    assert ceilings["wall_clock_sec"] == task_agent.EXT_MAX_WALL_CLOCK_SEC


def test_select_ceilings_internal_uses_general():
    """Internal invocations resolve the general ceilings (unchanged)."""
    ceilings = _select_ceilings(is_external=False)

    assert ceilings["cycles"] == task_agent.MAX_CYCLES
    assert ceilings["tokens"] == task_agent.MAX_TOKENS
    assert ceilings["wall_clock_sec"] == task_agent.MAX_WALL_CLOCK_SEC


def test_external_ceilings_are_bounded_and_at_most_internal():
    """EXT ceilings are finite/bounded and no looser than the internal backstop.

    A ceiling value of ``0`` means "disabled/unbounded". Every external ceiling
    is a positive finite bound. The internal ceilings are now a GENEROUS RUNAWAY
    BACKSTOP (not unbounded) after run 863816b4 looped ``submit_job`` 236× in one
    internal turn — so where the internal ceiling is finite the external one must
    be at least as tight (<=).
    """
    ext = _select_ceilings(is_external=True)
    internal = _select_ceilings(is_external=False)

    for key in ("cycles", "tokens", "wall_clock_sec"):
        assert ext[key] > 0, f"external {key} ceiling must be bounded (>0)"
        assert ext[key] < float("inf")
        # A finite internal backstop must not be tighter than the external
        # ceiling (external is the strict path); when internal is 0 (unbounded)
        # this is vacuously satisfied.
        if internal[key]:
            assert ext[key] <= internal[key]


def test_internal_ceilings_are_a_finite_runaway_backstop():
    """Internal invocations are bounded (not unbounded) so a single turn cannot
    loop a paid tool forever.

    Regression pin for run 863816b4: with the previous ``0`` (unbounded) internal
    ceilings, one internal dispatch turn fired ``submit_job`` 236× against a
    rejected endpoint id and then rolled up as a false SUCCEEDED. The cycle +
    wall-clock ceilings must now be finite (a generous backstop set well above any
    legitimate in-node poll loop), while the token ceiling stays opt-in (0).
    """
    internal = _select_ceilings(is_external=False)

    assert internal["cycles"] > 0, "internal cycle ceiling must be a finite backstop"
    assert internal["wall_clock_sec"] > 0, "internal wall-clock ceiling must be finite"
    # The runaway (236 cycles) must be caught by the cycle backstop.
    assert internal["cycles"] < 236
    # Generous headroom over a legitimate 'tens of cycles' in-node poll loop.
    assert internal["cycles"] >= 60


def test_select_ceilings_ext_falls_back_to_general_when_unset(monkeypatch):
    """When an EXT_* var is unset (0) the selection falls back to the general ceiling."""
    monkeypatch.setattr(task_agent, "EXT_MAX_CYCLES", 0, raising=False)
    monkeypatch.setattr(task_agent, "MAX_CYCLES", 25, raising=False)

    ceilings = _select_ceilings(is_external=True)

    assert ceilings["cycles"] == 25  # fell back to the general ceiling


# ---------------------------------------------------------------------------
# 14.5 — usage attribution keyed by agentId + End_User
# ---------------------------------------------------------------------------


def test_usage_attribution_keyed_by_agent_and_end_user():
    """A principal-bearing invocation attributes usage to (agentId, End_User)."""
    attribution = _usage_attribution(
        agent_id="agent-xyz",
        principal={"issuer": "https://issuer.example.com", "sub": "caller-1"},
        owner_actor_id="owner-user-123",
        usage={"totalTokens": 4321},
        cycles=7,
        duration_ms=1234,
        terminated=False,
    )

    assert attribution["agent_id"] == "agent-xyz"
    # End_User is the issuer-qualified Principal (issuer#sub).
    assert attribution["end_user"] == "https://issuer.example.com#caller-1"
    # Quota is attributed to the Owner (resource actor), never the caller.
    assert attribution["quota_owner"] == "owner-user-123"
    assert attribution["usage"] == {"totalTokens": 4321}
    assert attribution["cycles"] == 7
    assert attribution["duration_ms"] == 1234
    assert attribution["terminated"] is False
    # No ceiling breach → no ceiling key on a normal completion.
    assert "ceiling" not in attribution


def test_usage_attribution_records_ceiling_on_termination():
    """A ceiling-terminated invocation records the breached ceiling name."""
    attribution = _usage_attribution(
        agent_id="agent-xyz",
        principal={"issuer": "https://issuer.example.com", "sub": "caller-1"},
        owner_actor_id="owner-user-123",
        usage={"totalTokens": 400_000},
        cycles=50,
        duration_ms=240_000,
        terminated=True,
        ceiling="tokens",
    )

    assert attribution["terminated"] is True
    assert attribution["ceiling"] == "tokens"
    assert attribution["end_user"] == "https://issuer.example.com#caller-1"
    assert attribution["quota_owner"] == "owner-user-123"


def test_usage_attribution_empty_for_internal_no_principal():
    """Internal invocations (no principal) attribute nothing — returns {}."""
    assert (
        _usage_attribution(
            agent_id="agent-xyz",
            principal=None,
            owner_actor_id="owner-user-123",
            usage={"totalTokens": 10},
            cycles=1,
            duration_ms=5,
            terminated=False,
        )
        == {}
    )
