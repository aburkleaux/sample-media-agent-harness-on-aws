"""Long-term memory: user PREFERENCE records + recall over AgentCore Memory.

Memory holds two long-term kinds only: user PREFERENCES (``/preferences/{actorId}/``, written
here directly and by the built-in userPreference strategy) and per-session SUMMARIES (written by
the summary strategy). There are no long-term FACTS any more (removed 2026-10): an actor-wide,
never-expiring facts namespace with no project label leaked remembered file locations and media
claims across projects (staging session 07703a35). Files and their content belong to Projects
(project_status / show_asset / per-project search), never to memory, so a preference that names
a file, URL or storage path is refused (:func:`preference_rejection`).

Preferences are written as **direct** memory records (``batch_create_memory_records``) so the
user's wording is stored verbatim. Records land in the SAME namespace the session manager's
``retrieval_config`` reads (see ``security.identity``), so a saved preference is also injected
automatically on later turns — ``recall`` is the explicit deep-lookup path.

Everything here is BEST-EFFORT: each public function catches and logs, returning
a falsy/empty result rather than raising into a tool call. ``metadata`` is intentionally not
used (its typed-structure shape adds failure modes and is not returned by retrieval).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional

import boto3

from security import identity

logger = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def _client(region: Optional[str] = None, client: Optional[Any] = None):
    return client or boto3.client("bedrock-agentcore", region_name=region or REGION)


def _resolve(template: str, actor_id: str, session_id: str = "") -> str:
    # AgentCore requires an exact namespace (no wildcards) — every placeholder
    # must resolve. `summaries` carries {sessionId}; preferences do not.
    return template.format(actorId=actor_id, sessionId=session_id)


# A preference must not carry a file, a file location, a URL or an asset path: those belong to
# the project, and an actor-wide memory of them is exactly what leaked across projects.
_LOCATION_RE = re.compile(r"s3://|https?://|\b(?:artifacts|projects)/", re.I)


def preference_rejection(text: Any) -> str:
    """Why ``text`` cannot be saved as a preference, or ``""`` when it can."""
    if isinstance(text, str) and _LOCATION_RE.search(text):
        return ("Preferences cannot contain URLs (s3://, http://, https://) or artifacts/ or "
                "projects/ paths. Files and file locations live in the project: use "
                "project_status / show_asset to find them. Save only how the user likes things "
                "done (e.g. 'always 16:9', 'answer briefly').")
    return ""


@dataclass(frozen=True)
class LtmWriteResult:
    """Outcome of a long-term-memory write. ``error`` names the failure class
    (e.g. ``AccessDeniedException``) so a caller can tell a permission error from
    a transient one instead of reporting a bare "could not save"."""

    ok: bool
    error: str = ""
    message: str = ""


# Failure classes a retry cannot fix. Reported as such so the model stops retrying.
NON_RETRYABLE_ERRORS = frozenset({
    "AccessDeniedException", "ValidationException", "ResourceNotFoundException",
    "UnauthorizedException", "ParamValidationError", "NoCredentialsError",
})


def error_class(exc: BaseException) -> str:
    """The service error code for a botocore ``ClientError``, else the exception class name."""
    try:
        code = exc.response["Error"]["Code"]  # type: ignore[attr-defined]
        if code:
            return str(code)
    except Exception:  # noqa: BLE001 - not a ClientError
        pass
    return type(exc).__name__


def _record_failures(resp: Any) -> List[str]:
    """``errorCode`` of every failed record in a Batch* response (empty when none)."""
    if not isinstance(resp, dict):
        return []
    return [
        str(f.get("errorCode") or f.get("status") or "FailedRecord")
        for f in (resp.get("failedRecords") or [])
        if isinstance(f, dict)
    ]


def write_preference(
    *,
    memory_id: str,
    actor_id: str,
    text: str,
    region: Optional[str] = None,
    client: Optional[Any] = None,
) -> LtmWriteResult:
    """Write one user preference to ``/preferences/{actorId}/`` and say why it failed, if it did.

    Refuses text naming a file, URL or storage path (:func:`preference_rejection`) with
    ``error="NotAPreference"``. Idempotent per (actor, text) via ``requestIdentifier``.
    """
    body = (text or "").strip() if isinstance(text, str) else ""
    if not memory_id or not actor_id or not body:
        return LtmWriteResult(False, "InvalidInput", "memory id, actor and text are required")
    rejection = preference_rejection(body)
    if rejection:
        return LtmWriteResult(False, "NotAPreference", rejection)
    namespace = _resolve(identity.USER_PREFERENCE_NAMESPACE, actor_id)
    dedup = hashlib.sha256(f"{actor_id}|{namespace}|{body}".encode("utf-8")).hexdigest()[:40]
    try:
        resp = _client(region, client).batch_create_memory_records(
            memoryId=memory_id,
            records=[
                {
                    "requestIdentifier": dedup,
                    "namespaces": [namespace],
                    "content": {"text": body},
                    "timestamp": datetime.now(timezone.utc),
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 - reported, never raised into a tool call
        logger.warning("ltm.write_preference failed (%s)", error_class(exc), exc_info=True)
        return LtmWriteResult(False, error_class(exc), str(exc)[:300])
    failures = _record_failures(resp)
    if failures:
        logger.warning("ltm.write_preference: record rejected (%s)", ", ".join(failures))
        return LtmWriteResult(False, failures[0], "the service rejected the record")
    return LtmWriteResult(True)


def recall(
    *,
    memory_id: str,
    actor_id: str,
    query: str,
    session_id: str = "",
    top_k: int = 8,
    region: Optional[str] = None,
    client: Optional[Any] = None,
) -> List[str]:
    """Semantic-search the actor's preferences and the current session's summary.

    Preferences are actor-scoped (cross-session); the summary namespace is searched only when a
    session is bound. Never searches a facts namespace (there is none). De-duplicates, preserves
    first-seen order. Best-effort: returns [] on any failure.
    """
    if not memory_id or not actor_id or not query:
        return []
    namespaces = [_resolve(identity.USER_PREFERENCE_NAMESPACE, actor_id)]
    if session_id:
        namespaces.append(_resolve(identity.SESSION_SUMMARY_NAMESPACE, actor_id, session_id))
    seen: set[str] = set()
    out: List[str] = []
    try:
        c = _client(region, client)
        for ns in namespaces:
            resp = c.retrieve_memory_records(
                memoryId=memory_id,
                namespace=ns,
                searchCriteria={"searchQuery": query, "topK": top_k},
            )
            for rec in resp.get("memoryRecordSummaries", []) or []:
                content = rec.get("content") if isinstance(rec, dict) else None
                t = (content.get("text") or "").strip() if isinstance(content, dict) else ""
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
    except Exception:
        logger.warning("ltm.recall failed (best-effort)", exc_info=True)
    return out


# --------------------------------------------------------------------------
# forget
# --------------------------------------------------------------------------

# The namespaces `forget` may delete from: the actor-scoped preferences only. Never the
# per-session summaries (conversation compression the actor did not author as a note), and
# there is no facts namespace any more.
_FORGETTABLE_NAMESPACES = (
    identity.USER_PREFERENCE_NAMESPACE,
)
FORGET_MAX_RECORDS = 20
# Same floor the main agent's retrieval injects at (security.identity.build_memory_config), so
# `forget` only removes what could actually have been surfaced for this query.
FORGET_MIN_SCORE = 0.4


@dataclass
class ForgetResult:
    removed: List[dict] = field(default_factory=list)  # [{"id", "text"}]
    error: str = ""
    message: str = ""


def own_forgettable_namespaces(actor_id: str) -> List[str]:
    """The exact namespaces ``actor_id`` may delete from. Built from the actor id ALONE.

    Refuses an actor id that could make one actor's namespace a prefix of another's
    (``a`` vs ``a/b``) or leave a template placeholder unresolved: AgentCore matches a
    retrieval ``namespace`` as a PREFIX, so the namespace shape is what makes deleting another
    actor's record impossible by construction.
    """
    if not isinstance(actor_id, str) or not actor_id.strip():
        return []
    if any(ch in actor_id for ch in "/{}*") or actor_id != actor_id.strip():
        return []
    return [_resolve(t, actor_id) for t in _FORGETTABLE_NAMESPACES]


def _owned_by(record: dict, own: List[str]) -> bool:
    """True when EVERY namespace a record lives in is inside the actor's own namespaces."""
    nss = record.get("namespaces")
    if not isinstance(nss, list) or not nss:
        return False  # fail closed: ownership unprovable
    return all(isinstance(n, str) and any(n.startswith(o) for o in own) for n in nss)


