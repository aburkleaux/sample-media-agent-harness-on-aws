"""Unit tests for project_pin scope validator helpers.

Validates: Requirements 12.1, 12.2, 12.4 — project pins reject private-scoped
Published_Records at create/update time and at resolve time. Requirement 12.3
is preserved structurally by the Share operation rejecting narrowing
transitions and is exercised in the `share` CRUD tests, not here.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from lab.errors import PrivateScopePinError
from lab.models import PublishPolicy, Skill
from lab.project_pin import (
    assert_pin_resolvable_for_project_runtime,
    validate_pin_target,
)


def _published_record(scope: str, *, asset_id: str = "skl_abc", version: int = 1) -> Skill:
    """Build a minimal Published_Record-shaped Skill for pin tests."""
    return Skill(
        asset_id=asset_id,
        category="skill",
        name="example",
        status="published",
        scope=scope,  # type: ignore[arg-type]
        s3_prefix=f"users/u_admin/published/skills/{asset_id}/v{version}/",
        created_at=1_700_000_000_000,
        updated_at=1_700_000_000_000,
        version=version,
        publish_policy=PublishPolicy(owner_ids=["u_admin"]),
        current=True,
    )


class TestValidatePinTarget:
    """Pin-create / pin-update time guard."""

    def test_private_scope_raises(self):
        record = _published_record("private")
        with pytest.raises(PrivateScopePinError) as exc_info:
            validate_pin_target(record)

        assert exc_info.value.asset_id == "skl_abc"
        assert exc_info.value.version == 1
        assert "skl_abc@v1" in str(exc_info.value)
        assert "private" in str(exc_info.value)

    def test_group_scope_does_not_raise(self):
        record = _published_record("group")
        # Returns None on success.
        assert validate_pin_target(record) is None

    def test_org_scope_does_not_raise(self):
        record = _published_record("org")
        assert validate_pin_target(record) is None


class TestAssertPinResolvableForProjectRuntime:
    """Resolve-time guard — same semantics as the create-time guard."""

    def test_private_scope_raises(self):
        record = _published_record("private", asset_id="skl_xyz", version=3)
        with pytest.raises(PrivateScopePinError) as exc_info:
            assert_pin_resolvable_for_project_runtime(record)

        assert exc_info.value.asset_id == "skl_xyz"
        assert exc_info.value.version == 3

    def test_group_scope_does_not_raise(self):
        record = _published_record("group")
        assert assert_pin_resolvable_for_project_runtime(record) is None

    def test_org_scope_does_not_raise(self):
        record = _published_record("org")
        assert assert_pin_resolvable_for_project_runtime(record) is None


class TestSemanticParity:
    """Both helpers MUST enforce the same invariant.

    Requirement 12.4 explicitly says the project runtime refuses to load any
    version whose scope is private even if the pin record predates a later
    mutation, so the two guards must agree on every input.
    """

    @pytest.mark.parametrize("scope", ["private", "group", "org"])
    def test_helpers_agree_on_outcome(self, scope):
        record = _published_record(scope)

        create_raised = False
        try:
            validate_pin_target(record)
        except PrivateScopePinError:
            create_raised = True

        resolve_raised = False
        try:
            assert_pin_resolvable_for_project_runtime(record)
        except PrivateScopePinError:
            resolve_raised = True

        assert create_raised == resolve_raised
