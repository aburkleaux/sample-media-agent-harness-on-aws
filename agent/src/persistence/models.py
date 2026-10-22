"""Pure data models and typed errors for the OMS workflow-persistence tool.

This module is the value-object "seam" of the `Persistence_Tool`: it holds the
plain, I/O-free dataclasses that flow through the read → resolve → assemble →
validate → save pipeline, plus the one-per-failure-mode exception hierarchy the
`@tool` boundary catches and converts into a `PersistenceReport`.

Everything here is pure logic — no boto3, no MCP, no S3/DDB. Shapes mirror the
design's "Data Models" section exactly so the resolver, assembler, validator
gate, and driver can depend on typed fields rather than raw dicts. Dataclasses
that cross the OMS MCP boundary (`OMSTask`, `OMSTemplate`) or the registry read
boundary (`Capability`), and the report returned to the caller
(`PersistenceReport`), carry symmetric `to_dict` / `from_dict` helpers.

Each typed error carries the itemized offending steps/capabilities as
attributes and declares its `failing_stage` so R12.2/R12.3 reporting stays
structured rather than string-matched.

Requirements: 2.4, 5.1, 5.2, 12.2, 12.3
Properties: 3, 9
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private constants
# ---------------------------------------------------------------------------

#: Literal namespace separator inside a ``Gateway_Tool_Name`` (``<target>___<bare>``).
#: The bare tool name is the suffix after the final occurrence of this token.
_NAMESPACE_SEPARATOR: str = "___"

#: The only state a freshly persisted template may report — an inert draft (R8).
_TEMPLATE_STATE_DRAFT: str = "draft"

#: The OMS structured-error ``kind`` emitted when the hosted MCP endpoint's
#: read-only gate refuses a mutating tool (``OMS_MCP_READ_ONLY=true``).
_OMS_ERROR_KIND_READ_ONLY: str = "read_only"

PersistenceStatus = Literal["saved_draft", "failed"]
PersistenceOperation = Literal["created", "updated"]


# ---------------------------------------------------------------------------
# Shared bare-tool-name primitive (R3.2, Property 2)
# ---------------------------------------------------------------------------


def compute_bare_tool_name(gateway_tool_name: Optional[str]) -> Optional[str]:
    """Compute the bare tool name from a namespaced ``Gateway_Tool_Name``.

    A ``Gateway_Tool_Name`` is namespaced as ``<target>___<bare>`` (for example
    ``audio-stem___separate_audio_stems``). The bare tool name is the substring
    following the *final* ``___`` separator (R3.2, Property 2), so a
    multi-segment prefix still yields the trailing bare name.

    This is the single shared normalizer used by *both* sides of the resolver's
    join: the capability side (``capability_resolver.build_capability_index``)
    and the step side (``plan_reader._steps_from_sub_goal``). It lives in
    ``models`` as a public primitive so both importers depend on it without an
    import cycle; ``capability_resolver`` re-exports it for backwards
    compatibility.

    Args:
        gateway_tool_name: The published/namespaced gateway tool name, or
            ``None`` when the Capability publishes no tool name.

    Returns:
        The bare tool name, or ``None`` when the tool name is absent (``None`` /
        empty), has no ``___`` separator, or leaves an empty suffix after the
        final separator. On the capability side a ``None`` result marks the
        capability as *unpublished* — the distinct ``Bridge_Tool_Name_Publication``
        dependency gap the resolver classifies apart from a generic unmappable
        step (R4.2/R4.3).
    """
    if not gateway_tool_name:
        return None
    if _NAMESPACE_SEPARATOR not in gateway_tool_name:
        return None
    # ``rsplit(..., 1)`` isolates the suffix after the FINAL separator, so a
    # multi-segment target prefix (``a___b___bare``) still resolves to ``bare``.
    bare = gateway_tool_name.rsplit(_NAMESPACE_SEPARATOR, 1)[-1]
    # An empty suffix (e.g. ``target___``) is a malformed, unpublishable name.
    return bare or None


# ---------------------------------------------------------------------------
# Normalized plan step (R1.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowStep:
    """One persistable unit derived from a ``SubGoal`` (design "Normalized").

    A sub-goal contributes one step per suggested bare tool; a tool-less
    sub-goal still yields a step with ``bare_tool=None`` so it can be classified
    as unmappable downstream (R2.3). ``sub_goal_id`` is the dependency vertex and
    ``depends_on`` carries the sub-goal ids this step depends on, so the plan DAG
    survives normalization (Property 8).
    """

    sub_goal_id: str
    bare_tool: Optional[str]
    depends_on: list[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "sub_goal_id": self.sub_goal_id,
            "bare_tool": self.bare_tool,
            "depends_on": list(self.depends_on),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WorkflowStep:
        return cls(
            sub_goal_id=data["sub_goal_id"],
            bare_tool=data.get("bare_tool"),
            depends_on=list(data.get("depends_on", [])),
            description=data.get("description", ""),
        )


# ---------------------------------------------------------------------------
# Capability index — the registry snapshot the join reads (R3, R4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """A single registered OMS ``service.action`` and its published tool name.

    ``gateway_tool_name`` is the ``Bridge_Tool_Name_Publication`` read from the
    Service_Registry (may be absent). ``bare_tool_name`` is the suffix after the
    final ``___`` separator, or ``None`` when the tool name is unpublished or
    separator-less — the distinct "unpublished" dependency gap of R4.2/R4.3.
    """

    service: str
    action: str
    gateway_tool_name: Optional[str] = None
    bare_tool_name: Optional[str] = None

    @property
    def service_action(self) -> str:
        """The canonical ``service.action`` identifier for this Capability."""
        return f"{self.service}.{self.action}"

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "action": self.action,
            "gateway_tool_name": self.gateway_tool_name,
            "bare_tool_name": self.bare_tool_name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Capability:
        return cls(
            service=data["service"],
            action=data["action"],
            gateway_tool_name=data.get("gateway_tool_name"),
            bare_tool_name=data.get("bare_tool_name"),
        )


@dataclass
class CapabilityIndex:
    """The full set of registered Capabilities plus a bare-name lookup.

    ``lookup`` maps a bare tool name to every Capability publishing it; a list of
    length > 1 is what the resolver classifies as ambiguous (R3.4). The lookup is
    derived from ``capabilities`` and only indexes Capabilities that actually
    publish a bare tool name (unpublished ones cannot be joined).
    """

    capabilities: list[Capability] = field(default_factory=list)
    lookup: dict[str, list[Capability]] = field(default_factory=dict)
    #: Bare names of every status/polling tool across all integrations (the
    #: values of each integration's ``action_status_tools`` map, bare-reduced).
    #: A step whose bare tool is in this set is a status/poll tool OMS handles
    #: internally — it is intentionally skipped, never perennialized (R-status).
    status_tool_bare_names: set[str] = field(default_factory=set)

    def matches(self, bare_tool: Optional[str]) -> list[Capability]:
        """Return every Capability whose bare tool name exactly equals ``bare_tool``.

        Exact equality only — no fuzzy, case-insensitive, or substring matching
        (R3.5, Property 1). A ``None`` reference never matches.
        """
        if bare_tool is None:
            return []
        return list(self.lookup.get(bare_tool, []))


# ---------------------------------------------------------------------------
# Resolution report — outcome of the deterministic join (R2, R3, R4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MappedStep:
    """A step that resolved to exactly one Capability, with its derived task id."""

    step: WorkflowStep
    service: str
    action: str
    task_id: str

    def to_dict(self) -> dict:
        return {
            "sub_goal_id": self.step.sub_goal_id,
            "bare_tool": self.step.bare_tool,
            "service": self.service,
            "action": self.action,
            "task_id": self.task_id,
        }


@dataclass
class ResolutionReport:
    """The itemized result of resolving every step against the Capability index.

    ``mapped`` holds the successful joins; the three failure buckets itemize the
    offenders so R12.3 / Property 3 reporting names every one. ``ambiguous`` pairs
    each offending step with its conflicting Capabilities (R3.4); ``unpublished``
    pairs each step with the Capability whose tool name is not published
    (R4.2/R4.3, distinct from generic unmappable).
    """

    mapped: list[MappedStep] = field(default_factory=list)
    unmappable: list[WorkflowStep] = field(default_factory=list)
    ambiguous: list[tuple[WorkflowStep, list[Capability]]] = field(default_factory=list)
    unpublished: list[tuple[WorkflowStep, Capability]] = field(default_factory=list)
    #: Steps intentionally dropped from perennialization — status/polling tools
    #: OMS Core handles internally (their bare name is a value in some
    #: ``action_status_tools`` map). Skipped steps produce NO OMS task and do
    #: NOT fail resolution (they never enter unmappable).
    skipped: list[WorkflowStep] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no failure bucket is populated (skipped does NOT fail).

        ``skipped`` steps are intentionally non-perennializable, so a report with
        only ``mapped`` and/or ``skipped`` entries is still ``ok``.
        """
        return not (self.unmappable or self.ambiguous or self.unpublished)