def forget(
    *,
    memory_id: str,
    actor_id: str,
    query: str,
    max_records: int = 3,
    min_score: float = FORGET_MIN_SCORE,
    region: Optional[str] = None,
    client: Optional[Any] = None,
) -> ForgetResult:
    """Delete up to ``max_records`` of the actor's OWN preferences matching ``query``.

    Namespaces come from :func:`own_forgettable_namespaces` (the authenticated actor id only;
    nothing from model input), and each candidate is re-checked against them before deletion,
    so a record of another actor can never be deleted even if the service returned one.
    Returns what was removed. Never raises.
    """
    own = own_forgettable_namespaces(actor_id)
    if not memory_id or not own or not (query or "").strip():
        return ForgetResult(error="InvalidInput",
                            message="memory id, an actor and a query are required")
    try:
        limit = max(1, min(int(max_records), FORGET_MAX_RECORDS))
    except (TypeError, ValueError):
        limit = 3
    try:
        c = _client(region, client)
        candidates: dict[str, dict] = {}
        for ns in own:
            resp = c.retrieve_memory_records(
                memoryId=memory_id,
                namespace=ns,
                searchCriteria={"searchQuery": query, "topK": limit},
            )
            for rec in (resp or {}).get("memoryRecordSummaries", []) or []:
                if not isinstance(rec, dict):
                    continue
                rid = rec.get("memoryRecordId")
                if not isinstance(rid, str) or not rid or rid in candidates:
                    continue
                if not _owned_by(rec, own):
                    logger.warning("ltm.forget: skipped record outside the actor's namespaces")
                    continue
                score = rec.get("score")
                if isinstance(score, (int, float)) and score < min_score:
                    continue
                content = rec.get("content") if isinstance(rec.get("content"), dict) else {}
                candidates[rid] = {
                    "id": rid,
                    "text": (content.get("text") or "").strip(),
                    "score": score if isinstance(score, (int, float)) else 0.0,
                }
        chosen = sorted(candidates.values(), key=lambda r: r["score"], reverse=True)[:limit]
        if not chosen:
            return ForgetResult()
        resp = c.batch_delete_memory_records(
            memoryId=memory_id,
            records=[{"memoryRecordId": r["id"]} for r in chosen],
        )
    except Exception as exc:  # noqa: BLE001 - reported, never raised into a tool call
        logger.warning("ltm.forget failed (%s)", error_class(exc), exc_info=True)
        return ForgetResult(error=error_class(exc), message=str(exc)[:300])
    resp = resp if isinstance(resp, dict) else {}
    ok_ids = {
        s.get("memoryRecordId") for s in (resp.get("successfulRecords") or [])
        if isinstance(s, dict)
    }
    failures = _record_failures(resp)
    removed = [{"id": r["id"], "text": r["text"]} for r in chosen if r["id"] in ok_ids]
    result = ForgetResult(removed=removed)
    if failures:
        result.error = failures[0]
        result.message = f"{len(failures)} record(s) could not be deleted"
    return result
