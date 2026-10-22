"""Unit tests for DurableKnowledge — actor-scoped cross-session preferences.

Validates: Requirement 9.6
Verifies basic CRUD operations and actor isolation.
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

from lab.context import LabRequestContext
from lab.durable_knowledge import DurableKnowledge
from lab.s3_user_files_tool import S3UserFilesTool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BUCKET_NAME = "test-knowledge-bucket"
ACTOR_ID = "actor-knowledge-001"
OTHER_ACTOR_ID = "actor-knowledge-002"
SESSION_ID = "session-abc"


def _create_bucket(s3_client):
    """Create the test S3 bucket."""
    s3_client.create_bucket(Bucket=BUCKET_NAME)


@pytest.fixture
def aws_env():
    """Provide mocked AWS resources."""
    with mock_aws():
        s3_client = boto3.client("s3", region_name="us-east-1")
        _create_bucket(s3_client)
        yield s3_client


@pytest.fixture
def ctx():
    """Provide a LabRequestContext for the primary actor."""
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
def s3_tool(aws_env, ctx):
    """Provide an S3UserFilesTool bound to the primary actor."""
    return S3UserFilesTool(bucket=BUCKET_NAME, actor_id=ctx.actor_id)


@pytest.fixture
def knowledge(s3_tool, ctx):
    """Provide a DurableKnowledge instance for the primary actor."""
    return DurableKnowledge(s3=s3_tool, ctx=ctx)


@pytest.fixture
def other_knowledge(aws_env, other_ctx):
    """Provide a DurableKnowledge instance for a different actor."""
    s3 = S3UserFilesTool(bucket=BUCKET_NAME, actor_id=other_ctx.actor_id)
    return DurableKnowledge(s3=s3, ctx=other_ctx)


# ---------------------------------------------------------------------------
# Tests: Basic CRUD Operations
# ---------------------------------------------------------------------------


class TestDurableKnowledgeCRUD:
    """Tests for basic get/put/delete/list_keys operations."""

    @pytest.mark.asyncio
    async def test_put_and_get_string_value(self, knowledge):
        """Put a string value and retrieve it."""
        await knowledge.put("preferred_language", "python")
        result = await knowledge.get("preferred_language")
        assert result == "python"

    @pytest.mark.asyncio
    async def test_put_and_get_dict_value(self, knowledge):
        """Put a dict value and retrieve it."""
        prefs = {"theme": "dark", "font_size": 14, "auto_save": True}
        await knowledge.put("ui_preferences", prefs)
        result = await knowledge.get("ui_preferences")
        assert result == prefs

    @pytest.mark.asyncio
    async def test_put_and_get_list_value(self, knowledge):
        """Put a list value and retrieve it."""
        tags = ["ml", "vision", "moderation"]
        await knowledge.put("favorite_tags", tags)
        result = await knowledge.get("favorite_tags")
        assert result == tags

    @pytest.mark.asyncio
    async def test_put_and_get_numeric_value(self, knowledge):
        """Put a numeric value and retrieve it."""
        await knowledge.put("max_retries", 5)
        result = await knowledge.get("max_retries")
        assert result == 5

    @pytest.mark.asyncio
    async def test_put_and_get_null_value(self, knowledge):
        """Put a null value and retrieve it."""
        await knowledge.put("cleared_setting", None)
        result = await knowledge.get("cleared_setting")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_nonexistent_key_returns_none(self, knowledge):
        """Getting a key that doesn't exist returns None."""
        result = await knowledge.get("nonexistent_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_put_overwrites_existing_value(self, knowledge):
        """Putting to an existing key overwrites the value."""
        await knowledge.put("setting", "old_value")
        await knowledge.put("setting", "new_value")
        result = await knowledge.get("setting")
        assert result == "new_value"

    @pytest.mark.asyncio
    async def test_delete_removes_entry(self, knowledge):
        """Delete removes the entry so get returns None."""
        await knowledge.put("to_delete", "temporary")
        await knowledge.delete("to_delete")
        result = await knowledge.get("to_delete")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_key_is_noop(self, knowledge):
        """Deleting a non-existent key does not raise an error."""
        # Should not raise
        await knowledge.delete("never_existed")

    @pytest.mark.asyncio
    async def test_list_keys_empty(self, knowledge):
        """list_keys returns empty list when no entries exist."""
        keys = await knowledge.list_keys()
        assert keys == []

    @pytest.mark.asyncio
    async def test_list_keys_returns_all_keys(self, knowledge):
        """list_keys returns all stored keys sorted alphabetically."""
        await knowledge.put("zebra", "z")
        await knowledge.put("alpha", "a")
        await knowledge.put("middle", "m")

        keys = await knowledge.list_keys()
        assert keys == ["alpha", "middle", "zebra"]

    @pytest.mark.asyncio
    async def test_list_keys_excludes_deleted(self, knowledge):
        """list_keys does not include deleted entries."""
        await knowledge.put("keep", "yes")
        await knowledge.put("remove", "no")
        await knowledge.delete("remove")

        keys = await knowledge.list_keys()
        assert keys == ["keep"]


# ---------------------------------------------------------------------------
# Tests: Actor Isolation (Requirement 9.6)
# ---------------------------------------------------------------------------


class TestDurableKnowledgeActorIsolation:
    """Tests verifying that knowledge entries are scoped to actor_id."""

    @pytest.mark.asyncio
    async def test_different_actors_have_separate_namespaces(
        self, knowledge, other_knowledge
    ):
        """Requirement 9.6: Each actor's knowledge is isolated."""
        # Actor A writes a preference
        await knowledge.put("color", "blue")

        # Actor B writes the same key with a different value
        await other_knowledge.put("color", "red")

        # Each actor sees only their own value
        assert await knowledge.get("color") == "blue"
        assert await other_knowledge.get("color") == "red"

    @pytest.mark.asyncio
    async def test_actor_cannot_see_other_actors_keys(
        self, knowledge, other_knowledge
    ):
        """Actor A cannot list or read Actor B's knowledge entries."""
        await other_knowledge.put("secret_pref", "hidden")

        # Actor A should not see Actor B's key
        keys = await knowledge.list_keys()
        assert "secret_pref" not in keys

        # Actor A reading the same key gets None (their own namespace is empty)
        result = await knowledge.get("secret_pref")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_does_not_affect_other_actor(
        self, knowledge, other_knowledge
    ):
        """Deleting a key for one actor does not affect another actor's same key."""
        await knowledge.put("shared_name", "actor_a_value")
        await other_knowledge.put("shared_name", "actor_b_value")

        # Actor A deletes their entry
        await knowledge.delete("shared_name")

        # Actor B's entry is unaffected
        assert await other_knowledge.get("shared_name") == "actor_b_value"
        assert await knowledge.get("shared_name") is None

    @pytest.mark.asyncio
    async def test_actor_id_property(self, knowledge, other_knowledge):
        """The actor_id property reflects the bound actor."""
        assert knowledge.actor_id == ACTOR_ID
        assert other_knowledge.actor_id == OTHER_ACTOR_ID


# ---------------------------------------------------------------------------
# Tests: Validation and Edge Cases
# ---------------------------------------------------------------------------


class TestDurableKnowledgeValidation:
    """Tests for input validation and edge cases."""

    @pytest.mark.asyncio
    async def test_empty_key_raises_on_get(self, knowledge):
        """Empty key raises ValueError on get."""
        with pytest.raises(ValueError, match="key must not be empty"):
            await knowledge.get("")

    @pytest.mark.asyncio
    async def test_empty_key_raises_on_put(self, knowledge):
        """Empty key raises ValueError on put."""
        with pytest.raises(ValueError, match="key must not be empty"):
            await knowledge.put("", "value")

    @pytest.mark.asyncio
    async def test_empty_key_raises_on_delete(self, knowledge):
        """Empty key raises ValueError on delete."""
        with pytest.raises(ValueError, match="key must not be empty"):
            await knowledge.delete("")

    @pytest.mark.asyncio
    async def test_non_serializable_value_raises_type_error(self, knowledge):
        """Non-JSON-serializable value raises TypeError."""
        with pytest.raises(TypeError, match="not JSON-serializable"):
            await knowledge.put("bad_value", object())

    @pytest.mark.asyncio
    async def test_complex_nested_value(self, knowledge):
        """Complex nested structures round-trip correctly."""
        complex_value = {
            "workflows": [
                {"name": "pipeline-1", "steps": 5, "active": True},
                {"name": "pipeline-2", "steps": 3, "active": False},
            ],
            "metadata": {"version": "1.0", "tags": ["prod", "ml"]},
        }
        await knowledge.put("complex_prefs", complex_value)
        result = await knowledge.get("complex_prefs")
        assert result == complex_value
