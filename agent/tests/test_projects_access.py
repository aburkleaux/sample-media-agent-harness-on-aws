"""Python Project registry twin (media-harness-projects-access task 6.14).

``projects.access.check_access`` and ``projects.schema`` must agree with ``ui/server/projects/access.ts``
and ``schema.ts``: a membership check that is one consistent ``BatchGetItem``, a uniform not-found for
every denial class (Req 1.17), and the shared id fixture of task 1.1 driving valid/invalid ids.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from projects import schema  # noqa: E402
from projects.access import NOT_FOUND, AccessCheckIncomplete, check_access  # noqa: E402

FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "..", "infra", "lib", "projects", "fixtures", "project-id-cases.json"
)
with open(FIXTURE, encoding="utf-8") as _fh:
    _CASES = json.load(_fh)
VALID_IDS = _CASES["valid"]
INVALID_IDS = _CASES["invalid"]
INVALID_NON_STRINGS = _CASES["invalidNonStrings"]

REGION = "us-west-2"
TABLE = "mediaworks-harness-projects-test"
SUB = "8f7c1e2a-1111-2222-3333-444455556666"
OTHER_SUB = "11111111-2222-3333-4444-555555555555"
PID = VALID_IDS[0]


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        t = boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": "META", "projectId": PID, "status": "active"})
        t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": f"MEMBER#{SUB}", "role": "owner"})
        yield t


@pytest.fixture
def reads(table, monkeypatch):
    recorded: list[dict] = []
    real = table.meta.client.batch_get_item

    def recording(**kwargs):
        recorded.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(table.meta.client, "batch_get_item", recording)
    return recorded


# ------------------------------------------------------------------ schema agreement with the fixture

def test_schema_key_builders_accept_every_valid_id():
    for pid in VALID_IDS:
        assert schema.meta_key(pid) == {"PK": f"PROJECT#{pid}", "SK": "META"}
        assert schema.member_key(pid, SUB) == {"PK": f"PROJECT#{pid}", "SK": f"MEMBER#{SUB}"}
        assert schema.mirror_key(SUB, pid) == {"PK": f"USER#{SUB}", "SK": f"PROJECT#{pid}"}
        assert schema.project_prefix(pid) == f"projects/{pid}/"
        assert schema.pid_from_mirror_sk(f"PROJECT#{pid}") == pid


@pytest.mark.parametrize("bad", INVALID_IDS + INVALID_NON_STRINGS)
def test_schema_key_builders_reject_every_invalid_id(bad):
    for build in (schema.meta_key, lambda p: schema.member_key(p, SUB), schema.project_prefix):
        with pytest.raises(ValueError):
            build(bad)


def test_schema_rejects_empty_sub_in_member_and_user_keys():
    with pytest.raises(ValueError):
        schema.member_key(PID, "")
    with pytest.raises(ValueError):
        schema.user_pk("")


def test_tombstone_round_trip():
    item = schema.tombstone_item(PID, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    assert item["PK"] == f"PROJECT#{PID}" and item["SK"] == "META"
    assert schema.is_tombstone(item) is True
    assert schema.is_tombstone({"PK": f"PROJECT#{PID}", "SK": "META", "status": "active"}) is False
    assert schema.is_tombstone(None) is False


# ------------------------------------------------------------------ check_access: the happy path

def test_member_of_an_active_project_is_ok_with_one_consistent_read(table, reads):
    result = check_access(table, SUB, PID)
    assert result.ok is True and result.role == "owner"
    assert result.project["projectId"] == PID
    assert len(reads) == 1 and reads[0]["RequestItems"][TABLE]["ConsistentRead"] is True


# ------------------------------------------------------------------ check_access: uniform not-found

@pytest.mark.parametrize("bad", INVALID_IDS + INVALID_NON_STRINGS)
def test_malformed_id_is_not_found_with_no_read(table, reads, bad):
    assert check_access(table, SUB, bad) is NOT_FOUND
    assert reads == []


@pytest.mark.parametrize("bad_sub", ["", None, 0, []])
def test_empty_or_non_string_sub_is_not_found_with_no_read(table, reads, bad_sub):
    assert check_access(table, bad_sub, PID) is NOT_FOUND
    assert reads == []


def test_never_created_project_is_not_found(table):
    assert check_access(table, SUB, VALID_IDS[1]) is NOT_FOUND


def test_non_member_is_not_found(table):
    assert check_access(table, OTHER_SUB, PID) is NOT_FOUND


def test_removed_member_is_not_found(table):
    assert check_access(table, SUB, PID).ok is True
    table.delete_item(Key={"PK": f"PROJECT#{PID}", "SK": f"MEMBER#{SUB}"})
    assert check_access(table, SUB, PID) is NOT_FOUND


def test_deleted_or_inactive_or_tombstoned_meta_is_not_found(table):
    for item in (
        {"PK": f"PROJECT#{PID}", "SK": "META", "status": "deleted"},
        {"PK": f"PROJECT#{PID}", "SK": "META", "status": "active", "tombstone": True},
    ):
        table.put_item(Item=item)
        assert check_access(table, SUB, PID) is NOT_FOUND
    table.delete_item(Key={"PK": f"PROJECT#{PID}", "SK": "META"})
    assert check_access(table, SUB, PID) is NOT_FOUND


# ------------------------------------------------------------------ incomplete reads surface as failures

class _FakeClient:
    """Returns the requested items, but can leave keys unprocessed to drive the retry path."""

    def __init__(self, items, *, unprocessed_rounds=0):
        self._items = items
        self._unprocessed_rounds = unprocessed_rounds
        self.calls = 0

    def batch_get_item(self, *, RequestItems):
        self.calls += 1
        if self._unprocessed_rounds > 0:
            self._unprocessed_rounds -= 1
            return {"Responses": {TABLE: []}, "UnprocessedKeys": RequestItems}
        (_name, spec), = RequestItems.items()
        wanted = {(k["PK"], k["SK"]) for k in spec["Keys"]}
        got = [i for i in self._items if (i["PK"], i["SK"]) in wanted]
        return {"Responses": {TABLE: got}, "UnprocessedKeys": {}}


def _fake_table(client):
    return SimpleNamespace(name=TABLE, meta=SimpleNamespace(client=client))


def test_one_round_of_unprocessed_keys_is_retried_and_succeeds():
    items = [{"PK": f"PROJECT#{PID}", "SK": "META", "status": "active", "projectId": PID},
             {"PK": f"PROJECT#{PID}", "SK": f"MEMBER#{SUB}", "role": "member"}]
    client = _FakeClient(items, unprocessed_rounds=1)
    result = check_access(_fake_table(client), SUB, PID)
    assert result.ok is True and result.role == "member" and client.calls == 2


def test_still_unprocessed_after_the_retry_raises_rather_than_denying():
    client = _FakeClient([], unprocessed_rounds=2)
    with pytest.raises(AccessCheckIncomplete):
        check_access(_fake_table(client), SUB, PID)
    assert client.calls == 2
