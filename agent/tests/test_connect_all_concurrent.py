"""``MCPManager.connect_all`` connects gateways side by side, with unchanged semantics.

Cold staging session 89f6be71: four gateways connected one after another took 7.5s -- the SUM of
their OAuth + MCP initialize + tools/list round trips. Concurrently the floor is the slowest one.
"""
from __future__ import annotations

import contextvars
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import mcp_manager as mm  # noqa: E402

_ctx = contextvars.ContextVar("speed_test_ctx", default="unset")


class _FakeGateway:
    def __init__(self, gw_id, *, delay=0.3, fail=False, deferred=False):
        self.id = gw_id
        self.delay = delay
        self.fail = fail
        self.deferred = deferred
        self.seen_ctx = None
        self.thread = None
        self.connected = False

    def awaiting_user_token(self):
        return self.deferred

    def connect(self):
        self.seen_ctx = _ctx.get()
        self.thread = threading.current_thread().name
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError(f"{self.id} down")
        self.connected = True
        return {"status": "connected", "tools": [{"name": f"{self.id}___t"}]}


def _manager(gateways):
    m = mm.MCPManager()
    for gw in gateways:
        m._gateways[gw.id] = gw
    wrapped = []
    m._apply_job_tracking_wrap = lambda gw: wrapped.append(gw.id)
    return m, wrapped


def test_gateways_connect_concurrently():
    gws = [_FakeGateway(f"g{i}", delay=0.3) for i in range(4)]
    m, _ = _manager(gws)
    started = time.monotonic()
    results = m.connect_all()
    elapsed = time.monotonic() - started
    assert all(r["status"] == "connected" for r in results.values())
    # Sequential would be >= 1.2s; concurrent is about one connect.
    assert elapsed < 0.9, f"connect_all took {elapsed:.2f}s -- still sequential?"


def test_results_keep_registry_order_and_isolate_failures():
    gws = [_FakeGateway("slow", delay=0.3), _FakeGateway("broken", delay=0.0, fail=True),
           _FakeGateway("user", deferred=True), _FakeGateway("fast", delay=0.0)]
    m, wrapped = _manager(gws)
    results = m.connect_all()
    assert list(results) == ["slow", "broken", "user", "fast"]
    assert results["slow"]["status"] == "connected"
    assert results["broken"] == {"status": "error", "error": "broken down"}
    assert results["user"] == {"status": "deferred", "reason": "awaiting user token"}
    assert results["fast"]["status"] == "connected"
    # The job-tracking wrap ran for exactly the gateways that connected.
    assert sorted(wrapped) == ["fast", "slow"]


def test_workers_see_the_callers_contextvars():
    """MCPClient seeds its transport thread from the context current at connect."""
    gws = [_FakeGateway("a", delay=0.0), _FakeGateway("b", delay=0.0)]
    m, _ = _manager(gws)
    token = _ctx.set("caller-value")
    try:
        m.connect_all()
    finally:
        _ctx.reset(token)
    assert [g.seen_ctx for g in gws] == ["caller-value", "caller-value"]
    assert all(g.thread.startswith("gw-connect") for g in gws)


def test_single_gateway_connects_inline():
    gw = _FakeGateway("only", delay=0.0)
    m, _ = _manager([gw])
    assert m.connect_all()["only"]["status"] == "connected"
    assert gw.thread == threading.current_thread().name


def test_empty_registry():
    m, _ = _manager([])
    assert m.connect_all() == {}
