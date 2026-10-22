"""Re-lease a stale MCP tool onto its connection's CURRENT client — same credential only.

Session 36685185 (task runtime, 02:22:19): ``bind_delegation_grant_and_connect`` rebuilt the
'Your MCP servers' session because the delegated MACHINE token reached its refresh deadline. The
grant — and so the actor — was unchanged, but every node agent still held tool objects bound to
the old ``MCPClient``. ``call_lease`` correctly refused them ("auth context changed before tool
execution") and, because nothing rebound the tool, it refused every later call too: 125 failures
and a plan recovery that could never succeed.

Fix: on that one error, before anything is sent, ask the connection for its current client
pinned to the fingerprint the tool was wrapped under. Same grant / same user token -> rebind and
retry once. A different credential (another invocation's token or grant), a disconnected gateway,
or a second refusal -> surface the ORIGINAL error (fail closed, as before).
"""
from __future__ import annotations

import asyncio
import threading
import time
from contextlib import nullcontext

import pytest

import jobs.strands_tool_wrap as stw
from mcp_manager import GatewayConnection

_LEASE_MSG = "Gateway 'Your MCP servers' auth context changed before tool execution"


def _drain(agen):
    async def _c():
        return [x async for x in agen]
    return asyncio.run(_c())


class _FakeBase:
    """Stands in for ``MCPAgentTool.stream``; records the client each call ran on."""

    ran_on: list = []

    async def stream(self, tool_use, invocation_state, **kwargs):
        _FakeBase.ran_on.append(self.mcp_client)
        yield f"OK:{self.mcp_client}"


@pytest.fixture
def fake_base(monkeypatch):
    _FakeBase.ran_on = []
    monkeypatch.setattr(stw, "MCPAgentTool", _FakeBase)
    monkeypatch.delenv("ARTIFACTS_CDN_PRIVATE", raising=False)
    return _FakeBase


def _tool(*, client, call_lease, relink, reconnect=None):
    class Tool(stw._ResilientMCPMixin, _FakeBase):
        pass

    t = Tool()
    t.mcp_client = client
    t.tool_name = "usr-x___get_job_result"
    t._call_lease = call_lease
    t._relink = relink
    t._reconnect = reconnect
    return t


# ---------------------------------------------------------------------------
# classifier
# ---------------------------------------------------------------------------

def test_classifier_matches_only_the_lease_refusal():
    assert stw._is_auth_context_changed_error(RuntimeError(_LEASE_MSG))
    # reconnect()'s own cross-credential refusal must NOT be treated as relinkable
    assert not stw._is_auth_context_changed_error(RuntimeError(
        "Gateway 'g' auth context changed; refusing to retry under another invocation's credential"))
    assert not stw._is_auth_context_changed_error(ValueError(_LEASE_MSG))
    assert not stw._is_auth_context_changed_error(RuntimeError("HTTP 403 Forbidden"))


# ---------------------------------------------------------------------------
# wrapper behaviour with a scripted connection
# ---------------------------------------------------------------------------

class _ScriptedConn:
    """Lease admits only ``current``; relink returns ``relink_to`` (None = credential differs)."""

    def __init__(self, current, relink_to):
        self.current = current
        self.relink_to = relink_to
        self.relinks = 0

    def call_lease(self, client):
        if client is not self.current:
            raise RuntimeError(_LEASE_MSG)
        return nullcontext()

    def relink(self):
        self.relinks += 1
        return self.relink_to


def test_auth_change_under_same_credential_relinks_and_retries_once(fake_base):
    conn = _ScriptedConn(current="new-client", relink_to="new-client")
    reconnects = []
    t = _tool(client="old-client", call_lease=conn.call_lease, relink=conn.relink,
              reconnect=lambda: reconnects.append(1))

    assert _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {})) == ["OK:new-client"]
    assert conn.relinks == 1
    assert fake_base.ran_on == ["new-client"]   # never ran on the stale client
    assert t.mcp_client == "new-client"         # later calls no longer fail (the 125x loop)
    assert reconnects == []                     # a relink is not a teardown

    assert _drain(t._resilient_base_stream({"toolUseId": "u2"}, {}, {})) == ["OK:new-client"]
    assert conn.relinks == 1                    # second call needed no relink at all


def test_different_credential_surfaces_the_original_error(fake_base):
    conn = _ScriptedConn(current="other-actor-client", relink_to=None)
    t = _tool(client="old-client", call_lease=conn.call_lease, relink=conn.relink)
    with pytest.raises(RuntimeError, match="auth context changed before tool execution"):
        _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {}))
    assert fake_base.ran_on == []               # nothing ran under the other credential
    assert t.mcp_client == "old-client"


def test_second_refusal_surfaces_the_original_error(fake_base):
    # relink hands back a client the lease then refuses again (e.g. another rebuild raced it)
    conn = _ScriptedConn(current="newest-client", relink_to="new-client")
    t = _tool(client="old-client", call_lease=conn.call_lease, relink=conn.relink)
    with pytest.raises(RuntimeError) as ei:
        _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {}))
    assert str(ei.value) == _LEASE_MSG
    assert conn.relinks == 1                    # exactly once — no loop
    assert fake_base.ran_on == []


def test_original_error_object_is_the_one_surfaced(fake_base):
    first = RuntimeError(_LEASE_MSG + " (first)")
    calls = {"n": 0}

    def lease(client):
        calls["n"] += 1
        if calls["n"] == 1:
            raise first
        raise RuntimeError(_LEASE_MSG + " (second)")

    t = _tool(client="old", call_lease=lease, relink=lambda: "new")
    with pytest.raises(RuntimeError) as ei:
        _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {}))
    assert ei.value is first


