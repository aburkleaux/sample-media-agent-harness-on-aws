"""One-shot: rewrite legacy ``actorId="ui-user"`` rows to the new actor identity.

The proxy used to hardcode actor_id="ui-user" for the sessions sidebar.
After unifying on MEMORY_ACTOR (defaults to "local-dev-user"), legacy
rows become invisible. This script rewrites them in place.
"""
import boto3

REGION = "us-west-2"
TABLE = "mediaworks-harness-sessions"
OLD = "ui-user"
NEW = "local-dev-user"


def main():
    res = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    scan = res.scan()
    moved = 0
    for it in scan.get("Items", []):
        if it.get("actorId") == OLD:
            res.update_item(
                Key={"sessionId": it["sessionId"]},
                UpdateExpression="SET actorId = :new",
                ExpressionAttributeValues={":new": NEW},
            )
            moved += 1
            print(f"  moved {it['sessionId']} ({it.get('title')})")
    print(f"Updated {moved} rows.")


if __name__ == "__main__":
    main()
