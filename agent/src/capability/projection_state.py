"""Projection state — what the shared Registry currently reflects (R7).

One row per ``(environment, category, asset_id, version)``::

    PK = <environment>#<category>#<asset_id>
    SK = v<version>

It answers three questions the Registry itself cannot:

* **Has this already converged?** A stored ``descriptorDigest`` equal to the
  freshly recomputed one, with ``registryStatus = APPROVED``, means a replayed
  event is a no-op (Property 4).
* **Is this owner's publish discoverable yet?** ``pending`` / ``failed`` /
  ``ineligible`` is what the Capability API surfaces to an authorized owner
  instead of implying a record exists (R12).
* **What did the last attempt do?** ``attemptCount``, ``lastErrorCode``, and a
  redacted message support bounded retry, DLQ triage, and replay without
  keeping descriptor bodies or private paths in logs.

The table is a projection cache, never a source of truth: deleting it loses
nothing that reconciliation cannot rebuild from canonical state plus the
Registry.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .registry import DISCOVERABLE_STATUS

logger = logging.getLogger(__name__)

#: ``eligibility`` values. ``ineligible`` is a normal terminal state for a
#: private/group/working-copy asset, not a failure.
ELIGIBLE: str = "eligible"
INELIGIBLE: str = "ineligible"

#: Projection outcomes surfaced to owners, mirroring
#: :data:`capability.dto.PROJECTION_STATUSES`.
IN_SYNC: str = "in_sync"
PENDING: str = "pending"
FAILED: str = "failed"


def state_key(
    environment: str, category: str, asset_id: str, version: int | str
) -> dict[str, str]:
    """The ``(PK, SK)`` pair for one projected version."""
    return {
        "PK": f"{environment}#{category}#{asset_id}",
        "SK": f"v{version}",
    }


class ProjectionState:
    """Thin DynamoDB wrapper over the projection-state table.

    Args:
        table: A boto3 DynamoDB ``Table`` resource (or compatible fake).
        environment: ``DEPLOY_ENV``; part of the partition key so a table
            shared by accident cannot blur two environments' state.
    """

    def __init__(self, table: Any, *, environment: str) -> None:
        self._table = table
        self._environment = environment

    def get(
        self, category: str, asset_id: str, version: int | str
    ) -> Optional[dict[str, Any]]:
        """The stored row, or ``None`` when this version was never projected."""
        response = self._table.get_item(
            Key=state_key(self._environment, category, asset_id, version)
        )
        return response.get("Item")

    def record_success(
        self,
        *,
        category: str,
        asset_id: str,
        version: int | str,
        canonical_revision: str,
        descriptor_digest: str,
        registry_id: str,
        record_id: str,
        record_arn: str,
        registry_status: str,
        occurred_at: str,
    ) -> None:
        """Record a successful projection attempt, and whether it is *visible*.

        The attempt succeeding and the capability being discoverable are two
        different facts, so ``projectionStatus`` is derived from
        ``registry_status`` rather than assumed:

        * ``APPROVED`` — the only discoverable status — is ``in_sync``.
        * anything else (``CREATING``, ``UPDATING``, ``PENDING_APPROVAL``,
          ``DRAFT``) is ``pending``: the write landed, the record does not serve.

        This distinction is load-bearing because it is reachable on the **happy
        path**. ``CreateRegistryRecord`` commonly returns ``CREATING``, and
        :meth:`Projector._ensure_approved` then defers the approval submit on
        purpose — submitting against an in-flight record is rejected. The record
        settles to ``DRAFT`` and needs a later event or a replay to reach
        ``APPROVED``. Observed in sandbox on 2026-08-14: a v4 record sat at
        ``DRAFT`` and returned nothing from
        ``ListDiscoverableRegistryRecords``, while this row said ``in_sync``.

        Reporting ``in_sync`` there made the health field the opposite of the
        truth: an alarm on ``projectionStatus != in_sync`` saw nothing wrong
        while the capability was invisible to every consumer. A ``pending`` count
        is now both the honest signal and the work queue for a replay.
        """
        discoverable = registry_status == DISCOVERABLE_STATUS
        self._table.put_item(
            Item={
                **state_key(self._environment, category, asset_id, version),
                "environment": self._environment,
                "category": category,
                "assetId": asset_id,
                "version": version,
                "canonicalRevision": canonical_revision,
                "descriptorDigest": descriptor_digest,
                "eligibility": ELIGIBLE,
                "registryId": registry_id,
                "recordId": record_id,
                "recordArn": record_arn,
                "registryStatus": registry_status,
                "projectionStatus": IN_SYNC if discoverable else PENDING,
                "attemptCount": 0,
                # Only set when the record is actually serving. A `pending` row
                # carrying a `lastSuccessAt` would read as "converged at 04:35"
                # to anything measuring projection lag.
                **({"lastSuccessAt": occurred_at} if discoverable else {}),
                "lastAttemptAt": occurred_at,
            }
        )
        if not discoverable:
            logger.info(
                "projection pending: %s#%s v%s registryStatus=%s (written, "
                "not yet discoverable)",
                category,
                asset_id,
                version,
                registry_status,
            )

    def record_ineligible(
        self,
        *,
        category: str,
        asset_id: str,
        version: int | str,
        occurred_at: str,
        reason: str,
        registry_status: Optional[str] = None,
    ) -> None:
        """Mark a version as deliberately outside the shared Registry.

        Called both for an asset that was never eligible and for one whose
        Registry revision has just been deprecated after narrowing scope —
        the canonical row is untouched either way (R16, Property 1).
        """
        update = (
            "SET eligibility = :ineligible, projectionStatus = :ineligible, "
            "lastAttemptAt = :now, ineligibleReason = :reason, "
            "attemptCount = :zero"
        )
        values: dict[str, Any] = {
            ":ineligible": INELIGIBLE,
            ":now": occurred_at,
            ":reason": reason,
            ":zero": 0,
        }
        if registry_status is not None:
            update += ", registryStatus = :status"
            values[":status"] = registry_status
        self._table.update_item(
            Key=state_key(self._environment, category, asset_id, version),
            UpdateExpression=update,
            ExpressionAttributeValues=values,
        )

    def record_failure(
        self,
        *,
        category: str,
        asset_id: str,
        version: int | str,
        occurred_at: str,
        error_code: str,
        error_message: str,
    ) -> None:
        """Increment the attempt counter and store a redacted error class.

        ``error_message`` is truncated hard: a Registry validation error can
        echo the payload back, and descriptor bodies must not land in this
        table any more than in logs (design "Observability").
        """
        self._table.update_item(
            Key=state_key(self._environment, category, asset_id, version),
            UpdateExpression=(
                "SET projectionStatus = :failed, lastAttemptAt = :now, "
                "lastErrorCode = :code, lastErrorMessageRedacted = :msg "
                "ADD attemptCount :one"
            ),
            ExpressionAttributeValues={
                ":failed": FAILED,
                ":now": occurred_at,
                ":code": error_code,
                ":msg": error_message[:256],
                ":one": 1,
            },
        )


__all__ = [
    "ELIGIBLE",
    "FAILED",
    "INELIGIBLE",
    "IN_SYNC",
    "PENDING",
    "ProjectionState",
    "state_key",
]
