"""Tests for Wave 1 / option E — the async delegation-grant DELIVERY path.

The mint/persist/present/verify halves are covered elsewhere
(``test_plan_delegation_grant.py`` + the interceptor suite). This file covers
the two pieces that make the grant actually reach the interceptor:

* **Machine-token connect** (``mcp_manager``) — a ``user_jwt`` gateway with no
  user token but a signed grant + machine creds crosses the front door with a
  client_credentials token and presents the grant as ``DELEGATION_GRANT_HEADER``.
  The normal user-token path must NOT be weakened.
* **Forwarding path** (``task_agent``) — a plan node payload's ``delegation_grant``
  is threaded into ``_ensure_gateways_loaded`` and on to
  ``bind_delegation_grant_and_connect`` with the async-plan machine creds.
"""

from __future__ import annotations

import base64
from contextlib import nullcontext
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import mcp_manager as mm  # noqa: E402
from mcp_manager import (  # noqa: E402
    DELEGATION_GRANT_HEADER,
    GatewayConnection,
    MCPManager,
    _user_token_is_expired,
)


def _jwt_with_exp(exp_epoch: float | None) -> str:
    """Build an UNSIGNED JWT (header.payload.sig) with the given ``exp`` claim.

    Signature is irrelevant here — ``_user_token_is_expired`` reads ``exp``
    without verifying. ``None`` omits the claim (mirrors a token we can't date).
    """
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    claims: dict = {"sub": "alice"}
    if exp_epoch is not None:
        claims["exp"] = int(exp_epoch)
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


USER_TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.sig"
GRANT = "hdr.payload.sig"
MACHINE_TOKEN = "machine-cc-token"
CREDS = {
    "token_endpoint": "https://pool.auth.us-west-2.amazoncognito.com/oauth2/token",
    "client_id": "asyncplanclient123",
    "client_secret": "shhh",
    "scope": "proj-async-plan/invoke",
}


# ---------------------------------------------------------------------------
# _user_token_is_expired — reads exp WITHOUT verifying the signature. Only a
# PROVABLY-past token is "expired"; anything unreadable is "valid" so we never
# churn a live interactive session on a parsing quirk.
# ---------------------------------------------------------------------------


def test_expiry_true_for_past_token():
    assert _user_token_is_expired(_jwt_with_exp(time.time() - 120)) is True


def test_expiry_false_for_future_token():
    assert _user_token_is_expired(_jwt_with_exp(time.time() + 3600)) is False


def test_expiry_skew_treats_about_to_lapse_as_expired():
    # exp is 10s in the future but inside the 30s skew → expired (won't be
    # presented when it would lapse mid-connect).
    assert _user_token_is_expired(_jwt_with_exp(time.time() + 10)) is True


def test_expiry_false_when_no_exp_claim():
    # The legacy USER_TOKEN fixture (sub only, no exp) — unknown, treat as valid.
    assert _user_token_is_expired(USER_TOKEN) is False
    assert _user_token_is_expired(_jwt_with_exp(None)) is False


def test_expiry_false_for_garbage_and_empty():
    assert _user_token_is_expired("") is False
    assert _user_token_is_expired("not-a-jwt") is False
    assert _user_token_is_expired("only.two") is False


# ---------------------------------------------------------------------------
# The header name MUST byte-match the interceptor's DELEGATION_GRANT_HEADER.
# A mismatch is silent: the interceptor treats an unknown header as "no grant",
# falls to the JWT path, finds no user sub on a machine token, and DENIES.
# ---------------------------------------------------------------------------


def test_header_name_matches_the_interceptor_literal():
    assert DELEGATION_GRANT_HEADER == (
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-DelegationGrant"
    )


# ---------------------------------------------------------------------------
# Machine-token + grant connect (mcp_manager)
# ---------------------------------------------------------------------------


def _captured_headers(monkeypatch, conn: GatewayConnection) -> dict:
    """Drive ``_do_connect`` far enough to capture the outbound headers.

    Construction uses Strands' public ``MCPClient(url=, headers=)`` interface,
    so capture the ``headers`` kwarg rather than a streamablehttp factory.
    """
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


