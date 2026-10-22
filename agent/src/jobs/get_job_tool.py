"""Built-in ``get_job`` Strands tool — actor-scoped job status lookup.

Exposes a thin wrapper around :meth:`JobRegistry.get` so the orchestrator
can answer the user's "what's the status of job X" question without
invoking the trio's ``*_results`` tool. Per R10.1/R10.2/R10.3, the
results tool MUST only be called when the user explicitly asks for the
results AND the job has reached the ``SUCCEEDED`` terminal state — this
tool is the cheap status-only path the orchestrator uses to make that
decision.

The registry is wired by ``main.py`` at startup via :func:`set_registry`
once the ``JOB_REGISTRY_TABLE`` environment variable is resolved. Until
then, calls to :func:`get_job` return a ``registry_unavailable`` error
JSON so the agent fails loudly instead of pretending the job doesn't
exist — that distinction matters because the cross-actor and
not-found cases are deliberately collapsed to a single ``not_found``
response (R11.2), but a missing registry is a configuration problem
that should be surfaced as such.

Actor scoping (R11.2): :meth:`JobRegistry.get` enforces equality with
the caller's bound actor id and returns ``None`` for cross-actor
lookups. This tool maps both "missing record" and "wrong actor" to the
same ``not_found`` JSON shape so the agent (and ultimately the user)
can't infer whether another actor owns the job.

Requirements: 10.1, 10.2, 10.3, 11.2
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from strands import tool

from . import context as jobs_context
from .registry import JobRegistry

logger = logging.getLogger(__name__)

# Module-level singleton wired by ``main.py`` once the JobRegistry
# is constructed. Stays ``None`` in dev environments without
# ``JOB_REGISTRY_TABLE``; :func:`get_job` returns a
# ``registry_unavailable`` error in that case so the misconfiguration
# surfaces immediately rather than masquerading as a missing record.
_registry: Optional[JobRegistry] = None


def set_registry(registry: Optional[JobRegistry]) -> None:
    """Bind the JobRegistry singleton used by :func:`get_job`.

    Called by ``main.py`` during startup once the registry is built.
    Idempotent — safe to call multiple times (e.g. test harnesses
    rebinding to a stub registry, or a deferred re-init after a
    transient construction failure). Passing ``None`` explicitly
    resets the binding back to "unwired".
    """
    global _registry
    _registry = registry


@tool
def get_job(job_id: str) -> str:
    """Look up the status of an async job you previously started.

    Use this to check progress on a long-running job WITHOUT fetching
    its results. The orchestrator should call ``get_job`` whenever the
    user asks about a specific job, and should only invoke the trio's
    ``*_results`` tool when the user explicitly asks for the results
    AND the status returned here is ``SUCCEEDED``.

    Args:
        job_id: The job identifier returned by the original
            ``start_*`` tool call. Must be a non-empty string.

    Returns:
        A JSON string. On success, an object with the job's
        ``status`` plus ``job_id``, ``trio_id``, ``gateway_id``,
        ``status_tool``, ``results_tool``, ``start_at``,
        ``updated_at``, ``terminal_at``, ``last_error``, and
        ``consecutive_errors`` (snake_case throughout for the
        agent's consumption). On failure, an object with an
        ``error`` code (``invalid_job_id``, ``registry_unavailable``,
        ``no_actor``, or ``not_found``) and a human-readable
        ``message``. Cross-actor lookups are intentionally
        indistinguishable from missing records (R11.2).
    """
    if not isinstance(job_id, str) or not job_id.strip():
        return json.dumps(
            {
                "error": "invalid_job_id",
                "message": "job_id must be a non-empty string.",
            }
        )

    if _registry is None:
        return json.dumps(
            {
                "error": "registry_unavailable",
                "message": (
                    "Job registry is not configured for this runtime; "
                    "async job tracking is disabled."
                ),
            }
        )

    try:
        actor_id = jobs_context.current_actor_id()
    except RuntimeError as exc:
        # The interceptor and tool dispatch path bind the actor on
        # every request; an unbound actor here means the tool ran
        # outside a request context (e.g. a unit test that imported
        # the function directly). Surface a structured error rather
        # than letting RuntimeError propagate up through the agent.
        logger.warning("get_job: actor context unset: %s", exc)
        return json.dumps(
            {
                "error": "no_actor",
                "message": (
                    "No actor context bound for the current request; "
                    "cannot resolve job ownership."
                ),
            }
        )

    record = _registry.get(job_id.strip(), actor_id=actor_id)
    if record is None:
        # R11.2: cross-actor and missing collapse to the same shape
        # so the existence of another actor's job is never leaked.
        return json.dumps(
            {
                "error": "not_found",
                "message": "Job not found or you don't have access to it.",
            }
        )

    return json.dumps(
        {
            "job_id": record.job_id,
            "status": record.status,
            "trio_id": record.trio_id,
            "gateway_id": record.gateway_id,
            "status_tool": record.status_tool,
            "results_tool": record.results_tool,
            "start_at": record.start_at,
            "updated_at": record.updated_at,
            "terminal_at": record.terminal_at,
            "last_error": record.last_error,
            "consecutive_errors": record.consecutive_errors,
            "has_renderable_artifacts": record.has_renderable_artifacts,
        }
    )


__all__ = ["get_job", "set_registry"]
