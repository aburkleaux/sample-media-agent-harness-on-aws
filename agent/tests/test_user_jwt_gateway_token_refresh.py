"""Tests for rotating static user_jwt gateway credentials on long sessions.

MCPClient copies Authorization headers at connect time. Updating only the
connection object's token field leaves the old bearer on the wire, so a fresh
browser token must rotate the live MCP transport before another tool call.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcp_manager import GatewayConnection, MCPManager, _is_auth_expiry  # noqa: E402


def _gw(gw_id: str, *, connected: bool, auth_type: str = "user_jwt", token: str = "old") -> GatewayConnection:
    gw = GatewayConnection(gw_id, {"name": gw_id, "url": "https://gw.example.com/mcp",
                                   "auth_type": auth_type, "user_token": token})
    gw._connected = connected
    gw._bound_user_token = token if connected and auth_type == "user_jwt" else ""
    return gw


# --------------------------- _is_auth_expiry ------------------------------

def test_is_auth_expiry_detects_403_and_401():
    assert _is_auth_expiry("Client error '403 Forbidden' for url 'https://gw/mcp'")
    assert _is_auth_expiry("MCPClientInitializationError: ... 401 Unauthorized")


def test_is_auth_expiry_ignores_non_auth_failures():
    assert not _is_auth_expiry("Connection refused")
    assert not _is_auth_expiry("TLS handshake failed")
    assert not _is_auth_expiry("500 Internal Server Error")
    assert not _is_auth_expiry("")
    assert not _is_auth_expiry(None)  # type: ignore[arg-type]


# ------------------- bind_user_token_and_connect refresh ------------------

def test_rotates_a_live_connection_when_the_user_token_changes(monkeypatch):
    mgr = MCPManager()
    gw = _gw("harness", connected=True, token="old-token")
    calls: list[str] = []
    monkeypatch.setattr(gw, "disconnect", lambda: (calls.append("disconnect"), True)[1])
    monkeypatch.setattr(
        gw,
        "connect",
        lambda: (calls.append(f"connect:{gw.user_token}"), {"status": "ok"})[1],
    )
    monkeypatch.setattr(mgr, "_apply_job_tracking_wrap", lambda g: None)
    mgr._gateways = {"harness": gw}

    mgr.bind_user_token_and_connect("fresh-token")

    assert gw.user_token == "fresh-token"
    assert calls == ["disconnect", "connect:fresh-token"]


def test_connects_an_unconnected_user_jwt_gateway_with_the_token(monkeypatch):
    mgr = MCPManager()
    gw = _gw("harness", connected=False, token="")
    calls: list = []
    monkeypatch.setattr(gw, "connect", lambda: (calls.append(True), {"status": "ok"})[1])
    monkeypatch.setattr(mgr, "_apply_job_tracking_wrap", lambda g: None)
    mgr._gateways = {"harness": gw}

    mgr.bind_user_token_and_connect("fresh-token")

    assert gw.user_token == "fresh-token"
    assert calls == [True]


def test_leaves_non_user_jwt_gateways_untouched(monkeypatch):
    mgr = MCPManager()
    gw = _gw("m2m", connected=True, auth_type="oauth", token="")
    def _boom():
        raise AssertionError("must not connect/refresh a non-user_jwt gateway")
    monkeypatch.setattr(gw, "connect", _boom)
    mgr._gateways = {"m2m": gw}

    mgr.bind_user_token_and_connect("fresh-token")

    assert gw.user_token == ""  # untouched


def test_same_bound_user_token_reuses_the_live_connection(monkeypatch):
    mgr = MCPManager()
    gw = _gw("harness", connected=True, token="current-token")
    monkeypatch.setattr(
        gw, "disconnect", lambda: (_ for _ in ()).throw(AssertionError("must not rotate"))
    )
    monkeypatch.setattr(
        gw, "connect", lambda: (_ for _ in ()).throw(AssertionError("must not reconnect"))
    )
    mgr._gateways = {"harness": gw}

    assert mgr.bind_user_token_and_connect("current-token") == {}
    assert gw.user_token == "current-token"
