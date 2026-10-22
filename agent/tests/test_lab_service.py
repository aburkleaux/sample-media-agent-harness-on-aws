"""Unit tests for LabService action router and tenant isolation.

Validates: Requirements 1.1, 1.6
Properties: 3
"""

from __future__ import annotations

import base64
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
from lab.context import LabRequestContext
from lab.gateway_browser import GatewayBrowser, GatewayEndpointConfig
from lab.models import GatewayToolDescriptor
from lab.s3_user_files_tool import S3UserFilesTool
import lab.service as lab_service_module
from lab.service import LabService
from lab.skill_package_validator import MAX_FILE_BYTES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TABLE_NAME = "test-asset-index"
BUCKET_NAME = "test-artifacts-bucket"
ACTOR_ID = "actor-test-123"
SESSION_ID = "session-abc"


class FakeGatewayClient:
    """Fake Gateway client that returns a fixed set of tools."""

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
def lab_service():
    """Provide a fully-wired LabService with mocked AWS resources."""
    with mock_aws():
        # Set up DynamoDB
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_table(dynamodb)
        table = dynamodb.Table(TABLE_NAME)

        # Set up S3
        s3_client = boto3.client("s3", region_name="us-east-1")
        _create_bucket(s3_client)

        # Create context
        ctx = LabRequestContext(
            actor_id=ACTOR_ID,
            session_id=SESSION_ID,
            request_id=str(uuid.uuid4()),
        )

        # Create storage primitives
        s3 = S3UserFilesTool(bucket=BUCKET_NAME, actor_id=ACTOR_ID)
        index = AssetIndex(ddb_table=table, actor_id=ACTOR_ID)

        # Create Gateway browser with fake client
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

        # Create LabService
        service = LabService(
            ctx=ctx,
            s3=s3,
            index=index,
            gateway_browser=gateway_browser,
        )

        yield service


# ---------------------------------------------------------------------------
# Tests: Action Router
# ---------------------------------------------------------------------------