def test_grant_path_uses_machine_token_and_grant_header(monkeypatch):
    conn = GatewayConnection(
        "harness",
        {"url": "https://gw/mcp", "auth_type": "user_jwt", "delegation_grant": GRANT, **CREDS},
    )
    monkeypatch.setattr(GatewayConnection, "_get_oauth_token", lambda self: MACHINE_TOKEN)
    headers = _captured_headers(monkeypatch, conn)
    # Front door authenticated with the MACHINE token...
    assert headers["Authorization"] == f"Bearer {MACHINE_TOKEN}"
    # ...and the signed grant rides the exact header the interceptor reads.
    assert headers[DELEGATION_GRANT_HEADER] == GRANT


def test_user_token_still_wins_over_a_grant(monkeypatch):
    """The normal interactive path is UNCHANGED even if a grant is also set."""
    conn = GatewayConnection(
        "harness",
        {
            "url": "https://gw/mcp",
            "auth_type": "user_jwt",
            "user_token": USER_TOKEN,
            "delegation_grant": GRANT,
            **CREDS,
        },
    )

    def _must_not_mint(self):
        raise AssertionError("machine token minted on the user-token path")

    monkeypatch.setattr(GatewayConnection, "_get_oauth_token", _must_not_mint)
    headers = _captured_headers(monkeypatch, conn)
    assert headers["Authorization"] == f"Bearer {USER_TOKEN}"
    assert DELEGATION_GRANT_HEADER not in headers


def test_connect_without_token_or_grant_still_fails_loudly():
    conn = GatewayConnection("harness", {"url": "https://gw/mcp", "auth_type": "user_jwt"})
    with pytest.raises(RuntimeError, match="no user token was forwarded"):
        conn._do_connect()


def test_partial_creds_do_not_arm_the_grant_path():
    """A grant with no machine creds must NOT connect — it would 401, or worse."""
    conn = GatewayConnection(
        "harness",
        {"url": "https://gw/mcp", "auth_type": "user_jwt", "delegation_grant": GRANT},
    )
    with pytest.raises(RuntimeError, match="no user token was forwarded"):
        conn._do_connect()


# ---- awaiting_user_token gating ----


def test_awaiting_true_without_token_or_grant():
    conn = GatewayConnection("h", {"url": "u", "auth_type": "user_jwt"})
    assert conn.awaiting_user_token() is True


def test_awaiting_false_with_user_token():
    conn = GatewayConnection("h", {"url": "u", "auth_type": "user_jwt", "user_token": USER_TOKEN})
    assert conn.awaiting_user_token() is False


def test_awaiting_false_when_armed_with_grant_and_creds():
    conn = GatewayConnection(
        "h", {"url": "u", "auth_type": "user_jwt", "delegation_grant": GRANT, **CREDS}
    )
    assert conn.awaiting_user_token() is False


def test_set_delegated_machine_auth_arms_fields():
    conn = GatewayConnection("h", {"url": "u", "auth_type": "user_jwt"})
    conn.set_delegated_machine_auth(grant=GRANT, **CREDS)
    assert conn.delegation_grant == GRANT
    assert conn.client_id == CREDS["client_id"]
    assert conn.token_endpoint == CREDS["token_endpoint"]
    # Never touches the user-token path.
    assert conn.user_token == ""


# ---------------------------------------------------------------------------
# MCPManager.bind_delegation_grant_and_connect
# ---------------------------------------------------------------------------


class _FakeGW:
    def __init__(self, auth_type="user_jwt", connected=False, user_token="", grant="", alive=True):
        self.auth_type = auth_type
        self._connected = connected
        self.user_token = user_token
        self._bound_user_token = user_token if connected else ""
        self.delegation_grant = grant
        self._alive = alive
        self.armed = None
        self.connect_calls = 0
        self.disconnect_calls = 0
        # Real GatewayConnection serializes (re)connect on this lock; the fake must
        # carry one too or bind_delegation_grant_and_connect's `with gw._connect_lock`
        # raises. Reentrant, matching production.
        self._connect_lock = threading.RLock()
        # Optional delay INSIDE connect() to widen the concurrency window in the
        # thundering-herd test; 0 keeps every other test instant.
        self.connect_delay = 0.0

    @property
    def is_connected(self):
        return self._connected

    def _session_alive(self):
        # Mirrors GatewayConnection._session_alive: a sticky _connected can mask a
        # session the gateway idle-closed. Only "alive" when connected AND the probe passes.
        return self._connected and self._alive

    def _auth_transition(self):
        return nullcontext()

    def set_delegated_machine_auth(self, *, grant, token_endpoint, client_id, client_secret, scope=""):
        self.armed = {"grant": grant, "client_id": client_id, "token_endpoint": token_endpoint,
                      "client_secret": client_secret, "scope": scope}
        self.user_token = ""
        self.delegation_grant = grant

    def connect(self):
        if self.connect_delay:
            time.sleep(self.connect_delay)
        self.connect_calls += 1
        self._connected = True
        self._bound_user_token = self.user_token
        self._alive = True  # a fresh connect yields a LIVE session
        return {"status": "connected"}

    def _connect_impl(self):
        return self.connect()

    def disconnect(self):
        self.disconnect_calls += 1
        self._connected = False
        self._bound_user_token = ""
        return True

    def _disconnect_impl(self):
        return self.disconnect()


