"""Save / Activate Driver — the persistence pipeline's single write seam (R6, R8, R9.5).

This module owns *every* OMS Core **write** the `Persistence_Tool` performs. It
is deliberately the only place `save_template` and `activate_template` are ever
called, so the safety invariants around atomicity and activation live in one
narrow, auditable component:

- ``save(template)`` persists the assembled template as an **inert draft**
  (R8.1) via a create-or-update ("upsert") over ``save_template``. It reports the
  saved ``template_id`` and whether the operation created or updated (R6.2, R6.3,
  R8.2). It **never** calls ``activate_template`` (R8.3): saving can never
  activate.
- ``activate(template_id)`` is the *only* method that calls ``activate_template``
  (R9.5). It is reachable solely through the human-confirmation surface's
  ``confirm=true`` guard, never from the save flow.

### Create-or-update strategy (no lookup available)

The injected :class:`~persistence.oms_mcp_client.OMSMcpClient` exposes exactly
six OMS MCP tools and has **no** ``get_template`` / ``list_templates`` — so
existence cannot be probed before writing. The driver therefore uses a
**create-then-fallback-to-update** strategy that is safe (never produces a
duplicate) and deterministic (a pure function of the OMS response):

1. Attempt a create: ``save_template(payload, update=False)``.
2. Interpret the response. If OMS Core signals the derived ``template_id``
   already exists (a conflict / "already exists" envelope — see
   :func:`_indicates_already_exists`), transparently re-issue the write as an
   update: ``save_template(payload, update=True)`` and report ``"updated"``.
3. Otherwise the create succeeded and the driver reports ``"created"``.

Because ``template_id`` is a deterministic hash of the workflow content
(assembler ``derive_template_id``), re-persisting an *equivalent* workflow lands
on the same id, hits the create-conflict branch, and routes to an update rather
than creating a duplicate (R6.2, R6.3, Property 10). Genuine transport failures
raised by the client (``OMSUnavailableError``) are *not* swallowed: they
propagate so a failed request leaves OMS Core untouched (Property 5).

### Conflict detection vs the MCP structured-error chokepoint

`OMSMcpClient._call` now raises :class:`~persistence.models.OMSToolError` for
**any** OMS structured error envelope (``{"error": true, "kind": ..., "message":
...}``), so a refused write can never be silently read as a success. A create
conflict arrives through exactly that channel, so the driver **catches**
``OMSToolError`` around the create and classifies it with the same
:data:`_ALREADY_EXISTS_MARKERS` used for dict envelopes
(:func:`_error_indicates_already_exists`):

- an already-exists / conflict / duplicate / 409 signal → transparently re-issue
  the write as an update and report ``"updated"``;
- anything else (notably ``kind="read_only"``, the hosted endpoint refusing all
  mutating tools because it runs with ``OMS_MCP_READ_ONLY=true``) → re-raised
  unchanged, so the pipeline reports ``failed`` at the ``save`` stage.

The dict-shaped conflict signals (``{"conflict": true}``, ``{"created": false}``,
a string ``error`` naming a conflict) remain supported for clients that return
envelopes without raising, so both paths route consistently to an update.
Finally, a save is only reported as successful when the response is credible
evidence of a write (:func:`_assert_usable_save_response`): an empty / non-dict
response with no saved id and no acknowledgement raises rather than fabricating a
success from the id we submitted.

The ``OMSMcpClient`` is injected via the constructor (never constructed inline)
so the MCP write boundary is substitutable by a recording fake in tests, exactly
as the Validator Gate injects it for reads.

Requirements: 6.2, 6.3, 8.1, 8.2, 8.3, 9.5
Properties: 5, 10, 11
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from .models import OMSTemplate, OMSToolError, PersistenceOperation

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .oms_mcp_client import OMSMcpClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private constants
# ---------------------------------------------------------------------------

#: Response keys that may carry the saved ``template_id`` back from OMS Core,
#: tried in order. The derived id on the submitted template is the fallback so a
#: terse OMS response never loses the id we just wrote.
_TEMPLATE_ID_KEYS: tuple[str, ...] = ("template_id", "templateId", "id")

#: The pipeline stage the write belongs to, used when the driver itself raises.
_STAGE_SAVE: str = "save"

#: Response keys under which OMS Core may carry a human/machine error message on
#: a rejected create (``save_template(update=False)`` against an existing id).
_ERROR_KEYS: tuple[str, ...] = ("error", "message", "detail", "reason")

#: Substrings that, appearing in an error message, mark a create rejection as an
#: "already exists" conflict — the signal to fall back to an update. Matched
#: case-insensitively so envelope wording differences still route correctly.
_ALREADY_EXISTS_MARKERS: tuple[str, ...] = (
    "already exist",
    "already-exist",
    "alreadyexists",
    "exists",
    "conflict",
    "duplicate",
    "409",
)

#: Explicit boolean/flag keys that, when truthy, signal a create conflict without
#: a free-text message (e.g. ``{"conflict": true}`` / ``{"exists": true}``).
_CONFLICT_FLAG_KEYS: tuple[str, ...] = ("conflict", "exists", "already_exists")

#: Response keys whose presence acknowledges that OMS Core actually accepted the
#: write, used when no ``template_id`` came back. A response carrying neither an
#: id nor one of these is not evidence of a successful save, so the driver raises
#: rather than fabricating a success from the id it submitted.
_SAVE_ACK_KEYS: tuple[str, ...] = (
    "created",
    "updated",
    "operation",
    "status",
    "template",
    "saved",
    "ok",
    "state",
)


# ---------------------------------------------------------------------------
# Result value objects (local, lightweight — kept out of models.py by design)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SaveResult:
    """Outcome of an inert-draft save (create-or-update) over ``save_template``.

    ``operation`` is ``"created"`` or ``"updated"`` (R8.2); ``created`` /
    ``updated`` are its boolean projections for convenient assertions. ``raw`` is
    the decoded OMS response (of the write that ultimately succeeded) for
    surfacing/debugging. The saved template is always an inert draft (R8.1) — this
    result never implies activation.
    """

    template_id: str
    operation: PersistenceOperation
    raw: dict = field(default_factory=dict)

    @property
    def created(self) -> bool:
        return self.operation == "created"

    @property
    def updated(self) -> bool:
        return self.operation == "updated"

    def to_dict(self) -> dict:
        return {
            "template_id": self.template_id,
            "operation": self.operation,
            "created": self.created,
            "updated": self.updated,
        }


@dataclass(frozen=True)
class ActivateResult:
    """Outcome of an ``activate_template`` call (reachable only via confirmation).

    ``raw`` is the decoded OMS response. Producing this result is the *only* way
    the pipeline signals a template was activated (R9.5).
    """

    template_id: str
    activated: bool = True
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "template_id": self.template_id,
            "activated": self.activated,
        }


# ---------------------------------------------------------------------------
# Pure response interpretation helpers
# ---------------------------------------------------------------------------


def _extract_template_id(response: Any, fallback: str) -> str:
    """Return the saved ``template_id`` from an OMS response, else ``fallback``.

    Tries the common id keys in order (:data:`_TEMPLATE_ID_KEYS`); when the
    response carries none (or is not a dict), returns the derived id we submitted
    so the caller always reports the id it just wrote.
    """
    if isinstance(response, dict):
        for key in _TEMPLATE_ID_KEYS:
            value = response.get(key)
            if isinstance(value, str) and value:
                return value
    return fallback


def _indicates_already_exists(response: Any) -> bool:
    """True when a create response signals the ``template_id`` already exists.

    Interpreted defensively (mirroring the Validator Gate's envelope handling) so
    a slightly different OMS wording still routes to an update rather than being
    mistaken for a fresh create:

    - an explicit truthy conflict flag (:data:`_CONFLICT_FLAG_KEYS`), or
    - an error/message/kind field whose text contains an "already exists" marker
      (:data:`_ALREADY_EXISTS_MARKERS`), or
    - an explicit ``created`` flag that is present and falsy.

    Robust to a non-string truthy ``error`` value: OMS Core's structured envelope
    sets ``error`` to the BOOLEAN ``true`` and carries the detail in ``kind`` /
    ``message`` (``{"error": true, "kind": "conflict", "message": "..."}``). Only
    inspecting string values — as this helper originally did — skipped that
    envelope entirely, which is the root cause of the silent false success this
    module now guards against. Non-string error values therefore route the
    marker scan onto the sibling detail fields instead of being ignored.

    A non-dict response, or one with no conflict signal, is treated as a
    successful create.
    """
    if not isinstance(response, dict):
        return False

    for key in _CONFLICT_FLAG_KEYS:
        if bool(response.get(key)):
            return True

    if "created" in response and not bool(response.get("created")):
        return True

    return _markers_in(_conflict_texts(response))


def _conflict_texts(response: dict) -> list[str]:
    """Collect every free-text field that may describe why a create was rejected.

    Includes the string values of :data:`_ERROR_KEYS` plus the envelope's ``kind``
    discriminator, so a boolean ``error: true`` envelope is still classified from
    its ``kind`` / ``message``.
    """
    texts: list[str] = []
    for key in (*_ERROR_KEYS, "kind"):
        value = response.get(key)
        if isinstance(value, str) and value:
            texts.append(value)
    return texts


def _markers_in(texts: list[str]) -> bool:
    """True when any text contains an "already exists" marker (case-insensitive)."""
    for text in texts:
        lowered = text.lower()
        if any(marker in lowered for marker in _ALREADY_EXISTS_MARKERS):
            return True
    return False


def _error_indicates_already_exists(exc: OMSToolError) -> bool:
    """True when an :class:`OMSToolError` from a create is an already-exists conflict.

    The MCP client's chokepoint raises ``OMSToolError`` for *any* structured OMS
    error envelope, including the conflict envelope a create legitimately gets
    when the deterministic ``template_id`` already exists. This classifier reuses
    the same :data:`_ALREADY_EXISTS_MARKERS` against the error's ``kind`` and
    ``message`` so the create-then-update strategy keeps working, while every
    other refusal (notably ``kind="read_only"``) stays fatal.
    """
    return _markers_in([t for t in (exc.kind, exc.message) if isinstance(t, str) and t])


def _assert_usable_save_response(response: Any, tool_stage: str, submitted_id: str) -> None:
    """Raise unless ``response`` is credible evidence that OMS Core saved the template.

    Defense in depth behind the MCP chokepoint: a response that is not a dict, is
    an empty dict, or carries neither a saved id (:data:`_TEMPLATE_ID_KEYS`) nor
    any acknowledgement key (:data:`_SAVE_ACK_KEYS`) is *not* proof of a write. In
    that case :func:`_extract_template_id` would silently fall back to the id we
    submitted and the caller would report a success that may never have happened
    — exactly the failure mode this module now refuses.
    """
    if isinstance(response, dict) and response:
        has_id = any(
            isinstance(response.get(key), str) and response.get(key)
            for key in _TEMPLATE_ID_KEYS
        )
        if has_id or any(key in response for key in _SAVE_ACK_KEYS):
            return
    raise OMSToolError(
        tool="save_template",
        kind="unusable_response",
        message=(
            f"save_template returned no saved template id and no acknowledgement "
            f"for {submitted_id!r} (response={response!r}); refusing to report a "
            f"save that cannot be confirmed"
        ),
        stage=tool_stage,
    )


# ---------------------------------------------------------------------------
# Save / Activate Driver
# ---------------------------------------------------------------------------


class SaveActivateDriver:
    """Owns the persistence pipeline's OMS write calls (R6.2/R6.3, R8, R9.5).

    Constructor-injected with the :class:`~persistence.oms_mcp_client.OMSMcpClient`
    (real or a recording fake) so the write boundary is substitutable in tests.
    The save flow can never activate (R8.3); ``activate_template`` is reached only
    through :meth:`activate`, which the confirmation surface guards behind
    ``confirm=true`` (R9.5).
    """

    def __init__(self, oms_client: "OMSMcpClient") -> None:
        self._oms_client = oms_client

    # -- save: inert-draft create-or-update (R6.2, R6.3, R8.1, R8.2, R8.3) ---

    def save(self, template: OMSTemplate) -> SaveResult:
        """Persist ``template`` as an inert draft, creating or updating as needed.

        Attempts a create (``save_template(update=False)``); if OMS Core reports
        the derived ``template_id`` already exists, transparently re-issues the
        write as an update (``save_template(update=True)``) so an equivalent
        re-persist upserts rather than duplicating (R6.2, R6.3, Property 10). The
        saved template is always an inert draft — this method **never** calls
        ``activate_template`` (R8.1, R8.3).

        Args:
            template: The assembled template to persist (its ``template_id`` is
                the deterministic content hash).

        Returns:
            A :class:`SaveResult` with the saved ``template_id`` and whether the
            operation created or updated (R8.2).

        Raises:
            OMSUnavailableError: propagated unchanged from the client on any MCP
                transport failure, so a failed request leaves OMS Core untouched.
            OMSToolError: when OMS Core refused or failed the write (e.g. the
                hosted endpoint's read-only gate), or when the response is not
                credible evidence that a template was saved. A save is never
                reported as successful in either case.
        """
        payload = template.to_dict()

        conflict = False
        create_response: Any = None
        try:
            create_response = self._oms_client.save_template(payload, update=False)
        except OMSToolError as exc:
            # The MCP chokepoint raises for ANY structured OMS error envelope,
            # including the conflict envelope a create gets when the deterministic
            # id already exists. Only that conflict is recoverable; every other
            # refusal (e.g. kind="read_only") propagates so a refused write is
            # never reported as a save.
            if not _error_indicates_already_exists(exc):
                raise
            conflict = True
        else:
            conflict = _indicates_already_exists(create_response)

        if conflict:
            logger.info(
                "template %s already exists; routing save to update",
                template.template_id,
            )
            update_response = self._oms_client.save_template(payload, update=True)
            _assert_usable_save_response(
                update_response, _STAGE_SAVE, template.template_id
            )
            return SaveResult(
                template_id=_extract_template_id(update_response, template.template_id),
                operation="updated",
                raw=update_response if isinstance(update_response, dict) else {},
            )

        _assert_usable_save_response(create_response, _STAGE_SAVE, template.template_id)
        return SaveResult(
            template_id=_extract_template_id(create_response, template.template_id),
            operation="created",
            raw=create_response if isinstance(create_response, dict) else {},
        )

    # -- activate: only ever reached via the confirmation surface (R9.5) -----

    def activate(self, template_id: str) -> ActivateResult:
        """Activate a previously saved template (R9.5).

        Calls ``activate_template(template_id)`` on the injected client. This is
        the sole method that activates, and it is only ever invoked by the
        human-confirmation surface after an explicit ``confirm=true`` — never from
        :meth:`save`.

        Args:
            template_id: The id of the draft template to activate.

        Returns:
            An :class:`ActivateResult` recording the activated ``template_id``.

        Raises:
            OMSUnavailableError: propagated unchanged from the client on any MCP
                transport failure.
        """
        response = self._oms_client.activate_template(template_id)
        return ActivateResult(
            template_id=_extract_template_id(response, template_id),
            activated=True,
            raw=response if isinstance(response, dict) else {},
        )


__all__ = ["SaveActivateDriver", "SaveResult", "ActivateResult"]
