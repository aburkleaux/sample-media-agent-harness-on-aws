"""Unit tests for ConversationalMemory — turn record persistence and isolation.

Validates: Requirements 6.4, 6.5, 9.5
Properties: 21
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import boto3
import pytest
from moto import mock_aws

# Add agent/src to path so imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.chat_agent import LabChatAgent
from lab.context import LabRequestContext
from lab.conversational_memory import ConversationalMemory, TurnRecord
from lab.asset_index import AssetIndex
from lab.gateway_browser import GatewayBrowser
from lab.models import GatewayToolDescriptor
from lab.s3_user_files_tool import S3UserFilesTool
from lab.service import LabService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TABLE_NAME = "test-asset-index"
BUCKET_NAME = "test-artifacts-bucket"
ACTOR_ID = "actor-memory-test-001"
OTHER_ACTOR_ID = "actor-memory-other-002"
SESSION_ID = "session-memory-abc"
OTHER_SESSION_ID = "session-memory-xyz"


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
    """Provide mocked AWS resources."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_table(dynamodb)
        table = dynamodb.Table(TABLE_NAME)

        s3_client = boto3.client("s3", region_name="us-east-1")
        _create_bucket(s3_client)

        yield table, s3_client


@pytest.fixture
def ctx():
    """Provide a LabRequestContext."""
    return LabRequestContext(
        actor_id=ACTOR_ID,
        session_id=SESSION_ID,
        request_id=str(uuid.uuid4()),
    )


@pytest.fixture
def other_ctx():
    """Provide a LabRequestContext for a different actor."""
    return LabRequestContext(
        actor_id=OTHER_ACTOR_ID,
        session_id=SESSION_ID,
        request_id=str(uuid.uuid4()),
    )


@pytest.fixture
def other_session_ctx():
    """Provide a LabRequestContext for same actor, different session."""
    return LabRequestContext(
        actor_id=ACTOR_ID,
        session_id=OTHER_SESSION_ID,
        request_id=str(uuid.uuid4()),
    )


@pytest.fixture
def s3_tool(aws_env, ctx):
    """Provide an S3UserFilesTool bound to the test actor."""
    return S3UserFilesTool(bucket=BUCKET_NAME, actor_id=ctx.actor_id)


@pytest.fixture
def memory(s3_tool, ctx):
    """Provide a ConversationalMemory instance."""
    return ConversationalMemory(s3=s3_tool, ctx=ctx)


@pytest.fixture
def other_actor_memory(aws_env, other_ctx):
    """Provide a ConversationalMemory for a different actor."""
    s3 = S3UserFilesTool(bucket=BUCKET_NAME, actor_id=other_ctx.actor_id)
    return ConversationalMemory(s3=s3, ctx=other_ctx)


@pytest.fixture
def other_session_memory(aws_env, other_session_ctx):
    """Provide a ConversationalMemory for same actor, different session."""
    s3 = S3UserFilesTool(bucket=BUCKET_NAME, actor_id=other_session_ctx.actor_id)
    return ConversationalMemory(s3=s3, ctx=other_session_ctx)


@pytest.fixture
def lab_service(aws_env):
    """Provide a fully-wired LabService."""
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
            description="Extract frames",
            version="2.1.0",
            input_schema={},
        ),
    ])
    gateway_browser = GatewayBrowser(
        gateway_client=gateway_client,
        configured_endpoints=[],
    )

    return LabService(
        ctx=ctx,
        s3=s3,
        index=index,
        gateway_browser=gateway_browser,
    )


# ---------------------------------------------------------------------------
# Tests: TurnRecord Model
# ---------------------------------------------------------------------------


class TestTurnRecord:
    """Tests for TurnRecord serialization."""

    def test_to_dict_roundtrip(self):
        """TurnRecord serializes and deserializes correctly."""
        record = TurnRecord(
            turn_id="turn-123",
            timestamp=1700000000000,
            user_message="Create a skill",
            affected_asset_ids=["asset-1", "asset-2"],
            tool_calls_made=[{"tool": "create_skill", "success": True}],
        )

        data = record.to_dict()
        restored = TurnRecord.from_dict(data)

        assert restored.turn_id == record.turn_id
        assert restored.timestamp == record.timestamp
        assert restored.user_message == record.user_message
        assert restored.affected_asset_ids == record.affected_asset_ids
        assert restored.tool_calls_made == record.tool_calls_made

    def test_from_dict_with_missing_optional_fields(self):
        """TurnRecord handles missing optional fields gracefully."""
        data = {
            "turn_id": "turn-456",
            "timestamp": 1700000000000,
            "user_message": "Hello",
        }
        record = TurnRecord.from_dict(data)

        assert record.turn_id == "turn-456"
        assert record.affected_asset_ids == []
        assert record.tool_calls_made == []


