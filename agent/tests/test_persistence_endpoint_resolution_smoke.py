"""Smoke test for dynamic OMS endpoint resolution (task 14.2).

Asserts the OMS Core endpoint is sourced dynamically — from the gateway target
config (``GatewayConnection.url``) or, failing that, from SSM ``/oms/core/api-url``
— and is *never* a literal hardcoded in ``oms_mcp_client.py``:

- functionally, ``resolve_endpoint`` prefers the gateway target config, and falls
  back to the SSM parameter ``/oms/core/api-url`` when the target carries no URL
  (R11.2, R11.3).
- statically, the module contains no literal OMS endpoint URL (no ``https://`` /
  ``http://`` literal, no ``execute-api`` / ``.amazonaws.com`` host), and the SSM
  parameter name ``/oms/core/api-url`` is the declared resolution source.

Requirements: 11.2, 11.3
"""

from __future__ import annotations

import os
import re
import sys

import pytest

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from persistence.models import OMSUnavailableError
from persistence.oms_mcp_client import OMS_MCP_TOOLS, OMSMcpClient

_MODULE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "persistence", "oms_mcp_client.py")
)

#: The SSM parameter that must be the dynamic endpoint fallback (R11.3).
_SSM_PARAM = "/oms/core/api-url"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeGatewayConnection:
    def __init__(self, *, url) -> None:
        self.url = url
        self._client = None
        self._tools = [{"name": f"oms-core___{b}"} for b in OMS_MCP_TOOLS]

    def list_tools(self):
        return list(self._tools)


class _RecordingSSM:
    """Records ``get_parameter`` names and returns a scripted value."""

    def __init__(self, value: str) -> None:
        self._value = value
        self.names: list[str] = []

    def get_parameter(self, Name):  # noqa: N803 — boto3 kwarg name
        self.names.append(Name)
        return {"Parameter": {"Value": self._value}}


# ---------------------------------------------------------------------------
# Functional: endpoint sourced from gateway config, else SSM (R11.2, R11.3)
# ---------------------------------------------------------------------------


def test_endpoint_prefers_gateway_target_config():
    conn = _FakeGatewayConnection(url="https://oms.internal/mcp")
    # SSM must not be consulted when the target config already carries a URL.
    ssm = _RecordingSSM("https://should-not-be-used/api")
    client = OMSMcpClient(conn, ssm_client=ssm)

    assert client.resolve_endpoint() == "https://oms.internal/mcp"
    assert ssm.names == []


def test_endpoint_falls_back_to_ssm_oms_core_api_url():
    conn = _FakeGatewayConnection(url="")  # no URL on the target config
    ssm = _RecordingSSM("https://oms.from-ssm/api")
    client = OMSMcpClient(conn, ssm_client=ssm)

    assert client.resolve_endpoint() == "https://oms.from-ssm/api"
    # The dynamic fallback is exactly the /oms/core/api-url SSM parameter.
    assert ssm.names == [_SSM_PARAM]


def test_endpoint_resolution_failure_wraps_as_oms_unavailable():
    class _BoomSSM:
        def get_parameter(self, Name):  # noqa: N803
            raise RuntimeError("ssm denied")

    conn = _FakeGatewayConnection(url="")
    client = OMSMcpClient(conn, ssm_client=_BoomSSM())
    with pytest.raises(OMSUnavailableError):
        client.resolve_endpoint()


# ---------------------------------------------------------------------------
# Static: no literal OMS endpoint hardcoded; SSM param is the source
# ---------------------------------------------------------------------------


def _source() -> str:
    with open(_MODULE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def test_no_hardcoded_oms_endpoint_url_literal():
    source = _source()
    # No scheme-qualified URL literal anywhere in the module.
    assert not re.search(r"https?://", source), (
        "oms_mcp_client.py must not hardcode any endpoint URL literal"
    )
    # No API-Gateway / AWS host literal that would pin an OMS endpoint.
    assert "execute-api" not in source
    assert ".amazonaws.com" not in source


def test_ssm_parameter_name_is_the_resolution_source():
    source = _source()
    # The SSM parameter name is present as the declared dynamic endpoint source.
    assert _SSM_PARAM in source
    # And it is what the client actually queries by default.
    conn = _FakeGatewayConnection(url="")
    ssm = _RecordingSSM("https://oms.from-ssm/api")
    OMSMcpClient(conn, ssm_client=ssm).resolve_endpoint()
    assert ssm.names == [_SSM_PARAM]
