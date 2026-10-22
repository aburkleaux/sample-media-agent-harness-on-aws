"""Gateway boundary resilience tests (P1): OAuth token retry + circuit breaker."""
import pytest

import mcp_manager as m


class _Resp:
    def __init__(self, code, token=None):
        self.status_code = code
        self._token = token

    def json(self):
        return {"access_token": self._token, "expires_in": 3600}


def _conn(**overrides):
    cfg = {"url": "https://gw/mcp", "token_endpoint": "https://idp/token",
           "client_id": "c", "client_secret": "s", "scope": "x"}
    cfg.update(overrides)
    return m.GatewayConnection("g", cfg)


def test_token_retries_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(m, "_backoff_sleep", lambda *a, **k: None)
    seq = [_Resp(503), _Resp(200, "tok-abc")]
    calls = {"n": 0}

    def fake_post(*a, **k):
        r = seq[calls["n"]]
        calls["n"] += 1
        return r

    monkeypatch.setattr(m.httpx, "post", fake_post)
    assert _conn()._get_oauth_token() == "tok-abc"
    assert calls["n"] == 2  # retried once, then succeeded


def test_token_fails_fast_on_4xx(monkeypatch):
    monkeypatch.setattr(m, "_backoff_sleep", lambda *a, **k: None)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _Resp(401)

    monkeypatch.setattr(m.httpx, "post", fake_post)
    with pytest.raises(RuntimeError):
        _conn()._get_oauth_token()
    assert calls["n"] == 1  # 4xx is not retried


def test_circuit_breaker_opens_after_threshold(monkeypatch):
    conn = _conn()
    do_calls = {"n": 0}

    def boom():
        do_calls["n"] += 1
        raise RuntimeError("connect failed")

    monkeypatch.setattr(conn, "_do_connect", boom)
    for _ in range(m._CB_FAILURE_THRESHOLD):
        with pytest.raises(RuntimeError):
            conn.connect()
    attempts_before_open = do_calls["n"]
    # Breaker now open — fail fast without calling _do_connect.
    with pytest.raises(RuntimeError, match="circuit open"):
        conn.connect()
    assert do_calls["n"] == attempts_before_open