# ---------------------------------------------------------------------------
# Assembled OMS template (R5, R6, R10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OMSTask:
    """One OMS task assembled from a mapped step (design "Produced").

    ``depends_on`` holds task ids (translated from sub-goal ids) so the plan DAG
    is preserved exactly in the template (R5.3, Property 7). ``task_id`` is a
    deterministic function of ``service`` + ``action`` (R5.4).
    """

    task_id: str
    service: str
    action: str
    depends_on: list[str] = field(default_factory=list)
    required: bool = True
    timeout_seconds: int = 0
    config: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "service": self.service,
            "action": self.action,
            "depends_on": list(self.depends_on),
            "required": self.required,
            "timeout_seconds": self.timeout_seconds,
            "config": dict(self.config),
        }

    @classmethod
    def from_dict(cls, data: dict) -> OMSTask:
        return cls(
            task_id=data["task_id"],
            service=data["service"],
            action=data["action"],
            depends_on=list(data.get("depends_on", [])),
            required=bool(data.get("required", True)),
            timeout_seconds=int(data.get("timeout_seconds", 0)),
            config=dict(data.get("config", {})),
        )


@dataclass
class OMSTemplate:
    """A durable OMS workflow definition assembled from a fully-mapped plan.

    ``template_id`` is deterministic over the workflow content (R6.1). ``stages``
    carries ``validate`` and ``fulfill`` task lists (R5.1). ``conditions`` are
    scoped to the discovered workflow's intent and are never empty / match-all
    (R10, Property 12) — the assembler raises rather than emit such a template.
    """

    template_id: str
    name: str
    description: str
    conditions: dict = field(default_factory=dict)
    stages: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "template_id": self.template_id,
            "name": self.name,
            "description": self.description,
            "conditions": dict(self.conditions),
            "stages": dict(self.stages),
        }

    @classmethod
    def from_dict(cls, data: dict) -> OMSTemplate:
        return cls(
            template_id=data["template_id"],
            name=data.get("name", ""),
            description=data.get("description", ""),
            conditions=dict(data.get("conditions", {})),
            stages=dict(data.get("stages", {})),
        )


