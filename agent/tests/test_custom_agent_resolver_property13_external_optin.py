"""Property-based test for external tool-binding bounds and invariance.

Feature: custom-agents, Property 13: External tool binding is bounded by the owner opt-in set

Validates: Requirements 11.1, 11.3

Property 13 (design.md): For any Custom_Agent with external integration enabled,
and for any tool-result content produced during the turn, the set of tools bound
for an external invocation is a subset of the owner's **external opt-in set**,
which is itself a subset of the Req 7 full allowlist:

    external_bound ⊆ external_opt_in ⊆ selected_allowlist

and the bound set is invariant under tool-result content (untrusted tool results
never widen it — Req 11.3).

Layer under test
----------------
The resolver (`agent/src/custom_agent_resolver.py`, task 13.1) exposes pure,
config-derived helpers that realize the external opt-in narrowing:

  * ``external_opt_in_tool_names(tool_collections, opt_in_ids) -> set[str]`` —
    maps the owner's external opt-in *collection ids* onto the gateway tool
    *names* they reference, selecting only collections whose id is in the opt-in
    set. Because the opt-in ids are validated at CRUD time to be a subset of the
    owner-selected ``tool_collections`` (Req 7 allowlist), the returned names are
    ``⊆`` the full allowlist superset (``_referenced_tool_names``).

  * ``narrow_external_tools(resolved_tools, external_opt_in) -> list[str]`` — an
    order-preserving intersection ``resolved ∩ opt_in``. Its output depends ONLY
    on those two config-derived inputs; it takes NO tool-result argument, so no
    turn/tool-result content can ever be an input that widens the bound set.

This test asserts the subset chain, models the Req 11.3 invariance clause by
computing the bound set, then generating arbitrary "tool-result content" and
asserting recomputation is identical, and injects a resolved tool name that is
NOT in the opt-in set to prove it never surfaces in ``external_bound``.
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
    external_opt_in_tool_names,
    narrow_external_tools,
    resolve_tool_collections,
)
from lab.models import ToolRef  # noqa: E402

# Short, stable identifier alphabet for tool names / collection ids.
_token = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="-_",
    ),
    min_size=1,
    max_size=8,
)


def _tool_name(tok: str) -> str:
    """Namespace a token into the owner-selected tool-name keyspace."""
    return f"tool::{tok}"


# Arbitrary "tool-result content" — the untrusted turn data that Req 11.3 says
# must never widen the bound set. We generate deeply-arbitrary JSON-ish values,
# including strings shaped exactly like real tool names, to prove the binding
# functions ignore everything but their two config-derived inputs.
_tool_result_content = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.text(max_size=20),
        _token.map(_tool_name),  # adversarial: looks like a legit tool name
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=8), children, max_size=4),
    ),
    max_leaves=12,
)


@st.composite
def _scenario(draw):
    """Generate an owner selection, an external opt-in id subset, and resolved tools.

    Returns a dict carrying:
      - ``tool_collections``: owner-selected collections (dicts with ``id`` and
        ``tool_refs``); refs alternate between ``ToolRef`` objects and dict form.
      - ``opt_in_ids``: a subset of the selected collection ids (the external
        opt-in) PLUS some ids outside the selected set (which contribute no
        names — mirrors CRUD's opt_in ⊆ selected guarantee plus defensiveness).
      - ``resolved_tools``: a resolved allowlist ⊆ the selected superset, as the
        Req 7 resolver bounds it there via ``resolve_tool_collections``.
    """
    # --- owner-selected tool collections -----------------------------------
    n_collections = draw(st.integers(min_value=0, max_value=5))
    collections: list[dict] = []
    collection_ids: list[str] = []
    for idx in range(n_collections):
        cid = f"tc-{idx}"
        collection_ids.append(cid)
        tokens = draw(st.lists(_token, min_size=0, max_size=4, unique=True))
        refs: list = []
        for j, tok in enumerate(tokens):
            name = _tool_name(tok)
            if j % 2 == 0:
                refs.append(ToolRef(name=name, version="1"))
            else:
                refs.append({"name": name, "version": "1"})
        collections.append({"id": cid, "tool_refs": refs})

    # --- external opt-in ids: a subset of selected ids + some outsiders ------
    selected_opt_in = (
        draw(st.lists(st.sampled_from(collection_ids), max_size=len(collection_ids), unique=True))
        if collection_ids
        else []
    )
    outsider_ids = [f"not-selected-{t}" for t in draw(st.lists(_token, max_size=3, unique=True))]
    opt_in_ids = draw(st.permutations(selected_opt_in + outsider_ids))

    # --- resolved tools ⊆ selected superset (the Req 7 resolver bounds it) ---
    # Use the real resolver to produce the resolved allowlist, optionally
    # filtered by an availability subset (which only ever narrows the superset).
    superset = sorted(_referenced_tool_names(collections))
    if superset and draw(st.booleans()):
        available = draw(st.lists(st.sampled_from(superset), max_size=len(superset), unique=True))
    else:
        available = None
    resolved_tools, _dropped = resolve_tool_collections(
        collections, available_tool_names=available
    )

    return {
        "tool_collections": collections,
        "opt_in_ids": list(opt_in_ids),
        "selected_opt_in": selected_opt_in,
        "outsider_ids": outsider_ids,
        "resolved_tools": resolved_tools,
    }


# ---------------------------------------------------------------------------
# Property 13: External tool binding is bounded by the owner opt-in set
# ---------------------------------------------------------------------------

# Feature: custom-agents, Property 13: External tool binding is bounded by the owner opt-in set
@settings(max_examples=200, deadline=None)
@given(
    scenario=_scenario(),
    tool_results=st.lists(_tool_result_content, max_size=5),
    injected_tok=_token,
)
def test_property13_external_binding_bounds_and_invariance(scenario, tool_results, injected_tok):
    """external_bound ⊆ external_opt_in ⊆ selected_allowlist, invariant under tool results.

    Validates: Requirements 11.1, 11.3
    """
    tool_collections = scenario["tool_collections"]
    opt_in_ids = scenario["opt_in_ids"]
    resolved_tools = scenario["resolved_tools"]

    selected_allowlist = _referenced_tool_names(tool_collections)
    external_opt_in = external_opt_in_tool_names(tool_collections, opt_in_ids)
    external_bound = narrow_external_tools(resolved_tools, external_opt_in)

    # === Subset chain: external_bound ⊆ external_opt_in ⊆ selected_allowlist =
    assert external_opt_in <= selected_allowlist
    assert set(external_bound) <= external_opt_in
    assert set(external_bound) <= selected_allowlist
    # narrow_external_tools always returns an explicit list (never None) and
    # only ever drops from resolved_tools (never invents names).
    assert isinstance(external_bound, list)
    assert set(external_bound) <= set(resolved_tools)

    # Opt-in ids outside the owner-selected set contribute no names.
    assert external_opt_in_tool_names(tool_collections, scenario["outsider_ids"]) == set()
    # Empty opt-in yields an empty bound (fail-closed).
    assert external_opt_in_tool_names(tool_collections, []) == set()
    assert narrow_external_tools(resolved_tools, set()) == []

    # === Invariance under arbitrary tool-result content (Req 11.3) ==========
    # Compute the bound set, then produce arbitrary untrusted tool-result
    # content, then recompute with the SAME two config-derived inputs: the
    # result is identical because tool-result content is not an input at all.
    _ = tool_results  # arbitrary untrusted content produced during the turn
    rebound = narrow_external_tools(resolved_tools, external_opt_in)
    assert rebound == external_bound
    # Even names lifted from the tool-result content cannot enter the bound set
    # unless they were already in (resolved_tools ∩ external_opt_in).
    injected_names = [v for v in tool_results if isinstance(v, str)]
    for name in injected_names:
        if name in external_bound:
            assert name in resolved_tools and name in external_opt_in

    # === Inject a resolved tool NOT in the opt-in — it never binds ==========
    # Fabricate a name guaranteed to be outside the opt-in set, append it to the
    # resolved list, and assert narrowing never lets it into the bound set.
    injected_tool = f"injected::{injected_tok}"
    assert injected_tool not in external_opt_in  # namespace is disjoint from tool::
    widened_resolved = list(resolved_tools) + [injected_tool]
    bound_after_injection = narrow_external_tools(widened_resolved, external_opt_in)
    assert injected_tool not in bound_after_injection
    assert set(bound_after_injection) <= external_opt_in
    # Appending an out-of-opt-in name can never widen the bound set beyond the
    # original (order-preserving intersection over a superset input).
    assert set(bound_after_injection) == set(external_bound)
