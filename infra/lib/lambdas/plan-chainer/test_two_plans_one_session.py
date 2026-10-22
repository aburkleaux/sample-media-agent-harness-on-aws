"""Two plans in one session, with the SAME node ids, both progress and complete (R9).

Staging session 7b74d118: plans 132a86e5 and c60cc7cc both used sg1..sg5. Plan-state was keyed
(session_id, node_id) with the plan row at the literal "PLAN", so the second plan's rows
overwrote the first's and neither finished. New plans key their rows "PLAN#<plan_id>" and
"<plan_id>#<node_id>"; a legacy plan (bare keys) still in flight keeps its keys and finishes.

Driven end to end against moto: stream events in, the chainer's real reads and writes, and the
Task Agent replaced by a recorder plus a simulated end-of-run status write.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

HERE = Path(__file__).resolve().parent
REGION = "us-west-2"
SID = "7b74d118-1111-2222-3333-444444444444"
BUCKET = "skills-bkt"

for _k, _v in (("JOB_REGISTRY_TABLE", "job-registry"), ("PLAN_STATE_TABLE", "plan-state"),
               ("GATEWAY_REGISTRY_TABLE", "t"), ("SKILLS_S3_BUCKET", BUCKET),
               ("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")):
    os.environ.setdefault(_k, _v)
_spec = importlib.util.spec_from_file_location("_ch_two_plans", HERE / "handler.py")
pc = importlib.util.module_from_spec(_spec)
sys.modules["_ch_two_plans"] = pc
_spec.loader.exec_module(pc)

TWO_STEP = [{"id": "sg1", "tools": [], "depends_on": [], "prompt": "first"},
            {"id": "sg2", "tools": [], "depends_on": ["sg1"], "prompt": "second"}]


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
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}])
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        monkeypatch.setattr(pc, "_plan_table", plan)
        monkeypatch.setattr(pc, "_job_table", jobs)
        monkeypatch.setattr(pc, "_s3", s3)
        monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", BUCKET)
        dispatched = []
        monkeypatch.setattr(
            pc, "_invoke_task_agent",
            lambda session_id, body: dispatched.append(json.loads(body)) or {"response": b""})
        settled = []
        real_publish = pc._publish_plan_result
        monkeypatch.setattr(pc, "_publish_plan_result",
                            lambda s, p, st, *a, **k: settled.append((p, st)) or real_publish(s, p, st, *a, **k))
        yield {"plan": plan, "jobs": jobs, "dispatched": dispatched, "settled": settled}


def _write_plan(plan, plan_id, nodes, qualified=True, display_name=None):
    """What plan_and_execute writes: the PLAN row, then every node row pending."""
    plan.put_item(Item={
        "session_id": SID, "node_id": f"PLAN#{plan_id}" if qualified else "PLAN",
        "plan_id": plan_id, "display_name": display_name or f"plan-{plan_id}", "status": "pending",
        "nodes_json": json.dumps(nodes), "actor_id": "USER#a", "turn_id": "t",
        "updated_at": "2026-10-08T00:00:00+00:00", "ttl": 9999999999})
    for n in nodes:
        item = {"session_id": SID, "node_id": f"{plan_id}#{n['id']}" if qualified else n["id"],
                "status": "pending", "updated_at": "2026-10-08T00:00:00+00:00"}
        if qualified:
            item.update(plan_id=plan_id, node=n["id"])
        plan.put_item(Item=item)


def _event(sort_key, status, name="MODIFY"):
    return {"Records": [{"eventName": name, "dynamodb": {"NewImage": {
        "session_id": {"S": SID}, "node_id": {"S": sort_key}, "status": {"S": status}}}}]}


def _finish(plan, sort_key):
    """The Task Agent's end-of-run write for a sync node."""
    plan.update_item(Key={"session_id": SID, "node_id": sort_key},
                     UpdateExpression="SET #st = :c", ExpressionAttributeNames={"#st": "status"},
                     ExpressionAttributeValues={":c": "completed"})
    pc.handler(_event(sort_key, "completed"), None)


def _status(plan, sort_key):
    item = plan.get_item(Key={"session_id": SID, "node_id": sort_key}).get("Item")
    return item and item["status"]


def _dispatched(env):
    out = [(p["plan_id"], p["node_id"]) for p in env["dispatched"]]
    env["dispatched"].clear()
    return sorted(out)