# ---------------------------------------------------------------------------
# Report returned to the caller (R9.2, R10.2, R12)
# ---------------------------------------------------------------------------


@dataclass
class PersistenceReport:
    """The JSON-safe outcome surfaced at the ``@tool`` boundary.

    On success it carries the saved ``template_id``, the full capability mapping,
    the scoped ``conditions``, and the draft ``template_state`` so a human
    reviewer sees the mapping and conditions before activation (R9.2, R10.2, R12.1,
    Property 13). On failure it names the ``failing_stage`` and itemizes ``errors``
    (R12.2, R12.3) while leaving OMS Core unchanged.
    """

    status: PersistenceStatus
    template_id: Optional[str] = None
    operation: Optional[PersistenceOperation] = None
    mapping: list[dict] = field(default_factory=list)
    conditions: dict = field(default_factory=dict)
    template_state: str = _TEMPLATE_STATE_DRAFT
    failing_stage: Optional[str] = None
    errors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "template_id": self.template_id,
            "operation": self.operation,
            "mapping": [dict(m) for m in self.mapping],
            "conditions": dict(self.conditions),
            "template_state": self.template_state,
            "failing_stage": self.failing_stage,
            "errors": [dict(e) for e in self.errors],
        }

    @classmethod
    def from_dict(cls, data: dict) -> PersistenceReport:
        return cls(
            status=data["status"],
            template_id=data.get("template_id"),
            operation=data.get("operation"),
            mapping=[dict(m) for m in data.get("mapping", [])],
            conditions=dict(data.get("conditions", {})),
            template_state=data.get("template_state", _TEMPLATE_STATE_DRAFT),
            failing_stage=data.get("failing_stage"),
            errors=[dict(e) for e in data.get("errors", [])],
        )


