"""Smoke test for the OMS MCP surface (task 14.1).

Asserts the ``Persistence_Tool``'s only seam to OMS Core — ``OMSMcpClient`` —
exposes *exactly* the six OMS MCP tools it is allowed to invoke and reaches OMS
Core through the injected gateway/MCP client alone, never through a direct HTTP
transport:

- ``OMS_MCP_TOOLS`` is the closed six-tuple, and the adapter exposes exactly one
  public method per tool (no extra OMS-tool surface).
- Functionally, invoking each of the six routes through the injected
  ``MCPClient.call_tool_sync`` seam — six calls, one per tool, nothing else.
- Statically, the module imports no HTTP client library (``requests`` / ``urllib``
  / ``httpx`` / ``http.client`` / ``aiohttp`` / raw ``socket``): the only outbound
  path is the injected gateway connection's ``call_tool_sync``.

Requirements: 11.1
"""

from __future__ import annotations

import ast
import json
import os
import sys

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from persistence.oms_mcp_client import OMS_MCP_TOOLS, OMSMcpClient

_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "persistence", "oms_mcp_client.py"
)

#: The exact, closed set of OMS MCP tools the adapter is allowed to invoke (R11.1).
_EXPECTED_SIX = (
    "list_integrations",
    "get_integration",
    "get_action_schema",
    "validate_template",
    "save_template",
    "activate_template",
)

#: HTTP client libraries that would indicate a direct-to-OMS transport bypassing
#: the injected gateway/MCP client. None of these may be imported by the adapter.
_FORBIDDEN_HTTP_MODULES = {
    "requests",
    "urllib",
    "urllib.request",
    "urllib3",
    "httpx",
    "http",
    "http.client",
    "aiohttp",
    "socket",
}


# ---------------------------------------------------------------------------
# Fakes — a gateway connection that records every MCP call (no real transport)
# ---------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, payload) -> None:
        self.content = [_Block(json.dumps(payload))]


class _RecordingMCPClient:
    """Records every ``call_tool_sync`` — the sole outbound seam to OMS Core."""

    def __init__(self, responses: dict) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def call_tool_sync(self, tool_use_id: str, name: str, arguments: dict = None):
        # Matches the real strands signature: (tool_use_id, name, arguments).
        self.calls.append((name, arguments))
        return _Result(self._responses[name.split("___")[-1]])


class _FakeGatewayConnection:
    def __init__(self, responses: dict) -> None:
        self.url = "https://oms.example/mcp"
        self._client = _RecordingMCPClient(responses)
        self._tools = [
            {"name": f"oms-core___{bare}", "description": "", "state": "available"}
            for bare in OMS_MCP_TOOLS
        ]

    def list_tools(self) -> list[dict]:
        return list(self._tools)


def _responses() -> dict:
    return {
        "list_integrations": [{"integration_id": "audio"}],
        "get_integration": {"integration_id": "audio", "actions": ["stem_separation"]},
        "get_action_schema": {"service": "audio", "action": "stem_separation"},
        "validate_template": {"valid": True, "findings": []},
        "save_template": {"template_id": "tpl-1", "operation": "created"},
        "activate_template": {"activated": "tpl-1"},
    }


# ---------------------------------------------------------------------------
# Exactly six tools exposed — no more, no less (R11.1)
# ---------------------------------------------------------------------------


def test_oms_mcp_tools_is_the_closed_six_tuple():
    assert OMS_MCP_TOOLS == _EXPECTED_SIX


def test_client_exposes_exactly_one_public_method_per_tool():
    # The public OMS-tool surface (excluding construction/endpoint helpers) is
    # precisely the six tools — no additional OMS-tool method exists.
    public = {
        name
        for name in dir(OMSMcpClient)
        if not name.startswith("_")
        and callable(getattr(OMSMcpClient, name))
        and name not in {"from_manager", "resolve_endpoint"}
    }
    assert public == set(_EXPECTED_SIX)


# ---------------------------------------------------------------------------
# Every call rides the injected MCPClient.call_tool_sync (no direct HTTP)
# ---------------------------------------------------------------------------


def test_all_six_tools_route_through_injected_call_tool_sync():
    conn = _FakeGatewayConnection(_responses())
    client = OMSMcpClient(conn)

    client.list_integrations()
    client.get_integration("audio")
    client.get_action_schema("audio", "stem_separation")
    client.validate_template({"template_id": "t"})
    client.save_template({"template_id": "t"}, update=False)
    client.activate_template("tpl-1")

    # Exactly six invocations, all through the MCPClient seam — nothing else.
    assert len(conn._client.calls) == 6
    invoked = {name.split("___")[-1] for name, _ in conn._client.calls}
    assert invoked == set(_EXPECTED_SIX)


# ---------------------------------------------------------------------------
# Static source check — no direct HTTP transport imported (R11.1)
# ---------------------------------------------------------------------------


def _imported_module_names(source: str) -> set[str]:
    """Collect every top-level module name imported by ``source`` via AST."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
                names.add(node.module.split(".")[0])
    return names


def test_module_imports_no_http_transport_library():
    with open(os.path.abspath(_MODULE_PATH), "r", encoding="utf-8") as fh:
        source = fh.read()

    imported = _imported_module_names(source)
    leaked = imported & _FORBIDDEN_HTTP_MODULES
    assert not leaked, (
        f"OMSMcpClient must reach OMS Core only via the injected gateway/MCP "
        f"client, but it imports HTTP transport module(s): {sorted(leaked)}"
    )
