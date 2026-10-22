"""Main-agent delegated connect for token-less, grant-eligible invocations.

The Slack ingress invokes the main agent via IAM/SigV4 with NO user JWT, so
``connect_all()`` defers the ``user_jwt`` gateway (fal) and its tools never load
— which is why an ``@mention`` asking for image generation was told "fal isn't
wired into my toolset". When delegation grants are enabled and the actor can
delegate, the main agent now mints a grant and connects those gateways via the
async-plan machine token + grant (mirroring the Task Agent's async-plan connect),
so the tools load into the catalog on that cold-start.

These tests pin the wiring: enabled + eligible + creds arms the connect with the
minted grant and the async-plan creds; every missing precondition is a no-op that
leaves ``user_jwt`` gateways deferred (the unchanged pre-feature behavior); and a
connect failure must never break startup.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from security import delegation  # noqa: E402
import main  # noqa: E402


class _FakeMgr:
    """Stand-in for the main-agent MCPManager singleton (real one is None in tests)."""

    def __init__(self):
        self.calls: list[dict] = []

    def bind_delegation_grant_and_connect(
        self, grant, *, token_endpoint, client_id, client_secret, scope=""
    ):
        self.calls.append(
            {
                "grant": grant,
                "token_endpoint": token_endpoint,
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scope,
            }
        )
        return {"harness-user-mcp": {"status": "connected", "tools": ["fal_generate"]}}


def _arm_creds(monkeypatch):
    monkeypatch.setattr(main, "ASYNC_PLAN_TOKEN_ENDPOINT", "https://idp/oauth2/token")
    monkeypatch.setattr(main, "ASYNC_PLAN_CLIENT_ID", "cid")
    monkeypatch.setattr(main, "ASYNC_PLAN_CLIENT_SECRET", "secret")
    monkeypatch.setattr(main, "ASYNC_PLAN_SCOPE", "res/invoke")


def _arm_delegation(monkeypatch, *, enabled=True, eligible=True, grant="GRANT"):
    minted: dict = {}
    monkeypatch.setattr(delegation, "grants_enabled", lambda: enabled)
    monkeypatch.setattr(delegation, "can_delegate", lambda actor_id: eligible)

    def _mint(actor_id, plan_id, **kw):
        minted["actor_id"] = actor_id
        minted["plan_id"] = plan_id
        return grant

    monkeypatch.setattr(delegation, "mint_grant", _mint)
    return minted


def _install_mgr(monkeypatch) -> _FakeMgr:
    mgr = _FakeMgr()
    monkeypatch.setattr(main, "mcp_manager", mgr)
    return mgr


@pytest.mark.asyncio
async def test_arms_delegated_connect_when_enabled_eligible_and_creds_present(monkeypatch):
    _arm_creds(monkeypatch)
    minted = _arm_delegation(monkeypatch)
    mgr = _install_mgr(monkeypatch)

    await main._connect_delegated_gateways("slack-demo")

    assert len(mgr.calls) == 1
    c = mgr.calls[0]
    assert c["grant"] == "GRANT"
    assert c["token_endpoint"] == "https://idp/oauth2/token"
    assert c["client_id"] == "cid"
    assert c["client_secret"] == "secret"
    assert c["scope"] == "res/invoke"
    # The grant is minted for the invoking (synthetic) actor, not a real user.
    assert minted["actor_id"] == "slack-demo"


@pytest.mark.asyncio
async def test_noop_without_actor(monkeypatch):
    _arm_creds(monkeypatch)
    _arm_delegation(monkeypatch)
    mgr = _install_mgr(monkeypatch)
    await main._connect_delegated_gateways("")
    assert mgr.calls == []


@pytest.mark.asyncio
async def test_noop_when_grants_disabled(monkeypatch):
    _arm_creds(monkeypatch)
    _arm_delegation(monkeypatch, enabled=False)
    mgr = _install_mgr(monkeypatch)
    await main._connect_delegated_gateways("slack-demo")
    assert mgr.calls == []


@pytest.mark.asyncio
async def test_noop_when_actor_cannot_delegate(monkeypatch):
    _arm_creds(monkeypatch)
    _arm_delegation(monkeypatch, eligible=False)
    mgr = _install_mgr(monkeypatch)
    await main._connect_delegated_gateways("slack-demo")
    assert mgr.calls == []


@pytest.mark.asyncio
async def test_noop_when_async_plan_creds_unset(monkeypatch):
    # No machine creds → cannot mint a front-door token → stay deferred.
    monkeypatch.setattr(main, "ASYNC_PLAN_TOKEN_ENDPOINT", "")
    monkeypatch.setattr(main, "ASYNC_PLAN_CLIENT_ID", "")
    monkeypatch.setattr(main, "ASYNC_PLAN_CLIENT_SECRET", "")
    _arm_delegation(monkeypatch)
    mgr = _install_mgr(monkeypatch)
    await main._connect_delegated_gateways("slack-demo")
    assert mgr.calls == []


@pytest.mark.asyncio
async def test_connect_failure_is_swallowed(monkeypatch):
    _arm_creds(monkeypatch)
    _arm_delegation(monkeypatch)

    class _BoomMgr:
        def bind_delegation_grant_and_connect(self, *a, **k):
            raise RuntimeError("token endpoint down")

    monkeypatch.setattr(main, "mcp_manager", _BoomMgr())
    # Must not raise — a connect failure cannot break agent startup.
    await main._connect_delegated_gateways("slack-demo")