def _mgr_with(gateways: dict, monkeypatch) -> MCPManager:
    mgr = MCPManager()
    mgr._gateways = gateways
    # Neutralize the job-tracking wrap (no registry in these tests).
    monkeypatch.setattr(mgr, "_apply_job_tracking_wrap", lambda gw: None)
    return mgr


def test_bind_noop_without_creds(monkeypatch):
    gw = _FakeGW()
    mgr = _mgr_with({"h": gw}, monkeypatch)
    assert mgr.bind_delegation_grant_and_connect(GRANT, token_endpoint="", client_id="",
                                                 client_secret="", scope="") == {}
    assert gw.connect_calls == 0


def test_bind_connects_user_jwt_gateway(monkeypatch):
    gw = _FakeGW()
    mgr = _mgr_with({"h": gw}, monkeypatch)
    res = mgr.bind_delegation_grant_and_connect(GRANT, **CREDS)
    assert res["h"]["status"] == "connected"
    assert gw.connect_calls == 1
    assert gw.armed["grant"] == GRANT
    assert gw.armed["client_id"] == CREDS["client_id"]


def test_bind_skips_non_user_jwt_gateways(monkeypatch):
    gw = _FakeGW(auth_type="jwt")
    mgr = _mgr_with({"m2c": gw}, monkeypatch)
    assert mgr.bind_delegation_grant_and_connect(GRANT, **CREDS) == {}
    assert gw.connect_calls == 0


def test_bind_grant_replaces_live_user_token_connection(monkeypatch):
    gw = _FakeGW(connected=True, user_token=USER_TOKEN)
    mgr = _mgr_with({"h": gw}, monkeypatch)
    result = mgr.bind_delegation_grant_and_connect(GRANT, **CREDS)
    assert result["h"]["status"] == "connected"
    assert gw.connect_calls == 1
    assert gw.disconnect_calls == 1
    assert gw.user_token == ""
    assert gw.armed["grant"] == GRANT


def test_bind_grant_replaces_even_unexpired_user_token(monkeypatch):
    """Detached work uses its grant, never a mutable interactive credential."""
    gw = _FakeGW(connected=True, user_token=_jwt_with_exp(time.time() + 3600))
    mgr = _mgr_with({"h": gw}, monkeypatch)
    result = mgr.bind_delegation_grant_and_connect(GRANT, **CREDS)
    assert result["h"]["status"] == "connected"
    assert gw.connect_calls == 1
    assert gw.disconnect_calls == 1
    assert gw.user_token == ""
    assert gw.armed["grant"] == GRANT


def test_bind_rebinds_when_pinned_user_token_expired(monkeypatch):
    """THE FIX: a detached node whose shared connection is pinned to an EXPIRED
    user token must not present that token (the gateway 403s it as
    'insufficient_scope'). The expired token is cleared and the connection is
    rebuilt on the machine token + grant."""
    expired = _jwt_with_exp(time.time() - 60)
    gw = _FakeGW(connected=True, user_token=expired)  # grant="" (interactive-pinned)
    mgr = _mgr_with({"h": gw}, monkeypatch)
    res = mgr.bind_delegation_grant_and_connect(GRANT, **CREDS)
    # Fell through the guard: tore down the stale session and rebuilt delegated.
    assert gw.disconnect_calls == 1
    assert gw.connect_calls == 1
    assert res["h"]["status"] == "connected"
    # Armed with the machine creds + grant...
    assert gw.armed["grant"] == GRANT
    assert gw.armed["client_id"] == CREDS["client_id"]
    # ...and the expired user token is CLEARED so build_auth_headers takes the
    # machine+grant branch instead of presenting the dead token again.
    assert gw.user_token == ""


