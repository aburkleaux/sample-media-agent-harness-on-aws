"""Unit tests for the OMS MCP client adapter (task 6.2).

Covers, against a fake ``GatewayConnection`` that records every call:
- exactly the six OMS MCP tools are exposed and invocable (R11.1)
- the JSON content-block decode path (mirrors ``GatewayConnection.search_tools``)
- namespaced-tool-name resolution (``<target>___<bare>``)
- no direct HTTP transport to OMS — every call rides ``MCPClient.call_tool_sync``
- transport / protocol failures wrap as ``OMSUnavailableError``
- the endpoint is resolved dynamically (gateway config, else SSM), never hardcoded

Requirements: 11.1, 11.2, 11.3, 11.4
"""

from __future__ import annotations

import json
import os
import sys

import pytest

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from persistence.models import OMSUnavailableError
from persistence.oms_mcp_client import OMS_MCP_TOOLS, OMSMcpClient


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Block:
    """A minimal MCP content block carrying a JSON text payload."""

    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    """A minimal MCP tool result mirroring the real ``content`` shape."""

    def __init__(self, payload) -> None:
        self.content = [_Block(json.dumps(payload))]


class _FakeMCPClient:
    """Records every ``call_tool_sync`` and returns scripted payloads.

    Mirrors the real strands ``MCPClient.call_tool_sync`` signature —
    ``(tool_use_id, name, arguments)`` — so the adapter's call binding is
    exercised exactly as it is at runtime. ``calls`` records ``(name, args)``
    (the OMS tool name + arguments); ``tool_use_ids`` records the synthetic id
    the adapter must supply as the first positional arg.
    """

    def __init__(self, responses: dict) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []
        self.tool_use_ids: list[str] = []

    def call_tool_sync(self, tool_use_id: str, name: str, arguments: dict = None):
        self.tool_use_ids.append(tool_use_id)
        self.calls.append((name, arguments))
        # Resolve by bare suffix so scripting can use bare names.
        bare = name.split("___")[-1]
        payload = self._responses[bare]
        if isinstance(payload, Exception):
            raise payload
        return _Result(payload)


class _FakeGatewayConnection:
    """A stand-in for ``GatewayConnection`` — no real transport, records calls.

    Exposes only what the adapter touches: ``url`` (target config), ``list_tools``
    (namespace resolution), and ``_client`` (the MCPClient seam).
    """

    def __init__(self, responses: dict, *, url: str = "https://oms.example/mcp",
                 namespaced: bool = True) -> None:
        self.url = url
        self._client = _FakeMCPClient(responses)
        target = "oms-core"
        self._tools = [
            {"name": f"{target}___{bare}" if namespaced else bare,
             "description": "", "state": "available"}
            for bare in OMS_MCP_TOOLS
        ]

    def list_tools(self) -> list[dict]:
        return list(self._tools)


def _responses() -> dict:
    return {
        "list_integrations": [{"integration_id": "audio"}],
        "get_integration": {"integration_id": "audio", "actions": ["stem_separation"]},
        "get_action_schema": {
            "service": "audio",
            "action": "stem_separation",
            "tool_name": "audio-stem___separate_audio_stems",
        },
        "validate_template": {"valid": True, "findings": []},
        "save_template": {"template_id": "tpl-1", "operation": "created"},
        "activate_template": {"activated": "tpl-1"},
    }


# ---------------------------------------------------------------------------
# Exactly six tools exposed (R11.1)
# ---------------------------------------------------------------------------


def test_exactly_six_oms_tools_exposed():
    assert OMS_MCP_TOOLS == (
        "list_integrations",
        "get_integration",
        "get_action_schema",
        "validate_template",
        "save_template",
        "activate_template",
    )
    # The adapter exposes a public method for each of the six tools, and no
    # other public OMS-tool method.
    public = {
        name
        for name in dir(OMSMcpClient)
        if not name.startswith("_")
        and callable(getattr(OMSMcpClient, name))
        and name not in {"from_manager", "resolve_endpoint"}
    }
    assert public == set(OMS_MCP_TOOLS)


# ---------------------------------------------------------------------------
# JSON decode path + call routing for each tool (R11.1)
# ---------------------------------------------------------------------------


def test_list_integrations_decodes_json_list():
    conn = _FakeGatewayConnection(_responses())
    client = OMSMcpClient(conn)
    result = client.list_integrations()
    assert result == [{"integration_id": "audio"}]
    assert conn._client.calls[0] == ("oms-core___list_integrations", {})


def test_get_integration_passes_args_and_decodes_dict():
    conn = _FakeGatewayConnection(_responses())
    client = OMSMcpClient(conn)
    result = client.get_integration("audio")
    assert result["integration_id"] == "audio"
    assert conn._client.calls[0] == (
        "oms-core___get_integration",
        {"integration_id": "audio"},
    )


def test_get_action_schema_returns_published_tool_name():
    conn = _FakeGatewayConnection(_responses())
    client = OMSMcpClient(conn)
    result = client.get_action_schema("audio", "stem_separation")
    assert result["tool_name"] == "audio-stem___separate_audio_stems"
    assert conn._client.calls[0] == (
        "oms-core___get_action_schema",
        {"integration_id": "audio", "action": "stem_separation"},
    )


