"""Migrate the AssetIndex DDB table from v1 to v2 partitions.

v1 → v2 partition mapping
-------------------------

  USER#<u>#DRAFTS              → USER#<u>#OWNED        (drop ``#draft`` SK suffix)
  USER#<u>#PUBLISHED           → USER#<u>#VERSIONS     (SK unchanged)
  GROUP#<g>#PUBLISHED          → GROUP#<g>#SHARED      (SK unchanged)
  ORG#<o>#PUBLISHED            → ORG#<o>#SHARED        (SK unchanged)
  USER#<u>#TOOL_COLLECTIONS    → USER#<u>#OWNED        (SK unchanged: tool-collection#<id>)
  MIGRATION#MARKER             → leave as-is

Status normalization
--------------------

For each migrated row:

* ``status == "deprecated"``  → set ``deprecated = True`` (status retained for back-compat)
* ``status`` field always preserved on the new row so v1 readers keep working;
  the v2 path computes lifecycle from ``(version, deprecated)`` directly.

S3 prefix renames (best-effort)
-------------------------------

  users/<u>/drafts/<...>     → users/<u>/owned/<...>
  users/<u>/published/<...>  → users/<u>/versions/<...>

Other prefixes (groups/, org/) are left in place — they were already correct
for v2 reads.

Idempotency
-----------

Inserts a marker row at PK=``MIGRATION#MARKER``, SK=``v2-lifecycle-complete``
when finished. Re-running with the marker present prints status and exits.

Usage
-----

  ./.venv/bin/python scripts/migrate_v2_lifecycle.py --dry-run
  ./.venv/bin/python scripts/migrate_v2_lifecycle.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-west-2")
TABLE_NAME = os.environ.get("ASSET_INDEX_TABLE", "mediaworks-harness-lab-asset-index")
ARTIFACTS_BUCKET = os.environ.get(
    "ARTIFACTS_BUCKET", "mediaworks-harness-lab-artifacts-100000000004-us-west-2"
)

MARKER_PK = "MIGRATION#MARKER"
MARKER_SK = "v2-lifecycle-complete"


def remap_pk(pk: str) -> str | None:
    """Return the v2 PK or None if the row is not a v1 asset partition.

    Marker rows and unknown shapes return None (caller leaves them alone).
    """
    if pk == MARKER_PK:
        return None
    if pk.startswith("USER#") and pk.endswith("#DRAFTS"):
        return pk.replace("#DRAFTS", "#OWNED")
    if pk.startswith("USER#") and pk.endswith("#PUBLISHED"):
        return pk.replace("#PUBLISHED", "#VERSIONS")
    if pk.startswith("GROUP#") and pk.endswith("#PUBLISHED"):
        return pk.replace("#PUBLISHED", "#SHARED")
    if pk.startswith("ORG#") and pk.endswith("#PUBLISHED"):
        return pk.replace("#PUBLISHED", "#SHARED")
    if pk.startswith("USER#") and pk.endswith("#TOOL_COLLECTIONS"):
        # Collapse into OWNED.
        return pk.replace("#TOOL_COLLECTIONS", "#OWNED")
    return None


def remap_sk(sk: str) -> str:
    """Return the v2 SK. Drops the trailing ``#draft`` suffix from v1 draft SKs."""
    if sk.endswith("#draft"):
        return sk[: -len("#draft")]
    return sk