# ---------------------------------------------------------------------------
# Tests: ConversationalMemory Persistence (Requirement 6.4)
# ---------------------------------------------------------------------------


class TestConversationalMemoryPersistence:
    """Tests for turn record persistence."""

    @pytest.mark.asyncio
    async def test_persist_turn_creates_record(self, memory):
        """Requirement 6.4: Turn record is persisted with affected asset IDs."""
        record = await memory.persist_turn(
            user_message="Create a skill called Test",
            affected_asset_ids=["asset-abc"],
            tool_calls_made=[{"tool": "create_skill", "success": True}],
        )

        assert record.turn_id is not None
        assert record.timestamp > 0
        assert record.user_message == "Create a skill called Test"
        assert record.affected_asset_ids == ["asset-abc"]
        assert record.tool_calls_made == [{"tool": "create_skill", "success": True}]

    @pytest.mark.asyncio
    async def test_persist_turn_includes_affected_asset_ids(self, memory):
        """Requirement 6.4: Turn record includes list of affected Draft IDs."""
        record = await memory.persist_turn(
            user_message="Create two skills",
            affected_asset_ids=["asset-1", "asset-2", "asset-3"],
            tool_calls_made=[],
        )

        assert len(record.affected_asset_ids) == 3
        assert "asset-1" in record.affected_asset_ids
        assert "asset-2" in record.affected_asset_ids
        assert "asset-3" in record.affected_asset_ids

    @pytest.mark.asyncio
    async def test_persist_multiple_turns(self, memory):
        """Multiple turns can be persisted in the same session."""
        await memory.persist_turn(
            user_message="First turn",
            affected_asset_ids=["asset-1"],
            tool_calls_made=[],
        )
        await memory.persist_turn(
            user_message="Second turn",
            affected_asset_ids=["asset-2"],
            tool_calls_made=[],
        )

        turns = await memory.list_turns()
        assert len(turns) == 2

    @pytest.mark.asyncio
    async def test_get_turn_by_id(self, memory):
        """Can retrieve a specific turn by its turn_id."""
        record = await memory.persist_turn(
            user_message="Specific turn",
            affected_asset_ids=["asset-x"],
            tool_calls_made=[{"tool": "update_skill", "success": True}],
        )

        retrieved = await memory.get_turn(record.turn_id)
        assert retrieved is not None
        assert retrieved.turn_id == record.turn_id
        assert retrieved.user_message == "Specific turn"
        assert retrieved.affected_asset_ids == ["asset-x"]

    @pytest.mark.asyncio
    async def test_get_nonexistent_turn_returns_none(self, memory):
        """Getting a non-existent turn returns None."""
        result = await memory.get_turn("nonexistent-turn-id")
        assert result is None


# ---------------------------------------------------------------------------
# Tests: Cross-Session Isolation (Requirement 6.5, Property 21)
# ---------------------------------------------------------------------------


