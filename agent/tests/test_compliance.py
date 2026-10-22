"""Unit / example tests for compliance & data handling (task 18.1).

Feature: custom-agents, Task 18.1 (implementation).

Validates: Requirements 18.1, 18.2, 18.3

Covers the three data-protection obligations on the external surface:

* **18.1** — :func:`compliance.delete_end_user_data` purges an End_User's
  conversation data across AgentCore Memory (long-term records), session
  (short-term events), logs, and traces, keyed by the segregated ``EXT#`` key
  for ``(agentId, issuer, sub)``.
* **18.2** — :class:`compliance.RedactingLogWriter` / :func:`compliance.redact_record`
  strip secrets/tokens/credentials from log & trace records using the shared
  redaction filter (reused from task 13.1).
* **18.3** — :func:`compliance.resolve_residency_region` /
  :func:`compliance.assert_residency` pin externally-sourced End_User data to
  the region its issuer requires.
"""

from __future__ import annotations

import os
import sys

import pytest

# Add agent/src to path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from security import compliance  # noqa: E402
from security import identity  # noqa: E402
from security.redaction import REDACTION_PLACEHOLDER  # noqa: E402


MEMORY_ID = "mem-shared-task-agent"
AGENT_ID = "agent-a"
PRINCIPAL = {"issuer": "https://issuer-one.example.com", "sub": "user-1"}
OTHER_PRINCIPAL = {"issuer": "https://issuer-one.example.com", "sub": "user-2"}


# ---------------------------------------------------------------------------
# A fake AgentCore Memory data-plane client scoped to one caller's data.
# ---------------------------------------------------------------------------


class FakeMemoryClient:
    """In-memory stand-in for the ``bedrock-agentcore`` data-plane client.

    Stores events keyed by ``(actorId, sessionId)`` and long-term records keyed
    by ``namespace``. Records every delete so tests can assert exactly what was
    purged — and, critically, that another caller's data is untouched.
    """

    def __init__(self):
        # (actor_id, session_id) -> list of event dicts
        self.events: dict[tuple[str, str], list[dict]] = {}
        # namespace -> list of record dicts
        self.records: dict[str, list[dict]] = {}
        # actor_id -> list of session ids
        self.sessions_by_actor: dict[str, list[str]] = {}
        self.deleted_events: list[str] = []
        self.deleted_records: list[str] = []

    # --- seeding helpers -------------------------------------------------
    def seed_event(self, actor_id, session_id, event_id):
        self.events.setdefault((actor_id, session_id), []).append({"eventId": event_id})
        self.sessions_by_actor.setdefault(actor_id, [])
        if session_id not in self.sessions_by_actor[actor_id]:
            self.sessions_by_actor[actor_id].append(session_id)

    def seed_record(self, namespace, record_id):
        self.records.setdefault(namespace, []).append({"memoryRecordId": record_id})

    # --- data-plane API surface -----------------------------------------
    def list_sessions(self, memoryId, actorId, maxResults=100, nextToken=None):
        return {"sessionSummaries": [{"sessionId": s} for s in self.sessions_by_actor.get(actorId, [])]}

    def list_events(self, memoryId, actorId, sessionId, maxResults=100, nextToken=None):
        return {"events": list(self.events.get((actorId, sessionId), []))}

    def delete_event(self, memoryId, actorId, sessionId, eventId):
        bucket = self.events.get((actorId, sessionId), [])
        self.events[(actorId, sessionId)] = [e for e in bucket if e["eventId"] != eventId]
        self.deleted_events.append(eventId)
        return {}

    def list_memory_records(self, memoryId, namespace, maxResults=100, nextToken=None):
        return {"memoryRecordSummaries": list(self.records.get(namespace, []))}

    def delete_memory_record(self, memoryId, memoryRecordId):
        for ns, recs in self.records.items():
            self.records[ns] = [r for r in recs if r["memoryRecordId"] != memoryRecordId]
        self.deleted_records.append(memoryRecordId)
        return {}


def _seed_caller(client, principal, *, n_events=2, n_records=2):
    key = identity.derive_external_memory_key(AGENT_ID, principal["issuer"], principal["sub"])
    for i in range(n_events):
        client.seed_event(key.actor_id, key.session_id, f"evt-{principal['sub']}-{i}")
    for i in range(n_records):
        client.seed_record(key.namespace, f"rec-{principal['sub']}-{i}")
    return key


# ---------------------------------------------------------------------------
# Req 18.1 — deletion path
# ---------------------------------------------------------------------------


