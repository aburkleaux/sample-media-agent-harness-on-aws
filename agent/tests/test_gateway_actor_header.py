"""Tests for asserting the caller's actor to the gateway.

The actor is bound at connect time, not per request. That is correct because
AgentCore gives each session its own microVM and a session belongs to one
user, so a container serves one actor for its lifetime — and it is also
forced: Strands' MCPClient runs its transport on a background thread seeded
with a contextvars snapshot taken at ``__enter__``, so a contextvar set later
during a request never reaches the request headers.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.registry import (  # noqa: E402
    BearerAuthConfig,
    GatewayDescriptor,
    JwtAuthConfig,
    NoneAuthConfig,
)
from mcp_manager import ACTOR_HEADER, GatewayConnection, MCPManager  # noqa: E402

ACTOR = "USER#alice"


def _conn(**overrides) -> GatewayConnection:
    cfg = {"url": "https://gw.example.com/mcp", "auth_type": "none"}
    cfg.update(overrides)
    return GatewayConnection("gw", cfg)


def test_actor_header_present_when_actor_known():
    conn = _conn(actor_id=ACTOR)
    assert conn._actor_headers() == {ACTOR_HEADER: ACTOR}


def test_no_actor_header_when_actor_unknown():
    """Absent rather than empty — the interceptor fails closed on absence."""
    assert _conn()._actor_headers() == {}
    assert _conn(actor_id="")._actor_headers() == {}


def test_default_header_name():
    assert ACTOR_HEADER == "X-Actor-Id"


# ---------------------------------------------------------------------------
# Header assembly across every auth type
# ---------------------------------------------------------------------------


def _captured_headers(monkeypatch, conn: GatewayConnection) -> dict:
    """Run _do_connect far enough to capture the headers it would send.

    Construction uses Strands' public ``MCPClient(url=, headers=)`` interface
    (no ``streamablehttp_client`` factory), so capture the ``headers`` kwarg.
    """
    seen: dict = {}

    import mcp_manager as mm

    class _FakeClient:
        def __init__(self, transport_callable=None, *, url=None, headers=None, **_kw):
            seen["url"] = url
            seen["headers"] = dict(headers or {})

        def __enter__(self):
            raise RuntimeError("stop-before-connect")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mm, "MCPClient", _FakeClient)
    with pytest.raises(RuntimeError):
        conn._do_connect()
    return seen.get("headers", {})


def test_actor_header_sent_on_jwt_gateway(monkeypatch):
    conn = _conn(auth_type="jwt", actor_id=ACTOR)
    monkeypatch.setattr(GatewayConnection, "_get_oauth_token", lambda self: "tok")
    headers = _captured_headers(monkeypatch, conn)
    assert headers["Authorization"] == "Bearer tok"
    assert headers[ACTOR_HEADER] == ACTOR


def test_actor_header_sent_on_bearer_gateway(monkeypatch):
    conn = _conn(auth_type="bearer", bearer_token="pat", actor_id=ACTOR)
    headers = _captured_headers(monkeypatch, conn)
    assert headers["Authorization"] == "Bearer pat"
    assert headers[ACTOR_HEADER] == ACTOR


def test_actor_header_sent_on_unauthenticated_gateway(monkeypatch):
    """The 'none' branch used to send no headers at all."""
    conn = _conn(auth_type="none", actor_id=ACTOR)
    headers = _captured_headers(monkeypatch, conn)
    assert headers == {ACTOR_HEADER: ACTOR}


def test_no_headers_at_all_when_none_auth_and_no_actor(monkeypatch):
    conn = _conn(auth_type="none")
    assert _captured_headers(monkeypatch, conn) == {}


def test_bearer_still_fails_loudly_without_token(monkeypatch):
    conn = _conn(auth_type="bearer", actor_id=ACTOR)
    with pytest.raises(RuntimeError, match="no token was resolved"):
        conn._do_connect()


def test_api_key_style_header_preserved_alongside_actor(monkeypatch):
    conn = _conn(
        auth_type="bearer", bearer_token="k", header_name="X-API-Key",
        value_prefix="", actor_id=ACTOR,
    )
    headers = _captured_headers(monkeypatch, conn)
    assert headers["X-API-Key"] == "k"
    assert headers[ACTOR_HEADER] == ACTOR


# ---------------------------------------------------------------------------
# Plumbing from the descriptor loader
# ---------------------------------------------------------------------------


def _descriptor(auth_type="none", auth_config=None) -> GatewayDescriptor:
    return GatewayDescriptor(
        id="gw", name="GW", url="https://gw.example.com/mcp",
        auth_type=auth_type, auth_config=auth_config or NoneAuthConfig(),
    )


@pytest.mark.asyncio
async def test_builder_threads_actor_onto_connection():
    conn = await MCPManager()._build_connection_for_descriptor(
        _descriptor(), actor_id=ACTOR
    )
    assert conn is not None
    assert conn.actor_id == ACTOR


@pytest.mark.asyncio
async def test_builder_defaults_to_no_actor():
    conn = await MCPManager()._build_connection_for_descriptor(_descriptor())
    assert conn is not None
    assert conn.actor_id == ""


@pytest.mark.asyncio
async def test_actor_survives_secret_resolution():
    """The actor must not be lost on the branch that resolves an ARN."""
    class _SM:
        def get_secret_value(self, SecretId):  # noqa: N803
            return {"SecretString": "resolved"}

    desc = _descriptor(
        auth_type="bearer", auth_config=BearerAuthConfig(token_arn="arn:x")
    )
    conn = await MCPManager()._build_connection_for_descriptor(
        desc, secrets_client=_SM(), actor_id=ACTOR
    )
    assert conn is not None
    assert conn.bearer_token == "resolved"
    assert conn.actor_id == ACTOR


@pytest.mark.asyncio
async def test_registry_loader_passes_actor_to_every_connection():
    class _Registry:
        async def list_enabled(self):
            return [
                _descriptor(),
                GatewayDescriptor(
                    id="gw2", name="GW2", url="https://b/mcp", auth_type="jwt",
                    auth_config=JwtAuthConfig(token_endpoint="https://t", client_id="c"),
                ),
            ]

    mgr = MCPManager()
    await mgr.load_gateways_from_registry(_Registry(), actor_id=ACTOR)
    assert mgr._gateways, "no gateways registered"
    for gw_id, conn in mgr._gateways.items():
        assert conn.actor_id == ACTOR, gw_id


def test_gateway_descriptor_plane_defaults_to_tenant():
    """Req 13.8: plane is explicit; existing rows default to tenant, never inferred."""
    d = _descriptor()
    assert d.plane == "tenant"


def test_gateway_descriptor_accepts_platform_plane():
    d = GatewayDescriptor(
        id="plat", name="Platform", url="https://p/mcp",
        auth_type="none", auth_config=NoneAuthConfig(),
        plane="platform",
    )
    assert d.plane == "platform"
