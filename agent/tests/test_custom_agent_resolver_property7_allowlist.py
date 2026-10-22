"""Property-based test for Custom_Agent owner-selected allowlist bounding.

Feature: custom-agents, Property 7: Owner-selected allowlist bounds exposure

Validates: Requirements 7.1, 7.2, 7.4

Property 7 (design.md): For any Custom_Agent invocation, the set of skills and
tools exposed to the LLM is a subset of the owner-selected skills and
tool_collections; any skill or tool_collection not selected by the owner is
never bound, on every resolution path.

Layer under test
----------------
The resolver (`agent/src/custom_agent_resolver.py`) owns the config->payload
mapping. Task 3.3 added ``enforce_allowlist_bounds(skills, tools, *,
compatible_skills, tool_collections)`` and wired it into ``resolve_config`` so
that the resolved ``skills``/``tools`` are strictly a subset of the owner
selection on *every* path (including the graceful-degradation path):

  * resolved ``tools`` ⊆ ``_referenced_tool_names(tool_collections)`` — the
    union of every gateway tool name referenced by the owner-selected
    ``tool_collections``;
  * resolved ``skills`` identities ⊆ the owner-selected ``compatible_skills``
    id set (when supplied);
  * ``tools`` is ALWAYS an explicit list, never ``None`` (Req 7.5 invariant that
    keeps the "None ⇒ load all" Task-Agent default from widening the allowlist).

This property drives BOTH seams:

  1. ``resolve_config(...)`` end-to-end — including adversarial skill defs whose
     identity is NOT in ``compatible_skills`` (which must be dropped), across
     resolvable / unavailable / availability-filtered tool-collection mixes; and
  2. ``enforce_allowlist_bounds(...)`` directly with a ``tools`` list carrying a
     name referenced by NO owner-selected collection and skills outside the
     owner set — exercising the guard's drop path head-on.

On every path we assert: bounded ``tools`` ⊆ owner-referenced names; bounded
``skills`` identities ⊆ owner-selected ``compatible_skills``; nothing unselected
survives; and ``tools`` is an explicit list.
"""

from __future__ import annotations

import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from custom_agent_resolver import (  # noqa: E402
    _referenced_tool_names,
    _skill_identity,
    enforce_allowlist_bounds,
    resolve_config,
)
from lab.models import ToolRef  # noqa: E402

# Short, stable identifier alphabet for names/ids.
_token = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="-_",
    ),
    min_size=1,
    max_size=8,
)


def _owner_name(tok: str) -> str:
    """Namespace a token into the owner-selected tool-name keyspace."""
    return f"own::{tok}"


def _outsider_name(tok: str) -> str:
    """Namespace a token into a keyspace disjoint from any owner selection."""
    return f"out::{tok}"


