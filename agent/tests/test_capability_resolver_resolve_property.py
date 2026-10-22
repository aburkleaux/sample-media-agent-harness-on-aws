"""Property tests for the deterministic step -> Capability join (tasks 3.4-3.6).

Three Hypothesis properties (>=100 iterations each) over the pure
``resolve(steps, index)`` classifier, driven by ``high_level_plans()`` /
``capability_indexes()`` style generators:

- Property 1 (task 3.4): deterministic join, exact and only exact.
- Property 3 (task 3.5): itemized failure names every offender.
- Property 4 (task 3.6): unpublished tool name is a distinct dependency gap.

Requirements: 2.1, 2.2, 2.3, 3.3, 3.4, 3.5, 4.2, 4.3, 12.3
"""

from __future__ import annotations

import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from planning.high_level_plan import HighLevelPlan, SubGoal
from persistence.capability_resolver import resolve
from persistence.models import Capability, CapabilityIndex, WorkflowStep
from persistence.plan_reader import extract_steps

_SEP = "___"

# Alnum words joined by single underscores can never contain ``___`` and never
# lead/trail with ``_`` — so a bare token is always a clean, separator-less name.
_word = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
    min_size=1,
    max_size=6,
)


@st.composite
def _bare_token(draw: st.DrawFn) -> str:
    """A realistic bare tool / action token: alnum words joined by ``_``."""
    words = draw(st.lists(_word, min_size=1, max_size=3))
    return "_".join(words)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build_index(capabilities: list[Capability]) -> CapabilityIndex:
    """Build a CapabilityIndex whose lookup indexes only *published* caps.

    Mirrors the invariant of the (later-wave) ``build_capability_index``: the
    bare-name lookup only contains Capabilities that publish a bare tool name;
    the ``capabilities`` list still carries unpublished ones for gap detection.
    """
    lookup: dict[str, list[Capability]] = {}
    for cap in capabilities:
        if cap.bare_tool_name is not None:
            lookup.setdefault(cap.bare_tool_name, []).append(cap)
    return CapabilityIndex(capabilities=list(capabilities), lookup=lookup)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_ids = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=8)