def test_bind_reuses_live_same_grant_session(monkeypatch):
    """A same-grant warm connection that is still LIVE is now REUSED across nodes, not rebuilt.

    Unconditional per-node teardown+reconnect churned the shared, warm-container MCP session
    and produced intermittent "the client session was not initialized" errors; 3 of those trip
    the circuit breaker, which then gated whole fan-outs (session 05a6ff2f). Reuse is safe now:
    a stale (idle-closed) session that still reads as alive is backstopped by the resilient tool
    wrapper's reconnect+retry-once. Only a DEAD session or a DIFFERENT grant forces a rebuild
    (the two tests below)."""
    gw = _FakeGW(connected=True, grant=GRANT, alive=True)
    mgr = _mgr_with({"h": gw}, monkeypatch)
    res = mgr.bind_delegation_grant_and_connect(GRANT, **CREDS)
    assert gw.disconnect_calls == 0          # live same-grant session left intact
    assert gw.connect_calls == 0             # not rebuilt
    assert res["h"] == {"status": "already_connected", "reused": True}


def test_bind_rebuilds_same_grant_when_session_dead(monkeypatch):
    """A same-grant warm connection whose MCP session has idle-closed must be REBUILT,
    not reused — reusing a dead session is exactly what made fal video-gen submit nodes
    fail with 'the client session is not running' forever."""
    gw = _FakeGW(connected=True, grant=GRANT, alive=False)
    mgr = _mgr_with({"h": gw}, monkeypatch)
    res = mgr.bind_delegation_grant_and_connect(GRANT, **CREDS)
    assert gw.disconnect_calls == 1
    assert gw.connect_calls == 1
    assert res["h"]["status"] == "connected"
    assert gw.armed["grant"] == GRANT


def test_bind_reconnects_on_grant_change(monkeypatch):
    """A warm connection bound to a DIFFERENT grant (possibly a different actor)
    must be torn down and rebuilt — never reused with a stale grant header."""
    gw = _FakeGW(connected=True, grant="OLD.grant.token")
    mgr = _mgr_with({"h": gw}, monkeypatch)
    res = mgr.bind_delegation_grant_and_connect(GRANT, **CREDS)
    assert gw.disconnect_calls == 1
    assert gw.connect_calls == 1
    assert res["h"]["status"] == "connected"
    assert gw.armed["grant"] == GRANT


def test_bind_concurrent_fanout_collapses_to_one_rebuild(monkeypatch):
    """Session 23f1d54b regression: a warm task-runtime container serves an async
    plan's whole fan-out, so N nodes call bind_delegation_grant_and_connect at once
    against the SAME dead-session gateway. Without per-gateway serialization they all
    tore down and rebuilt one shared MCPClient concurrently → a cascade of
    'MCPClientInitializationError: the client session is not running' and every submit
    node's tools unresolvable.

    With the per-connection lock + re-check, exactly ONE thread rebuilds; the rest
    acquire the lock after it, see the now-live same-grant session, and REUSE it.
    """
    gw = _FakeGW(connected=True, grant=GRANT, alive=False)  # same grant, DEAD session
    gw.connect_delay = 0.05  # hold the critical section so peers pile up on the lock
    mgr = _mgr_with({"h": gw}, monkeypatch)

    N = 16
    results: list = []
    errors: list = []
    barrier = threading.Barrier(N)

    def worker():
        barrier.wait()  # release all threads simultaneously
        try:
            results.append(mgr.bind_delegation_grant_and_connect(GRANT, **CREDS))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent bind raised: {errors}"
    # Exactly one rebuild (one disconnect + one connect); the herd collapsed.
    assert gw.connect_calls == 1, f"expected 1 rebuild, got {gw.connect_calls}"
    assert gw.disconnect_calls == 1
    # Every node got a usable result: the one rebuilder 'connected', the rest 'reused'.
    statuses = [r["h"].get("status") for r in results]
    assert statuses.count("connected") == 1
    assert statuses.count("already_connected") == N - 1


