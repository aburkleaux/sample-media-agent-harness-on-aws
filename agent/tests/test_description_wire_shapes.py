"""The Lab service action envelope carries ``description`` in and out.

``list_custom_agents`` reads ``ListMyPublishedAssets``, so that handler in
particular must return it or Phase 2 has nothing to select on.
"""
from __future__ import annotations

import os
import sys
import uuid

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.asset_index import AssetIndex  # noqa: E402
from lab.context import LabRequestContext  # noqa: E402
from lab.gateway_browser import GatewayBrowser, GatewayEndpointConfig  # noqa: E402
from lab.models import GatewayToolDescriptor  # noqa: E402
from lab.s3_user_files_tool import S3UserFilesTool  # noqa: E402
from lab.service import LabService  # noqa: E402

TABLE_NAME = "test-asset-index"
BUCKET_NAME = "test-artifacts-bucket"
ACTOR_ID = "actor-test-123"
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

    Yields ``(service, ctx)`` for CRUD tests.
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
        yield service, ctx


@pytest.mark.asyncio
async def test_create_accepts_and_read_returns_description(wired):
    service, _ctx = wired
    created = await service.handle({
        "action": "CreateSpecialtyAgentDraft",
        "name": "Transcript Cleaner",
        "persona": "You clean transcripts.",
        "description": "Cleans up ASR transcripts",
    })
    got = await service.handle({
        "action": "ReadSpecialtyAgentDraft",
        "assetId": created["assetId"],
    })
    assert got["description"] == "Cleans up ASR transcripts"


@pytest.mark.asyncio
async def test_update_changes_the_description(wired):
    service, _ctx = wired
    created = await service.handle({
        "action": "CreateSpecialtyAgentDraft",
        "name": "n", "persona": "p", "description": "old",
    })
    await service.handle({
        "action": "UpdateSpecialtyAgentDraft",
        "assetId": created["assetId"],
        "description": "new",
    })
    got = await service.handle({
        "action": "ReadSpecialtyAgentDraft",
        "assetId": created["assetId"],
    })
    assert got["description"] == "new"


@pytest.mark.asyncio
async def test_update_without_description_preserves_existing(wired):
    service, _ctx = wired
    created = await service.handle({
        "action": "CreateSpecialtyAgentDraft",
        "name": "n", "persona": "p", "description": "original",
    })
    await service.handle({
        "action": "UpdateSpecialtyAgentDraft",
        "assetId": created["assetId"],
        "persona": "updated persona",  # update something else
    })
    got = await service.handle({
        "action": "ReadSpecialtyAgentDraft",
        "assetId": created["assetId"],
    })
    assert got["description"] == "original"


@pytest.mark.asyncio
async def test_update_with_none_clears_description(wired):
    service, _ctx = wired
    created = await service.handle({
        "action": "CreateSpecialtyAgentDraft",
        "name": "n", "persona": "p", "description": "to be cleared",
    })
    await service.handle({
        "action": "UpdateSpecialtyAgentDraft",
        "assetId": created["assetId"],
        "description": None,
    })
    got = await service.handle({
        "action": "ReadSpecialtyAgentDraft",
        "assetId": created["assetId"],
    })
    assert got["description"] == ""


@pytest.mark.asyncio
async def test_list_drafts_includes_description(wired):
    service, _ctx = wired
    await service.handle({
        "action": "CreateSpecialtyAgentDraft",
        "name": "n", "persona": "p", "description": "listed",
    })
    rows = await service.handle({"action": "ListSpecialtyAgentDrafts"})
    assert [r["description"] for r in rows] == ["listed"]


@pytest.mark.asyncio
async def test_list_my_published_assets_includes_description(wired):
    service, _ctx = wired
    created = await service.handle({
        "action": "CreateSpecialtyAgentDraft",
        "name": "n", "persona": "p", "description": "published desc",
    })
    await service.handle({
        "action": "PublishAsset",
        "assetId": created["assetId"],
        "category": "specialty-agent",
        "scope": "private",
    })
    rows = await service.handle({
        "action": "ListMyPublishedAssets",
        "category": "specialty-agent",
    })
    assert rows[0]["description"] == "published desc"
