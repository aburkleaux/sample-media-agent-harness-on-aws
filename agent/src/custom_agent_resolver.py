"""Invoke-time config resolution for Custom Agents (task 3.1).

A Custom Agent is the existing ``SpecialtyAgent`` asset extended with a
``model``, ``tool_collections``, and ``integration_config``. Custom Agents do
not get their own runtime — they are multiplexed onto the shared Task Agent
runtime. At invoke time the stored config must be mapped onto the payload the
Task Agent (`task_agent.py`) already consumes:

    persona.md        -> system_prompt
    model             -> model_id     (omitted when unset; runtime default applies)
    compatible_skills -> skills
    tool_collections  -> ToolRef -> gateway tool names -> tools

This module owns that mapping as a single cohesive unit. It is intentionally
pure and dependency-injected (no network, no AWS, no AssetIndex coupling): the
caller supplies the loaded config, the set of tool names bindable on the
runtime (built from :mod:`mcp_manager` — the same name-binding authority the
Task Agent uses), and optionally the known model-id set (from
:mod:`model_catalog`). This keeps the resolver fully unit-testable and lets a
later task layer owner-selected allowlist bounding (task 3.3) on top without
reshaping the seam.

Graceful degradation (Requirements 3.4, 3.5, 3.6):
- Tool collections that cannot be resolved to at least one bindable gateway
  tool name at invoke time are dropped and logged; the invocation continues
  with the union of the resolvable remainder.
- When no tool collection resolves, ``tools`` is the empty list ``[]`` — the
  agent proceeds skills-only.

Allowlist invariant (Requirement 7.5): the resolver ALWAYS emits an explicit
``tools`` list (the resolved allowlist, or ``[]``), NEVER ``None``. The Task
Agent treats a missing/``None`` ``tools`` value as "load ALL tools", which would
silently bypass the owner-selected allowlist. Because every bound name is
derived only from the owner-selected ``tool_collections`` (and every skill only
from the owner-selected ``compatible_skills``), the resolved sets are already a
subset of the owner selection; task 3.3 extends this unit to assert that bound.

Model defaulting (Requirement 2.5): when ``model`` is unset the resolver leaves
``model_id`` unset so the Task Agent applies its own configured default. Model
ids are Bedrock inference-profile ids drawn from
``model_catalog.discover_models()``; when the caller supplies that id set the
resolver defensively drops an unknown id (falling back to the runtime default)
rather than dispatching an invalid model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)


@dataclass
class ResolvedCustomAgentConfig:
    """The Task-Agent payload fragment resolved from a Custom Agent's config.

    Attributes:
        system_prompt: The persona mapped to ``system_prompt``. ``None`` when
            the config carried no persona (the Task Agent then applies its own
            baseline system prompt).
        model_id: The Bedrock inference-profile id mapped to ``model_id``, or
            ``None`` when the model is unset/unknown. ``None`` MUST be rendered
            as an *omitted* ``model_id`` key so the runtime default applies
            (Requirement 2.5) — never as ``model_id = None``.
        skills: The resolved skill definitions mapped to ``skills``.
        tools: The resolved gateway tool-name allowlist mapped to ``tools``.
            ALWAYS an explicit list (possibly empty); NEVER ``None``
            (Requirement 7.5).
        dropped_collections: Ids of tool collections dropped during graceful
            degradation (unavailable, or yielding no bindable tool). Surfaced
            for logging/observability and test assertions.
    """

    system_prompt: Optional[str] = None
    model_id: Optional[str] = None
    skills: list[dict] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    dropped_collections: list[str] = field(default_factory=list)

    def apply_to_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Merge this resolution onto a Task-Agent payload dict, in place.

        ``system_prompt`` and ``model_id`` are only written when present, so an
        unset model leaves the payload key absent (runtime default applies).
        ``tools`` is always written as an explicit list (Requirement 7.5), and
        ``skills`` is always written (possibly empty).
        """
        if self.system_prompt is not None:
            payload["system_prompt"] = self.system_prompt
        if self.model_id is not None:
            payload["model_id"] = self.model_id
        else:
            # Never leave a null model_id behind — omit so the Task Agent's
            # configured default is used (Requirement 2.5).
            payload.pop("model_id", None)
        payload["skills"] = self.skills
        payload["tools"] = self.tools
        return payload


def _ref_name(ref: Any) -> str:
    """Extract a gateway tool name from a ToolRef-like object or dict.

    Accepts a :class:`lab.models.ToolRef` (``.name``) or a plain dict using
    either the snake/camel serialized shapes (``name``). Returns ``""`` when no
    name can be extracted so the caller can skip it.
    """
    name = getattr(ref, "name", None)
    if name is None and isinstance(ref, dict):
        name = ref.get("name")
    return name if isinstance(name, str) else ""


