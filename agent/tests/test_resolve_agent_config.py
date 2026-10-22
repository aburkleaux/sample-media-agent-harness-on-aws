"""Tests for published-agent reads and the ResolveAgentConfig action.

Both exist to answer the dev-portal test console's core question: *when I call
this Custom Agent, which tools and skills actually load?* No invocation can
answer that — ``execution_summary.tools`` reports tools **called**, so a
bound-but-unused tool is invisible, and the bound counts only ever reached a
server-side log line.

Two invariants are load-bearing here and are asserted explicitly:

* ``SpecialtyCRUD.read`` stays draft-only. ``read_published`` is a sibling, not
  a relaxation — several callers depend on ``read`` rejecting non-drafts.
* ``read_published`` selects the same version a real external invocation would
  (``task_agent._pick_lineage_row``: explicit version, else ``current``, else
  highest non-deprecated). A console that resolved a different version than a
  real call would be worse than no console.

Uses moto-backed DynamoDB + S3 with a real ``AssetIndex`` so the lineage GSI
query is genuinely exercised rather than mocked.
"""

from __future__ import annotations

import os
import sys
import time
import uuid

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.asset_index import AssetIndex  # noqa: E402
from lab.context import LabRequestContext  # noqa: E402
from lab.gateway_browser import GatewayBrowser, GatewayEndpointConfig  # noqa: E402
from lab.models import GatewayToolDescriptor, SpecialtyAgent  # noqa: E402
from lab.s3_user_files_tool import S3UserFilesTool  # noqa: E402
from lab.service import LabService  # noqa: E402
from lab.specialty_crud import _pick_published_row  # noqa: E402

TABLE_NAME = "test-asset-index"
BUCKET_NAME = "test-artifacts-bucket"
ACTOR_ID = "actor-test-123"
OTHER_ACTOR = "actor-other-999"
SESSION_ID = "session-abc"

GW_TOOLS = ["media-processing___process_media", "shot-segmentation___segment_shots"]


class FakeGatewayClient:
    def __init__(self, tools=None):
        self._tools = tools or []

    def list_tools(self, actor_id: str, timeout: float):
        return self._tools


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
def wired():
    """A fully-wired LabService over moto DynamoDB + S3.

    Yields ``(service, index, s3, ctx)`` so tests can seed published rows
    directly through the real ``AssetIndex``.
    """
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_table(dynamodb)
        table = dynamodb.Table(TABLE_NAME)

        s3_client = boto3.client("s3", region_name="us-east-1")
        s3_client.create_bucket(Bucket=BUCKET_NAME)

        ctx = LabRequestContext(
            actor_id=ACTOR_ID, session_id=SESSION_ID, request_id=str(uuid.uuid4())
        )
        s3 = S3UserFilesTool(bucket=BUCKET_NAME, actor_id=ACTOR_ID)
        index = AssetIndex(ddb_table=table, actor_id=ACTOR_ID)

        gateway_browser = GatewayBrowser(
            gateway_client=FakeGatewayClient(
                tools=[
                    GatewayToolDescriptor(
                        name=n, description=f"{n} tool", version="1.0.0"
                    )
                    for n in GW_TOOLS
                ]
            ),
            configured_endpoints=[
                GatewayEndpointConfig(
                    endpoint_id="gw-test", url="https://gw.example.com/mcp"
                )
            ],
        )

        service = LabService(ctx=ctx, s3=s3, index=index, gateway_browser=gateway_browser)
        yield service, index, s3, ctx


async def _seed_published(
    index,
    s3,
    *,
    published_id: str,
    version: int,
    current: bool = False,
    deprecated: bool = False,
    owner: str = ACTOR_ID,
    persona: str = "You are a media assistant.",
    model=None,
    tool_collections=None,
    tool_collection_snapshots=None,
    compatible_skills=None,
):
    """Write one published SpecialtyAgent version + its persona.md.

    In the v2 schema ``asset_id`` IS the version-lineage id, and setting a
    positive ``version`` with ``scope="private"`` is what routes the row into
    the owner's VERSIONS partition and populates the byPublishedId GSI — so
    ``query_lineage`` finds it without any extra plumbing here.
    """
    now = int(time.time() * 1000)
    prefix = f"users/{owner}/published/specialty-agents/{published_id}/v{version}/"
    agent = SpecialtyAgent(
        asset_id=published_id,
        category="specialty-agent",
        name=f"agent-{published_id}",
        status="deprecated" if deprecated else "published",
        scope="private",
        s3_prefix=prefix,
        created_at=now,
        updated_at=now,
        compatible_skills=list(compatible_skills or []),
        model=model,
        tool_collections=list(tool_collections or []),
        # Passed through, NOT `or []`: `None` (unpinned) and `[]` (pinned to no
        # collections) are different states and the seeder must be able to
        # express both.
        tool_collection_snapshots=tool_collection_snapshots,
        version=version,
        current=current,
        deprecated=deprecated,
    )
    await index.put(agent, owner_user_id=owner)
    await s3.write_absolute(f"{prefix}persona.md", persona.encode("utf-8"))
    return agent


