"""Retrying a user_jwt gateway once a token arrives.

Gateway loading is latched once per container; the user's token arrives per
invocation and not on every invocation. Live consequence: opening the MCP panel
cold-started a container with no token, the user_jwt gateway failed for want of a
credential, and the latch meant the chat turn that DID forward a token never
retried -- the agent reported it had no such tools while the target was healthy.
"""
import sys, os
import threading
from contextlib import nullcontext
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from mcp_manager import MCPManager, _describe_exc


class FakeGW:
    def __init__(self, auth_type, connected=False, fail=None):
        self.auth_type = auth_type
        self._connected = connected
        self.user_token = ""
        self._bound_user_token = ""
        self._connect_lock = threading.RLock()
        self.fail = fail
        self.connect_calls = 0

    @property
    def is_connected(self):
        return self._connected

    def _auth_transition(self):
        return nullcontext()

    def connect(self):
        self.connect_calls += 1
        if self.fail:
            raise self.fail
        self._connected = True
        self._bound_user_token = self.user_token
        return {"status": "connected", "tools": ["a", "b"]}

    def disconnect(self):
        self._connected = False
        self._bound_user_token = ""
        return True


def _mgr(**gws):
    m = MCPManager.__new__(MCPManager)
    m._gateways = dict(gws)
    m._apply_job_tracking_wrap = lambda gw: None
    return m


class TestRetryOnTokenArrival:
    def test_connects_the_user_jwt_gateway_when_a_token_arrives(self):
        user = FakeGW("user_jwt")
        m = _mgr(harness=user)
        res = m.bind_user_token_and_connect("tok-abc")
        assert user.connect_calls == 1
        assert user.user_token == "tok-abc"
        assert res["harness"]["status"] == "connected"

    def test_no_token_is_a_no_op(self):
        """A tokenless management call must not thrash the connection."""
        user = FakeGW("user_jwt")
        m = _mgr(harness=user)
        assert m.bind_user_token_and_connect("") == {}
        assert user.connect_calls == 0

    def test_other_auth_types_are_untouched(self):
        """The media gateway authenticates with its own credential; a user token
        is irrelevant to it and reconnecting it would drop live tools."""
        media = FakeGW("jwt", connected=True)
        m = _mgr(media=media)
        assert m.bind_user_token_and_connect("tok") == {}
        assert media.connect_calls == 0

    def test_already_connected_with_same_bound_token_is_left_alone(self):
        """A live transport already carrying this token is reused."""
        user = FakeGW("user_jwt", connected=True)
        user.user_token = "tok"
        user._bound_user_token = "tok"
        m = _mgr(harness=user)
        assert m.bind_user_token_and_connect("tok") == {}
        assert user.connect_calls == 0

    def test_a_failure_is_reported_not_raised(self):
        """This runs on the invocation path; raising would fail the user's turn
        over a gateway that is merely unavailable."""
        user = FakeGW("user_jwt", fail=RuntimeError("401 Unauthorized"))
        m = _mgr(harness=user)
        res = m.bind_user_token_and_connect("tok")
        assert res["harness"]["status"] == "error"
        assert "401" in res["harness"]["error"]

    def test_one_failing_gateway_does_not_block_another(self):
        bad = FakeGW("user_jwt", fail=RuntimeError("boom"))
        good = FakeGW("user_jwt")
        m = _mgr(bad=bad, good=good)
        res = m.bind_user_token_and_connect("tok")
        assert res["bad"]["status"] == "error"
        assert res["good"]["status"] == "connected"


class TestExceptionDescription:
    def test_taskgroup_sub_exception_is_surfaced(self):
        """'unhandled errors in a TaskGroup (1 sub-exception)' names nothing.
        The sub-exception is the actual cause."""
        g = ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("HTTP 401")])
        assert "HTTP 401" in _describe_exc(g)

    def test_nested_groups_are_flattened(self):
        inner = ExceptionGroup("inner", [ValueError("tls fail")])
        outer = ExceptionGroup("outer", [inner])
        assert "tls fail" in _describe_exc(outer)

    def test_cause_chain_is_included(self):
        try:
            try:
                raise ValueError("dns")
            except ValueError as e:
                raise ConnectionError("transport") from e
        except ConnectionError as e:
            got = _describe_exc(e)
        assert "transport" in got and "dns" in got

    def test_recursion_is_bounded(self):
        """A cycle must not hang container startup."""
        e = RuntimeError("x")
        e.__cause__ = e
        assert isinstance(_describe_exc(e), str)

    def test_bare_exception_still_named(self):
        assert "RuntimeError" in _describe_exc(RuntimeError())