def test_validate_template_call():
    conn = _FakeGatewayConnection(_responses())
    client = OMSMcpClient(conn)
    result = client.validate_template({"template_id": "t"})
    assert result == {"valid": True, "findings": []}
    name, args = conn._client.calls[0]
    assert name == "oms-core___validate_template"
    assert args == {"template": {"template_id": "t"}}


def test_save_template_passes_update_flag():
    conn = _FakeGatewayConnection(_responses())
    client = OMSMcpClient(conn)
    result = client.save_template({"template_id": "t"}, update=True)
    assert result["template_id"] == "tpl-1"
    name, args = conn._client.calls[0]
    assert name == "oms-core___save_template"
    assert args == {"template": {"template_id": "t"}, "update": True}


def test_activate_template_call():
    conn = _FakeGatewayConnection(_responses())
    client = OMSMcpClient(conn)
    result = client.activate_template("tpl-1")
    assert result == {"activated": "tpl-1"}
    assert conn._client.calls[0] == (
        "oms-core___activate_template",
        {"template_id": "tpl-1"},
    )


# ---------------------------------------------------------------------------
# No direct HTTP — every call rides call_tool_sync (R11.1)
# ---------------------------------------------------------------------------


def test_all_calls_route_through_mcp_call_tool_sync():
    conn = _FakeGatewayConnection(_responses())
    client = OMSMcpClient(conn)
    client.list_integrations()
    client.get_integration("audio")
    client.get_action_schema("audio", "stem_separation")
    client.validate_template({"template_id": "t"})
    client.save_template({"template_id": "t"}, update=False)
    client.activate_template("tpl-1")
    # Six invocations, all through the MCPClient seam — no other transport.
    assert len(conn._client.calls) == 6
    assert {name.split("___")[-1] for name, _ in conn._client.calls} == set(OMS_MCP_TOOLS)


def test_bare_tool_name_resolution_when_not_namespaced():
    conn = _FakeGatewayConnection(_responses(), namespaced=False)
    client = OMSMcpClient(conn)
    client.list_integrations()
    assert conn._client.calls[0][0] == "list_integrations"


# ---------------------------------------------------------------------------
# Transport / protocol failures -> OMSUnavailableError
# ---------------------------------------------------------------------------


def test_transport_failure_wraps_as_oms_unavailable():
    responses = _responses()
    responses["save_template"] = RuntimeError("gateway down")
    conn = _FakeGatewayConnection(responses)
    client = OMSMcpClient(conn)
    with pytest.raises(OMSUnavailableError) as exc:
        client.save_template({"template_id": "t"}, update=False)
    assert exc.value.failing_stage == "save"


def test_missing_mcp_client_wraps_as_oms_unavailable():
    conn = _FakeGatewayConnection(_responses())
    conn._client = None
    client = OMSMcpClient(conn)
    with pytest.raises(OMSUnavailableError):
        client.list_integrations()


def test_undecodable_content_wraps_as_oms_unavailable():
    class _BadResult:
        def __init__(self):
            self.content = [_Block("not-json{{")]

    class _BadClient:
        def call_tool_sync(self, tool_use_id, name, arguments=None):
            return _BadResult()

    conn = _FakeGatewayConnection(_responses())
    conn._client = _BadClient()
    client = OMSMcpClient(conn)
    with pytest.raises(OMSUnavailableError):
        client.list_integrations()


def test_no_content_block_wraps_as_oms_unavailable():
    class _EmptyResult:
        content = []

    class _EmptyClient:
        def call_tool_sync(self, tool_use_id, name, arguments=None):
            return _EmptyResult()

    conn = _FakeGatewayConnection(_responses())
    conn._client = _EmptyClient()
    client = OMSMcpClient(conn)
    with pytest.raises(OMSUnavailableError):
        client.get_integration("audio")


# ---------------------------------------------------------------------------
# Decode across every shape call_tool_sync can return (the client-side bug)
# ---------------------------------------------------------------------------


def _client_returning(result):
    """Build an OMSMcpClient whose MCP client returns ``result`` verbatim."""

    class _Client:
        def __init__(self):
            self.tool_use_ids = []
            self.calls = []

        def call_tool_sync(self, tool_use_id, name, arguments=None):
            self.tool_use_ids.append(tool_use_id)
            self.calls.append((name, arguments))
            return result

    conn = _FakeGatewayConnection(_responses())
    conn._client = _Client()
    return OMSMcpClient(conn), conn


def test_decode_strands_toolresult_dict_with_json_block():
    # (i) strands ToolResult dict carrying a {"json": <obj>} content block.
    result = {
        "status": "success",
        "toolUseId": "oms-list_integrations",
        "content": [{"json": [{"integration_id": "audio"}]}],
    }
    client, _ = _client_returning(result)
    assert client.list_integrations() == [{"integration_id": "audio"}]


