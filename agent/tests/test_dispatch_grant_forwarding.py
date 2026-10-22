"""dispatch_tool forwards a delegation grant on the token-less path.

The main agent dispatches tool-heavy work (e.g. image generation) to the task
agent. On an interactive path WITH a user JWT, dispatch forwards the token so the
sub-agent connects user_jwt gateways (fal). On a TOKEN-LESS path (Slack IAM/SigV4)
there is no token, so dispatch instead mints a delegation grant for the actor and
forwards it as ``delegation_grant``; the task-agent entrypoint threads that into
its gateway load (see test_delegation_grant_forwarding for the connect half).

Without this, a token-less actor's dispatched image-gen sees only M2C on the
sub-agent and fal is unavailable — the exact "not available on the sub-agent
runtime" failure observed on a Slack @mention.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from security import delegation  # noqa: E402
from orchestration import dispatch_tool as dt  # noqa: E402
import jobs.context as jc  # noqa: E402


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_grant_cache():
    # The dispatch grant cache is process-global; isolate every test from the others.
    from orchestration import dispatch_credentials as _dc
    _dc._grant_cache.clear()
    yield
    _dc._grant_cache.clear()


def _set_ctx(monkeypatch, *, actor="slack-demo", token="", session="s1", turn="t1"):
    monkeypatch.setattr(jc, "current_actor_id", lambda: actor)
    monkeypatch.setattr(jc, "current_user_token", lambda: token)
    monkeypatch.setattr(jc, "current_session_id", lambda: session)
    monkeypatch.setattr(jc, "current_turn_id", lambda: turn)


def _set_delegation(monkeypatch, *, enabled=True, eligible=True, grant="GRANT"):
    seen: dict = {}
    monkeypatch.setattr(delegation, "grants_enabled", lambda: enabled)
    monkeypatch.setattr(delegation, "can_delegate", lambda a: eligible)

    def _mint(actor_id, plan_id, **k):
        seen["actor_id"] = actor_id
        seen["plan_id"] = plan_id
        return grant

    monkeypatch.setattr(delegation, "mint_grant", _mint)
    return seen


def test_forwards_grant_when_no_user_token(monkeypatch):
    _set_ctx(monkeypatch, token="")
    seen = _set_delegation(monkeypatch)
    payload: dict = {}
    dt._forward_request_context(payload)
    assert payload.get("delegation_grant") == "GRANT"
    assert "user_token" not in payload
    assert payload["actor_id"] == "slack-demo"
    # Grant is minted for the dispatching actor.
    assert seen["actor_id"] == "slack-demo"


def test_grant_is_preferred_over_a_user_token(monkeypatch):
    # Policy change (prod 45d2139c): a browser access token can lapse while the sub-agent reuses
    # its warm connection (403 insufficient_scope). Every dispatch now carries a FRESH grant when
    # one can be minted, and the user token is not forwarded alongside it.
    _set_ctx(monkeypatch, token="USERJWT")
    seen = _set_delegation(monkeypatch)
    payload: dict = {}
    dt._forward_request_context(payload)
    assert payload.get("delegation_grant") == "GRANT"
    assert "user_token" not in payload
    assert seen["actor_id"] == "slack-demo"


def test_user_token_is_the_fallback_when_no_grant(monkeypatch):
    # No grant possible (disabled) -> a still-valid user token is forwarded as before.
    _set_ctx(monkeypatch, token="USERJWT")  # opaque token: expiry unreadable -> treated as valid
    seen = _set_delegation(monkeypatch, enabled=False)
    payload: dict = {}
    dt._forward_request_context(payload)
    assert payload.get("user_token") == "USERJWT"
    assert "delegation_grant" not in payload
    assert seen == {}


def test_no_grant_when_grants_disabled(monkeypatch):
    _set_ctx(monkeypatch, token="")
    seen = _set_delegation(monkeypatch, enabled=False)
    payload: dict = {}
    dt._forward_request_context(payload)
    assert "delegation_grant" not in payload
    assert seen == {}


def test_no_grant_when_actor_ineligible(monkeypatch):
    _set_ctx(monkeypatch, token="")
    seen = _set_delegation(monkeypatch, eligible=False)
    payload: dict = {}
    dt._forward_request_context(payload)
    assert "delegation_grant" not in payload
    assert seen == {}


def test_no_grant_without_actor(monkeypatch):
    _set_ctx(monkeypatch, actor="", token="")
    _set_delegation(monkeypatch)
    payload: dict = {}
    dt._forward_request_context(payload)
    assert "delegation_grant" not in payload


def test_mint_failure_is_swallowed(monkeypatch):
    _set_ctx(monkeypatch, token="")
    monkeypatch.setattr(delegation, "grants_enabled", lambda: True)
    monkeypatch.setattr(delegation, "can_delegate", lambda a: True)

    def _boom(*a, **k):
        raise RuntimeError("kms unavailable")

    monkeypatch.setattr(delegation, "mint_grant", _boom)
    payload: dict = {}
    # Must not raise — a mint failure cannot break dispatch (non-user_jwt tools still work).
    dt._forward_request_context(payload)
    assert "delegation_grant" not in payload
