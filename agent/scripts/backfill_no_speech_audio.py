#!/usr/bin/env python3
"""One-off: move audio assets that FAILED only for having no speech to ``stored`` (not indexed).

Since 2026-10 an audio Project_Asset with no speech (music / sound-effect stems) is kept as ``stored`` with
``indexSkippedReason: no_speech`` instead of ``failed`` (ingestion Req 12.10; ``reducer.plan_audio`` /
``Tick._skip``). Rows that failed for that reason BEFORE the change still show a red Failed badge and count as
failures. This moves exactly those rows:

* ``kind == "audio"``, ``status == "failed"``, ``failure.step == "speech-diarization"``, and a failure reason that
  is a no-speech outcome (``workflows.is_no_speech``: ``No VAD turns found``, ``No high-quality embeddings found``;
  never a no-decodable-audio error, which is a broken file) or the old ``no speech to index``.

Per row, one transaction (the same counters ``PipelineStore.transition`` keeps):

* ASSET: ``failed`` -> ``stored``, ``SET indexSkippedReason, indexSkippedStep, statusChangedAt``, ``REMOVE failure,
  failedAt``, ``rowVersion + 1``; conditioned on still ``failed`` at the ``rowVersion`` read.
* META: ``statusCounts.failed - 1``, ``statusCounts.stored + 1``, ``changeSeq + 1``.
* FAILIDX: the row's failure-index item is deleted (so it leaves "newest failures").

Any other failed row is left alone. Safety (same pattern as ``backfill_generated_folders.py``):

* Dry run is the default. Writes need ``--execute``.
* STAGING ONLY: ``--env staging`` is the only choice, and the ambient credentials must be the staging account.

Usage::

    AWS_PROFILE=default uv run python scripts/backfill_no_speech_audio.py --env staging            # dry run
    AWS_PROFILE=default uv run python scripts/backfill_no_speech_audio.py --env staging --execute
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import workflows as W  # noqa: E402
from ingestion.reducer import NO_SPEECH_REASON  # noqa: E402
from projects import schema  # noqa: E402

ENVIRONMENTS = {"staging": "100000000002"}
REGION = "us-west-2"
PROJECT_NAME = "mediaworks-harness"
_OLD_REASON = "no speech to index"


def verify_caller(env: str, sts: Any) -> str:
    """Refuse unless the ambient credentials are ``env``'s account."""
    identity = sts.get_caller_identity()
    account = identity["Account"]
    expected = ENVIRONMENTS[env]
    if account != expected:
        raise SystemExit(f"refusing to act: --env {env} expects account {expected}, but the current credentials "
                         f"are account {account} ({identity.get('Arn')})")
    return account


def is_no_speech_failure(row: dict) -> bool:
    """Whether a failed ASSET row failed ONLY because its audio holds no speech."""
    if row.get("kind") != "audio" or row.get("status") != "failed":
        return False
    failure = row.get("failure") if isinstance(row.get("failure"), dict) else {}
    if failure.get("step") != W.SPEECH_DIARIZATION:
        return False
    reason = str(failure.get("reason") or "")
    return W.is_no_speech(reason) or _OLD_REASON in reason


def failed_audio_rows(table: Any) -> Iterable[dict]:
    """Every failed audio ASSET row of every Project (a paged Scan)."""
    from boto3.dynamodb.conditions import Attr

    kwargs: dict = {"FilterExpression": Attr("SK").begins_with("ASSET#") & Attr("kind").eq("audio")
                    & Attr("status").eq("failed")}
    while True:
        resp = table.scan(**kwargs)
        yield from resp.get("Items", [])
        if not resp.get("LastEvaluatedKey"):
            return
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, Decimal)):
        return int(v)
    return None


