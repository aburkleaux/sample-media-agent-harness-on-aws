"""D-08: sliding-window TTL on plan-state rows.

A plan-state row's ``ttl`` (a DynamoDB *absolute* expiry timestamp) used to be stamped ONCE at
row creation as ``now + 24h`` and never moved. A plan still running at T+24h therefore had its
own DAG-state rows swept out from under it mid-flight — silent self-erasure — and the DAG shape
(the highest-fidelity evidence in the system) was its shortest-lived data. The fix re-stamps
``ttl = now + 24h`` on every in-flight write that represents genuine progress, so an ACTIVE plan's
rows persist while a STALLED one still ages out at 24h-from-last-progress.

Two hard constraints these tests pin down:
  1. Restamp on WRITES, never on reads/polls — a stalled plan must still die at 24h-from-last
     genuine progress (that TTL sweep is D-01's only stop for it). See
     ``test_no_progress_poll_does_not_restamp_any_ttl``.
  2. The PLAN row (which holds ``nodes_json`` / the ``nodes_s3_key`` pointer — the DAG SHAPE) must
     slide on NODE progress too, not only on expansion/settle — otherwise a long node-by-node plan
     with no fan-out would slide its node rows while its PLAN row aged out at 24h-from-creation.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WINDOW = 86400


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE",
                 "SKILLS_S3_BUCKET", "TASK_AGENT_RUNTIME_ARN"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_ttl", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_ttl"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()


def _assert_fresh(ttl):
    """`ttl` is an absolute epoch ~ now + 24h (a few seconds of test slack for the fake clock)."""
    assert ttl is not None, "no ttl written"
    now = int(time.time())
    assert now + WINDOW - 5 <= int(ttl) <= now + WINDOW + 5, (
        f"ttl {ttl!r} is not ~ now+{WINDOW} (now={now})")


class _CCF(Exception):
    """Stands in for botocore's ConditionalCheckFailedException."""


def _meta():
    return type("M", (), {"client": type("C", (), {"exceptions": type(
        "E", (), {"ConditionalCheckFailedException": _CCF})})})


class _PlanTable:
    """In-memory plan-state double that records every write AND supports get_item, so the PLAN-row
    slide (`_slide_plan_ttl`, which point-reads to find the PLAN row's real sort key) is exercised.

    `ccf_keys` = (session_id, node_id) tuples whose update_item raises CCF (reproduces a node that
    a prior pass already advanced past its condition guard).
    """

    def __init__(self, rows=None, ccf_keys=None):
        self.rows = {(r["session_id"], r["node_id"]): dict(r) for r in (rows or [])}
        self.updates = []
        self.puts = []
        self.ccf_keys = set(ccf_keys or [])
        self.meta = _meta()

    def get_item(self, Key=None, **kw):
        row = self.rows.get((Key["session_id"], Key["node_id"]))
        return {"Item": dict(row)} if row is not None else {}

    def put_item(self, Item=None, **kw):
        self.puts.append(dict(Item))
        self.rows[(Item["session_id"], Item["node_id"])] = dict(Item)

    def update_item(self, Key=None, UpdateExpression="", ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ConditionExpression=None, **kw):
        vals = ExpressionAttributeValues or {}
        self.updates.append({"Key": dict(Key or {}), "expr": UpdateExpression or "",
                             "names": ExpressionAttributeNames or {}, "vals": dict(vals)})
        if (Key.get("session_id"), Key.get("node_id")) in self.ccf_keys:
            raise _CCF("condition not met")
        row = self.rows.setdefault((Key["session_id"], Key["node_id"]), dict(Key))
        if ":ttl" in vals:
            row["ttl"] = vals[":ttl"]
        return {}

    def node_update_ttl(self, node_id):
        """ttl on the last UPDATE whose Key node_id == node_id (the node row itself)."""
        for u in reversed(self.updates):
            if u["Key"].get("node_id") == node_id:
                return u["vals"].get(":ttl")
        return None

    def plan_slide_ttl(self):
        """ttl on the last UPDATE that targeted a PLAN row (legacy 'PLAN' or qualified 'PLAN#...')."""
        for u in reversed(self.updates):
            nid = str(u["Key"].get("node_id") or "")
            if nid == "PLAN" or nid.startswith("PLAN#"):
                return u["vals"].get(":ttl")
        return None


class _JobTable:
    """Minimal job-registry double: swallows writes, answers get_item from a seeded item."""

    def __init__(self, item=None):
        self._item = dict(item) if item else {}
        self.meta = _meta()

    def get_item(self, Key=None, **kw):
        return {"Item": dict(self._item)} if self._item else {}

    def update_item(self, **kw):
        return {}


class _FakeS3:
    def put_object(self, **kw):
        return {}

    def get_object(self, **kw):
        raise RuntimeError("no object")  # handler falls back to a fresh dict


