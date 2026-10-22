"""build_engine name-override: users can customize or DISABLE a built-in hook."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hooks.loader import build_engine  # noqa: E402


def _hook(name, enabled, *, event="after_tool_result", condition="result.mimes",
          action_type="notify", action_params=None, priority=100):
    return SimpleNamespace(
        name=name, enabled=enabled, event=event, condition=condition,
        action_type=action_type, action_params=action_params or {}, priority=priority,
    )


def _results_ctx():
    return {
        "tool": {"role": "results", "name": "x___get_diarization_results"},
        "result": {"urls": ["https://cf/x.wav"], "mimes": ["audio/wav"], "new_artifacts": ["https://cf/x.wav"]},
        "run_id": "",
    }


def test_builtin_render_artifacts_present_by_default():
    eng = build_engine([])
    matched = eng.matching("after_tool_result", _results_ctx())
    assert any(s.name == "render-artifacts" for s in matched)


def test_user_disabled_same_name_turns_builtin_off():
    # A user hook named exactly like the built-in, disabled → built-in OFF.
    dis = _hook("render-artifacts", enabled=False, action_type="render")
    eng = build_engine([dis])
    assert eng.matching("after_tool_result", _results_ctx()) == []


def test_user_enabled_same_name_overrides_builtin():
    # A same-named ENABLED user hook replaces the built-in (its spec is used).
    override = _hook("render-artifacts", enabled=True, action_type="notify", action_params={"message": "custom"})
    eng = build_engine([override])
    matched = eng.matching("after_tool_result", _results_ctx())
    names = [s.name for s in matched]
    assert names == ["render-artifacts"]
    assert matched[0].action_type == "notify"  # the user's version, not the built-in render


def test_unique_user_hook_added_alongside_default():
    u = _hook("my-notify", enabled=True, action_type="notify")
    eng = build_engine([u])
    names = {s.name for s in eng.matching("after_tool_result", _results_ctx())}
    assert "render-artifacts" in names and "my-notify" in names
