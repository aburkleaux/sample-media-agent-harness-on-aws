"""Phase 2: runtime map expansion + mechanical gather in the Plan Chainer.

A map node expands (once, when its producer completes) into one child task node per
collection item by rewriting the PLAN row's nodes_json; the gather fans them in without
re-concentrating output. Gated by DYNAMIC_FANOUT_ENABLED.
"""
from __future__ import annotations

import importlib.util
import pytest
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE", "SKILLS_S3_BUCKET"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_map", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_map"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()


class _FakeTable:
    def __init__(self):
        self.puts, self.updates = [], []

    def put_item(self, Item=None, **kw):
        self.puts.append(Item)

    def update_item(self, **kw):
        self.updates.append(kw)

    def get_item(self, Key=None, **kw):
        return {}


def _plan_record(nodes):
    return {"node_id": "PLAN", "plan_id": "P", "nodes_json": json.dumps(nodes),
            "actor_id": "actor1", "turn_id": "", "delegation_grant": "", "model_id": ""}


PROD = {"id": "prod", "kind": "task", "depends_on": [], "prompt": "diarize",
        "tools": [], "output_key": "spk", "output_type": "json"}
MAP = {"id": "m1", "kind": "map", "depends_on": ["prod"], "prompt": "", "tools": [],
       "output_key": "clips", "output_type": "json",
       "config": {"over": {"from": "prod", "path": "speakers[*].id"}, "item_var": "spk",
                  "template": {"prompt": "clip speaker {{spk}}", "tools": ["media___trim"],
                               "output_type": "s3_uri"}}}
GATHER = {"id": "g1", "kind": "gather", "depends_on": ["m1"], "prompt": "", "tools": [],
          "output_key": "all", "output_type": "json", "config": {"of": "m1"}}


def test_map_expands_and_dispatches_children(monkeypatch):
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "true")
    ptbl, jtbl = _FakeTable(), _FakeTable()
    monkeypatch.setattr(pc, "_plan_table", ptbl)
    monkeypatch.setattr(pc, "_job_table", jtbl)
    monkeypatch.setattr(pc, "_read_manifest_outputs",
                        lambda s, p: {"prod": {"value": {"speakers": [{"id": "S0"}, {"id": "S1"}]},
                                               "status": "completed"}})
    dispatched, gathered, completed = [], [], []
    monkeypatch.setattr(pc, "_dispatch_node", lambda s, p, a, nd, *r, **k: dispatched.append(nd["id"]))
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda s, p, a, nds, *r, **k: dispatched.extend(n["id"] for n in nds))
    monkeypatch.setattr(pc, "_run_gather", lambda s, p, nd: gathered.append(nd["id"]))
    monkeypatch.setattr(pc, "_mark_run_complete", lambda *a, **k: completed.append(True))

    pr = _plan_record([PROD, MAP, GATHER])
    items = [pr,
             {"node_id": "prod", "status": "completed", "plan_id": "P"},
             {"node_id": "m1", "status": "pending", "plan_id": "P"},
             {"node_id": "g1", "status": "pending", "plan_id": "P"}]
    pc._advance_plan("sess", pr, items)

    # Two children materialized and dispatched (S0, S1); gather NOT yet (children pending).
    assert sorted(dispatched) == ["m1__0", "m1__1"]
    assert gathered == [] and completed == []
    # Children persisted as pending rows + PLAN nodes_json rewritten.
    child_puts = [i for i in ptbl.puts if i.get("node_id", "").startswith("m1__")]
    assert {c["node_id"] for c in child_puts} == {"m1__0", "m1__1"}
    assert any("nodes_json" in (u.get("UpdateExpression") or "") for u in ptbl.updates)
    # The child prompt had the item substituted.
    assert any("clip speaker S0" in c.get("prompt", "") for c in child_puts)

    # Phase 3: child JOB rows carry dependsOn (the map) so PlanDagView draws edges.
    child_job_puts = [i for i in jtbl.puts if str(i.get("jobId", "")).startswith("P#m1__")]
    assert {j["jobId"] for j in child_job_puts} == {"P#m1__0", "P#m1__1"}
    assert all(j.get("dependsOn") == ["m1"] for j in child_job_puts)
    # Children depend on the MAP node (clean producer->map->children topology).
    assert all(c.get("depends_on") == json.dumps(["m1"]) for c in child_puts)
    # Map job row terminalized (SUCCEEDED) and gather rewired to depend on the children.
    map_upd = [u for u in jtbl.updates if u.get("Key", {}).get("jobId") == "P#m1"]
    assert any(u["ExpressionAttributeValues"].get(":s") == "SUCCEEDED" for u in map_upd)
    gather_upd = [u for u in jtbl.updates if u.get("Key", {}).get("jobId") == "P#g1"]
    assert any(u["ExpressionAttributeValues"].get(":d") == ["m1__0", "m1__1"] for u in gather_upd)


