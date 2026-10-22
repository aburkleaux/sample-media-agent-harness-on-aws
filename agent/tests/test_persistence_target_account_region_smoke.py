"""Smoke test for the OMS Core target account / region (task 14.3).

Asserts the configured OMS gateway target resolves to OMS Core in account
``100000000006`` / region ``us-east-1`` (R11.4). The client exposes these as the
module-level target constants and carries them on each instance (used when a
default SSM client must be built to resolve the endpoint). This test asserts the
declared target statically — it does **not** hit AWS.

Requirements: 11.4
"""

from __future__ import annotations

import os
import sys

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from persistence import oms_mcp_client as mod
from persistence.oms_mcp_client import OMS_MCP_TOOLS, OMSMcpClient

#: The OMS Core target the harness is pinned to (R11.4).
_EXPECTED_ACCOUNT_ID = "100000000006"
_EXPECTED_REGION = "us-east-1"


class _FakeGatewayConnection:
    def __init__(self) -> None:
        self.url = "https://oms.example/mcp"
        self._client = None
        self._tools = [{"name": f"oms-core___{b}"} for b in OMS_MCP_TOOLS]

    def list_tools(self):
        return list(self._tools)


# ---------------------------------------------------------------------------
# The configured target constants (R11.4)
# ---------------------------------------------------------------------------


def test_module_target_constants_pin_oms_core_account_and_region():
    assert mod._OMS_ACCOUNT_ID == _EXPECTED_ACCOUNT_ID
    assert mod._OMS_REGION == _EXPECTED_REGION


def test_client_default_target_resolves_to_account_and_region():
    # A default-constructed client (no AWS calls) carries the pinned target.
    client = OMSMcpClient(_FakeGatewayConnection())
    assert client._account_id == _EXPECTED_ACCOUNT_ID
    assert client._region == _EXPECTED_REGION


def test_from_manager_preserves_the_pinned_region():
    class _FakeManager:
        def __init__(self, conn) -> None:
            self._conn = conn

        def get_gateway(self, gw_id):
            return self._conn

    client = OMSMcpClient.from_manager(_FakeManager(_FakeGatewayConnection()))
    # from_manager defaults the region to the pinned OMS Core region.
    assert client._region == _EXPECTED_REGION
    assert client._account_id == _EXPECTED_ACCOUNT_ID
