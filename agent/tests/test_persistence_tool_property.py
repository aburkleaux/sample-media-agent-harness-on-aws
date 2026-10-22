"""Property tests for the Persistence_Tool @tool confirmation surface (tasks 10.3-10.7).

Five Hypothesis properties (>=100 iterations each) driven **through the @tool
layer / PersistenceService** with an in-memory recording fake OMS client that
logs every call and its args, so atomicity / ordering / no-auto-activation /
report-surfacing / S3-non-exposure are asserted against the recorded call log:

- Property 5  (task 10.3): no partial template — OMS unchanged on any pre-save failure.
- Property 6  (task 10.4): validate-before-write ordering.
- Property 11 (task 10.5): no activation without explicit human confirmation.
- Property 13 (task 10.6): report surfaces mapping, conditions, and state.
- Property 14 (task 10.7): S3 location is never exposed to OMS Core.

Requirements: 1.5, 2.4, 7.1, 7.2, 7.3, 7.4, 8.3, 9.1, 9.2, 9.3, 10.2, 12.1, 12.2
Properties: 5, 6, 11, 13, 14
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from planning.high_level_plan import HighLevelPlan, SubGoal
from persistence.capability_resolver import build_capability_index, resolve
from persistence.models import (
    CapabilityIndex,
    MissingPlanError,
    PlanParseError,
)
from persistence.persistence_service import PersistenceService
from persistence.persistence_tool import (
    confirm_workflow_activation,
    persist_workflow,
    reset_current_persistence_service,
    set_current_persistence_service,
)

_SEP = "___"


# ---------------------------------------------------------------------------
# Recording fake OMS client — records every call + args; scripts the registry
# ---------------------------------------------------------------------------


class RecordingFakeOMSClient:
    """A fake ``OMSMcpClient`` recording every call and its args.

    ``integrations`` maps integration_id -> list of ``{action, tool_name}`` so
    ``build_capability_index`` reads a realistic snapshot. ``validate_template``
    returns the scripted verdict; ``save_template`` models create/update; every
    call is appended to ``calls`` so tests can assert atomicity / ordering /
    no-auto-activation / S3-non-exposure against the recorded log.
    """

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
    """A stand-in ``PlanReader`` returning a scripted plan (or raising).

    Optionally carries a known S3 ``bucket_name`` / ``s3_key`` to model that the
    reader knows the internal S3 location but never surfaces it (Property 14).
    """

    def __init__(self, plan=None, error=None, bucket_name="", s3_key="") -> None:
        self._plan = plan
        self._error = error
        self.bucket_name = bucket_name
        self.s3_key = s3_key

    async def read_plan(self, session_id: str) -> HighLevelPlan:
        if self._error is not None:
            raise self._error
        return self._plan

    def extract_steps(self, plan: HighLevelPlan):
        from persistence.plan_reader import extract_steps

        return extract_steps(plan)


# ---------------------------------------------------------------------------
# Generators / builders
# ---------------------------------------------------------------------------

_word = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=6)


@st.composite
def _bare_token(draw: st.DrawFn) -> str:
    """A realistic bare tool / action token: alnum words joined by ``_``."""
    return "_".join(draw(st.lists(_word, min_size=1, max_size=3)))


def _integrations_from_tokens(tokens: list[str]) -> dict:
    """Publish one uniquely-named Capability per token (bare name == token)."""
    return {
        f"svc{i}": [{"action": f"act{i}", "tool_name": f"tgt{i}{_SEP}{tok}"}]
        for i, tok in enumerate(tokens)
    }


def _sub_goal(sub_goal_id: str, tools: list[str], depends_on=None) -> SubGoal:
    return SubGoal(
        id=sub_goal_id,
        description="separate the vocals and encode",
        reasoning="separate vocals from the backing track",
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


@st.composite
def _mappable_plans(draw: st.DrawFn):
    """A fully-mappable plan plus the registry that maps every step.

    Each unique token becomes exactly one published Capability; each sub-goal
    references one token (so every step maps to exactly one Capability). Edges
    only point at earlier sub-goals so the graph stays acyclic.
    """
    tokens = draw(st.lists(_bare_token(), min_size=1, max_size=5, unique=True))
    integrations = _integrations_from_tokens(tokens)
    ids = [f"sg{i}" for i in range(len(tokens))]
    sub_goals: list[SubGoal] = []
    for i, (sub_goal_id, tok) in enumerate(zip(ids, tokens)):
        earlier = ids[:i]
        depends_on = draw(
            st.lists(st.sampled_from(earlier), min_size=0, max_size=len(earlier), unique=True)
            if earlier
            else st.just([])
        )
        sub_goals.append(_sub_goal(sub_goal_id, [tok], depends_on=depends_on))
    return _plan(sub_goals), integrations


def _run_persist(service, session_id="sess-1") -> dict:
    """Bind the service, drive the ``persist_workflow`` @tool, return the report dict."""
    token = set_current_persistence_service(service)
    try:
        out = asyncio.run(persist_workflow._tool_func(session_id=session_id))
    finally:
        reset_current_persistence_service(token)
    return json.loads(out)


def _run_confirm(service, template_id, confirm) -> dict:
    """Bind the service, drive the ``confirm_workflow_activation`` @tool, return the result dict."""
    token = set_current_persistence_service(service)
    try:
        out = asyncio.run(
            confirm_workflow_activation._tool_func(template_id=template_id, confirm=confirm)
        )
    finally:
        reset_current_persistence_service(token)
    return json.loads(out)


# ---------------------------------------------------------------------------
# Property 5: No partial template — OMS unchanged on any pre-save failure — 10.3
# ---------------------------------------------------------------------------


@st.composite
def _pre_save_failures(draw: st.DrawFn):
    """Build a service whose pipeline fails before the write, plus its client.

    Covers every pre-save failure mode: read (missing / parse), resolve
    (unmappable / unpublished), registry coverage, and OMS validation. Returns
    ``(service, client, expected_stage)``.
    """
    mode = draw(
        st.sampled_from(
            ["read_missing", "read_parse", "unmappable", "unpublished", "registry", "validate"]
        )
    )

    if mode == "read_missing":
        client = RecordingFakeOMSClient(_integrations_from_tokens(draw(st.lists(_bare_token(), max_size=3, unique=True))))
        reader = FakePlanReader(error=MissingPlanError("sess-x"))
        service = PersistenceService(oms_client=client, plan_reader=reader)
        return service, client, "read_plan"

    if mode == "read_parse":
        client = RecordingFakeOMSClient({})
        reader = FakePlanReader(error=PlanParseError("bad plan", session_id="sess-x"))
        service = PersistenceService(oms_client=client, plan_reader=reader)
        return service, client, "read_plan"

    if mode == "unmappable":
        # Empty registry -> the referenced tool matches no Capability at all.
        client = RecordingFakeOMSClient({})
        plan = _plan([_sub_goal("sg0", [draw(_bare_token())])])
        service = PersistenceService(oms_client=client, plan_reader=FakePlanReader(plan=plan))
        return service, client, "resolve"

    if mode == "unpublished":
        # Registry has the capability but publishes no tool_name (bridge gap).
        client = RecordingFakeOMSClient({"audio": [{"action": "mix", "tool_name": None}]})
        plan = _plan([_sub_goal("sg0", ["mix"])])
        service = PersistenceService(oms_client=client, plan_reader=FakePlanReader(plan=plan))
        return service, client, "resolve"

    if mode == "registry":
        # Resolver maps against the full index, but the coverage index is empty.
        plan, integrations = draw(_mappable_plans())
        client = RecordingFakeOMSClient(integrations)
        full_index = build_capability_index(client)
        service = PersistenceService(
            oms_client=client,
            plan_reader=FakePlanReader(plan=plan),
            build_capability_index_fn=lambda _c: CapabilityIndex(capabilities=[]),
            resolve_fn=lambda steps, _index: resolve(steps, full_index),
        )
        return service, client, "validate"

    # validate: OMS reports the assembled template invalid.
    plan, integrations = draw(_mappable_plans())
    client = RecordingFakeOMSClient(
        integrations, validate_response={"valid": False, "findings": [{"code": "CYCLE"}]}
    )
    service = PersistenceService(oms_client=client, plan_reader=FakePlanReader(plan=plan))
    return service, client, "validate"


# Feature: oms-workflow-persistence, Property 5: No partial template — OMS unchanged on any pre-save failure
@settings(max_examples=150)
@given(_pre_save_failures())
def test_no_partial_template_on_any_pre_save_failure(case) -> None:
    """Validates: Requirements 2.4, 7.1, 7.3, 7.4, 12.2

    Any failure before a successful ``save_template`` leaves OMS Core unchanged:
    the report is ``failed`` at the right stage and the recording fake never logs
    a ``save_template`` call.
    """
    service, client, expected_stage = case

    report = _run_persist(service, session_id="sess-x")

    assert report["status"] == "failed"
    assert report["failing_stage"] == expected_stage
    assert report["template_id"] is None
    assert report["errors"], "a failure must itemize at least one error"
    # The single write is never reached on any pre-save failure.
    assert "save_template" not in client.called_tools


# ---------------------------------------------------------------------------
# Property 6: Validate-before-write ordering — 10.4
# ---------------------------------------------------------------------------


# Feature: oms-workflow-persistence, Property 6: Validate-before-write ordering
@settings(max_examples=150)
@given(_mappable_plans(), st.booleans())
def test_validate_precedes_write_and_no_write_on_validation_failure(case, fail_validation) -> None:
    """Validates: Requirements 7.2

    ``validate_template`` is invoked before ``save_template``; if validation
    fails, ``save_template`` is not invoked at all.
    """
    plan, integrations = case
    validate_response = (
        {"valid": False, "findings": [{"code": "REF"}]} if fail_validation else None
    )
    client = RecordingFakeOMSClient(integrations, validate_response=validate_response)
    service = PersistenceService(oms_client=client, plan_reader=FakePlanReader(plan=plan))

    report = _run_persist(service)

    tools = client.called_tools
    assert "validate_template" in tools
    if fail_validation:
        assert report["status"] == "failed"
        assert report["failing_stage"] == "validate"
        assert "save_template" not in tools
    else:
        assert report["status"] == "saved_draft"
        assert "save_template" in tools
        assert tools.index("validate_template") < tools.index("save_template")


# ---------------------------------------------------------------------------
# Property 11: No activation without explicit human confirmation — 10.5
# ---------------------------------------------------------------------------


# Feature: oms-workflow-persistence, Property 11: No activation without explicit human confirmation
@settings(max_examples=150)
@given(_mappable_plans())
def test_persist_workflow_never_activates(case) -> None:
    """Validates: Requirements 8.3, 9.1

    A successful ``persist_workflow`` execution never calls ``activate_template``.
    """
    plan, integrations = case
    client = RecordingFakeOMSClient(integrations)
    service = PersistenceService(oms_client=client, plan_reader=FakePlanReader(plan=plan))

    report = _run_persist(service)

    assert report["status"] == "saved_draft"
    assert "activate_template" not in client.called_tools


# Values that are not strictly ``True`` must never activate; only ``True`` does.
_confirm_values = st.sampled_from([True, False, None, 0, 1, "true", "false", "", [], {}, 1.0])


# Feature: oms-workflow-persistence, Property 11: No activation without explicit human confirmation
@settings(max_examples=150)
@given(_confirm_values)
def test_confirm_activation_only_activates_when_strictly_true(confirm) -> None:
    """Validates: Requirements 9.1, 9.3

    ``confirm_workflow_activation`` activates iff ``confirm`` is strictly ``True``;
    any other value returns ``confirmation_required`` and never activates.
    """
    client = RecordingFakeOMSClient({})
    service = PersistenceService(oms_client=client, plan_reader=FakePlanReader(plan=None))

    result = _run_confirm(service, "wf-xyz", confirm)

    if confirm is True:
        assert result.get("activated") is True
        assert result.get("template_id") == "wf-xyz"
        assert client.called_tools == ["activate_template"]
    else:
        assert result.get("error") == "confirmation_required"
        assert "activate_template" not in client.called_tools


# ---------------------------------------------------------------------------
# Property 13: Report surfaces mapping, conditions, and state — 10.6
# ---------------------------------------------------------------------------


# Feature: oms-workflow-persistence, Property 13: Report surfaces mapping, conditions, and state
@settings(max_examples=150)
@given(_mappable_plans())
def test_report_surfaces_mapping_conditions_and_draft_state(case) -> None:
    """Validates: Requirements 9.2, 10.2, 12.1

    Every successful report carries the saved ``template_id``, the full
    capability→``service.action`` mapping, the scoped ``conditions``, and
    ``template_state == "draft"``.
    """
    plan, integrations = case
    client = RecordingFakeOMSClient(integrations)
    service = PersistenceService(oms_client=client, plan_reader=FakePlanReader(plan=plan))

    report = _run_persist(service)

    assert report["status"] == "saved_draft"
    assert report["template_id"]
    assert report["template_state"] == "draft"

    # One mapping entry per plan step, each fully itemized as a service.action.
    n_steps = sum(len(sg.suggested_tools) for sg in plan.sub_goals)
    assert len(report["mapping"]) == n_steps
    for entry in report["mapping"]:
        assert set(entry) >= {"sub_goal_id", "bare_tool", "service", "action", "task_id"}

    # Conditions are scoped (non-empty) and mention the participating services.
    assert report["conditions"]
    assert report["conditions"].get("services")


# ---------------------------------------------------------------------------
# Property 14: S3 location is never exposed to OMS Core — 10.7
# ---------------------------------------------------------------------------


@st.composite
def _mappable_plans_with_s3(draw: st.DrawFn):
    """A mappable plan/registry plus a distinctive, known S3 bucket + key."""
    plan, integrations = draw(_mappable_plans())
    suffix = draw(st.text(alphabet="ABCDEF0123456789", min_size=6, max_size=10))
    bucket = f"HARNESS-SECRET-BUCKET-{suffix}"
    key = f"plans/session-{suffix}/planning/output.json"
    return plan, integrations, bucket, key


# Feature: oms-workflow-persistence, Property 14: S3 location is never exposed to OMS Core
@settings(max_examples=150)
@given(_mappable_plans_with_s3())
def test_s3_location_never_reaches_any_oms_call(case) -> None:
    """Validates: Requirements 1.5

    No argument of any recorded OMS MCP call contains the harness S3 bucket name
    or object key — the S3 location is used only as an internal input source.
    """
    plan, integrations, bucket, key = case
    client = RecordingFakeOMSClient(integrations)
    reader = FakePlanReader(plan=plan, bucket_name=bucket, s3_key=key)
    service = PersistenceService(oms_client=client, plan_reader=reader)

    report = _run_persist(service)
    assert report["status"] == "saved_draft"

    # Scan every argument of every recorded OMS call for the S3 location.
    for name, args in client.calls:
        blob = json.dumps(args, default=str)
        assert bucket not in blob, f"{name} leaked the S3 bucket name"
        assert key not in blob, f"{name} leaked the S3 object key"