# ---------------------------------------------------------------------------
# Typed errors — one class per failure mode (design "Error Handling" table)
# ---------------------------------------------------------------------------
#
# Every error is raised by the component responsible for its stage and caught
# at the `@tool` boundary, which converts it to a PersistenceReport with
# status="failed", the `failing_stage`, and an itemized `errors` list. No error
# path ever leaves a partial template (Property 5). Each error carries the
# specific offending steps/capabilities as attributes (R12.3) and declares its
# `failing_stage` so mapping to the report never string-matches messages.


class PersistenceError(Exception):
    """Base class for every typed persistence failure.

    ``failing_stage`` names the pipeline stage that raised (``read_plan``,
    ``resolve``, ``assemble``, ``validate``, ``activate``), matching the
    design's error table so R12.2 reporting is structural. Subclasses set it as
    a class attribute; the base default is left ``None`` for defensive catches.
    """

    failing_stage: Optional[str] = None


# --- Plan Reader (R1) ------------------------------------------------------


class MissingPlanError(PersistenceError):
    """No planning output exists for the requested session (R1.3). No OMS contact."""

    failing_stage = "read_plan"

    def __init__(self, session_id: str) -> None:
        super().__init__(f"no persisted plan for session {session_id!r}")
        self.session_id = session_id


