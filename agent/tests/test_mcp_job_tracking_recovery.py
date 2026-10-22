"""Regression guard (Req 12.10/12.12): wrapper-triggered recovery must NOT drop
async-job tracking.

A gateway rebuilds ``_all_tools`` from scratch on every connect/refresh (and a
tool's own resilient ``stream`` can trigger a reconnect). If a recovery path
rebuilt the tool list but forgot to re-apply the job-tracking wrap, trio tools
would come back UNWRAPPED and async jobs would silently stop being registered —
with no error. These tests drive the real ``MCPManager`` recovery entrypoints
and assert the re-wrap runs over the freshly-rebuilt tool list.

The leaf ``wrap_trio_tools`` (which wraps individual MCPAgentTools) is spied,
not exercised — its per-tool behavior is orthogonal to the invariant here, which
is "does recovery re-invoke the wrap over the current tool list".
"""

from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcp_manager import MCPManager  # noqa: E402

_FRESH = ["unwrapped-start-tool"]  # what a reconnect/refresh rebuilds (unwrapped)


class _FakeGW:
    """Models a connected gateway with one job trio and a rebuildable tool list."""

    def __init__(self):
        self.name = "gw1"
        self._connected = True
        self._all_tools = list(_FRESH)
        # Non-empty trios is all _apply_job_tracking_wrap inspects; the trio's
        # internals are wrap_trio_tools' concern (spied below).
        self.job_detection = types.SimpleNamespace(trios=[object()])
        self.reconnect = lambda: None
        self.connect_calls = 0
        self.refresh_calls = 0

    @property
    def is_connected(self):
        return self._connected

    def connect(self):
        self.connect_calls += 1
        self._all_tools = list(_FRESH)  # rebuilt fresh & UNWRAPPED
        return {"status": "connected"}

    def refresh_tools(self):
        self.refresh_calls += 1
        self._all_tools = list(_FRESH)  # rebuilt fresh & UNWRAPPED
        return {"status": "ok"}


def _install_spy(monkeypatch):
    calls: list = []

    def spy(tools, detection, **kwargs):
        calls.append((list(tools), detection, kwargs))
        return ["WRAPPED"]

    # _apply_job_tracking_wrap lazily imports the symbol from this module, so
    # patch it at the source.
    monkeypatch.setattr("jobs.strands_tool_wrap.wrap_trio_tools", spy)
    return calls


def _mgr(gw: _FakeGW) -> MCPManager:
    m = MCPManager()
    m._gateways = {"gw1": gw}
    return m


def _enable(m: MCPManager):
    m.set_job_tracking(
        object(),  # a non-None registry sentinel is all that's required
        actor_id_resolver=lambda: "actor",
        session_id_resolver=lambda: "session",
    )


def test_enabling_tracking_wraps_already_connected_gateway(monkeypatch):
    calls = _install_spy(monkeypatch)
    gw = _FakeGW()
    _enable(_mgr(gw))
    assert calls, "set_job_tracking must wrap already-connected gateways"
    assert gw._all_tools == ["WRAPPED"]


def test_refresh_rewraps_so_recovery_does_not_drop_tracking(monkeypatch):
    calls = _install_spy(monkeypatch)
    gw = _FakeGW()
    m = _mgr(gw)
    _enable(m)

    calls.clear()
    m.refresh_gateway("gw1")

    assert gw.refresh_calls == 1
    assert calls, "refresh_gateway must re-apply the job-tracking wrap"
    passed_tools, passed_detection, _ = calls[-1]
    assert passed_tools == _FRESH, "re-wrap must run over the freshly-rebuilt tool list"
    assert passed_detection is gw.job_detection
    assert gw._all_tools == ["WRAPPED"], "recovery must leave tools RE-wrapped, not dropped"


def test_reconnect_via_connect_gateway_rewraps(monkeypatch):
    calls = _install_spy(monkeypatch)
    gw = _FakeGW()
    m = _mgr(gw)
    _enable(m)

    calls.clear()
    m.connect_gateway("gw1")

    assert gw.connect_calls == 1
    assert calls, "connect_gateway must re-apply the job-tracking wrap"
    assert gw._all_tools == ["WRAPPED"]


def test_no_registry_means_no_wrap_and_no_crash(monkeypatch):
    calls = _install_spy(monkeypatch)
    gw = _FakeGW()
    m = _mgr(gw)  # set_job_tracking NOT called -> registry stays None

    m.refresh_gateway("gw1")

    assert not calls, "without a registry the wrap must not run"
    assert gw._all_tools == _FRESH, "tools untouched when tracking is disabled"
