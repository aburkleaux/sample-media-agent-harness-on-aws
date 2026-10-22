"""Session c3447913 (plan-meridian-reel-recovery): a plan node that waited for its own fal job.

The job finished while its node was still running. Three things then went wrong, one per fix here:

1. The job row did not name its node, so the changefeed bridge could only guess "the first running
   node" (tested in infra/test/changefeed-plan-node-match.test.ts). Now the job-tracking wrap stamps
   ``planNodeId`` from the bound node (``jobs.context.set_node_id``).
2. The node's end-of-run write parked it in ``processing`` although the job was already terminal, and
   nothing fires again for a terminal job. Now it reads the job's status (``_node_status_for_queue_job``).
3. That write was an unconditional full-item put, so it could overwrite a row the bridge/chainer had
   already advanced. Now it is guarded (``StateStore.put_node_status(unless_status=...)``).
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jobs import context as jobs_context  # noqa: E402
from jobs.models import JobRecord  # noqa: E402
from jobs.registry import JobRegistry  # noqa: E402
from state_store import StateStore  # noqa: E402

REGION = "us-west-2"
SID = "c3447913-08f2-43bd-be0c-a6314e389edc"
JOB = "01a1142a-d61a-7731-b64a-08f397b81c09"


@pytest.fixture
def aws(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(TableName="plan-state", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"},
                                    {"AttributeName": "node_id", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"},
                                               {"AttributeName": "node_id", "AttributeType": "S"}])
        ddb.create_table(TableName="job-registry", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}])
        yield ddb


# ---------------------------------------------------------------- 3. guarded end-of-run write
ADVANCED = ("results_ready", "completed", "failed", "skipped")


@pytest.mark.parametrize("current", ADVANCED)
def test_end_of_run_write_never_regresses_an_advanced_row(aws, current):
    store = StateStore("plan-state", REGION)
    aws.Table("plan-state").put_item(Item={"session_id": SID, "node_id": "animate_shot2", "status": current,
                                           "job_id": JOB})
    wrote = store.put_node_status(SID, "animate_shot2", "processing", job_id=JOB, unless_status=ADVANCED)
    assert wrote is False
    assert aws.Table("plan-state").get_item(Key={"session_id": SID, "node_id": "animate_shot2"})["Item"]["status"] == current


@pytest.mark.parametrize("current", [None, "running", "pending", "processing"])
def test_end_of_run_write_lands_on_a_row_still_in_flight(aws, current):
    store = StateStore("plan-state", REGION)
    if current:
        aws.Table("plan-state").put_item(Item={"session_id": SID, "node_id": "animate_shot2", "status": current,
                                               "retries": 1})
    assert store.put_node_status(SID, "animate_shot2", "processing", job_id=JOB, unless_status=ADVANCED) is True
    row = aws.Table("plan-state").get_item(Key={"session_id": SID, "node_id": "animate_shot2"})["Item"]
    assert row["status"] == "processing" and row["job_id"] == JOB
    if current:
        assert row["retries"] == 1  # retry accounting still carried forward


def test_unguarded_write_is_unchanged(aws):
    store = StateStore("plan-state", REGION)
    aws.Table("plan-state").put_item(Item={"session_id": SID, "node_id": "n", "status": "completed"})
    assert store.put_node_status(SID, "n", "running") is True
    assert aws.Table("plan-state").get_item(Key={"session_id": SID, "node_id": "n"})["Item"]["status"] == "running"


# ---------------------------------------------------------------- 2. reconcile the queue job's status
@pytest.fixture
def ta(aws, monkeypatch):
    import task_agent

    monkeypatch.setattr(task_agent, "JOB_REGISTRY_TABLE", "job-registry")
    monkeypatch.setattr(task_agent, "REGION", REGION)
    return task_agent


@pytest.mark.parametrize("status,expected", [
    ("SUCCEEDED", ("results_ready", "")),       # the c3447913 case: the job won the race
    ("RUNNING", ("processing", "")),            # the normal async path
    ("PENDING", ("processing", "")),
    ("FAILED", ("failed", "duration must be 6 or 10")),
    ("TIMED_OUT", ("failed", "duration must be 6 or 10")),
])
def test_queue_job_status_decides_the_node_status(ta, aws, status, expected):
    aws.Table("job-registry").put_item(Item={"jobId": JOB, "status": status, "actorId": "a",
                                             "lastError": "duration must be 6 or 10"})
    assert ta._node_status_for_queue_job(JOB) == expected


def test_unknown_or_unreadable_queue_job_keeps_the_async_path(ta, monkeypatch):
    assert ta._node_status_for_queue_job("no-such-job") == ("processing", "")
    assert ta._node_status_for_queue_job("") == ("processing", "")
    monkeypatch.setattr(ta, "JOB_REGISTRY_TABLE", "")
    assert ta._node_status_for_queue_job(JOB) == ("processing", "")


def test_end_of_run_uses_the_reconcile_and_the_guard():
    """`_execute_plan_node` is too large to drive here; pin the wiring like its sibling tests do."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "task_agent.py").read_text()
    body = src[src.index("async def _execute_plan_node"):src.index("async def _fetch_plan_node_results")]
    q = body.index("elif queue_handle:")
    assert "_node_status_for_queue_job(queue_handle[\"job_id\"])" in body[q:q + 1500]
    assert "unless_status=_NODE_ROW_ADVANCED" in body
    assert "jobs_context.set_node_id(node_id)" in body and "jobs_context.reset_node_id(" in body


