"""Task 1 — Hook asset model round-trip tests.

Validates that a ``Hook`` asset serializes to the AssetIndex DDB item shape
and reconstructs losslessly, and that the new ``"hook"`` category dispatches
to the ``Hook`` dataclass.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.models import Hook, from_index_item, to_index_item  # noqa: E402


def _make_hook(**overrides) -> Hook:
    now = int(time.time() * 1000)
    base = dict(
        asset_id="hk-1",
        category="hook",
        name="render-on-results",
        status="draft",
        scope="private",
        s3_prefix="USER#u-jane/hook/hk-1/",
        created_at=now,
        updated_at=now,
        event="after_tool_result",
        condition='tool.role == "results" and result.urls',
        action_type="render",
        action_params={"urls": "result.urls"},
        priority=50,
        enabled=True,
        builtin=True,
        hook_scope=None,
    )
    base.update(overrides)
    return Hook(**base)


def test_hook_roundtrip_preserves_fields():
    hook = _make_hook()
    item = to_index_item(hook, owner_user_id="u-jane", owner_group_id=None, owner_org_id=None)

    assert item["category"] == "hook"
    assert item["event"] == "after_tool_result"
    assert item["condition"] == 'tool.role == "results" and result.urls'
    assert item["action"] == {"type": "render", "params": {"urls": "result.urls"}}
    assert item["priority"] == 50
    assert item["enabled"] is True
    assert item["builtin"] is True
    assert "hookScope" not in item  # None omitted

    restored = from_index_item(item)
    assert isinstance(restored, Hook)
    assert restored.event == hook.event
    assert restored.condition == hook.condition
    assert restored.action_type == "render"
    assert restored.action_params == {"urls": "result.urls"}
    assert restored.priority == 50
    assert restored.enabled is True
    assert restored.builtin is True
    assert restored.hook_scope is None


def test_hook_scope_roundtrips_when_present():
    hook = _make_hook(hook_scope={"skill": "video-processing"}, builtin=False, priority=100)
    item = to_index_item(hook, owner_user_id="u-jane", owner_group_id=None, owner_org_id=None)
    assert item["hookScope"] == {"skill": "video-processing"}

    restored = from_index_item(item)
    assert isinstance(restored, Hook)
    assert restored.hook_scope == {"skill": "video-processing"}
    assert restored.builtin is False


def test_hook_defaults_on_minimal_item():
    # A minimal item (missing optional hook attrs) should deserialize to sane defaults.
    item = {
        "assetId": "hk-2",
        "category": "hook",
        "name": "bare",
        "status": "draft",
        "scope": "private",
        "s3Prefix": "USER#u/hook/hk-2/",
        "createdAt": 1,
        "updatedAt": 1,
    }
    restored = from_index_item(item)
    assert isinstance(restored, Hook)
    assert restored.event == ""
    assert restored.action_type == "notify"
    assert restored.priority == 100
    assert restored.enabled is True
    assert restored.builtin is False