@st.composite
def high_level_plans(draw: st.DrawFn, tool_pool: list[str]) -> HighLevelPlan:
    """Generate a valid HighLevelPlan drawing tools from a shared ``tool_pool``.

    Drawing suggested tools from the same small pool the capability index is
    built from guarantees a healthy mix of exact matches, ambiguity, and misses
    across iterations. ``depends_on`` edges only reference earlier sub-goals so
    the graph stays acyclic (as the Planning agent emits).
    """
    ids = draw(st.lists(_ids, min_size=0, max_size=5, unique=True))
    tool_choice = st.sampled_from(tool_pool) if tool_pool else _bare_token()

    sub_goals: list[SubGoal] = []
    for i, sub_goal_id in enumerate(ids):
        suggested_tools = draw(st.lists(tool_choice, min_size=0, max_size=3))
        earlier = ids[:i]
        depends_on = draw(
            st.lists(st.sampled_from(earlier), min_size=0, max_size=len(earlier), unique=True)
            if earlier
            else st.just([])
        )
        sub_goals.append(
            SubGoal(
                id=sub_goal_id,
                description=draw(st.text(max_size=12)),
                reasoning=draw(st.text(max_size=12)),
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
        reasoning=draw(st.text(max_size=12)),
    )


@st.composite
def capability_indexes(draw: st.DrawFn, tool_pool: list[str]) -> CapabilityIndex:
    """Build a CapabilityIndex over the shared ``tool_pool``.

    Each pool token becomes zero or more published Capabilities (0 => the token
    is a miss; 1 => mappable; >1 => ambiguous) and may additionally get an
    unpublished Capability (separator-less or missing tool name) so unpublished
    gaps arise. Service/action names are namespaced away from the bare-tool pool.
    """
    caps: list[Capability] = []
    for i, tok in enumerate(tool_pool):
        n_published = draw(st.integers(min_value=0, max_value=2))
        for k in range(n_published):
            caps.append(
                Capability(
                    service=f"svc{i}_{k}",
                    action=f"act{i}_{k}",
                    gateway_tool_name=f"tgt{i}_{k}{_SEP}{tok}",
                    bare_tool_name=tok,
                )
            )
        if draw(st.booleans()):
            # An unpublished Capability whose separator-less tool name intends
            # this token (bare_tool_name is None -> a bridge-publication gap).
            caps.append(
                Capability(
                    service=f"usvc{i}",
                    action=f"uact{i}",
                    gateway_tool_name=tok,  # separator-less => no bare name
                    bare_tool_name=None,
                )
            )
    return _build_index(caps)


@st.composite
def _plan_and_index(draw: st.DrawFn):
    """Draw a shared tool pool, then a plan and a capability index over it."""
    tool_pool = draw(st.lists(_bare_token(), min_size=0, max_size=6, unique=True))
    plan = draw(high_level_plans(tool_pool))
    index = draw(capability_indexes(tool_pool))
    return plan, index


# ---------------------------------------------------------------------------
# Property 1: Deterministic-join correctness (exact and only exact) — task 3.4
# ---------------------------------------------------------------------------


# Feature: oms-workflow-persistence, Property 1: Deterministic-join correctness (exact and only exact)
@settings(max_examples=200)
@given(_plan_and_index())
def test_deterministic_join_exact_only(case) -> None:
    """Validates: Requirements 3.3, 3.5

    A step maps to a service.action if and only if its bare tool reference is
    exactly equal to the published bare tool name of exactly one Capability;
    no fuzzy, case-insensitive, or substring matching.
    """
    plan, index = case
    steps = extract_steps(plan)
    published = [c for c in index.capabilities if c.bare_tool_name is not None]

    report = resolve(steps, index)

    # Every mapped step corresponds to exactly one exact published match, and its
    # service/action/task_id come from that Capability (exact join, R3.3).
    for mapped in report.mapped:
        exact = [c for c in published if c.bare_tool_name == mapped.step.bare_tool]
        assert len(exact) == 1
        assert (mapped.service, mapped.action) == (exact[0].service, exact[0].action)
        assert mapped.task_id == f"{mapped.service}__{mapped.action}"

    # Conversely: mapping happens iff exactly one exact published match exists.
    for step in steps:
        exact = (
            [c for c in published if c.bare_tool_name == step.bare_tool]
            if step.bare_tool is not None
            else []
        )
        mapped_here = [m for m in report.mapped if m.step == step]
        if len(exact) == 1:
            assert mapped_here, "a uniquely-matching step must be mapped"
        else:
            assert not mapped_here, "a non-unique / non-exact step must not be mapped"


# ---------------------------------------------------------------------------
# Property 3: Itemized failure names the offenders — task 3.5
# ---------------------------------------------------------------------------


@st.composite
def classified_scenarios(draw: st.DrawFn):
    """Build steps + index with a known-correct classification per step.

    Each distinct token is assigned a category (mapped / ambiguous / unpublished
    / nomatch); tokens are unique so categories never cross-contaminate. Returns
    the steps, the index, and the expected sub_goal_id set per bucket. Unique
    ``sgN`` ids make offender itemization checkable exactly.
    """
    tokens = draw(st.lists(_bare_token(), min_size=0, max_size=8, unique=True))
    caps: list[Capability] = []
    steps: list[WorkflowStep] = []
    expected: dict[str, set[str]] = {
        "mapped": set(),
        "ambiguous": set(),
        "unpublished": set(),
        "unmappable": set(),
    }
    sid = 0
    for tok in tokens:
        category = draw(st.sampled_from(["mapped", "ambiguous", "unpublished", "nomatch"]))
        name = f"sg{sid}"
        sid += 1
        if category == "mapped":
            caps.append(
                Capability(
                    service=f"svc{sid}",
                    action=f"act{sid}",
                    gateway_tool_name=f"tgt{sid}{_SEP}{tok}",
                    bare_tool_name=tok,
                )
            )
            expected["mapped"].add(name)
        elif category == "ambiguous":
            for k in range(draw(st.integers(min_value=2, max_value=3))):
                caps.append(
                    Capability(
                        service=f"svc{sid}_{k}",
                        action=f"act{sid}_{k}",
                        gateway_tool_name=f"tgt{sid}_{k}{_SEP}{tok}",
                        bare_tool_name=tok,
                    )
                )
            expected["ambiguous"].add(name)
        elif category == "unpublished":
            caps.append(
                Capability(
                    service=f"svc{sid}",
                    action=f"act{sid}",
                    gateway_tool_name=tok,  # separator-less => unpublished gap
                    bare_tool_name=None,
                )
            )
            expected["unpublished"].add(name)
        else:  # nomatch — no Capability references this token at all
            expected["unmappable"].add(name)
        steps.append(WorkflowStep(sub_goal_id=name, bare_tool=tok))

    # Optionally add a tool-less node (also unmappable, R2.3).
    if draw(st.booleans()):
        name = f"sg{sid}"
        sid += 1
        steps.append(WorkflowStep(sub_goal_id=name, bare_tool=None))
        expected["unmappable"].add(name)

    return steps, _build_index(caps), expected


# Feature: oms-workflow-persistence, Property 3: Itemized failure names the offenders
@settings(max_examples=200)
@given(classified_scenarios())
def test_itemized_failure_names_offenders(case) -> None:
    """Validates: Requirements 2.2, 2.3, 3.4, 12.3

    For any plan with unmappable / ambiguous / unpublished steps, the request
    fails and the report itemizes every offending step — and, for ambiguity, the
    conflicting Capabilities.
    """
    steps, index, expected = case
    report = resolve(steps, index)

    assert {m.step.sub_goal_id for m in report.mapped} == expected["mapped"]
    assert {s.sub_goal_id for s in report.unmappable} == expected["unmappable"]
    assert {s.sub_goal_id for s, _ in report.ambiguous} == expected["ambiguous"]
    assert {s.sub_goal_id for s, _ in report.unpublished} == expected["unpublished"]

    # Ambiguity records the conflicting Capabilities (>= 2), each exactly matching.
    for step, conflicts in report.ambiguous:
        assert len(conflicts) >= 2
        assert all(c.bare_tool_name == step.bare_tool for c in conflicts)

    # Aggregate fail-closed: ok iff no offender in any failure bucket.
    has_offenders = bool(
        expected["unmappable"] or expected["ambiguous"] or expected["unpublished"]
    )
    assert report.ok == (not has_offenders)


# ---------------------------------------------------------------------------
# Property 4: Unpublished tool name is a distinct dependency gap — task 3.6
# ---------------------------------------------------------------------------


@st.composite
def _unpublished_cases(draw: st.DrawFn):
    """A needed Capability with a missing OR separator-less tool name + its step."""
    flavor = draw(st.sampled_from(["separatorless", "missing"]))
    if flavor == "separatorless":
        tok = draw(_bare_token())
        cap = Capability(
            service="svc",
            action="act",
            gateway_tool_name=tok,  # no ``___`` => publishes no bare name
            bare_tool_name=None,
        )
        step_bare = tok
    else:  # no Gateway_Tool_Name at all => the action is the intended identity
        action = draw(_bare_token())
        cap = Capability(
            service="svc",
            action=action,
            gateway_tool_name=None,
            bare_tool_name=None,
        )
        step_bare = action
    return cap, step_bare


# Feature: oms-workflow-persistence, Property 4: Unpublished tool name is a distinct dependency gap
@settings(max_examples=200)
@given(_unpublished_cases())
def test_unpublished_is_distinct_gap(case) -> None:
    """Validates: Requirements 4.2, 4.3

    A step needing a Capability that exists but has no published (or
    separator-less) gateway tool name is classified ``unpublished`` and is NOT
    reported as a generic unmappable step.
    """
    cap, step_bare = case
    step = WorkflowStep(sub_goal_id="sg-needed", bare_tool=step_bare)

    report = resolve([step], _build_index([cap]))

    assert not report.ok
    # Classified as the distinct bridge-publication gap, pairing the Capability.
    assert [s.sub_goal_id for s, _ in report.unpublished] == ["sg-needed"]
    assert report.unpublished[0][1] is cap
    # Distinct from generic unmappable, and never mapped.
    assert step not in report.unmappable
    assert not report.mapped
