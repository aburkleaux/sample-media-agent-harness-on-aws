"""Validation pins the version the runtime would have chosen.

The pinned integer must come from ``read_published`` (explicit → ``current`` →
highest non-deprecated), NOT from ``ListMyPublishedAssets`` (latest
non-deprecated). Those agree until a rollback flips ``current`` to an older
snapshot without deprecating the newer one.
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
    description: str = "",
    model=None,
    tool_collections=None,
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
        description=description,
        status="deprecated" if deprecated else "published",
        scope="private",
        s3_prefix=prefix,
        created_at=now,
        updated_at=now,
        compatible_skills=list(compatible_skills or []),
        model=model,
        tool_collections=list(tool_collections or []),
        version=version,
        current=current,
        deprecated=deprecated,
    )
    await index.put(agent, owner_user_id=owner)
    await s3.write_absolute(f"{prefix}persona.md", persona.encode("utf-8"))
    return agent


@pytest.mark.asyncio
async def test_resolves_the_current_version(wired):
    service, index, s3, _ctx = wired
    # Seed a lineage: v1, v2 current, v3 not deprecated.
    await _seed_published(index, s3, published_id="ag-1", version=1)
    await _seed_published(
        index, s3, published_id="ag-1", version=2, current=True,
        description="Current version"
    )
    await _seed_published(index, s3, published_id="ag-1", version=3)

    got = await service.handle({
        "action": "ResolvePublishedAgentRef",
        "assetId": "ag-1",
    })
    assert got["version"] == 2
    assert got["assetId"] == "ag-1"


@pytest.mark.asyncio
async def test_pins_current_not_latest_after_a_rollback(wired):
    """The rollback case — the whole reason pinning uses read_published.

    current=v2 while the highest non-deprecated is v3. Anything that pinned
    from ListMyPublishedAssets would answer 3 here and diverge from a real
    /invoke, so this inequality is the assertion.
    """
    service, index, s3, _ctx = wired
    # Seed: v2 flagged current, v3 published and NOT deprecated.
    await _seed_published(index, s3, published_id="ag-1", version=1)
    await _seed_published(
        index, s3, published_id="ag-1", version=2, current=True
    )
    await _seed_published(index, s3, published_id="ag-1", version=3)

    ref = await service.handle({
        "action": "ResolvePublishedAgentRef",
        "assetId": "ag-1",
    })
    listed = await service.handle({
        "action": "ListMyPublishedAssets",
        "category": "specialty-agent",
    })
    latest = next(r for r in listed if r["assetId"] == "ag-1")["version"]
    assert ref["version"] == 2
    assert latest == 3
    assert ref["version"] != latest


@pytest.mark.asyncio
async def test_explicit_version_wins(wired):
    service, index, s3, _ctx = wired
    await _seed_published(index, s3, published_id="ag-1", version=1)
    await _seed_published(
        index, s3, published_id="ag-1", version=2, current=True
    )

    got = await service.handle({
        "action": "ResolvePublishedAgentRef",
        "assetId": "ag-1",
        "version": 1,
    })
    assert got["version"] == 1


@pytest.mark.asyncio
async def test_unknown_agent_is_not_found(wired):
    service, _index, _s3, _ctx = wired
    got = await service.handle({
        "action": "ResolvePublishedAgentRef",
        "assetId": "nope",
    })
    assert got["error"] == "agent_not_found"


@pytest.mark.asyncio
async def test_another_actors_agent_is_not_found(wired):
    """Owner scoping is enforced by read_published; no existence disclosure."""
    service, index, s3, _ctx = wired
    # Seed a published row whose s3_prefix is users/<other>/...
    await _seed_published(
        index, s3, published_id="ag-other", version=1, current=True,
        owner=OTHER_ACTOR
    )

    got = await service.handle({
        "action": "ResolvePublishedAgentRef",
        "assetId": "ag-other",
    })
    assert got["error"] == "agent_not_found"


@pytest.mark.asyncio
async def test_draft_only_agent_is_not_published(wired):
    """A different remedy from not-found, so a different code."""
    service, _index, _s3, _ctx = wired
    created = await service.handle({
        "action": "CreateSpecialtyAgentDraft",
        "name": "Unpublished", "persona": "p",
    })
    got = await service.handle({
        "action": "ResolvePublishedAgentRef",
        "assetId": created["assetId"],
    })
    assert got["error"] == "agent_not_published"


@pytest.mark.asyncio
async def test_unknown_explicit_version_is_version_not_found(wired):
    service, index, s3, _ctx = wired
    await _seed_published(
        index, s3, published_id="ag-1", version=1, current=True
    )

    got = await service.handle({
        "action": "ResolvePublishedAgentRef",
        "assetId": "ag-1",
        "version": 99,
    })
    assert got["error"] == "version_not_found"


@pytest.mark.asyncio
async def test_returns_the_description(wired):
    service, index, s3, _ctx = wired
    await _seed_published(
        index, s3, published_id="ag-1", version=1, current=True,
        description="Test agent description"
    )

    got = await service.handle({
        "action": "ResolvePublishedAgentRef",
        "assetId": "ag-1",
    })
    assert "description" in got
    assert got["description"] == "Test agent description"


def test_explicit_version_selection_matches_the_runtime():
    """The duplication AGENTS.md warns about, asserted directly.

    ``task_agent._pick_lineage_row`` and
    ``lab.specialty_crud._pick_published_row`` implement the same rule in two
    places. For an explicit version they must pick the same row, which is what
    makes wire-level version pinning safe: once the integer is pinned, whichever
    function runs is irrelevant.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from task_agent import _pick_lineage_row
    from lab.specialty_crud import _pick_published_row
    from lab.models import SpecialtyAgent

    def _row(version: int, current: bool, deprecated: bool = False):
        return SpecialtyAgent(
            asset_id="ag-1",
            category="specialty-agent",
            name="n",
            status="published",
            scope="private",
            s3_prefix=f"users/u1/published/specialty-agents/ag-1/v{version}/",
            created_at=1,
            updated_at=1,
            version=version,
            current=current,
            deprecated=deprecated,
        )

    # current=v2 while v3 is the highest non-deprecated — the rollback shape.
    lineage = [_row(1, False), _row(2, True), _row(3, False)]
    for explicit in (1, 2, 3):
        a = _pick_lineage_row(lineage, explicit)
        b = _pick_published_row(lineage, explicit)
        assert a.version == b.version == explicit, (explicit, a, b)
