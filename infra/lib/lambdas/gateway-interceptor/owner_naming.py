"""Target-name conventions for gateway tools.

This module used to derive ownership from a hash embedded in the target name,
which forced the same algorithm to exist in three places (this Lambda, the
proxy's TypeScript catalog reader, and the agent that creates targets) across
two languages. Ownership is now **recorded** at registration time instead --
see ``ownership.py`` -- so nothing here interprets a name.

What remains is not an algorithm we invented: AgentCore namespaces a target's
tools as ``target___tool``, and a dozen agent modules already depend on that.
Splitting on it is a string operation, not a shared derivation.

The ``usr_`` prefix survives for one narrow purpose: telling platform targets
from user-registered ones *before* consulting the ownership record, so a call
to a shared tool costs no lookup at all. It is a literal comparison, and the
authoritative owner is always the recorded one.
"""

from __future__ import annotations

# AgentCore joins the target name and the underlying tool name with this
# separator (e.g. ``media-outputs___get_download_url``).
TARGET_TOOL_SEPARATOR = "___"

# Prefix marking a target as a user-registered MCP server. Written by the
# registration path; only ever compared here, never parsed for meaning.
#
# Hyphen, not underscore: the service's target-name pattern is
# ([0-9a-zA-Z][-]?){1,100}, which rejects underscores. Must match
# _TARGET_PREFIX in agent/src/user_mcp.py and USER_TARGET_PREFIX in
# ui/server/toolCatalog.ts -- a mismatch makes every user tool look like
# platform surface, which fails OPEN.
USER_TARGET_PREFIX = "usr-"


def target_of_tool(tool_name: str) -> str | None:
    """Extract the target name from a gateway-qualified tool name.

    Returns ``None`` for unqualified names -- tools that are not namespaced to
    a target, such as the gateway's own built-in search tool.

    Tool names can carry more than one separator (``prefix___server___tool``),
    and the target is the FIRST segment. That matches how the rest of the
    system reads these names.
    """
    if TARGET_TOOL_SEPARATOR not in tool_name:
        return None
    return tool_name.split(TARGET_TOOL_SEPARATOR, 1)[0]


def is_user_target(target: str | None) -> bool:
    """Whether a target name denotes a user-registered MCP server.

    A fast negative check so platform tools skip the ownership lookup. A true
    result means "consult the ownership record", never "this caller owns it".
    """
    if not target:
        return False
    return target.startswith(USER_TARGET_PREFIX)
