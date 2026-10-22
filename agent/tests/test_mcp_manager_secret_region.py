"""Region-aware Secrets Manager client resolution tests.

Federated gateways can carry a `client_secret_arn` in a different region than
the runtime's default (e.g. the OMS Core M2M secret lives in us-east-1 while the
harness runs in us-west-2). `MCPManager._build_connection_for_descriptor` must
derive the region from the secret ARN and build a region-pinned
`secretsmanager` client when none is injected, while still honoring an
explicitly injected client (test/DI path).
"""
from __future__ import annotations

import asyncio
import os
import sys

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import mcp_manager as m
from gateway.registry import GatewayDescriptor, JwtAuthConfig


US_EAST_1_ARN = (
    "arn:aws:secretsmanager:us-east-1:100000000006:secret:"
    "oms/core/mcp/m2m-client-secret-xDMuF2"
)


def _run(coro):
    """Run an async coroutine to completion in a sync test."""
    return asyncio.run(coro)


def _oms_descriptor(secret_arn: str = US_EAST_1_ARN) -> GatewayDescriptor:
    """A JWT descriptor whose secret ARN points at a us-east-1 secret."""
    return GatewayDescriptor(
        id="oms-core",
        name="OMS Core (hosted MCP endpoint)",
        url="https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/x/invocations?qualifier=DEFAULT",
        auth_type="jwt",
        auth_config=JwtAuthConfig(
            token_endpoint="https://idp/oauth2/token",
            client_id="client-abc",
            client_secret_arn=secret_arn,
            scope="oms-core/invoke",
        ),
    )


class _FakeSecrets:
    """Minimal Secrets Manager client returning a bare secret string."""

    def get_secret_value(self, SecretId):  # noqa: N803 (boto3 kwarg name)
        return {"SecretString": "the-secret"}


def test_derives_region_from_arn_when_no_injected_client(monkeypatch):
    """No injected client → boto3.client called with region_name from the ARN."""
    calls = []

    def fake_boto_client(service, **kwargs):
        calls.append((service, kwargs))
        return _FakeSecrets()

    monkeypatch.setattr(m.boto3, "client", fake_boto_client)

    mgr = m.MCPManager()
    conn = _run(mgr._build_connection_for_descriptor(_oms_descriptor()))

    assert conn is not None
    assert calls == [("secretsmanager", {"region_name": "us-east-1"})]


def test_injected_client_is_used_verbatim(monkeypatch):
    """An injected secrets_client wins — boto3.client must not be called."""
    def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("boto3.client should not be called when a client is injected")

    monkeypatch.setattr(m.boto3, "client", boom)

    mgr = m.MCPManager()
    conn = _run(
        mgr._build_connection_for_descriptor(
            _oms_descriptor(), secrets_client=_FakeSecrets()
        )
    )

    assert conn is not None


def test_malformed_arn_falls_back_to_default_client(monkeypatch):
    """A malformed ARN (no region segment) → default client, no region_name."""
    calls = []

    def fake_boto_client(service, **kwargs):
        calls.append((service, kwargs))
        return _FakeSecrets()

    monkeypatch.setattr(m.boto3, "client", fake_boto_client)

    mgr = m.MCPManager()
    conn = _run(
        mgr._build_connection_for_descriptor(_oms_descriptor(secret_arn="not-an-arn"))
    )

    assert conn is not None
    assert calls == [("secretsmanager", {})]


def test_load_from_registry_passes_none_through_for_region_derivation(monkeypatch):
    """load_gateways_from_registry must NOT build a shared default client; each
    descriptor derives its own region-pinned client (region from the ARN)."""
    calls = []

    def fake_boto_client(service, **kwargs):
        calls.append((service, kwargs))
        return _FakeSecrets()

    monkeypatch.setattr(m.boto3, "client", fake_boto_client)

    descriptor = _oms_descriptor()

    class _Registry:
        async def list_enabled(self):
            return [descriptor]

    mgr = m.MCPManager()
    loaded = _run(mgr.load_gateways_from_registry(_Registry()))

    assert loaded == ["oms-core"]
    # The only secretsmanager client built is the region-pinned one derived
    # from the descriptor's ARN — no eager shared default-region client.
    assert calls == [("secretsmanager", {"region_name": "us-east-1"})]
