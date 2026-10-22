#!/usr/bin/env python3
"""Backfill ``folder`` onto generated Project_Assets registered before folders existed (design D20).

Every ASSET row with ``source == "generated"`` and no ``folder`` gets the folder Generated_Media now
assigns at registration: ``Generated/<session title> · <YYYY-MM-DD>`` (title from the sessions table row
of the asset's ``originSessionId``), else ``Generated/Session <first 8 of sid> · <date>``, else (no
session) ``Generated/<date>``. The date is the session's ``createdAt`` (UTC); where the live path would use
the source object's ``LastModified``, the backfill uses the row's ``registeredAt`` (the copy was made at
turn end, so it is the same day in practice) rather than HEADing every origin object.

The write is ``SET folder`` conditioned on the row existing and still having no folder, so a re-run, or a
row a newer registration already filed, is left alone. ``rowVersion`` is NOT bumped: a folder is display
metadata, and the pipeline's status transitions condition on ``rowVersion``.

Safety (same pattern as ``registry_provision.py``):

* Dry run is the default (prints what it would write). Writes need ``--execute``.
* ``--env`` is required and the ambient credentials must be that env's account; anything else refuses.
* ``prod`` additionally requires ``--i-understand-this-is-production``.

Usage::

    AWS_PROFILE=<p> uv run python scripts/backfill_generated_folders.py --env staging            # dry run
    AWS_PROFILE=<p> uv run python scripts/backfill_generated_folders.py --env staging --execute
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Iterable, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion.folders import generated_folder, date_from  # noqa: E402
from ingestion.handlers.generated_media import session_row  # noqa: E402

ENVIRONMENTS = {
    "sandbox": "100000000001",
    "staging": "100000000002",
    "prod": "100000000003",
}
REGION = "us-west-2"
PROJECT_NAME = "mediaworks-harness"


def verify_caller(env: str, sts: Any) -> str:
    """Refuse unless the ambient credentials are ``env``'s account."""
    identity = sts.get_caller_identity()
    account = identity["Account"]
    expected = ENVIRONMENTS[env]
    if account != expected:
        raise SystemExit(
            f"refusing to act: --env {env} expects account {expected}, but the current credentials are "
            f"account {account} ({identity.get('Arn')})"
        )
    return account


def generated_rows_without_folder(table: Any) -> Iterable[dict]:
    """Every ASSET row of every Project with ``source = generated`` and no ``folder`` (a paged Scan)."""
    from boto3.dynamodb.conditions import Attr

    kwargs: dict = {
        "FilterExpression": Attr("SK").begins_with("ASSET#") & Attr("source").eq("generated")
        & Attr("folder").not_exists(),
        "ProjectionExpression": "PK, SK, assetId, displayName, originSessionId, registeredAt",
    }
    while True:
        resp = table.scan(**kwargs)
        yield from resp.get("Items", [])
        if not resp.get("LastEvaluatedKey"):
            return
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def folder_for_row(row: dict, sessions_table: Any, cache: dict) -> str:
    """The folder the live Generated_Media path would give ``row`` (see the module docstring)."""
    sid = row.get("originSessionId") if isinstance(row.get("originSessionId"), str) else None
    if sid and sid not in cache:
        cache[sid] = session_row(sessions_table, sid)
    session = (cache.get(sid) if sid else None) or {}
    day = date_from(session.get("createdAt")) or date_from(row.get("registeredAt")) or "1970-01-01"
    return generated_folder(title=session.get("title"), session_id=sid, day=day)


def run(projects_table: Any, sessions_table: Any, *, execute: bool, out=print) -> dict:
    """Backfill (or, without ``execute``, report) every generated row without a folder."""
    from botocore.exceptions import ClientError

    cache: dict[str, Optional[dict]] = {}
    seen = written = skipped = 0
    for row in generated_rows_without_folder(projects_table):
        seen += 1
        folder = folder_for_row(row, sessions_table, cache)
        out(f"{'SET ' if execute else 'would set'} {row['PK']} {row['SK']} ({row.get('displayName')}) -> {folder}")
        if not execute:
            continue
        try:
            projects_table.update_item(
                Key={"PK": row["PK"], "SK": row["SK"]},
                UpdateExpression="SET folder = :f",
                ConditionExpression="attribute_exists(PK) AND attribute_not_exists(folder)",
                ExpressionAttributeValues={":f": folder},
            )
            written += 1
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            skipped += 1  # removed meanwhile, or already filed
    summary = {"rows": seen, "written": written, "skipped": skipped, "dryRun": not execute}
    out(f"summary: {summary}")
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, choices=sorted(ENVIRONMENTS))
    parser.add_argument("--execute", action="store_true", help="write (default: dry run)")
    parser.add_argument("--i-understand-this-is-production", dest="prod_ok", action="store_true")
    parser.add_argument("--projects-table", default=f"{PROJECT_NAME}-projects")
    parser.add_argument("--sessions-table", default=f"{PROJECT_NAME}-sessions")
    args = parser.parse_args(argv)
    if args.env == "prod" and args.execute and not args.prod_ok:
        raise SystemExit("refusing to write prod without --i-understand-this-is-production")

    import boto3

    account = verify_caller(args.env, boto3.client("sts", region_name=REGION))
    print(f"env={args.env} account={account} projects={args.projects_table} sessions={args.sessions_table} "
          f"{'EXECUTE' if args.execute else 'dry run'}")
    ddb = boto3.resource("dynamodb", region_name=REGION)
    run(ddb.Table(args.projects_table), ddb.Table(args.sessions_table), execute=args.execute)
    return 0


if __name__ == "__main__":
    sys.exit(main())