def test_map_children_inherit_the_template_endpoint_ids(monkeypatch):
    """A MAP of fal submits keeps its planned endpoint on every child, so each child's submit is
    held to it (task agent refuses any other endpoint)."""
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "true")
    monkeypatch.setattr(pc, "_plan_table", _FakeTable())
    monkeypatch.setattr(pc, "_job_table", _FakeTable())
    monkeypatch.setattr(pc, "_read_manifest_outputs",
                        lambda s, p: {"prod": {"value": {"speakers": [{"id": "S0"}, {"id": "S1"}]},
                                               "status": "completed"}})
    dispatched = []
    monkeypatch.setattr(pc, "_dispatch_node", lambda s, p, a, nd, *r, **k: dispatched.append(nd))
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda s, p, a, nds, *r, **k: dispatched.extend(nds))
    fal_map = json.loads(json.dumps(MAP))
    fal_map["config"]["template"]["endpoint_ids"] = ["minimax/h3-max/image-to-video"]
    pr = _plan_record([PROD, fal_map, GATHER])
    items = [pr, {"node_id": "prod", "status": "completed", "plan_id": "P"},
             {"node_id": "m1", "status": "pending", "plan_id": "P"},
             {"node_id": "g1", "status": "pending", "plan_id": "P"}]
    pc._advance_plan("sess", pr, items)
    assert [d.get("endpoint_ids") for d in dispatched] == [["minimax/h3-max/image-to-video"]] * 2


def test_gather_runs_when_children_complete(monkeypatch):
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "true")
    monkeypatch.setattr(pc, "_plan_table", _FakeTable())
    monkeypatch.setattr(pc, "_job_table", _FakeTable())
    monkeypatch.setattr(pc, "_read_manifest_outputs", lambda s, p: {})
    dispatched, gathered = [], []
    monkeypatch.setattr(pc, "_dispatch_node", lambda s, p, a, nd, *r, **k: dispatched.append(nd["id"]))
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda s, p, a, nds, *r, **k: dispatched.extend(n["id"] for n in nds))
    monkeypatch.setattr(pc, "_run_gather", lambda s, p, nd: gathered.append(nd["id"]))
    monkeypatch.setattr(pc, "_mark_run_complete", lambda *a, **k: None)

    # Post-expansion graph: map completed, children present + completed, gather pending.
    child0 = {"id": "m1__0", "kind": "task", "depends_on": ["prod"], "output_key": "c0", "output_type": "s3_uri", "prompt": "x", "tools": []}
    child1 = {"id": "m1__1", "kind": "task", "depends_on": ["prod"], "output_key": "c1", "output_type": "s3_uri", "prompt": "y", "tools": []}
    gather = dict(GATHER, depends_on=["m1__0", "m1__1"])
    nodes = [PROD, dict(MAP), gather, child0, child1]
    pr = _plan_record(nodes)
    items = [pr,
             {"node_id": "prod", "status": "completed", "plan_id": "P"},
             {"node_id": "m1", "status": "completed", "plan_id": "P"},
             {"node_id": "m1__0", "status": "completed", "plan_id": "P"},
             {"node_id": "m1__1", "status": "completed", "plan_id": "P"},
             {"node_id": "g1", "status": "pending", "plan_id": "P"}]
    pc._advance_plan("sess", pr, items)

    assert gathered == ["g1"]       # gather aggregated mechanically
    assert dispatched == []          # no task dispatched (children already done)


def test_render_item_template_scalar_and_field():
    assert pc._render_item_template("clip {{spk}}", "spk", "S0") == "clip S0"
    assert pc._render_item_template("id={{it.id}} n={{it.n}}", "it", {"id": "A", "n": 3}) == "id=A n=3"