# ---------------------------------------------------------------- 1. the job row names its node
def test_node_id_context_round_trip():
    def run():
        assert jobs_context.current_node_id() is None
        tok = jobs_context.set_node_id("animate_shot2")
        assert jobs_context.current_node_id() == "animate_shot2"
        jobs_context.reset_node_id(tok)
        assert jobs_context.current_node_id() is None
        assert jobs_context.set_node_id("") is not None and jobs_context.current_node_id() is None  # "" = unset

    contextvars.copy_context().run(run)


def test_registry_round_trips_plan_node_id_without_touching_node_id():
    rec = JobRecord(job_id=JOB, actor_id="a", session_id=SID, trio_id="t", gateway_id="g",
                    status_tool="s", results_tool="r", status="PENDING", start_at="x", updated_at="x",
                    plan_node_id="animate_shot2")
    item = JobRegistry._to_item(rec)
    # planNodeId, NOT nodeId: a row with nodeId is drawn as a plan node by the UI (PlanDagView).
    assert item["planNodeId"] == "animate_shot2" and "nodeId" not in item
    assert JobRegistry._from_item(item).plan_node_id == "animate_shot2"


def test_job_tracking_wrap_stamps_the_bound_plan_node():
    from jobs import strands_tool_wrap as stw
    from jobs.models import JobTrio

    captured = []

    class FakeRegistry:
        def register(self, record):
            captured.append(record)
            return record

    tool = stw.JobTrackingMCPAgentTool.__new__(stw.JobTrackingMCPAgentTool)
    tool._trio = JobTrio(trio_id="usr-1|fal", gateway_id="harness-user-mcp", start_tool="usr-1___submit_job",
                         status_tool="usr-1___check_job", results_tool="usr-1___get_job_result")
    tool._registry = FakeRegistry()
    tool._agent_tool_name = "usr-1___submit_job"
    tool._actor_id_resolver = lambda: "actor-1"
    tool._session_id_resolver = lambda: SID
    result = {"status": "success", "toolUseId": "t", "content": [{"text": json.dumps({
        "request_id": JOB, "endpoint_id": "minimax/h3-max",
        "status_url": f"https://queue.fal.run/minimax/h3-max/requests/{JOB}/status"})}]}

    async def submit_inside(node):
        tok = jobs_context.set_node_id(node) if node else None
        rtok = jobs_context.set_run_id("plan-1")
        try:
            await tool._maybe_register_start(result, {"toolUseId": "t", "name": "usr-1___submit_job", "input": {}})
        finally:
            jobs_context.reset_run_id(rtok)
            if tok:
                jobs_context.reset_node_id(tok)

    asyncio.run(submit_inside("animate_shot2"))
    asyncio.run(submit_inside(None))
    assert [r.plan_node_id for r in captured] == ["animate_shot2", None]
    assert all(r.job_id == JOB and r.run_id == "plan-1" for r in captured)
