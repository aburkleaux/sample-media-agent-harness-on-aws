"""Unit tests for the Validator Gate (task 8.2).

Covers, against a recording fake OMS client that logs every method call:
- ``assert_registry_coverage`` raises ``RegistryCoverageError`` naming the
  missing ``service.action`` ids and NEVER calls ``save_template`` (R7.1, R7.3).
- ``run_oms_validation`` raises ``TemplateValidationError`` carrying the reported
  findings and NEVER calls ``save_template`` (R7.4).
- the happy path passes through (coverage OK + valid verdict) without ever
  touching ``save_template``.
- defensive interpretation of a few ``validate_template`` envelope shapes.

Requirements: 7.1, 7.2, 7.3, 7.4
"""

from __future__ import annotations

import os
import sys

import pytest

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from persistence.models import (
    Capability,
    CapabilityIndex,
    OMSTemplate,
    RegistryCoverageError,
    TemplateValidationError,
)
from persistence.validator_gate import ValidatorGate


# ---------------------------------------------------------------------------
# Recording fake OMS client
# ---------------------------------------------------------------------------


class _RecordingOMSClient:
    """A fake OMSMcpClient that records every call and scripts validate_template.

    Only the two methods the gate may touch are implemented. ``save_template`` is
    present purely so tests can assert it is *never* invoked; calling it records
    the attempt (which the assertions treat as a failure of the atomicity rule).
    """

    def __init__(self, validate_response) -> None:
        self._validate_response = validate_response
        self.calls: list[tuple[str, tuple, dict]] = []

    def validate_template(self, template: dict) -> dict:
        self.calls.append(("validate_template", (template,), {}))
        if isinstance(self._validate_response, Exception):
            raise self._validate_response
        return self._validate_response

    def save_template(self, template: dict, update: bool) -> dict:
        # Recorded so tests can prove the gate never writes.
        self.calls.append(("save_template", (template, update), {}))
        return {"template_id": "should-never-happen"}

    @property
    def called_tools(self) -> list[str]:
        return [name for name, _, _ in self.calls]


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _template(*service_actions: tuple[str, str]) -> OMSTemplate:
    """Build a minimal template whose fulfill stage references the given pairs."""
    tasks = [
        {
            "task_id": f"{svc}__{act}",
            "service": svc,
            "action": act,
            "depends_on": [],
            "required": True,
            "timeout_seconds": 3600,
            "config": {},
        }
        for svc, act in service_actions
    ]
    return OMSTemplate(
        template_id="wf-test",
        name="test",
        description="test",
        conditions={"services": sorted({svc for svc, _ in service_actions})},
        stages={"validate": {"tasks": []}, "fulfill": {"tasks": tasks}},
    )


def _index(*service_actions: tuple[str, str]) -> CapabilityIndex:
    """Build a CapabilityIndex covering exactly the given service.action pairs."""
    caps = [
        Capability(
            service=svc,
            action=act,
            gateway_tool_name=f"t___{svc}_{act}",
            bare_tool_name=f"{svc}_{act}",
        )
        for svc, act in service_actions
    ]
    return CapabilityIndex(capabilities=caps)


# ---------------------------------------------------------------------------
# Registry coverage (R7.1, R7.3)
# ---------------------------------------------------------------------------


def test_coverage_ok_does_not_raise_and_does_not_save():
    client = _RecordingOMSClient({"valid": True, "findings": []})
    gate = ValidatorGate(client)
    template = _template(("audio", "stem_separation"), ("video", "encode"))
    index = _index(("audio", "stem_separation"), ("video", "encode"))

    gate.assert_registry_coverage(template, index)  # no raise

    assert "save_template" not in client.called_tools


def test_coverage_missing_raises_with_itemized_ids_and_no_save():
    client = _RecordingOMSClient({"valid": True, "findings": []})
    gate = ValidatorGate(client)
    template = _template(("audio", "stem_separation"), ("video", "encode"))
    # Registry only knows the audio capability -> video.encode is missing.
    index = _index(("audio", "stem_separation"))

    with pytest.raises(RegistryCoverageError) as exc:
        gate.assert_registry_coverage(template, index)

    assert exc.value.missing == ["video.encode"]
    assert exc.value.failing_stage == "validate"
    # The gate must not have written anything to OMS Core.
    assert "save_template" not in client.called_tools


def test_coverage_reports_every_missing_id():
    client = _RecordingOMSClient({"valid": True, "findings": []})
    gate = ValidatorGate(client)
    template = _template(("a", "x"), ("b", "y"), ("c", "z"))
    index = _index(("b", "y"))  # a.x and c.z missing

    with pytest.raises(RegistryCoverageError) as exc:
        gate.assert_registry_coverage(template, index)

    assert exc.value.missing == ["a.x", "c.z"]


# ---------------------------------------------------------------------------
# OMS validation (R7.2, R7.4)
# ---------------------------------------------------------------------------


def test_valid_template_passes_through_without_save():
    client = _RecordingOMSClient({"valid": True, "findings": []})
    gate = ValidatorGate(client)
    template = _template(("audio", "stem_separation"))

    result = gate.run_oms_validation(template)

    assert result == {"valid": True, "findings": []}
    assert client.called_tools == ["validate_template"]
    assert "save_template" not in client.called_tools


def test_invalid_template_raises_with_findings_and_no_save():
    findings = [{"code": "CYCLE", "message": "dependency cycle detected"}]
    client = _RecordingOMSClient({"valid": False, "findings": findings})
    gate = ValidatorGate(client)
    template = _template(("audio", "stem_separation"))

    with pytest.raises(TemplateValidationError) as exc:
        gate.run_oms_validation(template)

    assert exc.value.findings == findings
    assert exc.value.failing_stage == "validate"
    assert client.called_tools == ["validate_template"]
    assert "save_template" not in client.called_tools


def test_validate_template_receives_template_as_dict():
    client = _RecordingOMSClient({"valid": True, "findings": []})
    gate = ValidatorGate(client)
    template = _template(("audio", "stem_separation"))

    gate.run_oms_validation(template)

    (_, (submitted,), _) = client.calls[0]
    assert submitted == template.to_dict()
    assert isinstance(submitted, dict)


# ---------------------------------------------------------------------------
# Defensive interpretation of validate_template envelope shapes (R7.4)
# ---------------------------------------------------------------------------


def test_errors_list_without_valid_flag_is_treated_invalid():
    client = _RecordingOMSClient({"errors": [{"message": "bad ref"}]})
    gate = ValidatorGate(client)
    template = _template(("audio", "stem_separation"))

    with pytest.raises(TemplateValidationError) as exc:
        gate.run_oms_validation(template)

    assert exc.value.findings == [{"message": "bad ref"}]


def test_empty_response_without_flags_is_treated_valid():
    client = _RecordingOMSClient({})
    gate = ValidatorGate(client)
    template = _template(("audio", "stem_separation"))

    result = gate.run_oms_validation(template)
    assert result == {}


def test_invalid_without_findings_gets_synthetic_finding():
    client = _RecordingOMSClient({"valid": False})
    gate = ValidatorGate(client)
    template = _template(("audio", "stem_separation"))

    with pytest.raises(TemplateValidationError) as exc:
        gate.run_oms_validation(template)

    assert len(exc.value.findings) == 1
    assert "message" in exc.value.findings[0]


def test_non_dict_response_is_treated_invalid():
    client = _RecordingOMSClient(["unexpected"])
    gate = ValidatorGate(client)
    template = _template(("audio", "stem_separation"))

    with pytest.raises(TemplateValidationError):
        gate.run_oms_validation(template)
