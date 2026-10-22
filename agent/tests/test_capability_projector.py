"""Projection worker tests (migration Tasks 13-16).

Covers the convergence properties at-least-once delivery depends on:

* duplicate delivery converges to one record revision (Property 4),
* the canonical row is reloaded and re-checked, so a stale event cannot publish
  stale or newly-private data (Property 2),
* an ineligible version is *deprecated*, never deleted (R16, Property 1),
* a poison descriptor is recorded and re-raised so the batch reaches the DLQ
  with the canonical asset untouched,
* cross-environment events are dropped (Property 7).
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from capability.config import CapabilityConfig
from capability.descriptors import PoisonDescriptorError
from capability.projection_state import ProjectionState
from capability.projector import Projector, outbox_events
from lab.outbox import (
    EVENT_DEPRECATED,
    EVENT_PUBLISHED,
    EVENT_SCOPE_CHANGED,
    build_outbox_item,
)

REGISTRY_ID = "reg-abc"
ENV = "sandbox"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAssetTable:
    """Minimal `byPublishedId` GSI stand-in."""

    def __init__(self, rows: Optional[list[dict[str, Any]]] = None) -> None:
        self.rows = rows or []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs["ExpressionAttributeValues"]
        want_pk, want_sk = values[":pk"], values[":sk"]
        return {
            "Items": [
                row
                for row in self.rows
                if row.get("GSI1PK") == want_pk and row.get("GSI1SK") == want_sk
            ]
        }


class FakeStateTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, Key: dict[str, str]) -> dict[str, Any]:  # noqa: N803
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def put_item(self, Item: dict[str, Any]) -> None:  # noqa: N803
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, **_):  # noqa: N803
        """Apply a `SET a = :x, b = :y ADD c :z` expression.

        SET and ADD are split before the comma split: a naive `,`-split leaves
        `b = :y ADD c :z` as one clause and assigns `:z` to `b`, which silently
        stores the wrong value and made a truncation assertion pass/fail for the
        wrong reason.
        """
        item = self.items.setdefault((Key["PK"], Key["SK"]), dict(Key))
        expression = UpdateExpression
        add_section = ""
        if " ADD " in expression:
            expression, _, add_section = expression.partition(" ADD ")
        for clause in expression.removeprefix("SET ").split(","):
            name, _, placeholder = (part.strip() for part in clause.partition("="))
            if placeholder.startswith(":"):
                item[name] = ExpressionAttributeValues[placeholder]
        for clause in filter(None, (c.strip() for c in add_section.split(","))):
            name, placeholder = clause.split()
            item[name] = item.get(name, 0) + ExpressionAttributeValues[placeholder]


class FakeControl:
    """Records every control-plane call so tests can assert on the sequence."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self._next = 1
        self.auto_approve = True
        # Status returned by CreateRegistryRecord. The real service returned
        # `CREATING` in sandbox, which makes the projector defer the approval
        # submit — the path that used to be mislabelled `in_sync`.
        self.create_status = "DRAFT"

    def list_registry_records(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("list")
        wanted = None
        for f in kwargs.get("filters", []):
            if f["name"] == "name":
                wanted = f["values"][0]
        items = [
            r for r in self.records.values() if wanted is None or r["name"] == wanted
        ]
        return {"registryRecords": items}

    @staticmethod
    def _plain_description(value: Any) -> str:
        """Validate a CREATE-shaped ``description`` and return it.

        `description` is `min length: 1` in the `agent-registry-control` model,
        and botocore rejects a violation client-side with `ParamValidationError`
        — before any request is sent, so there is nothing in CloudTrail to find.

        This fake used to accept anything and not even store the value, which is
        why the whole suite passed while the deployed projector could not create a
        single record: canonical rows carry no `description` attribute, so the
        projector was passing `""` every time. A fake looser than the API under
        test is worse than no fake.
        """
        if not isinstance(value, str) or len(value) < 1:
            raise ValueError(
                "Parameter validation failed:\nInvalid length for parameter "
                f"description, value: {len(value or '')}, valid min length: 1"
            )
        if len(value) > 1024:
            raise ValueError(
                "Parameter validation failed:\nInvalid length for parameter "
                f"description, value: {len(value)}, valid max length: 1024"
            )
        return value

    @classmethod
    def _patched_description(cls, value: Any) -> str:
        """Validate an UPDATE-shaped ``description`` and unwrap it.

        Update takes a patch wrapper — ``{"optionalValue": "..."}`` — where create
        takes a plain string. The projector sent create shapes to update, and this
        fake accepted them, so the update path was broken in production while
        three tests exercising it passed. Enforcing the distinction here is what
        makes those tests mean something; `test_capability_registry_param_shapes`
        checks the same thing against botocore's real validator.
        """
        if not isinstance(value, dict) or "optionalValue" not in value:
            raise ValueError(
                "Parameter validation failed:\nInvalid type for parameter "
                f"description, value: {value!r}, type: {type(value)}, "
                "valid types: <class 'dict'>"
            )
        return cls._plain_description(value["optionalValue"])

    @staticmethod
    def _unwrap(value: Any) -> Any:
        """Undo one patch-wrapper level, recursively, for stored comparison."""
        if isinstance(value, dict) and set(value) == {"optionalValue"}:
            return FakeControl._unwrap(value["optionalValue"])
        if isinstance(value, dict):
            return {k: FakeControl._unwrap(v) for k, v in value.items()}
        return value

    def create_registry_record(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("create")
        kwargs["description"] = self._plain_description(kwargs.get("description"))
        record_id = f"rec-{self._next}"
        self._next += 1
        arn = f"arn:aws:agent-registry:us-west-2:1:registry/{REGISTRY_ID}/record/{record_id}"
        self.records[record_id] = {
            "recordId": record_id,
            "recordArn": arn,
            "registryArn": f"arn:aws:agent-registry:us-west-2:1:registry/{REGISTRY_ID}",
            "name": kwargs["name"],
            "recordVersion": kwargs["recordVersion"],
            "recordType": kwargs["recordType"],
            "displayName": kwargs.get("displayName"),
            "description": kwargs["description"],
            "descriptors": kwargs["descriptors"],
            "status": self.create_status,
        }
        return {"recordArn": arn, "status": self.create_status}

    def update_registry_record(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("update")
        record = self.records[kwargs["recordId"]]
        # Stored unwrapped so assertions read the same on both paths.
        record["description"] = self._patched_description(kwargs.get("description"))
        if "displayName" in kwargs:
            if not isinstance(kwargs["displayName"], dict):
                raise ValueError(
                    "Parameter validation failed:\nInvalid type for parameter "
                    f"displayName, value: {kwargs['displayName']!r}, "
                    "valid types: <class 'dict'>"
                )
            record["displayName"] = self._unwrap(kwargs["displayName"])
        if "descriptors" in kwargs:
            if not isinstance(kwargs["descriptors"], dict) or "optionalValue" not in (
                kwargs["descriptors"]
            ):
                raise ValueError(
                    "Parameter validation failed:\nUnknown parameter in descriptors"
                )
            record["descriptors"] = self._unwrap(kwargs["descriptors"])
        record["status"] = "DRAFT"
        return dict(record)

    def submit_registry_record_for_approval(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("submit")
        record = self.records[kwargs["recordId"]]
        record["status"] = "APPROVED" if self.auto_approve else "PENDING_APPROVAL"
        return {
            "registryArn": record["registryArn"],
            "recordArn": record["recordArn"],
            "recordId": record["recordId"],
            "status": record["status"],
        }

    def update_registry_record_status(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("status")
        record = self.records[kwargs["recordId"]]
        record["status"] = kwargs["status"]
        record["statusReason"] = kwargs["statusReason"]
        return {**record, "updatedAt": "now"}


def _config(**overrides: Any) -> CapabilityConfig:
    base = {
        "deploy_env": ENV,
        "region": "us-west-2",
        "asset_index_table": "assets",
        "projection_state_table": "state",
        "artifacts_bucket": "artifacts",
        "registry_id": REGISTRY_ID,
        "registry_name": f"mediaworks-harness-capabilities-{ENV}",
        "projection_enabled": True,
    }
    base.update(overrides)
    return CapabilityConfig(**base)


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "PK": "ORG#o-acme#SHARED",
        "SK": "skill#asset-123#v0000000002",
        "GSI1PK": "PUB#asset-123",
        "GSI1SK": "v0000000002",
        "assetId": "asset-123",
        "category": "skill",
        "name": "Video Processing",
        "description": "Transcode media",
        "scope": "org",
        "version": 2,
        "deprecated": False,
        "recordType": "published",
        "s3Prefix": "org/o-acme/published/skills/asset-123/v2/",
        "updatedAt": 1786000000000,
    }
    row.update(overrides)
    return row


def _event(event_type: str = EVENT_PUBLISHED, **overrides: Any) -> dict[str, Any]:
    item = build_outbox_item(
        event_type=event_type,
        category="skill",
        asset_id="asset-123",
        version=2,
        scope="org",
        canonical_revision="skill#asset-123#v0000000002",
        environment=ENV,
    )
    item.update(overrides)
    return item


def _projector(rows: Optional[list[dict[str, Any]]] = None, **cfg: Any):
    state_table = FakeStateTable()
    control = FakeControl()
    projector = Projector(
        config=_config(**cfg),
        asset_table=FakeAssetTable(rows if rows is not None else [_row()]),
        state=ProjectionState(state_table, environment=ENV),
        control=control,
        s3=None,
    )
    return projector, control, state_table


# ---------------------------------------------------------------------------
# Happy path + idempotency (Property 4)
# ---------------------------------------------------------------------------


def test_first_projection_creates_and_approves_a_record():
    projector, control, state = _projector()
    assert projector.project(_event()) == "created"
    assert control.calls == ["list", "create", "submit"]

    record = next(iter(control.records.values()))
    assert record["name"] == "mediaworks/skill/asset-123"
    assert record["recordVersion"] == "v2"
    assert record["recordType"] == "SKILL"
    assert record["status"] == "APPROVED"

    stored = state.items[(f"{ENV}#skill#asset-123", "v2")]
    assert stored["projectionStatus"] == "in_sync"
    assert stored["recordId"] == record["recordId"]
    assert stored["descriptorDigest"].startswith("sha256:")


def test_a_row_with_no_description_still_projects():
    """The shape of EVERY real canonical row.

    `AssetCommon` defines no `description` field and the authoring UI has no
    input for one, so no AssetIndex row has the attribute at all. The projector
    used to forward `row.get("description")` directly, which meant `""`, which
    the Registry rejects with `min length: 1` — client-side, inside botocore, so
    nothing reached CloudTrail and the projector logged a transient failure and
    retried into the DLQ.

    Every test in this file passed throughout, because `_row()` supplies a
    description that no production row has. That is the gap this closes.
    """
    row = _row()
    del row["description"]
    projector, control, state = _projector([row])

    assert projector.project(_event()) == "created"

    record = next(iter(control.records.values()))
    assert len(record["description"]) >= 1
    # Generated from fields the row always has, so it is useful, not filler.
    assert "Video Processing" in record["description"]
    assert "skill" in record["description"]
    assert state.items[(f"{ENV}#skill#asset-123", "v2")]["projectionStatus"] == "in_sync"


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_a_blank_description_is_replaced_not_forwarded(blank):
    """Whitespace passes a truthiness check but is not a description."""
    projector, control, _ = _projector([_row(description=blank)])
    assert projector.project(_event()) == "created"
    description = next(iter(control.records.values()))["description"]
    assert description.strip() == description
    assert len(description) >= 1


def test_an_explicit_description_is_preserved():
    """So adding the field to the model later needs no projector change."""
    projector, control, _ = _projector([_row(description="Transcode media, fast")])
    assert projector.project(_event()) == "created"
    assert next(iter(control.records.values()))["description"] == "Transcode media, fast"


def test_the_generated_description_is_deterministic():
    """A re-projection of the same version must stay a no-op.

    A description that varied per attempt would make every replay look like a
    content change and churn the record lineage.
    """
    row = _row()
    del row["description"]
    first, control_a, _ = _projector([dict(row)])
    second, control_b, _ = _projector([dict(row)])
    first.project(_event())
    second.project(_event())
    assert (
        next(iter(control_a.records.values()))["description"]
        == next(iter(control_b.records.values()))["description"]
    )


def test_an_overlong_description_is_truncated_to_the_api_limit():
    projector, control, _ = _projector([_row(description="x" * 5000)])
    assert projector.project(_event()) == "created"
    assert len(next(iter(control.records.values()))["description"]) == 1024


def test_the_update_path_also_sends_a_description():
    """Update has the same min-length constraint as create."""
    row = _row()
    del row["description"]
    projector, control, _ = _projector([row])
    assert projector.project(_event()) == "created"

    # Same lineage, changed content ⇒ update rather than create.
    row["name"] = "Video Processing v2"
    assert projector.project(_event()) == "updated"
    assert control.calls.count("update") == 1
    assert len(next(iter(control.records.values()))["description"]) >= 1


def test_duplicate_delivery_is_a_noop_not_a_second_record():
    projector, control, _ = _projector()
    assert projector.project(_event()) == "created"
    before = list(control.records)
    assert projector.project(_event()) == "noop"
    assert list(control.records) == before
    assert control.calls.count("create") == 1


def test_content_change_updates_the_existing_record_lineage():
    """A new descriptor digest updates; it does not fork a second record.

    Identity is `(name, recordVersion)` derived from canonical identity, so a
    metadata edit to the same version converges on the same record.
    """
    rows = [_row()]
    projector, control, _ = _projector(rows)
    projector.project(_event())

    rows[0]["description"] = "Transcode media, faster"
    assert projector.project(_event(canonicalRevision="rev-2")) == "updated"
    assert len(control.records) == 1
    assert control.calls.count("create") == 1
    assert control.calls.count("update") == 1


def test_replay_after_a_failed_submit_converges():
    """A record left DRAFT by a failed submit is submitted on the next event.

    The stored state's `registryStatus` is not APPROVED, so the digest check
    does not short-circuit — which is what makes a partial failure recoverable
    without operator intervention.
    """
    projector, control, state = _projector()
    control.auto_approve = False
    projector.project(_event())
    stored = state.items[(f"{ENV}#skill#asset-123", "v2")]
    assert stored["registryStatus"] == "PENDING_APPROVAL"
    # Not discoverable, so not `in_sync` — see `record_success`.
    assert stored["projectionStatus"] == "pending"

    control.auto_approve = True
    # PENDING_APPROVAL is in-flight, so the projector defers rather than
    # re-submitting into a rejection.
    assert projector.project(_event()) == "updated"


# ---------------------------------------------------------------------------
# projectionStatus must describe DISCOVERABILITY, not just a successful write
# ---------------------------------------------------------------------------


def test_a_deferred_approval_is_pending_not_in_sync():
    """The exact sandbox failure, reproduced.

    `CreateRegistryRecord` returns `CREATING`; the projector defers the approval
    submit (correct — submitting against an in-flight record is rejected); the
    record settles at DRAFT and serves nothing. Recording that as `in_sync` made
    an alarm on `projectionStatus != in_sync` blind to an invisible capability.
    """
    projector, control, state = _projector()
    control.create_status = "CREATING"

    assert projector.project(_event()) == "created"
    assert "submit" not in control.calls  # deferred, by design

    stored = state.items[(f"{ENV}#skill#asset-123", "v2")]
    assert stored["registryStatus"] == "CREATING"
    assert stored["projectionStatus"] == "pending"
    # Nothing converged, so nothing may claim a success timestamp — otherwise a
    # lag metric reads this as "done at 04:35".
    assert "lastSuccessAt" not in stored
    assert stored["lastAttemptAt"]


@pytest.mark.parametrize(
    "registry_status,expected",
    [
        ("APPROVED", "in_sync"),
        ("CREATING", "pending"),
        ("UPDATING", "pending"),
        ("PENDING_APPROVAL", "pending"),
        ("DRAFT", "pending"),
    ],
)
def test_projection_status_is_derived_from_the_registry_status(
    registry_status, expected
):
    """APPROVED is the only discoverable status, so it is the only `in_sync`."""
    table = FakeStateTable()
    ProjectionState(table, environment=ENV).record_success(
        category="skill",
        asset_id="asset-123",
        version=2,
        canonical_revision="skill#asset-123#v0000000002",
        descriptor_digest="sha256:abc",
        registry_id=REGISTRY_ID,
        record_id="rec-1",
        record_arn="arn:aws:agent-registry:::registry/r/record/rec-1",
        registry_status=registry_status,
        occurred_at="2026-08-14T05:00:00+00:00",
    )
    stored = table.items[(f"{ENV}#skill#asset-123", "v2")]
    assert stored["projectionStatus"] == expected
    assert ("lastSuccessAt" in stored) is (expected == "in_sync")


def test_a_pending_record_converges_to_in_sync_on_the_next_event():
    """`pending` is a work queue, not a terminal state.

    The stored `registryStatus` is not APPROVED, so the digest short-circuit does
    not fire and a replay re-enters the submit path.
    """
    projector, control, state = _projector()
    control.create_status = "CREATING"
    projector.project(_event())
    key = (f"{ENV}#skill#asset-123", "v2")
    assert state.items[key]["projectionStatus"] == "pending"

    # The record has settled by the time the next event arrives.
    for record in control.records.values():
        record["status"] = "DRAFT"

    assert projector.project(_event()) == "updated"
    stored = state.items[key]
    assert stored["registryStatus"] == "APPROVED"
    assert stored["projectionStatus"] == "in_sync"
    assert stored["lastSuccessAt"]


def test_the_happy_path_is_still_in_sync():
    """Guards against over-correcting: an approved record must not read pending."""
    projector, _, state = _projector()
    assert projector.project(_event()) == "created"
    stored = state.items[(f"{ENV}#skill#asset-123", "v2")]
    assert stored["registryStatus"] == "APPROVED"
    assert stored["projectionStatus"] == "in_sync"


# ---------------------------------------------------------------------------
# Scope re-check (Property 2)
# ---------------------------------------------------------------------------


def test_stale_event_for_a_now_private_version_does_not_publish_it():
    """The event says org; canonical says private. Canonical wins.

    This is the leak the re-check exists to prevent: an org→private transition
    that lands after the event was written must not result in a discoverable
    record.
    """
    rows = [_row(scope="private")]
    projector, control, state = _projector(rows)
    assert projector.project(_event(scope="org")) == "ineligible"
    assert control.records == {}
    stored = state.items[(f"{ENV}#skill#asset-123", "v2")]
    assert stored["eligibility"] == "ineligible"
    assert stored["ineligibleReason"] == "scope_private"


def test_narrowing_scope_deprecates_the_existing_record_without_deleting_it():
    rows = [_row()]
    projector, control, state = _projector(rows)
    projector.project(_event())
    record_id = next(iter(control.records))

    rows[0]["scope"] = "group"
    outcome = projector.project(_event(EVENT_SCOPE_CHANGED, scope="group"))

    assert outcome == "deprecated"
    # The record still EXISTS — deprecated, not deleted (R16).
    assert control.records[record_id]["status"] == "DEPRECATED"
    assert "narrowed" in control.records[record_id]["statusReason"]
    assert "delete" not in " ".join(control.calls)
    assert state.items[(f"{ENV}#skill#asset-123", "v2")]["eligibility"] == "ineligible"


def test_widening_back_to_org_reprojects():
    rows = [_row(scope="group")]
    projector, control, _ = _projector(rows)
    assert projector.project(_event(EVENT_SCOPE_CHANGED, scope="group")) == "ineligible"

    rows[0]["scope"] = "org"
    assert projector.project(_event(EVENT_SCOPE_CHANGED, scope="org")) == "created"
    assert next(iter(control.records.values()))["status"] == "APPROVED"


def test_deprecate_event_deprecates_the_registry_revision():
    rows = [_row()]
    projector, control, _ = _projector(rows)
    projector.project(_event())
    rows[0]["deprecated"] = True
    assert projector.project(_event(EVENT_DEPRECATED)) == "deprecated"
    assert next(iter(control.records.values()))["status"] == "DEPRECATED"


def test_missing_canonical_row_converges_to_ineligible():
    projector, control, state = _projector(rows=[])
    assert projector.project(_event()) == "ineligible"
    assert control.records == {}
    assert (
        state.items[(f"{ENV}#skill#asset-123", "v2")]["ineligibleReason"]
        == "canonical_row_missing"
    )


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_poison_descriptor_is_recorded_and_re_raised_for_the_dlq():
    """Recorded first, then re-raised.

    The DLQ message says a batch failed; the state row says which asset and
    why. Neither carries the payload.
    """
    rows = [_row(tags=["t" * 1000] * 200)]
    projector, control, state = _projector(rows)
    with pytest.raises(PoisonDescriptorError):
        projector.project(_event())
    stored = state.items[(f"{ENV}#skill#asset-123", "v2")]
    assert stored["projectionStatus"] == "failed"
    assert stored["lastErrorCode"] == "descriptor_too_large"
    assert control.records == {}


def test_transient_failure_records_the_attempt_and_re_raises():
    projector, control, state = _projector()

    def boom(**_: Any):
        raise RuntimeError("throttled")

    control.create_registry_record = boom  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        projector.project(_event())
    stored = state.items[(f"{ENV}#skill#asset-123", "v2")]
    assert stored["projectionStatus"] == "failed"
    assert stored["lastErrorCode"] == "RuntimeError"


def test_malformed_event_is_poison_not_a_retry_loop():
    projector, _, _ = _projector()
    with pytest.raises(PoisonDescriptorError) as excinfo:
        projector.project({"category": "skill", "assetId": "", "version": 0})
    assert excinfo.value.reason == "malformed_outbox_event"


def test_error_message_is_truncated_in_projection_state():
    """A Registry validation error can echo the payload back.

    Descriptor bodies must not land in the state table any more than in logs.
    """
    projector, control, state = _projector()

    def boom(**_: Any):
        raise RuntimeError("x" * 5000)

    control.create_registry_record = boom  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        projector.project(_event())
    stored = state.items[(f"{ENV}#skill#asset-123", "v2")]
    assert len(stored["lastErrorMessageRedacted"]) <= 256


# ---------------------------------------------------------------------------
# Stream decoding (Property 7)
# ---------------------------------------------------------------------------


def _stream_record(item: dict[str, Any], event_name: str = "INSERT") -> dict[str, Any]:
    def encode(value: Any) -> dict[str, Any]:
        if isinstance(value, bool):
            return {"BOOL": value}
        if isinstance(value, (int, float)):
            return {"N": str(value)}
        if value is None:
            return {"NULL": True}
        return {"S": str(value)}

    return {
        "eventName": event_name,
        "dynamodb": {"NewImage": {k: encode(v) for k, v in item.items()}},
    }


def test_outbox_events_yields_only_matching_inserts():
    event = {
        "Records": [
            _stream_record(_event()),
            # An asset row that slipped past the server-side filter.
            _stream_record({"recordType": "published", "environment": ENV}),
            # A modify, not an insert.
            _stream_record(_event(), event_name="MODIFY"),
        ]
    }
    yielded = list(outbox_events(event, environment=ENV))
    assert len(yielded) == 1
    assert yielded[0]["eventType"] == EVENT_PUBLISHED


def test_cross_environment_events_are_dropped():
    """A table restored into the wrong account must not project across envs."""
    event = {"Records": [_stream_record(_event(environment="prod"))]}
    assert list(outbox_events(event, environment=ENV)) == []


def test_projector_requires_a_registry_id():
    from capability.config import ConfigError

    with pytest.raises(ConfigError):
        Projector(
            config=_config(registry_id=None),
            asset_table=FakeAssetTable(),
            state=ProjectionState(FakeStateTable(), environment=ENV),
            control=FakeControl(),
        )


def test_descriptor_reaching_the_registry_carries_no_secrets():
    """End-to-end guard on what actually gets sent."""
    projector, control, _ = _projector()
    projector.project(_event())
    payload = json.dumps(next(iter(control.records.values()))["descriptors"]).lower()
    for marker in ("clientsecret", "credentialprovider", "x-amz-signature", "presigned"):
        assert marker not in payload