def _plan_row(session="sess", plan="P"):
    # Seeded as a LEGACY bare-"PLAN" row so the slide's qualified-then-legacy fallback is exercised.
    return {"session_id": session, "node_id": "PLAN", "plan_id": plan, "status": "running",
            "nodes_json": json.dumps([{"id": "n1"}]), "ttl": 111}


# --------------------------------------------------------------------------------------------
# The 8 UPDATE paths each stamp a fresh ttl, and each NODE transition also slides the PLAN row.
# --------------------------------------------------------------------------------------------

def test_mark_node_failed_stamps_node_ttl_and_slides_plan(monkeypatch):
    tbl = _PlanTable(rows=[_plan_row()])
    monkeypatch.setattr(pc, "_plan_table", tbl)
    monkeypatch.setattr(pc, "_job_table", _JobTable())
    pc._mark_node_failed("sess", "P", "n1", "boom")
    _assert_fresh(tbl.node_update_ttl("n1"))       # the node row got a fresh ttl
    _assert_fresh(tbl.plan_slide_ttl())            # constraint 2: PLAN row slid on node progress


def test_mark_node_skipped_stamps_node_ttl_and_slides_plan(monkeypatch):
    tbl = _PlanTable(rows=[_plan_row()])
    monkeypatch.setattr(pc, "_plan_table", tbl)
    monkeypatch.setattr(pc, "_job_table", _JobTable())
    pc._mark_node_skipped("sess", "P", "n1", "upstream dependency failed")
    _assert_fresh(tbl.node_update_ttl("n1"))
    _assert_fresh(tbl.plan_slide_ttl())


def test_retry_failed_node_stamps_node_ttl_and_slides_plan(monkeypatch):
    tbl = _PlanTable(rows=[_plan_row()])
    monkeypatch.setattr(pc, "_plan_table", tbl)
    monkeypatch.setattr(pc, "_job_table", _JobTable())
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: None)
    row = {"error": "duration must be >= 10, got 5", "retries": 0}
    pc._retry_failed_node("sess", "P", "actor", {"id": "n1"}, [{"id": "n1"}], {"n1": row}, row)
    node = next(u for u in tbl.updates if u["Key"]["node_id"] == "n1")
    assert node["vals"][":r"] == "running" and node["vals"][":n"] == 1   # transition preserved
    _assert_fresh(node["vals"].get(":ttl"))
    _assert_fresh(tbl.plan_slide_ttl())


def test_mark_run_complete_stamps_plan_row_ttl(monkeypatch):
    tbl = _PlanTable(rows=[_plan_row()])
    monkeypatch.setattr(pc, "_plan_table", tbl)
    monkeypatch.setattr(pc, "_job_table", _JobTable())
    monkeypatch.setattr(pc, "_publish_plan_result", lambda *a, **k: "")
    pc._mark_run_complete("sess", "P", [{"id": "n1"}], {"n1": "completed"})
    _assert_fresh(tbl.plan_slide_ttl())            # the settle write on the PLAN row carries ttl


def test_store_nodes_json_stamps_plan_row_ttl_inline(monkeypatch):
    tbl = _PlanTable(rows=[_plan_row()])
    monkeypatch.setattr(pc, "_plan_table", tbl)
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "b")
    pc._store_nodes_json_on_plan("sess", "P", "PLAN", [{"id": "a"}])
    upd = tbl.updates[-1]
    assert "nodes_json" in upd["expr"] and upd["Key"]["node_id"] == "PLAN"
    _assert_fresh(upd["vals"].get(":ttl"))


def test_store_nodes_json_stamps_plan_row_ttl_on_offload(monkeypatch):
    tbl = _PlanTable(rows=[_plan_row()])
    monkeypatch.setattr(pc, "_plan_table", tbl)
    monkeypatch.setattr(pc, "_s3", type("S", (), {"put_object": lambda self, **k: {}})())
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "b")
    monkeypatch.setattr(pc, "NODES_JSON_INLINE_MAX", 10)      # force the S3-offload branch
    big = [{"id": f"n{i}", "prompt": "x" * 20} for i in range(20)]
    pc._store_nodes_json_on_plan("sess", "P", "PLAN", big)
    upd = tbl.updates[-1]
    assert "nodes_s3_key" in upd["expr"]
    _assert_fresh(upd["vals"].get(":ttl"))


