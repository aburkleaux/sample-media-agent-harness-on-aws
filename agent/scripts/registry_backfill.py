#!/usr/bin/env python3
"""Backfill eligible canonical records into the Registry (migration Task 17-19).

Enumerates org-scoped published versions from the AssetIndex, builds the
descriptor each one *would* get, and — with ``--execute`` — projects it through
the same code path the live projector uses. Reusing
:class:`capability.projector.Projector` rather than a parallel implementation is
the point: the dry-run report is only a meaningful preview if the backfill and
the projector cannot disagree (Task 18's review gate depends on that).

Safety (R16):

* Dry run is the default; ``--execute`` is required to write.
* ``--env`` is required and verified against the caller's account.
* ``prod`` additionally requires ``--i-understand-this-is-production``.
* Nothing here deletes a canonical row, an S3 object, or a Registry record.

Resumability: ``--checkpoint <path>`` records every converged
``(category, assetId, version)`` so a re-run skips them. A crash mid-run
therefore costs the current item, not the whole backfill.

Usage::

    # dry run, all categories, writes a proposed-records report
    uv run python scripts/registry_backfill.py --env sandbox \
        --report /tmp/proposed-records.json

    # after the report is reviewed
    uv run python scripts/registry_backfill.py --env sandbox --execute \
        --checkpoint /tmp/backfill.checkpoint
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Iterator, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from capability import naming, registry as registry_client  # noqa: E402
from capability.config import load_config  # noqa: E402
from capability.descriptors import (  # noqa: E402
    PROJECTABLE_CATEGORIES,
    PoisonDescriptorError,
    build_descriptor,
    descriptor_digest,
    is_eligible,
)
from capability.projection_state import ProjectionState  # noqa: E402
from capability.projector import Projector  # noqa: E402
from lab.outbox import EVENT_REPLAY, canonical_revision  # noqa: E402

ENVIRONMENTS = {
    "sandbox": "100000000001",
    "staging": "100000000002",
    "prod": "100000000003",
}

#: Only these partitions can hold an org-scoped published version. Scanning the
#: whole table would also sweep working copies, private versions, group shares,
#: migration markers, and outbox rows — none of which are projectable, and one
#: of which (private versions) must never be.
ORG_PARTITION_PREFIX = "ORG#"


def verify_caller(env: str, region: str) -> str:
    import boto3

    identity = boto3.client("sts", region_name=region).get_caller_identity()
    account = identity["Account"]
    expected = ENVIRONMENTS[env]
    if account != expected:
        raise SystemExit(
            f"refusing to act: --env {env} expects account {expected}, but the "
            f"current credentials are account {account} ({identity['Arn']})"
        )
    return account


def iter_org_partitions(table: Any) -> Iterator[str]:
    """Distinct ``ORG#<orgId>#SHARED`` partition keys present in the table.

    A Scan projected to keys only. Unavoidable: DynamoDB cannot enumerate
    partition keys any other way, and there is no GSI on scope. Bounded to the
    key attributes so the cost is the key space, not the item bodies.
    """
    seen: set[str] = set()
    kwargs: dict[str, Any] = {
        "ProjectionExpression": "PK",
        "FilterExpression": "begins_with(PK, :prefix)",
        "ExpressionAttributeValues": {":prefix": ORG_PARTITION_PREFIX},
    }
    while True:
        response = table.scan(**kwargs)
        for item in response.get("Items", []):
            pk = item.get("PK")
            if isinstance(pk, str) and pk not in seen:
                seen.add(pk)
                yield pk
        last = response.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def iter_eligible_rows(
    table: Any, *, categories: set[str]
) -> Iterator[dict[str, Any]]:
    """Every org-scoped, non-deprecated published version in a wanted category."""
    for partition in iter_org_partitions(table):
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": partition},
        }
        while True:
            response = table.query(**kwargs)
            for row in response.get("Items", []):
                if row.get("category") not in categories:
                    continue
                if not is_eligible(row):
                    continue
                yield row
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last


def load_checkpoint(path: Optional[str]) -> set[str]:
    if not path or not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as handle:
        return {line.strip() for line in handle if line.strip()}


def append_checkpoint(path: Optional[str], key: str) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(key + "\n")


def as_replay_event(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a canonical row as the outbox event the projector expects.

    A synthetic ``Replay`` event rather than a fake ``CapabilityPublished``:
    the event type is part of the idempotency key, so reusing the publish type
    here would let a backfill and a real publish of the same version collide on
    one key and mask each other.
    """
    return {
        "eventType": EVENT_REPLAY,
        "category": row["category"],
        "assetId": row["assetId"],
        "version": int(row["version"]),
        "scope": row.get("scope"),
        "canonicalRevision": canonical_revision(row),
        "occurredAt": _iso_from_row(row),
    }


