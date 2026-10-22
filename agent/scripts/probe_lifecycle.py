"""Probe lifecycle round-trip end-to-end against moto.

Mirrors the test fixture but exposes intermediate state for debugging.
"""
import asyncio
import os
import sys
import uuid

import boto3
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.asset_index import AssetIndex
from lab.context import LabRequestContext
from lab.gateway_browser import GatewayBrowser
from lab.s3_user_files_tool import S3UserFilesTool
from lab.service import LabService


class _FakeGW:
    def list_tools(self, actor_id, timeout):
        return []


TABLE = "test-probe"
BUCKET = "test-probe"
ACTOR = "actor-probe"


def _create_table(ddb):
    ddb.create_table(
        TableName=TABLE,
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


async def main():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_table(ddb)
        table = ddb.Table(TABLE)

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        ctx = LabRequestContext(
            actor_id=ACTOR, session_id="s", request_id=str(uuid.uuid4())
        )
        s3_tool = S3UserFilesTool(bucket=BUCKET, actor_id=ACTOR)
        index = AssetIndex(ddb_table=table, actor_id=ACTOR)
        gw = GatewayBrowser(gateway_client=_FakeGW(), configured_endpoints=[])
        svc = LabService(ctx=ctx, s3=s3_tool, index=index, gateway_browser=gw)

        # Create.
        r = await svc.handle({"action": "CreateSkillDraft", "name": "Probe", "skillMd": "# x"})
        asset_id = r["assetId"]
        print(f"Created working copy {asset_id}")

        # Inspect DDB.
        items = table.scan()["Items"]
        print(f"After create: {len(items)} rows")
        for it in items:
            print(f"  PK={it['PK']} SK={it['SK']}  GSI1PK={it.get('GSI1PK')} GSI1SK={it.get('GSI1SK')}")

        # Publish.
        r2 = await svc.handle({"action": "PublishAsset", "category": "skill", "assetId": asset_id})
        print(f"\nPublished: {r2}")

        # Inspect DDB again.
        items = table.scan()["Items"]
        print(f"\nAfter publish: {len(items)} rows")
        for it in items:
            print(f"  PK={it['PK']} SK={it['SK']}  GSI1PK={it.get('GSI1PK')} GSI1SK={it.get('GSI1SK')}")

        # query_lineage.
        lineage = await index.query_lineage(asset_id)
        print(f"\nquery_lineage({asset_id}) → {len(lineage)} rows")

        # ListAssetVersions through service.
        r3 = await svc.handle({"action": "ListAssetVersions", "assetId": asset_id})
        print(f"ListAssetVersions: {r3}")


if __name__ == "__main__":
    asyncio.run(main())
