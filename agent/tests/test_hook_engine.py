"""Task 3 — hook engine + notify action tests."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orchestration import progress_channel  # noqa: E402
from hooks.actions import ActionResult, register_action  # noqa: E402
from hooks.engine import HookEngine, HookSpec  # noqa: E402


def _notify(name, event, condition, priority=100, enabled=True, message="hi"):
    return HookSpec(
        name=name, event=event, condition=condition,
        action_type="notify", action_params={"message": message},
        priority=priority, enabled=enabled,
    )


def _fresh_channel():
    ch = progress_channel.ProgressChannel()
    progress_channel.set_active_channel(ch)
    return ch


def test_notify_fires_on_matching_event_and_pushes_step():
    ch = _fresh_channel()
    eng = HookEngine([_notify("h1", "after_tool_result", 'tool.role == "results"', message="rendered {tool.role}")])
    results = eng.fire("after_tool_result", {"tool": {"role": "results"}})
    assert [r.status for r in results] == ["ok"]
    events = ch.drain()
    assert len(events) == 1
    assert events[0].agent == "hook"
    assert "rendered results" in events[0].label
    progress_channel.set_active_channel(None)


def test_no_fire_on_nonmatching_event():
    _fresh_channel()
    eng = HookEngine([_notify("h1", "after_tool_result", "")])
    assert eng.fire("before_tool", {}) == []
    progress_channel.set_active_channel(None)


def test_disabled_hook_does_not_fire():
    _fresh_channel()
    eng = HookEngine([_notify("h1", "session_start", "", enabled=False)])
    assert eng.fire("session_start", {}) == []
    progress_channel.set_active_channel(None)


def test_priority_ordering():
    _fresh_channel()
    order = []
    register_action("record", lambda p, c: (order.append(p["tag"]), ActionResult("record", "ok"))[1])
    hooks = [
        HookSpec("late", "e", "", "record", {"tag": "late"}, priority=200),
        HookSpec("early", "e", "", "record", {"tag": "early"}, priority=10),
    ]
    HookEngine(hooks).fire("e", {})
    assert order == ["early", "late"]
    progress_channel.set_active_channel(None)


def test_action_error_is_isolated():
    _fresh_channel()
    def boom(p, c):
        raise RuntimeError("kaboom")
    register_action("boom", boom)
    eng = HookEngine([
        HookSpec("bad", "e", "", "boom", {}, priority=10),
        _notify("good", "e", "", priority=20, message="still runs"),
    ])
    results = eng.fire("e", {})
    assert results[0].status == "error"
    assert results[1].status == "ok"  # later hook still ran
    progress_channel.set_active_channel(None)


def test_bad_condition_is_skipped_not_raised():
    _fresh_channel()
    eng = HookEngine([_notify("h1", "e", "__import__('os')")])  # invalid → skipped
    assert eng.fire("e", {}) == []
    progress_channel.set_active_channel(None)


def test_unknown_action_returns_error_result():
    _fresh_channel()
    eng = HookEngine([HookSpec("h", "e", "", "nonexistent", {})])
    results = eng.fire("e", {})
    assert results[0].status == "error"
    progress_channel.set_active_channel(None)


def test_from_hook_asset_conversion():
    from lab.models import Hook
    hk = Hook(
        asset_id="hk-1", category="hook", name="n", status="draft", scope="private",
        s3_prefix="p/", created_at=1, updated_at=1,
        event="after_tool_result", condition="result.urls",
        action_type="notify", action_params={"message": "m"}, priority=42, enabled=True,
    )
    spec = HookSpec.from_hook_asset(hk)
    assert spec.name == "n" and spec.event == "after_tool_result" and spec.priority == 42