def test_deletion_purges_memory_session_logs_and_traces():
    """Deletion removes events + records and invokes log/trace purgers (18.1)."""
    client = FakeMemoryClient()
    _seed_caller(client, PRINCIPAL, n_events=3, n_records=2)

    purged = {"logs": False, "traces": False}

    def log_purger(**kwargs):
        purged["logs"] = True

    def trace_purger(**kwargs):
        purged["traces"] = True

    report = compliance.delete_end_user_data(
        memory_id=MEMORY_ID,
        agent_id=AGENT_ID,
        principal=PRINCIPAL,
        memory_client=client,
        region="us-west-2",
        log_purger=log_purger,
        trace_purger=trace_purger,
    )

    assert report.ok
    assert report.deleted_events == 3
    assert report.deleted_records == 2
    assert report.purged_sessions == 1
    assert report.logs_purged and report.traces_purged
    assert purged == {"logs": True, "traces": True}
    # Nothing left behind for this caller.
    assert all(not evts for evts in client.events.values())
    assert all(not recs for recs in client.records.values())


def test_deletion_does_not_touch_another_callers_data():
    """A deletion for one End_User leaves a different End_User's data intact."""
    client = FakeMemoryClient()
    _seed_caller(client, PRINCIPAL, n_events=2, n_records=1)
    other_key = _seed_caller(client, OTHER_PRINCIPAL, n_events=2, n_records=1)

    report = compliance.delete_end_user_data(
        memory_id=MEMORY_ID,
        agent_id=AGENT_ID,
        principal=PRINCIPAL,
        memory_client=client,
        region="us-west-2",
    )

    assert report.ok
    # The other caller's events and records survive.
    assert client.events[(other_key.actor_id, other_key.session_id)], "other caller events purged!"
    assert client.records[other_key.namespace], "other caller records purged!"


def test_deletion_report_records_per_store_errors_without_aborting():
    """A failing store is recorded but does not stop the others (18.1)."""
    client = FakeMemoryClient()
    _seed_caller(client, PRINCIPAL, n_events=1, n_records=1)

    def boom(**kwargs):
        raise RuntimeError("trace store offline")

    report = compliance.delete_end_user_data(
        memory_id=MEMORY_ID,
        agent_id=AGENT_ID,
        principal=PRINCIPAL,
        memory_client=client,
        region="us-west-2",
        trace_purger=boom,
    )

    assert not report.ok
    assert any("trace_purger" in e for e in report.errors)
    # Memory deletion still completed despite the trace failure.
    assert report.deleted_events == 1
    assert report.deleted_records == 1


@pytest.mark.parametrize(
    "principal",
    [
        {"sub": "user-1"},                       # missing issuer
        {"issuer": "https://i.example.com"},     # missing sub
        {},                                       # both missing
    ],
)
def test_deletion_rejects_underqualified_principal(principal):
    """An issuer-less / sub-less principal is refused (never guess a key)."""
    with pytest.raises(ValueError):
        compliance.delete_end_user_data(
            memory_id=MEMORY_ID,
            agent_id=AGENT_ID,
            principal=principal,
            memory_client=FakeMemoryClient(),
            region="us-west-2",
        )


def test_deletion_targets_the_external_ext_key():
    """The report's external_key is the segregated EXT# key for the triple."""
    client = FakeMemoryClient()
    _seed_caller(client, PRINCIPAL)
    report = compliance.delete_end_user_data(
        memory_id=MEMORY_ID,
        agent_id=AGENT_ID,
        principal=PRINCIPAL,
        memory_client=client,
        region="us-west-2",
    )
    assert report.external_key.startswith(identity.EXTERNAL_KEYSPACE_PREFIX)


# ---------------------------------------------------------------------------
# Req 18.2 — secret redaction in the log/trace writer
# ---------------------------------------------------------------------------


def test_redacting_log_writer_strips_known_secret_before_sink():
    """The writer redacts a known Owner secret before the sink sees it."""
    written = []
    secret = "owner-session-token-abc123XYZ456"
    writer = compliance.RedactingLogWriter(written.append, known_secrets=[secret])

    writer.write(f"invoking with token {secret} now")

    assert len(written) == 1
    assert secret not in written[0]
    assert REDACTION_PLACEHOLDER in written[0]


def test_redacting_log_writer_scrubs_nested_structured_record():
    """Structured (dict/list) trace records are scrubbed recursively (18.2)."""
    written = []
    writer = compliance.RedactingLogWriter(written.append)

    record = {
        "event": "external_invoke",
        "headers": {"authorization": "Bearer abc123DEF456ghi789JKL012mno"},
        "notes": ["password=hunter2SuperSecret", "ordinary text"],
    }
    writer.write(record)

    out = written[0]
    assert out["headers"]["authorization"] == REDACTION_PLACEHOLDER or REDACTION_PLACEHOLDER in out["headers"]["authorization"]
    assert "hunter2SuperSecret" not in out["notes"][0]
    # Keys and non-secret text preserved.
    assert out["event"] == "external_invoke"
    assert out["notes"][1] == "ordinary text"


