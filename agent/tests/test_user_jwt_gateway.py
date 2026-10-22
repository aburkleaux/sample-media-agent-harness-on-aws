"""Tests for forwarding the end user's token to a user-pool gateway.

The harness gateway's authorizer is the end-user Cognito pool, so the runtime
must present the CALLER's own token rather than a shared machine credential.
That is what lets the gateway validate a real identity and its interceptor
scope tools to a verified claim instead of an asserted header.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.registry import (  # noqa: E402
    _AUTH_CONFIG_CLASSES,
    GatewayDescriptor,
    UserJwtAuthConfig,
    _auth_config_from_dict,
)
from mcp_manager import GatewayConnection, MCPManager  # noqa: E402

TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.sig"


# ---------------------------------------------------------------------------
# Registry variant
# ---------------------------------------------------------------------------


def test_variant_carries_no_credential_reference():
    """Unlike every other variant there is not even an ARN to store."""
    assert UserJwtAuthConfig().to_dict() == {"auth_type": "user_jwt"}
    assert UserJwtAuthConfig().public_dict() == {"auth_type": "user_jwt"}


def test_dispatch_resolves_user_jwt():
    assert isinstance(_auth_config_from_dict("user_jwt", {}), UserJwtAuthConfig)


def test_variant_registered_for_isinstance_checks():
    """Guards the bug where an inline tuple silently excluded a new variant."""
    assert UserJwtAuthConfig in _AUTH_CONFIG_CLASSES
    assert isinstance(UserJwtAuthConfig(), _AUTH_CONFIG_CLASSES)


def _descriptor() -> GatewayDescriptor:
    return GatewayDescriptor(
        id="harness", name="Harness", url="https://gw.example.com/mcp",
        auth_type="user_jwt", auth_config=UserJwtAuthConfig(),
    )


def test_descriptor_round_trips():
    again = GatewayDescriptor.from_dict(_descriptor().to_dict())
    assert isinstance(again.auth_config, UserJwtAuthConfig)
    assert again.auth_type == "user_jwt"


def test_connection_config_has_no_token():
    """The token comes per invocation, never from the persisted descriptor."""
    cfg = _descriptor().to_connection_config()
    assert cfg["auth_type"] == "user_jwt"
    assert "user_token" not in cfg
    assert "token_arn" not in cfg


# ---------------------------------------------------------------------------
# Connect-time header
# ---------------------------------------------------------------------------


def _captured_headers(monkeypatch, conn: GatewayConnection) -> dict:
    seen: dict = {}

    import mcp_manager as mm

    class _FakeClient:
        def __init__(self, transport_callable=None, *, url=None, headers=None, **_kw):
            seen["headers"] = dict(headers or {})

        def __enter__(self):
            raise RuntimeError("stop-before-connect")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mm, "MCPClient", _FakeClient)
    with pytest.raises(RuntimeError):
        conn._do_connect()
    return seen.get("headers", {})


def test_user_token_becomes_the_authorization_header(monkeypatch):
    conn = GatewayConnection(
        "harness",
        {"url": "https://gw/mcp", "auth_type": "user_jwt", "user_token": TOKEN},
    )
    assert _captured_headers(monkeypatch, conn)["Authorization"] == f"Bearer {TOKEN}"


def test_connect_fails_loudly_without_a_forwarded_token():
    conn = GatewayConnection(
        "harness", {"url": "https://gw/mcp", "auth_type": "user_jwt"}
    )
    with pytest.raises(RuntimeError, match="no user token was forwarded"):
        conn._do_connect()


def test_m2m_gateways_are_unaffected(monkeypatch):
    """The media gateway keeps its own machine credential."""
    conn = GatewayConnection(
        "media", {"url": "https://gw/mcp", "auth_type": "jwt"}
    )
    monkeypatch.setattr(GatewayConnection, "_get_oauth_token", lambda self: "m2m")
    assert _captured_headers(monkeypatch, conn)["Authorization"] == "Bearer m2m"


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_builder_threads_the_token():
    conn = await MCPManager()._build_connection_for_descriptor(
        _descriptor(), user_token=TOKEN
    )
    assert conn is not None and conn.user_token == TOKEN


@pytest.mark.asyncio
async def test_loader_threads_the_token():
    class _Registry:
        async def list_enabled(self):
            return [_descriptor()]

    mgr = MCPManager()
    await mgr.load_gateways_from_registry(_Registry(), user_token=TOKEN)
    assert mgr._gateways
    for conn in mgr._gateways.values():
        assert conn.user_token == TOKEN


# ---------------------------------------------------------------------------
# Header reader
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, headers):
        self.request_headers = headers


def _read(headers) -> str:
    import main
    return main._resolve_user_token(_Ctx(headers))


def test_reads_the_allowlisted_header():
    import main
    assert _read({main.USER_TOKEN_HEADER: TOKEN}) == TOKEN


def test_header_lookup_is_case_insensitive():
    import main
    assert _read({main.USER_TOKEN_HEADER.lower(): TOKEN}) == TOKEN


def test_strips_a_bearer_prefix():
    import main
    assert _read({main.USER_TOKEN_HEADER: f"Bearer {TOKEN}"}) == TOKEN


def test_absent_header_is_not_an_error():
    """Only user_jwt gateways need it, and those fail loudly at connect."""
    assert _read({}) == ""
    assert _read(None) == ""
    assert _read({"X-Other": "x"}) == ""


def test_missing_request_headers_attribute_is_tolerated():
    import main

    class _Bare:
        pass

    assert main._resolve_user_token(_Bare()) == ""


def test_header_name_matches_the_cdk_allowlist():
    """A mismatch here is dropped silently and presents as auth failure."""
    import main
    assert main.USER_TOKEN_HEADER == (
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-UserToken"
    )
