"""Plan-state rows are keyed by plan id (spec media-harness-asset-lineage, R9 / design §5).

The table is keyed (session_id, node_id). The plan row was the literal "PLAN" and node rows
the bare node id, so two plans in one session with the same node ids (sg1..sg5) overwrote each
other and neither finished (staging session 7b74d118, plans 132a86e5 and c60cc7cc).

New plans write "PLAN#<plan_id>" and "<plan_id>#<node_id>", with plan_id and the bare `node`
on every row. A plan already in flight on the bare keys keeps writing them until it finishes.
"""
from __future__ import annotations

import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orchestration import plan_and_execute_tool as pae  # noqa: E402
from state_store import StateStore, node_sk, plan_row_of, plan_sk  # noqa: E402

REGION = "us-west-2"
SID = "7b74d118-0000-0000-0000-000000000000"


@pytest.fixture
def store(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"),
                 ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(k, v)
    with mock_aws():
        boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName="plan-state", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"},
                       {"AttributeName": "node_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"},
                                  {"AttributeName": "node_id", "AttributeType": "S"}])
        yield StateStore("plan-state", REGION)


def _row(store, sk):
    return store._table.get_item(Key={"session_id": SID, "node_id": sk}).get("Item")


def test_key_helpers():
    assert plan_sk("p1") == "PLAN#p1" and plan_sk("") == "PLAN"
    assert node_sk("p1", "sg1") == "p1#sg1" and node_sk("", "sg1") == "sg1"
    assert plan_row_of("PLAN#p1") == "PLAN" and plan_row_of("p1#sg1") == "sg1"


# ---- put_node_status writes in the plan's own format -------------------------------------------

def test_status_write_lands_on_the_qualified_row(store):
    store._table.put_item(Item={"session_id": SID, "node_id": "pA#sg1", "plan_id": "pA",
                                "node": "sg1", "status": "running", "retries": 1})
    store.put_node_status(SID, "sg1", "completed", plan_id="pA", result_s3_key="k")
    row = _row(store, "pA#sg1")
    assert row["status"] == "completed" and row["plan_id"] == "pA" and row["node"] == "sg1"
    assert row["retries"] == 1  # retry accounting still carried
    assert _row(store, "sg1") is None, "must not create a bare row beside the qualified one"


def test_two_plans_with_the_same_node_id_keep_separate_rows(store):
    for pid in ("pA", "pB"):
        store._table.put_item(Item={"session_id": SID, "node_id": f"{pid}#sg1",
                                    "plan_id": pid, "node": "sg1", "status": "running"})
    store.put_node_status(SID, "sg1", "completed", plan_id="pA")
    store.put_node_status(SID, "sg1", "failed", plan_id="pB", error="boom")
    assert _row(store, "pA#sg1")["status"] == "completed"
    assert _row(store, "pB#sg1")["status"] == "failed"


def test_a_legacy_plan_in_flight_keeps_writing_its_bare_row(store):
    store._table.put_item(Item={"session_id": SID, "node_id": "sg1", "plan_id": "old",
                                "status": "running"})
    store.put_node_status(SID, "sg1", "completed", plan_id="old")
    assert _row(store, "sg1")["status"] == "completed"
    assert _row(store, "old#sg1") is None


def test_an_unstamped_legacy_row_is_still_used(store):
    store._table.put_item(Item={"session_id": SID, "node_id": "sg1", "status": "pending"})
    store.put_node_status(SID, "sg1", "running", plan_id="old")
    assert _row(store, "sg1")["status"] == "running"


def test_another_plans_bare_row_is_not_overwritten(store):
    store._table.put_item(Item={"session_id": SID, "node_id": "sg1", "plan_id": "old",
                                "status": "completed"})
    store.put_node_status(SID, "sg1", "running", plan_id="new")
    assert _row(store, "sg1")["status"] == "completed"
    assert _row(store, "new#sg1")["status"] == "running"


def test_the_no_regress_guard_reads_the_qualified_row(store):
    store._table.put_item(Item={"session_id": SID, "node_id": "pA#sg1", "plan_id": "pA",
                                "node": "sg1", "status": "completed"})
    wrote = store.put_node_status(SID, "sg1", "processing", plan_id="pA",
                                  unless_status=("completed",))
    assert wrote is False and _row(store, "pA#sg1")["status"] == "completed"


def test_conditional_update_addresses_the_qualified_row(store):
    store._table.put_item(Item={"session_id": SID, "node_id": "pA#sg1", "plan_id": "pA",
                                "node": "sg1", "status": "pending"})
    assert store.update_node_status(SID, "sg1", "running", "pending", plan_id="pA") is True
    assert _row(store, "pA#sg1")["status"] == "running"


# ---- get_plan_state ----------------------------------------------------------------------------

def _seed_two_plans(store):
    for pid, ts in (("pA", "2026-10-08T01:00:00"), ("pB", "2026-10-08T02:00:00")):
        store._table.put_item(Item={"session_id": SID, "node_id": f"PLAN#{pid}",
                                    "plan_id": pid, "node": "PLAN", "updated_at": ts})
        store._table.put_item(Item={"session_id": SID, "node_id": f"{pid}#sg1",
                                    "plan_id": pid, "node": "sg1", "status": "pending"})


def test_plan_state_by_plan_id(store):
    _seed_two_plans(store)
    state = store.get_plan_state(SID, plan_id="pA")
    assert state["plan"]["plan_id"] == "pA"
    assert [n["node_id"] for n in state["nodes"]] == ["pA#sg1"]


def test_plan_state_without_an_id_is_the_newest_plan(store):
    _seed_two_plans(store)
    state = store.get_plan_state(SID)
    assert state["plan"]["plan_id"] == "pB"
    assert [n["node_id"] for n in state["nodes"]] == ["pB#sg1"]


