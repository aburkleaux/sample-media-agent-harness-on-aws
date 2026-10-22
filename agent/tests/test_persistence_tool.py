"""Unit tests for the Persistence_Tool @tool boundary (task 10.8).

Exercises the thin ``@tool`` wrappers in ``persistence_tool.py`` end-to-end
through a request-bound ``PersistenceService`` wired to in-memory fakes:

- success reporting (8.2): a fully-mapped plan persists as ``saved_draft`` with
  the create/update ``operation`` surfaced.
- decline (9.4): ``confirm_workflow_activation`` without ``confirm=true`` returns
  ``confirmation_required`` and never activates.
- confirm (9.5): with ``confirm=true`` it activates the draft.
- ``failing_stage`` / atomicity error mapping (12.2, 12.3): typed pipeline errors
  are converted to a failed report naming the stage, leaving OMS untouched.

Requirements: 8.2, 9.4, 9.5, 12.2, 12.3
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from planning.high_level_plan import HighLevelPlan, SubGoal
from persistence.models import MissingPlanError
from persistence.persistence_service import PersistenceService
from persistence import persistence_tool
from persistence.persistence_tool import (
    PERSISTENCE_TOOLS,
    confirm_workflow_activation,
    persist_workflow,
    reset_current_persistence_service,
    set_current_persistence_service,
)

_SEP = "___"


# ---------------------------------------------------------------------------
# Recording fake OMS client + fake plan reader
# ---------------------------------------------------------------------------


class RecordingFakeOMSClient:
    """A fake ``OMSMcpClient`` that records calls and scripts the registry."""

    def __init__(self, integrations: dict, validate_response=None) -> None:
        self._integrations = integrations
        self._validate_response = (
            validate_response
            if validate_response is not None
            else {"valid": True, "findings": []}
        )
        self.calls: list[tuple[str, dict]] = []
        self.templates: dict[str, dict] = {}

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
        return {"actions": actions, "action_tool_names": action_tool_names}

    def get_action_schema(self, integration_id: str, action: str) -> dict:
        # Retained for interface fidelity; the builder no longer reads tool_name
        # from here (Option B). Returns only kit-resolved fields.
        self.calls.append(
            ("get_action_schema", {"integration_id": integration_id, "action": action})
        )
        return {"service": integration_id, "action": action}

    def validate_template(self, template: dict) -> dict:
        self.calls.append(("validate_template", {"template": template}))
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


class FakePlanReader:
    def __init__(self, plan=None, error=None) -> None:
        self._plan = plan
        self._error = error

    async def read_plan(self, session_id: str) -> HighLevelPlan:
        if self._error is not None:
            raise self._error
        return self._plan

    def extract_steps(self, plan: HighLevelPlan):
        from persistence.plan_reader import extract_steps

        return extract_steps(plan)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _sub_goal(sub_goal_id: str, tools: list[str], depends_on=None) -> SubGoal:
    return SubGoal(
        id=sub_goal_id,
        description="do the thing",
        reasoning="separate vocals",
        suggested_tools=tools,
        suggested_skills=[],
        depends_on=depends_on or [],
    )


def _plan(sub_goals: list[SubGoal]) -> HighLevelPlan:
    return HighLevelPlan(
        execution_mode="standard",
        sub_goals=sub_goals,
        suggested_tools={},
        suggested_skills={},
        reasoning="separate vocals from the music track and encode the result",
    )


_PUBLISHED = {
    "audio": [
        {"action": "stem_separation", "tool_name": f"audio-stem{_SEP}separate_audio_stems"},
        {"action": "encode", "tool_name": f"audio-enc{_SEP}encode_audio"},
    ],
}


def _service(plan_reader, client) -> PersistenceService:
    return PersistenceService(oms_client=client, plan_reader=plan_reader)


def _persist(service, session_id="sess-1") -> dict:
    token = set_current_persistence_service(service)
    try:
        out = asyncio.run(persist_workflow._tool_func(session_id=session_id))
    finally:
        reset_current_persistence_service(token)
    assert isinstance(out, str)
    return json.loads(out)


def _confirm(service, template_id, confirm) -> dict:
    token = set_current_persistence_service(service)
    try:
        out = asyncio.run(
            confirm_workflow_activation._tool_func(template_id=template_id, confirm=confirm)
        )
    finally:
        reset_current_persistence_service(token)
    assert isinstance(out, str)
    return json.loads(out)


# ---------------------------------------------------------------------------
# Registry / tool surface
# ---------------------------------------------------------------------------


def test_persistence_tools_registry_exposes_the_two_tools():
    assert len(PERSISTENCE_TOOLS) == 2
    names = {t.tool_spec["name"] for t in PERSISTENCE_TOOLS}
    assert names == {"persist_workflow", "confirm_workflow_activation"}


# ---------------------------------------------------------------------------
# Success reporting (8.2)
# ---------------------------------------------------------------------------


def test_persist_success_reports_saved_draft_created(monkeypatch=None):
    plan = _plan(
        [
            _sub_goal("sg1", ["separate_audio_stems"]),
            _sub_goal("sg2", ["encode_audio"], depends_on=["sg1"]),
        ]
    )
    client = RecordingFakeOMSClient(_PUBLISHED)
    report = _persist(_service(FakePlanReader(plan=plan), client))

    assert report["status"] == "saved_draft"
    assert report["operation"] == "created"
    assert report["template_state"] == "draft"
    assert report["template_id"] and report["template_id"].startswith("wf-")
    assert report["failing_stage"] is None
    assert report["errors"] == []

    mapped_pairs = {(m["service"], m["action"]) for m in report["mapping"]}
    assert mapped_pairs == {("audio", "stem_separation"), ("audio", "encode")}
    assert report["conditions"].get("services") == ["audio"]
    assert client.called_tools.count("save_template") == 1
    assert "activate_template" not in client.called_tools


def test_persist_re_persist_reports_updated_operation():
    plan = _plan([_sub_goal("sg1", ["separate_audio_stems"])])
    client = RecordingFakeOMSClient(_PUBLISHED)
    service = _service(FakePlanReader(plan=plan), client)

    first = _persist(service)
    second = _persist(service)

    assert first["operation"] == "created"
    assert second["operation"] == "updated"
    assert first["template_id"] == second["template_id"]  # deterministic id
    assert list(client.templates) == [first["template_id"]]  # no duplicate


# ---------------------------------------------------------------------------
# Decline activation (9.4)
# ---------------------------------------------------------------------------


def test_confirm_activation_declined_without_confirm_true():
    client = RecordingFakeOMSClient(_PUBLISHED)
    service = _service(FakePlanReader(plan=None), client)

    result = _confirm(service, "wf-abc", confirm=False)

    assert result["error"] == "confirmation_required"
    assert result["template_id"] == "wf-abc"
    assert "activate_template" not in client.called_tools


def test_confirm_activation_declined_when_confirm_is_truthy_but_not_true():
    client = RecordingFakeOMSClient(_PUBLISHED)
    service = _service(FakePlanReader(plan=None), client)

    # A truthy-but-not-strictly-True value must still be refused (guard uses `is True`).
    result = _confirm(service, "wf-abc", confirm=1)

    assert result["error"] == "confirmation_required"
    assert "activate_template" not in client.called_tools


# ---------------------------------------------------------------------------
# Confirm activation (9.5)
# ---------------------------------------------------------------------------


def test_confirm_activation_activates_with_confirm_true():
    client = RecordingFakeOMSClient(_PUBLISHED)
    service = _service(FakePlanReader(plan=None), client)

    result = _confirm(service, "wf-abc", confirm=True)

    assert result["activated"] is True
    assert result["template_id"] == "wf-abc"
    assert client.called_tools == ["activate_template"]


# ---------------------------------------------------------------------------
# failing_stage / atomicity error mapping at the @tool boundary (12.2, 12.3)
# ---------------------------------------------------------------------------


def test_missing_plan_maps_to_failed_report_no_oms_contact():
    client = RecordingFakeOMSClient(_PUBLISHED)
    service = _service(FakePlanReader(error=MissingPlanError("sess-x")), client)

    report = _persist(service, session_id="sess-x")

    assert report["status"] == "failed"
    assert report["failing_stage"] == "read_plan"
    assert report["template_id"] is None
    assert report["errors"][0]["error"] == "MissingPlanError"
    assert report["errors"][0]["session_id"] == "sess-x"
    assert client.calls == []  # no OMS interaction whatsoever


def test_validation_failure_maps_to_failed_report_no_save():
    plan = _plan([_sub_goal("sg1", ["separate_audio_stems"])])
    findings = [{"code": "CYCLE", "message": "dependency cycle detected"}]
    client = RecordingFakeOMSClient(_PUBLISHED, validate_response={"valid": False, "findings": findings})
    report = _persist(_service(FakePlanReader(plan=plan), client))

    assert report["status"] == "failed"
    assert report["failing_stage"] == "validate"
    assert report["errors"][0]["error"] == "TemplateValidationError"
    assert report["errors"][0]["finding"] == findings[0]
    assert "save_template" not in client.called_tools


def test_unmappable_step_maps_to_failed_report_no_save():
    plan = _plan([_sub_goal("sg1", ["no_such_tool"])])
    client = RecordingFakeOMSClient(_PUBLISHED)
    report = _persist(_service(FakePlanReader(plan=plan), client))

    assert report["status"] == "failed"
    assert report["failing_stage"] == "resolve"
    assert report["errors"][0]["error"] == "UnmappableStepsError"
    assert "save_template" not in client.called_tools


# ---------------------------------------------------------------------------
# Unbound service guard
# ---------------------------------------------------------------------------


def test_persist_without_bound_service_raises_runtime_error():
    # No service bound in this context -> _service() raises (mirrors lab tools).
    assert persistence_tool._current_persistence_service.get() is None
    with pytest.raises(RuntimeError):
        asyncio.run(persist_workflow._tool_func(session_id="sess-1"))
