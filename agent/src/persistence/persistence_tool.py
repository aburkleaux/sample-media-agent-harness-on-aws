"""Persistence_Tool — the Strands ``@tool`` confirmation surface (R9, R10.2, R12).

This module is the "user-interaction seam" of the workflow-persistence feature,
mirroring ``agent/src/main_lab_tools.py`` exactly: thin ``@tool async def``
wrappers that marshal typed snake_case kwargs, delegate to a request-bound
:class:`~persistence.persistence_service.PersistenceService`, and return a JSON
string. The per-request service is bound through a ``contextvars.ContextVar``
(``set_current_persistence_service`` / ``reset_current_persistence_service``,
with a ``_service()`` accessor that raises if unbound), exactly as the lab tools
bind their ``LabService`` per turn.

Two tools are exposed:

- ``persist_workflow(session_id)`` runs the read → resolve → assemble → validate
  → save pipeline and returns the JSON :class:`PersistenceReport` (saved
  ``template_id``, capability→``service.action`` mapping, scoped ``conditions``,
  draft state). It **never** activates (R8.3, R9.1, Property 11).
- ``confirm_workflow_activation(template_id, confirm=False)`` is the only path to
  activation and uses the ``_confirm_guard`` idiom from ``main_lab_tools``: unless
  ``confirm is True`` it returns ``{"error": "confirmation_required", ...}``
  *without* calling ``activate_template`` (R9.1, R9.3, R9.4, Property 11); only
  with ``confirm=true`` does it call the service's ``confirm_activation``
  (R9.5).

Requirements: 8.3, 9.1, 9.2, 9.3, 9.4, 9.5, 10.2, 12.1
Properties: 11
"""

from __future__ import annotations

import contextvars
import json
import logging
from typing import Any, Optional

from strands import tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-request PersistenceService binding
# ---------------------------------------------------------------------------
#
# Strands @tool callables are registered once at startup but invoked many times
# across requests. The active PersistenceService for the *current* request is
# stashed in a contextvar so each tool call resolves the correct request-scoped
# collaborators (mirrors main_lab_tools._current_lab_service).

_current_persistence_service: contextvars.ContextVar = contextvars.ContextVar(
    "current_persistence_service", default=None
)


def set_current_persistence_service(persistence_service) -> contextvars.Token:
    """Bind a PersistenceService for the current request. Returns the reset token."""
    return _current_persistence_service.set(persistence_service)


def reset_current_persistence_service(token: contextvars.Token) -> None:
    """Reset the contextvar to its prior value (paired with ``set_current_persistence_service``)."""
    _current_persistence_service.reset(token)


def _service():
    svc = _current_persistence_service.get()
    if svc is None:
        raise RuntimeError(
            "No PersistenceService bound for this request. "
            "Main Agent entrypoint must call set_current_persistence_service "
            "before invoking persistence tools."
        )
    return svc


def _confirm_guard(template_id: str, confirm: bool) -> Optional[dict]:
    """Return an error dict when ``confirm`` is not strictly True (R9.1, R9.3, R9.4).

    Mirrors ``main_lab_tools._confirm_guard``: activation is refused without an
    explicit ``confirm=true``, and the refusal carries no side effect —
    ``activate_template`` is never reached (Property 11).
    """
    if confirm is True:
        return None
    return {
        "error": "confirmation_required",
        "template_id": template_id,
        "message": (
            f"Refusing to activate template={template_id} without explicit "
            "confirmation. Review the proposed mapping and conditions, then pass "
            "confirm=true to proceed."
        ),
    }


# ---------------------------------------------------------------------------
# Persistence tools
# ---------------------------------------------------------------------------


@tool
async def persist_workflow(session_id: str) -> str:
    """Persist a discovered workflow as an inert OMS draft template. Never activates.

    Runs the full read → resolve → assemble → validate → save pipeline for the
    session's persisted plan and returns a JSON PersistenceReport: on success the
    saved ``template_id``, the capability→``service.action`` mapping, the scoped
    ``conditions``, and ``template_state == "draft"``; on failure the
    ``failing_stage`` and itemized ``errors`` (OMS Core left unchanged). This tool
    does NOT activate the template — activation is a separate, explicitly
    confirmed step via ``confirm_workflow_activation``.

    Args:
        session_id: The session whose discovered workflow to perennialize.
    """
    svc = _service()
    try:
        report = await svc.persist(session_id)
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("persist_workflow failed for session %s", session_id)
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})
    return json.dumps(report.to_dict())


@tool
async def confirm_workflow_activation(template_id: str, confirm: bool = False) -> str:
    """Activate a previously persisted draft template. Requires confirm=true.

    Without ``confirm=true`` this returns ``confirmation_required`` and does NOT
    call OMS Core's activate path (R9.1, R9.3, R9.4). Only with ``confirm=true``
    does it activate the previously saved draft ``template_id`` (R9.5).

    Args:
        template_id: The id of the draft template to activate.
        confirm: Must be set to true. Without it the tool refuses and returns
            confirmation_required without any side effect.
    """
    guard = _confirm_guard(template_id, confirm)
    if guard is not None:
        return json.dumps(guard)

    svc = _service()
    try:
        result = await svc.confirm_activation(template_id)
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("confirm_workflow_activation failed for %s", template_id)
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})
    return json.dumps(result.to_dict())


# ---------------------------------------------------------------------------
# Public registry
# ---------------------------------------------------------------------------


PERSISTENCE_TOOLS: list[Any] = [persist_workflow, confirm_workflow_activation]


__all__ = [
    "persist_workflow",
    "confirm_workflow_activation",
    "PERSISTENCE_TOOLS",
    "set_current_persistence_service",
    "reset_current_persistence_service",
]