def test_decode_strands_toolresult_dict_with_text_block():
    # (ii) strands ToolResult dict carrying a {"text": "<json>"} content block.
    result = {
        "status": "success",
        "toolUseId": "oms-get_integration",
        "content": [{"text": json.dumps({"integration_id": "audio"})}],
    }
    client, _ = _client_returning(result)
    assert client.get_integration("audio") == {"integration_id": "audio"}


def test_decode_strands_toolresult_dict_falls_back_to_structured_content():
    # A dict with no json/text block but populated structuredContent.
    result = {
        "status": "success",
        "toolUseId": "oms-get_integration",
        "content": [],
        "structuredContent": {"integration_id": "audio", "actions": []},
    }
    client, _ = _client_returning(result)
    assert client.get_integration("audio")["integration_id"] == "audio"


def test_decode_raw_calltoolresult_object_with_content_and_structured():
    # (iii) raw CallToolResult-like object: .content TextContent + .structuredContent.
    class _RawResult:
        def __init__(self):
            self.content = [_Block(json.dumps([{"integration_id": "audio"}]))]
            self.structuredContent = {"ignored": True}
            self.isError = False

    client, _ = _client_returning(_RawResult())
    assert client.list_integrations() == [{"integration_id": "audio"}]


def test_decode_raw_calltoolresult_object_prefers_structured_when_no_text():
    class _RawResult:
        content = []
        structuredContent = {"integration_id": "audio", "actions": []}

    client, _ = _client_returning(_RawResult())
    assert client.get_integration("audio")["integration_id"] == "audio"


def test_decode_strands_toolresult_error_status_raises():
    # (iv) an error-status ToolResult still raises OMSUnavailableError.
    result = {
        "status": "error",
        "toolUseId": "oms-save_template",
        "content": [{"text": "boom: template rejected"}],
    }

    class _Client:
        def call_tool_sync(self, tool_use_id, name, arguments=None):
            return result

    conn = _FakeGatewayConnection(_responses())
    conn._client = _Client()
    client = OMSMcpClient(conn)
    with pytest.raises(OMSUnavailableError) as exc:
        client.save_template({"template_id": "t"}, update=False)
    assert exc.value.failing_stage == "save"


def test_decode_strands_toolresult_empty_dict_raises():
    # (iv) success status but no content and no structuredContent -> raise.
    result = {"status": "success", "toolUseId": "oms-list_integrations", "content": []}
    client, _ = _client_returning(result)
    with pytest.raises(OMSUnavailableError):
        client.list_integrations()


def test_call_passes_synthetic_tool_use_id_first():
    # The adapter must supply tool_use_id as the REQUIRED first positional arg.
    conn = _FakeGatewayConnection(_responses())
    client = OMSMcpClient(conn)
    client.list_integrations()
    assert conn._client.tool_use_ids[0] == "oms-list_integrations"


# ---------------------------------------------------------------------------
# Endpoint resolution — dynamic, never hardcoded (R11.2, R11.3)
# ---------------------------------------------------------------------------


def test_endpoint_resolves_from_gateway_config():
    conn = _FakeGatewayConnection(_responses(), url="https://oms.internal/mcp")
    client = OMSMcpClient(conn)
    assert client.resolve_endpoint() == "https://oms.internal/mcp"


def test_endpoint_falls_back_to_ssm():
    class _FakeSSM:
        def __init__(self):
            self.calls = []

        def get_parameter(self, Name):  # noqa: N803 — boto3 kwarg name
            self.calls.append(Name)
            return {"Parameter": {"Value": "https://oms.from-ssm/api"}}

    ssm = _FakeSSM()
    conn = _FakeGatewayConnection(_responses(), url="")
    client = OMSMcpClient(conn, ssm_client=ssm)
    assert client.resolve_endpoint() == "https://oms.from-ssm/api"
    assert ssm.calls == ["/oms/core/api-url"]


def test_endpoint_resolution_failure_wraps_as_oms_unavailable():
    class _BoomSSM:
        def get_parameter(self, Name):  # noqa: N803
            raise RuntimeError("ssm denied")

    conn = _FakeGatewayConnection(_responses(), url="")
    client = OMSMcpClient(conn, ssm_client=_BoomSSM())
    with pytest.raises(OMSUnavailableError):
        client.resolve_endpoint()


# ---------------------------------------------------------------------------
# from_manager wiring (R11.1)
# ---------------------------------------------------------------------------


def test_from_manager_resolves_registered_gateway():
    conn = _FakeGatewayConnection(_responses())

    class _FakeManager:
        def get_gateway(self, gw_id):
            self.requested = gw_id
            return conn if gw_id == "oms-core" else None

    manager = _FakeManager()
    client = OMSMcpClient.from_manager(manager, gateway_id="oms-core")
    assert client.list_integrations() == [{"integration_id": "audio"}]


def test_from_manager_raises_when_gateway_absent():
    class _FakeManager:
        def get_gateway(self, gw_id):
            return None

    with pytest.raises(OMSUnavailableError):
        OMSMcpClient.from_manager(_FakeManager(), gateway_id="missing")