def test_two_plans_with_identical_node_ids_both_complete(aws):
    plan = aws["plan"]
    _write_plan(plan, "pA", TWO_STEP)
    _write_plan(plan, "pB", TWO_STEP)
    pc.handler(_event("PLAN#pA", "pending", "INSERT"), None)

    # Both plans' roots dispatch, each claimed on its own row.
    assert _dispatched(aws) == [("pA", "sg1"), ("pB", "sg1")]
    assert _status(plan, "pA#sg1") == "running" and _status(plan, "pB#sg1") == "running"
    claimed = plan.get_item(Key={"session_id": SID, "node_id": "pA#sg1"})["Item"]
    assert claimed["plan_id"] == "pA" and claimed["node"] == "sg1"

    # A's sg1 finishing advances ONLY A.
    _finish(plan, "pA#sg1")
    assert _dispatched(aws) == [("pA", "sg2")]
    assert _status(plan, "pB#sg2") == "pending"

    _finish(plan, "pB#sg1")
    assert _dispatched(aws) == [("pB", "sg2")]

    # A completes while B is still running; B is not affected.
    _finish(plan, "pA#sg2")
    assert _status(plan, "PLAN#pA") == "completed"
    assert _status(plan, "PLAN#pB") == "pending"
    assert aws["settled"] == [("pA", "SUCCEEDED")]

    _finish(plan, "pB#sg2")
    assert _status(plan, "PLAN#pB") == "completed"
    # Settled once each: B's events never re-settle the already-finished A.
    assert aws["settled"] == [("pA", "SUCCEEDED"), ("pB", "SUCCEEDED")]
    assert aws["jobs"].get_item(Key={"jobId": "pA"})["Item"]["status"] == "SUCCEEDED"
    assert aws["jobs"].get_item(Key={"jobId": "pB"})["Item"]["status"] == "SUCCEEDED"
    # Nothing was written under the legacy keys.
    assert _status(plan, "PLAN") is None and _status(plan, "sg1") is None


def test_a_failed_node_skips_only_its_own_plans_dependents(aws):
    plan = aws["plan"]
    _write_plan(plan, "pA", TWO_STEP)
    _write_plan(plan, "pB", TWO_STEP)
    pc.handler(_event("PLAN#pA", "pending", "INSERT"), None)
    _dispatched(aws)

    plan.update_item(Key={"session_id": SID, "node_id": "pA#sg1"},
                     UpdateExpression="SET #st = :f, #e = :e",
                     ExpressionAttributeNames={"#st": "status", "#e": "error"},
                     ExpressionAttributeValues={":f": "failed", ":e": ""})  # no error → no retry
    pc.handler(_event("pA#sg1", "failed"), None)

    assert _status(plan, "pA#sg2") == "skipped"
    assert _status(plan, "PLAN#pA") == "failed"
    assert _status(plan, "pB#sg2") == "pending" and _status(plan, "PLAN#pB") == "pending"


def test_a_legacy_plan_in_flight_finishes_on_its_bare_keys_beside_a_new_plan(aws):
    plan = aws["plan"]
    _write_plan(plan, "legacy", TWO_STEP, qualified=False)
    _write_plan(plan, "pNew", TWO_STEP)
    pc.handler(_event("PLAN", "pending", "INSERT"), None)
    assert _dispatched(aws) == [("legacy", "sg1"), ("pNew", "sg1")]
    assert _status(plan, "sg1") == "running" and _status(plan, "pNew#sg1") == "running"
    assert _status(plan, "legacy#sg1") is None, "a legacy plan must not grow qualified rows"

    _finish(plan, "sg1")
    assert _dispatched(aws) == [("legacy", "sg2")]
    _finish(plan, "sg2")
    assert _status(plan, "PLAN") == "completed"
    assert _status(plan, "PLAN#pNew") == "pending"

    _finish(plan, "pNew#sg1")
    assert _dispatched(aws) == [("pNew", "sg2")]
    _finish(plan, "pNew#sg2")
    assert _status(plan, "PLAN#pNew") == "completed"