# ---------------------------------------------------------------------------
# read stays draft-only; read_published is the sibling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_still_refuses_published_rows(wired):
    """The draft-only contract on ``read`` is unchanged by this feature."""
    service, index, s3, ctx = wired
    await _seed_published(index, s3, published_id="ag-1", version=1, current=True)

    assert await service.specialty_crud.read(ctx=ctx, asset_id="ag-1") is None


@pytest.mark.asyncio
async def test_read_published_returns_the_current_version(wired):
    service, index, s3, ctx = wired
    await _seed_published(index, s3, published_id="ag-1", version=1)
    await _seed_published(
        index, s3, published_id="ag-1", version=2, current=True,
        persona="v2 persona",
    )
    await _seed_published(index, s3, published_id="ag-1", version=3)

    got = await service.specialty_crud.read_published(ctx=ctx, asset_id="ag-1")

    assert got is not None
    assert got.metadata["version"] == 2
    assert got.metadata["current"] is True
    assert got.persona == "v2 persona"


@pytest.mark.asyncio
async def test_read_published_honors_explicit_version(wired):
    service, index, s3, ctx = wired
    await _seed_published(index, s3, published_id="ag-1", version=1, persona="v1")
    await _seed_published(
        index, s3, published_id="ag-1", version=2, current=True, persona="v2"
    )

    got = await service.specialty_crud.read_published(
        ctx=ctx, asset_id="ag-1", version=1
    )

    assert got is not None and got.metadata["version"] == 1
    assert got.persona == "v1"


@pytest.mark.asyncio
async def test_read_published_skips_deprecated_when_no_current(wired):
    """Highest NON-deprecated wins when nothing is flagged current."""
    service, index, s3, ctx = wired
    await _seed_published(index, s3, published_id="ag-1", version=1, persona="v1")
    await _seed_published(
        index, s3, published_id="ag-1", version=2, deprecated=True, persona="v2"
    )

    got = await service.specialty_crud.read_published(ctx=ctx, asset_id="ag-1")

    assert got is not None and got.metadata["version"] == 1


@pytest.mark.asyncio
async def test_read_published_missing_lineage_is_not_found(wired):
    service, _index, _s3, ctx = wired
    assert await service.specialty_crud.read_published(ctx=ctx, asset_id="nope") is None


@pytest.mark.asyncio
async def test_read_published_rejects_another_actors_row(wired):
    """Cross-actor read surfaces as not-found (no existence disclosure)."""
    service, index, s3, ctx = wired
    await _seed_published(
        index, s3, published_id="ag-x", version=1, current=True, owner=OTHER_ACTOR
    )

    assert await service.specialty_crud.read_published(ctx=ctx, asset_id="ag-x") is None


@pytest.mark.asyncio
async def test_read_published_tolerates_missing_persona(wired):
    """A published row whose persona.md is absent reads as empty, not a crash."""
    service, index, s3, ctx = wired
    agent = await _seed_published(
        index, s3, published_id="ag-1", version=1, current=True
    )
    await s3.delete_absolute(f"{agent.s3_prefix}persona.md")

    got = await service.specialty_crud.read_published(ctx=ctx, asset_id="ag-1")

    assert got is not None and got.persona == ""


def test_row_picker_matches_external_invoke_selection():
    """Selection order: explicit version, then current, then highest live.

    Mirrors ``task_agent._pick_lineage_row``; the duplication is deliberate
    (documented at the helper) so keep the two in lockstep.
    """
    class Row:
        def __init__(self, v, cur=False, dep=False):
            self.version, self.current, self.deprecated = v, cur, dep

    rows = [Row(1), Row(2, cur=True), Row(3)]
    assert _pick_published_row(rows, None).version == 2
    assert _pick_published_row(rows, 1).version == 1
    assert _pick_published_row([Row(1), Row(2), Row(3, dep=True)], None).version == 2
    assert _pick_published_row([Row(1, dep=True)], None) is None
    assert _pick_published_row([], None) is None


# ---------------------------------------------------------------------------
# ResolveAgentConfig
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_reports_runtime_default_when_model_unset(wired):
    service, index, s3, _ctx = wired
    await _seed_published(index, s3, published_id="ag-1", version=1, current=True)

    out = await service.handle(
        {"action": "ResolveAgentConfig", "assetId": "ag-1", "source": "published"}
    )

    assert out["modelId"] is None
    assert out["modelSource"] == "runtime-default"
    assert out["version"] == 1
    assert out["source"] == "published"


