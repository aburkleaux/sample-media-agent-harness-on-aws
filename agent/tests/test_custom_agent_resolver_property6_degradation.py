"""Property-based test for Custom_Agent graceful tool degradation + model defaulting.

Feature: custom-agents, Property 6: Graceful tool degradation and model defaulting

Validates: Requirements 3.4, 3.5, 3.6, 2.5

Property 6 (design.md): For any Custom_Agent config and any partition of its
``tool_collections`` into resolvable and unresolvable at invoke time, the bound
tool set equals the union of tools from the resolvable collections and the
invocation still proceeds; when no collection resolves the agent proceeds
skills-only (``tools == []``); and when ``model`` is unset the resolved payload
omits ``model_id``.

Test seam
---------
This drives the real invoke-time resolver (``custom_agent_resolver.py``):
``resolve_config`` (and, through it, ``resolve_tool_collections`` /
``resolve_model_id``), plus ``ResolvedCustomAgentConfig.apply_to_payload`` for
the payload-shape invariants (Req 2.5 model omission, Req 7.5 explicit tools
list). The resolver is pure and dependency-injected, so the property runs many
generated resolvable/unresolvable partitions with no AWS/network coupling.

A collection is *unresolvable* here when either:
  - it carries no tool refs (a dict without ``tool_refs``/``toolRefs`` -> the
    resolver treats it as unavailable at invoke time), or
  - none of its ref names are in ``available_tool_names`` (nothing binds).
A collection is *resolvable* when at least one of its ref names is available.
"""

from __future__ import annotations

import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from custom_agent_resolver import resolve_config  # noqa: E402


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Gateway-tool-name-ish identifiers: non-empty, from a stable alphabet.
_tool_name = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="._-",
    ),
    min_size=1,
    max_size=12,
)

# A tool-collection "spec" is one of:
#   ("missing",)                         -> dict with no tool_refs (unavailable)
#   ("refs", [names], camel_bool)        -> dict with tool_refs/toolRefs
# We resolve the spec into a concrete dict (with a unique id) inside the test so
# ids never collide and dropped-collection ids can be asserted exactly.


@st.composite
def _scenario(draw):
    """Generate (all_names, available_names, specs, model, known_model_ids).

    ``all_names`` is the universe of ref names; each name is independently
    marked available or not, giving an arbitrary resolvable/unresolvable
    partition. ``specs`` are the tool-collection specs (see above).
    """
    all_names = draw(st.lists(_tool_name, unique=True, min_size=0, max_size=8))

    # Partition the universe into available / unavailable names.
    if all_names:
        avail_flags = draw(
            st.lists(st.booleans(), min_size=len(all_names), max_size=len(all_names))
        )
        available_names = [n for n, f in zip(all_names, avail_flags) if f]
    else:
        available_names = []

    # Draw a ref list restricted to the universe (empty when the universe is).
    if all_names:
        _ref_list = st.lists(st.sampled_from(all_names), min_size=0, max_size=5)
    else:
        _ref_list = st.just([])

    def _spec():
        return st.one_of(
            st.tuples(st.just("missing")),
            st.tuples(st.just("refs"), _ref_list, st.booleans()),
        )

    specs = draw(st.lists(_spec(), min_size=0, max_size=6))

    # Model defaulting inputs (Req 2.5). Either unset (None / blank / whitespace)
    # or a known catalog member.
    known_model_ids = draw(st.lists(_tool_name, unique=True, min_size=0, max_size=5))
    if known_model_ids and draw(st.booleans()):
        model = draw(st.sampled_from(known_model_ids))  # set + known
    else:
        model = draw(st.sampled_from([None, "", "   ", "\t\n"]))  # unset/blank

    return all_names, available_names, specs, model, known_model_ids


def _build_collections(specs):
    """Turn specs into concrete tool_collection dicts with unique ids."""
    collections = []
    for i, spec in enumerate(specs):
        cid = f"tc-{i}"
        if spec[0] == "missing":
            collections.append({"id": cid})  # no tool_refs -> unavailable
        else:
            _, names, camel = spec
            key = "toolRefs" if camel else "tool_refs"
            collections.append({"id": cid, key: [{"name": n} for n in names]})
    return collections


def _expected_tools_and_dropped(specs, available_names):
    """Mirror the resolver's degradation logic to compute expectations."""
    available = set(available_names)
    expected_tools: list[str] = []
    seen: set[str] = set()
    expected_dropped: list[str] = []

    for i, spec in enumerate(specs):
        cid = f"tc-{i}"
        if spec[0] == "missing":
            expected_dropped.append(cid)  # unavailable
            continue
        names = spec[1]
        bindable = [n for n in names if n in available]
        if not bindable:
            expected_dropped.append(cid)  # resolvable asset, nothing binds
            continue
        for n in bindable:
            if n not in seen:
                seen.add(n)
                expected_tools.append(n)
    return expected_tools, expected_dropped


# ---------------------------------------------------------------------------
# Property 6: Graceful tool degradation and model defaulting
# ---------------------------------------------------------------------------

# Feature: custom-agents, Property 6: Graceful tool degradation and model defaulting
@settings(max_examples=200, deadline=None)
@given(scenario=_scenario())
def test_property6_graceful_degradation_and_model_defaulting(scenario):
    """Bound tools = union of resolvable collections; model_id omitted when unset.

    Validates: Requirements 3.4, 3.5, 3.6, 2.5
    """
    all_names, available_names, specs, model, known_model_ids = scenario

    collections = _build_collections(specs)
    expected_tools, expected_dropped = _expected_tools_and_dropped(
        specs, available_names
    )
    model_is_unset = not (model and str(model).strip())

    resolved = resolve_config(
        persona="you are a test agent",
        model=model,
        skills=[{"name": "skill-a"}],
        tool_collections=collections,
        available_tool_names=available_names,
        known_model_ids=known_model_ids,
    )

    # --- Graceful tool degradation (Req 3.4, 3.5, 3.6) ---------------------
    # Bound tools == de-duplicated union of names from the resolvable
    # collections (intersected with availability). Unresolvable collections are
    # dropped, and the invocation still proceeds (resolve_config always returns).
    assert resolved.tools == expected_tools
    assert resolved.dropped_collections == expected_dropped

    # tools is ALWAYS an explicit list, never None (Req 7.5 invariant).
    assert isinstance(resolved.tools, list)

    # When no collection resolves, the agent proceeds skills-only (Req 3.6).
    if not expected_tools:
        assert resolved.tools == []

    # --- Model defaulting (Req 2.5) ---------------------------------------
    if model_is_unset:
        # Unset/blank model -> omit model_id so the runtime default applies.
        assert resolved.model_id is None
    else:
        # Set + known catalog member -> passed through unchanged.
        assert resolved.model_id == model

    # --- Payload shape invariants via apply_to_payload --------------------
    # Seed a payload with a STALE model_id to prove it is removed when unset.
    payload = {"model_id": "STALE-DEFAULT", "prompt": "hi"}
    resolved.apply_to_payload(payload)

    # tools is always present as an explicit list on the payload (Req 7.5).
    assert isinstance(payload["tools"], list)
    assert payload["tools"] == expected_tools

    if model_is_unset:
        # model_id key must be ABSENT so the Task Agent's default applies (Req 2.5).
        assert "model_id" not in payload
    else:
        assert payload["model_id"] == model