def test_results_ready_from_the_bridge_completes_the_qualified_row(aws, monkeypatch):
    """An async node: the bridge marks <plan>#<node> results_ready, the chainer fetches."""
    plan = aws["plan"]
    _write_plan(plan, "pA", TWO_STEP)
    _write_plan(plan, "pB", TWO_STEP)
    pc.handler(_event("PLAN#pA", "pending", "INSERT"), None)
    _dispatched(aws)
    fetched = []
    monkeypatch.setattr(pc, "_dispatch_results_fetch",
                        lambda s, p, a, nid, row, **k: fetched.append((p, nid, row.get("job_id"))))
    plan.update_item(Key={"session_id": SID, "node_id": "pB#sg1"},
                     UpdateExpression="SET #st = :r, job_id = :j",
                     ExpressionAttributeNames={"#st": "status"},
                     ExpressionAttributeValues={":r": "results_ready", ":j": "job-b"})
    pc.handler(_event("pB#sg1", "results_ready"), None)
    assert fetched == [("pB", "sg1", "job-b")]


def test_map_fan_out_children_are_keyed_by_their_plan(aws, monkeypatch):
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "true")
    plan = aws["plan"]
    nodes = [
        {"id": "collect", "kind": "task", "depends_on": [], "prompt": "list", "tools": []},
        {"id": "m1", "kind": "map", "depends_on": ["collect"], "prompt": "", "tools": [],
         "config": {"over": {"from": "collect", "path": "items"}, "item_var": "x",
                    "template": {"prompt": "do {{x}}", "tools": []}}},
        {"id": "g1", "kind": "gather", "depends_on": ["m1"], "prompt": "", "tools": [],
         "config": {"of": "m1"}},
    ]
    _write_plan(plan, "pM", nodes)
    _write_plan(plan, "pOther", nodes)  # same ids in the same session, must stay untouched
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=BUCKET, Key=f"plans/{SID}/pM/collect/output",
        Body=json.dumps({"items": ["a", "b"]}).encode())
    plan.update_item(Key={"session_id": SID, "node_id": "pM#collect"},
                     UpdateExpression="SET #st = :c, result_s3_key = :k",
                     ExpressionAttributeNames={"#st": "status"},
                     ExpressionAttributeValues={":c": "completed",
                                                ":k": f"plans/{SID}/pM/collect/output"})
    pc.handler(_event("pM#collect", "completed"), None)

    assert _dispatched(aws) == [("pM", "m1__0"), ("pM", "m1__1"), ("pOther", "collect")]
    for child in ("m1__0", "m1__1"):
        row = plan.get_item(Key={"session_id": SID, "node_id": f"pM#{child}"})["Item"]
        assert row["plan_id"] == "pM" and row["node"] == child
        assert row["status"] in ("pending", "running")
    assert _status(plan, "pM#m1") == "completed"
    plan_row = plan.get_item(Key={"session_id": SID, "node_id": "PLAN#pM"})["Item"]
    assert {n["id"] for n in json.loads(plan_row["nodes_json"])} >= {"m1__0", "m1__1"}
    assert _status(plan, "pOther#m1") == "pending" and _status(plan, "m1__0") is None

    for child in ("m1__0", "m1__1"):
        _finish(plan, f"pM#{child}")
    assert _status(plan, "pM#g1") == "completed"
    pc.handler(_event("pM#g1", "completed"), None)
    assert _status(plan, "PLAN#pM") == "completed"


def test_external_input_resolves_the_named_plans_producer(aws):
    """Guided phases reuse node ids, so a producer is picked by its plan's display name."""
    plan = aws["plan"]
    s3 = boto3.client("s3", region_name=REGION)
    for pid, uri in (("pFound", "s3://b/found/transcript.json"), ("pOther", "s3://b/other/x.json")):
        _write_plan(plan, pid, [{"id": "sg2", "tools": [], "depends_on": [], "prompt": "p"}])
        plan.update_item(Key={"session_id": SID, "node_id": f"{pid}#sg2"},
                         UpdateExpression="SET #st = :c, result_s3_key = :k, updated_at = :u",
                         ExpressionAttributeNames={"#st": "status"},
                         ExpressionAttributeValues={
                             ":c": "completed", ":k": f"plans/{SID}/{pid}/sg2/output",
                             ":u": "2026-10-08T05:00:00" if pid == "pOther" else "2026-10-08T01:00:00"})
        s3.put_object(Bucket=BUCKET, Key=f"plans/{SID}/{pid}/manifest.json", Body=json.dumps(
            {"outputs": {"sg2": {"status": "completed", "value": {"transcript_uri": uri}}}}).encode())
    node_def = {"id": "sg4", "external_inputs": [{"plan": "plan-pFound", "node": "sg2"}]}
    text = pc._external_injection_text(node_def, SID, "pConsumer")
    assert "found/transcript.json" in text and "other/x.json" not in text
    # Without a name, two plans hold sg2: ambiguous, so neither is guessed (no "newest wins").
    text = pc._external_injection_text(
        {"id": "sg4", "external_inputs": [{"plan": "", "node": "sg2"}]}, SID, "pConsumer")
    assert "UNAVAILABLE" in text and "other/x.json" not in text and "found/transcript.json" not in text
    # A reference may also name the producer plan by its plan_id.
    text = pc._external_injection_text(
        {"id": "sg4", "external_inputs": [{"plan": "pOther", "node": "sg2"}]}, SID, "pConsumer")
    assert "other/x.json" in text and "found/transcript.json" not in text