def test_disabled_flag_skips_expansion(monkeypatch):
    monkeypatch.delenv("DYNAMIC_FANOUT_ENABLED", raising=False)
    monkeypatch.setattr(pc, "_plan_table", _FakeTable())
    monkeypatch.setattr(pc, "_job_table", _FakeTable())
    called = []
    monkeypatch.setattr(pc, "_expand_ready_map_nodes", lambda *a, **k: called.append(True))
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_run_gather", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_mark_run_complete", lambda *a, **k: None)
    pr = _plan_record([PROD, MAP, GATHER])
    items = [pr, {"node_id": "prod", "status": "completed", "plan_id": "P"},
             {"node_id": "m1", "status": "pending", "plan_id": "P"},
             {"node_id": "g1", "status": "pending", "plan_id": "P"}]
    pc._advance_plan("sess", pr, items)
    assert called == []   # expansion never attempted while the flag is off


def test_reproduces_db21d0c8_per_speaker_fanout(monkeypatch):
    """The original incident (session db21d0c8): "extract per speaker" — diarize discovers N
    speakers only at runtime, which a static DAG cannot enumerate (it ran 226 sequential
    steps). With fan-out, the map expands into exactly one node per discovered speaker."""
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "true")
    monkeypatch.setattr(pc, "_plan_table", _FakeTable())
    monkeypatch.setattr(pc, "_job_table", _FakeTable())
    monkeypatch.setattr(pc, "_read_manifest_outputs",
                        lambda s, p: {"prod": {"value": {"speakers": [{"id": "SPEAKER_00"},
                                                                      {"id": "SPEAKER_01"},
                                                                      {"id": "SPEAKER_02"}]},
                                               "status": "completed"}})
    dispatched = []
    monkeypatch.setattr(pc, "_dispatch_node", lambda s, p, a, nd, *r, **k: dispatched.append(nd["id"]))
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda s, p, a, nds, *r, **k: dispatched.extend(n["id"] for n in nds))
    monkeypatch.setattr(pc, "_run_gather", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_mark_run_complete", lambda *a, **k: None)
    pr = _plan_record([PROD, MAP, GATHER])
    items = [pr, {"node_id": "prod", "status": "completed", "plan_id": "P"},
             {"node_id": "m1", "status": "pending", "plan_id": "P"},
             {"node_id": "g1", "status": "pending", "plan_id": "P"}]
    pc._advance_plan("sess", pr, items)
    assert sorted(dispatched) == ["m1__0", "m1__1", "m1__2"]  # one node per discovered speaker


def test_empty_collection_skips_by_default(monkeypatch):
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "true")
    monkeypatch.setattr(pc, "_plan_table", _FakeTable())
    monkeypatch.setattr(pc, "_job_table", _FakeTable())
    monkeypatch.setattr(pc, "_read_manifest_outputs",
                        lambda s, p: {"prod": {"value": {"speakers": []}, "status": "completed"}})
    failed, dispatched, gathered = [], [], []
    monkeypatch.setattr(pc, "_mark_node_failed", lambda s, p, n, r: failed.append(n))
    monkeypatch.setattr(pc, "_dispatch_node", lambda s, p, a, nd, *r, **k: dispatched.append(nd["id"]))
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda s, p, a, nds, *r, **k: dispatched.extend(n["id"] for n in nds))
    monkeypatch.setattr(pc, "_run_gather", lambda s, p, nd: gathered.append(nd["id"]))
    monkeypatch.setattr(pc, "_mark_run_complete", lambda *a, **k: None)
    pr = _plan_record([PROD, MAP, GATHER])
    items = [pr, {"node_id": "prod", "status": "completed", "plan_id": "P"},
             {"node_id": "m1", "status": "pending", "plan_id": "P"},
             {"node_id": "g1", "status": "pending", "plan_id": "P"}]
    pc._advance_plan("sess", pr, items)
    assert failed == [] and dispatched == []   # empty -> skip (default), no children
    assert gathered == ["g1"]                  # gather still fans in (0 children)


