"""`agent-registry` client wiring and record identity (migration Tasks 3, 8).

Two things this guards that would otherwise fail only at deploy time:

* **The vendored service models still describe the operations we call.** The
  projection Lambda runs on a botocore that predates the `agent-registry`
  namespace and loads these models off ``AWS_DATA_PATH``. A stale copy would
  surface as ``UnknownServiceError`` on the first real event.
* **`recordId` is derived from `recordArn`.** ``CreateRegistryRecord`` returns
  ``recordArn`` and ``status`` but NOT ``recordId``, while every follow-up call
  (submit / update-status / get) takes ``recordId``. Reading a non-existent
  ``recordId`` key off the create response is an easy and silent mistake.
"""

from __future__ import annotations

import pytest

from capability import naming, registry


# ---------------------------------------------------------------------------
# Vendored models (Task 3)
# ---------------------------------------------------------------------------


def test_vendored_models_are_present_for_both_planes():
    assert registry.VENDORED_MODEL_PATH.is_dir()
    for service in (registry.DATA_PLANE_SERVICE, registry.CONTROL_PLANE_SERVICE):
        service_dir = registry.VENDORED_MODEL_PATH / service
        assert service_dir.is_dir(), f"missing vendored model for {service}"
        models = list(service_dir.glob("*/service-2.json*"))
        assert models, f"no service-2.json under {service_dir}"


def test_model_path_is_registered_on_aws_data_path():
    """Must happen before botocore builds its loader, i.e. at import time."""
    import os

    entries = os.environ.get("AWS_DATA_PATH", "").split(os.pathsep)
    assert str(registry.VENDORED_MODEL_PATH) in entries


def test_control_plane_exposes_every_operation_the_projector_calls():
    boto3 = pytest.importorskip("boto3")
    client = registry.control_client("us-west-2")
    operations = set(client.meta.service_model.operation_names)
    required = {
        "CreateRegistry",
        "GetRegistry",
        "ListRegistries",
        "CreateRegistryRecord",
        "GetRegistryRecord",
        "ListRegistryRecords",
        "UpdateRegistryRecord",
        "UpdateRegistryRecordStatus",
        "SubmitRegistryRecordForApproval",
    }
    assert required <= operations, sorted(required - operations)


def test_data_plane_exposes_every_operation_the_capability_api_calls():
    pytest.importorskip("boto3")
    client = registry.data_client("us-west-2")
    operations = set(client.meta.service_model.operation_names)
    assert operations >= {
        "ListDiscoverableRegistryRecords",
        "SearchDiscoverableRegistryRecords",
        "BatchGetDiscoverableRegistryRecord",
    }


def test_approve_all_is_a_real_enum_value():
    """`APPROVE_ALL` is asserted against the model, not assumed.

    An invalid auto-approval rule would leave every projected record sitting in
    PENDING_APPROVAL — a silent, discoverability-shaped failure.
    """
    pytest.importorskip("boto3")
    model = registry.control_client("us-west-2").meta.service_model
    rules = (
        model.operation_model("CreateRegistry")
        .input_shape.members["approvalConfiguration"]
        .members["autoApprovalRules"]
    )
    assert "APPROVE_ALL" in (rules.member.enum or [])


def test_update_registry_record_status_requires_a_status_reason():
    """Omitting it is a ParamValidationError at the deprecation call site."""
    pytest.importorskip("boto3")
    model = registry.control_client("us-west-2").meta.service_model
    shape = model.operation_model("UpdateRegistryRecordStatus").input_shape
    assert "statusReason" in shape.required_members


def test_create_registry_record_does_not_return_a_record_id():
    """The reason `record_id_from_arn` exists at all."""
    pytest.importorskip("boto3")
    model = registry.control_client("us-west-2").meta.service_model
    output = model.operation_model("CreateRegistryRecord").output_shape
    assert "recordArn" in output.members
    assert "recordId" not in output.members


def test_list_discoverable_records_returns_no_descriptors():
    """Which is why the list path never tries to read descriptor bodies.

    Descriptors come from Search or BatchGet; expecting them here would make
    every list row look empty.
    """
    pytest.importorskip("boto3")
    model = registry.data_client("us-west-2").meta.service_model
    listed = model.operation_model("ListDiscoverableRegistryRecords").output_shape
    record = listed.members["registryRecords"].member
    assert "descriptors" not in record.members
    batched = model.operation_model("BatchGetDiscoverableRegistryRecord").output_shape
    assert "descriptors" in batched.members["registryRecords"].member.members


# ---------------------------------------------------------------------------
# Record id derivation
# ---------------------------------------------------------------------------


def test_record_id_is_the_arn_tail():
    arn = (
        "arn:aws:agent-registry:us-west-2:100000000001:registry/reg-1/record/rec-42"
    )
    assert registry.record_id_from_arn(arn) == "rec-42"