def _completed_producer(plan, s3, pid, uri, updated_at, display_name):
    _write_plan(plan, pid, [{"id": "sg1", "tools": [], "depends_on": [], "prompt": "p"}],
                display_name=display_name)
    plan.update_item(Key={"session_id": SID, "node_id": f"{pid}#sg1"},
                     UpdateExpression="SET #st = :c, result_s3_key = :k, updated_at = :u",
                     ExpressionAttributeNames={"#st": "status"},
                     ExpressionAttributeValues={":c": "completed", ":k": f"plans/{SID}/{pid}/sg1/output",
                                                ":u": updated_at})
    s3.put_object(Bucket=BUCKET, Key=f"plans/{SID}/{pid}/manifest.json", Body=json.dumps(
        {"outputs": {"sg1": {"status": "completed", "value": {"shots_uri": uri}}}}).encode())


def test_same_named_producer_plans_fail_the_consumer_instead_of_taking_the_newest(aws):
    """Staging session d6a560a8: three Phase 1 plans (one per video), all named
    plan-content-moderation-foundation. "Newest wins" gave every Phase 2 node NBA's shots."""
    plan = aws["plan"]
    s3 = boto3.client("s3", region_name=REGION)
    name = "plan-content-moderation-foundation"
    for pid, uri, at in (("pMeridian", "s3://b/meridian/shots.json", "2026-10-09T11:47:25"),
                         ("pTidal", "s3://b/tidal/shots.json", "2026-10-09T11:53:05"),
                         ("pNba", "s3://b/nba/shots.json", "2026-10-09T12:03:20")):
        _completed_producer(plan, s3, pid, uri, at, name)
    _write_plan(plan, "pAnalysis", [{"id": "sg3", "tools": [], "depends_on": [], "prompt": "enrich",
                                     "external_inputs": [{"plan": name, "node": "sg1"}]}],
                display_name="plan-content-moderation-analysis")
    pc.handler(_event("PLAN#pAnalysis", "pending", "INSERT"), None)
    assert _dispatched(aws) == []  # never dispatched with a guessed producer
    row = plan.get_item(Key={"session_id": SID, "node_id": "pAnalysis#sg3"})["Item"]
    assert row["status"] == "failed"
    assert row["error"].startswith(f"ambiguous external_inputs: 3 plans named '{name}' hold \"sg1\"")
    assert "pMeridian, pNba, pTidal" in row["error"]
    assert pc._is_missing_input_error(row["error"])  # not retried: the agent cannot fix it by guessing


def test_a_uniquely_named_producer_still_hands_off(aws):
    plan = aws["plan"]
    s3 = boto3.client("s3", region_name=REGION)
    _completed_producer(plan, s3, "pOne", "s3://b/one/shots.json", "2026-10-09T11:00:00", "phase-1")
    _completed_producer(plan, s3, "pTwo", "s3://b/two/shots.json", "2026-10-09T12:00:00", "phase-1-rerun")
    node = {"id": "sg3", "external_inputs": [{"plan": "phase-1", "node": "sg1"}]}
    assert pc._external_inputs_ambiguity(node, SID, "pConsumer") == ""
    text = pc._external_injection_text(node, SID, "pConsumer")
    assert "one/shots.json" in text and "two/shots.json" not in text