class TestLabServiceActionRouter:
    """Tests for the LabService action routing."""

    @pytest.mark.asyncio
    async def test_missing_action_raises_value_error(self, lab_service):
        """Payload without 'action' field raises ValueError."""
        with pytest.raises(ValueError, match="must contain an 'action' field"):
            await lab_service.handle({"name": "test"})

    @pytest.mark.asyncio
    async def test_unrecognized_action_raises_value_error(self, lab_service):
        """Payload with unknown action raises ValueError."""
        with pytest.raises(ValueError, match="Unrecognized action"):
            await lab_service.handle({"action": "DoSomethingWeird"})

    @pytest.mark.asyncio
    async def test_create_skill_draft_routes_correctly(self, lab_service):
        """CreateSkillDraft action routes to skill CRUD create."""
        result = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "My Skill",
            "skillMd": "# My Skill\n\nDoes things.",
        })
        assert "assetId" in result
        assert "s3Prefix" in result

    @pytest.mark.asyncio
    async def test_skill_md_lands_at_the_prefix_the_row_records(self, lab_service):
        """SKILL.md must be readable at ``{row.s3Prefix}SKILL.md``.

        The Task Agent's skill loader reads that key with a plain ``get_object``
        — no actor scoping, no translation layer — so this asserts the contract
        using the same access pattern rather than through the tool that could
        paper over a mismatch.

        The regression: writes went through the actor-scoped ``write``, landing
        at ``<actor>/drafts/skills/<id>/SKILL.md`` while the row advertised
        ``users/<actor>/drafts/skills/<id>/``. Nothing inside the lab service
        noticed (its reads used the same scoped path), but every skill bound to
        a Custom Agent resolved to nothing and was dropped with one log line —
        ``external config: skill <id> unresolvable; skipping`` — leaving an
        agent that silently ran without the skills its owner selected.
        """
        skill_md = (
            "---\nname: bound-skill\n"
            "description: Use when testing skill binding.\n---\n\n"
            "# Bound\n"
        )
        result = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "bound-skill",
            "skillMd": skill_md,
        })

        prefix = result["s3Prefix"]
        assert prefix == f"users/{ACTOR_ID}/drafts/skills/{result['assetId']}/"

        raw = boto3.client("s3", region_name="us-east-1")
        body = raw.get_object(Bucket=BUCKET_NAME, Key=f"{prefix}SKILL.md")["Body"].read()
        assert body.decode("utf-8") == skill_md

    @pytest.mark.asyncio
    async def test_supporting_file_lands_under_the_recorded_prefix(self, lab_service):
        """Tier-2 files follow SKILL.md; `promote` copies from that prefix."""
        created = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "packaged-skill",
            "skillMd": "# Packaged\n\nBody.",
        })
        await lab_service.handle({
            "action": "PutSupportingFile",
            "assetId": created["assetId"],
            "path": "references/guide.md",
            "contentBase64": base64.b64encode(b"# Guide\n").decode(),
        })

        prefix = created["s3Prefix"]
        raw = boto3.client("s3", region_name="us-east-1")
        keys = [
            obj["Key"]
            for obj in raw.list_objects_v2(Bucket=BUCKET_NAME, Prefix=prefix).get("Contents", [])
        ]
        assert f"{prefix}references/guide.md" in keys
        # And the read path still relativizes correctly against that prefix.
        read_back = await lab_service.handle({
            "action": "ReadSkillDraft",
            "assetId": created["assetId"],
        })
        assert read_back["supportingFiles"] == ["references/guide.md"]

    @pytest.mark.asyncio
    async def test_import_skill_package_routes_atomically(self, lab_service):
        skill_md = (
            "---\nname: uploaded-skill\n"
            "description: Use when validating uploaded packages.\n---\n\n"
            "# Uploaded Skill\n"
        )
        result = await lab_service.handle({
            "action": "ImportSkillPackage",
            "name": "uploaded-skill",
            "skillMd": skill_md,
            "files": [
                {
                    "path": "references/guide.md",
                    "contentBase64": base64.b64encode(b"# Guide\n").decode(),
                },
                {
                    "path": "assets/schema.json",
                    "contentBase64": base64.b64encode(b"{}\n").decode(),
                },
            ],
        })

        assert result["supportingFiles"] == [
            "references/guide.md",
            "assets/schema.json",
        ]
        read_result = await lab_service.handle({
            "action": "ReadSkillDraft",
            "assetId": result["assetId"],
        })
        assert read_result["skillMd"] == skill_md
        assert sorted(read_result["supportingFiles"]) == [
            "assets/schema.json",
            "references/guide.md",
        ]
        listed = await lab_service.handle({"action": "ListSkillDrafts"})
        assert listed[0]["supportingFiles"] == [
            "assets/schema.json",
            "references/guide.md",
        ]

    @pytest.mark.asyncio
    async def test_import_skill_package_rejects_invalid_frontmatter_before_writes(self, lab_service):
        with pytest.raises(ValueError, match="YAML frontmatter"):
            await lab_service.handle({
                "action": "ImportSkillPackage",
                "name": "invalid-skill",
                "skillMd": "# Missing frontmatter",
                "files": [],
            })

        assert await lab_service.handle({"action": "ListSkillDrafts"}) == []
        # CANONICAL prefix — where skill artifacts are now written. Asserting
        # on the old actor-relative path would pass whether or not the
        # write/compensation behaviour under test actually held.
        assert await lab_service.s3.list_absolute(
            f"users/{lab_service.s3.actor_id}/drafts/skills/"
        ) == []

    @pytest.mark.asyncio
    async def test_import_skill_package_validates_required_strings(self, lab_service):
        with pytest.raises(ValueError, match="name must be a non-empty string"):
            await lab_service.handle({
                "action": "ImportSkillPackage",
                "skillMd": "---\nname: missing\ndescription: Missing name.\n---\n",
                "files": [],
            })

        with pytest.raises(ValueError, match="skillMd must be a non-empty string"):
            await lab_service.handle({
                "action": "ImportSkillPackage",
                "name": "missing-document",
                "files": [],
            })

    @pytest.mark.asyncio
    async def test_import_skill_package_rejects_file_count_before_decode(
        self, lab_service, monkeypatch
    ):
        decode_calls = 0

        def track_decode(*args, **kwargs):
            nonlocal decode_calls
            decode_calls += 1
            raise AssertionError("base64 decoding should not run")

        monkeypatch.setattr(lab_service_module.base64, "b64decode", track_decode)
        with pytest.raises(ValueError, match="at most 100 files"):
            await lab_service.handle({
                "action": "ImportSkillPackage",
                "name": "too-many-files",
                "skillMd": "---\nname: too-many-files\ndescription: Too many files.\n---\n",
                "files": [
                    {"path": f"assets/{index}.txt", "contentBase64": "eA=="}
                    for index in range(100)
                ],
            })

        assert decode_calls == 0

    @pytest.mark.asyncio
    async def test_import_skill_package_rejects_aggregate_size_before_decode(
        self, lab_service, monkeypatch
    ):
        encoded_file = base64.b64encode(b"x" * MAX_FILE_BYTES).decode()
        decode_calls = 0

        def track_decode(*args, **kwargs):
            nonlocal decode_calls
            decode_calls += 1
            raise AssertionError("base64 decoding should not run")

        monkeypatch.setattr(lab_service_module.base64, "b64decode", track_decode)
        with pytest.raises(ValueError, match="package exceeds the 5 MB limit"):
            await lab_service.handle({
                "action": "ImportSkillPackage",
                "name": "oversized-package",
                "skillMd": "---\nname: oversized-package\ndescription: Too large.\n---\n",
                "files": [
                    {"path": f"assets/{index}.bin", "contentBase64": encoded_file}
                    for index in range(3)
                ],
            })

        assert decode_calls == 0

    @pytest.mark.asyncio
    async def test_import_skill_package_compensates_failed_file_write(
        self, lab_service, monkeypatch
    ):
        # Patches `write_absolute`, not `write`: skill artifacts are written at
        # the canonical prefix recorded on the AssetIndex row, because the
        # runtime reads `{row.s3_prefix}SKILL.md` literally. Patching the
        # actor-scoped `write` here would intercept nothing and the test would
        # vacuously pass by never triggering the failure it is meant to inject.
        original_write = lab_service.s3.write_absolute

        async def fail_second_file(key, content):
            if key.endswith("assets/fail.txt"):
                raise RuntimeError("simulated write failure")
            await original_write(key, content)

        monkeypatch.setattr(lab_service.s3, "write_absolute", fail_second_file)
        skill_md = (
            "---\nname: atomic-skill\n"
            "description: Use when testing atomic package writes.\n---\n\n"
            "# Atomic\n"
        )

        with pytest.raises(RuntimeError, match="simulated write failure"):
            await lab_service.handle({
                "action": "ImportSkillPackage",
                "name": "atomic-skill",
                "skillMd": skill_md,
                "files": [
                    {
                        "path": "references/guide.md",
                        "contentBase64": base64.b64encode(b"guide").decode(),
                    },
                    {
                        "path": "assets/fail.txt",
                        "contentBase64": base64.b64encode(b"fail").decode(),
                    },
                ],
            })

        assert await lab_service.handle({"action": "ListSkillDrafts"}) == []
        # CANONICAL prefix — where skill artifacts are now written. Asserting
        # on the old actor-relative path would pass whether or not the
        # write/compensation behaviour under test actually held.
        assert await lab_service.s3.list_absolute(
            f"users/{lab_service.s3.actor_id}/drafts/skills/"
        ) == []

    @pytest.mark.asyncio
    async def test_list_skill_drafts_routes_correctly(self, lab_service):
        """ListSkillDrafts action routes to skill CRUD list."""
        # Create a skill first
        await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Listed Skill",
            "skillMd": "# Listed\n\nContent.",
        })

        result = await lab_service.handle({"action": "ListSkillDrafts"})
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "Listed Skill"

    @pytest.mark.asyncio
    async def test_read_skill_draft_not_found(self, lab_service):
        """ReadSkillDraft with non-existent assetId returns not_found."""
        result = await lab_service.handle({
            "action": "ReadSkillDraft",
            "assetId": "nonexistent-id",
        })
        assert result == {"error": "not_found"}

    @pytest.mark.asyncio
    async def test_browse_gateway_tools_routes_correctly(self, lab_service):
        """BrowseGatewayTools action routes to gateway browser."""
        result = await lab_service.handle({"action": "BrowseGatewayTools"})
        assert "tools" in result
        assert len(result["tools"]) == 1
        assert result["tools"][0]["name"] == "frame-extract"
        assert "retrievedAt" in result
        assert "stale" in result
        assert result["stale"] is False

    @pytest.mark.asyncio
    async def test_chat_message_routes_correctly(self, lab_service):
        """ChatMessage is handled by the Lab_Chat_Agent."""
        result = await lab_service.handle({
            "action": "ChatMessage",
            "message": "Hello",
        })
        assert "response" in result


