"""Gateway tool catalog helpers (Wave 2).

Pure normalization of raw MCP tool objects into the harness's
``{name, description, annotations}`` metadata shape — extracted from
``GatewayConnection`` where it was duplicated verbatim across connect and
refresh. Side effects (the read-only-name registry, search-availability flag,
allowlist / added / trusted state) remain on the connection.

The per-tool field extractors are injected rather than imported so this stays a
leaf module with no dependency back on ``mcp_manager`` (no import cycle).
"""

from __future__ import annotations

from typing import Any, Callable


def normalize_tool_metadata(
    raw_tools: list,
    *,
    extract_name: Callable[[Any], str],
    extract_description: Callable[[Any], str],
    extract_annotations: Callable[[Any], Any],
) -> list[dict]:
    """Return one ``{name, description, annotations}`` dict per raw tool."""
    return [
        {
            "name": extract_name(t),
            "description": extract_description(t),
            "annotations": extract_annotations(t),
        }
        for t in raw_tools
    ]


__all__ = ["normalize_tool_metadata"]
