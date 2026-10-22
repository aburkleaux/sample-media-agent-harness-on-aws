"""Deterministic Registry record identity (R4).

A Registry record's name and version are derived from *canonical identity*,
never from a display name:

    name          = mediaworks/<category>/<asset_id>
    recordVersion = v<canonical integer version>
    displayName   = canonical display name (free to change)

Built-ins have no AssetIndex lineage, so they get a reserved prefix:

    name          = mediaworks/builtin/<category>/<slug>
    recordVersion = v<build or semantic version>

Two consequences the projector and the backfill both rely on:

* renaming an asset never forks a Registry lineage, and
* replaying an event recomputes the identical ``(name, recordVersion)`` pair,
  which is what makes at-least-once projection converge (Property 4).

Shared MCP Gateway endpoints are not assets. Their record name is derived from
the gateway id so one reachable endpoint maps to exactly one ``MCP`` record —
never one per target or per tool (R4).
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

#: Root namespace segment for every record this harness projects. Scoping the
#: name (rather than relying on the Registry being per-environment) keeps a
#: hand-created record from colliding with a projected lineage.
NAMESPACE: str = "mediaworks"

#: Reserved second segment for capabilities that ship with the harness.
BUILTIN_SEGMENT: str = "builtin"

#: Reserved second segment for shared MCP Gateway endpoints.
MCP_ENDPOINT_CATEGORY: str = "mcp-endpoint"

#: Registry record names are path-like. Keep the character set narrow so a
#: category or asset id that somehow contains a slash cannot inject a segment.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_VERSION_RE = re.compile(r"^v(?P<version>[0-9A-Za-z][0-9A-Za-z.\-]*)$")


class RecordIdentity(NamedTuple):
    """A parsed Registry record name."""

    category: str
    asset_id: str
    is_builtin: bool


def _require_segment(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SEGMENT_RE.match(value):
        raise ValueError(
            f"{label} must match {_SEGMENT_RE.pattern!r}; got {value!r}"
        )
    return value


def record_name(category: str, asset_id: str) -> str:
    """``mediaworks/skill/asset-123`` for an authored asset."""
    return "/".join(
        (
            NAMESPACE,
            _require_segment(category, "category"),
            _require_segment(asset_id, "asset_id"),
        )
    )


def builtin_record_name(category: str, slug: str) -> str:
    """``mediaworks/builtin/skill/video-processing`` for a shipped capability."""
    return "/".join(
        (
            NAMESPACE,
            BUILTIN_SEGMENT,
            _require_segment(category, "category"),
            _require_segment(slug, "slug"),
        )
    )


def mcp_endpoint_record_name(gateway_id: str) -> str:
    """``mediaworks/mcp-endpoint/<gatewayId>`` — one record per endpoint.

    Deliberately keyed on the gateway id alone: targets and tools are nested
    provenance inside this record, not records of their own (R4).
    """
    return record_name(MCP_ENDPOINT_CATEGORY, gateway_id)


def record_version(version: int | str) -> str:
    """``2`` -> ``"v2"``. Accepts a build/semantic string for built-ins."""
    if isinstance(version, bool):  # bool is an int subclass; reject explicitly
        raise ValueError(f"version must not be a bool; got {version!r}")
    if isinstance(version, int):
        if version <= 0:
            raise ValueError(f"version must be a positive int; got {version!r}")
        return f"v{version}"
    text = str(version).strip()
    if not text:
        raise ValueError("version must be non-empty")
    if text.startswith("v"):
        text = text[1:]
    candidate = f"v{text}"
    if not _VERSION_RE.match(candidate):
        raise ValueError(f"unusable record version: {version!r}")
    return candidate


def parse_record_name(name: str) -> Optional[RecordIdentity]:
    """Inverse of :func:`record_name` / :func:`builtin_record_name`.

    Returns ``None`` for any name outside this harness's namespace — the
    Registry is per-environment but not exclusively ours, and an unparseable
    name must be skipped rather than guessed at.
    """
    parts = name.split("/") if name else []
    if len(parts) == 3 and parts[0] == NAMESPACE:
        return RecordIdentity(category=parts[1], asset_id=parts[2], is_builtin=False)
    if len(parts) == 4 and parts[0] == NAMESPACE and parts[1] == BUILTIN_SEGMENT:
        return RecordIdentity(category=parts[2], asset_id=parts[3], is_builtin=True)
    return None


def parse_record_version(value: Optional[str]) -> Optional[int | str]:
    """``"v2"`` -> ``2``; a non-numeric build version comes back as a string."""
    if not value:
        return None
    match = _VERSION_RE.match(value)
    if not match:
        return None
    version = match.group("version")
    return int(version) if version.isdigit() else version


def qualified_tool_name(target_name: str, bare_tool_name: str) -> str:
    """Target-qualified invocation identity used by the Gateway (``___``).

    The triple underscore is the AgentCore Gateway convention; it is the only
    thing that keeps two targets' ``get_job_status`` apart (R10).
    """
    return f"{target_name}___{bare_tool_name}"


def split_qualified_tool_name(value: str) -> tuple[Optional[str], str]:
    """``a___b`` -> ``("a", "b")``; an unqualified name -> ``(None, value)``."""
    if "___" not in value:
        return None, value
    target, _, bare = value.partition("___")
    return target, bare


def summary_id(
    *,
    category: str,
    asset_id: Optional[str] = None,
    version: Optional[int | str] = None,
    gateway_id: Optional[str] = None,
    target_id: Optional[str] = None,
    bare_tool_name: Optional[str] = None,
) -> str:
    """Build a ``CapabilitySummary.id``.

    Gateway tools key on ``(gatewayId, targetId, bareToolName)`` so repeated
    bare names across targets stay distinct rows in the UI.
    """
    if gateway_id and bare_tool_name:
        return ":".join(
            ("tool", gateway_id, target_id or "-", bare_tool_name)
        )
    if asset_id is None:
        raise ValueError("summary_id requires either gateway/tool ids or an asset_id")
    if version is None:
        return f"{category}:{asset_id}"
    return f"{category}:{asset_id}:{record_version(version)}"


__all__ = [
    "BUILTIN_SEGMENT",
    "MCP_ENDPOINT_CATEGORY",
    "NAMESPACE",
    "RecordIdentity",
    "builtin_record_name",
    "mcp_endpoint_record_name",
    "parse_record_name",
    "parse_record_version",
    "qualified_tool_name",
    "record_name",
    "record_version",
    "split_qualified_tool_name",
    "summary_id",
]