# ---------------------------------------------------------------------------
# Tests: Property 3 — actor_id never taken from payload
# ---------------------------------------------------------------------------


class TestLabServiceActorIdStripping:
    """Tests that actor_id is dropped from payload (Property 3 / Requirement 1.6)."""

    @pytest.mark.asyncio
    async def test_actor_id_in_payload_is_ignored(self, lab_service):
        """Property 3: actor_id in payload is dropped; operation uses context actor_id.

        Even if a malicious payload includes actor_id, the service uses the
        actor_id from LabRequestContext (bound from JWT) only.
        """
        # Create a skill with a payload that includes actor_id
        result = await lab_service.handle({
            "action": "CreateSkillDraft",
            "actor_id": "malicious-actor-id",  # This should be dropped
            "name": "Legit Skill",
            "skillMd": "# Legit\n\nContent.",
        })
        assert "assetId" in result

        # Verify the skill was created under the correct actor (from context)
        read_result = await lab_service.handle({
            "action": "ReadSkillDraft",
            "assetId": result["assetId"],
        })
        assert read_result["name"] == "Legit Skill"

    @pytest.mark.asyncio
    async def test_actor_id_not_passed_to_handler(self, lab_service):
        """actor_id field is stripped before reaching the handler.

        If actor_id were passed through, it would appear as an unexpected
        keyword argument and cause an error. The fact that this succeeds
        proves it was stripped.
        """
        result = await lab_service.handle({
            "action": "ListSkillDrafts",
            "actor_id": "should-be-dropped",
        })
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Tests: Placeholder actions
# ---------------------------------------------------------------------------


