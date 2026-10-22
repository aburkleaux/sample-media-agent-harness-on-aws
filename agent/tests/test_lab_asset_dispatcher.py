"""The Lab authoring API is reached through one tool, not 31.

The 31 wrappers were each bound on the Main Agent, so every conversation carried
the whole authoring surface — 73% of the builtin tool COUNT — including turns
that would never publish an asset. Each extra tool is another candidate the model
weighs when selecting.

Collapsed rather than mode-gated on purpose: a tool set that varies per turn
invalidates the cached prompt prefix, trading ~12KB of schema for a ~19k-token
prefix rewrite. The dispatcher is static.

Because `params` is a dict rather than a typed union, the guarantees a schema
would have given are asserted here instead: unknown actions, missing arguments
and unexpected arguments must all be reported rather than silently mis-called.
"""
import json
import sys

import pytest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import main_lab_tools as mlt  # noqa: E402

lab_asset = mlt.lab_asset.__wrapped__  # call the implementation directly


class TestSurface:
    def test_one_tool_is_bound(self):
        assert [t.tool_spec["name"] for t in mlt.LAB_TOOLS] == ["lab_asset"]

    def test_every_wrapper_is_reachable_as_an_action(self):
        """A wrapper absent from the registry would be a silently lost capability."""
        assert set(mlt._LAB_ACTIONS) == {
            t.tool_spec["name"] for t in mlt._INDIVIDUAL_LAB_TOOLS
        }

    def test_registry_is_derived_not_hand_listed(self):
        src = (SRC / "main_lab_tools.py").read_text()
        assert "_build_lab_action_registry" in src
        assert "__wrapped__" in src

    def test_it_is_much_smaller_than_what_it_replaced(self):
        one = len(json.dumps(mlt.lab_asset.tool_spec, separators=(",", ":")))
        many = sum(
            len(json.dumps(t.tool_spec, separators=(",", ":")))
            for t in mlt._INDIVIDUAL_LAB_TOOLS
        )
        assert one < many / 5, f"{one} vs {many}"

    def test_docstring_advertises_only_real_actions(self):
        """It once named persist_workflow, which is a separate persistence tool."""
        import re
        doc = mlt.lab_asset.tool_spec.get("description", "")
        named = set(re.findall(
            r"\b(?:list|create|read|update|delete|validate|publish|edit|discard|persist|confirm)_[a-z_]+\b",
            doc))
        assert not (named - set(mlt._LAB_ACTIONS))


class TestDescribe:
    async def _call(self, **kw):
        return json.loads(await lab_asset(**kw))

    @pytest.mark.asyncio
    async def test_lists_every_action_with_its_params(self):
        out = await self._call(action="describe")
        assert set(out["actions"]) == set(mlt._LAB_ACTIONS)
        assert "params" in out["actions"]["create_skill_draft"]

    @pytest.mark.asyncio
    async def test_describes_one_action(self):
        out = await self._call(action="describe", params={"action": "create_skill_draft"})
        assert out["action"] == "create_skill_draft"
        assert set(out["params"]) == {"name", "skill_md"}
        assert out["params"]["name"]["required"] is True

    @pytest.mark.asyncio
    async def test_describing_an_unknown_action_lists_the_valid_ones(self):
        out = await self._call(action="describe", params={"action": "nope"})
        assert "error" in out and "create_skill_draft" in out["actions"]


class TestValidation:
    async def _call(self, **kw):
        return json.loads(await lab_asset(**kw))

    @pytest.mark.asyncio
    async def test_unknown_action_is_rejected_with_the_valid_list(self):
        out = await self._call(action="frobnicate")
        assert "unknown action" in out["error"]
        assert "publish_asset" in out["actions"]

    @pytest.mark.asyncio
    async def test_missing_required_argument_is_named(self):
        out = await self._call(action="create_skill_draft", params={"name": "x"})
        assert out["missing"] == ["skill_md"]
        assert "params" in out  # what a schema would have told the model

    @pytest.mark.asyncio
    async def test_unexpected_argument_is_named(self):
        out = await self._call(
            action="create_skill_draft",
            params={"name": "x", "skill_md": "y", "colour": "blue"})
        assert out["unknown"] == ["colour"]

    @pytest.mark.asyncio
    async def test_no_params_at_all_still_reports_what_is_needed(self):
        out = await self._call(action="create_skill_draft")
        assert out["missing"] == ["name", "skill_md"]

    @pytest.mark.asyncio
    async def test_a_no_argument_action_needs_no_params(self, monkeypatch):
        """list_skill_drafts takes nothing — it must not demand a params dict."""
        called = {}

        async def fake():
            called["yes"] = True
            return json.dumps({"ok": True})

        monkeypatch.setitem(mlt._LAB_ACTIONS["list_skill_drafts"], "fn", fake)
        monkeypatch.setitem(mlt._LAB_ACTIONS["list_skill_drafts"], "params", {})
        out = await self._call(action="list_skill_drafts")
        assert called and out == {"ok": True}


class TestRouting:
    @pytest.mark.asyncio
    async def test_valid_call_reaches_the_wrapper_with_its_arguments(self, monkeypatch):
        seen = {}

        async def fake(name: str, skill_md: str):
            seen.update(name=name, skill_md=skill_md)
            return json.dumps({"assetId": "a1"})

        monkeypatch.setitem(mlt._LAB_ACTIONS["create_skill_draft"], "fn", fake)
        out = json.loads(await lab_asset(
            action="create_skill_draft", params={"name": "n", "skill_md": "# md"}))
        assert seen == {"name": "n", "skill_md": "# md"}
        assert out == {"assetId": "a1"}
