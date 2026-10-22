#!/usr/bin/env python3
"""Queue for indexing the PDF documents that were registered ``stored`` while session PDFs were not indexed.

From 2026-10-09 (``1d754e1d``) to this change, Generated_Media registered every session PDF (a content-moderation
compliance report, say) with ``queue_for_indexing=False`` — "a document has no ingestion pipeline", written the day
before the document pipeline landed (``384638f6``). So those reports were never searchable. New ones are queued at
registration; this moves the existing ones ``stored`` → ``queued`` with :meth:`AssetStore.queue_stored` (``kind=
"document"``), the same transaction the indexing switch uses: per asset, conditioned on the row still being
``stored`` at the version read, and only in a Project whose indexing is ON. A row a run already found unindexable
(``indexSkippedReason``, e.g. a scanned PDF) is never re-queued.

Safety (same pattern as ``backfill_generated_folders.py``):

* Dry run is the default (prints what it would queue). Writes need ``--execute``.
* ``--env`` is required and the ambient credentials must be that env's account; anything else refuses.
* ``prod`` additionally requires ``--i-understand-this-is-production``.

Usage::

    AWS_PROFILE=<p> uv run python scripts/requeue_stored_documents.py --env staging            # dry run
    AWS_PROFILE=<p> uv run python scripts/requeue_stored_documents.py --env staging --execute
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion.store import AssetStore, indexing_on, queueable  # noqa: E402

ENVIRONMENTS = {"sandbox": "100000000001", "staging": "100000000002", "prod": "100000000003"}
REGION = "us-west-2"
PROJECT_NAME = "mediaworks-harness"


def verify_caller(env: str, sts: Any) -> str:
    """Refuse unless the ambient credentials are ``env``'s account."""
    identity = sts.get_caller_identity()
    account, expected = identity["Account"], ENVIRONMENTS[env]
    if account != expected:
        raise SystemExit(f"refusing to act: --env {env} expects account {expected}, but the current credentials "
                         f"are account {account} ({identity.get('Arn')})")
    return account


def stored_documents(table: Any) -> dict[str, list[dict]]:
    """Project id -> its ``stored`` document ASSET rows (a paged Scan; the table holds every Project)."""
    from boto3.dynamodb.conditions import Attr

    by_pid: dict[str, list[dict]] = collections.defaultdict(list)
    kwargs: dict = {"FilterExpression": Attr("SK").begins_with("ASSET#document:") & Attr("status").eq("stored"),
                    "ProjectionExpression": "PK, SK, assetId, displayName, #r, indexSkippedReason",
                    "ExpressionAttributeNames": {"#r": "role"}}
    while True:
        resp = table.scan(**kwargs)
        for row in resp.get("Items", []):
            by_pid[str(row["PK"]).split("#", 1)[-1]].append(row)
        if not resp.get("LastEvaluatedKey"):
            return by_pid
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def run(table: Any, *, execute: bool, out=print) -> dict:
    """Queue (or, without ``execute``, report) every eligible stored document, Project by Project."""
    store = AssetStore(table)
    seen = eligible = queued = 0
    skipped_off = 0
    for pid, rows in sorted(stored_documents(table).items()):
        seen += len(rows)
        candidates = [r for r in rows if queueable(r)]
        if not candidates:
            continue
        if not indexing_on(store.get_meta(pid)):
            skipped_off += len(candidates)
            out(f"skip  {pid}: indexing is off ({len(candidates)} document(s) stay stored)")
            continue
        eligible += len(candidates)
        for r in candidates:
            out(f"{'queue' if execute else 'would queue'} {pid} {r.get('assetId')} ({r.get('displayName')})")
        if execute:
            queued += store.queue_stored(pid, kind="document")
    summary = {"storedDocuments": seen, "eligible": eligible, "queued": queued,
               "indexingOff": skipped_off, "dryRun": not execute}
    out(f"summary: {summary}")
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, choices=sorted(ENVIRONMENTS))
    parser.add_argument("--execute", action="store_true", help="write (default: dry run)")
    parser.add_argument("--i-understand-this-is-production", dest="prod_ok", action="store_true")
    parser.add_argument("--projects-table", default=f"{PROJECT_NAME}-projects")
    args = parser.parse_args(argv)
    if args.env == "prod" and args.execute and not args.prod_ok:
        raise SystemExit("refusing to write prod without --i-understand-this-is-production")

    import boto3

    account = verify_caller(args.env, boto3.client("sts", region_name=REGION))
    print(f"env={args.env} account={account} projects={args.projects_table} "
          f"{'EXECUTE' if args.execute else 'dry run'}")
    run(boto3.resource("dynamodb", region_name=REGION).Table(args.projects_table), execute=args.execute)
    return 0


if __name__ == "__main__":
    sys.exit(main())
