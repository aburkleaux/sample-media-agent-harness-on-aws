"""Unit tests for the PersistenceService pipeline composition (task 10.1).

Wires the whole read → resolve → assemble → validate → save pipeline with
in-memory fakes and asserts:

- happy path: a fully-mapped plan persists as ``saved_draft`` and the report
  carries the saved ``template_id``, the capability→``service.action`` mapping,
  the scoped ``conditions``, ``template_state == "draft"``, and the create/update
  ``operation`` (R12.1, Property 13).
- failure paths — missing plan (read_plan), unmappable step, unpublished gap
  (resolve), registry-coverage and OMS-validation (validate) — each return
  ``status == "failed"`` with the correct ``failing_stage`` and itemized
  ``errors``, and NEVER reach ``save_template`` (R12.2, R12.3, Property 5).

The pipeline's read-only work precedes the single write, so every pre-save
failure leaves the recording fake OMS client's ``save_template`` uncalled.

Requirements: 12.1, 12.2, 12.3
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from planning.high_level_plan import HighLevelPlan, SubGoal
from persistence.capability_resolver import build_capability_index, resolve
from persistence.models import (
    Capability,
    CapabilityIndex,
    MissingPlanError,
)
from persistence.persistence_service import PersistenceService


# ---------------------------------------------------------------------------
# Recording fake OMS client — records every call; scripts registry + verdicts
# ---------------------------------------------------------------------------


class RecordingFakeOMSClient:
    """A fake ``OMSMcpClient`` recording every call, driving the whole pipeline.

    The registry is scripted from an ``integrations`` map (integration_id ->
    list of ``{action, tool_name}`` dicts) so ``build_capability_index`` reads a
    realistic snapshot. ``validate_template`` returns the scripted verdict, and
    ``save_template`` models create/update while logging the write so tests can
    assert it is never reached on a pre-save failure.
    """

    def __init__(self, integrations: dict, validate_response=None) -> None:
        self._integrations = integrations
        self._validate_response = (
            validate_response if validate_response is not None else {"valid": True, "findings": []}
        )
        self.calls: list[tuple[str, dict]] = []
        self.templates: dict[str, dict] = {}

    # -- registry reads (build_capability_index) --

    def list_integrations(self) -> list[dict]:
        self.calls.append(("list_integrations", {}))
        return [{"integration_id": iid} for iid in self._integrations]

    def get_integration(self, integration_id: str) -> dict:
        self.calls.append(("get_integration", {"integration_id": integration_id}))
        entries = self._integrations.get(integration_id, [])
        actions = [a["action"] for a in entries]
        # Option B: the bridge publishes the per-action gateway tool name into the
        # service registry, surfaced here as the ``action_tool_names`` map.
        action_tool_names = {
            a["action"]: a["tool_name"]
            for a in entries
            if a.get("tool_name") is not None
        }
        # The bridge also publishes each action's status/polling tool name, which
        # OMS Core uses internally; surfaced here as ``action_status_tools``.
        action_status_tools = {
            a["action"]: a["status_tool"]
            for a in entries
            if a.get("status_tool") is not None
        }
        detail = {"actions": actions, "action_tool_names": action_tool_names}
        if action_status_tools:
            detail["action_status_tools"] = action_status_tools
        return detail

    def get_action_schema(self, integration_id: str, action: str) -> dict:
        # Retained for interface fidelity; the builder no longer reads tool_name
        # from here (Option B). Returns only kit-resolved fields.
        self.calls.append(
            ("get_action_schema", {"integration_id": integration_id, "action": action})
        )
        return {"service": integration_id, "action": action}

    # -- validate / save / activate --

    def validate_template(self, template: dict) -> dict:
        self.calls.append(("validate_template", {"template": template}))
        if isinstance(self._validate_response, Exception):
            raise self._validate_response
        return self._validate_response

    def save_template(self, template: dict, update: bool) -> dict:
        self.calls.append(("save_template", {"template": template, "update": update}))
        template_id = template["template_id"]
        if update:
            self.templates[template_id] = template
            return {"template_id": template_id, "updated": True}
        if template_id in self.templates:
            return {"error": f"template {template_id} already exists", "template_id": template_id}
        self.templates[template_id] = template
        return {"template_id": template_id, "created": True}

    def activate_template(self, template_id: str) -> dict:
        self.calls.append(("activate_template", {"template_id": template_id}))
        return {"template_id": template_id, "activated": True}

    @property
    def called_tools(self) -> list[str]:
        return [name for name, _ in self.calls]


# ---------------------------------------------------------------------------
# Fake Plan Reader — no I/O; scripts the plan (or a MissingPlanError)
# ---------------------------------------------------------------------------


class FakePlanReader:
    """A stand-in ``PlanReader`` that returns a scripted plan (or raises)."""

    def __init__(self, plan=None, error: Exception | None = None) -> None:
        self._plan = plan
        self._error = error
        self.read_calls: list[str] = []

    async def read_plan(self, session_id: str) -> HighLevelPlan:
        self.read_calls.append(session_id)
        if self._error is not None:
            raise self._error
        return self._plan

    def extract_steps(self, plan: HighLevelPlan):
        # Delegate to the real pure normalization so steps are realistic.
        from persistence.plan_reader import extract_steps

        return extract_steps(plan)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _plan(sub_goals: list[SubGoal]) -> HighLevelPlan:
    return HighLevelPlan(
        execution_mode="standard",
        sub_goals=sub_goals,
        suggested_tools={},
        suggested_skills={},
        reasoning="separate vocals from the music track and encode the result",
    )


def _sub_goal(sub_goal_id: str, tools: list[str], depends_on: list[str] | None = None) -> SubGoal:
    return SubGoal(
        id=sub_goal_id,
        description="do the thing",
        reasoning="separate vocals",
        suggested_tools=tools,
        suggested_skills=[],
        depends_on=depends_on or [],
    )


# Registry: audio.stem_separation publishes its tool_name; audio.mix does NOT.
_PUBLISHED_INTEGRATIONS = {
    "audio": [
        {"action": "stem_separation", "tool_name": "audio-stem___separate_audio_stems"},
        {"action": "encode", "tool_name": "audio-enc___encode_audio"},
    ],
}

_UNPUBLISHED_INTEGRATIONS = {
    "audio": [
        # No tool_name published -> unpublished capability (bridge gap).
        {"action": "mix", "tool_name": None},
    ],
}


# Registry mirroring the live audio integration: each action publishes its
# capability tool name AND a job-status polling tool OMS handles internally.
_STATUS_TOOL_INTEGRATIONS = {
    "audio": [
        {
            "action": "stem_separation",
            "tool_name": "audio-stem___separate_audio_stems",
            "status_tool": "audio-stem___get_job_status",
        },
    ],
    "speech-diarization": [
        {
            "action": "diarization",
            "tool_name": "speech-diarization___diarize_speech",
            "status_tool": "speech-diarization___get_job_status",
        },
    ],
}


def _service(plan_reader, client) -> PersistenceService:
    return PersistenceService(oms_client=client, plan_reader=plan_reader)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Happy path (R12.1, Property 13)
# ---------------------------------------------------------------------------


def test_persist_happy_path_saves_draft_with_mapping_and_conditions():
    plan = _plan(
        [
            _sub_goal("sg1", ["separate_audio_stems"]),
            _sub_goal("sg2", ["encode_audio"], depends_on=["sg1"]),
        ]
    )
    client = RecordingFakeOMSClient(_PUBLISHED_INTEGRATIONS)
    service = _service(FakePlanReader(plan=plan), client)

    report = _run(service.persist("sess-1"))

    assert report.status == "saved_draft"
    assert report.failing_stage is None
    assert report.errors == []
    assert report.template_state == "draft"
    assert report.operation == "created"
    assert report.template_id and report.template_id.startswith("wf-")

    # Mapping surfaces both capabilities as service.action pairs.
    mapped_pairs = {(m["service"], m["action"]) for m in report.mapping}
    assert mapped_pairs == {("audio", "stem_separation"), ("audio", "encode")}
    for entry in report.mapping:
        assert set(entry) >= {"sub_goal_id", "bare_tool", "service", "action", "task_id"}

    # Conditions are scoped (non-empty) and the single write happened once.
    assert report.conditions
    assert client.called_tools.count("save_template") == 1
    assert "activate_template" not in client.called_tools


def test_persist_namespaced_capability_and_status_tool_saves_one_task():
    # Regression for the live UnmappableStepsError: planning emits NAMESPACED
    # gateway tool names, and the plan includes a job-status polling tool. The
    # capability step must map (bare-name join) and the status step must be
    # SKIPPED (not unmappable), yielding exactly one saved task.
    plan = _plan(
        [
            _sub_goal("sg1", ["audio-stem___separate_audio_stems"]),
            _sub_goal("sg2", ["audio-stem___get_job_status"], depends_on=["sg1"]),
        ]
    )
    client = RecordingFakeOMSClient(_STATUS_TOOL_INTEGRATIONS)
    service = _service(FakePlanReader(plan=plan), client)

    report = _run(service.persist("sess-live"))

    # No UnmappableStepsError: the persist succeeds as a draft.
    assert report.status == "saved_draft", report.errors
    assert report.failing_stage is None
    # Only the real capability is mapped; the status tool is not in the mapping.
    mapped_pairs = {(m["service"], m["action"]) for m in report.mapping}
    assert mapped_pairs == {("audio", "stem_separation")}

    # The saved template carries exactly one fulfill task, with no dangling edge.
    saved = client.templates[report.template_id]
    fulfill_tasks = saved["stages"]["fulfill"]["tasks"]
    assert len(fulfill_tasks) == 1
    assert (fulfill_tasks[0]["service"], fulfill_tasks[0]["action"]) == ("audio", "stem_separation")
    assert client.called_tools.count("save_template") == 1


def test_persist_orders_validate_before_the_single_save():
    plan = _plan([_sub_goal("sg1", ["separate_audio_stems"])])
    client = RecordingFakeOMSClient(_PUBLISHED_INTEGRATIONS)
    service = _service(FakePlanReader(plan=plan), client)

    _run(service.persist("sess-1"))

    tools = client.called_tools
    assert "validate_template" in tools
    assert "save_template" in tools
    assert tools.index("validate_template") < tools.index("save_template")


# ---------------------------------------------------------------------------
# Failure: missing plan (read_plan stage) — no OMS contact at all
# ---------------------------------------------------------------------------


def test_persist_missing_plan_fails_at_read_plan_with_no_oms_contact():
    client = RecordingFakeOMSClient(_PUBLISHED_INTEGRATIONS)
    reader = FakePlanReader(error=MissingPlanError("sess-x"))
    service = _service(reader, client)

    report = _run(service.persist("sess-x"))

    assert report.status == "failed"
    assert report.failing_stage == "read_plan"
    assert report.template_id is None
    assert len(report.errors) == 1
    assert report.errors[0]["error"] == "MissingPlanError"
    assert report.errors[0]["session_id"] == "sess-x"
    # No OMS interaction whatsoever.
    assert client.calls == []


# ---------------------------------------------------------------------------
# Failure: unmappable step (resolve stage) — no save
# ---------------------------------------------------------------------------


def test_persist_unmappable_step_fails_at_resolve_with_no_save():
    # "no_such_tool" matches no published bare tool name and no unpublished cap.
    plan = _plan([_sub_goal("sg1", ["no_such_tool"])])
    client = RecordingFakeOMSClient(_PUBLISHED_INTEGRATIONS)
    service = _service(FakePlanReader(plan=plan), client)

    report = _run(service.persist("sess-1"))

    assert report.status == "failed"
    assert report.failing_stage == "resolve"
    assert report.errors[0]["error"] == "UnmappableStepsError"
    assert any(e.get("bare_tool") == "no_such_tool" for e in report.errors)
    assert "save_template" not in client.called_tools


# ---------------------------------------------------------------------------
# Failure: unpublished tool name (resolve stage, distinct gap) — no save
# ---------------------------------------------------------------------------


def test_persist_unpublished_capability_fails_as_distinct_gap_no_save():
    # The plan needs audio.mix, which exists in the registry with no tool_name.
    plan = _plan([_sub_goal("sg1", ["mix"])])
    client = RecordingFakeOMSClient(_UNPUBLISHED_INTEGRATIONS)
    service = _service(FakePlanReader(plan=plan), client)

    report = _run(service.persist("sess-1"))

    assert report.status == "failed"
    assert report.failing_stage == "resolve"
    assert report.errors[0]["error"] == "UnpublishedToolNameError"
    assert report.errors[0]["service_action"] == "audio.mix"
    assert "save_template" not in client.called_tools


# ---------------------------------------------------------------------------
# Failure: registry coverage (validate stage) — no save
# ---------------------------------------------------------------------------


def test_persist_registry_coverage_failure_fails_at_validate_no_save():
    # The resolver maps the step (index has the capability), but the validator
    # gate is handed an index that does NOT cover it -> RegistryCoverageError.
    plan = _plan([_sub_goal("sg1", ["separate_audio_stems"])])
    client = RecordingFakeOMSClient(_PUBLISHED_INTEGRATIONS)

    # Inject a resolver that maps against the full index, but a build_index seam
    # that returns an EMPTY coverage index so assert_registry_coverage fails.
    full_index = build_capability_index(client)

    service = PersistenceService(
        oms_client=client,
        plan_reader=FakePlanReader(plan=plan),
        build_capability_index_fn=lambda _client: CapabilityIndex(capabilities=[]),
        resolve_fn=lambda steps, _index: resolve(steps, full_index),
    )

    report = _run(service.persist("sess-1"))

    assert report.status == "failed"
    assert report.failing_stage == "validate"
    assert report.errors[0]["error"] == "RegistryCoverageError"
    assert report.errors[0]["service_action"] == "audio.stem_separation"
    assert "save_template" not in client.called_tools


# ---------------------------------------------------------------------------
# Failure: OMS validation rejects the template (validate stage) — no save
# ---------------------------------------------------------------------------


def test_persist_validation_failure_fails_at_validate_no_save():
    plan = _plan([_sub_goal("sg1", ["separate_audio_stems"])])
    findings = [{"code": "CYCLE", "message": "dependency cycle detected"}]
    client = RecordingFakeOMSClient(
        _PUBLISHED_INTEGRATIONS, validate_response={"valid": False, "findings": findings}
    )
    service = _service(FakePlanReader(plan=plan), client)

    report = _run(service.persist("sess-1"))

    assert report.status == "failed"
    assert report.failing_stage == "validate"
    assert report.errors[0]["error"] == "TemplateValidationError"
    assert report.errors[0]["finding"] == findings[0]
    assert "save_template" not in client.called_tools


# ---------------------------------------------------------------------------
# confirm_activation delegates to the driver (guard lives in the tool layer)
# ---------------------------------------------------------------------------


def test_confirm_activation_delegates_to_driver():
    client = RecordingFakeOMSClient(_PUBLISHED_INTEGRATIONS)
    service = _service(FakePlanReader(plan=None), client)

    result = _run(service.confirm_activation("wf-abc"))

    assert result.activated is True
    assert result.template_id == "wf-abc"
    assert client.called_tools == ["activate_template"]