def _collection_id(collection: Any) -> str:
    """Best-effort id extraction for logging a dropped collection."""
    if isinstance(collection, dict):
        cid = collection.get("id") or collection.get("assetId") or collection.get("asset_id")
        return cid if isinstance(cid, str) else "<unknown>"
    cid = getattr(collection, "id", None) or getattr(collection, "asset_id", None)
    return cid if isinstance(cid, str) else "<unknown>"


def _collection_tool_refs(collection: Any) -> Optional[Sequence[Any]]:
    """Return a collection's tool refs, or ``None`` when unresolvable.

    ``None`` signals the collection could not be resolved to its tool refs at
    invoke time (e.g. the Tool_Collection asset was unavailable) and must be
    dropped-and-logged (Requirement 3.5). An empty sequence is a *resolvable*
    collection that simply carries no tools.

    Accepts a dict (``tool_refs`` or ``toolRefs``) or an object exposing
    ``tool_refs``.
    """
    if isinstance(collection, dict):
        if "tool_refs" in collection:
            refs = collection.get("tool_refs")
        elif "toolRefs" in collection:
            refs = collection.get("toolRefs")
        else:
            return None
    else:
        refs = getattr(collection, "tool_refs", None)
    if refs is None:
        return None
    if isinstance(refs, (list, tuple)):
        return refs
    return None


def _skill_identity(skill: Any) -> str:
    """Extract a stable identity for a resolved skill definition.

    Used by the allowlist-bounding guard (Requirements 7.1, 7.2, 7.4) to test a
    resolved skill against the owner-selected ``compatible_skills`` id set.
    Accepts a dict or an object and tries the id-like fields first, falling back
    to ``name`` (the field the Task Agent keys skills on). Returns ``""`` when no
    identity can be extracted.
    """
    keys = ("id", "assetId", "asset_id", "skillId", "skill_id", "name")
    if isinstance(skill, dict):
        for k in keys:
            v = skill.get(k)
            if isinstance(v, str) and v:
                return v
        return ""
    for k in keys:
        v = getattr(skill, k, None)
        if isinstance(v, str) and v:
            return v
    return ""


def _referenced_tool_names(tool_collections: Optional[Iterable[Any]]) -> set[str]:
    """Union of ALL gateway tool names referenced by the owner-selected
    ``tool_collections``, independent of runtime availability.

    This is the owner-selected tool allowlist *superset*: the resolved ``tools``
    MUST be a subset of it on every resolution path (Requirements 7.1, 7.2,
    7.4). Availability filtering only ever *narrows* the resolved set below this
    superset — it can never widen it — so this set is the correct upper bound to
    assert against. Collections that are unavailable (no resolvable refs)
    contribute nothing, which is sound because they also contribute no resolved
    tools.
    """
    names: set[str] = set()
    for collection in tool_collections or []:
        refs = _collection_tool_refs(collection)
        if not refs:
            continue
        for r in refs:
            n = _ref_name(r)
            if n:
                names.add(n)
    return names


