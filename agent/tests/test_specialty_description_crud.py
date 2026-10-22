"""``description`` is accepted on create, updatable, and returned on read.

``update`` uses the ``_UNSET`` sentinel rather than ``None``-means-unchanged,
because "" is a meaningful stored value (it clears the description) — the same
reasoning the ``model`` field already documents.
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
async def test_create_persists_the_description(wired):
    service, ctx = wired
    result = await service.specialty_crud.create(
        ctx=ctx,
        name="Transcript Cleaner",
        persona="You clean transcripts.",
        description="Cleans up ASR transcripts",
    )
    read = await service.specialty_crud.read(ctx=ctx, asset_id=result.asset_id)
    assert read.description == "Cleans up ASR transcripts"


@pytest.mark.asyncio
async def test_create_defaults_the_description_to_empty(wired):
    service, ctx = wired
    result = await service.specialty_crud.create(
        ctx=ctx, name="n", persona="p"
    )
    read = await service.specialty_crud.read(ctx=ctx, asset_id=result.asset_id)
    assert read.description == ""


@pytest.mark.asyncio
async def test_update_sets_the_description(wired):
    service, ctx = wired
    created = await service.specialty_crud.create(
        ctx=ctx, name="n", persona="p", description="old"
    )
    await service.specialty_crud.update(
        ctx=ctx, asset_id=created.asset_id, description="new"
    )
    read = await service.specialty_crud.read(ctx=ctx, asset_id=created.asset_id)
    assert read.description == "new"


@pytest.mark.asyncio
async def test_omitted_description_leaves_it_unchanged(wired):
    service, ctx = wired
    created = await service.specialty_crud.create(
        ctx=ctx, name="n", persona="p", description="keep me"
    )
    await service.specialty_crud.update(
        ctx=ctx, asset_id=created.asset_id, persona="edited"
    )
    read = await service.specialty_crud.read(ctx=ctx, asset_id=created.asset_id)
    assert read.description == "keep me"


@pytest.mark.asyncio
async def test_empty_string_clears_the_description(wired):
    service, ctx = wired
    created = await service.specialty_crud.create(
        ctx=ctx, name="n", persona="p", description="remove me"
    )
    await service.specialty_crud.update(
        ctx=ctx, asset_id=created.asset_id, description=""
    )
    read = await service.specialty_crud.read(ctx=ctx, asset_id=created.asset_id)
    assert read.description == ""