def test_empty_collection_fails_when_on_empty_fail(monkeypatch):
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "true")
    monkeypatch.setattr(pc, "_plan_table", _FakeTable())
    monkeypatch.setattr(pc, "_job_table", _FakeTable())
    monkeypatch.setattr(pc, "_read_manifest_outputs",
                        lambda s, p: {"prod": {"value": {"speakers": []}, "status": "completed"}})
    failed = []
    monkeypatch.setattr(pc, "_mark_node_failed", lambda s, p, n, r: failed.append(n))
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_run_gather", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_mark_run_complete", lambda *a, **k: None)
    mapn = dict(MAP, config=dict(MAP["config"], on_empty="fail"))
    pr = _plan_record([PROD, mapn, GATHER])
    items = [pr, {"node_id": "prod", "status": "completed", "plan_id": "P"},
             {"node_id": "m1", "status": "pending", "plan_id": "P"},
             {"node_id": "g1", "status": "pending", "plan_id": "P"}]
    pc._advance_plan("sess", pr, items)
    assert failed == ["m1"]   # on_empty=fail -> map fails (dependents skip via Phase 1.5)


def test_max_fanout_exceeded_fails_map(monkeypatch):
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "true")
    monkeypatch.setattr(pc, "_plan_table", _FakeTable())
    monkeypatch.setattr(pc, "_job_table", _FakeTable())
    monkeypatch.setattr(pc, "_read_manifest_outputs",
                        lambda s, p: {"prod": {"value": {"speakers": [{"id": "a"}, {"id": "b"}]},
                                               "status": "completed"}})
    failed = []
    monkeypatch.setattr(pc, "_mark_node_failed", lambda s, p, n, r: failed.append(n))
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_run_gather", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_mark_run_complete", lambda *a, **k: None)
    mapn = dict(MAP, config=dict(MAP["config"], max_fanout=1))
    pr = _plan_record([PROD, mapn, GATHER])
    items = [pr, {"node_id": "prod", "status": "completed", "plan_id": "P"},
             {"node_id": "m1", "status": "pending", "plan_id": "P"},
             {"node_id": "g1", "status": "pending", "plan_id": "P"}]
    pc._advance_plan("sess", pr, items)
    assert failed == ["m1"]   # 2 items > max_fanout 1 -> map fails loudly, no silent blow-up


class _FakeS3:
    """Minimal S3 double: key -> (str|bytes) body; supports get_object + list_objects_v2."""
    def __init__(self, objs):
        self.objs = objs

    def get_object(self, Bucket=None, Key=None, **kw):
        data = self.objs[Key]
        if isinstance(data, str):
            data = data.encode("utf-8")

        class _B:
            def read(self_inner):
                return data
        return {"Body": _B()}

    def list_objects_v2(self, Bucket=None, Prefix=None, **kw):
        return {"Contents": [{"Key": k} for k in self.objs if k.startswith(Prefix)]}


def test_resolve_map_items_uses_manifest_value_first():
    items = pc._resolve_map_items("sess", "P", "prod", "speakers[*].id", node_states={},
                                  outputs={"prod": {"value": {"speakers": [{"id": "X"}]}}})
    assert items == ["X"]


def test_resolve_map_items_reads_producer_result_fresh_not_from_snapshot(monkeypatch):
    """Value not in the manifest -> read the producer row FRESH (get_item -> result_s3_key),
    NOT the possibly-stale node_states snapshot. This is the e8e4f52b fix: the result existed
    but the same-invocation snapshot missed it."""
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "skills")
    monkeypatch.setattr(pc, "_s3", _FakeS3({"res/out": json.dumps({"items": ["S0", "S1"]})}))

    class _T:  # fresh row HAS result_s3_key even though the node_states snapshot below does NOT
        def get_item(self, Key=None, **kw):
            return {"Item": {"session_id": Key["session_id"], "node_id": Key["node_id"],
                             "result_s3_key": "res/out"}}
    monkeypatch.setattr(pc, "_plan_table", _T())
    items = pc._resolve_map_items("sess", "P", "collect", "items", node_states={"collect": {}}, outputs={})
    assert items == ["S0", "S1"]


def test_resolve_map_items_terminal_when_producer_completed_without_result(monkeypatch):
    """Completed producer + strongly-consistent read + no result_s3_key => genuinely no output =>
    TERMINAL, never a hang. Retrying cannot conjure a result."""
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "skills")
    monkeypatch.setattr(pc, "_plan_table",
                        type("T", (), {"get_item": lambda self, Key=None, **k: {}})())
    with pytest.raises(pc.fanout_extract.FanoutExtractError):
        pc._resolve_map_items("sess", "P", "prod", "items", node_states={}, outputs={})


