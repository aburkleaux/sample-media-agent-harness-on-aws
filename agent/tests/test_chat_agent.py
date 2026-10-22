"""Unit tests for LabChatAgent — inventory snapshot building and tool routing.

Validates: Requirements 6.1, 6.2, 6.3
Properties: 13, 14
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

import boto3
import pytest
from moto import mock_aws

# Add agent/src to path so imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.asset_index import AssetIndex
from lab.chat_agent import (
    CATEGORIES,
    CRUD_TOOL_DEFINITIONS,
    CRUDToolRouter,
    DisambiguationResult,
    InventorySnapshot,
    LabChatAgent,
    build_system_prompt,
    disambiguate,
)
from lab.context import LabRequestContext
from lab.gateway_browser import GatewayBrowser, GatewayEndpointConfig
from lab.models import GatewayToolDescriptor, Skill
from lab.s3_user_files_tool import S3UserFilesTool
from lab.service import LabService


def _run(coro):
    """Helper to run async coroutines from sync tests."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TABLE_NAME = "test-asset-index"
BUCKET_NAME = "test-artifacts-bucket"
ACTOR_ID = "actor-chat-test-001"
OTHER_ACTOR_ID = "actor-other-002"
SESSION_ID = "session-chat-abc"


class FakeGatewayClient:
    """Fake Gateway client for testing."""

    def __init__(self, tools=None):
        self._tools = tools or []

    def list_tools(self, actor_id: str, timeout: float) -> list[GatewayToolDescriptor]:
        return self._tools


def _create_table(dynamodb):
    """Create the Asset_Index DynamoDB table for testing.

    Uses the unified-platform single-table schema: PK / SK partition keys
    plus the byPublishedId GSI on (GSI1PK, GSI1SK) for lineage queries.
    """
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


def _create_bucket(s3_client):
    """Create the Artifacts_Bucket for testing."""
    s3_client.create_bucket(Bucket=BUCKET_NAME)


