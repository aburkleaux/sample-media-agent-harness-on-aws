"""Registry projection worker — DynamoDB stream → AWS Agent Registry (R7).

Lambda entrypoint: ``capability.projector.handler``. The deployment asset is
``agent/src``, so this module imports :mod:`lab.asset_index` and
:mod:`lab.outbox` directly rather than re-declaring their schema constants —
a duplicated pad width or discriminator string is precisely the kind of drift
that stops projection silently.

Flow per event::

    outbox row (INSERT, recordType=outbox, environment matches)
        │
        ├─ reload the canonical version from the byPublishedId GSI
        ├─ re-check eligibility against THAT row, not the event
        ├─ build the descriptor (shared builders) and digest it
        ├─ converged? -> no-op
        └─ create-or-update the record, submit, record state

Invariants worth stating because they are load-bearing:

* **The event is a trigger, not data.** It carries identifiers only; every
  projected byte comes from the reloaded canonical row. A replayed or
  out-of-order event therefore cannot publish stale content, and an
  ``org → private`` transition that happened after the event was written is
  caught by the scope re-check immediately before the write (Property 2).
* **Canonical publish never depends on this.** Any failure here leaves the
  canonical row exactly as it was; the version stays published and usable,
  the owner sees ``pending`` or ``failed`` projection status (Property 1).
* **Ineligible means deprecate, never delete.** Narrowing scope removes a
  record from discovery by deprecating its revision. Nothing in this module
  deletes a Registry record or a canonical row (R16).
* **Raising is how an event reaches the DLQ.** The event source is configured
  with bounded retries and an SQS failure destination, so a poison descriptor
  surfaces as a DLQ message with the canonical asset untouched.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from lab.asset_index import GSI1_NAME, VERSION_PAD_WIDTH
from lab.outbox import (
    EVENT_DEPRECATED,
    EVENT_SCOPE_CHANGED,
    OUTBOX_RECORD_TYPE,
)

from . import naming, registry
from .config import CapabilityConfig, ConfigError, load_config
from .descriptors import (
    PoisonDescriptorError,
    RECORD_TYPE,
    build_descriptor,
    descriptor_digest,
    is_eligible,
)
from .projection_state import ProjectionState

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: Terminal-failure statuses. Retrying a submit on these would loop.
_FAILED_STATUSES = {"CREATE_FAILED", "UPDATE_FAILED"}

#: In-flight statuses; a submit against one of these is rejected by the
#: service, so the projector defers instead of consuming a retry.
_TRANSIENT_STATUSES = {"CREATING", "UPDATING", "PENDING_APPROVAL"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_description(row: dict[str, Any], category: str, asset_id: str) -> str:
    """A non-empty ``description`` for Create/UpdateRegistryRecord.

    The Registry enforces ``min length: 1`` on this field. Canonical rows have no
    ``description`` attribute at all — :class:`lab.models.AssetCommon` does not
    define one and the authoring UI has no field for it — so passing the row's
    value straight through meant passing ``""`` for **every asset in every
    category**, and `CreateRegistryRecord` rejected all of them with
    ``ParamValidationError`` before the request left the Lambda.

    That failure was invisible: it happens inside botocore's client-side
    validation, so there is no API call to find in CloudTrail, and the projector
    correctly recorded it as a transient failure and retried into the DLQ.
    Combined with the unstamped ``DEPLOY_ENV`` (fixed separately), the projection
    pipeline had two independent total blockers and had never once succeeded.

    So the description is generated. It is derived only from fields the row
    always has, which keeps it deterministic — a re-projection of the same
    version produces the same string and stays a no-op. It deliberately does not
    include the version: ``recordVersion`` already carries that, and folding it
    in here would make the text churn against a field the Registry versions
    separately.

    An explicit ``description`` is still honoured if one ever lands on the row,
    so adding the field later needs no change here.
    """
    explicit = str(row.get("description") or "").strip()
    if explicit:
        return explicit[:1024]
    name = str(row.get("name") or asset_id).strip() or asset_id
    scope = str(row.get("scope") or "org").strip()
    return f"{name} — {scope}-shared {category} from the MediaWorks harness"[:1024]


class Projector:
    """Projects canonical lifecycle events into one environment's Registry.

    Constructed once per Lambda container and reused across invocations. Every
    AWS client is injectable so the projection tests can drive the whole
    convergence path — duplicate, stale, out-of-order, retry, deprecate —
    without touching AWS.
    """

    def __init__(
        self,
        *,
        config: CapabilityConfig,
        asset_table: Any,
        state: ProjectionState,
        control: Any,
        s3: Any = None,
    ) -> None:
        if not config.registry_id:
            raise ConfigError("projection requires REGISTRY_ID")
        self._config = config
        self._assets = asset_table
        self._state = state
        self._control = control
        self._s3 = s3
        self._registry_id = config.registry_id

    # -- canonical reload ----------------------------------------------

    def load_canonical(self, category: str, asset_id: str, version: int) -> Optional[dict[str, Any]]:
        """Reload one immutable version from the ``byPublishedId`` GSI.

        The GSI is queried rather than the base table because the outbox event
        does not (and must not) carry the owning partition: an org-scoped row
        lives under ``ORG#<orgId>#SHARED``, and putting the org id in the event
        would make the event authoritative about scope — the exact coupling the
        re-check exists to avoid.
        """
        padded = str(version).zfill(VERSION_PAD_WIDTH)
        response = self._assets.query(
            IndexName=GSI1_NAME,
            KeyConditionExpression=(
                "GSI1PK = :pk AND GSI1SK = :sk"
            ),
            ExpressionAttributeValues={
                ":pk": f"PUB#{asset_id}",
                ":sk": f"v{padded}",
            },
            Limit=2,
        )
        for item in response.get("Items", []):
            if item.get("category") == category:
                return item
        return None

    def load_skill_md(self, row: dict[str, Any]) -> Optional[str]:
        """Fetch ``SKILL.md`` for discovery text; ``None`` when unavailable.

        Best-effort by design: the markdown is a search aid, and the package
        itself is referenced by immutable pointer. Failing a projection because
        one object read hiccupped would trade a real capability for a nicety.
        """
        bucket = self._config.artifacts_bucket
        prefix = row.get("s3Prefix") or row.get("s3_prefix")
        if not (self._s3 and bucket and isinstance(prefix, str) and prefix):
            return None
        key = f"{prefix.rstrip('/')}/SKILL.md"
        try:
            body = self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            return body.decode("utf-8", errors="replace")
        except Exception as err:  # noqa: BLE001 — any read failure is non-fatal
            logger.info(
                "skill_md unavailable for projection: key=%s error=%s",
                key,
                type(err).__name__,
            )
            return None

    # -- projection ----------------------------------------------------

    def project(self, event: dict[str, Any]) -> str:
        """Converge one outbox event. Returns a short outcome label.

        Outcomes: ``noop`` (already converged), ``created``, ``updated``,
        ``deprecated`` (became ineligible), ``ineligible`` (never eligible).

        Raises:
            PoisonDescriptorError: The descriptor is unprojectable; the event
                belongs in the DLQ rather than being retried unchanged.
            Exception: Any transient AWS failure, re-raised after the attempt
                is recorded so the event source retries with backoff.
        """
        category = str(event.get("category") or "")
        asset_id = str(event.get("assetId") or "")
        version = int(event.get("version") or 0)
        event_type = str(event.get("eventType") or "")
        occurred_at = str(event.get("occurredAt") or _now_iso())
        revision = str(event.get("canonicalRevision") or "")

        if not (category and asset_id and version > 0):
            raise PoisonDescriptorError(
                "malformed_outbox_event", detail=f"{category}/{asset_id}/v{version}"
            )
        if category not in RECORD_TYPE:
            raise PoisonDescriptorError("unprojectable_category", detail=category)

        row = self.load_canonical(category, asset_id, version)

        # A deprecate event, or any row that is gone / no longer org-scoped,
        # converges to "not discoverable". Deliberately checked against the
        # reloaded row so a stale ScopeChanged event cannot re-widen something
        # that has since been narrowed again.
        if (
            row is None
            or event_type == EVENT_DEPRECATED
            or not is_eligible(row)
        ):
            reason = (
                "canonical_row_missing"
                if row is None
                else "deprecated"
                if event_type == EVENT_DEPRECATED or bool(row.get("deprecated"))
                else f"scope_{row.get('scope')}"
            )
            return self._converge_ineligible(
                category=category,
                asset_id=asset_id,
                version=version,
                occurred_at=occurred_at,
                reason=reason,
                narrowed=event_type == EVENT_SCOPE_CHANGED,
            )

        skill_md = self.load_skill_md(row) if category == "skill" else None
        try:
            descriptor = build_descriptor(
                category,
                row,
                artifacts_bucket=self._config.artifacts_bucket,
                skill_md=skill_md,
            )
        except PoisonDescriptorError as err:
            # Record before re-raising: the DLQ message says *an* event failed,
            # this row says which asset and why, without the payload.
            self._state.record_failure(
                category=category,
                asset_id=asset_id,
                version=version,
                occurred_at=occurred_at,
                error_code=err.reason,
                error_message=err.reason,
            )
            logger.error(
                "poison descriptor: category=%s asset=%s v=%s reason=%s",
                category,
                asset_id,
                version,
                err.reason,
            )
            raise

        digest = descriptor_digest(descriptor)
        record_name = naming.record_name(category, asset_id)
        record_version = naming.record_version(version)

        prior = self._state.get(category, asset_id, version)
        if (
            prior
            and prior.get("descriptorDigest") == digest
            and prior.get("registryStatus") == registry.DISCOVERABLE_STATUS
        ):
            logger.info(
                "projection converged already: %s %s", record_name, record_version
            )
            return "noop"

        try:
            existing = registry.find_record(
                self._control,
                self._registry_id,
                name=record_name,
                record_version=record_version,
            )
            if existing is None:
                response = self._control.create_registry_record(
                    registryId=self._registry_id,
                    name=record_name,
                    displayName=str(row.get("name") or asset_id),
                    description=_record_description(row, category, asset_id),
                    recordType=RECORD_TYPE[category],
                    recordVersion=record_version,
                    descriptors=descriptor,
                )
                record_arn = response["recordArn"]
                # CreateRegistryRecord returns recordArn + status but NOT
                # recordId, while every follow-up call takes recordId.
                record_id = registry.record_id_from_arn(record_arn)
                status = response.get("status", "")
                outcome = "created"
            else:
                record_id = existing["recordId"]
                record_arn = existing["recordArn"]
                # Update takes the SAME logical fields as create in a different
                # structural form — a patch wrapper at every level. See
                # `registry.to_update_patch`.
                response = self._control.update_registry_record(
                    registryId=self._registry_id,
                    recordId=record_id,
                    displayName=registry.to_update_patch(
                        str(row.get("name") or asset_id)
                    ),
                    description=registry.to_update_patch(
                        _record_description(row, category, asset_id)
                    ),
                    descriptors=registry.to_update_patch(descriptor),
                )
                status = response.get("status", existing.get("status", ""))
                outcome = "updated"

            status = self._ensure_approved(record_id, status)
        except PoisonDescriptorError:
            raise
        except Exception as err:  # noqa: BLE001 — classified, recorded, re-raised
            self._state.record_failure(
                category=category,
                asset_id=asset_id,
                version=version,
                occurred_at=occurred_at,
                error_code=type(err).__name__,
                error_message=str(err),
            )
            logger.error(
                "projection attempt failed: %s %s error=%s",
                record_name,
                record_version,
                type(err).__name__,
            )
            raise

        self._state.record_success(
            category=category,
            asset_id=asset_id,
            version=version,
            canonical_revision=revision,
            descriptor_digest=digest,
            registry_id=self._registry_id,
            record_id=record_id,
            record_arn=record_arn,
            registry_status=status,
            occurred_at=occurred_at,
        )
        logger.info(
            "projection %s: %s %s status=%s", outcome, record_name, record_version, status
        )
        return outcome

    def _ensure_approved(self, record_id: str, status: str) -> str:
        """Submit a record so ``APPROVE_ALL`` can approve it.

        Auto-approval is a Registry *policy*, not authorization: canonical
        publish authorization, harness-shared eligibility, descriptor
        validation, and the scope re-check above are the governance gates and
        have all already run by the time this is called (R7).

        A record already ``APPROVED`` needs no submit. ``CREATING`` /
        ``UPDATING`` are in-flight states that a submit would reject, so the
        record is left to settle and the next event (or a reconciliation
        replay) converges it — better than burning the batch's retries on a
        race the service is already resolving.
        """
        if status == registry.DISCOVERABLE_STATUS:
            return status
        if status in _FAILED_STATUSES:
            raise RuntimeError(f"registry record in terminal state {status}")
        if status in _TRANSIENT_STATUSES:
            logger.info(
                "record %s is %s; deferring approval submit", record_id, status
            )
            return status
        response = self._control.submit_registry_record_for_approval(
            registryId=self._registry_id, recordId=record_id
        )
        return response.get("status", status)

    def _converge_ineligible(
        self,
        *,
        category: str,
        asset_id: str,
        version: int,
        occurred_at: str,
        reason: str,
        narrowed: bool,
    ) -> str:
        """Make an ineligible version non-discoverable, without deleting it.

        If a Registry revision exists it is set to ``DEPRECATED``; canonical
        data is never touched. When no record exists this is just a state
        write recording that the asset is deliberately outside the shared
        catalog.
        """
        record_version = naming.record_version(version)
        existing = registry.find_record(
            self._control,
            self._registry_id,
            name=naming.record_name(category, asset_id),
            record_version=record_version,
        )
        registry_status: Optional[str] = None
        outcome = "ineligible"
        if existing and existing.get("status") != registry.DEPRECATED_STATUS:
            response = self._control.update_registry_record_status(
                registryId=self._registry_id,
                recordId=existing["recordId"],
                status=registry.DEPRECATED_STATUS,
                statusReason=(
                    "canonical scope narrowed below organization"
                    if narrowed
                    else f"canonical state no longer eligible ({reason})"
                ),
            )
            registry_status = response.get("status", registry.DEPRECATED_STATUS)
            outcome = "deprecated"
        elif existing:
            registry_status = registry.DEPRECATED_STATUS

        self._state.record_ineligible(
            category=category,
            asset_id=asset_id,
            version=version,
            occurred_at=occurred_at,
            reason=reason,
            registry_status=registry_status,
        )
        logger.info(
            "projection %s: %s/%s v%s reason=%s",
            outcome,
            category,
            asset_id,
            version,
            reason,
        )
        return outcome


# ---------------------------------------------------------------------------
# Stream decoding
# ---------------------------------------------------------------------------


def _deserialize(image: dict[str, Any]) -> dict[str, Any]:
    from boto3.dynamodb.types import TypeDeserializer

    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in image.items()}


def outbox_events(
    stream_event: dict[str, Any], *, environment: str
) -> Iterable[dict[str, Any]]:
    """Yield outbox rows from a DynamoDB stream event.

    Filters on INSERT + ``recordType`` + ``environment``. The event source
    already filters on ``recordType`` server-side; repeating it here means a
    filter regression shows up as "nothing projected" rather than as asset
    rows being fed to :meth:`Projector.project`.
    """
    for record in stream_event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue
        image = record.get("dynamodb", {}).get("NewImage")
        if not image:
            continue
        item = _deserialize(image)
        if item.get("recordType") != OUTBOX_RECORD_TYPE:
            continue
        if item.get("environment") != environment:
            logger.warning(
                "skipping cross-environment outbox event: event_env=%s self=%s",
                item.get("environment"),
                environment,
            )
            continue
        yield item


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------

_PROJECTOR: Optional[Projector] = None


def _build_projector() -> Projector:
    import boto3

    config = load_config()
    if not config.projection_enabled:
        raise ConfigError(
            "REGISTRY_PROJECTION_ENABLED is not true; refusing to project"
        )
    if not config.projection_state_table:
        raise ConfigError("PROJECTION_STATE_TABLE is required for projection")

    ddb = boto3.resource("dynamodb", region_name=config.region)
    return Projector(
        config=config,
        asset_table=ddb.Table(config.asset_index_table),
        state=ProjectionState(
            ddb.Table(config.projection_state_table), environment=config.deploy_env
        ),
        control=registry.control_client(config.region),
        s3=boto3.client("s3", region_name=config.region),
    )


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Lambda handler for the AssetIndex DynamoDB stream.

    Returns a per-outcome tally for the invocation's structured log. Any
    exception propagates so the event source can retry the batch and, once
    retries are exhausted, deliver it to the encrypted DLQ (R7/R15).
    """
    global _PROJECTOR
    if _PROJECTOR is None:
        _PROJECTOR = _build_projector()

    environment = os.environ.get("DEPLOY_ENV", "")
    outcomes: dict[str, int] = {}
    for item in outbox_events(event, environment=environment):
        outcome = _PROJECTOR.project(item)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    logger.info("projection batch complete: %s", outcomes)
    return {"outcomes": outcomes}


__all__ = ["Projector", "handler", "outbox_events"]