def test_resolve_map_items_not_ready_on_transient_s3_read_failure(monkeypatch):
    """result_s3_key IS present (object exists) but the read momentarily fails -> _MapNotReady
    (transient/retryable), NOT terminal."""
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "skills")
    monkeypatch.setattr(pc, "_plan_table",
                        type("T", (), {"get_item": lambda self, Key=None, **k:
                                       {"Item": {"result_s3_key": "res/x"}}})())

    class _Boom:
        def get_object(self, **kw):
            raise RuntimeError("throttled")
    monkeypatch.setattr(pc, "_s3", _Boom())
    with pytest.raises(pc._MapNotReady):
        pc._resolve_map_items("sess", "P", "prod", "items", node_states={}, outputs={})


def test_resolve_map_items_terminal_when_result_present_but_no_list():
    """Result present but no list at path -> terminal FanoutExtractError naming the collector fix."""
    with pytest.raises(pc.fanout_extract.FanoutExtractError) as ei:
        pc._resolve_map_items("sess", "P", "prod", "items", node_states={},
                              outputs={"prod": {"value": {"not_a_list": 1}}})
    assert "collector" in str(ei.value).lower()


def test_failed_map_is_not_retried(monkeypatch):
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "true")
    monkeypatch.setattr(pc, "MAX_NODE_RETRIES", 1)
    monkeypatch.setattr(pc, "_plan_table", _FakeTable())
    monkeypatch.setattr(pc, "_job_table", _FakeTable())
    retried, skipped, completed = [], [], []
    monkeypatch.setattr(pc, "_retry_failed_node", lambda s, p, a, nd, *r, **k: retried.append(nd["id"]))
    monkeypatch.setattr(pc, "_mark_node_skipped", lambda s, p, n, r: skipped.append(n))
    monkeypatch.setattr(pc, "_mark_run_complete", lambda *a, **k: completed.append(True))
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda *a, **k: None)
    # map already failed (expansion error recorded); gather depends on it
    nodes = [PROD, dict(MAP), GATHER]
    pr = _plan_record(nodes)
    items = [pr, {"node_id": "prod", "status": "completed", "plan_id": "P"},
             {"node_id": "m1", "status": "failed", "error": "map expansion failed: ...", "retries": "0", "plan_id": "P"},
             {"node_id": "g1", "status": "pending", "plan_id": "P"}]
    pc._advance_plan("sess", pr, items)
    assert retried == []              # a failed MAP is NOT re-dispatched as a task
    assert skipped == ["g1"]          # its gather is skipped
    assert completed == [True]        # plan settles


def test_retry_resets_job_row_to_running(monkeypatch):
    monkeypatch.setattr(pc, "MAX_NODE_RETRIES", 1)
    pt, jt = _FakeTable(), _FakeTable()
    monkeypatch.setattr(pc, "_plan_table", pt)
    monkeypatch.setattr(pc, "_job_table", jt)
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda *a, **k: None)
    row = {"error": "output truncated", "retries": 0}
    pc._retry_failed_node("sess", "P", "actor", {"id": "sg3"}, [{"id": "sg3"}], {"sg3": row}, row)
    # the node's job row is reset to RUNNING (so a successful retry can set_terminal SUCCEEDED)
    reset = [u for u in jt.updates if u.get("Key", {}).get("jobId") == "P#sg3"]
    assert any(u["ExpressionAttributeValues"].get(":r") == "RUNNING" for u in reset)
    assert any("REMOVE terminalAt" in (u.get("UpdateExpression") or "") for u in reset)


# ---- Full-lifecycle integration test: producer -> map expand -> children -> gather -> settle ----

class _StatefulPlanTable:
    """In-memory plan-state table (plain dicts, like the boto3 resource API returns). Supports
    the put_item/update_item/query the chainer actually issues, so we can drive multiple
    _advance_plan passes and watch the graph grow + settle."""
    def __init__(self, rows):
        self.rows = {(r["session_id"], r["node_id"]): dict(r) for r in rows}

    def put_item(self, Item=None, **kw):
        self.rows[(Item["session_id"], Item["node_id"])] = dict(Item)

    def get_item(self, Key=None, **kw):
        row = self.rows.get((Key["session_id"], Key["node_id"]))
        return {"Item": dict(row)} if row is not None else {}

    def update_item(self, Key=None, UpdateExpression="", ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ConditionExpression=None, **kw):
        row = self.rows.setdefault((Key["session_id"], Key["node_id"]), dict(Key))
        names, vals, expr = ExpressionAttributeNames or {}, ExpressionAttributeValues or {}, UpdateExpression or ""
        if "SET" in expr:
            set_part = expr.split("SET", 1)[1].split("REMOVE")[0]
            for assign in set_part.split(","):
                if "=" in assign:
                    f, v = [x.strip() for x in assign.split("=", 1)]
                    row[names.get(f, f)] = vals.get(v)
        if "REMOVE" in expr:
            for f in expr.split("REMOVE", 1)[1].split(","):
                row.pop(names.get(f.strip(), f.strip()), None)

    def query(self, **kw):
        return {"Items": list(self.rows.values())}

    def rows_for(self, sid):
        return [dict(v) for v in self.rows.values() if v.get("session_id") == sid]


