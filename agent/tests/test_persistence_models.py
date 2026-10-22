"""Tests for persistence data models and typed errors (task 1.2).

Covers:
- ResolutionReport.ok truth table (mapped-only True; each failure bucket False)
- Typed error classes carry their itemized step/capability payloads
- failing_stage is declared per error class (design error table / R12.2)
- to_dict / from_dict round-trips for the I/O-boundary dataclasses

Requirements: 2.4, 12.3
"""

from __future__ import annotations

import os
import sys

import pytest

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from persistence.models import (
    ActivationNotConfirmedError,
    AmbiguousStepError,
    Capability,
    CapabilityIndex,
    EmptyConditionsError,
    MappedStep,
    MissingPlanError,
    OMSTask,
    OMSTemplate,
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


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _step(sub_goal_id: str = "sg1", bare_tool: str | None = "separate_audio_stems") -> WorkflowStep:
    return WorkflowStep(
        sub_goal_id=sub_goal_id,
        bare_tool=bare_tool,
        depends_on=[],
        description="",
    )


def _capability(service: str = "audio", action: str = "stem_separation") -> Capability:
    return Capability(
        service=service,
        action=action,
        gateway_tool_name=f"audio-stem___{action}",
        bare_tool_name=action,
    )


def _mapped(step: WorkflowStep) -> MappedStep:
    return MappedStep(step=step, service="audio", action="stem_separation", task_id="audio__stem_separation")


# ---------------------------------------------------------------------------
# ResolutionReport.ok truth table
# ---------------------------------------------------------------------------


def test_ok_true_when_only_mapped():
    report = ResolutionReport(mapped=[_mapped(_step())])
    assert report.ok is True


def test_ok_true_when_completely_empty():
    # No steps at all is trivially ok — nothing failed to resolve.
    assert ResolutionReport().ok is True


def test_ok_false_when_unmappable_present():
    report = ResolutionReport(mapped=[_mapped(_step())], unmappable=[_step("sg2", None)])
    assert report.ok is False


def test_ok_false_when_ambiguous_present():
    report = ResolutionReport(ambiguous=[(_step("sg2"), [_capability(), _capability("audio", "other")])])
    assert report.ok is False


def test_ok_false_when_unpublished_present():
    report = ResolutionReport(unpublished=[(_step("sg2"), _capability())])
    assert report.ok is False


@pytest.mark.parametrize(
    "unmappable, ambiguous, unpublished, expected",
    [
        (False, False, False, True),
        (True, False, False, False),
        (False, True, False, False),
        (False, False, True, False),
        (True, True, True, False),
    ],
)
def test_ok_truth_table(unmappable, ambiguous, unpublished, expected):
    report = ResolutionReport(
        mapped=[_mapped(_step())],
        unmappable=[_step("u", None)] if unmappable else [],
        ambiguous=[(_step("a"), [_capability()])] if ambiguous else [],
        unpublished=[(_step("p"), _capability())] if unpublished else [],
    )
    assert report.ok is expected


# ---------------------------------------------------------------------------
# Error classes carry itemized payloads and declare failing_stage
# ---------------------------------------------------------------------------


def test_missing_plan_error_carries_session_id():
    err = MissingPlanError("sess-123")
    assert err.session_id == "sess-123"
    assert err.failing_stage == "read_plan"
    assert isinstance(err, PersistenceError)
    assert "sess-123" in str(err)


def test_plan_parse_error_carries_message_and_session():
    err = PlanParseError("bad json", session_id="sess-9")
    assert err.message == "bad json"
    assert err.session_id == "sess-9"
    assert err.failing_stage == "read_plan"


def test_unmappable_steps_error_carries_steps():
    steps = [_step("sg1", None), _step("sg2", "unknown_tool")]
    err = UnmappableStepsError(steps)
    assert err.steps == steps
    assert err.failing_stage == "resolve"
    assert "sg1" in str(err) and "sg2" in str(err)


def test_ambiguous_step_error_carries_step_and_conflicts():
    step = _step("sg1")
    caps = [_capability("audio", "stem_separation"), _capability("audio2", "stem_separation")]
    err = AmbiguousStepError([(step, caps)])
    assert err.ambiguous[0][0] is step
    assert err.ambiguous[0][1] == caps
    assert err.failing_stage == "resolve"
    assert "sg1" in str(err)


def test_unpublished_tool_name_error_carries_step_and_capability():
    step = _step("sg1")
    cap = Capability(service="audio", action="stem_separation")
    err = UnpublishedToolNameError([(step, cap)])
    assert err.unpublished[0] == (step, cap)
    assert err.failing_stage == "resolve"
    assert "audio.stem_separation" in str(err)


def test_empty_conditions_error_defaults():
    err = EmptyConditionsError()
    assert err.failing_stage == "assemble"
    assert isinstance(err, PersistenceError)


def test_registry_coverage_error_carries_missing():
    err = RegistryCoverageError(["audio.stem_separation", "video.shot_segmentation"])
    assert err.missing == ["audio.stem_separation", "video.shot_segmentation"]
    assert err.failing_stage == "validate"
    assert "video.shot_segmentation" in str(err)


def test_template_validation_error_carries_findings():
    findings = [{"code": "CYCLE", "detail": "t1 -> t2 -> t1"}]
    err = TemplateValidationError(findings)
    assert err.findings == findings
    assert err.failing_stage == "validate"


def test_activation_not_confirmed_error_carries_template_id():
    err = ActivationNotConfirmedError("tmpl-abc")
    assert err.template_id == "tmpl-abc"
    assert err.failing_stage == "activate"
    assert "tmpl-abc" in str(err)


def test_oms_unavailable_error_records_stage():
    err = OMSUnavailableError("gateway timeout", stage="validate")
    assert err.message == "gateway timeout"
    assert err.failing_stage == "validate"
    # Stage is optional — transport can fail anywhere.
    assert OMSUnavailableError("boom").failing_stage is None


def test_all_errors_are_persistence_errors():
    for err in (
        MissingPlanError("s"),
        PlanParseError("m"),
        UnmappableStepsError([]),
        AmbiguousStepError([]),
        UnpublishedToolNameError([]),
        EmptyConditionsError(),
        RegistryCoverageError([]),
        TemplateValidationError([]),
        ActivationNotConfirmedError("t"),
        OMSUnavailableError("m"),
    ):
        assert isinstance(err, PersistenceError)


# ---------------------------------------------------------------------------
# to_dict / from_dict round-trips at the I/O boundaries
# ---------------------------------------------------------------------------


def test_capability_round_trip():
    cap = _capability()
    assert Capability.from_dict(cap.to_dict()) == cap
    assert cap.service_action == "audio.stem_separation"


def test_capability_index_matches_exact_only():
    cap = _capability()
    index = CapabilityIndex(capabilities=[cap], lookup={cap.bare_tool_name: [cap]})
    assert index.matches("stem_separation") == [cap]
    assert index.matches("stem") == []  # no substring match
    assert index.matches(None) == []


def test_workflow_step_round_trip():
    step = WorkflowStep("sg1", "tool", depends_on=["sg0"], description="d")
    assert WorkflowStep.from_dict(step.to_dict()) == step


def test_oms_task_round_trip():
    task = OMSTask(
        task_id="audio__stem_separation",
        service="audio",
        action="stem_separation",
        depends_on=["video__shot_segmentation"],
        required=True,
        timeout_seconds=300,
        config={"k": "v"},
    )
    assert OMSTask.from_dict(task.to_dict()) == task


def test_oms_template_round_trip():
    tmpl = OMSTemplate(
        template_id="tmpl-1",
        name="n",
        description="d",
        conditions={"orderType": "audio"},
        stages={"validate": {"tasks": []}, "fulfill": {"tasks": []}},
    )
    assert OMSTemplate.from_dict(tmpl.to_dict()) == tmpl


def test_persistence_report_round_trip():
    report = PersistenceReport(
        status="saved_draft",
        template_id="tmpl-1",
        operation="created",
        mapping=[{"sub_goal_id": "sg1"}],
        conditions={"orderType": "audio"},
        template_state="draft",
    )
    assert PersistenceReport.from_dict(report.to_dict()) == report


def test_mapped_step_to_dict_shape():
    m = _mapped(_step())
    assert m.to_dict() == {
        "sub_goal_id": "sg1",
        "bare_tool": "separate_audio_stems",
        "service": "audio",
        "action": "stem_separation",
        "task_id": "audio__stem_separation",
    }
