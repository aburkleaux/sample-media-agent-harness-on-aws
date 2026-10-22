"""Plan speed + robustness: concurrent dispatch, per-node runtime sessions, the minute watchdog.

Measured on staging plan 81d9198e (8-shot anime restyle, 2026-10-16): the chainer invoked its 8
ready nodes one after another over 29.4s (two 502 retries and a 15s wait for a replacement microVM
sat in front of every later sibling); all 8 shared the PARENT session id, so they ran in ONE
microVM, and when it was replaced shot1+shot2 died with it; the watchdog only ran when some other
event fired the chainer, so the plan was failed 3h18m later ("no progress for 11922s").

Driven against moto (real DynamoDB semantics: conditional claims, the job-registry GSI) with the
Task Agent invoke replaced by a recorder.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

HERE = Path(__file__).resolve().parent
REGION = "us-west-2"
SID = "0a1b2c3d-1c4d-4a2e-9f31-6b0d5e2a77c1"
PID = "81d9198e-0000-4000-8000-000000000001"
BUCKET = "skills-bkt"

for _k, _v in (("JOB_REGISTRY_TABLE", "job-registry"), ("PLAN_STATE_TABLE", "plan-state"),
               ("GATEWAY_REGISTRY_TABLE", "t"), ("SKILLS_S3_BUCKET", BUCKET),
               ("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")):
    os.environ.setdefault(_k, _v)
_spec = importlib.util.spec_from_file_location("_ch_parallel", HERE / "handler.py")
pc = importlib.util.module_from_spec(_spec)
sys.modules["_ch_parallel"] = pc
_spec.loader.exec_module(pc)

SHOTS = [{"id": f"shot{i}", "tools": ["fal___submit_job"], "depends_on": [],
          "prompt": f"restyle shot {i}", "output_key": f"s{i}", "output_type": "json"}
         for i in range(1, 9)]


class _Invoker:
    """Records each invoke (thread, start, end); optionally sleeps or fails per node."""

    def __init__(self, delay=0.0, fail=(), timeout=()):
        self.calls, self.delay, self.fail, self.timeout = [], delay, set(fail), set(timeout)
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def __call__(self, runtime_session_id, body):
        payload = json.loads(body)
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        start = time.monotonic()
        try:
            time.sleep(self.delay)
            if payload["node_id"] in self.fail:
                raise pc._agentcore.exceptions.ClientError(
                    {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                    "InvokeAgentRuntime")
            if payload["node_id"] in self.timeout:
                raise RuntimeError("Read timeout on endpoint URL: read operation timed out")
            return {"response": b""}
        finally:
            with self.lock:
                self.active -= 1
                self.calls.append({"rsid": runtime_session_id, "payload": payload,
                                   "start": start, "end": time.monotonic()})


@pytest.fixture
def aws(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"),
                 ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        plan = ddb.create_table(
            TableName="plan-state", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"},
                       {"AttributeName": "node_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"},
                                  {"AttributeName": "node_id", "AttributeType": "S"}])
        jobs = ddb.create_table(
            TableName="job-registry", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"},
                                  {"AttributeName": "actorId", "AttributeType": "S"},
                                  {"AttributeName": "updatedAt", "AttributeType": "S"}],
            GlobalSecondaryIndexes=[{
                "IndexName": "actor-status-index",
                "KeySchema": [{"AttributeName": "actorId", "KeyType": "HASH"},
                              {"AttributeName": "updatedAt", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"}}])
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        monkeypatch.setattr(pc, "_plan_table", plan)
        monkeypatch.setattr(pc, "_job_table", jobs)
        monkeypatch.setattr(pc, "_s3", s3)
        monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", BUCKET)
        monkeypatch.setattr(pc, "_WATCHED", set())
        settled = []
        monkeypatch.setattr(pc, "_publish_plan_result", lambda s, p, st, *a, **k: settled.append((p, st)))
        yield {"plan": plan, "jobs": jobs, "settled": settled}


def _write_plan(plan, nodes, plan_id=PID, status="pending", actor="USER#a"):
    plan.put_item(Item={
        "session_id": SID, "node_id": f"PLAN#{plan_id}", "plan_id": plan_id,
        "display_name": "plan-anime", "status": status, "nodes_json": json.dumps(nodes),
        "actor_id": actor, "turn_id": "t", "updated_at": "2026-10-16T21:02:57+00:00",
        "ttl": 9999999999})
    for n in nodes:
        plan.put_item(Item={"session_id": SID, "node_id": f"{plan_id}#{n['id']}", "plan_id": plan_id,
                            "node": n["id"], "status": "pending",
                            "updated_at": "2026-10-16T21:02:57+00:00"})


def _row(plan, node, plan_id=PID):
    return plan.get_item(Key={"session_id": SID, "node_id": f"{plan_id}#{node}"}).get("Item") or {}


def _insert_event(plan_id=PID):
    return {"Records": [{"eventName": "INSERT", "dynamodb": {"NewImage": {
        "session_id": {"S": SID}, "node_id": {"S": f"PLAN#{plan_id}"}, "status": {"S": "pending"}}}}]}


# --- 1. parallel dispatch -------------------------------------------------------------------


def test_eight_ready_nodes_are_invoked_concurrently_and_each_claimed_once(aws, monkeypatch):
    inv = _Invoker(delay=0.4)
    monkeypatch.setattr(pc, "_invoke_task_agent", inv)
    _write_plan(aws["plan"], SHOTS)

    started = time.monotonic()
    pc.handler(_insert_event(), None)
    elapsed = time.monotonic() - started

    assert sorted(c["payload"]["node_id"] for c in inv.calls) == sorted(n["id"] for n in SHOTS)
    assert inv.max_active > 1, "the invokes must overlap"
    assert elapsed < 8 * 0.4 * 0.5, f"8 x 0.4s invokes took {elapsed:.2f}s — still serial"
    for n in SHOTS:
        assert _row(aws["plan"], n["id"])["status"] == "running"
        job = aws["jobs"].get_item(Key={"jobId": f"{PID}#{n['id']}"}).get("Item")
        assert job is None or job.get("status") != "FAILED"


def test_a_redelivered_batch_invokes_nothing(aws, monkeypatch):
    inv = _Invoker()
    monkeypatch.setattr(pc, "_invoke_task_agent", inv)
    _write_plan(aws["plan"], SHOTS)
    pc.handler(_insert_event(), None)
    first = len(inv.calls)
    pc.handler(_insert_event(), None)  # the stream re-delivers the same batch
    assert first == 8 and len(inv.calls) == 8, "claimed nodes must never be invoked twice"


def test_one_failed_invoke_fails_only_its_node_and_a_read_timeout_is_accepted(aws, monkeypatch):
    inv = _Invoker(fail={"shot3"}, timeout={"shot5"})
    monkeypatch.setattr(pc, "_invoke_task_agent", inv)
    monkeypatch.setattr(pc, "_should_retry_node", lambda row: False)
    _write_plan(aws["plan"], SHOTS)
    pc.handler(_insert_event(), None)

    assert _row(aws["plan"], "shot3")["status"] == "failed"
    assert "dispatch failed" in _row(aws["plan"], "shot3")["error"]
    assert _row(aws["plan"], "shot5")["status"] == "running", "a read timeout is an accepted invoke"
    assert all(_row(aws["plan"], n["id"])["status"] == "running"
               for n in SHOTS if n["id"] not in ("shot3",))


def test_bounded_concurrency(aws, monkeypatch):
    monkeypatch.setattr(pc, "_DISPATCH_CONCURRENCY", 3)
    inv = _Invoker(delay=0.15)
    monkeypatch.setattr(pc, "_invoke_task_agent", inv)
    _write_plan(aws["plan"], SHOTS)
    pc.handler(_insert_event(), None)
    assert len(inv.calls) == 8 and inv.max_active <= 3


# --- 2. per-node runtime sessions, parent attribution ------------------------------------------


def test_each_node_gets_its_own_runtime_session_but_attributes_to_the_parent(aws, monkeypatch):
    inv = _Invoker()
    monkeypatch.setattr(pc, "_invoke_task_agent", inv)
    _write_plan(aws["plan"], SHOTS)
    pc.handler(_insert_event(), None)

    rsids = {c["rsid"] for c in inv.calls}
    assert len(rsids) == 8, "one microVM per node — losing one cannot take its siblings with it"
    for c in inv.calls:
        p = c["payload"]
        assert c["rsid"] == pc._node_runtime_session_id(PID, p["node_id"])
        assert c["rsid"].startswith(f"plan-session-{PID}-")
        # Session outputs, render manifests, job-registry rows and plan-state rows all key on these.
        assert p["session_id"] == SID and p["__plan_session_id"] == SID
        assert p["plan_id"] == PID
    # Plan-state rows keep the "<plan_id>#<node>" key format under the PARENT session partition.
    assert _row(aws["plan"], "shot1")["status"] == "running"


def test_error_aware_retry_runs_in_a_new_session_with_its_error(aws, monkeypatch):
    inv = _Invoker()
    monkeypatch.setattr(pc, "_invoke_task_agent", inv)
    nodes = [SHOTS[0]]
    _write_plan(aws["plan"], nodes)
    aws["plan"].update_item(
        Key={"session_id": SID, "node_id": f"{PID}#shot1"},
        UpdateExpression="SET #st = :f, #err = :e",
        ExpressionAttributeNames={"#st": "status", "#err": "error"},
        ExpressionAttributeValues={":f": "failed", ":e": "node stalled: no progress for 950s (watchdog)"})
    pc.handler({"Records": [{"eventName": "MODIFY", "dynamodb": {"NewImage": {
        "session_id": {"S": SID}, "node_id": {"S": f"{PID}#shot1"}, "status": {"S": "failed"}}}}]}, None)

    assert len(inv.calls) == 1
    assert inv.calls[0]["rsid"] == pc._node_runtime_session_id(PID, "shot1", 1)
    assert "YOUR PREVIOUS ATTEMPT AT THIS STEP FAILED" in inv.calls[0]["payload"]["prompt"]
    assert _row(aws["plan"], "shot1")["status"] == "running"
    assert int(_row(aws["plan"], "shot1")["retries"]) == 1


# --- 3. watchdog thresholds + the minute sweep ---------------------------------------------------


def _set_running(plan, node, quiet_for, plan_id=PID, **extra):
    names = {"#st": "status", "#ttl": "ttl"}
    values = {":r": "running", ":t": int(time.time()) + pc._PLAN_TTL_SECONDS - int(quiet_for)}
    expr = "SET #st = :r, #ttl = :t"
    for i, (k, v) in enumerate(extra.items()):
        expr += f", {k} = :x{i}"
        values[f":x{i}"] = v
    plan.update_item(Key={"session_id": SID, "node_id": f"{plan_id}#{node}"},
                     UpdateExpression=expr, ExpressionAttributeNames=names,
                     ExpressionAttributeValues=values)


def _sweep(context=None):
    return pc.handler({"source": "aws.events", "detail-type": "Scheduled Event"}, context)


def test_threshold_is_exec_timeout_plus_margin():
    assert pc._PLAN_NODE_STALL_SECONDS == 900  # PLAN_NODE_EXEC_TIMEOUT 600 + 300


def test_sweep_reaps_a_dead_node_without_any_stream_event(aws, monkeypatch):
    inv = _Invoker()
    monkeypatch.setattr(pc, "_invoke_task_agent", inv)
    nodes = SHOTS[:3]
    _write_plan(aws["plan"], nodes)
    pc.handler(_insert_event(), None)  # dispatch puts the plan on the watch list
    inv.calls.clear()
    _set_running(aws["plan"], "shot1", quiet_for=901)   # died with its microVM
    _set_running(aws["plan"], "shot2", quiet_for=899)   # just under: still legitimately running
    _set_running(aws["plan"], "shot3", quiet_for=5)

    stats = _sweep()

    assert stats["reaped"] == 1
    assert _row(aws["plan"], "shot1")["status"] == "failed"
    assert "node stalled" in _row(aws["plan"], "shot1")["error"]
    assert _row(aws["plan"], "shot2")["status"] == "running"
    assert _row(aws["plan"], "shot3")["status"] == "running"
    assert inv.calls == [], "the sweep only reaps; the failed write's stream event does the rest"
    job = aws["jobs"].get_item(Key={"jobId": f"{PID}#shot1"}).get("Item")
    assert job and job["status"] == "FAILED"


def test_a_node_waiting_on_a_registered_async_job_is_not_stalled(aws, monkeypatch):
    monkeypatch.setattr(pc, "_invoke_task_agent", _Invoker())
    _write_plan(aws["plan"], SHOTS[:2])
    pc.handler(_insert_event(), None)
    _set_running(aws["plan"], "shot1", quiet_for=1500)
    _set_running(aws["plan"], "shot2", quiet_for=1500)
    recent = datetime.now(timezone.utc).isoformat()
    # shot1's turn submitted a fal job that is still being polled (registered by the job-tracking
    # wrap with runId + planNodeId); shot2's job already FAILED terminally.
    aws["jobs"].put_item(Item={"jobId": "fal-req-1", "actorId": "USER#a", "updatedAt": recent,
                               "runId": PID, "planNodeId": "shot1", "status": "RUNNING",
                               "gatewayId": "harness"})
    aws["jobs"].put_item(Item={"jobId": "fal-req-2", "actorId": "USER#a", "updatedAt": recent,
                               "runId": PID, "planNodeId": "shot2", "status": "FAILED",
                               "gatewayId": "harness"})
    # A coordinator row for the node is not a job it submitted.
    aws["jobs"].put_item(Item={"jobId": f"{PID}#shot2", "actorId": "USER#a", "updatedAt": recent,
                               "runId": PID, "nodeId": "shot2", "status": "RUNNING",
                               "gatewayId": "__coordinator__"})

    _sweep()

    assert _row(aws["plan"], "shot1")["status"] == "running", "an in-flight fal job is not a hang"
    assert _row(aws["plan"], "shot2")["status"] == "failed"


def test_a_node_with_a_pending_job_id_on_its_row_is_exempt(aws, monkeypatch):
    monkeypatch.setattr(pc, "_invoke_task_agent", _Invoker())
    _write_plan(aws["plan"], SHOTS[:1])
    pc.handler(_insert_event(), None)
    arn = "arn:aws:states:us-west-2:1:execution:m2c:abc"
    _set_running(aws["plan"], "shot1", quiet_for=5000, job_id=arn)
    aws["jobs"].put_item(Item={"jobId": arn, "actorId": "x", "updatedAt": "2026-01-01",
                               "status": "RUNNING"})
    _sweep()
    assert _row(aws["plan"], "shot1")["status"] == "running"


def test_registry_lookup_error_never_reaps(aws, monkeypatch):
    monkeypatch.setattr(pc, "JOB_REGISTRY_ACTOR_INDEX", "no-such-index")
    assert pc._node_has_inflight_registered_job("USER#a", PID, "shot1", 1000) is True


def test_watchdog_never_overwrites_a_node_that_just_reported(aws):
    _write_plan(aws["plan"], SHOTS[:1])
    aws["plan"].update_item(Key={"session_id": SID, "node_id": f"{PID}#shot1"},
                            UpdateExpression="SET #st = :c", ExpressionAttributeNames={"#st": "status"},
                            ExpressionAttributeValues={":c": "completed"})
    pc._remember_plan_key(PID, f"PLAN#{PID}")
    assert pc._mark_node_failed(SID, PID, "shot1", "node stalled", only_if_running=True) is False
    assert _row(aws["plan"], "shot1")["status"] == "completed"
    assert aws["jobs"].get_item(Key={"jobId": f"{PID}#shot1"}).get("Item") is None


def test_settled_plans_leave_the_watch_list(aws, monkeypatch):
    monkeypatch.setattr(pc, "_invoke_task_agent", _Invoker())
    _write_plan(aws["plan"], SHOTS[:1])
    pc.handler(_insert_event(), None)
    watch = aws["plan"].query(KeyConditionExpression=boto3.dynamodb.conditions.Key("session_id")
                              .eq(pc._WATCH_PARTITION))["Items"]
    assert [w["node_id"] for w in watch] == [f"{SID}|{PID}"]

    # The node finishes; the chainer settles the plan and drops it from the list.
    aws["plan"].update_item(Key={"session_id": SID, "node_id": f"{PID}#shot1"},
                            UpdateExpression="SET #st = :c", ExpressionAttributeNames={"#st": "status"},
                            ExpressionAttributeValues={":c": "completed"})
    pc.handler({"Records": [{"eventName": "MODIFY", "dynamodb": {"NewImage": {
        "session_id": {"S": SID}, "node_id": {"S": f"{PID}#shot1"},
        "status": {"S": "completed"}}}}]}, None)
    assert aws["settled"] == [(PID, "SUCCEEDED")]
    assert aws["plan"].query(KeyConditionExpression=boto3.dynamodb.conditions.Key("session_id")
                             .eq(pc._WATCH_PARTITION))["Items"] == []


def test_sweep_drops_a_plan_that_is_gone_or_settled(aws):
    aws["plan"].put_item(Item={"session_id": pc._WATCH_PARTITION, "node_id": f"{SID}|gone",
                               "watched_session": SID, "plan_id": "gone"})
    stats = _sweep()
    assert stats["unwatched"] == 1
    assert aws["plan"].query(KeyConditionExpression=boto3.dynamodb.conditions.Key("session_id")
                             .eq(pc._WATCH_PARTITION))["Items"] == []


def test_watch_rows_never_drive_the_stream_path(monkeypatch):
    seen = []
    monkeypatch.setattr(pc, "_process_plan", seen.append)
    pc.handler({"Records": [{"eventName": "INSERT", "dynamodb": {"NewImage": {
        "session_id": {"S": pc._WATCH_PARTITION}, "node_id": {"S": f"{SID}|{PID}"},
        "status": {"S": ""}}}}]}, None)
    assert seen == []


def test_sweep_respects_the_lambda_time_budget(aws):
    for i in range(3):
        aws["plan"].put_item(Item={"session_id": pc._WATCH_PARTITION, "node_id": f"s{i}|p{i}",
                                   "watched_session": f"s{i}", "plan_id": f"p{i}"})

    class _Ctx:
        def get_remaining_time_in_millis(self):
            return 1000

    assert _sweep(_Ctx())["plans"] == 0