class _FakeS3RW(_FakeS3):
    def put_object(self, Bucket=None, Key=None, Body=None, **kw):
        self.objs[Key] = Body


def test_full_lifecycle_diarize_map_gather_settles(monkeypatch):
    """End-to-end (in-process): a diarize producer whose output is an S3 pointer expands into
    one child per discovered speaker, children complete, gather fans in, plan settles SUCCEEDED."""
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "true")
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "skills")
    sid = "sess-e2e"

    diarize = {"id": "diarize", "kind": "task", "depends_on": [], "prompt": "d", "tools": [],
               "output_key": "spk", "output_type": "json"}
    # Collector node: reads the diarize output and materialises the speaker list as a canonical
    # inline {"items": [...]}. Deriving the list is the collector's job, not the map runtime's.
    collect = {"id": "collect", "kind": "task", "depends_on": ["diarize"], "prompt": "extract speakers",
               "tools": [], "output_key": "speakers", "output_type": "json"}
    mp = {"id": "m1", "kind": "map", "depends_on": ["collect"], "prompt": "", "tools": [],
          "output_key": "clips", "output_type": "json",
          "config": {"over": {"from": "collect", "path": "items"}, "item_var": "spk",
                     "template": {"prompt": "clip {{spk}}", "tools": ["m___trim"], "output_type": "s3_uri"}}}
    gath = {"id": "g1", "kind": "gather", "depends_on": ["m1"], "prompt": "", "tools": [],
            "output_key": "all", "output_type": "json", "config": {"of": "m1"}}

    tbl = _StatefulPlanTable([
        {"session_id": sid, "node_id": "PLAN", "plan_id": "P", "actor_id": "a", "turn_id": "",
         "delegation_grant": "", "model_id": "", "nodes_json": json.dumps([diarize, collect, mp, gath]),
         "status": "pending"},
        {"session_id": sid, "node_id": "diarize", "plan_id": "P", "status": "completed"},
        {"session_id": sid, "node_id": "collect", "plan_id": "P", "status": "completed",
         "result_s3_key": "res/collect"},
        {"session_id": sid, "node_id": "m1", "plan_id": "P", "status": "pending"},
        {"session_id": sid, "node_id": "g1", "plan_id": "P", "status": "pending"},
    ])
    # collector output = a clean inline {"items": [...]} at result_s3_key (no pointer, no crawl)
    s3 = _FakeS3RW({"res/collect": json.dumps({"items": ["SPEAKER_00", "SPEAKER_01"]})})
    monkeypatch.setattr(pc, "_plan_table", tbl)
    monkeypatch.setattr(pc, "_job_table", _FakeTable())
    monkeypatch.setattr(pc, "_s3", s3)
    monkeypatch.setattr(pc, "_read_manifest_outputs", lambda s, p: {})   # force result_s3_key resolution
    dispatched = []
    monkeypatch.setattr(pc, "_dispatch_node", lambda s, p, a, nd, *r, **k: dispatched.append(nd["id"]))
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda s, p, a, nds, *r, **k: dispatched.extend(n["id"] for n in nds))
    settled = {}
    monkeypatch.setattr(pc, "_mark_run_complete",
                        lambda s, p, nj, ns, **_: settled.update(status="SUCCEEDED" if all(
                            ns.get(n["id"], "").lower() in ("completed", "succeeded") for n in nj) else "FAILED"))

    # Pass 1: diarize done -> map expands into 2 children (dispatched), gather waits
    pc._advance_plan(sid, tbl.rows[(sid, "PLAN")], tbl.rows_for(sid))
    assert sorted(dispatched) == ["m1__0", "m1__1"], f"expected 2 per-speaker children, got {dispatched}"
    assert (tbl.rows[(sid, "m1")]["status"]).lower() == "completed"   # map control node done
    assert not settled

    # Simulate the two child task nodes completing
    for c in ("m1__0", "m1__1"):
        tbl.rows[(sid, c)]["status"] = "completed"

    # Pass 2: children complete -> gather runs (mechanical) -> completes
    pc._advance_plan(sid, tbl.rows[(sid, "PLAN")], tbl.rows_for(sid))
    assert (tbl.rows[(sid, "g1")]["status"]).lower() == "completed", "gather should fan in and complete"

    # Pass 3: everything terminal -> plan settles SUCCEEDED
    pc._advance_plan(sid, tbl.rows[(sid, "PLAN")], tbl.rows_for(sid))
    assert settled.get("status") == "SUCCEEDED", f"plan should settle SUCCEEDED, got {settled}"


