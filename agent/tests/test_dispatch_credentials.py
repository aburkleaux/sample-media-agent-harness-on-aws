"""Dispatch credential rule + expired-token guards (prod session 45d2139c).

A dispatched sub-agent reused a warm connection bound to the user's browser token; the token
lapsed and three parallel fal calls got a front-door ``403 insufficient_scope``. The fix: every
dispatch carries a freshly minted delegation grant when possible, a user token is only a
still-valid fallback, and nothing ever binds a lapsed bearer.
"""
import asyncio
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from security import delegation  # noqa: E402
from orchestration import dispatch_credentials as dc  # noqa: E402
import mcp_manager as mm  # noqa: E402


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_grant_cache():
    # The dispatch grant cache is process-global; isolate every test from the others.
    from orchestration import dispatch_credentials as _dc
    _dc._grant_cache.clear()
    yield
    _dc._grant_cache.clear()


def _jwt(exp_offset_s: float) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f'{b64({"alg": "none"})}.{b64({"exp": time.time() + exp_offset_s, "sub": "u"})}.sig'


def _arm(monkeypatch, *, enabled=True, eligible=True, mint=lambda a, p, **k: "GRANT"):
    monkeypatch.setattr(delegation, "grants_enabled", lambda: enabled)
    monkeypatch.setattr(delegation, "can_delegate", lambda a: eligible)
    monkeypatch.setattr(delegation, "mint_grant", mint)


# ---- dispatch_credentials -------------------------------------------------------------------

def test_grant_preferred_even_with_a_fresh_user_token(monkeypatch):
    _arm(monkeypatch)
    assert dc.dispatch_credentials("actor-1", _jwt(3600)) == {"delegation_grant": "GRANT"}


def test_fresh_user_token_is_the_fallback(monkeypatch):
    _arm(monkeypatch, enabled=False)
    tok = _jwt(3600)
    assert dc.dispatch_credentials("actor-1", tok) == {"user_token": tok}


def test_token_expiring_within_a_dispatch_window_is_not_forwarded(monkeypatch):
    # 2 minutes left < the 5-minute fallback floor: binding it would lapse mid-turn.
    _arm(monkeypatch, enabled=False)
    assert dc.dispatch_credentials("actor-1", _jwt(120)) == {}


def test_expired_token_is_not_forwarded(monkeypatch):
    _arm(monkeypatch, enabled=False)
    assert dc.dispatch_credentials("actor-1", _jwt(-60)) == {}


def test_mint_failure_falls_back_to_a_valid_token(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("kms unavailable")
    _arm(monkeypatch, mint=_boom)
    tok = _jwt(3600)
    assert dc.dispatch_credentials("actor-1", tok) == {"user_token": tok}


def test_no_actor_no_grant_and_no_token_is_empty(monkeypatch):
    _arm(monkeypatch)
    assert dc.dispatch_credentials("", "") == {}


def test_opaque_token_without_exp_is_treated_as_valid(monkeypatch):
    # Unreadable expiry is "unknown, treat as valid" (matches _user_token_is_expired).
    _arm(monkeypatch, enabled=False)
    assert dc.dispatch_credentials("actor-1", "opaque") == {"user_token": "opaque"}


# ---- bind_user_token_and_connect refuses a lapsed bearer -------------------------------------

class _GW:
    def __init__(self):
        import threading
        self.auth_type = "user_jwt"
        self._connect_lock = threading.Lock()
        self._bound_user_token = "OLD"
        self.is_connected = True
        self.disconnects = 0
        self.connects = 0

    def disconnect(self):
        self.disconnects += 1

    def connect(self):
        self.connects += 1
        return {"status": "connected"}


def test_bind_refuses_an_expired_user_token():
    mgr = mm.MCPManager.__new__(mm.MCPManager)
    gw = _GW()
    mgr._gateways = {"user-mcp": gw}
    assert mgr.bind_user_token_and_connect(_jwt(-60)) == {}
    assert gw.disconnects == 0 and gw.connects == 0  # live transport untouched


# ---- task agent drops an expired forwarded token ---------------------------------------------

def test_task_agent_ignores_expired_forwarded_token(monkeypatch):
    import task_agent as ta
    seen = {}

    async def _bind(*, user_token="", delegation_grant=""):
        seen["user_token"] = user_token
        seen["delegation_grant"] = delegation_grant
        return False

    monkeypatch.setattr(ta, "_gateway_initialized", True)
    monkeypatch.setattr(ta, "_bind_invocation_gateway_auth", _bind)
    asyncio.run(ta._ensure_gateways_loaded(actor_id="a", user_token=_jwt(-60)))
    assert seen["user_token"] == ""  # never re-pinned to a dead bearer


def test_task_agent_keeps_grant_when_token_expired(monkeypatch):
    import task_agent as ta
    seen = {}

    async def _bind(*, user_token="", delegation_grant=""):
        seen.update(user_token=user_token, delegation_grant=delegation_grant)
        return True

    monkeypatch.setattr(ta, "_gateway_initialized", True)
    monkeypatch.setattr(ta, "_bind_invocation_gateway_auth", _bind)
    asyncio.run(ta._ensure_gateways_loaded(actor_id="a", user_token=_jwt(-60), delegation_grant="G"))
    assert seen == {"user_token": "", "delegation_grant": "G"}


# ---- grant reuse across parallel dispatches ---------------------------------------------------

def test_grant_is_reused_across_dispatches(monkeypatch):
    # Parallel dispatches share one session-pinned sub-agent microVM; the same grant lets it
    # reuse the live delegated session instead of rebuilding the transport per call.
    dc._grant_cache.clear()
    calls = []
    _arm(monkeypatch, mint=lambda a, p, **k: calls.append(a) or f"GRANT-{len(calls)}")
    got = {dc.dispatch_credentials("actor-1")["delegation_grant"] for _ in range(3)}
    assert got == {"GRANT-1"} and calls == ["actor-1"]


def test_grant_is_reissued_near_expiry(monkeypatch):
    dc._grant_cache.clear()
    calls = []
    _arm(monkeypatch, mint=lambda a, p, **k: calls.append(a) or f"GRANT-{len(calls)}")
    dc.dispatch_credentials("actor-1")
    g, _exp = dc._grant_cache["actor-1"]
    dc._grant_cache["actor-1"] = (g, time.time() + 60)  # < reissue window left
    assert dc.dispatch_credentials("actor-1")["delegation_grant"] == "GRANT-2"


def test_grants_are_per_actor(monkeypatch):
    dc._grant_cache.clear()
    _arm(monkeypatch, mint=lambda a, p, **k: f"GRANT-{a}")
    assert dc.dispatch_credentials("a1")["delegation_grant"] == "GRANT-a1"
    assert dc.dispatch_credentials("a2")["delegation_grant"] == "GRANT-a2"
