"""Smoke tests for the Main / Lab merge.

Verifies three properties of the merged Main Agent surface:
  1. The Lab CRUD tools (defined in main_lab_tools) call through to the
     request-bound LabService.handle.
  2. Direct-CRUD bypass action set covers every LabService action so
     {action: ...} payloads are dispatchable from the Main Agent entrypoint.
  3. delete_*_draft tools refuse without confirm=true.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.asset_index import AssetIndex
from lab.context import LabRequestContext
from lab.gateway_browser import GatewayBrowser
from lab.s3_user_files_tool import S3UserFilesTool
from lab.service import LabService

import main_lab_tools
from skills_manager import SkillsManager


TABLE_NAME = "test-main-lab-merge-index"
BUCKET_NAME = "test-main-lab-merge-bucket"
ACTOR_ID = "actor-merge-1"


class _FakeGatewayClient:
    def list_tools(self, actor_id: str, timeout: float):
        return []


def _create_table(dynamodb):
    dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "byPublishedId",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def lab_service_fixture():
    """Wire a real LabService against moto-mocked AWS."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_table(dynamodb)
        table = dynamodb.Table(TABLE_NAME)

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET_NAME)

        ctx = LabRequestContext(
            actor_id=ACTOR_ID,
            session_id="session-merge",
            request_id=str(uuid.uuid4()),
        )
        s3_tool = S3UserFilesTool(bucket=BUCKET_NAME, actor_id=ACTOR_ID)
        index = AssetIndex(ddb_table=table, actor_id=ACTOR_ID)
        gateway_browser = GatewayBrowser(
            gateway_client=_FakeGatewayClient(),
            configured_endpoints=[],
        )

        yield LabService(ctx=ctx, s3=s3_tool, index=index, gateway_browser=gateway_browser)


# ---------------------------------------------------------------------------
# Property 1 — chat tool path lands on LabService.handle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_skill_draft_tool_calls_lab_service(lab_service_fixture):
    """create_skill_draft tool wrapper routes through LabService and returns a JSON-serialized result."""
    token = main_lab_tools.set_current_lab_service(lab_service_fixture)
    try:
        # Strands @tool wraps the function; call the underlying async impl.
        # DecoratedFunctionTool stores the original at ._tool_func.
        raw = main_lab_tools.create_skill_draft._tool_func
        out = await raw(name="Smoke Skill", skill_md="# Smoke")
    finally:
        main_lab_tools.reset_current_lab_service(token)

    payload = json.loads(out)
    assert "assetId" in payload
    assert payload.get("s3Prefix", "").startswith(f"users/{ACTOR_ID}/drafts/skills/")

    # Now list — should see the draft we just made.
    token = main_lab_tools.set_current_lab_service(lab_service_fixture)
    try:
        list_raw = main_lab_tools.list_skill_drafts._tool_func
        listed = json.loads(await list_raw())
    finally:
        main_lab_tools.reset_current_lab_service(token)
    assert any(item.get("assetId") == payload["assetId"] for item in listed)


# ---------------------------------------------------------------------------
# Property 2 — bypass set matches LabService dispatch table
# ---------------------------------------------------------------------------

def test_bypass_action_set_covers_lab_service_dispatch(lab_service_fixture):
    """LAB_BYPASS_ACTIONS must be a superset of LabService's registered actions
    so the Main Agent's bypass route never drops a structured payload."""
    registered = set(lab_service_fixture._action_handlers.keys())
    missing = registered - main_lab_tools.LAB_BYPASS_ACTIONS
    assert not missing, f"Bypass set is missing actions: {missing}"


@pytest.mark.asyncio
async def test_direct_bypass_dispatches_list_action(lab_service_fixture):
    """A {action: ListSkillDrafts} payload returns an empty list when no drafts exist."""
    result = await lab_service_fixture.handle({"action": "ListSkillDrafts"})
    assert result == []


# ---------------------------------------------------------------------------
# Property 3 — delete tools refuse without confirm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_skill_draft_requires_confirm(lab_service_fixture):
    token = main_lab_tools.set_current_lab_service(lab_service_fixture)
    try:
        raw = main_lab_tools.delete_skill_draft._tool_func
        # Without confirm
        no_confirm = json.loads(await raw(asset_id="skill-not-real"))
        assert no_confirm.get("error") == "confirmation_required"
        assert no_confirm.get("kind") == "skill"

        # confirm=False also refuses
        false_confirm = json.loads(await raw(asset_id="skill-not-real", confirm=False))
        assert false_confirm.get("error") == "confirmation_required"
    finally:
        main_lab_tools.reset_current_lab_service(token)