def test_redact_record_leaves_clean_records_unchanged():
    record = {"event": "ok", "count": 3, "items": ["a", "b"]}
    assert compliance.redact_record(record) == record


# ---------------------------------------------------------------------------
# Req 18.3 — region / data residency pinning
# ---------------------------------------------------------------------------


def test_per_issuer_residency_region_wins():
    """A per-issuer residency entry pins that issuer's data to its region."""
    issuer_map = {"https://eu-idp.example.com": "eu-west-1"}
    region = compliance.resolve_residency_region(
        "https://eu-idp.example.com",
        default="us-west-2",
        issuer_map=issuer_map,
    )
    assert region == "eu-west-1"


def test_global_residency_region_applies_when_no_issuer_entry():
    region = compliance.resolve_residency_region(
        "https://other.example.com",
        default="us-west-2",
        issuer_map={},
        global_required="eu-central-1",
    )
    assert region == "eu-central-1"


def test_no_constraint_falls_back_to_default_region():
    region = compliance.resolve_residency_region(
        "https://other.example.com",
        default="us-west-2",
        issuer_map={},
        global_required=None,
    )
    assert region == "us-west-2"
    assert not compliance.residency_constraint_applies(
        "https://other.example.com", issuer_map={}, global_required=None
    )


def test_assert_residency_raises_on_region_mismatch():
    with pytest.raises(compliance.ResidencyViolation):
        compliance.assert_residency("us-east-1", "eu-west-1", issuer="https://eu.example.com")
    # Matching region (and the no-constraint case) do not raise.
    compliance.assert_residency("eu-west-1", "eu-west-1")
    compliance.assert_residency("us-east-1", "")


def test_issuer_residency_map_parsed_from_json():
    parsed = compliance._load_issuer_residency_map('{"https://a": "eu-west-1", "https://b": "ap-southeast-2"}')
    assert parsed == {"https://a": "eu-west-1", "https://b": "ap-southeast-2"}
    # Malformed JSON yields an empty map (no constraint) rather than raising.
    assert compliance._load_issuer_residency_map("{not json") == {}


# ---------------------------------------------------------------------------
# Req 18.3 — region-residency PLACEMENT check
#
# The tests above verify residency *resolution* (which region is required).
# These verify residency *placement*: that the AgentCore Memory data-plane
# client is actually constructed bound to the resolved region, so
# externally-sourced End_User data is stored/processed there and nowhere else.
# ---------------------------------------------------------------------------


class FakeBoto3:
    """Records the region_name every ``client(...)`` call is pinned to."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def client(self, service, region_name=None):
        self.calls.append((service, region_name))
        # Return a fully data-plane-capable fake so the deletion path can run.
        return FakeMemoryClient()


def test_pinned_client_is_bound_to_the_requested_region():
    """pinned_memory_data_plane_client binds the data-plane client to region (18.3)."""
    boto3_mod = FakeBoto3()
    compliance.pinned_memory_data_plane_client("eu-west-1", boto3_module=boto3_mod)
    assert boto3_mod.calls == [("bedrock-agentcore", "eu-west-1")]


def test_deletion_places_client_in_resolved_per_issuer_region():
    """With no injected client, deletion builds one pinned to the issuer's
    residency region and reports that region (18.3 placement)."""
    boto3_mod = FakeBoto3()
    issuer = PRINCIPAL["issuer"]

    # Force a per-issuer residency constraint for this issuer.
    original = os.environ.get(compliance._ENV_ISSUER_RESIDENCY_MAP)
    os.environ[compliance._ENV_ISSUER_RESIDENCY_MAP] = f'{{"{issuer}": "eu-west-1"}}'
    try:
        report = compliance.delete_end_user_data(
            memory_id=MEMORY_ID,
            agent_id=AGENT_ID,
            principal=PRINCIPAL,
            memory_client=None,          # force placement path
            boto3_module=boto3_mod,
        )
    finally:
        if original is None:
            os.environ.pop(compliance._ENV_ISSUER_RESIDENCY_MAP, None)
        else:
            os.environ[compliance._ENV_ISSUER_RESIDENCY_MAP] = original

    # The client was created, pinned to the issuer's required region...
    assert boto3_mod.calls == [("bedrock-agentcore", "eu-west-1")]
    # ...and the report records placement in that same region.
    assert report.region == "eu-west-1"


def test_explicit_region_override_pins_placement():
    """An explicit region argument pins the built client to that region (18.3)."""
    boto3_mod = FakeBoto3()
    report = compliance.delete_end_user_data(
        memory_id=MEMORY_ID,
        agent_id=AGENT_ID,
        principal=PRINCIPAL,
        memory_client=None,
        region="ap-southeast-2",
        boto3_module=boto3_mod,
    )
    assert boto3_mod.calls == [("bedrock-agentcore", "ap-southeast-2")]
    assert report.region == "ap-southeast-2"