@pytest.mark.asyncio
async def test_resolve_reports_pinned_model(wired):
    service, index, s3, _ctx = wired
    await _seed_published(
        index, s3, published_id="ag-1", version=1, current=True,
        model="us.anthropic.claude-sonnet-5",
    )

    out = await service.handle(
        {"action": "ResolveAgentConfig", "assetId": "ag-1", "source": "published"}
    )

    assert out["modelId"] == "us.anthropic.claude-sonnet-5"
    assert out["modelSource"] == "pinned"


@pytest.mark.asyncio
async def test_resolve_never_returns_persona_text(wired):
    """The published view withholds persona by design; this must not leak it."""
    service, index, s3, _ctx = wired
    secret = "SECRET-PERSONA-BODY"
    await _seed_published(
        index, s3, published_id="ag-1", version=1, current=True, persona=secret
    )

    out = await service.handle(
        {"action": "ResolveAgentConfig", "assetId": "ag-1", "source": "published"}
    )

    assert secret not in repr(out)
    assert "persona" not in out
    assert out["personaChars"] == len(secret)


@pytest.mark.asyncio
async def test_resolve_drops_unreadable_tool_collection(wired):
    """A selected-but-unreadable collection is REPORTED, not silently empty.

    This is the single most likely cause of "my agent can't do the thing", and
    before this action it was invisible outside a server log.
    """
    service, index, s3, _ctx = wired
    await _seed_published(
        index, s3, published_id="ag-1", version=1, current=True,
        tool_collections=["tc-missing"],
    )

    out = await service.handle(
        {"action": "ResolveAgentConfig", "assetId": "ag-1", "source": "published"}
    )

    assert out["droppedCollections"] == ["tc-missing"]
    assert out["tools"] == []


@pytest.mark.asyncio
async def test_resolve_uses_the_versions_pinned_collections(wired):
    """The console must report what an invoke binds, which is the version's pin.

    Reading the owner's live collection rows here instead would let the console
    confidently show a tool set no invocation would use — and report a
    since-deleted collection as dropped while the real version still runs it.
    ``read_published`` carries the pin so both paths agree.
    """
    from lab.models import ToolCollectionSnapshot, ToolRef

    service, index, s3, _ctx = wired
    await _seed_published(
        index, s3, published_id="ag-1", version=1, current=True,
        tool_collections=["tc-gone"],
        tool_collection_snapshots=[
            ToolCollectionSnapshot(
                asset_id="tc-gone",
                name="media-tools",
                tool_refs=[ToolRef(name=GW_TOOLS[0], version="1.0.0")],
            )
        ],
    )

    out = await service.handle(
        {"action": "ResolveAgentConfig", "assetId": "ag-1", "source": "published"}
    )

    # No collection row exists for `tc-gone` at all — only the pin does.
    assert out["droppedCollections"] == []
    assert out["tools"] == [GW_TOOLS[0]]
    assert out["toolCollections"] == [{"id": "tc-gone", "name": "media-tools"}]


@pytest.mark.asyncio
async def test_resolve_surfaces_compatible_skills(wired):
    service, index, s3, _ctx = wired
    await _seed_published(
        index, s3, published_id="ag-1", version=1, current=True,
        compatible_skills=["skill-a", "skill-b"],
    )

    out = await service.handle(
        {"action": "ResolveAgentConfig", "assetId": "ag-1", "source": "published"}
    )

    assert out["skills"] == ["skill-a", "skill-b"]


@pytest.mark.asyncio
async def test_resolve_not_found_for_unknown_agent(wired):
    service, _index, _s3, _ctx = wired
    out = await service.handle(
        {"action": "ResolveAgentConfig", "assetId": "nope", "source": "published"}
    )
    assert out == {"error": "not_found"}


@pytest.mark.asyncio
async def test_resolve_rejects_bad_params(wired):
    service, _index, _s3, _ctx = wired

    missing = await service.handle({"action": "ResolveAgentConfig"})
    assert missing["error"] == "invalid_params"

    bad_source = await service.handle(
        {"action": "ResolveAgentConfig", "assetId": "ag-1", "source": "sideways"}
    )
    assert bad_source["error"] == "invalid_params"


@pytest.mark.asyncio
async def test_resolve_action_is_llm_bypassed():
    """The action must skip the LLM path like every other CRUD action."""
    import main_lab_tools

    assert "ResolveAgentConfig" in main_lab_tools.LAB_BYPASS_ACTIONS