def enforce_allowlist_bounds(
    skills: Sequence[dict],
    tools: Sequence[str],
    *,
    compatible_skills: Optional[Iterable[str]] = None,
    tool_collections: Optional[Iterable[Any]] = None,
) -> tuple[list[dict], list[str]]:
    """Bound resolved ``skills``/``tools`` to the owner selection (task 3.3).

    Final, hard allowlist guard applied on *every* resolution path — including
    the graceful-degradation path — so that nothing outside the owner's
    selection can ever be bound for an invocation (Requirements 7.1, 7.2, 7.4),
    while preserving the "``tools`` is always an explicit list" invariant
    (Requirement 7.5).

    By construction the resolver derives tool names only from the owner-selected
    ``tool_collections`` and passes through only the caller-supplied ``skills``
    (themselves derived from the owner-selected ``compatible_skills``), so this
    guard is a no-op on correct inputs. It is enforced explicitly nonetheless as
    a defense-in-depth backstop against any future path (or upstream bug) that
    might otherwise leak an unselected capability:

    - **tools**: filtered to the union of names referenced by the owner-selected
      ``tool_collections`` (``_referenced_tool_names``). Anything outside that
      superset is dropped and logged as an invariant breach.
    - **skills**: when ``compatible_skills`` is supplied, filtered to skills
      whose extracted identity is a member of that owner-selected id set.
      When ``compatible_skills`` is ``None`` the skill set is passed through
      unchanged (the caller has not supplied an allowlist to bound against).

    Returns:
        ``(bounded_skills, bounded_tools)`` — both subsets of their inputs,
        with ``bounded_tools`` always an explicit list (Requirement 7.5).
    """
    # --- tools ⊆ union(owner-selected tool_collection refs) ---
    owner_tool_superset = _referenced_tool_names(tool_collections)
    bounded_tools = [t for t in tools if t in owner_tool_superset]
    if len(bounded_tools) != len(tools):
        leaked = [t for t in tools if t not in owner_tool_superset]
        # Should be unreachable — resolve_tool_collections only ever emits
        # names drawn from these very collections. Log loudly if it ever fires.
        logger.error(
            "Custom Agent resolver: allowlist-bounding guard removed %d tool(s) "
            "outside the owner-selected tool_collections superset: %s "
            "(Req 7.1/7.2/7.4)",
            len(leaked),
            leaked,
        )

    # --- skills ⊆ owner-selected compatible_skills ---
    if compatible_skills is not None:
        allow = set(compatible_skills)
        bounded_skills: list[dict] = []
        for s in skills:
            sid = _skill_identity(s)
            if sid in allow:
                bounded_skills.append(s)
            else:
                logger.warning(
                    "Custom Agent resolver: allowlist-bounding guard dropping "
                    "skill %r not in the owner-selected compatible_skills "
                    "allowlist (Req 7.1/7.2/7.4)",
                    sid or "<unknown>",
                )
    else:
        bounded_skills = list(skills)

    return bounded_skills, bounded_tools


# ---------------------------------------------------------------------------
# External path — confused-deputy tool-binding guardrails (task 13.1, Req 11)
#
# Externally-invoked Custom Agents run with the *Owner's* resource access but
# are driven by *Caller* prompts, so the external path narrows the Req 7
# allowlist to the Owner's explicit **external opt-in** set
# (``IntegrationConfig.external_tool_collections``). These helpers are pure and
# operate on tool-name sets so Property 13 can test them in isolation:
#
#   external_bound = external_opt_in ∩ resolvable   ⊆ external_opt_in ⊆ selected
#
# The bound set is a function only of config-derived inputs (the opt-in set and
# the resolved allowlist) — never of tool-result content — so it is invariant
# under untrusted tool results (Req 11.3 / Property 13 invariance clause).
# ---------------------------------------------------------------------------


def external_opt_in_tool_names(
    tool_collections: Optional[Iterable[Any]],
    opt_in_ids: Optional[Iterable[str]],
) -> set[str]:
    """Union of gateway tool names referenced by the owner's external opt-in.

    Maps the owner's external opt-in *collection ids*
    (``IntegrationConfig.external_tool_collections``) onto the concrete gateway
    tool *names* they reference, by selecting only the ``tool_collections``
    whose id is in ``opt_in_ids`` and unioning their ``ToolRef`` names.

    Because the opt-in ids are validated at CRUD time to be a subset of the
    agent's selected ``tool_collections`` (Req 7 allowlist), the returned name
    set is ``⊆`` the full allowlist's name set — the ``external_opt_in ⊆
    selected_allowlist`` half of Property 13.

    Args:
        tool_collections: The owner-selected collections (dicts or objects with
            an id and ``tool_refs``/``toolRefs``), same shape the resolver
            consumes.
        opt_in_ids: The owner's external opt-in collection ids. ``None``/empty
            yields an empty set (fail-closed: no external tools).

    Returns:
        The set of gateway tool names permitted on the external path.
    """
    opt_in = {i for i in (opt_in_ids or []) if isinstance(i, str)}
    names: set[str] = set()
    if not opt_in:
        return names
    for collection in tool_collections or []:
        if _collection_id(collection) not in opt_in:
            continue
        refs = _collection_tool_refs(collection)
        if not refs:
            continue
        for r in refs:
            n = _ref_name(r)
            if n:
                names.add(n)
    return names


