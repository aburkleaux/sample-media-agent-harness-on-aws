"""Search_Tool binding (design Component 7 "Binding rules", Req 4.12, 4.16, 4.18, 5.3, 5.5, 9.10).

Pure: given what an invocation is, say which search tool (if any) it gets. Search is arm-A only —
the Main_Agent runs the Search_Tool itself; there is no task-runtime search-dispatch path.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

AGENT_SCOPE_MARKERS = ("principal", "agent_test", "delegated")


@dataclass(frozen=True)
class SearchDescriptor:
    runtime: str                      # "main" | "task"
    has_turn_context: bool = False
    project_access: Optional[str] = None
    has_token: bool = False
    agent_scoped: bool = False


def resolve_search_binding(d: SearchDescriptor) -> Optional[str]:
    """One of "search_tool", "search_tool_unavailable", or None.

    The Main_Agent binds the Search_Tool when it holds a turn context: normally, or in
    `unavailable` mode when project access is not `ok` or no token is present. An agent-scoped
    invoke, or any non-main runtime, binds nothing.
    """
    if d.agent_scoped:
        return None
    if d.runtime == "main":
        if not d.has_turn_context:
            return None
        unavailable = d.project_access != "ok" or not d.has_token
        return "search_tool_unavailable" if unavailable else "search_tool"
    return None