@pytest.mark.parametrize("arn", ["", "not-an-arn", "arn:aws:agent-registry:::registry/"])
def test_record_id_from_a_malformed_arn_raises(arn):
    with pytest.raises(ValueError):
        registry.record_id_from_arn(arn)


# ---------------------------------------------------------------------------
# Deterministic identity (R4)
# ---------------------------------------------------------------------------


def test_record_name_is_derived_from_canonical_identity():
    assert naming.record_name("skill", "asset-123") == "mediaworks/skill/asset-123"
    assert (
        naming.builtin_record_name("specialty-agent", "task-agent")
        == "mediaworks/builtin/specialty-agent/task-agent"
    )
    assert (
        naming.mcp_endpoint_record_name("gw-abc") == "mediaworks/mcp-endpoint/gw-abc"
    )


def test_record_name_round_trips():
    for category, asset_id in (("skill", "asset-1"), ("workflow", "wf-2")):
        parsed = naming.parse_record_name(naming.record_name(category, asset_id))
        assert parsed == (category, asset_id, False)
    parsed = naming.parse_record_name(
        naming.builtin_record_name("skill", "video-processing")
    )
    assert parsed == ("skill", "video-processing", True)


def test_foreign_record_names_are_skipped_not_guessed_at():
    """The registry is per-environment but not exclusively ours."""
    for name in ("", "other/skill/x", "mediaworks", "mediaworks/skill", "a/b/c/d/e"):
        assert naming.parse_record_name(name) is None


def test_a_segment_cannot_inject_a_path_separator():
    """An asset id containing `/` would otherwise forge a builtin identity."""
    with pytest.raises(ValueError):
        naming.record_name("skill", "a/b")
    with pytest.raises(ValueError):
        naming.record_name("../builtin", "x")


def test_record_version_formats_and_round_trips():
    assert naming.record_version(2) == "v2"
    assert naming.record_version("2026.08.13") == "v2026.08.13"
    assert naming.record_version("v3") == "v3"
    assert naming.parse_record_version("v2") == 2
    assert naming.parse_record_version("v2026.08.13") == "2026.08.13"
    assert naming.parse_record_version(None) is None
    assert naming.parse_record_version("garbage") is None


@pytest.mark.parametrize("version", [0, -1, True, ""])
def test_invalid_record_versions_are_rejected(version):
    with pytest.raises(ValueError):
        naming.record_version(version)


def test_qualified_tool_name_uses_the_gateway_convention():
    """`___` is the AgentCore convention, relied on across the agent modules."""
    assert (
        naming.qualified_tool_name("media-processing", "process_media")
        == "media-processing___process_media"
    )
    assert naming.split_qualified_tool_name("media-processing___process_media") == (
        "media-processing",
        "process_media",
    )
    assert naming.split_qualified_tool_name("process_media") == (None, "process_media")


def test_summary_id_distinguishes_repeated_bare_tool_names():
    first = naming.summary_id(
        category="tool", gateway_id="gw", target_id="t1", bare_tool_name="get_job_status"
    )
    second = naming.summary_id(
        category="tool", gateway_id="gw", target_id="t2", bare_tool_name="get_job_status"
    )
    assert first != second


def test_summary_id_requires_an_identity():
    with pytest.raises(ValueError):
        naming.summary_id(category="skill")


# ---------------------------------------------------------------------------
# Record lookup helpers
# ---------------------------------------------------------------------------


class _PagingControl:
    def __init__(self, pages):
        self._pages = pages
        self.calls = 0

    def list_registry_records(self, **kwargs):
        page = self._pages[self.calls]
        self.calls += 1
        return page


def test_find_record_matches_on_name_and_version_across_pages():
    """`ListRegistryRecords` filters on name but not recordVersion.

    So the version match has to happen client-side, and it has to survive
    pagination — otherwise a lineage with more than a page of versions would
    look like a missing record and get a duplicate created.
    """
    control = _PagingControl(
        [
            {
                "registryRecords": [
                    {"recordId": "r1", "name": "mediaworks/skill/a", "recordVersion": "v1"}
                ],
                "nextToken": "t",
            },
            {
                "registryRecords": [
                    {"recordId": "r2", "name": "mediaworks/skill/a", "recordVersion": "v2"}
                ]
            },
        ]
    )
    found = registry.find_record(
        control, "reg-1", name="mediaworks/skill/a", record_version="v2"
    )
    assert found["recordId"] == "r2"


def test_find_record_returns_none_when_absent():
    control = _PagingControl([{"registryRecords": []}])
    assert (
        registry.find_record(
            control, "reg-1", name="mediaworks/skill/a", record_version="v9"
        )
        is None
    )


def test_discoverable_listing_is_bounded():
    """One runaway registry must not turn a 2s category read into a page walk."""

    class Endless:
        def list_discoverable_registry_records(self, **kwargs):
            return {
                "registryRecords": [{"recordId": "r"} for _ in range(50)],
                "nextToken": "more",
            }

    records = list(
        registry.iter_discoverable_records(Endless(), "reg-1", max_records=120)
    )
    assert len(records) == 120
