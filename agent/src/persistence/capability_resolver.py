"""Deterministic capability resolution for the OMS workflow-persistence tool.

This module is the "join seam" of the `Persistence_Tool`: it turns a discovered
workflow step's *bare* tool reference into an OMS Core `service.action`
Capability through an exact, non-heuristic match on the bare tool name — no
fuzzy matching, no embeddings, no LLM inference (R3.5).

This module contributes the **pure** join logic plus the I/O-driven registry
snapshot build:

- `compute_bare_tool_name` (R3.2, Property 2) computes the bare tool name of a
  `Gateway_Tool_Name` (`<target>___<bare>`) as the substring following the
  *final* namespace separator.
- `resolve` (R2/R3/R4, Properties 1/3/4) performs the deterministic,
  exact-equality join of every `WorkflowStep` against a `CapabilityIndex` and
  classifies each step as mapped / unmappable / ambiguous / unpublished.
- `build_capability_index` (R3.1, R4.1) is the I/O-driven builder: it reads the
  registry snapshot over the injected `OMSMcpClient` (`list_integrations` →
  `get_integration`) and assembles the `CapabilityIndex`. The per-action gateway
  tool name comes from the integration detail's `action_tool_names` map (the
  service-registry metadata the bridge publishes verbatim), *not* from
  `get_action_schema` — which resolves only `{service, action, input, output}`
  from installed kits and never surfaces the published `tool_name`.

`compute_bare_tool_name` and `resolve` remain pure logic — no boto3, no MCP, no
S3/DDB. `build_capability_index` is the sole seam that touches the OMS MCP
boundary, and it does so only through the injected client; any OMS transport
failure surfaces as `OMSUnavailableError` (already raised by the client).

Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2, 4.3
Properties: 1, 2, 3, 4
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .models import (
    Capability,
    CapabilityIndex,
    MappedStep,
    ResolutionReport,
    WorkflowStep,
    compute_bare_tool_name,
)

if TYPE_CHECKING:  # pragma: no cover — typing only, avoids importing boto3 at runtime
    from .oms_mcp_client import OMSMcpClient

logger = logging.getLogger(__name__)


# ``compute_bare_tool_name`` is the single shared bare-name normalizer; it lives
# in ``persistence.models`` (alongside ``_NAMESPACE_SEPARATOR``) as a public
# primitive so both the capability side (here) and the step side
# (``plan_reader``) depend on it without an import cycle. It is re-exported from
# this module (see the ``from .models import`` above and ``__all__`` below) so
# existing ``capability_resolver.compute_bare_tool_name`` imports/tests keep
# working (R3.2, Property 2).


# ---------------------------------------------------------------------------
# Deterministic step -> Capability join (R2, R3, R4; Properties 1, 3, 4)
# ---------------------------------------------------------------------------


def _derive_task_id(service: str, action: str) -> str:
    """Derive a mapped step's ``task_id`` from its ``service`` + ``action``.

    Uses the assembler's ``f"{service}__{action}"`` convention (R5.4) so a step
    resolved here and the task the assembler later emits share the same id.
    """
    return f"{service}__{action}"


def _intended_bare_name(cap: Capability) -> Optional[str]:
    """The bare identity an *unpublished* Capability intends to expose.

    Only meaningful for a Capability whose ``bare_tool_name is None`` (it has not
    published a usable ``Gateway_Tool_Name``). Its intent is derived
    deterministically so the resolver can tell "the bridge hasn't published this
    capability yet" (unpublished, R4.2/R4.3) apart from "this reference is not a
    registered capability at all" (unmappable, R2.2):

    - a non-empty but separator-less ``gateway_tool_name`` yields no bare suffix,
      so the whole string is the intended bare identity; otherwise
    - with no ``gateway_tool_name`` at all, the Capability's ``action`` is its
      natural bare identity.

    Returns ``None`` for a published Capability (not part of unpublished
    detection).
    """
    if cap.bare_tool_name is not None:
        return None  # pragma: no cover — defensive; callers pre-filter unpublished
    if cap.gateway_tool_name:
        return cap.gateway_tool_name
    return cap.action


def _match_unpublished(
    bare_tool: str, unpublished: list[Capability]
) -> Optional[Capability]:
    """Return the first unpublished Capability whose intent equals ``bare_tool``.

    Deterministic: scans ``unpublished`` in registry order and returns the first
    Capability whose ``_intended_bare_name`` is exactly ``bare_tool`` (no fuzzy
    matching, R3.5). ``None`` when no unpublished Capability explains the step.
    """
    for cap in unpublished:
        if _intended_bare_name(cap) == bare_tool:
            return cap
    return None


def resolve(steps: list[WorkflowStep], index: CapabilityIndex) -> ResolutionReport:
    """Join every ``WorkflowStep`` against ``index`` and classify each step.

    Pure (no I/O). The join is an **exact string equality** of a step's bare tool
    reference against each Capability's published bare tool name — no fuzzy,
    case-insensitive, substring, embedding, or LLM matching (R3.5, Property 1).
    Each step lands in exactly one bucket of the returned ``ResolutionReport``:

    - **mapped** — the step's ``bare_tool`` exactly equals the published bare tool
      name of *exactly one* Capability (R3.3). Its ``task_id`` is derived from the
      Capability's ``service`` + ``action`` via ``f"{service}__{action}"`` (R5.4).
    - **unmappable** — the step is tool-less (``bare_tool is None``, e.g. a pure
      agent/model/prompt node, R2.3) or its reference exactly matches no published
      bare tool name and no unpublished Capability's intent (R2.2).
    - **ambiguous** — the step's ``bare_tool`` exactly matches *more than one*
      published Capability; the conflicting Capabilities are recorded (R3.4).
    - **skipped** — the step has no published capability match but its
      ``bare_tool`` is a status/polling tool OMS Core handles internally (its
      bare name is a value in some integration's ``action_status_tools`` map,
      recorded in ``index.status_tool_bare_names``). Such a step is intentionally
      dropped: it is not perennialized into a task and does NOT fail resolution.
      A genuine capability match always wins over this status-tool
      classification (precedence: mapped/ambiguous before skipped).
    - **unpublished** — the step has no published match but is explained by a
      Capability that exists in the registry with **no** published (or
      separator-less) ``Gateway_Tool_Name`` (``bare_tool_name is None``). This is
      the distinct ``Bridge_Tool_Name_Publication`` dependency gap (R4.2/R4.3),
      reported apart from generic unmappable. The unpublished Capability's intent
      is derived by ``_intended_bare_name``; the first such Capability (in
      registry order) whose intent equals the step's ``bare_tool`` is recorded.

    Any populated unmappable/ambiguous/unpublished bucket makes
    ``ResolutionReport.ok`` ``False`` (aggregate fail-closed), with every offender
    itemized so the caller can name them (R12.3, Property 3).

    Args:
        steps: The normalized workflow steps to resolve (from the Plan Reader).
        index: The registry snapshot to join against. Its ``lookup`` indexes only
            *published* Capabilities; its ``capabilities`` list also carries
            unpublished ones (``bare_tool_name is None``) used for gap detection.

    Returns:
        An itemized ``ResolutionReport`` partitioning the steps into the four
        buckets above.
    """
    report = ResolutionReport()
    # Unpublished Capabilities are the only ones that can explain an otherwise
    # unmatched step as a bridge-publication gap rather than truly unmappable.
    unpublished = [cap for cap in index.capabilities if cap.bare_tool_name is None]

    for step in steps:
        # A tool-less node has no reference to join — unmappable by R2.3.
        if step.bare_tool is None:
            report.unmappable.append(step)
            continue

        # Exact-equality join against published bare tool names only (Property 1).
        matches = index.matches(step.bare_tool)
        if len(matches) == 1:
            cap = matches[0]
            report.mapped.append(
                MappedStep(
                    step=step,
                    service=cap.service,
                    action=cap.action,
                    task_id=_derive_task_id(cap.service, cap.action),
                )
            )
        elif len(matches) > 1:
            # More than one published Capability claims the same bare name (R3.4).
            report.ambiguous.append((step, list(matches)))
        elif step.bare_tool in index.status_tool_bare_names:
            # No published capability, but this bare name is a status/polling
            # tool OMS Core handles internally (a value in some integration's
            # action_status_tools map). It is intentionally NOT perennialized and
            # must NOT fail resolution — skip it. A real capability match (above)
            # always wins over this status-tool name collision.
            report.skipped.append(step)
        else:
            # No published match — is this a bridge-publication gap (R4.2/R4.3)
            # or genuinely unmappable (R2.2)?
            gap = _match_unpublished(step.bare_tool, unpublished)
            if gap is not None:
                report.unpublished.append((step, gap))
            else:
                report.unmappable.append(step)

    return report


# ---------------------------------------------------------------------------
# Registry snapshot build over MCP (R3.1, R4.1)
# ---------------------------------------------------------------------------


def _extract_integration_id(integration: dict) -> Optional[str]:
    """Pull the integration identifier from a ``list_integrations`` item.

    Tolerates the registry's naming variants (``integration_id`` / ``id`` /
    ``service``); returns ``None`` when the item carries no usable identifier so
    the caller can skip it rather than fabricate one.
    """
    for key in ("integration_id", "id", "service"):
        value = integration.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_action_names(integration_detail: dict) -> list[str]:
    """Normalize an integration's ``actions`` into a list of action-name strings.

    The registry may carry ``actions`` as bare strings or as dicts keyed by
    ``action`` / ``name``; both shapes reduce to the action-name strings the
    per-action schema read needs. Order is preserved so the built index is
    deterministic.
    """
    actions = integration_detail.get("actions")
    if not isinstance(actions, list):
        return []
    names: list[str] = []
    for action in actions:
        if isinstance(action, str) and action:
            names.append(action)
        elif isinstance(action, dict):
            name = action.get("action") or action.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def build_capability_index(oms_client: "OMSMcpClient") -> CapabilityIndex:
    """Build the registry snapshot the deterministic join reads (R3.1, R4.1).

    Reads the OMS Core Service_Registry over the injected ``OMSMcpClient`` — the
    *only* seam here that touches the MCP boundary — and assembles a
    :class:`CapabilityIndex`:

    1. ``list_integrations`` enumerates the registered services.
    2. ``get_integration`` per service enumerates its actions *and* carries the
       per-action gateway tool name in its ``action_tool_names`` map
       (``{"<action>": "<gateway_tool_name>", ...}``) — the service-registry
       metadata the bridge publishes verbatim into ``POST /integrations``.

    The per-action ``tool_name`` is read from the integration detail's
    ``action_tool_names`` map (``detail.get("action_tool_names", {}).get(action)``),
    **not** from ``get_action_schema``: that tool resolves only
    ``{service, action, input, output}`` from installed kits and never surfaces a
    registry-published ``tool_name``, so a bridge-published name would never appear
    there. A ``tool_name`` is *unpublished* for an action when the map is absent or
    has no entry for that action (yielding ``None``).

    Each registered ``service.action`` becomes a :class:`Capability` whose
    ``gateway_tool_name`` is the published ``tool_name`` (``None`` when absent)
    and whose ``bare_tool_name`` is ``compute_bare_tool_name(tool_name)``
    (``None`` when unpublished or separator-less).

    The returned index carries **every** Capability — published *and* unpublished
    — in ``capabilities`` so the resolver can detect the unpublished
    ``Bridge_Tool_Name_Publication`` dependency gap (R4.2/R4.3). ``lookup`` indexes
    **only** published Capabilities (``bare_tool_name is not None``) by their bare
    tool name; a list of length > 1 for a bare name is exactly what ``resolve``
    classifies as ambiguous (R3.4).

    Args:
        oms_client: The injected MCP adapter over OMS Core. Read failures surface
            as :class:`OMSUnavailableError` (raised by the client) so the caller's
            pipeline leaves OMS Core untouched (R12.2).

    Returns:
        A :class:`CapabilityIndex` snapshotting the current registry.

    Raises:
        OMSUnavailableError: propagated from ``oms_client`` on any OMS transport
            or protocol failure during the reads.
    """
    capabilities: list[Capability] = []
    # Bare names of every status/polling tool across all integrations. A step
    # whose bare tool is one of these is handled internally by OMS Core (polling)
    # and must be skipped, not perennialized or failed (R-status).
    status_tool_bare_names: set[str] = set()

    for integration in oms_client.list_integrations():
        integration_id = _extract_integration_id(integration)
        if integration_id is None:
            # A registry row with no usable identifier cannot be read per-action.
            continue
        detail = oms_client.get_integration(integration_id)
        # The bridge publishes the per-action gateway tool name into the service
        # registry verbatim; get_integration returns it here (get_action_schema
        # never surfaces a registry-published tool_name — Option B).
        action_tool_names = detail.get("action_tool_names")
        if not isinstance(action_tool_names, dict):
            action_tool_names = {}
        # action_status_tools maps action -> the gateway tool name OMS polls for
        # that action's job status; each value's bare name is a status tool.
        action_status_tools = detail.get("action_status_tools")
        if isinstance(action_status_tools, dict):
            for status_tool_name in action_status_tools.values():
                bare = compute_bare_tool_name(status_tool_name)
                if bare is not None:
                    status_tool_bare_names.add(bare)
        for action in _extract_action_names(detail):
            # tool_name is unpublished (None) when the map is absent or has no
            # entry for this action.
            tool_name = action_tool_names.get(action)
            capabilities.append(
                Capability(
                    service=integration_id,
                    action=action,
                    gateway_tool_name=tool_name,
                    bare_tool_name=compute_bare_tool_name(tool_name),
                )
            )

    # lookup indexes only *published* capabilities (bare_tool_name not None);
    # a >1-length bucket is the ambiguity the join detects (R3.4).
    lookup: dict[str, list[Capability]] = {}
    for cap in capabilities:
        if cap.bare_tool_name is not None:
            lookup.setdefault(cap.bare_tool_name, []).append(cap)

    return CapabilityIndex(
        capabilities=capabilities,
        lookup=lookup,
        status_tool_bare_names=status_tool_bare_names,
    )


__all__ = ["compute_bare_tool_name", "resolve", "build_capability_index"]