@st.composite
def _scenario(draw):
    """Generate an owner selection plus adversarial, unselected inputs.

    Returns a dict carrying:
      - ``tool_collections``: owner-selected collections (dicts with ``id`` and
        ``tool_refs``); some are marked unavailable (``tool_refs=None``) and
        refs alternate between ``ToolRef`` objects and dict form.
      - ``compatible_skills``: the owner-selected skill id allowlist.
      - ``resolve_skills``: the skill defs handed to ``resolve_config`` — a mix
        of SELECTED defs (identity ∈ compatible_skills) and ADVERSARIAL defs
        (identity ∉ compatible_skills, from the disjoint ``out::`` keyspace).
      - ``available_tool_names``: availability filter (``None`` or a subset of
        all names) — availability only ever NARROWS the resolved tools.
      - ``adversarial_tools``: a tools list containing a name referenced by NO
        owner collection, for the direct ``enforce_allowlist_bounds`` path.
    """
    # --- owner-selected tool collections -----------------------------------
    owner_tokens = draw(st.lists(_token, min_size=0, max_size=6, unique=True))
    owner_names = [_owner_name(t) for t in owner_tokens]

    # Partition the owner names into a handful of collections.
    n_collections = draw(st.integers(min_value=0, max_value=4))
    collections = []
    # round-robin assign names to collections
    buckets: list[list[str]] = [[] for _ in range(n_collections)]
    for i, name in enumerate(owner_names):
        if n_collections:
            buckets[i % n_collections].append(name)
    for idx, bucket in enumerate(buckets):
        unavailable = draw(st.booleans())
        if unavailable:
            # Unresolvable collection: contributes NOTHING (dropped + logged).
            collections.append({"id": f"tc-{idx}", "tool_refs": None})
            continue
        refs: list = []
        for j, name in enumerate(bucket):
            if j % 2 == 0:
                refs.append(ToolRef(name=name, version="1"))
            else:
                refs.append({"name": name, "version": "1"})
        collections.append({"id": f"tc-{idx}", "tool_refs": refs})

    # --- owner-selected compatible_skills ----------------------------------
    skill_tokens = draw(st.lists(_token, min_size=0, max_size=6, unique=True))
    compatible_skills = [_owner_name(t) for t in skill_tokens]

    # SELECTED skill defs: identity is a member of compatible_skills.
    selected_defs = [{"id": sid, "name": f"skill-{sid}"} for sid in compatible_skills]

    # ADVERSARIAL skill defs: identity in the disjoint ``out::`` keyspace, so it
    # can never be a member of compatible_skills.
    adv_skill_tokens = draw(st.lists(_token, min_size=0, max_size=4, unique=True))
    adversarial_defs = [
        {"id": _outsider_name(t), "name": f"skill-{_outsider_name(t)}"}
        for t in adv_skill_tokens
    ]

    resolve_skills = draw(st.permutations(selected_defs + adversarial_defs))

    # --- availability filter (only narrows) --------------------------------
    all_names = owner_names + [_outsider_name(t) for t in adv_skill_tokens]
    if all_names and draw(st.booleans()):
        available_tool_names = draw(
            st.lists(st.sampled_from(all_names), max_size=len(all_names), unique=True)
        )
    else:
        available_tool_names = None

    # --- adversarial tools list for the direct-guard path ------------------
    # Names referenced by NO owner collection, plus (some of) the legitimate
    # owner names, in arbitrary order.
    injected = [_outsider_name(t) for t in draw(st.lists(_token, min_size=1, max_size=4, unique=True))]
    adversarial_tools = draw(st.permutations(list(owner_names) + injected))

    return {
        "tool_collections": collections,
        "compatible_skills": compatible_skills,
        "resolve_skills": list(resolve_skills),
        "available_tool_names": available_tool_names,
        "adversarial_tools": list(adversarial_tools),
        "adversarial_skills": adversarial_defs,
    }


# ---------------------------------------------------------------------------
# Property 7: Owner-selected allowlist bounds exposure
# ---------------------------------------------------------------------------