def test_no_relink_callback_keeps_old_fail_closed_behaviour(fake_base):
    conn = _ScriptedConn(current="new-client", relink_to="new-client")
    t = _tool(client="old-client", call_lease=conn.call_lease, relink=None,
              reconnect=lambda: "should-not-be-used")
    with pytest.raises(RuntimeError, match="auth context changed before tool execution"):
        _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {}))
    assert fake_base.ran_on == []


def test_relink_lookup_exception_surfaces_original(fake_base):
    def relink():
        raise OSError("boom")

    conn = _ScriptedConn(current="new-client", relink_to=None)
    t = _tool(client="old-client", call_lease=conn.call_lease, relink=relink)
    with pytest.raises(RuntimeError, match="auth context changed before tool execution"):
        _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {}))


def test_wrap_helpers_plumb_relink(fake_base):
    from strands.tools.mcp.mcp_agent_tool import MCPAgentTool as RealMCPAgentTool

    class _MCPTool:
        name = "submit_job"
        description = ""
        inputSchema = {"type": "object", "properties": {}}
        outputSchema = None

    real = RealMCPAgentTool(_MCPTool(), mcp_client=object())
    marker = lambda: None  # noqa: E731
    stw.MCPAgentTool = RealMCPAgentTool  # wrap_resilient's isinstance check needs the real class
    try:
        [wrapped] = stw.wrap_resilient([real], reconnect=None, call_lease=None, relink=marker)
    finally:
        stw.MCPAgentTool = _FakeBase
    assert wrapped._relink is marker


# ---------------------------------------------------------------------------
# GatewayConnection.current_client_for_auth — the safety gate itself
# ---------------------------------------------------------------------------

def _gw(auth_type="user_jwt") -> GatewayConnection:
    return GatewayConnection("harness-user-mcp", {"name": "Your MCP servers",
                                                  "url": "https://gw.example/mcp",
                                                  "auth_type": auth_type})


def _connect_grant(gw, client, grant):
    gw._client = client
    gw._connected = True
    gw._bound_user_token = ""
    gw.delegation_grant = grant


def test_same_grant_rebuilt_client_is_returned():
    gw = _gw()
    _connect_grant(gw, "client-A", "grant-1")
    relink = gw.relink_for_current_auth()
    _connect_grant(gw, "client-B", "grant-1")   # machine-token refresh: new client, same grant
    assert relink() == "client-B"


def test_different_grant_or_user_token_is_refused():
    gw = _gw()
    _connect_grant(gw, "client-A", "grant-1")
    relink = gw.relink_for_current_auth()
    _connect_grant(gw, "client-B", "grant-2")   # another plan / actor's grant
    assert relink() is None
    _connect_grant(gw, "client-C", "")
    gw._bound_user_token = "browser-token"      # switched to an interactive user token
    assert relink() is None


def test_user_token_mode_same_token_ok_new_token_refused():
    gw = _gw()
    gw._client, gw._connected, gw._bound_user_token = "A", True, "tok-1"
    relink = gw.relink_for_current_auth()
    gw._client = "B"
    assert relink() == "B"
    gw._client, gw._bound_user_token = "C", "tok-2"
    assert relink() is None


def test_disconnected_or_unpinned_user_jwt_is_refused():
    gw = _gw()
    assert gw.relink_for_current_auth()() is None          # never connected: fingerprint None
    _connect_grant(gw, "client-A", "grant-1")
    relink = gw.relink_for_current_auth()
    gw._connected = False
    assert relink() is None


def test_shared_credential_gateway_relinks_on_none_fingerprint():
    gw = _gw(auth_type="sigv4")
    gw._client, gw._connected = "A", True
    relink = gw.relink_for_current_auth()
    gw._client = "B"
    assert relink() == "B"


def test_waits_for_an_inflight_transition_then_returns_the_new_client():
    gw = _gw()
    _connect_grant(gw, "client-A", "grant-1")
    relink = gw.relink_for_current_auth()
    entered = threading.Event()

    def rebuild():
        with gw._auth_transition():
            entered.set()
            time.sleep(0.2)
            _connect_grant(gw, "client-B", "grant-1")

    th = threading.Thread(target=rebuild)
    th.start()
    entered.wait(2)
    assert relink() == "client-B"                 # waited out the switch, saw the new client
    th.join()


def test_transition_that_outlasts_the_wait_is_refused():
    gw = _gw()
    _connect_grant(gw, "client-A", "grant-1")
    expected = gw.auth_fingerprint
    with gw._call_state:
        gw._auth_switching = True
    try:
        assert gw.current_client_for_auth(expected, timeout=0.05) is None
    finally:
        with gw._call_state:
            gw._auth_switching = False


def test_end_to_end_real_lease_same_grant_then_other_grant(fake_base):
    """Real ``call_lease`` + ``relink_for_current_auth`` + the wrapper."""
    gw = _gw()
    _connect_grant(gw, "client-A", "grant-1")
    t = _tool(client="client-A", call_lease=gw.call_lease, relink=gw.relink_for_current_auth())

    _connect_grant(gw, "client-B", "grant-1")     # the 02:22:19 rebuild
    assert _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {})) == ["OK:client-B"]
    assert gw._active_calls == 0

    _connect_grant(gw, "client-C", "grant-2")     # now a DIFFERENT credential
    with pytest.raises(RuntimeError, match="auth context changed before tool execution"):
        _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {}))
    assert fake_base.ran_on == ["client-B"]       # never ran under grant-2
    assert gw._active_calls == 0