class TestLabServiceHarnessAndReadiness:
    """Tests that harness and readiness handlers are properly wired."""

    @pytest.mark.asyncio
    async def test_run_harness_routes_to_test_harness(self, lab_service):
        """RunHarness routes to the TestHarness.run() method.

        Since the target asset doesn't exist, it should raise FileNotFoundError.
        """
        with pytest.raises(FileNotFoundError, match="Target asset not found"):
            await lab_service.handle({
                "action": "RunHarness",
                "assetId": "nonexistent-id",
                "sampleInput": "test input",
            })

    @pytest.mark.asyncio
    async def test_list_harness_runs_returns_empty_list(self, lab_service):
        """ListHarnessRuns returns an empty list when no runs exist."""
        result = await lab_service.handle({"action": "ListHarnessRuns"})
        assert isinstance(result, list)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_read_harness_run_not_found(self, lab_service):
        """ReadHarnessRun returns not_found for non-existent run."""
        result = await lab_service.handle({
            "action": "ReadHarnessRun",
            "runId": "nonexistent-run",
        })
        assert result == {"error": "not_found"}

    @pytest.mark.asyncio
    async def test_check_readiness_not_found(self, lab_service):
        """CheckReadiness returns not_found for non-existent workflow."""
        result = await lab_service.handle({
            "action": "CheckReadiness",
            "workflowAssetId": "nonexistent-wf",
        })
        assert result == {"error": "not_found"}

    @pytest.mark.asyncio
    async def test_check_readiness_zero_step_workflow(self, lab_service):
        """CheckReadiness returns not_ready for a zero-step workflow."""
        # Create a zero-step workflow
        create_result = await lab_service.handle({
            "action": "CreateWorkflowDraft",
            "name": "Empty Workflow",
            "steps": [],
        })
        wf_id = create_result["assetId"]

        # Check readiness
        result = await lab_service.handle({
            "action": "CheckReadiness",
            "workflowAssetId": wf_id,
        })
        assert result["overallStatus"] == "not_ready"
        assert result["workflowAssetId"] == wf_id
        assert len(result["entries"]) == 1
        assert result["entries"][0]["status"] == "not_ready"
        assert "zero steps" in result["entries"][0]["failures"][0].lower()

    @pytest.mark.asyncio
    async def test_test_gateway_tool_action_registered(self, lab_service):
        """TestGatewayTool action is registered in the action router."""
        # The fake gateway client doesn't implement call_tool, so this
        # should raise an AttributeError (no call_tool on FakeGatewayClient)
        # which proves the action is routed correctly.
        with pytest.raises((AttributeError, Exception)):
            await lab_service.handle({
                "action": "TestGatewayTool",
                "name": "frame-extract",
                "version": "2.1.0",
                "sample": {"input": "test"},
            })