# ---- Fix 1b: nodes_json S3 offload (keep the PLAN item under DynamoDB's 400KB cap) ----

def test_load_nodes_json_prefers_inline():
    assert pc._load_nodes_json({"plan_id": "P", "nodes_json": json.dumps([{"id": "a"}])}) == [{"id": "a"}]


def test_load_nodes_json_reads_s3_pointer_when_inline_empty(monkeypatch):
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "skills")
    monkeypatch.setattr(pc, "_s3", _FakeS3({"plans/S/P/nodes.json": json.dumps([{"id": "b"}, {"id": "c"}])}))
    rec = {"plan_id": "P", "nodes_json": "", "nodes_s3_key": "plans/S/P/nodes.json"}
    assert pc._load_nodes_json(rec) == [{"id": "b"}, {"id": "c"}]


def test_store_nodes_json_inline_when_small(monkeypatch):
    tbl = _StatefulPlanTable([{"session_id": "S", "node_id": "PLAN", "plan_id": "P", "status": "pending"}])
    monkeypatch.setattr(pc, "_plan_table", tbl)
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "skills")
    pc._store_nodes_json_on_plan("S", "P", "PLAN", [{"id": "a"}])
    row = tbl.rows[("S", "PLAN")]
    assert json.loads(row["nodes_json"]) == [{"id": "a"}]
    assert "nodes_s3_key" not in row                      # small stays inline; pointer cleared


def test_store_nodes_json_offloads_when_large(monkeypatch):
    tbl = _StatefulPlanTable([{"session_id": "S", "node_id": "PLAN", "plan_id": "P", "status": "pending"}])
    s3 = _FakeS3RW({})
    monkeypatch.setattr(pc, "_plan_table", tbl)
    monkeypatch.setattr(pc, "_s3", s3)
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "skills")
    monkeypatch.setattr(pc, "NODES_JSON_INLINE_MAX", 100)  # force the offload path
    big = [{"id": f"n{i}", "prompt": "x" * 20} for i in range(20)]
    pc._store_nodes_json_on_plan("S", "P", "PLAN", big)
    row = tbl.rows[("S", "PLAN")]
    assert row["nodes_s3_key"] == "plans/S/P/nodes.json"
    assert row["nodes_json"] == ""                         # inline cleared so the item stays small
    assert json.loads(s3.objs["plans/S/P/nodes.json"]) == big
    assert pc._load_nodes_json(row) == big                 # round-trips back through the reader


def test_resolve_map_items_recovers_from_prose_wrapped_wrong_value(monkeypatch):
    """466addc8 regression: the manifest snapshot value is the WRONG object ({"uris":...}) and
    the raw producer result is prose-prefixed ('I'll start...{"batches":[...]}') so a strict parse
    misses the list. _resolve_map_items must fall back to scanning the raw result and recover the
    real {"batches":[...]} rather than failing the whole index phase."""
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "skills")
    raw = 'I\'ll start by presigning the manifest.{"batches": [{"batch_id": 0}, {"batch_id": 1}]}'
    monkeypatch.setattr(pc, "_s3", _FakeS3({"res/out": raw}))
    monkeypatch.setattr(pc, "_plan_table",
                        type("T", (), {"get_item": lambda self, Key=None, **k:
                                       {"Item": {"result_s3_key": "res/out"}}})())
    items = pc._resolve_map_items("sess", "P", "prod", "batches", node_states={},
                                  outputs={"prod": {"value": {"uris": ["a", "b"]}}})
    assert items == [{"batch_id": 0}, {"batch_id": 1}]
