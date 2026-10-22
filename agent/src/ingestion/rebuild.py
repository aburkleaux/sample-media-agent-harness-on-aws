"""Collection_Rebuild triggers and coalescing (Req 6.9, 10.5, 10.9, 10.10, 10.12, 10.13; design Component 14).

A rebuild replaces a Project's search collection with a fresh incarnation holding only its ``indexed`` assets:
the way removed assets and stale scopes leave search. Starting one is a single conditional update of META:

- ``ready`` → ``rebuilding``, ``rebuild.seq + 1``, ``phase = waiting``, then ``StartExecution(rebuild-<pid>-<seq>)``;
- ``rebuilding`` → ``rebuild.pending = true`` (any number of triggers collapse into one follow-up);
- ``rebuild_failed`` → nothing automatic; only :func:`retry_rebuild` (the Projects surface's retry) starts one;
- ``absent`` → nothing to rebuild.

The rebuild's last step sets ``ready`` and, when ``pending`` was set, starts the follow-up in the same pass.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from botocore.exceptions import ClientError

from projects import schema

log = logging.getLogger(__name__)

STARTED, COALESCED, NOT_STARTED = "started", "coalesced", "not_started"


def execution_name(pid: str, seq: int) -> str:
    return f"rebuild-{pid}-{int(seq)}"


def _cond_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def start_execution(sfn: Any, sm_arn: str, pid: str, seq: int) -> None:
    try:
        sfn.start_execution(stateMachineArn=sm_arn, name=execution_name(pid, seq),
                            input=json.dumps({"pid": pid, "seq": int(seq)}, sort_keys=True))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
            raise


def _begin(table: Any, pid: str, from_state: str, old_seq: Optional[int], now_iso: str, reason: str) -> int:
    seq = int(old_seq or 0) + 1
    cond = "#cs = :from AND #st = :active AND " + (
        "attribute_not_exists(#rb)" if old_seq is None else "#rb.seq = :old")
    values: dict = {":from": from_state, ":active": "active", ":rb": "rebuilding", ":one": 1,
                    ":r": {"seq": seq, "phase": "waiting", "pending": False, "reason": reason[:200],
                           "startedAt": now_iso}}
    if old_seq is not None:
        values[":old"] = old_seq
    table.update_item(Key=schema.meta_key(pid),
                      UpdateExpression="SET #cs = :rb, #rb = :r, changeSeq = changeSeq + :one",
                      ConditionExpression=cond,
                      ExpressionAttributeNames={"#cs": "collectionState", "#st": "status", "#rb": "rebuild"},
                      ExpressionAttributeValues=values)
    return seq


def request_rebuild(table: Any, sfn: Any, sm_arn: str, pid: str, *, reason: str, now_iso: str) -> str:
    """Start or coalesce a rebuild. Returns ``started`` / ``coalesced`` / ``not_started``."""
    for _ in range(3):
        meta = table.get_item(Key=schema.meta_key(pid), ConsistentRead=True).get("Item") or {}
        if meta.get("status") != "active":
            return NOT_STARTED
        state = meta.get("collectionState") or "absent"
        rb = meta.get("rebuild") or {}
        old = int(rb["seq"]) if isinstance(rb, dict) and rb.get("seq") is not None else None
        try:
            if state == "ready":
                seq = _begin(table, pid, "ready", old, now_iso, reason)
                start_execution(sfn, sm_arn, pid, seq)
                log.info("rebuild %s started for %s (%s)", seq, pid, reason)
                return STARTED
            if state == "rebuilding":
                table.update_item(Key=schema.meta_key(pid), UpdateExpression="SET #rb.pending = :t",
                                  ConditionExpression="#cs = :rb",
                                  ExpressionAttributeNames={"#cs": "collectionState", "#rb": "rebuild"},
                                  ExpressionAttributeValues={":t": True, ":rb": "rebuilding"})
                return COALESCED
            return NOT_STARTED
        except ClientError as exc:
            if not _cond_failed(exc):
                raise
    return NOT_STARTED


def retry_rebuild(table: Any, sfn: Any, sm_arn: str, pid: str, *, now_iso: str) -> Optional[int]:
    """``rebuild_failed`` → a new rebuild from step (a) (Req 10.13). ``None`` when not in ``rebuild_failed``."""
    meta = table.get_item(Key=schema.meta_key(pid), ConsistentRead=True).get("Item") or {}
    if meta.get("collectionState") != "rebuild_failed":
        return None
    rb = meta.get("rebuild") or {}
    try:
        seq = _begin(table, pid, "rebuild_failed", int(rb["seq"]) if rb.get("seq") is not None else None,
                     now_iso, "retry")
    except ClientError as exc:
        if _cond_failed(exc):
            return None
        raise
    start_execution(sfn, sm_arn, pid, seq)
    return seq


def accepted_in_current(row: dict, meta: dict) -> bool:
    """Did the current incarnation accept an ``index_content`` call for this asset? (Req 10.5)"""
    inc = str(int(meta.get("incarnation") or 0))
    return bool(((row.get("index") or {}).get("byIncarnation") or {}).get(inc, {}).get("accepted"))