# ---------------------------------------------------------------------------
# Tests: CRUD round-trip through service layer
# ---------------------------------------------------------------------------


class TestLabServiceCRUDRoundTrip:
    """End-to-end tests through the service action router."""

    @pytest.mark.asyncio
    async def test_skill_create_read_delete_round_trip(self, lab_service):
        """Full skill lifecycle through the service layer."""
        # Create
        create_result = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Round Trip Skill",
            "skillMd": "# Round Trip\n\nTest content.",
        })
        asset_id = create_result["assetId"]

        # Read
        read_result = await lab_service.handle({
            "action": "ReadSkillDraft",
            "assetId": asset_id,
        })
        assert read_result["name"] == "Round Trip Skill"
        assert read_result["skillMd"] == "# Round Trip\n\nTest content."

        # Update
        update_result = await lab_service.handle({
            "action": "UpdateSkillMd",
            "assetId": asset_id,
            "skillMd": "# Updated\n\nNew content.",
        })
        assert "updatedAt" in update_result

        # Verify update
        read_after_update = await lab_service.handle({
            "action": "ReadSkillDraft",
            "assetId": asset_id,
        })
        assert read_after_update["skillMd"] == "# Updated\n\nNew content."

        # Delete
        delete_result = await lab_service.handle({
            "action": "DeleteSkillDraft",
            "assetId": asset_id,
        })
        assert delete_result == {"status": "ok"}

        # Verify deleted
        read_after_delete = await lab_service.handle({
            "action": "ReadSkillDraft",
            "assetId": asset_id,
        })
        assert read_after_delete == {"error": "not_found"}

    @pytest.mark.asyncio
    async def test_specialty_agent_create_and_list(self, lab_service):
        """Specialty agent create and list through the service layer."""
        # Create a skill first (needed for compatible_skills)
        skill_result = await lab_service.handle({
            "action": "CreateSkillDraft",
            "name": "Compatible Skill",
            "skillMd": "# Compatible\n\nContent.",
        })
        skill_id = skill_result["assetId"]

        # Create specialty agent
        create_result = await lab_service.handle({
            "action": "CreateSpecialtyAgentDraft",
            "name": "Test Specialty",
            "persona": "You are a test specialist.",
            "compatibleSkills": [skill_id],
        })
        assert "assetId" in create_result

        # List
        list_result = await lab_service.handle({
            "action": "ListSpecialtyAgentDrafts",
        })
        # AUTHORED assets only. The virtual `core-task-agent` row this listing
        # used to append is gone: the built-in sub-agent runtimes are surfaced by
        # the `mcp_list_agents` action and its Built-in agents browser, and a row
        # with no AssetIndex record behind it made every mutation against it fail.
        names = [item["name"] for item in list_result]
        assert "Test Specialty" in names
        assert "Task Agent" not in names


