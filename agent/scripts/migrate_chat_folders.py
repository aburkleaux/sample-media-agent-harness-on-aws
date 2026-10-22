#!/usr/bin/env python3
"""Move every chat folder under the one ``Chats/`` root.

Chat (session) outputs were filed under ``Sessions/<title> · <date>`` until 2026-10, and the older generated copies
under ``Generated/<title> · <date>``. New ones go to ``Chats/<title> · <date>`` (``ingestion.folders.SESSIONS_ROOT``).
This re-files the existing rows the same way ``rename_folder`` does: ``Sessions/X`` → ``Chats/X`` and
``Generated/X`` → ``Chats/X``, keeping every subfolder. A folder is metadata only — no bytes move and nothing is
re-indexed — and :meth:`AssetStore.set_folder` never bumps ``rowVersion``, so a pipeline mid-run is undisturbed.
Re-running it moves nothing twice (an already-moved row is no longer under a legacy root).

Safety (same pattern as ``requeue_stored_documents.py``):

* Dry run is the default (prints what it would move). Writes need ``--execute``.
* ``--env`` is required and the ambient credentials must be that env's account; anything else refuses.
* ``prod`` additionally requires ``--i-understand-this-is-production``.

Usage::

    AWS_PROFILE=<p> uv run python scripts/migrate_chat_folders.py --env staging            # dry run
    AWS_PROFILE=<p> uv run python scripts/migrate_chat_folders.py --env staging --execute
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion.folders import LEGACY_SESSION_ROOTS, SESSIONS_ROOT, folder_or_error  # noqa: E402
from ingestion.store import AssetNotFound, AssetStore  # noqa: E402

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


def target_folder(folder: Any) -> Optional[str]:
    """``Chats/…`` for a folder under a legacy chat root, else ``None`` (not a chat folder, or already moved)."""
    if not isinstance(folder, str):
        return None
    for root in LEGACY_SESSION_ROOTS:
        if folder == root or folder.startswith(f"{root}/"):
            moved, err = folder_or_error(f"{SESSIONS_ROOT}{folder[len(root):]}")
            return moved if moved and not err else None
    return None


def legacy_rows(table: Any) -> dict[str, list[dict]]:
    """Project id -> its live ASSET rows filed under a legacy chat root (a paged Scan over every Project)."""
    from boto3.dynamodb.conditions import Attr

    cond = Attr("SK").begins_with("ASSET#") & Attr("status").ne("removed")
    roots = None
    for root in LEGACY_SESSION_ROOTS:
        c = Attr("folder").eq(root) | Attr("folder").begins_with(f"{root}/")
        roots = c if roots is None else roots | c
    by_pid: dict[str, list[dict]] = collections.defaultdict(list)
    kwargs: dict = {"FilterExpression": cond & roots, "ProjectionExpression": "PK, SK, displayName, folder"}
    while True:
        resp = table.scan(**kwargs)
        for row in resp.get("Items", []):
            by_pid[str(row["PK"]).split("#", 1)[-1]].append(row)
        if not resp.get("LastEvaluatedKey"):
            return by_pid
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def run(table: Any, *, execute: bool, out=print) -> dict:
    """Re-file (or, without ``execute``, report) every legacy chat-folder row, Project by Project."""
    store = AssetStore(table)
    seen = moved = gone = 0
    for pid, rows in sorted(legacy_rows(table).items()):
        for r in rows:
            to = target_folder(r.get("folder"))
            if to is None:
                continue
            seen += 1
            aid = str(r["SK"]).split("#", 1)[-1]
            out(f"{'move' if execute else 'would move'} {pid} {aid}: {r.get('folder')!r} -> {to!r}")
            if execute:
                try:
                    store.set_folder(pid, aid, to)
                    moved += 1
                except AssetNotFound:
                    gone += 1  # removed since the scan read it
    summary = {"legacyRows": seen, "moved": moved, "removedMeanwhile": gone, "dryRun": not execute}
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