@pytest.mark.asyncio
async def test_delete_specialty_agent_draft_requires_confirm(lab_service_fixture):
    token = main_lab_tools.set_current_lab_service(lab_service_fixture)
    try:
        raw = main_lab_tools.delete_specialty_agent_draft._tool_func
        result = json.loads(await raw(asset_id="agent-not-real"))
        assert result.get("error") == "confirmation_required"
        assert result.get("kind") == "specialty-agent"
    finally:
        main_lab_tools.reset_current_lab_service(token)


@pytest.mark.asyncio
async def test_delete_tool_collection_draft_requires_confirm(lab_service_fixture):
    token = main_lab_tools.set_current_lab_service(lab_service_fixture)
    try:
        raw = main_lab_tools.delete_tool_collection_draft._tool_func
        result = json.loads(await raw(asset_id="tc-not-real"))
        assert result.get("error") == "confirmation_required"
        assert result.get("kind") == "tool-collection"
    finally:
        main_lab_tools.reset_current_lab_service(token)


@pytest.mark.asyncio
async def test_delete_workflow_draft_requires_confirm(lab_service_fixture):
    token = main_lab_tools.set_current_lab_service(lab_service_fixture)
    try:
        raw = main_lab_tools.delete_workflow_draft._tool_func
        result = json.loads(await raw(asset_id="wf-not-real"))
        assert result.get("error") == "confirmation_required"
        assert result.get("kind") == "workflow"
    finally:
        main_lab_tools.reset_current_lab_service(token)


@pytest.mark.asyncio
async def test_hook_chat_tools_validate_create_update_and_delete(lab_service_fixture):
    token = main_lab_tools.set_current_lab_service(lab_service_fixture)
    try:
        validate_raw = main_lab_tools.validate_hook_condition._tool_func
        validation = json.loads(await validate_raw(
            condition='tool.role == "results"',
            sample_event={"tool": {"role": "results"}},
        ))
        assert validation == {"valid": True, "matched": True}

        create_raw = main_lab_tools.create_hook._tool_func
        created = json.loads(await create_raw(
            name="chat-hook",
            event="after_tool_result",
            condition='tool.role == "results"',
            action_type="notify",
            action_params={"message": "done"},
            enabled=False,
        ))
        asset_id = created["assetId"]

        listed = json.loads(await main_lab_tools.list_hooks._tool_func())
        assert any(h["assetId"] == asset_id for h in listed["userHooks"])
        assert any(h["name"] == "render-artifacts" for h in listed["builtinHooks"])

        updated = json.loads(await main_lab_tools.update_hook._tool_func(
            asset_id=asset_id, enabled=True, priority=25,
        ))
        assert updated == {"updated": True}
        read = json.loads(await main_lab_tools.read_hook._tool_func(asset_id=asset_id))
        assert read["enabled"] is True and read["priority"] == 25

        guarded = json.loads(await main_lab_tools.delete_hook._tool_func(asset_id=asset_id))
        assert guarded["error"] == "confirmation_required"
        assert guarded["kind"] == "hook"

        deleted = json.loads(await main_lab_tools.delete_hook._tool_func(
            asset_id=asset_id, confirm=True,
        ))
        assert deleted == {"deleted": True}
    finally:
        main_lab_tools.reset_current_lab_service(token)


# ---------------------------------------------------------------------------
# Property 4 — confirm=true actually deletes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_skill_draft_with_confirm_actually_deletes(lab_service_fixture):
    token = main_lab_tools.set_current_lab_service(lab_service_fixture)
    try:
        # Create a skill, then delete with confirm
        create_raw = main_lab_tools.create_skill_draft._tool_func
        created = json.loads(await create_raw(name="To Delete", skill_md="# x"))
        asset_id = created["assetId"]

        delete_raw = main_lab_tools.delete_skill_draft._tool_func
        result = json.loads(await delete_raw(asset_id=asset_id, confirm=True))
        assert result.get("status") == "ok"

        # List should no longer contain it
        list_raw = main_lab_tools.list_skill_drafts._tool_func
        listed = json.loads(await list_raw())
        assert not any(item.get("assetId") == asset_id for item in listed)
    finally:
        main_lab_tools.reset_current_lab_service(token)


