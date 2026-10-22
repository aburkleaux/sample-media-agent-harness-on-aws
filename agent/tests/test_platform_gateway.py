"""Wave 4: the runtime connects the shared Platform gateway (AWS_IAM inbound) via per-request
SigV4 using its own IAM identity, plane=platform, additively from env — it is NOT a registry row.
Inert until a shared target is attached; must never disturb the tenant (CUSTOM_JWT) path.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_manager import MCPManager  # noqa: E402
from gateway.registry import GatewayDescriptor, SigV4AuthConfig  # noqa: E402

PLATFORM_ID = "megs-platform-gw-abc123def0"
PLATFORM_URL = "https://megs-platform-gw-abc123def0.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"


def test_no_platform_gateway_when_env_unset(monkeypatch):
    monkeypatch.delenv("HARNESS_PLATFORM_GATEWAY_ID", raising=False)
    monkeypatch.delenv("HARNESS_PLATFORM_GATEWAY_URL", raising=False)
    mgr = MCPManager()
    gid = asyncio.run(mgr.load_platform_gateway_from_env())
    assert gid is None
    assert mgr._gateways == {}  # nothing connected in unprovisioned envs


def test_no_platform_gateway_when_only_id_set(monkeypatch):
    # Half-configured env must not attempt a connection with a missing URL.
    monkeypatch.setenv("HARNESS_PLATFORM_GATEWAY_ID", PLATFORM_ID)
    monkeypatch.delenv("HARNESS_PLATFORM_GATEWAY_URL", raising=False)
    mgr = MCPManager()
    assert asyncio.run(mgr.load_platform_gateway_from_env()) is None
    assert mgr._gateways == {}


def test_loads_platform_gateway_sigv4_from_env(monkeypatch):
    monkeypatch.setenv("HARNESS_PLATFORM_GATEWAY_ID", PLATFORM_ID)
    monkeypatch.setenv("HARNESS_PLATFORM_GATEWAY_URL", PLATFORM_URL)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    mgr = MCPManager()
    gid = asyncio.run(mgr.load_platform_gateway_from_env())
    assert gid == PLATFORM_ID
    conn = mgr._gateways[PLATFORM_ID]
    assert conn.auth_type == "sigv4"                 # runtime IAM identity, not JWT
    assert conn.sigv4_region == "us-west-2"
    assert conn.sigv4_service == "bedrock-agentcore"
    assert conn.url == PLATFORM_URL


def test_platform_load_does_not_disturb_existing_tenant_connections(monkeypatch):
    # A pre-existing tenant connection must survive the additive platform load.
    monkeypatch.setenv("HARNESS_PLATFORM_GATEWAY_ID", PLATFORM_ID)
    monkeypatch.setenv("HARNESS_PLATFORM_GATEWAY_URL", PLATFORM_URL)
    mgr = MCPManager()
    from mcp_manager import GatewayConnection
    mgr._gateways["tenant-gw"] = GatewayConnection("tenant-gw", {"url": "https://tenant/mcp", "auth_type": "jwt"})
    asyncio.run(mgr.load_platform_gateway_from_env())
    assert "tenant-gw" in mgr._gateways           # tenant untouched
    assert PLATFORM_ID in mgr._gateways            # platform added alongside
    assert mgr._gateways["tenant-gw"].auth_type == "jwt"


def test_platform_descriptor_shape():
    d = GatewayDescriptor(
        id="g", name="platform-gateway", url="https://x/mcp", auth_type="sigv4",
        auth_config=SigV4AuthConfig(region="us-west-2", service="bedrock-agentcore"),
        plane="platform",
    )
    assert d.plane == "platform"
    cfg = d.to_connection_config()
    assert cfg["auth_type"] == "sigv4"
    assert cfg["region"] == "us-west-2"
    assert cfg["service"] == "bedrock-agentcore"