def test_dispatch_sfn_output_stamps_node_ttl_and_slides_plan(monkeypatch):
    tbl = _PlanTable(rows=[_plan_row()])
    monkeypatch.setattr(pc, "_plan_table", tbl)
    monkeypatch.setattr(pc, "_job_table", _JobTable(item={"status": "RUNNING"}))
    monkeypatch.setattr(pc, "_s3", _FakeS3())
    monkeypatch.setattr(pc, "_sfn_client_for_arn",
                        lambda *a, **k: type("F", (), {"describe_execution":
                                                       lambda self, executionArn=None:
                                                       {"status": "SUCCEEDED", "output": '{"ok": true}'}})())
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "b")
    pc._dispatch_sfn_output_as_result("sess", "P", "n1", "arn:job", "2026-08-24T00:00:00+00:00")
    _assert_fresh(tbl.node_update_ttl("n1"))
    _assert_fresh(tbl.plan_slide_ttl())


def test_dispatch_sfn_output_does_not_slide_when_node_already_advanced(monkeypatch):
    # Constraint 1 at the transition level: a ConditionalCheckFailed means a prior pass already
    # advanced this node — re-mirroring the projection is NOT new progress, so no PLAN slide.
    tbl = _PlanTable(rows=[_plan_row()], ccf_keys=[("sess", "n1")])
    monkeypatch.setattr(pc, "_plan_table", tbl)
    monkeypatch.setattr(pc, "_job_table", _JobTable(item={"status": "RUNNING"}))
    monkeypatch.setattr(pc, "_s3", _FakeS3())
    monkeypatch.setattr(pc, "_sfn_client_for_arn",
                        lambda *a, **k: type("F", (), {"describe_execution":
                                                       lambda self, executionArn=None:
                                                       {"status": "SUCCEEDED", "output": '{"ok": true}'}})())
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "b")
    pc._dispatch_sfn_output_as_result("sess", "P", "n1", "arn:job", "2026-08-24T00:00:00+00:00")
    # No PLAN-row write happened at all (the mirror still runs, but on the job table, not here).
    assert tbl.plan_slide_ttl() is None, "PLAN ttl was slid on a no-progress re-mirror"


def test_dispatch_results_fetch_stamps_node_ttl_and_slides_plan(monkeypatch):
    tbl = _PlanTable(rows=[_plan_row()])
    monkeypatch.setattr(pc, "_plan_table", tbl)
    monkeypatch.setattr(pc, "_job_table",
                        _JobTable(item={"resultsTool": "foo_results", "resultsGatewayId": "gw1"}))
    monkeypatch.setattr(pc, "_gateway_table", object())         # must be truthy
    monkeypatch.setattr(pc, "invoke_mcp_tool", lambda **k: {"ok": True})
    monkeypatch.setattr(pc, "_s3", _FakeS3())
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "b")
    pc._dispatch_results_fetch("sess", "P", "actor", "n1", {"job_id": "job-1"})
    _assert_fresh(tbl.node_update_ttl("n1"))
    _assert_fresh(tbl.plan_slide_ttl())


# --------------------------------------------------------------------------------------------
# Constraint 1 (the D-01 guard): a no-progress poll must NOT rewrite any ttl.
# --------------------------------------------------------------------------------------------

def test_no_progress_poll_does_not_restamp_any_ttl(monkeypatch):
    """A pass that makes no state transition (a node is running, nothing else is ready or
    terminal) must issue ZERO plan-state writes — so a STALLED plan's ttl is untouched and it
    still ages out at 24h-from-last-progress. This is the test that proves the sliding window
    does not weaken D-01."""
    tbl = _PlanTable()
    monkeypatch.setattr(pc, "_plan_table", tbl)
    nodes = [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": ["a"]}]
    plan_record = {"node_id": "PLAN", "plan_id": "P", "nodes_json": json.dumps(nodes),
                   "actor_id": "", "turn_id": "", "delegation_grant": "", "model_id": ""}
    items = [plan_record,
             {"node_id": "a", "status": "running", "plan_id": "P"},   # in-flight, no transition
             {"node_id": "b", "status": "pending", "plan_id": "P"}]
    pc._advance_plan("sess", plan_record, items)
    assert tbl.updates == [], f"a no-progress poll rewrote plan rows: {tbl.updates}"
    assert tbl.puts == [], f"a no-progress poll created plan rows: {tbl.puts}"


# --------------------------------------------------------------------------------------------
# Regression guard: the CREATE paths still stamp ttl.
# --------------------------------------------------------------------------------------------

def test_persist_map_children_create_still_stamps_ttl(monkeypatch):
    tbl = _PlanTable(rows=[_plan_row()])
    monkeypatch.setattr(pc, "_plan_table", tbl)
    monkeypatch.setattr(pc, "_job_table", _JobTable())
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "")            # keep _store_nodes_json inline-only
    child = {"id": "m1__0", "depends_on": ["m1"], "tools": ["t"], "prompt": "p", "output_type": "json"}
    pc._persist_map_children("sess", "P", "actor", [child], [{"id": "m1"}], "PLAN", "m1")
    assert tbl.puts, "no child rows were created"
    for item in tbl.puts:
        _assert_fresh(item.get("ttl"))