def test_error_aware_retry_redispatches_on_the_qualified_row(aws):
    plan = aws["plan"]
    _write_plan(plan, "pA", TWO_STEP)
    _write_plan(plan, "pB", TWO_STEP)
    pc.handler(_event("PLAN#pA", "pending", "INSERT"), None)
    _dispatched(aws)
    plan.update_item(Key={"session_id": SID, "node_id": "pA#sg1"},
                     UpdateExpression="SET #st = :f, #e = :e",
                     ExpressionAttributeNames={"#st": "status", "#e": "error"},
                     ExpressionAttributeValues={":f": "failed",
                                                ":e": "ValidationException: width must be > 0"})
    pc.handler(_event("pA#sg1", "failed"), None)

    assert _dispatched(aws) == [("pA", "sg1")]
    row = plan.get_item(Key={"session_id": SID, "node_id": "pA#sg1"})["Item"]
    assert row["status"] == "running" and int(row["retries"]) == 1
    assert row["plan_id"] == "pA" and row["node"] == "sg1"
    assert _status(plan, "pB#sg1") == "running" and _status(plan, "sg1") is None


def _set(plan, sort_key, status):
    plan.update_item(Key={"session_id": SID, "node_id": sort_key},
                     UpdateExpression="SET #st = :s", ExpressionAttributeNames={"#st": "status"},
                     ExpressionAttributeValues={":s": status})


def test_rerunning_a_failed_phase_under_the_same_name_still_hands_off(aws):
    """A failed Phase 1 re-run under the skill's required name: the failed attempt's producer
    row is not a candidate, so the retry's output is used (no "ambiguous external_inputs")."""
    plan = aws["plan"]
    s3 = boto3.client("s3", region_name=REGION)
    name = "plan-content-moderation-foundation"
    _completed_producer(plan, s3, "pFailed", "s3://b/failed/shots.json", "2026-10-09T11:00:00", name)
    _set(plan, "pFailed#sg1", "failed")
    _set(plan, "PLAN#pFailed", "failed")
    _completed_producer(plan, s3, "pRetry", "s3://b/retry/shots.json", "2026-10-09T12:00:00", name)
    node = {"id": "sg3", "external_inputs": [{"plan": name, "node": "sg1"}]}
    assert pc._external_inputs_ambiguity(node, SID, "pConsumer") == ""
    text = pc._external_injection_text(node, SID, "pConsumer")
    assert "retry/shots.json" in text and "failed/shots.json" not in text


def test_a_failed_plan_whose_producer_node_completed_is_superseded_by_its_rerun(aws):
    """Phase 1 failed AFTER sg1 completed; the re-run's sg1 wins, and the failed plan's copy is
    used only when nothing else holds the node."""
    plan = aws["plan"]
    s3 = boto3.client("s3", region_name=REGION)
    name = "plan-content-moderation-foundation"
    _completed_producer(plan, s3, "pFailed", "s3://b/failed/shots.json", "2026-10-09T11:00:00", name)
    _set(plan, "PLAN#pFailed", "failed")  # sg1 itself stays completed
    node = {"id": "sg3", "external_inputs": [{"plan": name, "node": "sg1"}]}
    assert "failed/shots.json" in pc._external_injection_text(node, SID, "pConsumer")

    _completed_producer(plan, s3, "pRetry", "s3://b/retry/shots.json", "2026-10-09T12:00:00", name)
    assert pc._external_inputs_ambiguity(node, SID, "pConsumer") == ""
    text = pc._external_injection_text(node, SID, "pConsumer")
    assert "retry/shots.json" in text and "failed/shots.json" not in text


def test_a_running_rerun_beside_a_completed_same_named_plan_stays_ambiguous(aws):
    """Pending/running producers stay in the pool (readiness waits on them), so two LIVE
    same-named plans still fail closed rather than guessing."""
    plan = aws["plan"]
    s3 = boto3.client("s3", region_name=REGION)
    name = "plan-content-moderation-foundation"
    _completed_producer(plan, s3, "pA", "s3://b/a/shots.json", "2026-10-09T11:00:00", name)
    _write_plan(plan, "pB", [{"id": "sg1", "tools": [], "depends_on": [], "prompt": "p"}],
                display_name=name)
    _set(plan, "pB#sg1", "running")
    node = {"id": "sg3", "external_inputs": [{"plan": name, "node": "sg1"}]}
    assert pc._external_inputs_ambiguity(node, SID, "pConsumer").startswith("ambiguous external_inputs")