class TestConversationalMemoryIsolation:
    """Tests for cross-session and cross-actor isolation — Property 21."""

    @pytest.mark.asyncio
    async def test_list_turns_scoped_to_session(self, memory, other_session_memory):
        """Property 21: list_turns returns only turns from same session."""
        # Persist turns in session A
        await memory.persist_turn(
            user_message="Session A turn",
            affected_asset_ids=["asset-a"],
            tool_calls_made=[],
        )

        # Persist turns in session B (same actor, different session)
        await other_session_memory.persist_turn(
            user_message="Session B turn",
            affected_asset_ids=["asset-b"],
            tool_calls_made=[],
        )

        # List turns for session A — should only see session A's turn
        turns_a = await memory.list_turns()
        assert len(turns_a) == 1
        assert turns_a[0].user_message == "Session A turn"

        # List turns for session B — should only see session B's turn
        turns_b = await other_session_memory.list_turns()
        assert len(turns_b) == 1
        assert turns_b[0].user_message == "Session B turn"

    @pytest.mark.asyncio
    async def test_list_turns_scoped_to_actor(self, memory, other_actor_memory):
        """Property 21: list_turns returns no turns from other actors."""
        # Persist a turn for actor A
        await memory.persist_turn(
            user_message="Actor A turn",
            affected_asset_ids=["asset-a"],
            tool_calls_made=[],
        )

        # Persist a turn for actor B
        await other_actor_memory.persist_turn(
            user_message="Actor B turn",
            affected_asset_ids=["asset-b"],
            tool_calls_made=[],
        )

        # List turns for actor A — should only see actor A's turn
        turns_a = await memory.list_turns()
        assert len(turns_a) == 1
        assert turns_a[0].user_message == "Actor A turn"

        # List turns for actor B — should only see actor B's turn
        turns_b = await other_actor_memory.list_turns()
        assert len(turns_b) == 1
        assert turns_b[0].user_message == "Actor B turn"

    @pytest.mark.asyncio
    async def test_get_turn_from_other_session_returns_none(
        self, memory, other_session_memory
    ):
        """Cannot retrieve a turn from a different session."""
        record = await memory.persist_turn(
            user_message="Session A only",
            affected_asset_ids=["asset-a"],
            tool_calls_made=[],
        )

        # Try to get the turn from the other session's memory
        result = await other_session_memory.get_turn(record.turn_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_turns_sorted_by_timestamp(self, memory):
        """list_turns returns turns sorted by timestamp (oldest first)."""
        await memory.persist_turn(
            user_message="First",
            affected_asset_ids=["a1"],
            tool_calls_made=[],
        )
        await memory.persist_turn(
            user_message="Second",
            affected_asset_ids=["a2"],
            tool_calls_made=[],
        )
        await memory.persist_turn(
            user_message="Third",
            affected_asset_ids=["a3"],
            tool_calls_made=[],
        )

        turns = await memory.list_turns()
        assert len(turns) == 3
        assert turns[0].user_message == "First"
        assert turns[1].user_message == "Second"
        assert turns[2].user_message == "Third"
        # Timestamps should be non-decreasing
        assert turns[0].timestamp <= turns[1].timestamp <= turns[2].timestamp


# ---------------------------------------------------------------------------
# Tests: Integration with LabChatAgent (Requirement 6.4)
# ---------------------------------------------------------------------------


class TestChatAgentMemoryIntegration:
    """Tests for auto-persistence of turn records via LabChatAgent."""

    @pytest.mark.asyncio
    async def test_mutation_turn_persists_record(self, lab_service):
        """Requirement 6.4: Turn that modifies drafts persists a turn record."""
        chat_agent = lab_service.chat_agent

        # Execute a turn that creates a skill (mutation)
        await chat_agent.handle_message(
            message="Create a skill called Memory Test",
            tool_calls=[{
                "name": "create_skill",
                "arguments": {
                    "name": "Memory Test Skill",
                    "skill_md": "# Memory Test\n\nContent.",
                },
            }],
        )

        # Verify a turn record was persisted
        turns = await chat_agent.memory.list_turns()
        assert len(turns) == 1
        assert turns[0].user_message == "Create a skill called Memory Test"
        assert len(turns[0].affected_asset_ids) == 1

    @pytest.mark.asyncio
    async def test_non_mutation_turn_does_not_persist(self, lab_service):
        """No turn record persisted when no drafts are modified."""
        chat_agent = lab_service.chat_agent

        # Execute a turn with no tool calls (no mutation)
        await chat_agent.handle_message(
            message="What do I have in my lab?",
        )

        # No turn record should be persisted
        turns = await chat_agent.memory.list_turns()
        assert len(turns) == 0

    @pytest.mark.asyncio
    async def test_turn_record_includes_all_affected_assets(self, lab_service):
        """Turn record includes all asset IDs affected in the turn."""
        chat_agent = lab_service.chat_agent

        # Create two skills in one turn
        await chat_agent.handle_message(
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

        turns = await chat_agent.memory.list_turns()
        assert len(turns) == 1
        assert len(turns[0].affected_asset_ids) == 2

    @pytest.mark.asyncio
    async def test_turn_record_includes_tool_calls(self, lab_service):
        """Turn record includes tool call details."""
        chat_agent = lab_service.chat_agent

        await chat_agent.handle_message(
            message="Create a skill",
            tool_calls=[{
                "name": "create_skill",
                "arguments": {
                    "name": "Tool Call Test",
                    "skill_md": "# Tool Call\n\nContent.",
                },
            }],
        )

        turns = await chat_agent.memory.list_turns()
        assert len(turns) == 1
        assert len(turns[0].tool_calls_made) == 1
        assert turns[0].tool_calls_made[0]["tool"] == "create_skill"
        assert turns[0].tool_calls_made[0]["success"] is True