def _iso_from_row(row: dict[str, Any]) -> str:
    from datetime import datetime, timezone

    updated = row.get("updatedAt")
    try:
        return datetime.fromtimestamp(
            int(updated) / 1000, tz=timezone.utc
        ).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, choices=sorted(ENVIRONMENTS))
    parser.add_argument(
        "--category",
        action="append",
        choices=sorted(PROJECTABLE_CATEGORIES - {"mcp-endpoint"}),
        help="restrict to one category; repeatable. Default: all projectable.",
    )
    parser.add_argument("--execute", action="store_true", help="write to the Registry")
    parser.add_argument("--i-understand-this-is-production", action="store_true")
    parser.add_argument("--report", help="write the proposed-record JSON report here")
    parser.add_argument("--checkpoint", help="resumable progress file")
    parser.add_argument(
        "--limit", type=int, default=0, help="stop after N rows (0 = no limit)"
    )
    args = parser.parse_args(argv)

    if args.env == "prod" and args.execute and not args.i_understand_this_is_production:
        raise SystemExit(
            "production writes require --i-understand-this-is-production (R16)"
        )

    config = load_config()
    if config.deploy_env != args.env:
        raise SystemExit(
            f"DEPLOY_ENV={config.deploy_env!r} does not match --env {args.env!r}; "
            "export the target environment's configuration before running"
        )
    if args.execute and not config.registry_id:
        raise SystemExit("REGISTRY_ID is unset; run scripts/registry_provision.py first")

    verify_caller(args.env, config.region)

    import boto3

    ddb = boto3.resource("dynamodb", region_name=config.region)
    asset_table = ddb.Table(config.asset_index_table)

    projector: Optional[Projector] = None
    if args.execute:
        if not config.projection_state_table:
            raise SystemExit("PROJECTION_STATE_TABLE is required for --execute")
        projector = Projector(
            config=config,
            asset_table=asset_table,
            state=ProjectionState(
                ddb.Table(config.projection_state_table),
                environment=config.deploy_env,
            ),
            control=registry_client.control_client(config.region),
            s3=boto3.client("s3", region_name=config.region),
        )

    categories = set(args.category or (PROJECTABLE_CATEGORIES - {"mcp-endpoint"}))
    done = load_checkpoint(args.checkpoint)
    report: list[dict[str, Any]] = []
    counts = {"seen": 0, "skipped": 0, "poison": 0, "projected": 0}

    for row in iter_eligible_rows(asset_table, categories=categories):
        category, asset_id = row["category"], row["assetId"]
        version = int(row["version"])
        key = f"{category}/{asset_id}/v{version}"
        counts["seen"] += 1
        if key in done:
            counts["skipped"] += 1
            continue

        try:
            descriptor = build_descriptor(
                category, row, artifacts_bucket=config.artifacts_bucket
            )
            entry = {
                "name": naming.record_name(category, asset_id),
                "recordVersion": naming.record_version(version),
                "recordType": row["category"],
                "displayName": row.get("name"),
                "digest": descriptor_digest(descriptor),
                "scope": row.get("scope"),
            }
        except PoisonDescriptorError as err:
            counts["poison"] += 1
            # Reported, not raised: one unprojectable asset must not abort a
            # backfill of hundreds, and the report is exactly the artifact the
            # Task 18 review reads.
            report.append({"key": key, "error": err.reason, "detail": err.detail})
            print(f"POISON {key}: {err.reason} ({err.detail})")
            continue

        report.append(entry)
        if projector is not None:
            outcome = projector.project(as_replay_event(row))
            counts["projected"] += 1
            append_checkpoint(args.checkpoint, key)
            print(f"{outcome:>10}  {entry['name']} {entry['recordVersion']}")
        else:
            print(f"{'dry-run':>10}  {entry['name']} {entry['recordVersion']} {entry['digest'][:19]}")

        if args.limit and counts["seen"] >= args.limit:
            print(f"stopping at --limit {args.limit}")
            break

    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(
                {"env": args.env, "counts": counts, "records": report},
                handle,
                indent=2,
            )
        print(f"\nreport written to {args.report}")

    print(f"\n{'EXECUTED' if args.execute else 'DRY RUN'} {counts}")
    if counts["poison"]:
        print(
            f"{counts['poison']} record(s) could not be projected — review the "
            "report before treating this backfill as complete (Task 18)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
