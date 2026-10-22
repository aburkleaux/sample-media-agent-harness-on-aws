"""A tokenless user_jwt gateway is deferred, not reported as a failure.

A `user_jwt` gateway is only connectable on invocations that carry the caller's
token. Management calls do not: opening the MCP panel or fetching the tool
catalog reaches the runtime without one. Attempting anyway raises, and
connect_all logged that at ERROR -- 20 times an hour in production, for a
condition that is expected.

Distinct from the token-latch bug, which was that a tokenless cold start left the
gateway permanently failed for the container's life. That is fixed separately by
bind_user_token_and_connect, and these tests assert the retry path still works.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcp_manager import GatewayConnection, MCPManager


def _gw(auth_type="user_jwt", token=None):
    c = GatewayConnection("h", {"name": "Your MCP servers",
                                "url": "https://gw.example/mcp",
                                "auth_type": auth_type})
    if token:
        c.user_token = token
    return c


def test_awaiting_user_token_only_for_tokenless_user_jwt():
    assert _gw().awaiting_user_token() is True
    assert _gw(token="eyJ.a.b").awaiting_user_token() is False
    # other auth types are never deferred -- they carry their own credentials
    for at in ("jwt", "sigv4", "none", "bearer"):
        assert _gw(auth_type=at).awaiting_user_token() is False, at


def test_connect_all_defers_instead_of_erroring():
    m = MCPManager.__new__(MCPManager)
    m._gateways = {"h": _gw()}
    result = m.connect_all()
    assert result["h"]["status"] == "deferred"
    assert "token" in result["h"]["reason"]


def test_deferral_does_not_stop_other_gateways(monkeypatch):
    """A deferred gateway must not short-circuit the loop."""
    m = MCPManager.__new__(MCPManager)
    ok = _gw(auth_type="none")
    monkeypatch.setattr(ok, "connect", lambda: {"status": "connected", "tools": []})
    monkeypatch.setattr(m, "_apply_job_tracking_wrap", lambda gw: None, raising=False)
    m._gateways = {"deferred": _gw(), "fine": ok}
    result = m.connect_all()
    assert result["deferred"]["status"] == "deferred"
    assert result["fine"]["status"] == "connected"
