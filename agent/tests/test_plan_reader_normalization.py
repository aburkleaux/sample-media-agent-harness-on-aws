"""Property test for Plan Reader step normalization (task 2.2).

Validates: Requirements 1.2

Feature: oms-workflow-persistence, Property 8: Step normalization preserves ids,
tools, and edges.

Asserts that normalizing any valid ``HighLevelPlan`` into ``WorkflowStep``s
preserves the complete set of sub-goal ids, each sub-goal's tool references, and
each sub-goal's ``depends_on`` edges. Uses a ``high_level_plans()`` generator and
runs a minimum of 100 iterations.
"""

from __future__ import annotations

import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from planning.high_level_plan import HighLevelPlan, SubGoal
from persistence.models import compute_bare_tool_name
from persistence.plan_reader import extract_steps


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Constrain to the plan's real input space: distinct sub-goal ids, plausible
# bare tool names, and depends_on edges that only reference earlier sub-goals
# (a DAG, matching how the Planning agent emits them).
_ids = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=8)
_tools = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=12)


@st.composite
def high_level_plans(draw) -> HighLevelPlan:
    """Generate a valid HighLevelPlan with a DAG of sub-goals.

    Sub-goal ids are unique; each sub-goal suggests zero or more bare tools; and
    each ``depends_on`` edge points only at an earlier sub-goal id so the graph
    stays acyclic (as the Planning agent produces).
    """
    ids = draw(st.lists(_ids, min_size=0, max_size=6, unique=True))

    sub_goals: list[SubGoal] = []
    for i, sub_goal_id in enumerate(ids):
        suggested_tools = draw(st.lists(_tools, min_size=0, max_size=4))
        earlier = ids[:i]
        depends_on = draw(
            st.lists(st.sampled_from(earlier), min_size=0, max_size=len(earlier), unique=True)
            if earlier
            else st.just([])
        )
        sub_goals.append(
            SubGoal(
                id=sub_goal_id,
                description=draw(st.text(max_size=20)),
                reasoning=draw(st.text(max_size=20)),
                suggested_tools=suggested_tools,
                suggested_skills=[],
                depends_on=depends_on,
            )
        )

    return HighLevelPlan(
        execution_mode=draw(st.sampled_from(["direct", "standard", "complex"])),
        sub_goals=sub_goals,
        suggested_tools={},
        suggested_skills={},
        reasoning=draw(st.text(max_size=20)),
    )


# ---------------------------------------------------------------------------
# Property 8: Step normalization preserves ids, tools, and edges
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(plan=high_level_plans())
def test_normalization_preserves_ids_tools_and_edges(plan: HighLevelPlan):
    steps = extract_steps(plan)

    # 1. Complete set of sub-goal ids is preserved — every sub-goal contributes
    #    at least one step, and no foreign ids are introduced.
    assert {s.sub_goal_id for s in steps} == {sg.id for sg in plan.sub_goals}

    # 2. Per-sub-goal tool references are preserved (normalized to bare names) in
    #    declared order. A tool-less sub-goal yields exactly one step with
    #    bare_tool=None; otherwise one step per suggested tool, each reduced to
    #    its bare name (namespaced -> suffix; already-bare kept verbatim).
    for sub_goal in plan.sub_goals:
        got = [s.bare_tool for s in steps if s.sub_goal_id == sub_goal.id]
        expected = (
            [compute_bare_tool_name(t) or t for t in sub_goal.suggested_tools]
            if sub_goal.suggested_tools
            else [None]
        )
        assert got == expected

    # 3. Per-sub-goal depends_on edges are preserved exactly on every emitted step.
    for sub_goal in plan.sub_goals:
        for step in (s for s in steps if s.sub_goal_id == sub_goal.id):
            assert step.depends_on == list(sub_goal.depends_on)


# ---------------------------------------------------------------------------
# Unit examples: bare-name normalization of suggested tools (R3.2/R3.5)
# ---------------------------------------------------------------------------


def _plan_with_tools(tools: list[str]) -> HighLevelPlan:
    """A minimal single-sub-goal plan suggesting ``tools``."""
    return HighLevelPlan(
        execution_mode="standard",
        sub_goals=[
            SubGoal(
                id="sg1",
                description="",
                reasoning="",
                suggested_tools=list(tools),
                suggested_skills=[],
                depends_on=[],
            )
        ],
        suggested_tools={},
        suggested_skills={},
        reasoning="",
    )


def test_namespaced_suggested_tool_is_reduced_to_bare():
    steps = extract_steps(_plan_with_tools(["audio-stem___separate_audio_stems"]))
    assert [s.bare_tool for s in steps] == ["separate_audio_stems"]


def test_already_bare_suggested_tool_is_kept_verbatim():
    steps = extract_steps(_plan_with_tools(["separate_audio_stems"]))
    assert [s.bare_tool for s in steps] == ["separate_audio_stems"]


def test_tool_less_sub_goal_stays_none():
    steps = extract_steps(_plan_with_tools([]))
    assert [s.bare_tool for s in steps] == [None]


def test_multi_segment_namespace_uses_final_separator():
    steps = extract_steps(_plan_with_tools(["a___b___separate_audio_stems"]))
    assert [s.bare_tool for s in steps] == ["separate_audio_stems"]