# ---------------------------------------------------------------------------
# End-to-end at the header level (real GatewayConnection): an expired pinned
# user token must not be presented — the next connect uses machine + grant.
# ---------------------------------------------------------------------------


def test_expired_user_token_rebind_produces_machine_grant_headers(monkeypatch):
    conn = GatewayConnection(
        "harness",
        {
            "url": "https://gw/mcp",
            "auth_type": "user_jwt",
            "user_token": _jwt_with_exp(time.time() - 60),  # pinned, now stale
        },
    )
    # Pretend it holds a live interactive session (stale token, but "connected").
    conn._connected = True
    monkeypatch.setattr(GatewayConnection, "_session_alive", lambda self: True)
    monkeypatch.setattr(GatewayConnection, "_get_oauth_token", lambda self: MACHINE_TOKEN)
    # The rebuild itself (real connect/disconnect network path) is exercised by
    # the _FakeGW tests above; here we only care that the manager mutates the
    # connection's AUTH STATE correctly, so stub the transport calls.
    monkeypatch.setattr(conn, "connect", lambda: {"status": "connected"})
    monkeypatch.setattr(conn, "disconnect", lambda: True)

    mgr = MCPManager()
    mgr._gateways = {"harness": conn}
    monkeypatch.setattr(mgr, "_apply_job_tracking_wrap", lambda gw: None)

    mgr.bind_delegation_grant_and_connect(GRANT, **CREDS)

    # The manager dropped the expired token and armed the machine + grant creds.
    assert conn.user_token == ""
    assert conn.delegation_grant == GRANT

    # PROOF the mechanism works end-to-end: with the stale token gone, the headers
    # this connection now builds authenticate as a MACHINE and carry the grant —
    # never the expired user token the gateway 403s as "insufficient_scope".
    headers = _captured_headers(monkeypatch, conn)
    assert headers["Authorization"] == f"Bearer {MACHINE_TOKEN}"
    assert headers[DELEGATION_GRANT_HEADER] == GRANT


# ---------------------------------------------------------------------------
# Forwarding path (task_agent) — payload grant → machine creds → connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_agent_connect_delegated_calls_manager(monkeypatch):
    import task_agent as ta

    captured = {}

    def _fake_bind(grant, *, token_endpoint, client_id, client_secret, scope=""):
        captured.update(
            grant=grant, token_endpoint=token_endpoint, client_id=client_id,
            client_secret=client_secret, scope=scope,
        )
        return {"harness": {"status": "connected"}}

    monkeypatch.setattr(ta.mcp_manager, "bind_delegation_grant_and_connect", _fake_bind)
    monkeypatch.setattr(ta, "ASYNC_PLAN_TOKEN_ENDPOINT", CREDS["token_endpoint"])
    monkeypatch.setattr(ta, "ASYNC_PLAN_CLIENT_ID", CREDS["client_id"])
    monkeypatch.setattr(ta, "ASYNC_PLAN_CLIENT_SECRET", CREDS["client_secret"])
    monkeypatch.setattr(ta, "ASYNC_PLAN_SCOPE", CREDS["scope"])

    await ta._connect_delegated_gateways(GRANT)

    assert captured["grant"] == GRANT
    assert captured["client_id"] == CREDS["client_id"]
    assert captured["token_endpoint"] == CREDS["token_endpoint"]
    assert captured["scope"] == CREDS["scope"]


