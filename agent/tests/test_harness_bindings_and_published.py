"""Two harness defects that made it quietly do the wrong thing rather than fail.

1. The UI sends camelCase binding keys (`agentAssetId`); the execution paths read
   snake_case (`specialty_asset_id`). Nothing mapped between them, so choosing an
   agent to compose with a skill added NO persona layer and raised nothing — the run
   reported success having ignored the binding.

2. The target lookup searched only the OWNED (drafts) partition. Publishing consumes
   the working copy, so pressing Test on anything shipped failed with "Target asset
   not found" — the normal case for a published asset, not an edge one.

Kept apart from `test_test_harness.py` because these are regression cases with their
own fixtures rather than additions to that file's suites.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add agent/src to path so imports resolve, as the sibling harness tests do.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.test_harness import TestHarness, normalise_bindings  # noqa: E402
from lab.context import LabRequestContext  # noqa: E402
from lab.models import SampleRef, Skill, SpecialtyAgent  # noqa: E402

ACTOR = "actor-123"


def _ctx() -> LabRequestContext:
    return LabRequestContext(actor_id=ACTOR, session_id="sess-1", request_id="req-1")


def _s3(read_returns=None, read_side_effect=None):
    """Mock S3UserFilesTool. The harness reads artifacts through the bucket-absolute
    methods, so those are the ones that must be wired."""
    s3 = MagicMock()
    s3.actor_id = ACTOR
    s3.write = AsyncMock()
    s3.list = AsyncMock(return_value=[])
    s3.read = AsyncMock(return_value=read_returns, side_effect=read_side_effect)
    s3.read_absolute = s3.read
    s3.write_absolute = s3.write
    s3.list_absolute = s3.list
    return s3


def _index(get_returns=None, lineage=None):
    index = MagicMock()
    index.get = AsyncMock(return_value=get_returns)
    index.put = AsyncMock(return_value=None)
    index.update = AsyncMock(return_value=None)
    index.query_pk = AsyncMock(return_value=([], None))
    index.transact_write = AsyncMock(return_value=None)
    index.query_lineage = AsyncMock(return_value=list(lineage or []))
    return index


def _draft_skill(asset_id: str = "skill-1") -> Skill:
    return Skill(
        asset_id=asset_id,
        category="skill",
        name="wip-skill",
        status="draft",
        scope="private",
        s3_prefix=f"users/{ACTOR}/drafts/skills/{asset_id}/",
        created_at=1700000000000,
        updated_at=1700000000000,
    )


def _published_skill(asset_id: str = "skill-1", version: int = 2) -> Skill:
    return Skill(
        asset_id=asset_id,
        category="skill",
        name="shipped-skill",
        status="published",
        scope="private",
        version=version,
        current=True,
        s3_prefix=f"users/{ACTOR}/published/skills/{asset_id}/v{version}/",
        created_at=1700000000000,
        updated_at=1700000000000,
    )


class TestNormaliseBindings:
    """Every accepted spelling has to reach the snake_case readers."""

    def test_maps_the_ui_spellings(self):
        assert normalise_bindings({
            "agentAssetId": "a1",
            "skillAssetId": "s1",
            "toolCollectionAssetId": "t1",
        }) == {
            "specialty_asset_id": "a1",
            "skill_asset_id": "s1",
            "tool_collection_asset_id": "t1",
        }

    def test_passes_canonical_keys_through(self):
        canonical = {"specialty_asset_id": "a1", "skill_asset_id": "s1"}
        assert normalise_bindings(canonical) == canonical

    def test_drops_empty_selections(self):
        # The UI's "None" option submits "", which must mean "no binding" rather than
        # a lookup for the asset whose id is the empty string.
        assert normalise_bindings({"agentAssetId": "", "skillAssetId": None}) == {}

    def test_handles_absent_bindings(self):
        assert normalise_bindings(None) == {}
        assert normalise_bindings({}) == {}


@pytest.mark.asyncio
class TestAgentBindingComposesThePersona:
    async def test_ui_spelling_adds_the_persona_layer(self):
        """The whole point of binding an agent: a P layer in the composed prompt.

        Asserted on the prompt actually dispatched, not on the binding dict, because
        the bug was precisely that a well-formed binding produced a prompt without it.
        """
        skill = _draft_skill()
        specialty = SpecialtyAgent(
            asset_id="agent-1",
            category="specialty-agent",
            name="test-agent",
            status="draft",
            scope="private",
            s3_prefix=f"users/{ACTOR}/drafts/specialty-agents/agent-1/",
            created_at=1700000000000,
            updated_at=1700000000000,
            compatible_skills=["skill-1"],
        )
        index = _index(get_returns=specialty)
        s3 = _s3(read_side_effect=[b"# Skill body", b"You are a persona."])

        harness = TestHarness(s3=s3, index=index)
        with patch.object(harness, "_invoke_task_agent", AsyncMock(return_value={})) as invoke:
            await harness._execute_skill(
                ctx=_ctx(),
                asset_record=skill,
                sample_input=SampleRef(kind="inline", value="do the thing"),
                bindings=normalise_bindings({"agentAssetId": "agent-1"}),
                working_dir="/tmp/run",
            )

        composed = invoke.await_args.args[0]
        assert "You are a persona." in composed, "persona (P) layer missing"
        assert "# Skill body" in composed, "skill (K) layer missing"
        assert "do the thing" in composed, "sample input (I) layer missing"

    async def test_no_binding_still_composes_without_a_persona(self):
        """The unbound case must stay R+K+I — the fix must not invent a persona."""
        skill = _draft_skill()
        harness = TestHarness(s3=_s3(read_returns=b"# Skill body"), index=_index())

        with patch.object(harness, "_invoke_task_agent", AsyncMock(return_value={})) as invoke:
            await harness._execute_skill(
                ctx=_ctx(),
                asset_record=skill,
                sample_input=SampleRef(kind="inline", value="go"),
                bindings=normalise_bindings(None),
                working_dir="/tmp/run",
            )

        composed = invoke.await_args.args[0]
        assert "# Skill body" in composed
        assert "persona" not in composed.lower()


@pytest.mark.asyncio
class TestTargetingAPublishedAsset:
    async def test_falls_back_to_the_published_version(self):
        published = _published_skill()
        index = _index(get_returns=None, lineage=[published])  # no working copy
        harness = TestHarness(s3=_s3(), index=index)

        found = await harness._lookup_asset(_ctx(), "skill-1")

        assert found is not None
        assert found.version == 2

    async def test_prefers_a_working_copy_when_one_exists(self):
        """The fallback must not change what Test does while a draft is present —
        that is the copy you are editing, and the one you mean to test."""
        draft = _draft_skill()
        published = _published_skill()
        index = _index(get_returns=draft, lineage=[published])
        harness = TestHarness(s3=_s3(), index=index)

        found = await harness._lookup_asset(_ctx(), "skill-1")

        assert found is not None
        assert found.status == "draft"
        assert getattr(found, "version", None) is None

    async def test_still_reports_a_genuinely_missing_asset(self):
        harness = TestHarness(s3=_s3(), index=_index(get_returns=None, lineage=[]))
        assert await harness._lookup_asset(_ctx(), "nope") is None

    async def test_reads_skill_md_from_the_published_prefix(self):
        """The artifact read follows the row's OWN prefix. Deriving the drafts key —
        as it used to — cannot reach a published version at all."""
        published = _published_skill()
        s3 = _s3(read_returns=b"# Published body")
        harness = TestHarness(s3=s3, index=_index())

        with patch.object(harness, "_invoke_task_agent", AsyncMock(return_value={})):
            await harness._execute_skill(
                ctx=_ctx(),
                asset_record=published,
                sample_input=SampleRef(kind="inline", value="go"),
                bindings={},
                working_dir="/tmp/run",
            )

        assert s3.read.await_args.args[0] == (
            f"users/{ACTOR}/published/skills/skill-1/v2/SKILL.md"
        )

    async def test_reads_a_draft_from_the_draft_prefix(self):
        """Same read path, other branch — the draft case must be unchanged."""
        draft = _draft_skill()
        s3 = _s3(read_returns=b"# Draft body")
        harness = TestHarness(s3=s3, index=_index())

        with patch.object(harness, "_invoke_task_agent", AsyncMock(return_value={})):
            await harness._execute_skill(
                ctx=_ctx(),
                asset_record=draft,
                sample_input=SampleRef(kind="inline", value="go"),
                bindings={},
                working_dir="/tmp/run",
            )

        assert s3.read.await_args.args[0] == (
            f"users/{ACTOR}/drafts/skills/skill-1/SKILL.md"
        )