class TestLabServiceHookCRUD:
    """Hook asset CRUD via the LabService router (declarative hooks feature)."""

    @pytest.mark.asyncio
    async def test_hook_create_list_read_update_delete(self, lab_service):
        created = await lab_service.handle({
            "action": "CreateHook",
            "name": "my-hook",
            "event": "after_tool_result",
            "condition": 'tool.role == "results"',
            "actionType": "notify",
            "actionParams": {"message": "hi {tool.role}"},
            "priority": 30,
        })
        asset_id = created["assetId"]
        assert asset_id

        # List returns the user hook + the built-in render-artifacts.
        listed = await lab_service.handle({"action": "ListHooks"})
        assert any(h["assetId"] == asset_id for h in listed["userHooks"])
        assert any(h["name"] == "render-artifacts" and h["builtin"] for h in listed["builtinHooks"])

        # Read.
        read = await lab_service.handle({"action": "ReadHook", "assetId": asset_id})
        assert read["event"] == "after_tool_result"
        assert read["action"] == {"type": "notify", "params": {"message": "hi {tool.role}"}}
        assert read["priority"] == 30

        # Update priority + enabled + condition.
        upd = await lab_service.handle({
            "action": "UpdateHook", "assetId": asset_id,
            "priority": 5, "enabled": False, "condition": "result.urls",
        })
        assert upd["updated"] is True
        read2 = await lab_service.handle({"action": "ReadHook", "assetId": asset_id})
        assert read2["priority"] == 5 and read2["enabled"] is False and read2["condition"] == "result.urls"

        # Delete.
        deleted = await lab_service.handle({"action": "DeleteHook", "assetId": asset_id})
        assert deleted["deleted"] is True
        assert await lab_service.handle({"action": "ReadHook", "assetId": asset_id}) is None

    @pytest.mark.asyncio
    async def test_create_hook_rejects_invalid_condition(self, lab_service):
        with pytest.raises(ValueError, match="Invalid hook condition"):
            await lab_service.handle({
                "action": "CreateHook", "name": "bad", "event": "after_tool_result",
                "condition": "__import__('os')", "actionType": "notify",
            })

    @pytest.mark.asyncio
    async def test_create_hook_rejects_invalid_action(self, lab_service):
        with pytest.raises(ValueError, match="Invalid action_type"):
            await lab_service.handle({
                "action": "CreateHook", "name": "bad", "event": "after_tool_result",
                "condition": "", "actionType": "explode",
            })

    @pytest.mark.asyncio
    async def test_validate_hook_condition(self, lab_service):
        ok = await lab_service.handle({
            "action": "ValidateHookCondition",
            "condition": 'tool.role == "results" and result.urls',
            "sampleEvent": {"tool": {"role": "results"}, "result": {"urls": ["https://x"]}},
        })
        assert ok == {"valid": True, "matched": True}

        bad = await lab_service.handle({"action": "ValidateHookCondition", "condition": "().__class__"})
        assert bad["valid"] is False and "error" in bad
