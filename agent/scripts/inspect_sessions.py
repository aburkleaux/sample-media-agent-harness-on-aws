"""Inspect the mediaworks-harness-sessions DDB table.

Verifies:
  1. Table exists and has the expected GSI for the sidebar query.
  2. Recent rows actually got written (after a session was used).
  3. Schema matches what /sessions GET returns to the UI.
"""
import boto3
import json

REGION = "us-west-2"
TABLE = "mediaworks-harness-sessions"


def main():
    ddb = boto3.client("dynamodb", region_name=REGION)
    res_table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)

    # 1. Describe.
    desc = ddb.describe_table(TableName=TABLE)["Table"]
    print(f"Table: {desc['TableName']}  status: {desc['TableStatus']}  items: {desc.get('ItemCount', 'n/a')}")
    pk = next((k["AttributeName"] for k in desc["KeySchema"] if k["KeyType"] == "HASH"), None)
    sk = next((k["AttributeName"] for k in desc["KeySchema"] if k["KeyType"] == "RANGE"), None)
    print(f"  PK: {pk}  SK: {sk}")
    for gsi in desc.get("GlobalSecondaryIndexes", []) or []:
        keys = "/".join(f"{k['KeyType']}={k['AttributeName']}" for k in gsi["KeySchema"])
        print(f"  GSI: {gsi['IndexName']}  {keys}")

    # 2. Scan a few rows.
    scan = res_table.scan(Limit=5)
    print(f"\nFirst {len(scan.get('Items', []))} row(s):")
    for it in scan.get("Items", []):
        # Only print known fields; redact long blobs.
        slim = {
            k: (v if not isinstance(v, list) else f"<list len={len(v)}>")
            for k, v in it.items()
        }
        print("  " + json.dumps(slim, default=str))

    # 3. Mimic the proxy's sidebar query.
    print("\nGSI query (sidebar):")
    try:
        result = res_table.query(
            IndexName="active-lastActivity-index",
            KeyConditionExpression="actorId = :actor",
            FilterExpression="active = :active",
            ExpressionAttributeValues={":actor": "ui-user", ":active": 1},
            ScanIndexForward=False,
        )
        print(f"  ui-user active sessions: {len(result.get('Items', []))}")
        for it in result.get("Items", [])[:3]:
            print(f"    {it.get('sessionId')}  title={it.get('title')!r}  lastActivity={it.get('lastActivityAt')}")
    except Exception as e:
        print(f"  GSI query failed: {e}")


if __name__ == "__main__":
    main()