def test_circuit_breaker_resets_on_success(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(conn, "_do_connect", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        conn.connect()
    assert conn._cb_failures == 1
    monkeypatch.setattr(conn, "_do_connect", lambda: {"status": "connected"})
    assert conn.connect() == {"status": "connected"}
    assert conn._cb_failures == 0 and conn._cb_open_until == 0.0


# --- Dead-session self-heal (fal video-gen loop root cause) ---------------------------
#
# A warm container's ``_connected`` flag stays True over a session the gateway idle-closed,
# so tool calls raise ``the client session is not running`` and nothing reconnects. connect()
# must probe liveness and rebuild a dead session instead of handing it back.

class _FakeClient:
    """Minimal stand-in for the strands MCPClient seam used by GatewayConnection."""

    def __init__(self, alive=True):
        self._alive = alive

    def _is_session_active(self):
        # Strands' real synchronous liveness check — this is what _session_alive must use.
        return self._alive

    def list_tools_sync(self):
        # Guard: _session_alive must NOT probe with this (it spawns an un-awaited coroutine on a
        # warm/dead session and trips the circuit breaker). Blow up if anything calls it.
        raise AssertionError("list_tools_sync must not be used as a liveness probe")

    def __exit__(self, *_a):
        return False


def test_session_alive_reflects_client_state():
    conn = _conn()
    # Not connected → not alive.
    assert conn._session_alive() is False
    # Connected + live client → alive.
    conn._connected = True
    conn._client = _FakeClient(alive=True)
    assert conn._session_alive() is True
    # Connected flag sticky but session dead → not alive.
    conn._client = _FakeClient(alive=False)
    assert conn._session_alive() is False


def test_connect_rebuilds_dead_cached_session(monkeypatch):
    conn = _conn()
    conn._connected = True
    conn._client = _FakeClient(alive=False)  # idle-closed session, flag still True
    do = {"n": 0}

    def fake_do_connect():
        do["n"] += 1
        conn._connected = True
        conn._client = _FakeClient(alive=True)
        return {"status": "connected"}

    monkeypatch.setattr(conn, "_do_connect", fake_do_connect)
    result = conn.connect()
    assert do["n"] == 1                       # reconnected, did NOT no-op
    assert result == {"status": "connected"}


def test_connect_reuses_live_cached_session(monkeypatch):
    conn = _conn()
    conn._connected = True
    conn._client = _FakeClient(alive=True)
    conn._tool_metadata = [{"name": "x"}]
    monkeypatch.setattr(
        conn, "_do_connect",
        lambda: pytest.fail("must not reconnect a live session"),
    )
    result = conn.connect()
    assert result["status"] == "already_connected"


# --- Circuit-breaker HALF-OPEN decay (session 05a6ff2f fan-out re-gating) --------------
#
# Before the fix, ``_cb_failures`` only reset on a SUCCESSFUL connect. After the cooldown
# elapsed the count was still >= threshold, so a SINGLE post-cooldown failure instantly
# re-opened for another full cooldown — which kept an 8-frame fan-out gated in repeating
# 60s cycles off a couple of transient blips. connect() must treat the first attempt after
# the cooldown as a clean probe.

def test_circuit_breaker_half_open_probe_does_not_reopen_on_single_failure(monkeypatch):
    conn = _conn()
    clock = {"t": 1000.0}
    monkeypatch.setattr(m.time, "time", lambda: clock["t"])
    monkeypatch.setattr(conn, "_do_connect", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    for _ in range(m._CB_FAILURE_THRESHOLD):
        with pytest.raises(RuntimeError):
            conn.connect()
    assert conn._cb_open_until > clock["t"]  # breaker open

    # Within cooldown: fast-fail, no probe.
    do = {"n": 0}

    def boom():
        do["n"] += 1
        raise RuntimeError("connect failed")

    monkeypatch.setattr(conn, "_do_connect", boom)
    with pytest.raises(RuntimeError, match="circuit open"):
        conn.connect()
    assert do["n"] == 0

    # Past cooldown: HALF-OPEN — one probe runs, and a single failure must NOT re-open.
    clock["t"] += m._CB_COOLDOWN_SECONDS + 1
    with pytest.raises(RuntimeError, match="connect failed"):
        conn.connect()
    assert do["n"] == 1                 # the probe actually attempted
    assert conn._cb_failures == 1       # counts from 1, not from threshold
    assert conn._cb_open_until == 0.0   # did NOT re-open on a single probe failure


def test_circuit_breaker_half_open_success_recovers(monkeypatch):
    conn = _conn()
    clock = {"t": 1000.0}
    monkeypatch.setattr(m.time, "time", lambda: clock["t"])
    monkeypatch.setattr(conn, "_do_connect", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    for _ in range(m._CB_FAILURE_THRESHOLD):
        with pytest.raises(RuntimeError):
            conn.connect()
    clock["t"] += m._CB_COOLDOWN_SECONDS + 1
    monkeypatch.setattr(conn, "_do_connect", lambda: {"status": "connected"})
    assert conn.connect() == {"status": "connected"}
    assert conn._cb_failures == 0 and conn._cb_open_until == 0.0


# --- Delegated reuse-if-alive (removes the per-node teardown churn that trips the breaker) ---
#
# bind_delegation_grant_and_connect used to tear down + reconnect the shared delegated session
# EVERY node. On a warm container that churn produced intermittent "session was not initialized"
# errors that tripped the breaker and gated the fan-out. It must now REUSE a live, same-grant
# session (a stale reuse is backstopped by the resilient tool wrapper's reconnect+retry-once),
# and only reconnect when the grant differs or the session is dead/absent.

def _delegated_mgr(monkeypatch, *, grant_on_conn, alive):
    mgr = m.MCPManager()
    conn = _conn(auth_type="user_jwt")
    conn.user_token = ""                      # no interactive token → delegated path applies
    conn._connected = True
    conn.delegation_grant = grant_on_conn
    conn._client = _FakeClient(alive=alive)
    mgr._gateways = {"g": conn}
    calls = {"connect": 0, "disconnect": 0, "wrap": 0}

    def fake_connect():
        calls["connect"] += 1
        conn._connected = True
        conn._client = _FakeClient(alive=True)
        return {"status": "connected"}

    def fake_disconnect():
        calls["disconnect"] += 1
        conn._connected = False
        return True

    def fake_wrap(gw):
        calls["wrap"] += 1

    monkeypatch.setattr(conn, "connect", fake_connect)
    monkeypatch.setattr(conn, "disconnect", fake_disconnect)
    monkeypatch.setattr(mgr, "_apply_job_tracking_wrap", fake_wrap)
    return mgr, conn, calls


def test_delegated_reuses_live_same_grant_session(monkeypatch):
    mgr, conn, calls = _delegated_mgr(monkeypatch, grant_on_conn="GRANT-A", alive=True)
    res = mgr.bind_delegation_grant_and_connect(
        grant="GRANT-A", token_endpoint="t", client_id="c", client_secret="s",
    )
    assert res["g"] == {"status": "already_connected", "reused": True}
    assert calls["connect"] == 0        # did NOT reconnect
    assert calls["disconnect"] == 0     # did NOT tear the live session down
    assert calls["wrap"] == 1           # job-tracking wrap re-applied to the reused session


def test_delegated_reconnects_on_grant_change(monkeypatch):
    mgr, conn, calls = _delegated_mgr(monkeypatch, grant_on_conn="OLD-GRANT", alive=True)
    res = mgr.bind_delegation_grant_and_connect(
        grant="NEW-GRANT", token_endpoint="t", client_id="c", client_secret="s",
    )
    assert res["g"] == {"status": "connected"}
    assert calls["disconnect"] == 1 and calls["connect"] == 1  # fresh session for the new grant
    assert conn.delegation_grant == "NEW-GRANT"


def test_delegated_reconnects_on_dead_same_grant_session(monkeypatch):
    mgr, conn, calls = _delegated_mgr(monkeypatch, grant_on_conn="GRANT-A", alive=False)
    res = mgr.bind_delegation_grant_and_connect(
        grant="GRANT-A", token_endpoint="t", client_id="c", client_secret="s",
    )
    assert res["g"] == {"status": "connected"}
    assert calls["disconnect"] == 1 and calls["connect"] == 1  # dead session rebuilt
