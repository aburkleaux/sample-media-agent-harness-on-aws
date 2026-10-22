"""Public Capability View — a sanitized, user-facing projection of what the
harness can currently do, for the ``agent-harness-help`` Skill.

Why this module exists (design D3 / Requirements 3, 4):

* The routing catalog in ``main.py`` is execution-oriented — it carries
  ``target___tool`` names, gateway ids, and internal primitives that are the
  wrong vocabulary for product help. The Help Skill must answer from a
  *sanitized* projection instead.
* Availability must reflect the *live* deployment (Req 4): a capability is
  presented as available only when an authorized source reports it, and a
  source that could not be read is reported as ``partial`` rather than being
  silently rendered as an empty/healthy absence.

Design shape: a **pure** projection. The impure gathering (reading gateways,
catching :class:`SourceUnavailable`, actor scoping) happens in ``main.py`` and
is handed to :func:`build_public_capability_view` as already-collected data
plus a list of degraded source labels. That keeps this module trivially
unit-testable — the internal-name-leakage property test (Property 1) can drive
it with adversarial catalogs and no AWS.

The deny policy is the repository's own :data:`PROHIBITED_TOOLS` (Req 3.4), so
adding a new internal capability cannot silently make it user-visible here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Optional, Sequence

from capability.descriptors import PROHIBITED_TOOLS

Availability = Literal["available", "unavailable", "unknown"]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicCapability:
    """One user-facing capability. Never carries an internal identifier."""

    category: str
    display_name: str
    description: str
    availability: Availability = "available"


@dataclass(frozen=True)
class PublicCapabilityView:
    """The sanitized projection handed to the Help Skill.

    ``partial_sources`` names the sources that could not be confirmed this
    request (e.g. an unreachable gateway). It is NOT an error list the user
    sees raw — the Help Skill turns it into "I couldn't confirm X" rather than
    claiming X is absent (Req 4.2).
    """

    foundational: tuple[PublicCapability, ...] = ()
    extensions: tuple[PublicCapability, ...] = ()
    partial_sources: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Internal-identifier guard (Req 3.1, 3.4)
# ---------------------------------------------------------------------------

# Shapes that must never reach user-facing help, independent of the tool
# denylist: gateway-qualified names, ARNs, S3 URIs, service endpoints, 12-digit
# account ids, and canonical URIs.
_IDENTIFIER_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"___"),                      # target___tool qualification
    re.compile(r"\barn:aws"),                # ARNs
    re.compile(r"s3://"),                    # bucket paths
    re.compile(r"\.amazonaws\.com"),         # service endpoints
    re.compile(r"canonical://"),             # canonical MCP links
    re.compile(r"\b\d{12}\b"),               # AWS account ids
)


def is_internal_identifier(text: Optional[str]) -> bool:
    """True when ``text`` is (or embeds) something users must never be shown.

    Combines the repository's prohibited-tool policy with structural identifier
    shapes. Applied to every string before it is surfaced.
    """
    if not text:
        return False
    if text in PROHIBITED_TOOLS:
        return True
    for pat in _IDENTIFIER_SHAPES:
        if pat.search(text):
            return True
    return False


def _friendly(name: str) -> str:
    """``diarize_speech`` -> ``Diarize speech``. Presentation only, never id."""
    return name.replace("_", " ").replace("-", " ").strip().capitalize()


# ---------------------------------------------------------------------------
# Stable foundational capabilities (Req 2.1) — always present, no live state,
# no tool names. These are product concepts, not deployment inventory.
# ---------------------------------------------------------------------------

FOUNDATIONAL: tuple[PublicCapability, ...] = (
    PublicCapability("files", "Work with your files",
                     "Attach, read, analyze, transform, preview, and download files."),
    PublicCapability("results", "Get results and downloads",
                     "Retrieve generated results — documents, media, data — and download them."),
    PublicCapability("workflows", "Run multi-step work",
                     "Break a goal into steps the assistant runs for you, tracking progress."),
    PublicCapability("jobs", "Track long-running jobs",
                     "Start work that runs in the background and check on it or get results later."),
    PublicCapability("skills", "Use and create Skills",
                     "Skills add domain instructions and methods; you can use built-in ones or create your own."),
    PublicCapability("custom-agents", "Use Custom Agents",
                     "A persona with its own model, Skills, and permitted capabilities for a focused job."),
    PublicCapability("tool-collections", "Use Tool Collections",
                     "A curated, reusable set of capabilities you can grant to an agent."),
    PublicCapability("connectors", "Connect external tools",
                     "Add external capabilities through MCP connectors/servers."),
    PublicCapability("troubleshooting", "Get help when something goes wrong",
                     "Understand why a step failed, what's unavailable, and what to try next."),
)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _clean_capability(
    *, category: str, raw_name: str, raw_description: Optional[str],
    availability: Availability,
) -> Optional[PublicCapability]:
    """Sanitize one live entry into a PublicCapability, or drop it.

    Drops the capability entirely when its name is an internal identifier or
    prohibited tool. Keeps the capability but drops the description when only
    the description embeds an identifier shape.
    """
    name = (raw_name or "").strip()
    if not name:
        return None
    # A gateway may report an already-qualified name. Reduce to the bare tail
    # BEFORE guarding, so a legitimate external tool reported as
    # ``target___tool`` still surfaces (as a friendly label) while the
    # qualification itself never leaks; then re-check the tail against policy.
    if "___" in name:
        name = name.rsplit("___", 1)[-1].strip()
    if not name or is_internal_identifier(name):
        return None
    description = (raw_description or "").strip()
    if is_internal_identifier(description):
        description = ""
    return PublicCapability(
        category=category,
        display_name=_friendly(name) if category != "skill" else name,
        description=description,
        availability=availability,
    )


def build_public_capability_view(
    *,
    skills: Iterable[dict[str, Any]] = (),
    external_tools: Iterable[dict[str, Any]] = (),
    partial_sources: Sequence[str] = (),
) -> PublicCapabilityView:
    """Project live, already-authorized sources into the sanitized view.

    Args:
        skills: ``{"name", "description"}`` for the request's authorized Skills.
        external_tools: ``{"name", "description", "available"?}`` for shared
            external (Gateway/connector) capabilities the actor may see.
            Prohibited/internal-shaped entries are dropped here as a
            defence-in-depth even if a caller forgot to filter.
        partial_sources: labels of sources that could not be confirmed this
            request. Deduped and sorted for stable output.

    Returns:
        A :class:`PublicCapabilityView` whose every surfaced string is free of
        prohibited tool names and internal identifier shapes.
    """
    extensions: list[PublicCapability] = []

    for skill in skills:
        cap = _clean_capability(
            category="skill",
            raw_name=str(skill.get("name") or ""),
            raw_description=skill.get("description"),
            availability="available",
        )
        if cap is not None:
            extensions.append(cap)

    for tool in external_tools:
        # A source that reported the tool but flagged it unavailable stays in
        # the view as unavailable (Req 4.3 — never claim the caller can use it).
        avail: Availability = "available" if tool.get("available", True) else "unavailable"
        cap = _clean_capability(
            category="connector",
            raw_name=str(tool.get("name") or ""),
            raw_description=tool.get("description"),
            availability=avail,
        )
        if cap is not None:
            extensions.append(cap)

    return PublicCapabilityView(
        foundational=FOUNDATIONAL,
        extensions=tuple(extensions),
        partial_sources=tuple(sorted({s for s in partial_sources if s})),
    )


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def render_public_capabilities(view: PublicCapabilityView) -> str:
    """Render the ``<public_capabilities>`` block for the Main Agent prompt.

    Bounded and identifier-free by construction (every field passed through the
    guard). The Help Skill is instructed to answer product-help questions from
    THIS block only, never from the execution-oriented routing catalog.
    """
    lines: list[str] = ["<public_capabilities>"]
    lines.append("  <foundational>")
    for cap in view.foundational:
        lines.append(f"    - {cap.display_name}: {cap.description}")
    lines.append("  </foundational>")

    if view.extensions:
        lines.append("  <available_now>")
        for cap in view.extensions:
            suffix = "" if cap.availability == "available" else f" [{cap.availability}]"
            desc = f": {cap.description}" if cap.description else ""
            lines.append(f"    - ({cap.category}) {cap.display_name}{desc}{suffix}")
        lines.append("  </available_now>")

    if view.partial_sources:
        lines.append("  <unconfirmed_sources>")
        lines.append(
            "    Some capability sources could not be confirmed this request; "
            "state that availability is unconfirmed rather than absent: "
            + ", ".join(view.partial_sources)
        )
        lines.append("  </unconfirmed_sources>")

    lines.append("</public_capabilities>")
    return "\n".join(lines)


__all__ = [
    "Availability",
    "PublicCapability",
    "PublicCapabilityView",
    "FOUNDATIONAL",
    "is_internal_identifier",
    "build_public_capability_view",
    "render_public_capabilities",
]
