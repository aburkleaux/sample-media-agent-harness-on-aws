"""Transactional outbox tests (migration Tasks 11-12).

The outbox is the only thing that makes canonical state and the shared Registry
converge, so the properties worth pinning are:

* it carries identifiers ONLY — no credentials, package bytes, or descriptor
  text, because a stale event must not be able to publish stale data,
* the idempotency key is deterministic and recomputable (Property 4),
* scope changes emit in BOTH directions so narrowing can un-publish,
* the reserved partition never collides with an asset partition.
"""

from __future__ import annotations

import json

import pytest

from lab.asset_index import (
    MIGRATION_MARKER_PK,
    make_pk_group_shared,
    make_pk_org_shared,
    make_pk_user_owned,
    make_pk_user_versions,
)
from lab.outbox import (
    EVENT_DEPRECATED,
    EVENT_PUBLISHED,
    EVENT_SCOPE_CHANGED,
    OUTBOX_RECORD_TYPE,
    build_outbox_item,
    build_outbox_put,
    canonical_revision,
    deploy_env,
    idempotency_key,
    registry_eligible,
    should_emit,
)


def _item(**overrides):
    kwargs = {
        "event_type": EVENT_PUBLISHED,
        "category": "skill",
        "asset_id": "asset-123",
        "version": 2,
        "scope": "org",
        "canonical_revision": "skill#asset-123#v0000000002",
        "environment": "sandbox",
    }
    kwargs.update(overrides)
    return build_outbox_item(**kwargs)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_only_org_scope_is_registry_eligible():
    assert registry_eligible("org") is True
    assert registry_eligible("group") is False
    assert registry_eligible("private") is False
    assert registry_eligible(None) is False


def test_hooks_are_never_registry_eligible():
    """Actor-private hooks stay management-only (R3)."""
    assert registry_eligible("org", "hook") is False
    assert registry_eligible("org", "skill") is True


def test_scope_change_emits_in_both_directions():
    """Narrowing is when a record must STOP being discoverable.

    Emitting only on widening would leave an org→private asset discoverable
    forever, which is the visibility-widening failure Property 2 forbids.
    """
    assert should_emit(category="skill", old_scope="private", new_scope="org") is True
    assert should_emit(category="skill", old_scope="org", new_scope="group") is True
    # Neither side is eligible — invisible to the shared catalog either way.
    assert should_emit(category="skill", old_scope="private", new_scope="group") is False
    assert should_emit(category="hook", old_scope="private", new_scope="org") is False


# ---------------------------------------------------------------------------
# Item shape
# ---------------------------------------------------------------------------


def test_outbox_partition_is_reserved_and_cannot_collide_with_an_asset_partition():
    item = _item()
    pk = item["PK"]
    assert pk == "OUTBOX#skill#asset-123"
    for other in (
        make_pk_user_owned("u-1"),
        make_pk_user_versions("u-1"),
        make_pk_group_shared("g-1"),
        make_pk_org_shared("o-1"),
        MIGRATION_MARKER_PK,
    ):
        assert pk != other
        assert not pk.startswith(other.split("#")[0] + "#")


def test_discriminator_matches_the_event_source_filter():
    """The CDK event-source filter matches this literal.

    Changing it without changing `infra/lib/agent-infra-stack.ts` in the same
    commit stops projection silently — no error, just no events.
    """
    assert _item()["recordType"] == OUTBOX_RECORD_TYPE == "outbox"


def test_event_carries_identifiers_only():
    """No credentials, package bytes, or descriptor text.

    The projector reloads canonical state, so anything embedded here would only
    add a way for a replayed event to publish something stale.
    """
    serialized = json.dumps(_item(), default=str).lower()
    for forbidden in (
        "secret",
        "token",
        "password",
        "presigned",
        "skillmd",
        "descriptor",
        "persona",
        "arn:aws:secretsmanager",
    ):
        assert forbidden not in serialized, forbidden


def test_idempotency_key_is_deterministic_and_recomputable():
    expected = idempotency_key(
        environment="sandbox",
        category="skill",
        asset_id="asset-123",
        version=2,
        event_type=EVENT_PUBLISHED,
        canonical_revision="skill#asset-123#v0000000002",
    )
    assert _item()["idempotencyKey"] == expected
    assert expected == (
        "sandbox/skill/asset-123/v2/CapabilityPublished/skill#asset-123#v0000000002"
    )


def test_event_type_is_part_of_the_idempotency_key():
    """Otherwise a publish and a deprecate of one version would collide.

    They would then mask each other, and whichever arrived second would be
    dropped as an already-seen duplicate.
    """
    published = _item(event_type=EVENT_PUBLISHED)["idempotencyKey"]
    deprecated = _item(event_type=EVENT_DEPRECATED)["idempotencyKey"]
    assert published != deprecated