@pytest.fixture
def aws_env():
    """Provide mocked AWS resources and return (table, s3_client)."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_table(dynamodb)
        table = dynamodb.Table(TABLE_NAME)

        s3_client = boto3.client("s3", region_name="us-east-1")
        _create_bucket(s3_client)

        yield table, s3_client


@pytest.fixture
def lab_service(aws_env):
    """Provide a fully-wired LabService with mocked AWS resources."""
    table, _ = aws_env

    ctx = LabRequestContext(
        actor_id=ACTOR_ID,
        session_id=SESSION_ID,
        request_id=str(uuid.uuid4()),
    )

    s3 = S3UserFilesTool(bucket=BUCKET_NAME, actor_id=ACTOR_ID)
    index = AssetIndex(ddb_table=table, actor_id=ACTOR_ID)

    gateway_client = FakeGatewayClient(tools=[
        GatewayToolDescriptor(
            name="frame-extract",
            description="Extract frames from video",
            version="2.1.0",
            input_schema={},
        ),
    ])
    gateway_browser = GatewayBrowser(
        gateway_client=gateway_client,
        configured_endpoints=[],
    )

    service = LabService(
        ctx=ctx,
        s3=s3,
        index=index,
        gateway_browser=gateway_browser,
    )

    yield service


@pytest.fixture
def chat_agent(lab_service):
    """Provide a LabChatAgent instance."""
    return LabChatAgent(lab_service)


# ---------------------------------------------------------------------------
# Tests: Inventory Snapshot Building (Property 13)
# ---------------------------------------------------------------------------


class TestInventorySnapshot:
    """Tests for InventorySnapshot.build() — Property 13."""

    def test_empty_inventory_snapshot(self, lab_service):
        """Empty inventory produces snapshot with zero drafts in all categories."""
        snapshot = _run(InventorySnapshot.build(
            index=lab_service.index,
            actor_id=ACTOR_ID,
        ))
        assert snapshot.actor_id == ACTOR_ID
        assert snapshot.total_count == 0
        for category in CATEGORIES:
            assert snapshot.drafts_by_category[category] == []

    @pytest.mark.asyncio
    async def test_snapshot_includes_all_active_drafts(self, lab_service):
        """Snapshot includes all active drafts owned by the actor."""
        # Create drafts in different categories
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Skill A",
            "skillMd": "# Skill A\n\nContent.",
        })
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Skill B",
            "skillMd": "# Skill B\n\nContent.",
        })
        await lab_service.handle({
            "action": "CreateSpecialtyAgentDraft",
            "name": "Agent X",
            "persona": "You are agent X.",
        })

        snapshot = await InventorySnapshot.build(
            index=lab_service.index,
            actor_id=ACTOR_ID,
        )

        assert snapshot.total_count == 3
        assert len(snapshot.drafts_by_category["skill"]) == 2
        assert len(snapshot.drafts_by_category["specialty-agent"]) == 1
        assert len(snapshot.drafts_by_category["tool-collection"]) == 0
        assert len(snapshot.drafts_by_category["workflow"]) == 0

    @pytest.mark.asyncio
    async def test_snapshot_excludes_deleted_drafts(self, lab_service):
        """Snapshot does not include archived/deleted drafts."""
        # Create then delete a skill
        result = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Deleted Skill",
            "skillMd": "# Deleted\n\nContent.",
        })
        asset_id = result["assetId"]

        await lab_service.handle({
            "action": "DeleteSkillDraft",
            "assetId": asset_id,
        })

        snapshot = await InventorySnapshot.build(
            index=lab_service.index,
            actor_id=ACTOR_ID,
        )

        assert snapshot.total_count == 0
        assert len(snapshot.drafts_by_category["skill"]) == 0

    @pytest.mark.asyncio
    async def test_snapshot_no_cross_actor_drafts(self, aws_env, lab_service):
        """Property 13: Snapshot contains no cross-actor drafts."""
        table, _ = aws_env

        # Create a draft under the test actor
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "My Skill",
            "skillMd": "# Mine\n\nContent.",
        })

        # Manually insert a draft for a different actor directly via the
        # AssetIndex API, using the new typed-asset put() signature.
        other_index = AssetIndex(ddb_table=table, actor_id=OTHER_ACTOR_ID)
        now_ms = int(time.time() * 1000)
        await other_index.put(
            Skill(
                asset_id="other-skill-id",
                category="skill",
                name="Other Actor Skill",
                status="draft",
                scope="private",
                s3_prefix=f"users/{OTHER_ACTOR_ID}/drafts/skills/other-skill-id/",
                created_at=now_ms,
                updated_at=now_ms,
            ),
            owner_user_id=OTHER_ACTOR_ID,
        )

        # Build snapshot for our actor
        snapshot = await InventorySnapshot.build(
            index=lab_service.index,
            actor_id=ACTOR_ID,
        )

        # Should only see our own draft
        assert snapshot.total_count == 1
        assert len(snapshot.drafts_by_category["skill"]) == 1
        assert snapshot.drafts_by_category["skill"][0].name == "My Skill"


class TestInventorySnapshotFormatting:
    """Tests for InventorySnapshot.format_as_prompt_section()."""

    def test_empty_inventory_format(self, lab_service):
        """Empty inventory produces appropriate message."""
        snapshot = _run(InventorySnapshot.build(
            index=lab_service.index,
            actor_id=ACTOR_ID,
        ))
        formatted = snapshot.format_as_prompt_section()
        assert "empty" in formatted.lower()

    @pytest.mark.asyncio
    async def test_populated_inventory_format(self, lab_service):
        """Populated inventory lists drafts by category."""
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Format Test Skill",
            "skillMd": "# Format\n\nContent.",
        })

        snapshot = await InventorySnapshot.build(
            index=lab_service.index,
            actor_id=ACTOR_ID,
        )
        formatted = snapshot.format_as_prompt_section()

        # v2 inventory says "asset(s)" — drafts and versions counted together.
        assert "1 asset(s)" in formatted
        assert "Format Test Skill" in formatted
        assert "## Skills (1)" in formatted


# ---------------------------------------------------------------------------
# Tests: System Prompt Building (Requirement 6.2)
# ---------------------------------------------------------------------------


class TestSystemPrompt:
    """Tests for system prompt construction."""

    def test_system_prompt_contains_inventory(self, lab_service):
        """System prompt includes the inventory section."""
        snapshot = _run(InventorySnapshot.build(
            index=lab_service.index,
            actor_id=ACTOR_ID,
        ))
        prompt = build_system_prompt(snapshot)

        assert "Lab Chat Agent" in prompt
        assert "inventory" in prompt.lower()
        assert "CRUD" in prompt

    @pytest.mark.asyncio
    async def test_system_prompt_lists_drafts(self, lab_service):
        """System prompt lists actual drafts from the snapshot."""
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Prompt Skill",
            "skillMd": "# Prompt\n\nContent.",
        })

        snapshot = await InventorySnapshot.build(
            index=lab_service.index,
            actor_id=ACTOR_ID,
        )
        prompt = build_system_prompt(snapshot)

        assert "Prompt Skill" in prompt


# ---------------------------------------------------------------------------
# Tests: CRUD Tool Routing (Property 14)
# ---------------------------------------------------------------------------


class TestCRUDToolRouter:
    """Tests for CRUDToolRouter — Property 14."""

    @pytest.mark.asyncio
    async def test_create_skill_via_tool_router(self, lab_service):
        """Property 14: create_skill tool produces same state as direct CRUD."""
        router = CRUDToolRouter(lab_service)

        result = await router.invoke("create_skill", {
            "name": "Router Skill",
            "skill_md": "# Router\n\nContent.",
        })

        assert result.success is True
        assert "assetId" in result.data

        # Verify the skill exists via direct read
        read_result = await lab_service.handle({
            "action": "ReadSkillDraft",
            "assetId": result.data["assetId"],
        })
        assert read_result["name"] == "Router Skill"
        assert read_result["skillMd"] == "# Router\n\nContent."

    @pytest.mark.asyncio
    async def test_read_skill_via_tool_router(self, lab_service):
        """read_skill tool returns the same data as direct CRUD read."""
        # Create via direct service
        create_result = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Read Test",
            "skillMd": "# Read\n\nContent.",
        })
        asset_id = create_result["assetId"]

        # Read via tool router
        router = CRUDToolRouter(lab_service)
        result = await router.invoke("read_skill", {"asset_id": asset_id})

        assert result.success is True
        assert result.data["name"] == "Read Test"
        assert result.data["skillMd"] == "# Read\n\nContent."

    @pytest.mark.asyncio
    async def test_update_skill_via_tool_router(self, lab_service):
        """update_skill tool updates the same way as direct CRUD."""
        # Create
        create_result = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Update Test",
            "skillMd": "# Original\n\nContent.",
        })
        asset_id = create_result["assetId"]

        # Update via tool router
        router = CRUDToolRouter(lab_service)
        result = await router.invoke("update_skill", {
            "asset_id": asset_id,
            "skill_md": "# Updated\n\nNew content.",
        })

        assert result.success is True

        # Verify via direct read
        read_result = await lab_service.handle({
            "action": "ReadSkillDraft",
            "assetId": asset_id,
        })
        assert read_result["skillMd"] == "# Updated\n\nNew content."

    @pytest.mark.asyncio
    async def test_delete_skill_via_tool_router(self, lab_service):
        """delete_skill tool deletes the same way as direct CRUD."""
        # Create
        create_result = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Delete Test",
            "skillMd": "# Delete\n\nContent.",
        })
        asset_id = create_result["assetId"]

        # Delete via tool router
        router = CRUDToolRouter(lab_service)
        result = await router.invoke("delete_skill", {"asset_id": asset_id})

        assert result.success is True

        # Verify deleted via direct read
        read_result = await lab_service.handle({
            "action": "ReadSkillDraft",
            "assetId": asset_id,
        })
        assert read_result == {"error": "not_found"}

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, lab_service):
        """Unknown tool name returns a failure result."""
        router = CRUDToolRouter(lab_service)
        result = await router.invoke("nonexistent_tool", {})

        assert result.success is False
        assert "Unknown tool" in result.error

    @pytest.mark.asyncio
    async def test_create_specialty_agent_via_tool_router(self, lab_service):
        """create_specialty_agent tool routes through validated CRUD."""
        router = CRUDToolRouter(lab_service)

        result = await router.invoke("create_specialty_agent", {
            "name": "Test Agent",
            "persona": "You are a test agent.",
        })

        assert result.success is True
        assert "assetId" in result.data

    @pytest.mark.asyncio
    async def test_tool_router_available_tools(self, lab_service):
        """Tool router exposes all expected CRUD tools."""
        router = CRUDToolRouter(lab_service)
        tools = router.available_tools

        assert "create_skill" in tools
        assert "read_skill" in tools
        assert "update_skill" in tools
        assert "delete_skill" in tools
        assert "create_specialty_agent" in tools
        assert "create_tool_collection" in tools
        assert "create_workflow" in tools
        assert len(tools) == 16  # 4 operations × 4 categories


# ---------------------------------------------------------------------------
# Tests: LabChatAgent Integration (Requirements 6.1, 6.2, 6.3)
# ---------------------------------------------------------------------------


class TestLabChatAgent:
    """Integration tests for the LabChatAgent class."""

    def test_agent_builds_snapshot_on_each_turn(self, chat_agent):
        """Requirement 6.1: Agent builds inventory snapshot per turn."""
        snapshot = _run(chat_agent.build_inventory_snapshot())
        assert isinstance(snapshot, InventorySnapshot)
        assert snapshot.actor_id == ACTOR_ID

    @pytest.mark.asyncio
    async def test_handle_message_without_tools(self, chat_agent):
        """Handle a message with no tool calls — just inventory awareness."""
        result = await chat_agent.handle_message("What do I have?")

        assert isinstance(result.response, str)
        assert result.affected_asset_ids == []
        assert result.tool_calls_made == []

    @pytest.mark.asyncio
    async def test_handle_message_with_tool_call(self, chat_agent):
        """Requirement 6.3: Tool calls route through validated CRUD."""
        result = await chat_agent.handle_message(
            message="Create a skill called Test",
            tool_calls=[{
                "name": "create_skill",
                "arguments": {
                    "name": "Chat Created Skill",
                    "skill_md": "# Chat Created\n\nContent.",
                },
            }],
        )

        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0]["success"] is True
        assert len(result.affected_asset_ids) == 1

    @pytest.mark.asyncio
    async def test_handle_message_tracks_affected_assets(self, chat_agent, lab_service):
        """Turn result includes affected asset IDs for turn record."""
        result = await chat_agent.handle_message(
            message="Create two skills",
            tool_calls=[
                {
                    "name": "create_skill",
                    "arguments": {
                        "name": "Skill One",
                        "skill_md": "# One\n\nContent.",
                    },
                },
                {
                    "name": "create_skill",
                    "arguments": {
                        "name": "Skill Two",
                        "skill_md": "# Two\n\nContent.",
                    },
                },
            ],
        )

        assert len(result.affected_asset_ids) == 2
        assert len(result.tool_calls_made) == 2
        assert all(tc["success"] for tc in result.tool_calls_made)

    @pytest.mark.asyncio
    async def test_snapshot_reflects_mutations_from_prior_turn(self, chat_agent):
        """After a mutation, the next snapshot reflects the change."""
        # First turn: create a skill
        await chat_agent.handle_message(
            message="Create a skill",
            tool_calls=[{
                "name": "create_skill",
                "arguments": {
                    "name": "Reflected Skill",
                    "skill_md": "# Reflected\n\nContent.",
                },
            }],
        )

        # Second turn: snapshot should include the new skill
        snapshot = await chat_agent.build_inventory_snapshot()
        assert snapshot.total_count == 1
        assert len(snapshot.drafts_by_category["skill"]) == 1
        assert snapshot.drafts_by_category["skill"][0].name == "Reflected Skill"

    def test_tool_definitions_exposed(self, chat_agent):
        """Agent exposes CRUD tool definitions."""
        defs = chat_agent.tool_definitions
        assert len(defs) == len(CRUD_TOOL_DEFINITIONS)
        names = [d.name for d in defs]
        assert "create_skill" in names
        assert "delete_workflow" in names

    @pytest.mark.asyncio
    async def test_failed_tool_call_reported(self, chat_agent):
        """Failed tool calls are reported in the turn result."""
        result = await chat_agent.handle_message(
            message="Read a nonexistent skill",
            tool_calls=[{
                "name": "read_skill",
                "arguments": {"asset_id": "nonexistent-id"},
            }],
        )

        # The read succeeds (returns not_found data), but doesn't track asset_id
        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0]["success"] is True

    @pytest.mark.asyncio
    async def test_unknown_tool_call_fails_gracefully(self, chat_agent):
        """Unknown tool calls fail gracefully without crashing."""
        result = await chat_agent.handle_message(
            message="Do something weird",
            tool_calls=[{
                "name": "nonexistent_tool",
                "arguments": {},
            }],
        )

        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0]["success"] is False
        assert "Unknown tool" in result.tool_calls_made[0]["error"]


# ---------------------------------------------------------------------------
# Tests: Disambiguation Flow (Requirement 6.6)
# ---------------------------------------------------------------------------


class TestDisambiguation:
    """Tests for the disambiguation flow — Requirement 6.6.

    Requirement 6.6: IF the user's message asks the Lab_Chat_Agent to act on
    a Draft identified only by name and more than one Draft in the
    Inventory_Snapshot matches that name within the same category, THEN THE
    Lab_Chat_Agent SHALL ask the user to disambiguate and SHALL NOT mutate
    any Draft until the user selects one.
    """

    @pytest.mark.asyncio
    async def test_disambiguate_single_match_resolves(self, lab_service, chat_agent):
        """Exactly 1 match → returns resolved with the asset_id."""
        # Create a single skill
        result = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Unique Skill",
            "skillMd": "# Unique Skill\n\nContent.",
        })
        asset_id = result["assetId"]

        # Disambiguate should resolve to that single skill
        dis_result = await chat_agent.disambiguate_draft("Unique Skill", "skill")

        assert dis_result.outcome == "resolved"
        assert dis_result.asset_id == asset_id
        assert len(dis_result.matches) == 1
        assert dis_result.matches[0].name == "Unique Skill"

    @pytest.mark.asyncio
    async def test_disambiguate_single_match_case_insensitive(self, lab_service, chat_agent):
        """Name matching is case-insensitive."""
        result = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "My Skill",
            "skillMd": "# My Skill\n\nContent.",
        })
        asset_id = result["assetId"]

        # Search with different casing
        dis_result = await chat_agent.disambiguate_draft("my skill", "skill")

        assert dis_result.outcome == "resolved"
        assert dis_result.asset_id == asset_id

    @pytest.mark.asyncio
    async def test_disambiguate_zero_matches_returns_not_found(self, lab_service, chat_agent):
        """Zero matches → returns not_found with nearest matches."""
        # Create some skills with known names
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Image Processing",
            "skillMd": "# Image Processing\n\nContent.",
        })
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Image Analysis",
            "skillMd": "# Image Analysis\n\nContent.",
        })
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Text Extraction",
            "skillMd": "# Text Extraction\n\nContent.",
        })

        # Search for a name that doesn't exist
        dis_result = await chat_agent.disambiguate_draft("Image Editing", "skill")

        assert dis_result.outcome == "not_found"
        assert dis_result.asset_id is None
        assert len(dis_result.nearest_matches) > 0
        # The nearest matches should include the "Image" skills since they're similar
        nearest_names = [m.name for m in dis_result.nearest_matches]
        assert "Image Processing" in nearest_names or "Image Analysis" in nearest_names

    @pytest.mark.asyncio
    async def test_disambiguate_zero_matches_empty_inventory(self, chat_agent):
        """Zero matches on empty inventory → not_found with no nearest matches."""
        dis_result = await chat_agent.disambiguate_draft("Nonexistent", "skill")

        assert dis_result.outcome == "not_found"
        assert dis_result.asset_id is None
        assert dis_result.nearest_matches == []

    @pytest.mark.asyncio
    async def test_disambiguate_multiple_matches_returns_ambiguous(self, lab_service, chat_agent):
        """2+ matches in same category → returns ambiguous, never mutates."""
        # Create two skills with the same name
        result1 = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Duplicate Name",
            "skillMd": "# Duplicate Name\n\nFirst version.",
        })
        result2 = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Duplicate Name",
            "skillMd": "# Duplicate Name\n\nSecond version.",
        })

        # Disambiguate should return ambiguous
        dis_result = await chat_agent.disambiguate_draft("Duplicate Name", "skill")

        assert dis_result.outcome == "ambiguous"
        assert dis_result.asset_id is None
        assert len(dis_result.matches) == 2
        match_ids = {m.asset_id for m in dis_result.matches}
        assert result1["assetId"] in match_ids
        assert result2["assetId"] in match_ids

    @pytest.mark.asyncio
    async def test_disambiguate_multiple_matches_case_insensitive(self, lab_service, chat_agent):
        """Ambiguity detection is case-insensitive."""
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "My Skill",
            "skillMd": "# My Skill\n\nFirst.",
        })
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "my skill",
            "skillMd": "# my skill\n\nSecond.",
        })

        dis_result = await chat_agent.disambiguate_draft("MY SKILL", "skill")

        assert dis_result.outcome == "ambiguous"
        assert len(dis_result.matches) == 2

    @pytest.mark.asyncio
    async def test_disambiguate_scoped_to_category(self, lab_service, chat_agent):
        """Disambiguation only searches within the specified category."""
        # Create a skill and a specialty agent with the same name
        skill_result = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Shared Name",
            "skillMd": "# Shared Name\n\nSkill content.",
        })
        await lab_service.handle({
            "action": "CreateSpecialtyAgentDraft",
            "name": "Shared Name",
            "persona": "You are Shared Name agent.",
        })

        # Disambiguate in skill category should find exactly one
        dis_result = await chat_agent.disambiguate_draft("Shared Name", "skill")

        assert dis_result.outcome == "resolved"
        assert dis_result.asset_id == skill_result["assetId"]

    @pytest.mark.asyncio
    async def test_disambiguate_never_mutates_when_ambiguous(self, lab_service, chat_agent):
        """Requirement 6.6: SHALL NOT mutate any Draft when ambiguous."""
        # Create two skills with the same name
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Same Name",
            "skillMd": "# Same Name\n\nOriginal A.",
        })
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Same Name",
            "skillMd": "# Same Name\n\nOriginal B.",
        })

        # Disambiguate returns ambiguous
        dis_result = await chat_agent.disambiguate_draft("Same Name", "skill")
        assert dis_result.outcome == "ambiguous"

        # Verify no mutations occurred — both skills still exist unchanged
        snapshot = await chat_agent.build_inventory_snapshot()
        skills = snapshot.drafts_by_category["skill"]
        assert len(skills) == 2
        same_name_skills = [s for s in skills if s.name == "Same Name"]
        assert len(same_name_skills) == 2

    @pytest.mark.asyncio
    async def test_disambiguate_nearest_matches_substring(self, lab_service, chat_agent):
        """Nearest matches include substring matches ranked highly."""
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Video Frame Extraction",
            "skillMd": "# Video Frame Extraction\n\nContent.",
        })
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Audio Extraction",
            "skillMd": "# Audio Extraction\n\nContent.",
        })
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Unrelated Skill",
            "skillMd": "# Unrelated Skill\n\nContent.",
        })

        # Search for "Extraction" — no exact match, but substring matches exist
        dis_result = await chat_agent.disambiguate_draft("Extraction", "skill")

        assert dis_result.outcome == "not_found"
        assert len(dis_result.nearest_matches) > 0
        # Both extraction skills should appear in nearest matches
        nearest_names = [m.name for m in dis_result.nearest_matches]
        assert "Video Frame Extraction" in nearest_names
        assert "Audio Extraction" in nearest_names


class TestDisambiguateFunctionDirect:
    """Direct tests for the disambiguate() module-level function."""

    @staticmethod
    def _make_skill(name: str, asset_id: str) -> Skill:
        """Build a minimal Skill dataclass for direct disambiguate() tests."""
        return Skill(
            asset_id=asset_id,
            category="skill",
            name=name,
            status="draft",
            scope="private",
            s3_prefix=f"users/test-actor/drafts/skills/{asset_id}/",
            created_at=0,
            updated_at=0,
        )

    def test_disambiguate_with_empty_snapshot(self):
        """disambiguate on empty snapshot returns not_found."""
        snapshot = InventorySnapshot(
            actor_id="test-actor",
            drafts_by_category={cat: [] for cat in CATEGORIES},
            total_count=0,
        )

        result = disambiguate(snapshot, "anything", "skill")

        assert result.outcome == "not_found"
        assert result.asset_id is None
        assert result.nearest_matches == []

    def test_disambiguate_exact_match(self):
        """disambiguate with exactly one match returns resolved."""
        snapshot = InventorySnapshot(
            actor_id="test-actor",
            drafts_by_category={
                "skill": [
                    self._make_skill("Alpha", "id-alpha"),
                    self._make_skill("Beta", "id-beta"),
                ],
                "specialty-agent": [],
                "tool-collection": [],
                "workflow": [],
            },
            total_count=2,
        )

        result = disambiguate(snapshot, "Alpha", "skill")

        assert result.outcome == "resolved"
        assert result.asset_id == "id-alpha"

    def test_disambiguate_multiple_exact_matches(self):
        """disambiguate with 2+ exact matches returns ambiguous."""
        snapshot = InventorySnapshot(
            actor_id="test-actor",
            drafts_by_category={
                "skill": [
                    self._make_skill("Dup", "id-1"),
                    self._make_skill("Dup", "id-2"),
                    self._make_skill("Dup", "id-3"),
                ],
                "specialty-agent": [],
                "tool-collection": [],
                "workflow": [],
            },
            total_count=3,
        )

        result = disambiguate(snapshot, "Dup", "skill")

        assert result.outcome == "ambiguous"
        assert result.asset_id is None
        assert len(result.matches) == 3

    def test_disambiguate_no_match_with_nearest(self):
        """disambiguate with no exact match returns nearest alternatives."""
        snapshot = InventorySnapshot(
            actor_id="test-actor",
            drafts_by_category={
                "skill": [
                    self._make_skill("Image Processing", "id-1"),
                    self._make_skill("Image Analysis", "id-2"),
                    self._make_skill("Text Parser", "id-3"),
                ],
                "specialty-agent": [],
                "tool-collection": [],
                "workflow": [],
            },
            total_count=3,
        )

        result = disambiguate(snapshot, "Image Editing", "skill")

        assert result.outcome == "not_found"
        assert result.asset_id is None
        assert len(result.nearest_matches) > 0
        # Image-related skills should rank higher than "Text Parser"
        nearest_names = [m.name for m in result.nearest_matches]
        # At minimum, the image-related ones should appear
        assert any("Image" in n for n in nearest_names)

    def test_disambiguate_max_nearest_limit(self):
        """nearest_matches respects the max_nearest parameter."""
        snapshot = InventorySnapshot(
            actor_id="test-actor",
            drafts_by_category={
                "skill": [
                    self._make_skill(f"Skill {i}", f"id-{i}")
                    for i in range(20)
                ],
                "specialty-agent": [],
                "tool-collection": [],
                "workflow": [],
            },
            total_count=20,
        )

        result = disambiguate(snapshot, "Nonexistent", "skill", max_nearest=3)

        assert result.outcome == "not_found"
        assert len(result.nearest_matches) <= 3
