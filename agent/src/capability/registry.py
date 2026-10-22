"""boto3 clients for the ``agent-registry`` namespace (R9).

Two clients, deliberately separate so IAM can be too (R5, design "AWS roles"):

* :func:`data_client` — ``agent-registry``, the discovery data plane. The
  Capability API's query role holds only the three Discoverable* reads.
* :func:`control_client` — ``agent-registry-control``. The projector's role
  holds record create/update/submit/status on one registry ARN, and the
  provisioning script's caller holds registry creation.

The deprecated Registry surface under ``bedrock-agentcore`` is never used.
``bedrock-agentcore`` identity and runtime resources keep their names — this
module simply does not touch them.

Model loading: see ``botocore_models/README.md``. The Lambda runtime's bundled
botocore predates this namespace, so the vendored service models are pushed
onto ``AWS_DATA_PATH`` before the first client is built. That must happen
before botocore constructs its loader, which is why it runs at import time
rather than inside the factory functions.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

DATA_PLANE_SERVICE: str = "agent-registry"
CONTROL_PLANE_SERVICE: str = "agent-registry-control"

#: Directory holding the vendored ``<service>/<api-version>/service-2.json.gz``
#: trees. Resolved from this file so it works both at ``/var/task/capability``
#: in Lambda and in a source checkout.
VENDORED_MODEL_PATH: Path = Path(__file__).resolve().parent / "botocore_models"

#: Record statuses that mean "discoverable by every authorized consumer".
#: Anything else is pending, rejected, or deprecated and must not be presented
#: as organization-discoverable (R12).
DISCOVERABLE_STATUS: str = "APPROVED"
DEPRECATED_STATUS: str = "DEPRECATED"


def _ensure_model_path() -> None:
    """Prepend the vendored models to ``AWS_DATA_PATH`` (idempotent)."""
    if not VENDORED_MODEL_PATH.is_dir():
        # A source tree without the vendored models is fine wherever the
        # ambient botocore already knows the namespace; only Lambda depends
        # on them, and its asset always carries the directory.
        return
    entry = str(VENDORED_MODEL_PATH)
    existing = os.environ.get("AWS_DATA_PATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if entry in parts:
        return
    os.environ["AWS_DATA_PATH"] = os.pathsep.join([entry, *parts])


_ensure_model_path()


def _client(service: str, region: Optional[str], **kwargs: Any) -> Any:
    import boto3  # imported lazily so ``AWS_DATA_PATH`` is set first

    _ensure_model_path()
    return boto3.client(service, region_name=region, **kwargs)


def data_client(region: Optional[str] = None, **kwargs: Any) -> Any:
    """``agent-registry`` discovery data plane."""
    return _client(DATA_PLANE_SERVICE, region, **kwargs)


def control_client(region: Optional[str] = None, **kwargs: Any) -> Any:
    """``agent-registry-control`` registry/record control plane."""
    return _client(CONTROL_PLANE_SERVICE, region, **kwargs)


def record_id_from_arn(record_arn: str) -> str:
    """Extract a record id from a record ARN.

    ``CreateRegistryRecord`` returns ``recordArn`` and ``status`` but **not**
    ``recordId``, while every follow-up call (``SubmitRegistryRecordForApproval``,
    ``UpdateRegistryRecordStatus``, ``GetRegistryRecord``) takes ``recordId``.
    The id is the ARN's last path segment.

    Raises:
        ValueError: The ARN has no path segment to take an id from.
    """
    if not record_arn or "/" not in record_arn:
        raise ValueError(f"not a record ARN: {record_arn!r}")
    candidate = record_arn.rsplit("/", 1)[1]
    if not candidate:
        raise ValueError(f"record ARN has an empty id segment: {record_arn!r}")
    return candidate


def to_update_patch(value: Any) -> dict[str, Any]:
    """Wrap a create-shaped value in ``UpdateRegistryRecord``'s patch shape.

    ``CreateRegistryRecord`` and ``UpdateRegistryRecord`` take the *same* logical
    fields in structurally different forms. Create takes plain values; update
    takes a leaf patch wrapper at **every** level, documented in the model as
    "Omit the wrapper to leave the field unchanged; supply an empty object to
    unset; supply ``optionalValue`` to set."

    So ``displayName`` is a string on create and ``{"optionalValue": "..."}`` on
    update, and a descriptor nests the wrapper once per level::

        create:  {"custom": {"data": "<payload>"}}
        update:  {"optionalValue":
                    {"custom": {"optionalValue":
                        {"data": {"optionalValue": "<payload>"}}}}}

    The projector passed create-shaped values to update, so botocore rejected
    every update client-side with ``ParamValidationError``. Nothing in the test
    suite caught it because the fake control client accepted any shape, and
    nothing in production reached it: the update branch only runs when a record
    version already exists and its content changed, which never happened while
    record creation itself was broken.

    The recursion is uniform because the model is: a mapping becomes
    ``{"optionalValue": {k: to_update_patch(v)}}`` and a scalar becomes
    ``{"optionalValue": scalar}``. That holds for the descriptor kinds this
    projector emits (``custom``, ``agentSkillsDefinition``, ``mcpServer``)
    including their nested ``additionalData``.
    """
    if isinstance(value, Mapping):
        return {"optionalValue": {k: to_update_patch(v) for k, v in value.items()}}
    return {"optionalValue": value}


def iter_registry_records(
    client: Any, registry_id: str, *, page_size: int = 50
) -> Iterator[dict[str, Any]]:
    """Page every record in a registry (control plane, includes non-approved).

    Used by reconciliation and by the projector's convergence check, both of
    which must see ``DRAFT`` / ``PENDING_APPROVAL`` / ``DEPRECATED`` records —
    the data plane only exposes discoverable ones.
    """
    next_token: Optional[str] = None
    while True:
        kwargs: dict[str, Any] = {"registryId": registry_id, "maxResults": page_size}
        if next_token:
            kwargs["nextToken"] = next_token
        response = client.list_registry_records(**kwargs)
        yield from response.get("registryRecords", [])
        next_token = response.get("nextToken")
        if not next_token:
            return


def find_record(
    client: Any, registry_id: str, *, name: str, record_version: str
) -> Optional[dict[str, Any]]:
    """Locate a record by its deterministic ``(name, recordVersion)`` pair.

    This is what makes projection idempotent without storing a mapping first:
    a replayed event recomputes the same pair and finds the record it already
    created (Property 4). ``ListRegistryRecords`` supports a ``name`` filter,
    but not ``recordVersion``, so the version match is done here.
    """
    next_token: Optional[str] = None
    while True:
        kwargs: dict[str, Any] = {
            "registryId": registry_id,
            "maxResults": 50,
            "filters": [{"name": "name", "values": [name]}],
        }
        if next_token:
            kwargs["nextToken"] = next_token
        response = client.list_registry_records(**kwargs)
        for record in response.get("registryRecords", []):
            if (
                record.get("name") == name
                and record.get("recordVersion") == record_version
            ):
                return record
        next_token = response.get("nextToken")
        if not next_token:
            return None


def iter_discoverable_records(
    client: Any, registry_id: str, *, page_size: int = 50, max_records: int = 2000
) -> Iterator[dict[str, Any]]:
    """Page discoverable records (data plane).

    ``ListDiscoverableRegistryRecords`` returns metadata only — no
    ``descriptors``. Callers that need descriptor bodies follow up with
    ``BatchGetDiscoverableRegistryRecord``, which does return them.

    ``max_records`` is a hard stop so one runaway registry cannot turn a
    2-second category read into an unbounded page walk (R13).
    """
    next_token: Optional[str] = None
    seen = 0
    while True:
        kwargs: dict[str, Any] = {"registryId": registry_id, "maxResults": page_size}
        if next_token:
            kwargs["nextToken"] = next_token
        response = client.list_discoverable_registry_records(**kwargs)
        for record in response.get("registryRecords", []):
            yield record
            seen += 1
            if seen >= max_records:
                logger.warning(
                    "registry listing truncated at %d records (registry=%s)",
                    max_records,
                    registry_id,
                )
                return
        next_token = response.get("nextToken")
        if not next_token:
            return


__all__ = [
    "CONTROL_PLANE_SERVICE",
    "DATA_PLANE_SERVICE",
    "DEPRECATED_STATUS",
    "DISCOVERABLE_STATUS",
    "VENDORED_MODEL_PATH",
    "control_client",
    "data_client",
    "find_record",
    "iter_discoverable_records",
    "iter_registry_records",
    "record_id_from_arn",
    "to_update_patch",
]