def migrate(dry_run: bool) -> int:
    ddb = boto3.resource("dynamodb", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    table = ddb.Table(TABLE_NAME)

    # Idempotency check.
    try:
        marker = table.get_item(Key={"PK": MARKER_PK, "SK": MARKER_SK}).get("Item")
        if marker and not dry_run:
            print(
                f"Marker present (completedAt={marker.get('completedAt')}). "
                "Migration already ran; nothing to do. Pass --force to re-run."
            )
            return 0
    except ClientError as exc:
        print(f"WARN: marker read failed: {exc}", file=sys.stderr)

    # Scan everything.
    print(f"Scanning {TABLE_NAME}...")
    items: list[dict] = []
    last_key = None
    while True:
        kwargs = {}
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    print(f"Scanned {len(items)} rows.")

    transitions: Counter[tuple[str, str]] = Counter()
    rows_to_migrate: list[tuple[dict, str, str]] = []  # (item, new_pk, new_sk)
    skipped: list[dict] = []

    for item in items:
        pk = item.get("PK", "")
        sk = item.get("SK", "")
        new_pk = remap_pk(pk)
        if new_pk is None:
            skipped.append(item)
            transitions[("skip", pk[:25])] += 1
            continue
        new_sk = remap_sk(sk)
        rows_to_migrate.append((item, new_pk, new_sk))
        transitions[(pk[:30], new_pk[:30])] += 1

    print(f"\n=== Transition summary ===")
    for k, count in sorted(transitions.items(), key=lambda kv: -kv[1]):
        print(f"  {k[0]:30} → {k[1]:30}  {count} rows")

    if dry_run:
        print(f"\n[dry-run] {len(rows_to_migrate)} rows would be migrated, "
              f"{len(skipped)} skipped.")
        for sample in rows_to_migrate[:5]:
            item, new_pk, new_sk = sample
            print(f"  {item['PK']} / {item['SK']}  →  {new_pk} / {new_sk}")
        return 0

    # Apply: per-row transact_write of (Put new, Delete old). One row per
    # transaction keeps blast radius small if any row fails.
    print(f"\nMigrating {len(rows_to_migrate)} rows...")
    client = boto3.client("dynamodb", region_name=REGION)
    success = 0
    failures: list[str] = []

    # Use the resource layer's serializer for the new item; for the
    # transact_write client API we need attribute-typed Marshalling, so
    # easier path: do two separate operations (put_item then delete_item)
    # via the resource which uses native types. Loss: not atomic per row.
    # Acceptable for one-shot migration.
    for item, new_pk, new_sk in rows_to_migrate:
        old_pk, old_sk = item["PK"], item["SK"]
        new_item = dict(item)
        new_item["PK"] = new_pk
        new_item["SK"] = new_sk
        # Add deprecated boolean explicitly if missing (read path fallback
        # already handles legacy rows, but writing it makes scans cheaper).
        if "deprecated" not in new_item:
            new_item["deprecated"] = (item.get("status") == "deprecated")

        try:
            table.put_item(Item=new_item)
            table.delete_item(Key={"PK": old_pk, "SK": old_sk})
            success += 1
        except ClientError as exc:
            failures.append(f"{old_pk}/{old_sk}: {exc}")
            print(f"  FAIL {old_pk}/{old_sk}: {exc}", file=sys.stderr)

    print(f"\nMigrated {success}/{len(rows_to_migrate)} rows.")
    if failures:
        print(f"Failures: {len(failures)}", file=sys.stderr)

    # Post-condition row count check.
    print("Re-scanning to verify...")
    post_items = []
    last_key = None
    while True:
        kwargs = {}
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = table.scan(**kwargs)
        post_items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    v1_remaining = sum(
        1 for it in post_items
        if (it.get("PK", "").endswith("#DRAFTS")
            or it.get("PK", "").endswith("#PUBLISHED")
            or it.get("PK", "").endswith("#TOOL_COLLECTIONS"))
    )
    print(f"  total rows now: {len(post_items)}  (was {len(items)})")
    print(f"  v1-shaped rows remaining: {v1_remaining}")
    if v1_remaining > 0:
        print("  WARNING: some v1 rows survived; investigate before marking complete.",
              file=sys.stderr)

    # S3 prefix rename — best effort. Only attempt if every row migration succeeded.
    if not failures:
        rename_s3_prefixes(s3)

    # Idempotency marker.
    if not failures and v1_remaining == 0:
        table.put_item(Item={
            "PK": MARKER_PK,
            "SK": MARKER_SK,
            "completedAt": int(time.time() * 1000),
            "rowsMigrated": success,
        })
        print("Marker written.")
        return 0
    return 1


def rename_s3_prefixes(s3) -> None:
    """Rename users/<u>/drafts/* → users/<u>/owned/* and users/<u>/published/* → users/<u>/versions/*.

    Best-effort: copies + deletes objects. Idempotent per object.
    """
    print(f"\nRenaming S3 prefixes in {ARTIFACTS_BUCKET}...")

    rename_pairs = [
        ("drafts/", "owned/"),
        ("published/", "versions/"),
    ]

    paginator = s3.get_paginator("list_objects_v2")
    user_prefixes: set[str] = set()

    # Discover all user-level prefixes.
    for page in paginator.paginate(Bucket=ARTIFACTS_BUCKET, Prefix="users/", Delimiter="/"):
        for cp in page.get("CommonPrefixes", []) or []:
            user_prefixes.add(cp["Prefix"])  # users/<u>/

    print(f"  Found {len(user_prefixes)} user prefixes to scan.")

    moved = 0
    for user_prefix in sorted(user_prefixes):
        for src, dst in rename_pairs:
            src_full = f"{user_prefix}{src}"
            dst_full = f"{user_prefix}{dst}"
            for page in paginator.paginate(Bucket=ARTIFACTS_BUCKET, Prefix=src_full):
                for obj in page.get("Contents", []) or []:
                    src_key = obj["Key"]
                    dst_key = src_key.replace(src_full, dst_full, 1)
                    try:
                        s3.copy_object(
                            Bucket=ARTIFACTS_BUCKET,
                            CopySource={"Bucket": ARTIFACTS_BUCKET, "Key": src_key},
                            Key=dst_key,
                        )
                        s3.delete_object(Bucket=ARTIFACTS_BUCKET, Key=src_key)
                        moved += 1
                    except ClientError as exc:
                        print(f"  WARN copy {src_key} → {dst_key}: {exc}", file=sys.stderr)
    print(f"  Renamed {moved} S3 objects.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print transitions without writing.")
    parser.add_argument("--force", action="store_true", help="Ignore idempotency marker.")
    args = parser.parse_args()

    if args.force:
        ddb = boto3.resource("dynamodb", region_name=REGION)
        try:
            ddb.Table(TABLE_NAME).delete_item(Key={"PK": MARKER_PK, "SK": MARKER_SK})
            print("Marker cleared (--force).")
        except ClientError:
            pass

    return migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
