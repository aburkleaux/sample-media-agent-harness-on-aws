"""The projector's Registry calls must validate against the real service model.

Every other test in this suite talks to a hand-written fake, and a fake is only
as good as the constraints it copies. Two production-breaking bugs got through
precisely there:

* `description` was sent as `""` — rejected by `min length: 1`,
* `UpdateRegistryRecord` was sent create-shaped values — rejected because update
  takes a patch wrapper (`{"optionalValue": ...}`) at every level.

Both are *client-side* botocore validation failures. No request is sent, so
there is nothing in CloudTrail and no service behaviour to mock. The only honest
check is to run botocore's own `ParamValidator` against the vendored
`agent-registry-control` model — the same model, and the same validator, that the
deployed Lambda uses. No credentials, no network.

This is the test that would have caught both bugs before deploy.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from botocore.model import ServiceModel  # noqa: E402
from botocore.validate import ParamValidator  # noqa: E402

from capability import registry  # noqa: E402
from capability.descriptors import build_descriptor  # noqa: E402

_MODEL_PATH = (
    registry.VENDORED_MODEL_PATH
    / "agent-registry-control"
    / "2025-12-01"
    / "service-2.json.gz"
)


@pytest.fixture(scope="module")
def service_model() -> ServiceModel:
    with gzip.open(_MODEL_PATH, "rt", encoding="utf-8") as handle:
        return ServiceModel(json.load(handle), service_name="agent-registry-control")


def assert_valid(service_model: ServiceModel, operation: str, params: dict[str, Any]):
    """Fail with botocore's own report if `params` would be rejected."""
    shape = service_model.operation_model(operation).input_shape
    report = ParamValidator().validate(params, shape)
    assert not report.has_errors(), f"{operation}: {report.generate_report()}"


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "assetId": "asset-123",
        "category": "specialty-agent",
        "name": "registry-merge-test",
        "scope": "org",
        "version": 4,
        "s3Prefix": "org/mediaworks/published/specialty-agents/asset-123/v4/",
        "updatedAt": 1786000000000,
        "persona": "You are a media operations assistant.",
        "toolCollections": ["tc-1", "tc-2"],
        "compatibleSkills": [],
    }
    row.update(overrides)
    return row


# The three descriptor kinds the projector emits, with the category that
# produces each. `mcp-endpoint` is built by a different entry point and is
# covered through `to_update_patch` directly below.
_ASSET_CATEGORIES = ["specialty-agent", "tool-collection", "workflow"]


def test_the_model_is_actually_loadable(service_model):
    """Guards the fixture: a missing model would make every test below vacuous."""
    assert "CreateRegistryRecord" in service_model.operation_names
    assert "UpdateRegistryRecord" in service_model.operation_names


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", _ASSET_CATEGORIES)
def test_create_params_validate(service_model, category):
    descriptor = build_descriptor(
        category, _row(category=category), artifacts_bucket="bucket"
    )
    assert_valid(
        service_model,
        "CreateRegistryRecord",
        {
            "registryId": "EXAMPLEregistry1",
            "name": f"mediaworks/{category}/asset-123",
            "displayName": "registry-merge-test",
            "description": "registry-merge-test — org-shared agent",
            "recordType": "AGENT",
            "recordVersion": "v4",
            "descriptors": descriptor,
        },
    )


def test_an_empty_description_is_rejected_by_the_real_model(service_model):
    """The first production bug, reproduced against the model that rejected it."""
    shape = service_model.operation_model("CreateRegistryRecord").input_shape
    report = ParamValidator().validate(
        {
            "registryId": "r",
            "name": "n",
            "description": "",
            "recordType": "AGENT",
            "recordVersion": "v1",
            "descriptors": {"custom": {"data": "x"}},
        },
        shape,
    )
    assert report.has_errors()
    assert "description" in report.generate_report()


# ---------------------------------------------------------------------------
# Update — the patch-wrapper shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", _ASSET_CATEGORIES)
def test_update_params_validate_when_wrapped(service_model, category):
    descriptor = build_descriptor(
        category, _row(category=category), artifacts_bucket="bucket"
    )
    assert_valid(
        service_model,
        "UpdateRegistryRecord",
        {
            "registryId": "EXAMPLEregistry1",
            "recordId": "769KqGqkDozw",
            "displayName": registry.to_update_patch("registry-merge-test"),
            "description": registry.to_update_patch("a description"),
            "descriptors": registry.to_update_patch(descriptor),
        },
    )


@pytest.mark.parametrize("category", _ASSET_CATEGORIES)
def test_update_params_are_rejected_when_NOT_wrapped(service_model, category):
    """The second production bug, reproduced.

    This is what the projector sent before the fix. If this ever stops failing,
    the model has changed to accept create shapes on update and
    `to_update_patch` should be revisited.
    """
    descriptor = build_descriptor(
        category, _row(category=category), artifacts_bucket="bucket"
    )
    shape = service_model.operation_model("UpdateRegistryRecord").input_shape
    report = ParamValidator().validate(
        {
            "registryId": "r",
            "recordId": "rec-1",
            "displayName": "registry-merge-test",
            "description": "a description",
            "descriptors": descriptor,
        },
        shape,
    )
    assert report.has_errors()
    rendered = report.generate_report()
    assert "displayName" in rendered and "description" in rendered


def test_skill_descriptor_with_nested_additional_data_validates(service_model):
    """`agentSkillsDefinition` nests `additionalData`, so the wrapper recurses."""
    descriptor = build_descriptor(
        "skill",
        _row(category="skill", name="video-processing"),
        artifacts_bucket="bucket",
        skill_md="---\nname: video-processing\ndescription: d\n---\nBody.\n",
    )
    assert "additionalData" in descriptor["agentSkillsDefinition"]
    assert_valid(
        service_model,
        "UpdateRegistryRecord",
        {
            "registryId": "r",
            "recordId": "rec-1",
            "descriptors": registry.to_update_patch(descriptor),
        },
    )


# ---------------------------------------------------------------------------
# to_update_patch itself
# ---------------------------------------------------------------------------


def test_to_update_patch_wraps_every_level():
    assert registry.to_update_patch("x") == {"optionalValue": "x"}
    assert registry.to_update_patch({"custom": {"data": "x"}}) == {
        "optionalValue": {
            "custom": {"optionalValue": {"data": {"optionalValue": "x"}}}
        }
    }


def test_to_update_patch_leaves_non_mapping_leaves_alone():
    # Lists are values, not nested field structures — wrapping their elements
    # would invent a shape the model does not have.
    assert registry.to_update_patch(["a", "b"]) == {"optionalValue": ["a", "b"]}
    assert registry.to_update_patch(3) == {"optionalValue": 3}


def test_to_update_patch_does_not_mutate_its_input():
    original = {"custom": {"data": "x"}}
    snapshot = json.dumps(original, sort_keys=True)
    registry.to_update_patch(original)
    assert json.dumps(original, sort_keys=True) == snapshot
