"""PersistenceService — the request-bound pipeline composition (R12).

This module is the "composition seam" of the `Persistence_Tool`: a request-bound
`PersistenceService` instantiates and wires the pipeline collaborators — Plan
Reader, capability resolver (index build + join), Template Assembler, Validator
Gate, and Save/Activate Driver — and runs them in the safe order, mirroring how
`agent/src/lab/service.py`'s `LabService` composes its CRUD collaborators.

The single public write happens exactly once and only after **all** read-only
work has succeeded: read → resolve → assemble → validate → save. Any failure
before that write is a typed `PersistenceError` raised by the responsible
collaborator; `persist` catches every one at the top of the pipeline and
converts it to a `PersistenceReport(status="failed", failing_stage=..., errors=[...])`,
leaving OMS Core untouched on any pre-save failure (R12.2, R12.3, Property 5).

All boundaries are injectable so tests can substitute fakes: the `OMSMcpClient`
and `PlanReader` are passed in via the constructor, and the resolver/assembler
*functions* plus the Validator Gate / Save-Activate Driver collaborators default
to the real implementations but can be overridden. `confirm_activation` performs
activation via the driver when called — the `confirm=true` `@tool` guard that
gates it lives in the confirmation surface (task 10.2), not here.

Requirements: 12.1, 12.2, 12.3
Properties: 5, 13
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Callable, Optional

from planning.high_level_plan import HighLevelPlan

from .capability_resolver import build_capability_index as _default_build_index
from .capability_resolver import resolve as _default_resolve
from .models import (
    AmbiguousStepError,
    CapabilityIndex,
    EmptyConditionsError,
    MissingPlanError,
    OMSTemplate,
    OMSToolError,
    OMSUnavailableError,
    PersistenceError,
    PersistenceReport,
    PlanParseError,
    RegistryCoverageError,
    ResolutionReport,
    TemplateValidationError,
    UnmappableStepsError,
    UnpublishedToolNameError,
    WorkflowStep,
)
from .oms_mcp_client import OMSMcpClient
from .plan_reader import PlanReader
from .save_activate_driver import ActivateResult, SaveActivateDriver, SaveResult
from .template_assembler import assemble as _default_assemble
from .validator_gate import ValidatorGate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private constants
# ---------------------------------------------------------------------------

#: Status the report carries on a successful persist — an inert draft (R8, R12.1).
_STATUS_SAVED_DRAFT: str = "saved_draft"

#: Status the report carries when any pipeline stage fails (R12.2).
_STATUS_FAILED: str = "failed"

#: Template state surfaced to the reviewer on success — never activated here (R8).
_TEMPLATE_STATE_DRAFT: str = "draft"


# ---------------------------------------------------------------------------
# Service composition
# ---------------------------------------------------------------------------


class PersistenceService:
    """Request-bound composition of the persistence pipeline (R12).

    Instantiates its collaborators in ``__init__`` from the injected boundaries,
    mirroring ``LabService``. The ``OMSMcpClient`` and ``PlanReader`` are injected
    directly; the Validator Gate and Save/Activate Driver default to real
    instances bound to the injected client (overridable for tests); the resolver
    (``build_capability_index`` / ``resolve``) and assembler (``assemble``) are
    injectable function seams defaulting to the real pure implementations.

    Args:
        oms_client: The MCP-only adapter over OMS Core (real or a recording fake).
        plan_reader: The Plan Reader resolving the persisted ``HighLevelPlan``.
        validator_gate: Pre-write gate; defaults to ``ValidatorGate(oms_client)``.
        save_activate_driver: Single write seam; defaults to
            ``SaveActivateDriver(oms_client)``.
        build_capability_index_fn: Registry-snapshot builder; defaults to the
            real ``capability_resolver.build_capability_index``.
        resolve_fn: Deterministic join; defaults to ``capability_resolver.resolve``.
        assemble_fn: Template assembler; defaults to ``template_assembler.assemble``.
    """

    def __init__(
        self,
        *,
        oms_client: OMSMcpClient,
        plan_reader: PlanReader,
        validator_gate: Optional[ValidatorGate] = None,
        save_activate_driver: Optional[SaveActivateDriver] = None,
        build_capability_index_fn: Callable[[Any], CapabilityIndex] = _default_build_index,
        resolve_fn: Callable[[list[WorkflowStep], CapabilityIndex], ResolutionReport] = _default_resolve,
        assemble_fn: Callable[[ResolutionReport, HighLevelPlan], OMSTemplate] = _default_assemble,
    ) -> None:
        self._oms_client = oms_client
        self._plan_reader = plan_reader
        # Collaborators default to real instances bound to the injected client,
        # but stay overridable so tests can substitute fakes (mirrors LabService).
        self._validator_gate = validator_gate or ValidatorGate(oms_client)
        self._driver = save_activate_driver or SaveActivateDriver(oms_client)
        # Pure-logic function seams (no I/O) — injectable for isolation in tests.
        self._build_capability_index = build_capability_index_fn
        self._resolve = resolve_fn
        self._assemble = assemble_fn

    # -- persist: read -> resolve -> assemble -> validate -> save (R12) ------

    async def persist(self, session_id: str) -> PersistenceReport:
        """Run the full persistence pipeline for ``session_id`` (R12.1).

        Ordered so that **all** read-only work (read plan → build index + resolve
        → assemble → registry coverage + OMS validation) precedes the single
        ``save_template`` write. On success, returns a
        ``PersistenceReport(status="saved_draft")`` carrying the saved
        ``template_id``, the capability→``service.action`` mapping, the scoped
        ``conditions``, ``template_state="draft"``, and the create/update
        ``operation`` (R12.1, Property 13).

        Every typed :class:`PersistenceError` raised by a collaborator is caught
        here and converted to a ``PersistenceReport(status="failed")`` naming the
        ``failing_stage`` and itemizing ``errors``; because the single write is
        the last step, any pre-save failure leaves OMS Core unchanged (R12.2,
        R12.3, Property 5).

        Args:
            session_id: The session whose discovered workflow to perennialize.

        Returns:
            A JSON-safe :class:`PersistenceReport` (success or failure).
        """
        try:
            # 1. Read-only: resolve + normalize the persisted plan (no OMS yet).
            plan = await self._plan_reader.read_plan(session_id)
            steps = self._plan_reader.extract_steps(plan)

            # 2. Read-only: build the registry snapshot and join (fail-closed if
            #    any step is unmappable / ambiguous / unpublished — R2/R3/R4).
            index = self._build_capability_index(self._oms_client)
            report = self._resolve(steps, index)
            if not report.ok:
                self._raise_resolution_error(report)

            # 3. Pure: assemble the durable template (scoped conditions, det. id).
            template = self._assemble(report, plan)

            # 4. Read-only gate: registry coverage + OMS validate_template. Any
            #    failure here raises before the write, leaving OMS untouched (R7).
            self._validator_gate.assert_registry_coverage(template, index)
            self._validator_gate.run_oms_validation(template)

            # 5. The single write — persist as an inert draft (create or update).
            save_result = self._driver.save(template)

            return self._build_success_report(report, template, save_result)
        except PersistenceError as exc:
            # Fail closed: every pre-save failure is reported without any partial
            # template ever reaching OMS Core (R12.2, R12.3, Property 5).
            logger.warning(
                "persist(%s) failed at stage %s: %s",
                session_id,
                getattr(exc, "failing_stage", None),
                exc,
            )
            return self._build_failure_report(exc)

    # -- confirm_activation: driver.activate (guard lives in task 10.2) ------

    async def confirm_activation(self, template_id: str) -> ActivateResult:
        """Activate a previously saved draft via the driver (R9.5).

        This method performs the activation when called; the explicit
        ``confirm=true`` human-confirmation guard that gates *whether* it is
        called lives in the ``@tool`` confirmation surface (task 10.2), not here.

        Args:
            template_id: The id of the draft template to activate.

        Returns:
            The :class:`ActivateResult` recording the activated ``template_id``.

        Raises:
            OMSUnavailableError: propagated from the driver on any MCP transport
                failure.
        """
        return self._driver.activate(template_id)

    # -- report construction -------------------------------------------------

    def _build_success_report(
        self,
        report: ResolutionReport,
        template: OMSTemplate,
        save_result: SaveResult,
    ) -> PersistenceReport:
        """Assemble the ``saved_draft`` report surfaced to the reviewer (Property 13).

        Carries the saved ``template_id`` and create/update ``operation`` from the
        driver, the full capability→``service.action`` mapping (from
        ``report.mapped``), the scoped ``conditions`` from the assembled template,
        and the draft ``template_state`` so the mapping and conditions are visible
        before any activation (R9.2, R10.2, R12.1).
        """
        mapping = [mapped.to_dict() for mapped in report.mapped]
        return PersistenceReport(
            status=_STATUS_SAVED_DRAFT,
            template_id=save_result.template_id,
            operation=save_result.operation,
            mapping=self._make_json_safe(mapping),
            conditions=self._make_json_safe(dict(template.conditions)),
            template_state=_TEMPLATE_STATE_DRAFT,
            failing_stage=None,
            errors=[],
        )

    def _build_failure_report(self, exc: PersistenceError) -> PersistenceReport:
        """Convert a typed :class:`PersistenceError` into a failure report (R12.2).

        Names the ``failing_stage`` the error declares and itemizes the offending
        steps/capabilities/findings so an operator can act (R12.3). No template
        fields are populated — OMS Core is untouched on any pre-save failure.
        """
        return PersistenceReport(
            status=_STATUS_FAILED,
            template_id=None,
            operation=None,
            mapping=[],
            conditions={},
            template_state=_TEMPLATE_STATE_DRAFT,
            failing_stage=getattr(exc, "failing_stage", None),
            errors=self._make_json_safe(self._itemize_errors(exc)),
        )

    # -- typed-error mapping helpers -----------------------------------------

    @staticmethod
    def _raise_resolution_error(report: ResolutionReport) -> None:
        """Raise the appropriate typed resolution error for a failed join (R2/R3/R4).

        A single failed request may accumulate offenders in more than one bucket;
        the errors are raised in a deterministic priority order (unmappable →
        ambiguous → unpublished), each carrying *all* of its itemized offenders so
        the report never loses one (R12.3, Property 3). Never called when
        ``report.ok`` is true.
        """
        if report.unmappable:
            raise UnmappableStepsError(report.unmappable)
        if report.ambiguous:
            raise AmbiguousStepError(report.ambiguous)
        if report.unpublished:
            raise UnpublishedToolNameError(report.unpublished)
        # Defensive: only reached if ok==False with no populated bucket, which the
        # ResolutionReport invariant forbids.
        raise UnmappableStepsError([])  # pragma: no cover — defensive

    @staticmethod
    def _itemize_errors(exc: PersistenceError) -> list[dict]:
        """Itemize a typed error into JSON-safe error entries (R12.3).

        Each entry carries the error class name plus the specific offender it
        names (step, capability, missing ``service.action``, or validation
        finding), so the report identifies every cause. Errors that carry no
        itemizable payload degrade to a single ``{error, message}`` entry.
        """
        error_name = type(exc).__name__

        if isinstance(exc, UnmappableStepsError):
            return [
                {
                    "error": error_name,
                    "sub_goal_id": step.sub_goal_id,
                    "bare_tool": step.bare_tool,
                }
                for step in exc.steps
            ] or [{"error": error_name, "message": str(exc)}]

        if isinstance(exc, AmbiguousStepError):
            return [
                {
                    "error": error_name,
                    "sub_goal_id": step.sub_goal_id,
                    "bare_tool": step.bare_tool,
                    "candidates": [cap.service_action for cap in caps],
                }
                for step, caps in exc.ambiguous
            ] or [{"error": error_name, "message": str(exc)}]

        if isinstance(exc, UnpublishedToolNameError):
            return [
                {
                    "error": error_name,
                    "sub_goal_id": step.sub_goal_id,
                    "bare_tool": step.bare_tool,
                    "service_action": cap.service_action,
                }
                for step, cap in exc.unpublished
            ] or [{"error": error_name, "message": str(exc)}]

        if isinstance(exc, RegistryCoverageError):
            return [
                {"error": error_name, "service_action": service_action}
                for service_action in exc.missing
            ] or [{"error": error_name, "message": str(exc)}]

        if isinstance(exc, TemplateValidationError):
            return [
                {"error": error_name, "finding": finding} for finding in exc.findings
            ] or [{"error": error_name, "message": str(exc)}]

        if isinstance(exc, OMSToolError):
            # OMS Core refused / failed a tool call (e.g. the hosted endpoint's
            # read-only gate). Name the tool and the envelope kind so an operator
            # can act, and make it unmistakable that nothing was persisted.
            return [
                {
                    "error": error_name,
                    "message": str(exc),
                    "tool": exc.tool,
                    "kind": exc.kind,
                    "oms_message": exc.message,
                }
            ]

        if isinstance(exc, (MissingPlanError, PlanParseError)):
            return [
                {
                    "error": error_name,
                    "message": str(exc),
                    "session_id": getattr(exc, "session_id", None),
                }
            ]

        # EmptyConditionsError, OMSUnavailableError, and any other PersistenceError.
        return [{"error": error_name, "message": str(exc)}]

    # -- JSON safety (mirrors LabService._make_json_safe) --------------------

    @staticmethod
    def _make_json_safe(obj: Any) -> Any:
        """Recursively convert ``Decimal`` and other non-JSON types to JSON-safe.

        DynamoDB numbers surface as :class:`Decimal`, and an OMS validation
        finding echoed into ``errors`` could carry one, so the report is
        normalized before it crosses the ``@tool`` boundary (mirrors
        ``LabService._make_json_safe``).
        """
        if isinstance(obj, Decimal):
            return int(obj) if obj == int(obj) else float(obj)
        if isinstance(obj, dict):
            return {k: PersistenceService._make_json_safe(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [PersistenceService._make_json_safe(item) for item in obj]
        return obj


__all__ = ["PersistenceService"]
