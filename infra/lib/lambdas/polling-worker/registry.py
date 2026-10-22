"""Polling-worker view of the JobRegistry — boto3 wrapper.

This module is intentionally a *minimal copy* of the surface area from
``agent/src/jobs/registry.py`` (task 4.1) that the polling worker needs.
The polling worker is a separate deployment artifact (its own Lambda
zip), so it does not import from ``agent/src``; bundling that whole
package would drag in MCP, Strands, and httpx for no reason. The
relevant idioms are mirrored here:

* ``boto3.resource("dynamodb", region_name=region).Table(table_name)``
  construction — same shape as ``agent/src/state_store.py``.
* ``UpdateItem`` + ``ConditionExpression`` for every status transition
  (R4.2, R4.3, R4.5).
* Epoch-seconds ``ttl`` attribute set only on terminal transition
  (R5.3) — active jobs never carry a TTL and never get expired.
* ``REMOVE pollPartition`` on terminal transition so the row drops out
  of the ``poll-partition-index`` GSI and stops being polled (R4.3).

If task 4.1's ``JobRegistry`` lands and is later refactored into a
shared package the polling worker can consume, this file becomes
redundant and should be deleted.

See ``.kiro/specs/async-job-tracking/design.md`` for the canonical
schema and the "Polling_Worker" / "Implementation idioms (mirror
state_store.py)" sections for the patterns being mirrored.

Requirements: 4.2, 4.3, 4.5, 5.3, 11.3
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterable

import boto3
from boto3.dynamodb.conditions import Attr, Key

# 30 days, matching the design default. Configurable via stack
# parameter in CDK task 8.1; the runtime side reads it from the
# JobRegistry class. Kept as a module-level constant here so the
# polling worker doesn't grow an env-var read it never needs to
# customize.
RETENTION_SECONDS = 30 * 24 * 60 * 60

# The literal partition value used on every active job. Mirrors the
# constant in the design doc — written by ``register`` (task 4.1) on
# first registration and ``REMOVE``d on terminal transition.
ACTIVE_POLL_PARTITION = "ACTIVE"


def _now_iso() -> str:
    """ISO-8601 UTC timestamp matching the format ``state_store.py`` writes."""
    return datetime.now(timezone.utc).isoformat()


class JobRegistry:
    """Polling-worker boto3 wrapper for the job-registry table.

    Only the methods the polling worker actually needs are implemented:

    * :meth:`query_active_jobs` — read the ``poll-partition-index`` GSI
      to drive the polling cycle (R4.1).
    * :meth:`set_active_status` — conditional active->active write
      (R4.2). Mirrors ``StateStore.update_node_status``: an
      expected-status guard prevents racing a terminal write back to
      active.
    * :meth:`bump_error` — increment ``consecutiveErrors`` and stash
      ``lastError``. Used on every non-transient failure as the
      pre-step before the threshold check (R4.5).
    * :meth:`set_terminal` — terminal transition: stamp
      ``terminalAt`` + ``ttl``, drop ``pollPartition`` so the row
      leaves the GSI, and clear ``consecutiveErrors`` (R4.3, R5.3).

    Read paths in this worker are not actor-scoped — the worker runs
    as a service identity that polls *every* active job across all
    actors. Per-job actor enforcement is the responsibility of the
    user-facing read paths (``GET /jobs``, ``get_job`` tool).
    """

    def __init__(self, table_name: str, region: str, poll_index_name: str):
        self._resource = boto3.resource("dynamodb", region_name=region)
        self._table = self._resource.Table(table_name)
        self._poll_index_name = poll_index_name

    # --- read path ---------------------------------------------------

    def query_active_jobs(self) -> Iterable[dict[str, Any]]:
        """Yield every active job row from the ``poll-partition-index`` GSI.

        Active jobs are exactly those whose ``pollPartition`` attribute
        equals :data:`ACTIVE_POLL_PARTITION`. Terminal jobs have the
        attribute removed by :meth:`set_terminal` and so don't appear
        on this index at all (R4.3).

        Pages through ``LastEvaluatedKey`` so a registry with more
        active jobs than fit in one page still produces the full set.
        Yields raw DDB items (dicts of attribute_name -> value) so the
        caller can read whatever fields it needs without an
        intermediate dataclass.
        """
        kwargs: dict[str, Any] = {
            "IndexName": self._poll_index_name,
            "KeyConditionExpression": Key("pollPartition").eq(ACTIVE_POLL_PARTITION),
        }
        while True:
            resp = self._table.query(**kwargs)
            for item in resp.get("Items", []):
                yield item
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                return
            kwargs["ExclusiveStartKey"] = last_key

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """One job row by id, read CONSISTENTLY, or ``None`` when absent.

        Used by the Step Functions completion-event path, which must decide
        whether an execution is one of ours and still active. A consistent read
        matters there: an eventually-consistent miss right after registration
        would drop the event, and the job would wait for the next poll.
        """
        resp = self._table.get_item(Key={"jobId": job_id}, ConsistentRead=True)
        item = resp.get("Item")
        return item if isinstance(item, dict) else None

    # --- write paths -------------------------------------------------

    def set_active_status(
        self,
        job_id: str,
        *,
        status: str,
        expected_status: str,
    ) -> bool:
        """Conditional active->active status update (R4.2).

        Returns ``True`` if the update committed, ``False`` if the
        expected-status guard failed (e.g. another writer already
        moved the job to a different status). Also clears
        ``consecutiveErrors`` since a successful status response is
        evidence the upstream tool is healthy again.
        """
        now = _now_iso()
        try:
            self._table.update_item(
                Key={"jobId": job_id},
                UpdateExpression=(
                    "SET #st = :s, updatedAt = :now, "
                    "consecutiveErrors = :zero"
                ),
                ConditionExpression=Attr("status").eq(expected_status),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":s": status,
                    ":now": now,
                    ":zero": 0,
                },
            )
            return True
        except self._resource.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def bump_error(self, job_id: str, error: str) -> int:
        """Increment ``consecutiveErrors`` and record ``lastError``.

        Returns the new ``consecutiveErrors`` count so the caller can
        compare against the threshold without an extra read. Uses
        DDB's ``ADD`` arithmetic so concurrent bumps from a future
        parallelized polling worker remain race-safe.

        No expected-status guard: if the job is already terminal,
        ``set_terminal`` will have removed ``pollPartition``, so the
        polling worker won't see it at all in the next cycle. Bumping
        an error on a terminal record is harmless (the row's
        ``ttl`` still ticks down) and cheaper than a conditional
        write that would race the terminal transition.
        """
        now = _now_iso()
        resp = self._table.update_item(
            Key={"jobId": job_id},
            UpdateExpression=(
                "ADD consecutiveErrors :one "
                "SET lastError = :err, updatedAt = :now"
            ),
            ExpressionAttributeValues={
                ":one": 1,
                ":err": error,
                ":now": now,
            },
            ReturnValues="UPDATED_NEW",
        )
        attrs = resp.get("Attributes", {})
        # `consecutiveErrors` comes back as a Decimal from DDB;
        # the caller compares it as an int.
        return int(attrs.get("consecutiveErrors", 0))

    def set_terminal(
        self,
        job_id: str,
        *,
        status: str,
        error: str | None,
        has_renderable_artifacts: bool = False,
        artifact_urls: list[str] | None = None,
        result_key: str = "",
    ) -> bool:
        """Move the job to a terminal status (R4.3, R5.3).

        Stamps ``terminalAt`` + ``ttl``, removes ``pollPartition`` so
        the row drops out of the ``poll-partition-index`` GSI, and
        clears ``consecutiveErrors`` so the record reads cleanly to a
        consumer that doesn't care about the failure history.

        ``result_key`` (when present) points at the job's RESOLVED result artifact,
        the same schema the plan chainer writes at settle. Retrieving a lone job
        used to fan out to ~27 model steps because no aggregate existed for it.

        ``artifact_urls`` (when present) are the concrete renderable output
        URL(s) extracted from the completed status payload, persisted so the
        browser can render a finished job deterministically (no LLM turn) via
        the ``/render`` endpoint.

        Guarded by ``status IN (PENDING, RUNNING)`` so a concurrent
        terminal write can't double-stamp the row. Returns ``False``
        when the guard fails (e.g. another worker already moved the
        job to terminal); callers treat that as a no-op.
        """
        now = _now_iso()
        ttl = int(time.time()) + RETENTION_SECONDS
        try:
            self._table.update_item(
                Key={"jobId": job_id},
                UpdateExpression=(
                    "SET #st = :s, terminalAt = :now, updatedAt = :now, "
                    "lastError = :err, #ttl = :ttl, "
                    "consecutiveErrors = :zero, hasRenderableArtifacts = :renderable, "
                    "artifactUrls = :urls"
                    + (", resultKey = :rk" if result_key else "")
                    + " REMOVE pollPartition"
                ),
                ConditionExpression=Attr("status").is_in(["PENDING", "RUNNING"]),
                ExpressionAttributeNames={"#st": "status", "#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":s": status,
                    ":now": now,
                    ":err": error,
                    ":ttl": ttl,
                    ":zero": 0,
                    ":renderable": bool(has_renderable_artifacts),
                    ":urls": list(artifact_urls or []),
                    **({":rk": result_key} if result_key else {}),
                },
            )
            return True
        except self._resource.meta.client.exceptions.ConditionalCheckFailedException:
            return False
