#!/usr/bin/env python3
"""Migrate every actor-keyed row/object from ``local-dev-user`` to a real Cognito sub.

Background
----------
Before user-auth landed (R6), the platform ran in single-user dev mode and
every actor-keyed read/write keyed on the literal string ``local-dev-user``.
Now that the runtime authorizer is JWT-backed and ``context.actor_id`` is
the user pool ``sub`` claim, the legacy rows become invisible to the user
who used to "own" them.

This script is the one-shot remediation referenced in
``.kiro/specs/user-auth/tasks.md`` task 9.1 and Requirement R6. It walks
the three actor-keyed resources used by the lab and rewrites the legacy
identity in place:

1. **SessionsTable** — attribute-based:
   ``actorId`` is a plain string attribute, the partition key is
   ``sessionId``. Migration is a single ``UpdateItem`` with a
   ``ConditionExpression`` to refuse rows whose ``actorId`` has already
   been moved off ``local-dev-user`` (avoids clobbering concurrent
   writes).

2. **LabAssetIndexTable** — partition-key-encoded:
   PK shapes are ``USER#<u>#OWNED``, ``USER#<u>#VERSIONS``,
   ``USER#<u>#DRAFTS``, ``USER#<u>#PUBLISHED``,
   ``USER#<u>#TOOL_COLLECTIONS`` (see ``agent/src/lab/asset_index.py``).
   Because DDB items are immutable on PK, the migration is a
   transactional copy-then-delete: we ``Put`` the rewritten row with
   ``attribute_not_exists(PK)`` and ``Delete`` the original under one
   ``TransactWriteItems`` call so a partial failure leaves the table
   untouched. Embedded actor strings inside ``s3Prefix`` and
   ``scopeHistory[].actorId`` are also rewritten so the row stays
   consistent with the renamed S3 prefix.

3. **LabArtifactsBucket** — prefix-encoded:
   Objects under ``users/local-dev-user/...`` (drafts, published,
   lab-runs) are copied to ``users/<target>/...`` and the originals
   deleted. ``--dry-run`` short-circuits both DDB and S3 mutations.

Usage examples
--------------

Dry run (default — see what would change without writing anything)::

    python scripts/migrate_local_dev_user.py \\
        --target-actor-id 12345678-1234-4123-9123-123456789abc \\
        --dry-run

Real migration with explicit table/bucket overrides::

    SESSIONS_TABLE=mediaworks-harness-sessions \\
    LAB_ASSET_INDEX_TABLE=mediaworks-harness-lab-asset-index \\
    LAB_ARTIFACTS_BUCKET=mediaworks-harness-lab-artifacts-100000000004-us-west-2 \\
    python scripts/migrate_local_dev_user.py \\
        --target-actor-id 12345678-1234-4123-9123-123456789abc

Override a single resource via CLI flag (CLI wins over env vars)::

    python scripts/migrate_local_dev_user.py \\
        --target-actor-id 12345678-1234-4123-9123-123456789abc \\
        --sessions-table mediaworks-harness-sessions \\
        --asset-index-table mediaworks-harness-lab-asset-index \\
        --artifacts-bucket mediaworks-harness-lab-artifacts-100000000004-us-west-2

Output
------
A JSON document is emitted on stdout when the script finishes, with one
key per actor-keyed resource and a ``totals`` block::

    {
      "legacyActorId": "local-dev-user",
      "targetActorId": "<sub>",
      "dryRun": false,
      "resources": {
        "sessionsTable": {"scanned": ..., "migrated": ..., "skipped": ..., "errors": ...},
        "labAssetIndexTable": {...},
        "labArtifactsBucket": {...}
      },
      "totals": {"scanned": ..., "migrated": ..., "skipped": ..., "errors": ...}
    }

Counters
--------
``scanned`` rows/objects examined in the resource (matching the legacy
actor). ``migrated`` rows/objects successfully rewritten (always 0 in
``--dry-run``). ``skipped`` rows that weren't migrated because a
concurrent write changed the row underneath us — these are NOT errors,
they're normal optimistic-concurrency outcomes. ``errors`` everything
else: missing tables, AccessDenied, throttling that exhausted retries.

Idempotency
-----------
Re-running the script after a successful migration is safe: nothing
matches the legacy actor anymore, so ``scanned`` should be 0 across the
board. Re-running a partially-completed migration is also safe — DDB
conditional writes refuse to clobber already-migrated rows and S3 copies
are idempotent on the destination key.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("migrate_local_dev_user")


LEGACY_ACTOR_ID = "local-dev-user"

# DDB sample size shown in dry-run output. Keep small so a giant table
# doesn't drown the operator in keys; the full count is always printed.
DRY_RUN_SAMPLE_SIZE = 5

# S3 prefix that embeds the actor id. Lab artifacts use this for drafts,
# published rows, and lab-runs (see ``agent/src/lab/path_validator.py``
# and ``agent/src/lab/test_harness.py``).
S3_USER_PREFIX_TEMPLATE = "users/{actor}/"

# DDB partition-key prefixes that embed the actor id in the
# Lab_Asset_Index table. See ``agent/src/lab/asset_index.py`` for the
# canonical encoding.
DDB_PK_PREFIX = "USER#{actor}#"


def _make_counters() -> dict[str, int]:
    return {"scanned": 0, "migrated": 0, "skipped": 0, "errors": 0}


# ---------------------------------------------------------------------------
# SessionsTable — attribute-based actorId
# ---------------------------------------------------------------------------


def migrate_sessions_table(
    *,
    table_name: str,
    target_actor_id: str,
    dry_run: bool,
    region: str | None,
) -> dict[str, Any]:
    """Rewrite ``actorId`` attribute on rows where it equals ``local-dev-user``.

    The table's primary key is ``sessionId`` (string). ``actorId`` is a
    plain attribute (also indexed via the ``active-lastActivity-index``
    GSI). We can rewrite the row in place with a single ``UpdateItem``
    guarded by a ``ConditionExpression`` that asserts the row still
    matches the legacy actor — that way concurrent writes that flipped
    the actor id between scan and update are counted as ``skipped``,
    not ``errors``.
    """
    counters = _make_counters()
    sample_keys: list[str] = []

    if not table_name:
        logger.warning(
            "sessions table not configured (set SESSIONS_TABLE env var or "
            "--sessions-table flag); skipping"
        )
        return {**counters, "tableName": None, "sampleKeys": []}

    ddb = boto3.resource("dynamodb", region_name=region)
    table = ddb.Table(table_name)

    # Scan with a server-side filter so we only pay for rows that match.
    # Pagination is handled by ``LastEvaluatedKey``.
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": "actorId = :legacy",
        "ExpressionAttributeValues": {":legacy": LEGACY_ACTOR_ID},
        # Only project what we need for the conditional update.
        "ProjectionExpression": "sessionId, actorId",
    }

    while True:
        try:
            resp = table.scan(**scan_kwargs)
        except ClientError as exc:
            logger.error(
                "scan failed on %s: %s", table_name, exc.response["Error"]
            )
            counters["errors"] += 1
            break

        for item in resp.get("Items", []):
            counters["scanned"] += 1
            session_id = item["sessionId"]
            if len(sample_keys) < DRY_RUN_SAMPLE_SIZE:
                sample_keys.append(session_id)

            if dry_run:
                continue

            try:
                table.update_item(
                    Key={"sessionId": session_id},
                    UpdateExpression="SET actorId = :new",
                    ConditionExpression="actorId = :legacy",
                    ExpressionAttributeValues={
                        ":new": target_actor_id,
                        ":legacy": LEGACY_ACTOR_ID,
                    },
                )
                counters["migrated"] += 1
            except ClientError as exc:
                code = exc.response["Error"].get("Code", "")
                if code == "ConditionalCheckFailedException":
                    # Concurrent writer beat us — not an error.
                    counters["skipped"] += 1
                else:
                    logger.error(
                        "update_item failed for sessionId=%s: %s",
                        session_id,
                        exc.response["Error"],
                    )
                    counters["errors"] += 1

        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        scan_kwargs["ExclusiveStartKey"] = last

    return {**counters, "tableName": table_name, "sampleKeys": sample_keys}


# ---------------------------------------------------------------------------
# LabAssetIndexTable — PK-encoded actor id
# ---------------------------------------------------------------------------


def _rewrite_asset_index_item(
    item: dict[str, Any],
    *,
    legacy_actor: str,
    target_actor: str,
) -> dict[str, Any]:
    """Return a new item with the actor id rewritten in PK and any
    embedded fields (``s3Prefix``, ``scopeHistory[].actorId``,
    ``publishPolicy.ownerIds``).

    GSI keys are recomputed implicitly because they're derived from the
    published id, not the actor — see ``agent/src/lab/asset_index.py``.
    """
    new_item = dict(item)

    legacy_pk_prefix = DDB_PK_PREFIX.format(actor=legacy_actor)
    target_pk_prefix = DDB_PK_PREFIX.format(actor=target_actor)
    pk = item.get("PK", "")
    if isinstance(pk, str) and pk.startswith(legacy_pk_prefix):
        new_item["PK"] = target_pk_prefix + pk[len(legacy_pk_prefix):]

    legacy_s3_prefix = S3_USER_PREFIX_TEMPLATE.format(actor=legacy_actor)
    target_s3_prefix = S3_USER_PREFIX_TEMPLATE.format(actor=target_actor)
    s3_prefix = item.get("s3Prefix")
    if isinstance(s3_prefix, str) and s3_prefix.startswith(legacy_s3_prefix):
        new_item["s3Prefix"] = (
            target_s3_prefix + s3_prefix[len(legacy_s3_prefix):]
        )

    # ``scopeHistory`` entries record the actor that performed each
    # scope change. Rewrite the legacy actor in those audit entries so
    # the row's history reflects the new identity. Other audit fields
    # (timestamp, from/to scope) are untouched.
    history = item.get("scopeHistory")
    if isinstance(history, list):
        new_history = []
        for entry in history:
            if (
                isinstance(entry, dict)
                and entry.get("actorId") == legacy_actor
            ):
                new_history.append({**entry, "actorId": target_actor})
            else:
                new_history.append(entry)
        new_item["scopeHistory"] = new_history

    # ``publishPolicy.ownerIds`` and ``contributorIds`` are user-id
    # lists; rewrite any legacy entries.
    policy = item.get("publishPolicy")
    if isinstance(policy, dict):
        new_policy = dict(policy)
        for field_name in ("ownerIds", "contributorIds"):
            ids = policy.get(field_name)
            if isinstance(ids, list):
                new_policy[field_name] = [
                    target_actor if v == legacy_actor else v for v in ids
                ]
        new_item["publishPolicy"] = new_policy

    return new_item


def migrate_asset_index_table(
    *,
    table_name: str,
    target_actor_id: str,
    dry_run: bool,
    region: str | None,
) -> dict[str, Any]:
    """Copy-then-delete every row whose PK starts with ``USER#local-dev-user#``.

    DDB items are immutable on the partition key, so we cannot
    ``UpdateItem`` here. Instead we use ``TransactWriteItems`` with a
    ``Put`` (conditional on ``attribute_not_exists(PK)`` so we never
    clobber an already-migrated row) and a ``Delete`` of the original
    under one transaction — partial failure leaves the table untouched.
    """
    counters = _make_counters()
    sample_keys: list[str] = []

    if not table_name:
        logger.warning(
            "asset-index table not configured (set LAB_ASSET_INDEX_TABLE "
            "env var or --asset-index-table flag); skipping"
        )
        return {**counters, "tableName": None, "sampleKeys": []}

    legacy_pk_prefix = DDB_PK_PREFIX.format(actor=LEGACY_ACTOR_ID)

    ddb_resource = boto3.resource("dynamodb", region_name=region)
    ddb_client = boto3.client("dynamodb", region_name=region)
    table = ddb_resource.Table(table_name)

    # Scan with a server-side ``begins_with(PK, ...)`` filter so we
    # don't iterate the whole table. PK is the partition key, so this
    # is still a Scan (no PK-prefix Query exists for the base table)
    # but the filter keeps response payloads small.
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": "begins_with(PK, :prefix)",
        "ExpressionAttributeValues": {":prefix": legacy_pk_prefix},
    }

    while True:
        try:
            resp = table.scan(**scan_kwargs)
        except ClientError as exc:
            logger.error(
                "scan failed on %s: %s", table_name, exc.response["Error"]
            )
            counters["errors"] += 1
            break

        for item in resp.get("Items", []):
            counters["scanned"] += 1
            old_pk = item["PK"]
            old_sk = item["SK"]
            if len(sample_keys) < DRY_RUN_SAMPLE_SIZE:
                sample_keys.append(f"{old_pk} | {old_sk}")

            if dry_run:
                continue

            new_item = _rewrite_asset_index_item(
                item,
                legacy_actor=LEGACY_ACTOR_ID,
                target_actor=target_actor_id,
            )

            # ``TransactWriteItems`` wants the low-level client format,
            # so serialize via boto3's public DynamoDB type serializer.
            # (We used to reach into ``meta.client._serializer`` here, but
            # newer botocore wraps that attribute in a
            # ``ParamValidationDecorator`` without a ``serialize`` method.
            # ``boto3.dynamodb.types.TypeSerializer`` is the supported
            # public API and behaves identically to the old private one.)
            serializer = TypeSerializer()
            try:
                ddb_item = {
                    k: serializer.serialize(v) for k, v in new_item.items()
                }
            except Exception as exc:  # pragma: no cover — defensive
                logger.error(
                    "failed to serialize rewritten item PK=%s SK=%s: %s",
                    old_pk,
                    old_sk,
                    exc,
                )
                counters["errors"] += 1
                continue

            try:
                ddb_client.transact_write_items(
                    TransactItems=[
                        {
                            "Put": {
                                "TableName": table_name,
                                "Item": ddb_item,
                                "ConditionExpression": (
                                    "attribute_not_exists(PK)"
                                ),
                            }
                        },
                        {
                            "Delete": {
                                "TableName": table_name,
                                "Key": {
                                    "PK": {"S": old_pk},
                                    "SK": {"S": old_sk},
                                },
                                "ConditionExpression": (
                                    "attribute_exists(PK)"
                                ),
                            }
                        },
                    ]
                )
                counters["migrated"] += 1
            except ClientError as exc:
                code = exc.response["Error"].get("Code", "")
                if code in (
                    "TransactionCanceledException",
                    "ConditionalCheckFailedException",
                ):
                    # Either the destination already exists (idempotent
                    # re-run) or the source was deleted under us. Either
                    # way, not an error — count as skipped.
                    counters["skipped"] += 1
                else:
                    logger.error(
                        "transact_write_items failed for PK=%s SK=%s: %s",
                        old_pk,
                        old_sk,
                        exc.response["Error"],
                    )
                    counters["errors"] += 1

        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        scan_kwargs["ExclusiveStartKey"] = last

    return {**counters, "tableName": table_name, "sampleKeys": sample_keys}


# ---------------------------------------------------------------------------
# LabArtifactsBucket — actor-prefixed S3 keys
# ---------------------------------------------------------------------------


def migrate_artifacts_bucket(
    *,
    bucket: str,
    target_actor_id: str,
    dry_run: bool,
    region: str | None,
) -> dict[str, Any]:
    """Copy ``users/local-dev-user/...`` → ``users/<target>/...`` and delete the source.

    S3 objects are immutable on key, so this is a copy-then-delete. We
    use ``CopyObject`` (server-side, no payload through the script) and
    delete only after a successful copy.
    """
    counters = _make_counters()
    sample_keys: list[str] = []

    if not bucket:
        logger.warning(
            "artifacts bucket not configured (set LAB_ARTIFACTS_BUCKET env "
            "var or --artifacts-bucket flag); skipping"
        )
        return {**counters, "bucket": None, "sampleKeys": []}

    legacy_prefix = S3_USER_PREFIX_TEMPLATE.format(actor=LEGACY_ACTOR_ID)
    target_prefix = S3_USER_PREFIX_TEMPLATE.format(actor=target_actor_id)

    s3 = boto3.client("s3", region_name=region)
    paginator = s3.get_paginator("list_objects_v2")

    try:
        page_iter = paginator.paginate(Bucket=bucket, Prefix=legacy_prefix)
    except ClientError as exc:
        logger.error(
            "list_objects_v2 paginator setup failed for %s: %s",
            bucket,
            exc.response["Error"],
        )
        return {
            **counters,
            "errors": counters["errors"] + 1,
            "bucket": bucket,
            "sampleKeys": [],
        }

    try:
        for page in page_iter:
            for obj in page.get("Contents", []) or []:
                counters["scanned"] += 1
                src_key = obj["Key"]
                if not src_key.startswith(legacy_prefix):
                    # Defensive — paginator filter should already exclude
                    # these, but skip if S3 returns something unexpected.
                    counters["skipped"] += 1
                    continue
                dst_key = target_prefix + src_key[len(legacy_prefix):]
                if len(sample_keys) < DRY_RUN_SAMPLE_SIZE:
                    sample_keys.append(f"{src_key} -> {dst_key}")

                if dry_run:
                    continue

                try:
                    s3.copy_object(
                        Bucket=bucket,
                        Key=dst_key,
                        CopySource={"Bucket": bucket, "Key": src_key},
                        MetadataDirective="COPY",
                    )
                except ClientError as exc:
                    logger.error(
                        "copy_object failed for %s -> %s: %s",
                        src_key,
                        dst_key,
                        exc.response["Error"],
                    )
                    counters["errors"] += 1
                    continue

                try:
                    s3.delete_object(Bucket=bucket, Key=src_key)
                except ClientError as exc:
                    logger.error(
                        "delete_object failed for %s (copy already "
                        "succeeded to %s): %s",
                        src_key,
                        dst_key,
                        exc.response["Error"],
                    )
                    # Copy succeeded, delete didn't — count as error so
                    # the operator notices the orphaned source object.
                    counters["errors"] += 1
                    continue

                counters["migrated"] += 1
    except ClientError as exc:
        logger.error(
            "pagination failed for %s: %s", bucket, exc.response["Error"]
        )
        counters["errors"] += 1

    return {**counters, "bucket": bucket, "sampleKeys": sample_keys}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite every actor-keyed row/object from "
            f"'{LEGACY_ACTOR_ID}' to a real Cognito sub. See module "
            "docstring for full usage examples."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target-actor-id",
        required=True,
        help=(
            "The Cognito 'sub' claim (or any non-legacy actor id) to "
            "rewrite legacy rows to. Required."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Scan only. Print counts and a sample of keys but do not "
            "mutate any DDB row or S3 object."
        ),
    )
    parser.add_argument(
        "--sessions-table",
        default=os.environ.get("SESSIONS_TABLE"),
        help=(
            "DynamoDB table holding session metadata (PK=sessionId, "
            "actorId attr). Defaults to $SESSIONS_TABLE."
        ),
    )
    parser.add_argument(
        "--asset-index-table",
        default=os.environ.get("LAB_ASSET_INDEX_TABLE"),
        help=(
            "DynamoDB single-table for the Lab Asset_Index "
            "(PK=USER#<u>#..., SK=<category>#...). Defaults to "
            "$LAB_ASSET_INDEX_TABLE."
        ),
    )
    parser.add_argument(
        "--artifacts-bucket",
        default=os.environ.get("LAB_ARTIFACTS_BUCKET"),
        help=(
            "S3 bucket holding 'users/<actor>/...' artifact prefixes. "
            "Defaults to $LAB_ARTIFACTS_BUCKET."
        ),
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        help="AWS region for boto3 clients (defaults to $AWS_REGION).",
    )
    return parser


def _validate_target(target: str) -> None:
    """Refuse pathological target ids that would make the migration a no-op
    or actively dangerous (e.g. rewriting legacy rows back onto themselves)."""
    if not target or not target.strip():
        raise SystemExit("--target-actor-id must be non-empty")
    if target == LEGACY_ACTOR_ID:
        raise SystemExit(
            f"--target-actor-id cannot equal the legacy actor id "
            f"'{LEGACY_ACTOR_ID}' (would be a no-op)"
        )
    # Rough Cognito sub check is intentionally not enforced — the legacy
    # tests use shorter ids, and admins might want to migrate to any
    # non-legacy string. Just reject obvious typos.
    if "/" in target or " " in target:
        raise SystemExit(
            f"--target-actor-id must not contain '/' or whitespace; got {target!r}"
        )


def _sum_counters(blocks: list[dict[str, Any]]) -> dict[str, int]:
    totals = _make_counters()
    for block in blocks:
        for key in totals:
            totals[key] += int(block.get(key, 0))
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    _validate_target(args.target_actor_id)

    sessions = migrate_sessions_table(
        table_name=args.sessions_table,
        target_actor_id=args.target_actor_id,
        dry_run=args.dry_run,
        region=args.region,
    )
    asset_index = migrate_asset_index_table(
        table_name=args.asset_index_table,
        target_actor_id=args.target_actor_id,
        dry_run=args.dry_run,
        region=args.region,
    )
    artifacts = migrate_artifacts_bucket(
        bucket=args.artifacts_bucket,
        target_actor_id=args.target_actor_id,
        dry_run=args.dry_run,
        region=args.region,
    )

    summary = {
        "legacyActorId": LEGACY_ACTOR_ID,
        "targetActorId": args.target_actor_id,
        "dryRun": args.dry_run,
        "resources": {
            "sessionsTable": sessions,
            "labAssetIndexTable": asset_index,
            "labArtifactsBucket": artifacts,
        },
        "totals": _sum_counters([sessions, asset_index, artifacts]),
    }
    json.dump(summary, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if summary["totals"]["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