# Feature: custom-agents, Property 7: Owner-selected allowlist bounds exposure
@settings(max_examples=200, deadline=None)
@given(scenario=_scenario())
def test_property7_owner_selected_allowlist_bounds_exposure(scenario):
    """Exposed skills/tools ⊆ owner selection on every resolution path.

    Drives both the end-to-end ``resolve_config`` path (with adversarial,
    unselected skills injected and resolvable/unavailable/availability-filtered
    tool-collection mixes) and the ``enforce_allowlist_bounds`` guard directly
    (with a tool name referenced by no owner collection and skills outside the
    owner set). Asserts nothing unselected is ever bound and ``tools`` is always
    an explicit list.

    Validates: Requirements 7.1, 7.2, 7.4
    """
    tool_collections = scenario["tool_collections"]
    compatible_skills = scenario["compatible_skills"]
    owner_tool_superset = _referenced_tool_names(tool_collections)
    owner_skill_set = set(compatible_skills)

    # === Path 1: resolve_config end-to-end =================================
    resolved = resolve_config(
        persona="p",
        model=None,
        skills=scenario["resolve_skills"],
        tool_collections=tool_collections,
        available_tool_names=scenario["available_tool_names"],
        compatible_skills=compatible_skills,
    )

    # tools is ALWAYS an explicit list (Req 7.5), never None.
    assert isinstance(resolved.tools, list)
    # Resolved tools ⊆ owner-referenced tool names; nothing unselected present.
    assert set(resolved.tools) <= owner_tool_superset
    # No name from the disjoint outsider keyspace ever leaks through.
    assert all(not t.startswith("out::") for t in resolved.tools)

    # Resolved skills' identities ⊆ owner-selected compatible_skills.
    resolved_skill_ids = {_skill_identity(s) for s in resolved.skills}
    assert resolved_skill_ids <= owner_skill_set
    # Every adversarial (unselected) skill def was dropped.
    adv_ids = {_skill_identity(s) for s in scenario["adversarial_skills"]}
    assert resolved_skill_ids.isdisjoint(adv_ids)
    assert all(not sid.startswith("out::") for sid in resolved_skill_ids)

    # === Path 2: enforce_allowlist_bounds direct adversarial injection =====
    # Feed a tools list containing names referenced by NO owner collection, and
    # skills whose identity is outside the owner set. The guard must strip them.
    injected_tools = scenario["adversarial_tools"]
    injected_skills = scenario["resolve_skills"]  # selected + adversarial mix

    bounded_skills, bounded_tools = enforce_allowlist_bounds(
        injected_skills,
        injected_tools,
        compatible_skills=compatible_skills,
        tool_collections=tool_collections,
    )

    assert isinstance(bounded_tools, list)
    # Bounded tools ⊆ owner-referenced names; the injected out:: names removed.
    assert set(bounded_tools) <= owner_tool_superset
    assert all(not t.startswith("out::") for t in bounded_tools)
    # Bounded tools ⊆ the original input (guard only ever drops, never adds).
    assert set(bounded_tools) <= set(injected_tools)

    # Bounded skills ⊆ owner-selected compatible_skills; adversarial dropped.
    bounded_skill_ids = {_skill_identity(s) for s in bounded_skills}
    assert bounded_skill_ids <= owner_skill_set
    assert bounded_skill_ids.isdisjoint(adv_ids)


# Feature: custom-agents, Property 7: Owner-selected allowlist bounds exposure
@settings(max_examples=200, deadline=None)
@given(
    scenario=_scenario(),
    extra=st.lists(_token, min_size=1, max_size=5, unique=True),
)
def test_property7_unselected_names_never_bound_when_skills_allowlist_absent(scenario, extra):
    """When no ``compatible_skills`` allowlist is supplied, tools stay bounded.

    A companion facet of Property 7: even when the caller supplies no skill
    allowlist to bound against (``compatible_skills=None``), the tool allowlist
    is still strictly bounded by the owner-selected ``tool_collections`` — an
    outsider tool name injected directly can never be bound.

    Validates: Requirements 7.1, 7.2, 7.4
    """
    tool_collections = scenario["tool_collections"]
    owner_tool_superset = _referenced_tool_names(tool_collections)

    injected = [_outsider_name(t) for t in extra]
    tools_in = list(owner_tool_superset) + injected

    bounded_skills, bounded_tools = enforce_allowlist_bounds(
        scenario["resolve_skills"],
        tools_in,
        compatible_skills=None,
        tool_collections=tool_collections,
    )

    assert isinstance(bounded_tools, list)
    # No unselected tool bound; every injected outsider name stripped.
    assert set(bounded_tools) <= owner_tool_superset
    assert all(name not in bounded_tools for name in injected)
    # With no skill allowlist supplied, skills pass through unchanged.
    assert bounded_skills == list(scenario["resolve_skills"])