def test_environment_is_part_of_the_idempotency_key():
    assert _item(environment="staging")["idempotencyKey"] != _item()["idempotencyKey"]


def test_an_unset_deploy_env_stamps_unknown_which_the_projector_will_drop(monkeypatch):
    """The failure this pairing actually produced in sandbox.

    `deploy_env()` falls back to the literal "unknown", and
    `projector._outbox_events` skips any event whose `environment` does not
    equal its own. Both halves are correct in isolation — the guard is
    deliberately fail-closed (Property 7) — so the bug lives entirely in
    deployment: a runtime without DEPLOY_ENV writes a valid canonical row, emits
    a valid-looking outbox row, and the projection never happens. The only
    evidence is a CloudWatch warning.

    Observed 2026-08-13: an org-scoped publish landed at ORG#mediaworks#SHARED
    with a CapabilityPublished event stamped `environment: "unknown"`, and the
    projection-state table stayed empty. Fixed by adding DEPLOY_ENV to the agent
    runtimes in `infra/lib/agent-infra-stack.ts`.

    This test pins the contract so the two halves cannot drift apart silently:
    if the fallback ever changes to something a projector would accept, an
    unstamped event would start projecting into whichever environment read it.
    """
    monkeypatch.delenv("DEPLOY_ENV", raising=False)
    assert deploy_env() == "unknown"

    # `environment=None` means "resolve from the process", which is what every
    # canonical op does — none of them pass an explicit environment.
    item = _item(environment=None)
    assert item["environment"] == "unknown"

    # And that value is not one any real deployment matches.
    for real_env in ("sandbox", "staging", "prod"):
        assert item["environment"] != real_env


def test_deploy_env_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DEPLOY_ENV", "sandbox")
    assert deploy_env() == "sandbox"
    assert _item(environment=None)["environment"] == "sandbox"
    # Whitespace-only is as good as unset — a blank CDK value must not pass.
    monkeypatch.setenv("DEPLOY_ENV", "   ")
    assert deploy_env() == "unknown"


def test_sort_keys_are_unique_within_one_transaction_window():
    """Two events in the same microsecond must not collide.

    A transaction cannot contain two writes to the same key, so a collision
    would cancel an otherwise-valid publish.
    """
    keys = {_item()["SK"] for _ in range(200)}
    assert len(keys) == 200


def test_scope_change_records_the_previous_scope():
    item = _item(event_type=EVENT_SCOPE_CHANGED, scope="group", previous_scope="org")
    assert item["previousScope"] == "org"
    assert item["scope"] == "group"


def test_ttl_is_set_so_the_reserved_partition_does_not_grow_forever():
    item = _item()
    assert item["ttl"] > 0


@pytest.mark.parametrize("version", [0, -1, None, True, "2"])
def test_invalid_version_is_rejected_at_the_call_site(version):
    """Emitting a malformed event would poison the projector instead.

    Failing here points at the buggy call site; failing there points at the
    DLQ.
    """
    with pytest.raises(ValueError):
        _item(version=version)


def test_unknown_event_type_is_rejected():
    with pytest.raises(ValueError, match="unknown outbox event type"):
        _item(event_type="SomethingElse")


# ---------------------------------------------------------------------------
# Transaction entry
# ---------------------------------------------------------------------------


def test_build_outbox_put_has_no_condition_expression():
    """A condition here could cancel the canonical mutation it rides with.

    The projector is idempotent, so a duplicate event is strictly cheaper than
    a failed publish.
    """
    entry = build_outbox_put(
        table_name="assets",
        event_type=EVENT_PUBLISHED,
        category="skill",
        asset_id="asset-123",
        version=2,
        scope="org",
        canonical_revision="rev",
    )
    assert set(entry) == {"Put"}
    assert entry["Put"]["TableName"] == "assets"
    assert "ConditionExpression" not in entry["Put"]


# ---------------------------------------------------------------------------
# Revision derivation
# ---------------------------------------------------------------------------


def test_canonical_revision_prefers_a_package_digest():
    assert canonical_revision({"packageDigest": "sha256:abc", "SK": "x"}) == "sha256:abc"


def test_canonical_revision_falls_back_to_the_immutable_sort_key():
    """A versioned row's SK is immutable and unique, which is all this needs."""
    assert canonical_revision({"SK": "skill#a#v0000000002"}) == "skill#a#v0000000002"


def test_canonical_revision_never_returns_empty():
    """An empty revision would make the idempotency key ambiguous."""
    assert canonical_revision({}) == "unknown"
    assert canonical_revision({"updatedAt": 123}) == "123"