# ---------------------------------------------------------------------------
# Property 5 — tool registry shape
# ---------------------------------------------------------------------------

def test_lab_tools_registry_shape():
    # 5 CRUD per versioned category × 4 = 20, 6 hook tools, 5 lifecycle, 1 discovery tool.
    # The Main Agent binds ONE dispatcher, not 32 wrappers: the authoring API was
    # 73% of the builtin tool count and rode in every conversation. The wrappers
    # remain importable, and every one must be reachable as an action.
    assert len(main_lab_tools.LAB_TOOLS) == 1
    assert main_lab_tools.LAB_TOOLS[0].tool_spec["name"] == "lab_asset"
    assert len(main_lab_tools._INDIVIDUAL_LAB_TOOLS) == 32
    assert set(main_lab_tools._LAB_ACTIONS) == {
        t.tool_spec["name"] for t in main_lab_tools._INDIVIDUAL_LAB_TOOLS
    }
    names = {t.tool_spec["name"] for t in main_lab_tools._INDIVIDUAL_LAB_TOOLS}
    skill_tools = {n for n in names if "skill" in n and "specialty" not in n}
    assert len(skill_tools) == 5  # CRUD only — lifecycle tools are category-agnostic
    hook_tools = {
        "list_hooks", "create_hook", "read_hook", "update_hook",
        "delete_hook", "validate_hook_condition",
    }
    assert hook_tools.issubset(names)
    lifecycle = {
        "publish_asset", "edit_asset", "discard_changes",
        "list_asset_versions", "list_my_published_assets",
    }
    assert lifecycle.issubset(names)



# ---------------------------------------------------------------------------
# Lifecycle: publish / edit / discard end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_then_edit_round_trip(lab_service_fixture):
    """Create a working copy, publish v1, edit (fork v1), publish v2."""
    token = main_lab_tools.set_current_lab_service(lab_service_fixture)
    try:
        # Create a working copy.
        create_raw = main_lab_tools.create_skill_draft._tool_func
        created = json.loads(await create_raw(name="Lifecycle Test", skill_md="# v0"))
        asset_id = created["assetId"]

        # Publish v1.
        publish_raw = main_lab_tools.publish_asset._tool_func
        v1 = json.loads(await publish_raw(category="skill", asset_id=asset_id))
        assert v1.get("version") == 1, f"expected v1, got {v1}"

        # Working copy is gone — list_skill_drafts should not include it.
        list_raw = main_lab_tools.list_skill_drafts._tool_func
        listed = json.loads(await list_raw())
        assert not any(s.get("assetId") == asset_id for s in listed), (
            "working copy survived publish"
        )

        # list_asset_versions shows v1.
        versions_raw = main_lab_tools.list_asset_versions._tool_func
        versions = json.loads(await versions_raw(asset_id=asset_id))
        assert len(versions) == 1
        assert versions[0]["version"] == 1
        assert versions[0]["current"] is True

        # Edit — forks v1 into a new working copy.
        edit_raw = main_lab_tools.edit_asset._tool_func
        forked = json.loads(await edit_raw(category="skill", asset_id=asset_id))
        assert forked.get("forkedFrom", {}).get("version") == 1, forked

        # Working copy is back in the list.
        listed = json.loads(await list_raw())
        assert any(s.get("assetId") == asset_id for s in listed)

        # Edit again should refuse — working copy already exists.
        forked2 = json.loads(await edit_raw(category="skill", asset_id=asset_id))
        assert forked2.get("error") == "working_copy_exists", forked2

        # Publish v2.
        v2 = json.loads(await publish_raw(category="skill", asset_id=asset_id))
        assert v2.get("version") == 2

        # Versions now lists v1 and v2; v2 is current.
        versions = json.loads(await versions_raw(asset_id=asset_id))
        assert {v["version"] for v in versions} == {1, 2}
        current_versions = [v for v in versions if v["current"]]
        assert len(current_versions) == 1 and current_versions[0]["version"] == 2

    finally:
        main_lab_tools.reset_current_lab_service(token)


