"""Compliance & data-handling for externally-sourced End_User data (task 18.1).

This module is the single home for the three data-protection obligations the
custom-agents design places on the **external** invoke surface (Requirement 18):

* **Req 18.1 — End_User deletion.** :func:`delete_end_user_data` purges an
  End_User's identifiers and conversation data for a given
  ``(agentId, principal)`` across **all four** stores the design enumerates:
  AgentCore Memory (short-term *events* and long-term *records* under the
  segregated ``EXT#`` actor/namespace from Req 10), the *session*, the *logs*,
  and the *traces*. The consistent ``EXT#`` keying from Req 10
  (:mod:`identity`) is what makes the deletion tractable — every store is
  addressed by the same derived key.

* **Req 18.2 — secret hygiene in logs/traces.** :class:`RedactingLogWriter`
  wraps any log/trace sink and runs every record through the **shared**
  redaction filter created in task 13.1 (:func:`redaction.redact_secrets`)
  before it is written, so secrets/tokens/credentials never land in logs or
  traces. This is deliberately the *same* filter used for Req 11.2 external
  output filtering — one redaction implementation, two call sites.

* **Req 18.3 — region/data residency.** :func:`resolve_residency_region` and
  :func:`assert_residency` pin the storage/processing of externally-sourced
  End_User data to the region required for its issuer, so residency-constrained
  data never leaves its region. :func:`pinned_memory_data_plane_client` builds
  an AgentCore Memory data-plane client bound to that region.

The deletion orchestration takes its collaborators (the Memory data-plane
client, the log purger, the trace purger) by dependency injection so the whole
path is unit/example-testable without touching AWS (task 18.2). Every sub-step
is defensively wrapped: one failing store never aborts the others, and the
:class:`DeletionReport` records exactly what was purged and what errored so a
partial failure is auditable and retryable.

_Requirements: 18.1, 18.2, 18.3_
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional

from security import identity
from security.redaction import redact_secrets

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Principal helpers
# ---------------------------------------------------------------------------


def end_user_label(principal: Mapping[str, Any]) -> str:
    """Return the issuer-qualified End_User label ``"<issuer>#<sub>"``.

    This is the stable, issuer-qualified identifier the design uses for the
    External End_User across usage attribution, audit, logs, and traces — it is
    never a bare ``sub`` (which is not globally unique under bring-your-own
    OIDC). Returns ``""`` when neither field is present.
    """
    if not isinstance(principal, Mapping):
        return ""
    issuer = principal.get("issuer") or ""
    sub = principal.get("sub") or ""
    if not issuer and not sub:
        return ""
    return f"{issuer}#{sub}"


# ---------------------------------------------------------------------------
# Req 18.3 — region / data residency pinning
# ---------------------------------------------------------------------------

# A global residency region that ALL externally-sourced End_User data must be
# stored/processed in, when set. Overrides the ambient default region.
_ENV_GLOBAL_RESIDENCY_REGION = "EXT_DATA_RESIDENCY_REGION"

# A JSON object mapping an OIDC ``issuer`` to the region its callers' data must
# reside in, e.g. ``{"https://eu-idp.example.com": "eu-west-1"}``. Takes
# precedence over the global region for a matching issuer, so a multi-issuer
# deployment can honour per-jurisdiction residency.
_ENV_ISSUER_RESIDENCY_MAP = "EXT_RESIDENCY_ISSUER_REGIONS"

# The ambient default region (same env var the rest of the agent uses).
_ENV_DEFAULT_REGION = "AWS_REGION"
_FALLBACK_REGION = "us-west-2"


class ResidencyViolation(RuntimeError):
    """Raised when externally-sourced End_User data would be stored/processed
    outside the region its residency constraint requires (Req 18.3)."""


def _load_issuer_residency_map(raw: Optional[str] = None) -> dict[str, str]:
    """Parse the issuer→region residency map from JSON (env by default).

    Returns an empty map for unset/blank/malformed input — a missing map means
    "no per-issuer residency constraint", which is a valid configuration.
    """
    if raw is None:
        raw = os.environ.get(_ENV_ISSUER_RESIDENCY_MAP, "")
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning(
            "compliance: %s is not valid JSON; ignoring issuer residency map",
            _ENV_ISSUER_RESIDENCY_MAP,
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "compliance: %s must be a JSON object of issuer->region; ignoring",
            _ENV_ISSUER_RESIDENCY_MAP,
        )
        return {}
    # Keep only string->non-empty-string entries.
    return {
        str(k): str(v).strip()
        for k, v in parsed.items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
    }


def default_region() -> str:
    """The ambient default region (``AWS_REGION`` or the fallback)."""
    return os.environ.get(_ENV_DEFAULT_REGION, "").strip() or _FALLBACK_REGION


def resolve_residency_region(
    issuer: Optional[str],
    *,
    default: Optional[str] = None,
    issuer_map: Optional[Mapping[str, str]] = None,
    global_required: Optional[str] = None,
) -> str:
    """Resolve the region externally-sourced data for ``issuer`` must reside in.

    Resolution order (most specific first):

    1. A per-issuer residency region (``issuer_map`` / ``EXT_RESIDENCY_ISSUER_REGIONS``).
       This is the jurisdiction-specific constraint and always wins for a match.
    2. A global residency region (``global_required`` / ``EXT_DATA_RESIDENCY_REGION``)
       that applies to all external data when no per-issuer entry matches.
    3. The ambient default region (``default`` / ``AWS_REGION``) — used when no
       residency constraint applies, i.e. the data may live in the default region.

    The result is where storage/processing of that issuer's End_User data is
    *pinned*; :func:`assert_residency` enforces it against a client's actual
    region.
    """
    if issuer_map is None:
        issuer_map = _load_issuer_residency_map()
    if global_required is None:
        global_required = os.environ.get(_ENV_GLOBAL_RESIDENCY_REGION, "").strip() or None
    resolved_default = (default or "").strip() or default_region()

    if issuer and issuer in issuer_map:
        return issuer_map[issuer]
    if global_required:
        return global_required
    return resolved_default


def residency_constraint_applies(
    issuer: Optional[str],
    *,
    issuer_map: Optional[Mapping[str, str]] = None,
    global_required: Optional[str] = None,
) -> bool:
    """Whether an explicit residency constraint applies to ``issuer``'s data.

    ``True`` when a per-issuer entry exists or a global residency region is
    configured; ``False`` when data may simply live in the ambient default
    region (the "WHERE ... constraints apply" guard of Req 18.3).
    """
    if issuer_map is None:
        issuer_map = _load_issuer_residency_map()
    if global_required is None:
        global_required = os.environ.get(_ENV_GLOBAL_RESIDENCY_REGION, "").strip() or None
    return bool((issuer and issuer in issuer_map) or global_required)


def assert_residency(active_region: str, required_region: str, *, issuer: str = "") -> None:
    """Assert ``active_region`` matches the residency-``required_region``.

    Call this before storing/processing externally-sourced End_User data on a
    region-bound client. Raises :class:`ResidencyViolation` on mismatch so a
    misconfiguration fails closed rather than silently placing data in the
    wrong region (Req 18.3).
    """
    if not required_region:
        return
    if active_region != required_region:
        raise ResidencyViolation(
            f"externally-sourced End_User data for issuer={issuer!r} must be "
            f"stored/processed in region {required_region!r} but the active "
            f"region is {active_region!r}"
        )


def pinned_memory_data_plane_client(region: str, *, boto3_module=None):
    """Build an AgentCore Memory data-plane client pinned to ``region``.

    Centralizes construction so every external memory read/write/delete is
    bound to the residency region resolved for the caller (Req 18.3). ``boto3``
    is imported lazily (and is injectable for tests) so importing this module
    never requires AWS libraries.
    """
    if boto3_module is None:
        import boto3 as boto3_module  # local import: avoid hard dep at import time
    return boto3_module.client("bedrock-agentcore", region_name=region)


# ---------------------------------------------------------------------------
# Req 18.2 — secret redaction in the log/trace writer
# ---------------------------------------------------------------------------


def redact_record(value: Any, *, known_secrets: Optional[Iterable[str]] = None) -> Any:
    """Recursively redact secrets from a log/trace record before it is written.

    Strings are passed through the shared :func:`redaction.redact_secrets`
    filter; dicts/lists/tuples are walked so structured records (the common
    shape of a trace span or a JSON log line) are scrubbed everywhere a secret
    could hide, including nested values and dict *values* (keys are preserved so
    the record stays greppable). Non-string scalars are returned unchanged.

    ``known_secrets`` is materialized once so an exhaustible iterable still
    applies to every nested string.
    """
    secrets = list(known_secrets) if known_secrets is not None else None
    return _redact_record(value, secrets)


def _redact_record(value: Any, secrets: Optional[list[str]]) -> Any:
    if isinstance(value, str):
        return redact_secrets(value, known_secrets=secrets)
    if isinstance(value, Mapping):
        return {k: _redact_record(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        redacted = [_redact_record(v, secrets) for v in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted
    return value


class RedactingLogWriter:
    """A log/trace writer that strips secrets/tokens/credentials (Req 18.2).

    Wraps an underlying ``sink`` callable (e.g. ``logger.info``, a trace-span
    emitter, or an audit writer). Every message/record is run through the
    shared redaction filter *before* it reaches the sink, so secrets never land
    in logs or traces — the same guarantee the external output filter gives for
    caller-visible output (Req 11.2), using the same implementation.

    The writer is intentionally sink-agnostic and never raises from the redact
    step: redaction is pure and side-effect free, and a sink failure is the
    caller's concern, not this wrapper's.
    """

    def __init__(
        self,
        sink: Callable[[Any], Any],
        *,
        known_secrets: Optional[Iterable[str]] = None,
    ) -> None:
        self._sink = sink
        # Materialize so a one-shot iterable survives repeated writes.
        self._known_secrets = list(known_secrets) if known_secrets is not None else []

    def write(self, record: Any, *, extra_secrets: Optional[Iterable[str]] = None) -> Any:
        """Redact ``record`` then forward it to the wrapped sink.

        ``extra_secrets`` are merged with the writer's configured known secrets
        for this write only (e.g. a per-request Owner token the runtime just
        resolved).
        """
        secrets = list(self._known_secrets)
        if extra_secrets is not None:
            secrets.extend(extra_secrets)
        scrubbed = redact_record(record, known_secrets=secrets or None)
        return self._sink(scrubbed)

    # Convenience so the writer can stand in for a plain callable.
    __call__ = write


# ---------------------------------------------------------------------------
# Req 18.1 — End_User deletion path
# ---------------------------------------------------------------------------


@dataclass
class DeletionReport:
    """Structured, auditable result of an End_User deletion (Req 18.1).

    Records what was purged from each store so the deletion is verifiable and a
    partial failure is retryable. ``ok`` is ``True`` only when every store
    completed without error.
    """

    agent_id: str
    end_user: str
    external_key: str
    region: str
    deleted_events: int = 0
    deleted_records: int = 0
    purged_sessions: int = 0
    logs_purged: bool = False
    traces_purged: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "end_user": self.end_user,
            "external_key": self.external_key,
            "region": self.region,
            "deleted_events": self.deleted_events,
            "deleted_records": self.deleted_records,
            "purged_sessions": self.purged_sessions,
            "logs_purged": self.logs_purged,
            "traces_purged": self.traces_purged,
            "errors": list(self.errors),
            "ok": self.ok,
        }


# A purger callable receives the End_User label and the derived EXT# key and
# removes that End_User's entries from a store (logs or traces). It returns
# nothing; raising is caught and recorded on the report.
Purger = Callable[..., Any]


def _iter_events(memory_client, memory_id: str, actor_id: str, session_id: str):
    """Yield every short-term event under ``(actor_id, session_id)``.

    Manually paginates via ``nextToken`` so a simple fake client (dict-returning
    ``list_events``) works in tests without a boto3 paginator.
    """
    next_token = None
    while True:
        params = {
            "memoryId": memory_id,
            "actorId": actor_id,
            "sessionId": session_id,
            "maxResults": 100,
        }
        if next_token:
            params["nextToken"] = next_token
        resp = memory_client.list_events(**params) or {}
        for event in resp.get("events", []) or []:
            yield event
        next_token = resp.get("nextToken")
        if not next_token:
            break


def _iter_memory_records(memory_client, memory_id: str, namespace: str):
    """Yield every long-term memory record under ``namespace`` (paginated)."""
    next_token = None
    while True:
        params = {"memoryId": memory_id, "namespace": namespace, "maxResults": 100}
        if next_token:
            params["nextToken"] = next_token
        resp = memory_client.list_memory_records(**params) or {}
        # The API returns summaries under either key depending on version.
        records = resp.get("memoryRecordSummaries") or resp.get("memoryRecords") or []
        for record in records:
            yield record
        next_token = resp.get("nextToken")
        if not next_token:
            break


def _actor_session_ids(memory_client, memory_id: str, actor_id: str, derived_session_id: str) -> list[str]:
    """All session ids to purge for ``actor_id``.

    Always includes the derived key's own session id; additionally enumerates
    any other sessions the actor has via ``list_sessions`` so a caller whose
    conversation spanned multiple sessions is fully purged. Falls back to just
    the derived session id when the client can't list sessions.
    """
    sessions = {derived_session_id}
    lister = getattr(memory_client, "list_sessions", None)
    if callable(lister):
        try:
            next_token = None
            while True:
                params = {"memoryId": memory_id, "actorId": actor_id, "maxResults": 100}
                if next_token:
                    params["nextToken"] = next_token
                resp = lister(**params) or {}
                for summary in resp.get("sessionSummaries", []) or []:
                    sid = summary.get("sessionId")
                    if sid:
                        sessions.add(sid)
                next_token = resp.get("nextToken")
                if not next_token:
                    break
        except Exception as exc:  # noqa: BLE001 — session enumeration is best-effort
            logger.warning("compliance: list_sessions failed for actor purge: %s", exc)
    return sorted(sessions)


def _purge_memory(memory_client, memory_id: str, key: identity.MemoryKey, report: DeletionReport) -> None:
    """Purge AgentCore Memory short-term events + long-term records for ``key``.

    Session state (short-term events) and long-term records are both keyed by
    the single derived ``EXT#`` value, so deleting under that actor/session and
    namespace removes the End_User's conversation data in full (Req 18.1).
    """
    actor_id = key.actor_id
    namespace = key.namespace or key.actor_id

    # Short-term events across every session belonging to this actor (covers
    # the "session" store in the deletion set).
    for session_id in _actor_session_ids(memory_client, memory_id, actor_id, key.session_id):
        session_had_delete = False
        try:
            for event in _iter_events(memory_client, memory_id, actor_id, session_id):
                event_id = event.get("eventId") if isinstance(event, Mapping) else None
                if not event_id:
                    continue
                try:
                    memory_client.delete_event(
                        memoryId=memory_id,
                        actorId=actor_id,
                        sessionId=session_id,
                        eventId=event_id,
                    )
                    report.deleted_events += 1
                    session_had_delete = True
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"delete_event({event_id}): {exc}")
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"list_events(session={session_id}): {exc}")
        if session_had_delete:
            report.purged_sessions += 1

    # Long-term records under the caller's namespace.
    try:
        for record in _iter_memory_records(memory_client, memory_id, namespace):
            record_id = (
                record.get("memoryRecordId") or record.get("recordId")
                if isinstance(record, Mapping)
                else None
            )
            if not record_id:
                continue
            try:
                memory_client.delete_memory_record(
                    memoryId=memory_id, memoryRecordId=record_id
                )
                report.deleted_records += 1
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"delete_memory_record({record_id}): {exc}")
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"list_memory_records(namespace={namespace}): {exc}")


def delete_end_user_data(
    *,
    memory_id: str,
    agent_id: str,
    principal: Mapping[str, Any],
    memory_client: Any = None,
    region: Optional[str] = None,
    log_purger: Optional[Purger] = None,
    trace_purger: Optional[Purger] = None,
    boto3_module=None,
) -> DeletionReport:
    """Purge an End_User's identifiers & conversation data (Req 18.1).

    Deletes, for the ``(agentId, principal)`` End_User, across all four stores
    the design enumerates:

    * **Memory** — long-term records under the caller's ``EXT#`` namespace.
    * **Session** — short-term events across every session of the ``EXT#`` actor.
    * **Logs** — via the injected ``log_purger`` (called with the End_User label
      and derived key).
    * **Traces** — via the injected ``trace_purger``.

    The External End_User is the issuer-qualified Principal ``(issuer, sub)``;
    the durable-memory address is the segregated ``EXT#`` key derived from
    ``(agentId, issuer, sub)`` (:func:`identity.derive_external_memory_key`), so
    the deletion targets exactly that caller-on-that-agent and can never purge
    another caller's, issuer's, or agent's data.

    Storage/processing is pinned to the residency region resolved for the
    caller's issuer (Req 18.3): when no ``region`` and no ``memory_client`` are
    supplied, a data-plane client is built bound to that region.

    Args:
        memory_id: The AgentCore Memory store id.
        agent_id: The Custom Agent id being purged.
        principal: The External End_User ``{"issuer", "sub"}``.
        memory_client: An AgentCore Memory data-plane client (injected for
            tests). When ``None`` one is constructed pinned to the residency
            region.
        region: Override for the residency region. When ``None`` it is resolved
            from the issuer's residency configuration.
        log_purger: Callable purging the End_User's log entries. Optional.
        trace_purger: Callable purging the End_User's traces. Optional.
        boto3_module: Injectable boto3 (tests); lazily imported otherwise.

    Returns:
        A :class:`DeletionReport` describing what was purged and any per-store
        errors. ``report.ok`` is ``True`` only when every store succeeded.

    Raises:
        ValueError: If the principal is under-qualified (missing issuer/sub) or
            ``agent_id``/``memory_id`` is empty — deriving an under-qualified
            key would risk purging the wrong (or no) data.
    """
    issuer = (principal or {}).get("issuer") if isinstance(principal, Mapping) else None
    sub = (principal or {}).get("sub") if isinstance(principal, Mapping) else None
    if not (isinstance(agent_id, str) and agent_id.strip()):
        raise ValueError("delete_end_user_data: agent_id must be a non-empty string")
    if not (isinstance(memory_id, str) and memory_id.strip()):
        raise ValueError("delete_end_user_data: memory_id must be a non-empty string")
    if not (isinstance(issuer, str) and issuer.strip()):
        raise ValueError("delete_end_user_data: principal.issuer must be a non-empty string")
    if not (isinstance(sub, str) and sub.strip()):
        raise ValueError("delete_end_user_data: principal.sub must be a non-empty string")

    key = identity.derive_external_memory_key(agent_id, issuer, sub)
    resolved_region = (region or "").strip() or resolve_residency_region(issuer)
    label = end_user_label(principal)

    report = DeletionReport(
        agent_id=agent_id,
        end_user=label,
        external_key=key.actor_id,
        region=resolved_region,
    )

    # Build a region-pinned client when none is injected (Req 18.3): the delete
    # touches externally-sourced data, so it must run in the residency region.
    if memory_client is None:
        try:
            memory_client = pinned_memory_data_plane_client(
                resolved_region, boto3_module=boto3_module
            )
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"memory_client_init({resolved_region}): {exc}")
            return report

    # 1 & 2) Memory records + session events.
    _purge_memory(memory_client, memory_id, key, report)

    # 3) Logs.
    if log_purger is not None:
        try:
            log_purger(end_user=label, key=key, agent_id=agent_id, region=resolved_region)
            report.logs_purged = True
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"log_purger: {exc}")

    # 4) Traces.
    if trace_purger is not None:
        try:
            trace_purger(end_user=label, key=key, agent_id=agent_id, region=resolved_region)
            report.traces_purged = True
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"trace_purger: {exc}")

    logger.info(
        "compliance: End_User deletion agent_id=%s end_user=%s region=%s "
        "events=%d records=%d sessions=%d logs=%s traces=%s ok=%s",
        agent_id, label, resolved_region, report.deleted_events,
        report.deleted_records, report.purged_sessions, report.logs_purged,
        report.traces_purged, report.ok,
    )
    return report
