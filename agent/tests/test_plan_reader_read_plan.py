"""Unit tests for Plan Reader I/O wiring — ``read_plan`` (task 2.4).

Validates: Requirements 1.1, 1.3, 1.4, 1.5

Covers, against in-memory fakes for the ``Plan_State_Table`` (DDB) and S3:
- success path: the ``PLAN`` row's ``output_s3_key`` is followed to S3 and the
  object is parsed into a ``HighLevelPlan`` (R1.1)
- missing-plan: no DDB row, or the row points at an absent S3 object, raises
  ``MissingPlanError`` (R1.3)
- parse-failure: a non-conforming S3 object raises ``PlanParseError`` (R1.4)
- the missing/parse-failure edges make NO OMS contact — a recording fake OMS
  client is never touched (the reader has no OMS client at all) (R1.5 spirit:
  S3/DDB are internal-only and reads fail closed before any OMS interaction)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from botocore.exceptions import ClientError

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from planning.high_level_plan import HighLevelPlan, SubGoal
from persistence.models import MissingPlanError, PlanParseError
from persistence.plan_reader import PlanReader


# ---------------------------------------------------------------------------
# Fakes — record every call so we can assert atomicity / no-OMS-contact
# ---------------------------------------------------------------------------


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeTable:
    """A stand-in for a boto3 DynamoDB ``Table`` resource."""

    def __init__(self, item, recorder: list) -> None:
        self._item = item
        self._recorder = recorder

    def get_item(self, Key):  # noqa: N803 — boto3 kwarg name
        self._recorder.append(("get_item", Key))
        return {"Item": self._item} if self._item is not None else {}


class _FakeDDB:
    """A stand-in for a boto3 DynamoDB resource exposing ``.Table(name)``."""

    def __init__(self, item) -> None:
        self.item = item
        self.calls: list = []

    def Table(self, name):  # noqa: N802 — boto3 resource method name
        self.calls.append(("Table", name))
        return _FakeTable(self.item, self.calls)


class _FakeS3:
    """A stand-in for a boto3 S3 client; scripts one object by (bucket, key)."""

    def __init__(self, objects: dict) -> None:
        self._objects = objects
        self.calls: list = []

    def get_object(self, Bucket, Key):  # noqa: N803 — boto3 kwarg names
        self.calls.append(("get_object", Bucket, Key))
        if (Bucket, Key) not in self._objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )
        return {"Body": _FakeBody(self._objects[(Bucket, Key)])}


class _RecordingOMS:
    """Records every call; asserts read failures never reach OMS."""

    def __init__(self) -> None:
        self.calls: list = []

    def __getattr__(self, name):
        def _method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None

        return _method


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_TABLE = "plan-state-table"
_BUCKET = "skills-bucket"
_SESSION = "sess-123"
_KEY = f"plans/{_SESSION}/planning/output.json"


def _valid_plan_json() -> str:
    plan = HighLevelPlan(
        execution_mode="standard",
        sub_goals=[
            SubGoal(
                id="sg1",
                description="separate stems",
                reasoning="need stems",
                suggested_tools=["separate_audio_stems"],
                suggested_skills=[],
                depends_on=[],
            )
        ],
        suggested_tools={"sg1": ["separate_audio_stems"]},
        suggested_skills={},
        reasoning="do the thing",
    )
    return plan.to_json()


def _reader(item, objects):
    return PlanReader(
        ddb_client=_FakeDDB(item),
        s3_client=_FakeS3(objects),
        table_name=_TABLE,
        bucket_name=_BUCKET,
    )


def _run(coro):
    """Run an async coroutine to completion in a sync test (repo pattern)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Success path (R1.1)
# ---------------------------------------------------------------------------


def test_read_plan_success_follows_output_key_and_parses():
    item = {"session_id": _SESSION, "node_id": "PLAN", "output_s3_key": _KEY}
    objects = {(_BUCKET, _KEY): _valid_plan_json().encode("utf-8")}
    reader = _reader(item, objects)

    plan = _run(reader.read_plan(_SESSION))

    assert isinstance(plan, HighLevelPlan)
    assert plan.execution_mode == "standard"
    assert [sg.id for sg in plan.sub_goals] == ["sg1"]
    assert plan.sub_goals[0].suggested_tools == ["separate_audio_stems"]


def test_read_plan_falls_back_to_conventional_key_when_row_lacks_output_key():
    # Row exists but carries no explicit output_s3_key — reader uses the
    # conventional plans/{session}/planning/output.json path.
    item = {"session_id": _SESSION, "node_id": "PLAN"}
    objects = {(_BUCKET, _KEY): _valid_plan_json().encode("utf-8")}
    reader = _reader(item, objects)

    plan = _run(reader.read_plan(_SESSION))
    assert isinstance(plan, HighLevelPlan)


def test_read_plan_never_returns_s3_location():
    # R1.5: read_plan returns only the parsed plan; the return value carries no
    # bucket/key. (The S3 location is internal to the reader.)
    item = {"session_id": _SESSION, "node_id": "PLAN", "output_s3_key": _KEY}
    objects = {(_BUCKET, _KEY): _valid_plan_json().encode("utf-8")}
    reader = _reader(item, objects)

    plan = _run(reader.read_plan(_SESSION))
    serialized = json.dumps(plan.to_dict())
    assert _BUCKET not in serialized
    assert _KEY not in serialized


# ---------------------------------------------------------------------------
# Missing plan (R1.3) — no OMS contact
# ---------------------------------------------------------------------------


def test_read_plan_missing_ddb_row_raises_missing_plan_no_oms():
    oms = _RecordingOMS()
    reader = _reader(item=None, objects={})  # no DDB row

    with pytest.raises(MissingPlanError) as exc:
        _run(reader.read_plan(_SESSION))

    assert exc.value.session_id == _SESSION
    assert exc.value.failing_stage == "read_plan"
    assert oms.calls == []


def test_read_plan_missing_s3_object_raises_missing_plan_no_oms():
    oms = _RecordingOMS()
    item = {"session_id": _SESSION, "node_id": "PLAN", "output_s3_key": _KEY}
    reader = _reader(item, objects={})  # row present, object absent

    with pytest.raises(MissingPlanError):
        _run(reader.read_plan(_SESSION))

    assert oms.calls == []


# ---------------------------------------------------------------------------
# Parse failure (R1.4) — no OMS contact
# ---------------------------------------------------------------------------


def test_read_plan_invalid_json_raises_parse_error_no_oms():
    oms = _RecordingOMS()
    item = {"session_id": _SESSION, "node_id": "PLAN", "output_s3_key": _KEY}
    objects = {(_BUCKET, _KEY): b"this is not json {{{"}
    reader = _reader(item, objects)

    with pytest.raises(PlanParseError) as exc:
        _run(reader.read_plan(_SESSION))

    assert exc.value.session_id == _SESSION
    assert exc.value.failing_stage == "read_plan"
    assert oms.calls == []


def test_read_plan_valid_json_wrong_shape_raises_parse_error():
    # Valid JSON but missing required HighLevelPlan fields → PlanParseError.
    item = {"session_id": _SESSION, "node_id": "PLAN", "output_s3_key": _KEY}
    objects = {(_BUCKET, _KEY): json.dumps({"not": "a plan"}).encode("utf-8")}
    reader = _reader(item, objects)

    with pytest.raises(PlanParseError):
        _run(reader.read_plan(_SESSION))