@pytest.mark.asyncio
async def test_discard_changes_drops_working_copy_only(lab_service_fixture):
    """discard_changes removes the working copy but keeps published versions."""
    token = main_lab_tools.set_current_lab_service(lab_service_fixture)
    try:
        # Create + publish.
        create_raw = main_lab_tools.create_skill_draft._tool_func
        created = json.loads(await create_raw(name="Discard Test", skill_md="# x"))
        asset_id = created["assetId"]

        publish_raw = main_lab_tools.publish_asset._tool_func
        await publish_raw(category="skill", asset_id=asset_id)

        # Edit forks v1.
        edit_raw = main_lab_tools.edit_asset._tool_func
        await edit_raw(category="skill", asset_id=asset_id)

        # discard_changes without confirm refuses.
        discard_raw = main_lab_tools.discard_changes._tool_func
        no_confirm = json.loads(
            await discard_raw(category="skill", asset_id=asset_id)
        )
        assert no_confirm.get("error") == "confirmation_required"

        # discard_changes with confirm succeeds.
        result = json.loads(
            await discard_raw(category="skill", asset_id=asset_id, confirm=True)
        )
        assert result.get("discarded") is True

        # Working copy gone.
        list_raw = main_lab_tools.list_skill_drafts._tool_func
        listed = json.loads(await list_raw())
        assert not any(s.get("assetId") == asset_id for s in listed)

        # v1 still there.
        versions_raw = main_lab_tools.list_asset_versions._tool_func
        versions = json.loads(await versions_raw(asset_id=asset_id))
        assert len(versions) == 1 and versions[0]["version"] == 1

    finally:
        main_lab_tools.reset_current_lab_service(token)


@pytest.mark.asyncio
async def test_owned_skill_loader_keeps_working_copy_ahead_of_published_version(
    lab_service_fixture, tmp_path
):
    token = main_lab_tools.set_current_lab_service(lab_service_fixture)
    package_roots = []
    try:
        create_raw = main_lab_tools.create_skill_draft._tool_func
        created = json.loads(
            await create_raw(name="Runtime Collision", skill_md="# Published instructions")
        )
        asset_id = created["assetId"]

        publish_raw = main_lab_tools.publish_asset._tool_func
        published = json.loads(
            await publish_raw(category="skill", asset_id=asset_id)
        )
        assert published["version"] == 1

        edit_raw = main_lab_tools.edit_asset._tool_func
        await edit_raw(category="skill", asset_id=asset_id)
        update_raw = main_lab_tools.update_skill_md._tool_func
        await update_raw(asset_id=asset_id, skill_md="# Working instructions")

        manager = SkillsManager(skills_dir=tmp_path)
        actor_skills, package_roots = (
            await main_lab_tools.load_owned_drafts_into_skills_manager(
                manager, lab_service_fixture
            )
        )

        colliding = [
            skill for skill in actor_skills if skill.name == "runtime-collision"
        ]
        assert len(colliding) == 2
        assert colliding[0].instructions == "# Working instructions"
        assert colliding[1].instructions == "# Published instructions"
        assert package_roots == []
    finally:
        for package_root in package_roots:
            SkillsManager.cleanup_runtime_package(package_root)
        main_lab_tools.reset_current_lab_service(token)



@pytest.mark.asyncio
async def test_specialty_can_bind_published_skill(lab_service_fixture):
    """Validator accepts a published skill the actor owns (post-publish path).

    Regresses the bug where _validate_compatible_skills only checked
    USER#<u>#OWNED so binding any specialty to a published skill failed.
    """
    token = main_lab_tools.set_current_lab_service(lab_service_fixture)
    try:
        # Create + publish a skill so it lives only in VERSIONS.
        create_raw = main_lab_tools.create_skill_draft._tool_func
        created = json.loads(await create_raw(name="Bound Skill", skill_md="# x"))
        skill_id = created["assetId"]

        publish_raw = main_lab_tools.publish_asset._tool_func
        v1 = json.loads(await publish_raw(category="skill", asset_id=skill_id))
        assert v1.get("version") == 1

        # No working copy should exist anymore.
        list_raw = main_lab_tools.list_skill_drafts._tool_func
        listed = json.loads(await list_raw())
        assert not any(s.get("assetId") == skill_id for s in listed)

        # Now create a specialty agent that binds the published skill.
        create_specialty = main_lab_tools.create_specialty_agent_draft._tool_func
        result = json.loads(
            await create_specialty(
                name="Binder",
                persona="binds published skill",
                compatible_skills=[skill_id],
            )
        )
        assert "assetId" in result, result
        assert "error" not in result, result
    finally:
        main_lab_tools.reset_current_lab_service(token)
