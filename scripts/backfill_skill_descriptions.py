#!/usr/bin/env python3
"""Backfill `description` on skill rows written before the projection existed.

`SkillCRUD.create` now copies the SKILL.md frontmatter description onto the
AssetIndex row, because the inventory lists rows while the description lives in a
file in S3. Rows written before that carry no description, and for a PUBLISHED
version there is no other way to acquire one: publishing consumes the working copy,
so re-saving the draft is not available. Hence this.

Reads SKILL.md from each row's own `s3Prefix` and writes what it finds, using the
SAME extractor the write path uses (`lab.skill_package_validator`), so a backfilled
row and a freshly-saved one cannot disagree.

DRY RUN by default — prints the exact change per row and writes nothing. Pass
`--apply` to perform the updates. Idempotent: a row that already has a description is
skipped, so re-running after a partial failure is safe.

    python scripts/backfill_skill_descriptions.py                    # plan only
    python scripts/backfill_skill_descriptions.py --apply            # do it
    python scripts/backfill_skill_descriptions.py --table X --bucket Y

Non-destructive by construction: the only mutation is `SET description`, guarded by
`attribute_exists(PK)` and `attribute_not_exists(description)` so a concurrent save
wins over the backfill rather than being overwritten by it.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent", "src"))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from lab.skill_package_validator import extract_skill_description  # noqa: E402

DEFAULT_TABLE = "mediaworks-harness-lab-asset-index"
DEFAULT_REGION = "us-west-2"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--bucket", default=None, help="Lab artifacts bucket. Derived from the account when omitted.")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--apply", action="store_true", help="Perform the writes.")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    bucket = args.bucket or f"mediaworks-harness-lab-artifacts-{account}-{args.region}"
    ddb = session.client("dynamodb")
    s3 = session.client("s3")

    print(f"account={account} region={args.region}")
    print(f"table={args.table}")
    print(f"bucket={bucket}")
    print(f"mode={'APPLY' if args.apply else 'DRY RUN'}\n")

    scanned = updated = skipped = missing = 0
    paginator = ddb.get_paginator("scan")
    for page in paginator.paginate(TableName=args.table):
        for item in page.get("Items", []):
            if item.get("category", {}).get("S") != "skill":
                continue
            scanned += 1
            name = item.get("name", {}).get("S", "?")
            version = item.get("version", {}).get("N")
            label = f"{name}{f' v{version}' if version else ' (draft)'}"

            if item.get("description", {}).get("S", "").strip():
                print(f"  skip     {label} — already has one")
                skipped += 1
                continue

            prefix = item.get("s3Prefix", {}).get("S", "")
            if not prefix:
                print(f"  MISSING  {label} — row has no s3Prefix")
                missing += 1
                continue

            key = f"{prefix}SKILL.md"
            try:
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
            except ClientError as err:
                print(f"  MISSING  {label} — s3://{bucket}/{key}: {err.response['Error']['Code']}")
                missing += 1
                continue

            description = extract_skill_description(body)
            if not description:
                print(f"  MISSING  {label} — SKILL.md declares no description")
                missing += 1
                continue

            preview = description if len(description) <= 90 else description[:87] + "..."
            print(f"  {'update  ' if args.apply else 'would   '} {label} -> {preview}")

            if args.apply:
                try:
                    ddb.update_item(
                        TableName=args.table,
                        Key={"PK": item["PK"], "SK": item["SK"]},
                        UpdateExpression="SET description = :d",
                        ExpressionAttributeValues={":d": {"S": description}},
                        # A save that landed between the scan and here wins: its
                        # description came from the current body, this one from the
                        # body as it was when the scan started.
                        ConditionExpression="attribute_exists(PK) AND attribute_not_exists(description)",
                    )
                except ClientError as err:
                    if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                        print(f"           {label} — written concurrently, left alone")
                        skipped += 1
                        continue
                    raise
            updated += 1

    verb = "updated" if args.apply else "would update"
    print(f"\nskill rows scanned={scanned} {verb}={updated} skipped={skipped} unresolved={missing}")
    if not args.apply and updated:
        print("Re-run with --apply to perform these writes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
