"""Gateway domain package.

Gateway tool registry (``gateway.registry``) and per-user MCP server config
(``gateway.user_mcp``), grouped here (C1 modularization). The MCP client manager
(``mcp_manager`` — imported at module scope by the entrypoints; the 2026 outage
module) stays top-level for now and is a candidate for its own isolated move.
Entrypoints import these via ``from gateway.<mod> import ...``.
"""