def narrow_external_tools(
    resolved_tools: Sequence[str],
    external_opt_in: Iterable[str],
) -> list[str]:
    """Narrow a resolved tool-name list to the owner's external opt-in set.

    Pure set intersection: returns the ``resolved_tools`` that are also in
    ``external_opt_in``, preserving ``resolved_tools`` order and dropping
    everything outside the opt-in. This is the runtime realization of
    Property 13:

    * ``external_bound ⊆ external_opt_in`` — every returned name is in the
      opt-in set, so the bound set can never exceed what the Owner permitted
      for external invocation (Req 11.1).
    * **Invariance under tool-result content** — the result depends *only* on
      the two config-derived inputs (the resolved allowlist and the opt-in
      set); no tool-result/turn content is an input, so untrusted tool results
      can never widen the bound set (Req 11.3). Callers therefore compute this
      **once, fixed at dispatch**.

    Args:
        resolved_tools: The tool names resolved for the invocation (the Req 7
            allowlist ∩ runtime-resolvable), always an explicit list.
        external_opt_in: The owner's external opt-in tool names (see
            :func:`external_opt_in_tool_names`).

    Returns:
        The narrowed, order-preserving list ``resolved_tools ∩ external_opt_in``
        (always an explicit list, possibly empty — never ``None``).
    """
    opt_in = set(external_opt_in)
    narrowed = [t for t in resolved_tools if t in opt_in]
    if len(narrowed) != len(resolved_tools):
        dropped = [t for t in resolved_tools if t not in opt_in]
        logger.info(
            "Custom Agent resolver: external opt-in narrowing dropped %d "
            "tool(s) outside the owner external opt-in set: %s (Req 11.1/11.3)",
            len(dropped),
            dropped,
        )
    return narrowed


def authorize_external_tools(
    tool_names: Iterable[str],
    *,
    principal: Optional[dict] = None,
    agent_id: str = "",
) -> list[str]:
    """Per-tool / per-caller authorization extension point (Req 11.4 / 7.6).

    **v1: no-op allow-all.** This is the documented seam where a future
    fine-grained, per-tool/per-caller authorization decision layer (for
    example a Cedar-style policy evaluated at a gateway boundary) is layered
    *above* the Owner opt-in set, before external tool access is dispatched.
    The custom-agents design models it as an optional decision layer invoked
    **between session/memory-key derivation and dispatch**; v1 leaves it as a
    pass-through so the extension path exists without changing behavior.

    A future implementation MUST only ever *narrow* the input set (it sits
    above, never below, the opt-in bound), returning the subset of
    ``tool_names`` the caller is authorized to use.

    Args:
        tool_names: The candidate external tool names (already narrowed to the
            Owner opt-in set by :func:`narrow_external_tools`).
        principal: The issuer-qualified external caller ``{issuer, sub}``.
            Reserved for a future per-caller policy; unused in v1.
        agent_id: The Custom Agent id. Reserved for a future per-agent policy;
            unused in v1.

    Returns:
        The authorized tool names. In v1 this is the input unchanged (a fresh
        explicit list).
    """
    # v1 pass-through. Intentionally returns a copy as an explicit list so the
    # caller can treat the result as owned/mutable and never receives ``None``.
    return list(tool_names)


def resolve_tool_collections(
    tool_collections: Optional[Iterable[Any]],
    *,
    available_tool_names: Optional[Iterable[str]] = None,
) -> tuple[list[str], list[str]]:
    """Resolve ``tool_collections`` to a bindable gateway-tool-name allowlist.

    Walks ``tool_collections -> ToolRef -> gateway tool names`` and unions the
    resolvable names, preserving first-seen order and de-duplicating. A
    collection is *dropped* (Requirement 3.5) when it is unavailable (its refs
    could not be resolved) or when none of its tool names bind to
    ``available_tool_names``.

    Args:
        tool_collections: The owner-selected collections. Each entry may be a
            dict (``{"id", "tool_refs"|"toolRefs"}``) or an object exposing an
            ``id``/``tool_refs``. A collection whose refs resolve to ``None``
            is treated as unavailable.
        available_tool_names: The tool names bindable on the runtime (built from
            :mod:`mcp_manager` connected gateways plus built-ins). When ``None``
            no availability filtering is applied (every ref name is considered
            bindable) — used by callers that resolve availability elsewhere.

    Returns:
        ``(tools, dropped)`` where ``tools`` is the de-duplicated union of
        bindable names (possibly empty) and ``dropped`` is the list of dropped
        collection ids.
    """
    available: Optional[set[str]] = (
        set(available_tool_names) if available_tool_names is not None else None
    )
    tools: list[str] = []
    seen: set[str] = set()
    dropped: list[str] = []

    for collection in tool_collections or []:
        cid = _collection_id(collection)
        refs = _collection_tool_refs(collection)
        if refs is None:
            # Unavailable at invoke time — drop and continue (Req 3.5).
            dropped.append(cid)
            logger.warning(
                "Custom Agent resolver: dropping unresolvable tool collection %r "
                "(unavailable at invoke time); continuing with resolvable remainder",
                cid,
            )
            continue

        names = [n for n in (_ref_name(r) for r in refs) if n]
        if available is not None:
            bindable = [n for n in names if n in available]
        else:
            bindable = names

        if not bindable:
            # Resolvable asset, but no tool binds right now — degrade (Req 3.5).
            dropped.append(cid)
            logger.warning(
                "Custom Agent resolver: dropping tool collection %r — no bindable "
                "gateway tools (of %d referenced) available at invoke time",
                cid,
                len(names),
            )
            continue

        for n in bindable:
            if n not in seen:
                seen.add(n)
                tools.append(n)

    return tools, dropped


