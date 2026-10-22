"""Regression tests: an OMS structured error payload must fail LOUDLY and typed.

Live bug this pins down: the deployed OMS hosted MCP endpoint ran with
``OMS_MCP_READ_ONLY=true``, so its read-only gate refused the mutating
``save_template`` and answered with a structured error envelope::

    {"error": true, "kind": "read_only", "message": "...", "tool": "save_template"}

The MCP call itself succeeded, so the payload decoded as an ordinary ``dict`` and
nobody inspected it: ``persist_workflow`` reported ``status="saved_draft"``,
``operation="created"`` and a ``template_id`` while NOTHING was written to OMS
Core — a silent false success.

Covered here:
- ``OMSMcpClient`` raises :class:`OMSToolError` for a structured error envelope on
  every tool, tagged with the right ``failing_stage`` and ``kind``;
- non-error payloads (``error: false``, no ``error`` key, and the plural
  ``{"valid": true, "errors": []}`` shape) still decode normally;
- a persist run whose ``save_template`` is refused reports
  ``status="failed"`` / ``failing_stage="save"`` with an itemized error and NEVER
  a saved ``template_id``;
- the create-then-update conflict path still works when the conflict arrives as a
  structured error envelope (raised by the chokepoint).
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
from persistence.models import OMSTemplate, OMSToolError, PersistenceError
from persistence.oms_mcp_client import (
    OMS_MCP_TOOLS,
    OMSMcpClient,
    is_structured_oms_error,
)
from persistence.persistence_service import PersistenceService
from persistence.persistence_tool import (
    persist_workflow,
    reset_current_persistence_service,
    set_current_persistence_service,
)
from persistence.save_activate_driver import SaveActivateDriver

_SEP = "___"

_READ_ONLY_ENVELOPE = {
    "error": True,
    "kind": "read_only",
    "message": "server is read-only; tool 'save_template' is not permitted",
    "tool": "save_template",
}


# ---------------------------------------------------------------------------
# Fakes for the MCP boundary
# ---------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, payload) -> None:
        self.content = [_Block(json.dumps(payload))]


class _FakeMCPClient:
    def __init__(self, responses: dict) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def call_tool_sync(self, tool_use_id: str, name: str, arguments: dict = None):
        self.calls.append((name, arguments))
        bare = name.split(_SEP)[-1]
        return _Result(self._responses[bare])


class _FakeGatewayConnection:
    def __init__(self, responses: dict) -> None:
        self.url = "https://oms.example/mcp"
        self._client = _FakeMCPClient(responses)
        self._tools = [
            {"name": f"oms-core{_SEP}{bare}", "description": "", "state": "available"}
            for bare in OMS_MCP_TOOLS
        ]

    def list_tools(self) -> list[dict]:
        return list(self._tools)


def _client_with(**overrides) -> OMSMcpClient:
    responses = {
        "list_integrations": [{"integration_id": "audio"}],
        "get_integration": {"integration_id": "audio", "actions": []},
        "get_action_schema": {"service": "audio", "action": "stem_separation"},
        "validate_template": {"valid": True, "errors": []},
        "save_template": {"template_id": "tpl-1", "operation": "created"},
        "activate_template": {"activated": "tpl-1"},
    }
    responses.update(overrides)
    return OMSMcpClient(_FakeGatewayConnection(responses))


# ---------------------------------------------------------------------------
# OMSMcpClient chokepoint — structured error envelopes raise OMSToolError
# ---------------------------------------------------------------------------


def test_read_only_envelope_on_save_raises_oms_tool_error():
    client = _client_with(save_template=dict(_READ_ONLY_ENVELOPE))
    with pytest.raises(OMSToolError) as exc:
        client.save_template({"template_id": "wf-1"}, update=False)
    err = exc.value
    assert err.failing_stage == "save"
    assert err.kind == "read_only"
    assert err.tool == "save_template"
    # Explicit + actionable: names the tool, the kind, and the read-only cause.
    assert "save_template" in str(err)
    assert "read_only" in str(err)
    assert "OMS_MCP_READ_ONLY=true" in str(err)
    # It is a PersistenceError, so the @tool boundary's broad catch maps it.
    assert isinstance(err, PersistenceError)


def test_error_envelope_on_list_integrations_raises_with_resolve_stage():
    client = _client_with(
        list_integrations={
            "error": True,
            "kind": "client_error",
            "message": "not found",
            "tool": "list_integrations",
            "status": 404,
        }
    )
    with pytest.raises(OMSToolError) as exc:
        client.list_integrations()
    assert exc.value.failing_stage == "resolve"
    assert exc.value.kind == "client_error"


def test_error_envelope_on_activate_raises_with_activate_stage():
    client = _client_with(activate_template=dict(_READ_ONLY_ENVELOPE, tool="activate_template"))
    with pytest.raises(OMSToolError) as exc:
        client.activate_template("wf-1")
    assert exc.value.failing_stage == "activate"
    assert exc.value.kind == "read_only"


def test_string_error_envelope_raises_and_carries_message():
    client = _client_with(save_template={"error": "template rejected by OMS"})
    with pytest.raises(OMSToolError) as exc:
        client.save_template({"template_id": "wf-1"}, update=False)
    assert exc.value.kind is None
    assert "template rejected by OMS" in str(exc.value)


# ---------------------------------------------------------------------------
# ... and legitimate payloads still decode normally (no false positives)
# ---------------------------------------------------------------------------


def test_error_false_payload_returned_normally():
    client = _client_with(save_template={"error": False, "template_id": "tpl-1"})
    assert client.save_template({"template_id": "t"}, update=False) == {
        "error": False,
        "template_id": "tpl-1",
    }


def test_payload_without_error_key_returned_normally():
    client = _client_with()
    assert client.save_template({"template_id": "t"}, update=False)["template_id"] == "tpl-1"


def test_plural_errors_list_is_not_an_error_payload():
    # validate_template's ordinary shape must NEVER be read as a structured error.
    assert is_structured_oms_error({"valid": True, "errors": []}) is False
    client = _client_with(validate_template={"valid": True, "errors": []})
    assert client.validate_template({"template_id": "t"}) == {"valid": True, "errors": []}


def test_populated_plural_errors_list_is_still_not_a_structured_error():
    payload = {"valid": False, "errors": [{"message": "cycle detected"}]}
    assert is_structured_oms_error(payload) is False
    client = _client_with(validate_template=payload)
    assert client.validate_template({"template_id": "t"}) == payload


# ---------------------------------------------------------------------------
# Driver: the create-then-update conflict path still works
# ---------------------------------------------------------------------------


class _ConflictThenUpdateClient:
    """Models the chokepoint: the create raises a conflict envelope as OMSToolError."""

    def __init__(self, *, create_error: OMSToolError) -> None:
        self._create_error = create_error
        self.calls: list[tuple[str, dict]] = []

    def save_template(self, template: dict, update: bool) -> dict:
        self.calls.append(("save_template", {"update": update}))
        if not update:
            raise self._create_error
        return {"template_id": template["template_id"], "updated": True}


def _conflict_error() -> OMSToolError:
    return OMSToolError(
        tool="save_template",
        kind="conflict",
        message="template wf-dup already exists",
        stage="save",
    )


def test_conflict_error_routes_to_update():
    client = _ConflictThenUpdateClient(create_error=_conflict_error())
    driver = SaveActivateDriver(client)

    result = driver.save(_template("wf-dup"))

    assert result.operation == "updated"
    assert result.template_id == "wf-dup"
    assert [c[1]["update"] for c in client.calls] == [False, True]


def test_read_only_error_is_never_treated_as_a_conflict():
    read_only = OMSToolError(
        tool="save_template",
        kind="read_only",
        message="server is read-only",
        stage="save",
    )
    client = _ConflictThenUpdateClient(create_error=read_only)
    driver = SaveActivateDriver(client)

    with pytest.raises(OMSToolError) as exc:
        driver.save(_template("wf-1"))
    assert exc.value.kind == "read_only"
    # No update write was attempted — the refusal is fatal.
    assert [c[1]["update"] for c in client.calls] == [False]


class _UnusableResponseClient:
    def __init__(self, response) -> None:
        self._response = response
        self.calls: list[tuple[str, dict]] = []

    def save_template(self, template: dict, update: bool):
        self.calls.append(("save_template", {"update": update}))
        return self._response


@pytest.mark.parametrize("response", [{}, None, "ok", []])
def test_unusable_save_response_raises_instead_of_false_success(response):
    driver = SaveActivateDriver(_UnusableResponseClient(response))
    with pytest.raises(OMSToolError) as exc:
        driver.save(_template("wf-1"))
    assert exc.value.failing_stage == "save"


def _template(template_id: str) -> OMSTemplate:
    return OMSTemplate(
        template_id=template_id,
        name="test workflow",
        description="test",
        conditions={"services": ["audio"]},
        stages={"validate": {"tasks": []}, "fulfill": {"tasks": []}},
    )


# ---------------------------------------------------------------------------
# Service / @tool level: a refused save reports failed, never saved_draft
# ---------------------------------------------------------------------------


class _RefusingOMSClient:
    """A fake OMS client whose ``save_template`` is refused by the read-only gate."""

    def __init__(self, integrations: dict) -> None:
        self._integrations = integrations
        self.calls: list[tuple[str, dict]] = []

    def list_integrations(self) -> list[dict]:
        self.calls.append(("list_integrations", {}))
        return [{"integration_id": iid} for iid in self._integrations]

    def get_integration(self, integration_id: str) -> dict:
        self.calls.append(("get_integration", {"integration_id": integration_id}))
        entries = self._integrations.get(integration_id, [])
        return {
            "actions": [a["action"] for a in entries],
            "action_tool_names": {a["action"]: a["tool_name"] for a in entries},
        }

    def get_action_schema(self, integration_id: str, action: str) -> dict:
        self.calls.append(("get_action_schema", {"integration_id": integration_id}))
        return {"service": integration_id, "action": action}

    def validate_template(self, template: dict) -> dict:
        self.calls.append(("validate_template", {"template": template}))
        return {"valid": True, "errors": []}

    def save_template(self, template: dict, update: bool) -> dict:
        self.calls.append(("save_template", {"update": update}))
        # The real chokepoint raises for this envelope; model that here.
        raise OMSToolError(
            tool="save_template",
            kind="read_only",
            message=_READ_ONLY_ENVELOPE["message"],
            stage="save",
        )

    def activate_template(self, template_id: str) -> dict:
        self.calls.append(("activate_template", {"template_id": template_id}))
        return {"template_id": template_id, "activated": True}

    @property
    def called_tools(self) -> list[str]:
        return [name for name, _ in self.calls]


class _FakePlanReader:
    def __init__(self, plan: HighLevelPlan) -> None:
        self._plan = plan

    async def read_plan(self, session_id: str) -> HighLevelPlan:
        return self._plan

    def extract_steps(self, plan: HighLevelPlan):
        from persistence.plan_reader import extract_steps

        return extract_steps(plan)


_PUBLISHED = {
    "audio": [
        {"action": "stem_separation", "tool_name": f"audio-stem{_SEP}separate_audio_stems"},
    ],
}


def _plan() -> HighLevelPlan:
    return HighLevelPlan(
        execution_mode="standard",
        sub_goals=[
            SubGoal(
                id="sg1",
                description="do the thing",
                reasoning="separate vocals",
                suggested_tools=["separate_audio_stems"],
                suggested_skills=[],
                depends_on=[],
            )
        ],
        suggested_tools={},
        suggested_skills={},
        reasoning="separate vocals from the music track",
    )


def test_service_reports_failed_at_save_when_write_is_refused():
    client = _RefusingOMSClient(_PUBLISHED)
    service = PersistenceService(oms_client=client, plan_reader=_FakePlanReader(_plan()))

    report = asyncio.run(service.persist("sess-1"))

    assert report.status == "failed"
    assert report.failing_stage == "save"
    assert report.template_id is None
    assert report.operation is None
    assert report.errors
    item = report.errors[0]
    assert item["error"] == "OMSToolError"
    assert item["kind"] == "read_only"
    assert item["tool"] == "save_template"
    assert "OMS_MCP_READ_ONLY=true" in item["message"]


def test_tool_boundary_never_reports_saved_draft_when_write_is_refused():
    client = _RefusingOMSClient(_PUBLISHED)
    service = PersistenceService(oms_client=client, plan_reader=_FakePlanReader(_plan()))

    token = set_current_persistence_service(service)
    try:
        out = asyncio.run(persist_workflow._tool_func(session_id="sess-1"))
    finally:
        reset_current_persistence_service(token)

    report = json.loads(out)
    assert report["status"] == "failed"
    assert report["failing_stage"] == "save"
    assert report["template_id"] is None
    assert report["status"] != "saved_draft"
    assert report["errors"][0]["error"] == "OMSToolError"
    # The write was attempted exactly once and never retried as an update.
    assert client.called_tools.count("save_template") == 1