class PlanParseError(PersistenceError):
    """Persisted planning output is not a valid HighLevelPlan (R1.4). No OMS contact."""

    failing_stage = "read_plan"

    def __init__(self, message: str, session_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.session_id = session_id


# --- Capability Resolver (R2, R3, R4) --------------------------------------


class UnmappableStepsError(PersistenceError):
    """One or more steps map to no registered Capability (R2.2, R2.3)."""

    failing_stage = "resolve"

    def __init__(self, steps: list[WorkflowStep]) -> None:
        self.steps = list(steps)
        ids = ", ".join(s.sub_goal_id for s in self.steps) or "<none>"
        super().__init__(f"unmappable steps: {ids}")


class AmbiguousStepError(PersistenceError):
    """One or more steps match more than one Capability (R3.4).

    Carries each offending step paired with its conflicting Capabilities.
    """

    failing_stage = "resolve"

    def __init__(
        self, ambiguous: list[tuple[WorkflowStep, list[Capability]]]
    ) -> None:
        self.ambiguous = [(step, list(caps)) for step, caps in ambiguous]
        ids = ", ".join(step.sub_goal_id for step, _ in self.ambiguous) or "<none>"
        super().__init__(f"ambiguous steps: {ids}")


class UnpublishedToolNameError(PersistenceError):
    """A needed Capability exists but has no published Gateway_Tool_Name (R4.2, R4.3).

    Reported as a distinct ``Bridge_Tool_Name_Publication`` dependency gap, not a
    generic unmappable-step error. Carries each offending step paired with the
    Capability whose tool name is unpublished.
    """

    failing_stage = "resolve"

    def __init__(self, unpublished: list[tuple[WorkflowStep, Capability]]) -> None:
        self.unpublished = [(step, cap) for step, cap in unpublished]
        caps = (
            ", ".join(cap.service_action for _, cap in self.unpublished) or "<none>"
        )
        super().__init__(
            f"capabilities missing a published tool name: {caps}"
        )


# --- Template Assembler (R10) ----------------------------------------------


class EmptyConditionsError(PersistenceError):
    """Conditions could not be scoped safely — empty / match-all refused (R10.3)."""

    failing_stage = "assemble"

    def __init__(self, message: str = "refusing to emit empty / match-all conditions") -> None:
        super().__init__(message)
        self.message = message


# --- Validator Gate (R7) ---------------------------------------------------


class RegistryCoverageError(PersistenceError):
    """A referenced ``service.action`` is missing from the registry (R7.1, R7.3).

    Carries the missing ``service.action`` identifiers. No ``save_template`` is
    called.
    """

    failing_stage = "validate"

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(missing)
        joined = ", ".join(self.missing) or "<none>"
        super().__init__(f"service.action not found in registry: {joined}")


class TemplateValidationError(PersistenceError):
    """``validate_template`` reported the template invalid (R7.4).

    Carries the findings returned by OMS Core. No ``save_template`` is called.
    """

    failing_stage = "validate"

    def __init__(self, findings: list[dict]) -> None:
        self.findings = list(findings)
        super().__init__(
            f"template validation failed with {len(self.findings)} finding(s)"
        )


# --- Confirmation Surface (R9) ---------------------------------------------


class ActivationNotConfirmedError(PersistenceError):
    """Activation was requested without explicit human confirmation (R9.1, R9.3, R9.4).

    Surfaced as ``confirmation_required``; ``activate_template`` is never called.
    """

    failing_stage = "activate"

    def __init__(self, template_id: str) -> None:
        super().__init__(
            f"activation of template {template_id!r} requires explicit confirmation"
        )
        self.template_id = template_id


# --- OMS MCP Client (R11) --------------------------------------------------


class OMSUnavailableError(PersistenceError):
    """Gateway / MCP transport failure reaching OMS Core (R11.x). Nothing written."""

    def __init__(self, message: str, stage: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        # Transport can fail at any stage; record where so the report stays honest.
        self.failing_stage = stage


class OMSToolError(PersistenceError):
    """OMS Core returned a structured error payload for a tool call (e.g. the
    hosted endpoint's read-only gate refused a mutating tool).

    Distinct from :class:`OMSUnavailableError`: the MCP *transport* succeeded, so
    the call "worked" at the protocol level — OMS Core simply answered with an
    error envelope:

        {"error": true, "kind": "read_only"|"client_error"|...,
         "message": "<...>", "tool": "<tool>", ...}

    Such a payload decodes to a perfectly ordinary ``dict``, which is exactly how
    a refused write could previously be mistaken for a successful one. Raising
    this typed error at the MCP decode chokepoint makes any refused/failed tool
    call fail loudly instead.

    Attributes:
        tool: The bare OMS tool name that was refused (e.g. ``"save_template"``).
        kind: The envelope's ``kind`` discriminator when present, else ``None``.
        message: The message text OMS Core reported (raw, un-decorated). The
            *exception* message (``str(exc)``) is the fuller, actionable
            rendering including the tool, the kind, and a read-only hint.
        failing_stage: The pipeline stage the refused call belongs to, mirroring
            :class:`OMSUnavailableError` (a tool call can fail at any stage).
    """

    def __init__(
        self,
        tool: str,
        kind: Optional[str],
        message: str,
        stage: Optional[str] = None,
    ) -> None:
        detail = f"OMS Core refused tool {tool!r}"
        if kind:
            detail += f" (kind={kind})"
        detail += f": {message}"
        if kind == _OMS_ERROR_KIND_READ_ONLY:
            detail += (
                " — the hosted OMS MCP endpoint runs with OMS_MCP_READ_ONLY=true, "
                "so mutating tools are refused and NOTHING was written. Deploy / "
                "configure the endpoint with OMS_MCP_READ_ONLY=false (or point at a "
                "writable endpoint) before retrying."
            )
        super().__init__(detail)
        self.tool = tool
        self.kind = kind
        self.message = message
        self.failing_stage = stage


__all__ = [
    # Literals
    "PersistenceStatus",
    "PersistenceOperation",
    # Shared primitives
    "compute_bare_tool_name",
    # Value objects / reports
    "WorkflowStep",
    "Capability",
    "CapabilityIndex",
    "MappedStep",
    "ResolutionReport",
    "OMSTask",
    "OMSTemplate",
    "PersistenceReport",
    # Errors
    "PersistenceError",
    "MissingPlanError",
    "PlanParseError",
    "UnmappableStepsError",
    "AmbiguousStepError",
    "UnpublishedToolNameError",
    "EmptyConditionsError",
    "RegistryCoverageError",
    "TemplateValidationError",
    "ActivationNotConfirmedError",
    "OMSUnavailableError",
    "OMSToolError",
]