def test_plan_state_falls_back_to_legacy_rows(store):
    store._table.put_item(Item={"session_id": SID, "node_id": "PLAN", "plan_id": "old"})
    store._table.put_item(Item={"session_id": SID, "node_id": "sg1", "status": "running"})
    state = store.get_plan_state(SID, plan_id="old")
    assert state["plan"]["plan_id"] == "old"
    assert [n["node_id"] for n in state["nodes"]] == ["sg1"]


# ---- plan_and_execute writes the qualified keys ------------------------------------------------

class _Node:
    def __init__(self, node_id):
        self.id, self.tools, self.depends_on = node_id, ["code_interpreter"], []
        self.prompt, self.output_type, self.output_key, self.skills = "p", "json", None, []

    def to_dict(self):
        return {"id": self.id, "tools": self.tools, "depends_on": [], "prompt": self.prompt}


class _Plan:
    def __init__(self):
        self.nodes, self.goal, self.plan_id = [_Node("sg1"), _Node("sg2")], "g", "plan-display"


def _dispatch(monkeypatch, store):
    import planning.plan_manifest as plan_manifest
    import state_store as state_store_mod
    from jobs import context as jobs_context

    class _Manifest:
        s3_uri = "s3://b/k"

        def __init__(self, *a, **k):
            pass

        def create(self, **k):
            return None

    monkeypatch.setattr(state_store_mod, "StateStore", lambda *a, **k: store)
    monkeypatch.setattr(plan_manifest, "PlanManifest", _Manifest)
    monkeypatch.setattr(pae, "PLAN_STATE_TABLE", "plan-state")
    monkeypatch.setattr(pae, "JOB_REGISTRY_TABLE", "")
    monkeypatch.setattr(pae, "SKILLS_S3_BUCKET", "")
    monkeypatch.setattr(jobs_context, "current_actor_id", lambda: "USER#alice")
    monkeypatch.setattr(jobs_context, "current_session_id", lambda: SID)
    monkeypatch.setattr(jobs_context, "current_turn_id", lambda: "turn-1")
    result = pae._dispatch_plan_async(
        tool_use_id="tu", plan=_Plan(), execution_mode="standard",
        cumulative_tokens={"inputTokens": 0, "outputTokens": 0}, session_id=SID)
    assert result["status"] == "success", result
    return result["executionSummary"]["plan_id"]


def test_dispatch_writes_rows_keyed_by_plan_id(monkeypatch, store):
    pid = _dispatch(monkeypatch, store)
    plan_row = _row(store, f"PLAN#{pid}")
    assert plan_row and plan_row["plan_id"] == pid and plan_row["status"] == "pending"
    for nid in ("sg1", "sg2"):
        row = _row(store, f"{pid}#{nid}")
        assert row["plan_id"] == pid and row["node"] == nid and row["status"] == "pending"
    assert _row(store, "PLAN") is None and _row(store, "sg1") is None


def test_a_second_plan_in_the_session_does_not_overwrite_the_first(monkeypatch, store):
    first = _dispatch(monkeypatch, store)
    store.put_node_status(SID, "sg1", "completed", plan_id=first)
    second = _dispatch(monkeypatch, store)
    assert first != second
    assert _row(store, f"PLAN#{first}")["plan_id"] == first
    assert _row(store, f"{first}#sg1")["status"] == "completed"
    assert _row(store, f"{second}#sg1")["status"] == "pending"


# ---- improve's harvested plan reader picks the session's newest plan ---------------------------

def _ddb(**attrs):
    return {k: {"S": v} for k, v in attrs.items()}


def test_harvested_plan_source_reads_the_newest_qualified_plan(tmp_path):
    import json

    from improve.observe.build_run import HarvestedPlanSource

    rows = [
        _ddb(session_id=SID, node_id="PLAN#pA", plan_id="pA", goal="old goal",
             updated_at="2026-10-08T01:00:00",
             nodes_json=json.dumps([{"id": "sg1", "depends_on": []}])),
        _ddb(session_id=SID, node_id="pA#sg1", status="completed"),
        _ddb(session_id=SID, node_id="PLAN#pB", plan_id="pB", goal="new goal",
             updated_at="2026-10-08T02:00:00",
             nodes_json=json.dumps([{"id": "sg1", "depends_on": []},
                                    {"id": "sg2", "depends_on": ["sg1"]}])),
        _ddb(session_id=SID, node_id="pB#sg1", status="completed"),
        _ddb(session_id=SID, node_id="pB#sg2", status="running"),
    ]
    path = tmp_path / "plan-state.json"
    path.write_text(json.dumps({"Items": rows}))
    plan = HarvestedPlanSource(str(path)).plan_for(SID)
    assert plan.goal == "new goal"
    assert sorted(n.id for n in plan.nodes) == ["PLAN", "sg1", "sg2"]
    assert next(n for n in plan.nodes if n.id == "sg2").depends_on == ["sg1"]
    assert plan.edges_available is True


def test_harvested_plan_source_still_reads_legacy_rows(tmp_path):
    import json

    from improve.observe.build_run import HarvestedPlanSource

    rows = [_ddb(session_id=SID, node_id="PLAN", plan_id="old", goal="g",
                 nodes_json=json.dumps([{"id": "t1", "depends_on": []}])),
            _ddb(session_id=SID, node_id="t1", status="completed")]
    path = tmp_path / "plan-state.json"
    path.write_text(json.dumps({"Items": rows}))
    plan = HarvestedPlanSource(str(path)).plan_for(SID)
    assert sorted(n.id for n in plan.nodes) == ["PLAN", "t1"] and plan.goal == "g"
