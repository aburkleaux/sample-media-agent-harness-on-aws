"""Unit tests for the issuer's membership check (task 3.2) against a moto DynamoDB table.

The table is created with the real key schema (``PK``/``SK`` strings) and read through a boto3 resource
``Table``, the same object Lambda and the local-dev wrapper pass. Calls are counted by wrapping the
client's ``batch_get_item`` and delegating to moto, so "no read" and "one read" are asserted on real
traffic; only the UnprocessedKeys test substitutes a first response.
"""
from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

import access
from access import NOT_FOUND, AccessCheckIncomplete, check_access, is_project_id, open_table

TABLE = "mediaworks-harness-projects-test"
REGION = "us-west-2"
PID = "p" + "a" * 32
OTHER_PID = "p" + "b" * 32
SUB = "8f7c1e2a-1111-2222-3333-444455556666"
OTHER_SUB = "11111111-2222-3333-4444-555555555555"

_ID_CASES = json.loads(
    (Path(__file__).resolve().parents[3] / "projects" / "fixtures" / "project-id-cases.json").read_text()
)


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        t = ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield t


@pytest.fixture
def calls(table, monkeypatch):
    """Record every BatchGetItem request, delegating to moto."""
    recorded: list[dict] = []
    real = table.meta.client.batch_get_item

    def recording(**kwargs):
        recorded.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(table.meta.client, "batch_get_item", recording)
    return recorded


def _meta(pid=PID, **overrides):
    item = {
        "PK": f"PROJECT#{pid}",
        "SK": "META",
        "projectId": pid,
        "name": "Trailer cut",
        "nameFold": "trailer cut",
        "ownerSub": SUB,
        "personal": False,
        "status": "active",
        "memberCount": 0,
        "createdAt": "2026-09-01T00:00:00Z",
    }
    item.update(overrides)
    return item


def _member(pid=PID, sub=SUB, role="owner"):
    return {"PK": f"PROJECT#{pid}", "SK": f"MEMBER#{sub}", "role": role, "display": "a@example.com"}


def _seed(table, *items):
    for item in items:
        table.put_item(Item=item)


def test_is_project_id_is_the_tokens_implementation():
    import tokens

    assert access.is_project_id is tokens.is_project_id
    for value in _ID_CASES["valid"]:
        assert is_project_id(value)


def test_owner_of_active_project_is_ok(table, calls):
    _seed(table, _meta(), _member())
    result = check_access(table, SUB, PID)
    assert result.ok is True
    assert result.role == "owner"
    assert result.project["projectId"] == PID
    assert len(calls) == 1
    (req,) = calls[0]["RequestItems"].values()
    assert calls[0]["RequestItems"].keys() == {TABLE}
    assert req["ConsistentRead"] is True
    assert req["Keys"] == [{"PK": f"PROJECT#{PID}", "SK": "META"}, {"PK": f"PROJECT#{PID}", "SK": f"MEMBER#{SUB}"}]


def test_member_is_ok_with_member_role(table, calls):
    _seed(table, _meta(memberCount=1), _member(), _member(sub=OTHER_SUB, role="member"))
    result = check_access(table, OTHER_SUB, PID)
    assert result.ok is True and result.role == "member"


@pytest.mark.parametrize("bad", _ID_CASES["invalid"] + _ID_CASES["invalidNonStrings"])
def test_malformed_id_is_not_found_without_a_read(table, calls, bad):
    assert check_access(table, SUB, bad) == NOT_FOUND
    assert calls == []


@pytest.mark.parametrize("bad_sub", ["", None, 7])
def test_empty_or_non_string_sub_is_not_found_without_a_read(table, calls, bad_sub):
    _seed(table, _meta(), _member())
    assert check_access(table, bad_sub, PID) == NOT_FOUND
    assert calls == []


@pytest.mark.parametrize(
    "items",
    [
        pytest.param(lambda: [], id="nothing"),
        pytest.param(lambda: [_member()], id="member-without-meta"),
        pytest.param(lambda: [_meta()], id="meta-without-member"),
        pytest.param(lambda: [_meta(), _member(sub=OTHER_SUB, role="member")], id="other-users-membership"),
        pytest.param(lambda: [_meta(pid=OTHER_PID), _member(pid=OTHER_PID)], id="member-of-another-project"),
        pytest.param(lambda: [_meta(status="deleted", deletedAt="2026-09-02T00:00:00Z"), _member()], id="deleted"),
        pytest.param(lambda: [_meta(status="archived"), _member()], id="unknown-status"),
        pytest.param(lambda: [_meta(status=None), _member()], id="null-status"),
        pytest.param(
            lambda: [
                {"PK": f"PROJECT#{PID}", "SK": "META", "tombstone": True, "deletedAt": "x", "purgedAt": "y"},
                _member(),
            ],
            id="tombstone",
        ),
        pytest.param(lambda: [_meta(tombstone=True), _member()], id="tombstone-with-active-status"),
    ],
)
def test_denials_are_uniform_not_found(table, calls, items):
    _seed(table, *items())
    assert check_access(table, SUB, PID) == NOT_FOUND
    assert len(calls) == 1


def test_unprocessed_keys_are_retried_once(table, monkeypatch):
    _seed(table, _meta(), _member())
    real = table.meta.client.batch_get_item
    seen: list[dict] = []

    def first_unprocessed(**kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            return {"Responses": {TABLE: []}, "UnprocessedKeys": kwargs["RequestItems"]}
        return real(**kwargs)

    monkeypatch.setattr(table.meta.client, "batch_get_item", first_unprocessed)
    result = check_access(table, SUB, PID)
    assert result.ok is True
    assert len(seen) == 2
    assert seen[1]["RequestItems"][TABLE]["ConsistentRead"] is True


def test_partial_first_response_is_merged_with_the_retry(table, monkeypatch):
    _seed(table, _meta(), _member())
    real = table.meta.client.batch_get_item
    seen: list[dict] = []

    def meta_then_member(**kwargs):
        seen.append(kwargs)
        req = kwargs["RequestItems"][TABLE]
        if len(seen) == 1:
            got = real(RequestItems={TABLE: {**req, "Keys": req["Keys"][:1]}})
            return {**got, "UnprocessedKeys": {TABLE: {**req, "Keys": req["Keys"][1:]}}}
        return real(**kwargs)

    monkeypatch.setattr(table.meta.client, "batch_get_item", meta_then_member)
    assert check_access(table, SUB, PID).ok is True
    assert len(seen) == 2


def test_keys_still_unprocessed_after_retry_raise_instead_of_denying(table, monkeypatch):
    _seed(table, _meta(), _member())
    seen: list[dict] = []

    def always_unprocessed(**kwargs):
        seen.append(kwargs)
        return {"Responses": {TABLE: []}, "UnprocessedKeys": kwargs["RequestItems"]}

    monkeypatch.setattr(table.meta.client, "batch_get_item", always_unprocessed)
    with pytest.raises(AccessCheckIncomplete):
        check_access(table, SUB, PID)
    assert len(seen) == 2


def test_open_table_uses_the_given_name_and_endpoint(table):
    t = open_table(TABLE, endpoint_url="http://127.0.0.1:5000", region_name=REGION)
    assert t.name == TABLE
    assert t.meta.client.meta.endpoint_url == "http://127.0.0.1:5000"
    with pytest.raises(ValueError):
        open_table("")


def test_open_table_default_endpoint_reads_the_table(table):
    _seed(table, _meta(), _member())
    assert check_access(open_table(TABLE, region_name=REGION), SUB, PID).ok is True