def transaction_for(table_name: str, row: dict, now_iso: str) -> list[dict]:
    """The low-level TransactItems moving ``row`` from failed to stored (see the module docstring)."""
    pk, asset_id = row["PK"], str(row["assetId"])
    pid = pk.split("#", 1)[1]
    rv = _int(row.get("rowVersion"))
    ops: list[dict] = [{"Update": {
        "TableName": table_name, "Key": {"PK": {"S": pk}, "SK": {"S": row["SK"]}},
        "UpdateExpression": ("SET #st = :stored, statusChangedAt = :now, indexSkippedReason = :r, "
                             "indexSkippedStep = :step, rowVersion = rowVersion + :one REMOVE failure, failedAt"),
        "ConditionExpression": "#st = :failed AND rowVersion = :rv",
        "ExpressionAttributeNames": {"#st": "status"},
        "ExpressionAttributeValues": {":stored": {"S": "stored"}, ":failed": {"S": "failed"},
                                      ":now": {"S": now_iso}, ":r": {"S": NO_SPEECH_REASON},
                                      ":step": {"S": W.SPEECH_DIARIZATION}, ":one": {"N": "1"},
                                      ":rv": {"N": str(rv if rv is not None else 0)}}}},
        {"Update": {
            "TableName": table_name, "Key": {"PK": {"S": pk}, "SK": {"S": "META"}},
            "UpdateExpression": ("SET statusCounts.#f = statusCounts.#f - :one, "
                                 "statusCounts.#sd = if_not_exists(statusCounts.#sd, :zero) + :one, "
                                 "changeSeq = changeSeq + :one"),
            "ConditionExpression": "attribute_exists(statusCounts)",
            "ExpressionAttributeNames": {"#f": "failed", "#sd": "stored"},
            "ExpressionAttributeValues": {":one": {"N": "1"}, ":zero": {"N": "0"}}}}]
    failed_at = _int(row.get("failedAt"))
    if failed_at is not None:
        key = schema.failidx_key(pid, failed_at, asset_id)
        ops.append({"Delete": {"TableName": table_name,
                               "Key": {"PK": {"S": key["PK"]}, "SK": {"S": key["SK"]}}}})
    return ops


def run(table: Any, client: Any, *, execute: bool, out=print, now_iso: Optional[str] = None) -> dict:
    """Move (or, without ``execute``, report) every no-speech failed audio row."""
    from botocore.exceptions import ClientError

    now_iso = now_iso or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    seen = matched = written = lost = 0
    changed: list[dict] = []
    for row in failed_audio_rows(table):
        seen += 1
        if not is_no_speech_failure(row):
            continue
        matched += 1
        reason = str((row.get("failure") or {}).get("reason") or "")[:120]
        out(f"{'MOVE' if execute else 'would move'} {row['PK']} {row['SK']} ({row.get('displayName')}) "
            f"failed -> stored [{reason}]")
        if not execute:
            continue
        try:
            client.transact_write_items(TransactItems=transaction_for(table.name, row, now_iso))
            written += 1
            changed.append({"pk": row["PK"], "assetId": row.get("assetId"), "displayName": row.get("displayName")})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            lost += 1  # moved meanwhile (retried, removed): left alone
    summary = {"failedAudioRows": seen, "noSpeech": matched, "written": written, "skipped": lost,
               "dryRun": not execute, "changed": changed}
    out(f"summary: {summary}")
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, choices=sorted(ENVIRONMENTS))
    parser.add_argument("--execute", action="store_true", help="write (default: dry run)")
    parser.add_argument("--projects-table", default=f"{PROJECT_NAME}-projects")
    args = parser.parse_args(argv)

    import boto3

    account = verify_caller(args.env, boto3.client("sts", region_name=REGION))
    print(f"env={args.env} account={account} projects={args.projects_table} "
          f"{'EXECUTE' if args.execute else 'dry run'}")
    ddb = boto3.resource("dynamodb", region_name=REGION)
    run(ddb.Table(args.projects_table), boto3.client("dynamodb", region_name=REGION), execute=args.execute)
    return 0


if __name__ == "__main__":
    sys.exit(main())
