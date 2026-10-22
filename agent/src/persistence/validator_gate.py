"""Validator Gate — the mandatory pre-write gate of the persistence pipeline (R7).

This module is the "gate seam" of the `Persistence_Tool`: it is the last set of
checks that must pass before the single `save_template` write is ever attempted.
It exists so that **no** template is persisted until (a) every ``service.action``
the assembled template references is confirmed present in the registry snapshot
and (b) OMS Core's own ``validate_template`` accepts the template. Any failure
here raises a typed error and returns *without* contacting the write path, so
OMS Core is left completely unchanged on a failed request (Property 5).

Two responsibilities, mirroring the design's Validator Gate interface:

- ``assert_registry_coverage(template, index)`` (R7.1, R7.3) — pure coverage
  check of every referenced ``service.action`` against the already-built
  :class:`~persistence.models.CapabilityIndex`. Any missing id raises
  :class:`~persistence.models.RegistryCoverageError` carrying the itemized
  missing ids. It never calls ``save_template``.
- ``run_oms_validation(template)`` (R7.2, R7.4) — submits the assembled template
  (as a plain dict via ``template.to_dict()``) to the injected ``OMSMcpClient``'s
  ``validate_template`` for cycle detection, reference integrity, and
  typed-mapping compatibility. If OMS Core reports the template invalid, it
  raises :class:`~persistence.models.TemplateValidationError` carrying the
  reported findings. It never calls ``save_template``.

The ``OMSMcpClient`` is injected via the constructor (never constructed inline)
so the MCP boundary is substitutable by a recording fake in tests. This gate is
deliberately narrow: it *reads* (`validate_template`) but never *writes*.

Requirements: 7.1, 7.2, 7.3, 7.4
Properties: 5, 6
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .models import (
    CapabilityIndex,
    OMSTemplate,
    RegistryCoverageError,
    TemplateValidationError,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .oms_mcp_client import OMSMcpClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private constants
# ---------------------------------------------------------------------------

#: Response keys under which OMS Core's ``validate_template`` may itemize the
#: reasons a template was rejected. Interpreted defensively / in order so a
#: slightly different envelope shape still surfaces the findings (R7.4).
_FINDING_KEYS: tuple[str, ...] = ("findings", "errors", "issues", "violations")

#: Response keys carrying an explicit boolean verdict, tried in order (R7.2).
_VALID_KEYS: tuple[str, ...] = ("valid", "is_valid", "ok")

#: Synthetic finding used when OMS Core reports "invalid" without itemizing why,
#: so the raised :class:`TemplateValidationError` is never emptily uninformative.
_UNSPECIFIED_FINDING: dict = {
    "message": "template reported invalid by OMS Core validate_template"
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _referenced_service_actions(template: OMSTemplate) -> list[str]:
    """Collect every ``service.action`` referenced by the template's tasks.

    Walks every stage's task list (``stages.<stage>.tasks``) and returns the
    referenced ``f"{service}.{action}"`` identifiers in first-appearance order,
    de-duplicated. A task missing a ``service`` or ``action`` contributes no
    reference (there is nothing coherent to cover-check).

    Args:
        template: The assembled template whose task references to enumerate.

    Returns:
        The ordered, de-duplicated list of referenced ``service.action`` ids.
    """
    referenced: list[str] = []
    seen: set[str] = set()
    for stage in template.stages.values():
        if not isinstance(stage, dict):
            continue  # pragma: no cover — defensive; stages are always dicts
        for task in stage.get("tasks", []) or []:
            if not isinstance(task, dict):
                continue  # pragma: no cover — defensive
            service = task.get("service")
            action = task.get("action")
            if not service or not action:
                continue
            service_action = f"{service}.{action}"
            if service_action not in seen:
                seen.add(service_action)
                referenced.append(service_action)
    return referenced


def _interpret_validation_response(response: Any) -> tuple[bool, list[dict]]:
    """Interpret an OMS ``validate_template`` response defensively (R7.2, R7.4).

    Tolerates the common envelope shapes rather than assuming one exact schema:

    - ``{"valid": bool, "findings": [...]}`` — explicit verdict plus findings.
    - an errors/issues list under any of :data:`_FINDING_KEYS`.
    - a bare boolean verdict under any of :data:`_VALID_KEYS`.

    When no explicit verdict field is present, the template is considered valid
    **iff** no findings/errors were reported. A non-dict response is treated as
    invalid (an unexpected shape must never be read as a silent pass).

    Args:
        response: The decoded ``validate_template`` payload.

    Returns:
        A ``(valid, findings)`` tuple: ``valid`` is the interpreted verdict and
        ``findings`` the itemized reasons (may be empty).
    """
    if not isinstance(response, dict):
        return False, [
            {"message": "unexpected validate_template response", "raw": response}
        ]

    findings: list[dict] = []
    for key in _FINDING_KEYS:
        value = response.get(key)
        if isinstance(value, list):
            findings.extend(
                item if isinstance(item, dict) else {"message": str(item)}
                for item in value
            )

    valid: bool
    for key in _VALID_KEYS:
        if key in response:
            valid = bool(response[key])
            break
    else:
        # No explicit verdict — infer from whether anything was flagged.
        valid = not findings

    if not valid and not findings:
        findings = [dict(_UNSPECIFIED_FINDING)]
    return valid, findings


# ---------------------------------------------------------------------------
# Validator Gate
# ---------------------------------------------------------------------------


class ValidatorGate:
    """The mandatory pre-write validation gate (R7).

    Constructor-injected with the :class:`~persistence.oms_mcp_client.OMSMcpClient`
    (real or a recording fake) so the MCP boundary is substitutable in tests. The
    gate only ever *reads* OMS Core (``validate_template``); it never calls
    ``save_template`` — that write is owned by the Save/Activate Driver and is only
    reached after both checks here pass.
    """

    def __init__(self, oms_client: "OMSMcpClient") -> None:
        self._oms_client = oms_client

    # -- registry coverage (R7.1, R7.3) --------------------------------------

    def assert_registry_coverage(
        self, template: OMSTemplate, index: CapabilityIndex
    ) -> None:
        """Confirm every referenced ``service.action`` exists in the registry.

        Pure check against the already-built ``index`` snapshot: collects every
        ``service.action`` the template's tasks reference and verifies each is a
        registered Capability (R7.1). Any missing one raises
        :class:`RegistryCoverageError` carrying the itemized missing ids, and does
        so **without** calling ``save_template`` (R7.3) — the request fails closed
        with OMS Core untouched.

        Args:
            template: The assembled template to cover-check.
            index: The registry snapshot of known Capabilities.

        Raises:
            RegistryCoverageError: when one or more referenced ``service.action``
                ids are absent from ``index``.
        """
        registered = {cap.service_action for cap in index.capabilities}
        referenced = _referenced_service_actions(template)
        missing = [sa for sa in referenced if sa not in registered]
        if missing:
            logger.warning(
                "registry coverage failed for template %s: missing %s",
                template.template_id,
                missing,
            )
            raise RegistryCoverageError(missing)

    # -- OMS validation (R7.2, R7.4) -----------------------------------------

    def run_oms_validation(self, template: OMSTemplate) -> dict:
        """Submit the template to OMS ``validate_template`` and gate on the verdict.

        Sends the assembled template as a plain dict (``template.to_dict()``) to
        the injected client's ``validate_template`` for cycle detection, reference
        integrity, and typed-mapping compatibility (R7.2). If OMS Core reports the
        template invalid, raises :class:`TemplateValidationError` carrying the
        reported findings and does **not** call ``save_template`` (R7.4).

        Args:
            template: The assembled template to validate.

        Returns:
            The decoded ``validate_template`` response dict on a valid verdict, so
            the caller can surface any advisory (non-fatal) findings.

        Raises:
            TemplateValidationError: when OMS Core reports the template invalid.
        """
        response = self._oms_client.validate_template(template.to_dict())
        valid, findings = _interpret_validation_response(response)
        if not valid:
            logger.warning(
                "OMS validate_template rejected template %s with %d finding(s)",
                template.template_id,
                len(findings),
            )
            raise TemplateValidationError(findings)
        return response if isinstance(response, dict) else {}


__all__ = ["ValidatorGate"]