@pytest.mark.asyncio
async def test_task_agent_connect_delegated_noop_without_creds(monkeypatch):
    """No async-plan client configured (dev) → do not attempt a doomed connect."""
    import task_agent as ta

    called = {"n": 0}

    def _fake_bind(*a, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(ta.mcp_manager, "bind_delegation_grant_and_connect", _fake_bind)
    monkeypatch.setattr(ta, "ASYNC_PLAN_TOKEN_ENDPOINT", "")
    monkeypatch.setattr(ta, "ASYNC_PLAN_CLIENT_ID", "")
    monkeypatch.setattr(ta, "ASYNC_PLAN_CLIENT_SECRET", "")

    await ta._connect_delegated_gateways(GRANT)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_task_agent_connect_delegated_noop_without_grant(monkeypatch):
    import task_agent as ta

    called = {"n": 0}
    monkeypatch.setattr(
        ta.mcp_manager, "bind_delegation_grant_and_connect",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {},
    )
    monkeypatch.setattr(ta, "ASYNC_PLAN_TOKEN_ENDPOINT", CREDS["token_endpoint"])
    monkeypatch.setattr(ta, "ASYNC_PLAN_CLIENT_ID", CREDS["client_id"])
    monkeypatch.setattr(ta, "ASYNC_PLAN_CLIENT_SECRET", CREDS["client_secret"])

    await ta._connect_delegated_gateways("")
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# END-TO-END (local): the detached-node entry path on a WARM container whose
# shared fal connection is pinned to an EXPIRED user token. Drives the REAL
# task_agent._ensure_gateways_loaded → REAL MCPManager.bind_delegation_grant_and_connect,
# reproducing the production bug's setup, and asserts the fix rebinds on the
# machine token instead of presenting the dead user token (the 403 cause).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detached_node_rebinds_expired_pinned_user_token_e2e(monkeypatch):
    import task_agent as ta

    # A warm container: gateways already loaded, and the fal user_jwt gateway is
    # live but pinned to a now-EXPIRED user token from earlier interactive use.
    expired = _jwt_with_exp(time.time() - 300)
    gw = _FakeGW(connected=True, user_token=expired)  # grant="" (interactive-pinned)

    mgr = MCPManager()
    mgr._gateways = {"harness": gw}
    monkeypatch.setattr(mgr, "_apply_job_tracking_wrap", lambda g: None)

    monkeypatch.setattr(ta, "mcp_manager", mgr)
    monkeypatch.setattr(ta, "_gateway_initialized", True)  # warm path
    monkeypatch.setattr(ta, "ASYNC_PLAN_TOKEN_ENDPOINT", CREDS["token_endpoint"])
    monkeypatch.setattr(ta, "ASYNC_PLAN_CLIENT_ID", CREDS["client_id"])
    monkeypatch.setattr(ta, "ASYNC_PLAN_CLIENT_SECRET", CREDS["client_secret"])
    monkeypatch.setattr(ta, "ASYNC_PLAN_SCOPE", CREDS["scope"])

    # A detached plan node dispatch: a signed grant, NO user token (the turn ended).
    await ta._ensure_gateways_loaded(actor_id="USER#alice", user_token="", delegation_grant=GRANT)

    # The expired pinned token was dropped and the connection rebuilt on the
    # machine token + grant — exactly what stops the gateway 403 in production.
    assert gw.user_token == ""
    assert gw.disconnect_calls == 1
    assert gw.connect_calls == 1
    assert gw.armed["grant"] == GRANT
    assert gw.armed["client_id"] == CREDS["client_id"]


@pytest.mark.asyncio
async def test_detached_node_replaces_valid_pinned_user_token_e2e(monkeypatch):
    """A grant-bearing node always selects machine+grant auth.

    The latest ``gw.user_token`` value can be fresh while the live MCPClient
    still carries an older expired bearer, so apparent user-token validity must
    never override the detached node's grant.
    """
    import task_agent as ta

    gw = _FakeGW(connected=True, user_token=_jwt_with_exp(time.time() + 1800))

    mgr = MCPManager()
    mgr._gateways = {"harness": gw}
    monkeypatch.setattr(mgr, "_apply_job_tracking_wrap", lambda g: None)

    monkeypatch.setattr(ta, "mcp_manager", mgr)
    monkeypatch.setattr(ta, "_gateway_initialized", True)
    monkeypatch.setattr(ta, "ASYNC_PLAN_TOKEN_ENDPOINT", CREDS["token_endpoint"])
    monkeypatch.setattr(ta, "ASYNC_PLAN_CLIENT_ID", CREDS["client_id"])
    monkeypatch.setattr(ta, "ASYNC_PLAN_CLIENT_SECRET", CREDS["client_secret"])
    monkeypatch.setattr(ta, "ASYNC_PLAN_SCOPE", CREDS["scope"])

    await ta._ensure_gateways_loaded(actor_id="USER#alice", user_token="", delegation_grant=GRANT)

    assert gw.user_token == ""
    assert gw.connect_calls == 1
    assert gw.disconnect_calls == 1
    assert gw.armed["grant"] == GRANT