def resolve_model_id(
    model: Optional[str],
    *,
    known_model_ids: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Resolve the stored ``model`` to a payload ``model_id`` (or ``None``).

    Returns ``None`` (meaning: omit ``model_id`` so the runtime default applies,
    Requirement 2.5) when the model is unset/blank, or when a non-empty
    ``known_model_ids`` set is supplied and the model is not a member of it. Model
    ids come from ``model_catalog.discover_models()``; passing that id set lets
    the resolver fall back to the runtime default rather than dispatching an id
    the runtime cannot serve. When ``known_model_ids`` is ``None`` or empty the
    model is passed through unchanged (CRUD-time validation is authoritative).
    """
    if not model or not str(model).strip():
        return None
    if known_model_ids is not None:
        known = set(known_model_ids)
        if known and model not in known:
            logger.warning(
                "Custom Agent resolver: model %r not in known catalog ids; "
                "omitting model_id so the runtime default applies",
                model,
            )
            return None
    return model


def resolve_config(
    *,
    persona: Optional[str] = None,
    model: Optional[str] = None,
    skills: Optional[Iterable[dict]] = None,
    tool_collections: Optional[Iterable[Any]] = None,
    available_tool_names: Optional[Iterable[str]] = None,
    known_model_ids: Optional[Iterable[str]] = None,
    compatible_skills: Optional[Iterable[str]] = None,
) -> ResolvedCustomAgentConfig:
    """Map a Custom Agent's stored config onto a Task-Agent payload fragment.

    See the module docstring for the full mapping table and invariants.

    Args:
        persona: The persona text (from ``persona.md``) -> ``system_prompt``.
        model: The stored Bedrock inference-profile id -> ``model_id`` (omitted
            when unset; Requirement 2.5).
        skills: The resolved ``compatible_skills`` definitions -> ``skills``.
        tool_collections: The owner-selected tool collections -> ``tools`` via
            ``ToolRef`` -> gateway tool names, with graceful degradation.
        available_tool_names: Bindable tool names on the runtime (from
            :mod:`mcp_manager`); ``None`` disables availability filtering.
        known_model_ids: Known catalog model ids (from
            :func:`model_catalog.discover_models`); ``None`` disables the check.
        compatible_skills: The owner-selected skill id allowlist. When supplied,
            the resolved ``skills`` are bounded to this set as a final guard
            (Requirements 7.1, 7.2, 7.4); ``None`` passes the skills through
            unchanged (no allowlist supplied to bound against).

    Returns:
        A :class:`ResolvedCustomAgentConfig`. ``tools`` is always an explicit
        list; ``model_id`` is ``None`` (omit) when the model is unset/unknown.

    Allowlist bounding (task 3.3, Requirements 7.1, 7.2, 7.4, 7.5): the resolved
    ``skills``/``tools`` are strictly a subset of the owner-selected
    ``compatible_skills``/``tool_collections``. This holds by construction (tool
    names are only ever derived from the owner-selected collections, and skills
    are only ever the caller-supplied defs), and is then re-asserted as an
    explicit final guard via :func:`enforce_allowlist_bounds` so that nothing
    outside the owner selection can be bound on any path — including the
    degradation path — while ``tools`` stays an explicit list (never ``None``).
    """
    tools, dropped = resolve_tool_collections(
        tool_collections, available_tool_names=available_tool_names
    )
    resolved_skills = list(skills) if skills else []

    # Final allowlist-bounding guard on every resolution path (task 3.3).
    resolved_skills, tools = enforce_allowlist_bounds(
        resolved_skills,
        tools,
        compatible_skills=compatible_skills,
        tool_collections=tool_collections,
    )

    return ResolvedCustomAgentConfig(
        system_prompt=persona if persona else None,
        model_id=resolve_model_id(model, known_model_ids=known_model_ids),
        skills=resolved_skills,
        tools=tools,
        dropped_collections=dropped,
    )
