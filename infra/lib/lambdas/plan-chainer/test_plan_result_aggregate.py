"""A settled plan must publish ONE resolved result artifact, and notify with it.

Pins the behaviour the aggregation exists for:

  * the artifact is written for FAILED plans too. Previously a failed plan produced
    nothing readable, which is how a dependency failure stayed invisible -- in one
    run a node held ``{"request_id": ...}`` the orchestrator could not resolve, and
    the only record of it was prose in a chat turn.
  * the artifact key is stamped on the job row, so the changefeed notification that
    write triggers carries a pointer to one fetchable result.
  * the artifact is written BEFORE the status write, so the notification can never
    point at a key that does not exist.
  * aggregation failure must not strand the plan non-terminal.
  * queue handles resolve from the job row the POLLER already resolved, not by
    re-invoking a gateway and guessing an endpoint id.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("PLAN_STATE_TABLE", "plans")
os.environ.setdefault("JOB_REGISTRY_TABLE", "jobs")
os.environ.setdefault("SKILLS_S3_BUCKET", "skills-bucket")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

_HERE = Path(__file__).resolve().parent
# The layer is on sys.path at /opt/python in Lambda; make it importable here.
sys.path.insert(0, str(_HERE.parent / "_layers" / "result-resolution" / "python"))

# Load by path under a unique name: this bundle and the polling worker both define
# `handler` and `gateway_invoker`, and a sys.path insert lets whichever imports
# first shadow the other.
_spec = importlib.util.spec_from_file_location("pc_handler_settle", _HERE / "handler.py")
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


class _S3:
    def __init__(self, manifest=None, objects=None):
        self.manifest = manifest
        self.objects = objects or {}
        self.puts = []

    def get_object(self, Bucket, Key, **kw):
        if Key.endswith("manifest.json"):
            if self.manifest is None:
                raise RuntimeError("NoSuchKey")
            body = json.dumps(self.manifest).encode()
        elif Key in self.objects:
            body = json.dumps(self.objects[Key]).encode()
        else:
            raise RuntimeError("NoSuchKey")
        return {"Body": _Body(body)}

    def head_object(self, Bucket, Key, **kw):
        if Key in self.objects:
            return {"ContentLength": len(json.dumps(self.objects[Key]))}
        raise RuntimeError("NoSuchKey")

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts.append({"key": Key, "body": Body})
        return {}


class _Body:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw


class _JobTable:
    def __init__(self, rows=None, fail_update=False):
        self.rows = rows or {}
        self.updates = []
        self.fail_update = fail_update

    def get_item(self, Key):
        row = self.rows.get(Key["jobId"])
        return {"Item": row} if row else {}

    def update_item(self, **kw):
        if self.fail_update:
            raise RuntimeError("throttled")
        self.updates.append(kw)
        return {}


def _wire(monkeypatch, s3, jobs):
    monkeypatch.setattr(pc, "_s3", s3, raising=False)
    monkeypatch.setattr(pc, "_job_table", jobs, raising=False)
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "skills-bucket", raising=False)


def _nodes(*ids):
    return [{"id": i} for i in ids]


def test_a_succeeded_plan_writes_the_result_artifact_and_stamps_the_key(monkeypatch):
    s3 = _S3(manifest={
        "display_name": "Analysis",
        "goal": "analyse",
        "outputs": {
            "t1": {"status": "completed", "value": "s3://b/proxy/transcoded.mp4"},
            "t2": {"status": "completed", "value": {"s3uri": "s3://b/shots.json"}},
        },
    }, objects={"shots.json": {"n_shots": 94, "n_recurrence_groups": 6}})
    jobs = _JobTable()
    _wire(monkeypatch, s3, jobs)

    pc._mark_run_complete("sess", "plan-1", _nodes("t1", "t2"),
                          {"t1": "completed", "t2": "completed"})

    assert len(s3.puts) == 1
    assert s3.puts[0]["key"] == "plans/sess/plan-1/result.json"
    agg = json.loads(s3.puts[0]["body"].decode())
    assert agg["schema"] == "plan-result/v1"
    assert agg["status"] == "SUCCEEDED"
    kinds = {n["node_id"]: n["kind"] for n in agg["nodes"]}
    assert kinds == {"t1": "media", "t2": "analysis"}
    findings = {n["node_id"]: n["findings"] for n in agg["nodes"]}
    assert findings["t2"]["n_shots"] == 94

    # The key must reach the row, because that write is what notifies.
    assert any("resultKey" in u.get("UpdateExpression", "") for u in jobs.updates)
    assert jobs.updates[0]["ExpressionAttributeValues"][":rk"] == "plans/sess/plan-1/result.json"


def test_a_failed_plan_still_publishes_its_partial_results(monkeypatch):
    s3 = _S3(manifest={
        "outputs": {
            "ok": {"status": "completed", "value": "s3://b/a.wav"},
            "broken": {"status": "failed", "error": "403 HeadObject Forbidden"},
        },
    })
    jobs = _JobTable()
    _wire(monkeypatch, s3, jobs)

    pc._mark_run_complete("sess", "plan-2", _nodes("ok", "broken"),
                          {"ok": "completed", "broken": "failed"})

    agg = json.loads(s3.puts[0]["body"].decode())
    assert agg["status"] == "FAILED"
    by_id = {n["node_id"]: n for n in agg["nodes"]}
    assert by_id["ok"]["kind"] == "media"
    assert by_id["broken"]["kind"] == "error"
    assert "403" in by_id["broken"]["error"]
    assert jobs.updates[0]["ExpressionAttributeValues"][":s"] == "FAILED"


def test_the_artifact_is_written_before_the_status_write(monkeypatch):
    """Order matters: the status write triggers the notification."""
    order = []
    s3 = _S3(manifest={"outputs": {"t1": {"status": "completed", "value": "s3://b/a.mp4"}}})
    jobs = _JobTable()

    real_put = s3.put_object
    def tracking_put(**kw):
        order.append("put")
        return real_put(**kw)
    s3.put_object = lambda Bucket, Key, Body, **kw: tracking_put(Bucket=Bucket, Key=Key, Body=Body, **kw)

    real_update = jobs.update_item
    def tracking_update(**kw):
        order.append("status")
        return real_update(**kw)
    jobs.update_item = tracking_update

    _wire(monkeypatch, s3, jobs)
    pc._mark_run_complete("sess", "plan-3", _nodes("t1"), {"t1": "completed"})
    assert order == ["put", "status"]


def test_a_queue_handle_resolves_from_the_row_the_poller_already_resolved(monkeypatch):
    # Not a gateway re-invocation: the poller recorded artifactUrls under the
    # queue's own request id, so reading the row cannot 404/405 on an endpoint id.
    s3 = _S3(manifest={
        "outputs": {
            "t1_video": {
                "status": "completed",
                "value": {"request_id": "req-1", "model": "bytedance/seedance-2.0/fast/image-to-video"},
            },
        },
    })
    jobs = _JobTable(rows={
        "req-1": {"status": "SUCCEEDED", "artifactUrls": ["https://cdn/video.mp4"]},
    })
    _wire(monkeypatch, s3, jobs)

    pc._mark_run_complete("sess", "plan-4", _nodes("t1_video"), {"t1_video": "completed"})

    agg = json.loads(s3.puts[0]["body"].decode())
    rec = agg["nodes"][0]
    assert rec["kind"] == "media"
    assert rec["artifacts"][0]["url"] == "https://cdn/video.mp4"
    # The derived endpoint is the queue APP id, not the full model path.
    assert rec["handle"]["endpoint_id"] == "bytedance/seedance-2.0"


def test_an_unresolved_queue_handle_is_pending_not_a_crash(monkeypatch):
    s3 = _S3(manifest={
        "outputs": {
            "t1": {"status": "completed", "value": {"request_id": "req-x", "model": "a/b"}},
        },
    })
    jobs = _JobTable(rows={"req-x": {"status": "RUNNING"}})
    _wire(monkeypatch, s3, jobs)

    pc._mark_run_complete("sess", "plan-5", _nodes("t1"), {"t1": "completed"})
    agg = json.loads(s3.puts[0]["body"].decode())
    assert agg["nodes"][0]["kind"] == "pending"


def test_a_missing_manifest_does_not_stop_the_plan_going_terminal(monkeypatch):
    s3 = _S3(manifest=None)
    jobs = _JobTable()
    _wire(monkeypatch, s3, jobs)

    pc._mark_run_complete("sess", "plan-6", _nodes("t1"), {"t1": "completed"})

    assert s3.puts == []
    # Terminal status still recorded, without a resultKey.
    assert jobs.updates and jobs.updates[0]["ExpressionAttributeValues"][":s"] == "SUCCEEDED"
    assert ":rk" not in jobs.updates[0]["ExpressionAttributeValues"]


def test_aggregation_failure_does_not_stop_the_plan_going_terminal(monkeypatch):
    class _Boom(_S3):
        def put_object(self, **kw):
            raise RuntimeError("AccessDenied")

    s3 = _Boom(manifest={"outputs": {"t1": {"status": "completed", "value": "s3://b/a.mp4"}}})
    jobs = _JobTable()
    _wire(monkeypatch, s3, jobs)

    pc._mark_run_complete("sess", "plan-7", _nodes("t1"), {"t1": "completed"})
    assert jobs.updates and jobs.updates[0]["ExpressionAttributeValues"][":s"] == "SUCCEEDED"


def test_the_aggregate_publishes_the_dag_edges_and_tools(monkeypatch):
    """`depends_on` + `tool` must reach the aggregate — the render hook needs both.

    `tool` selects the render VIEW deterministically; `depends_on` is what lets the hook
    tell a deliverable from plumbing (a proxy transcode feeding an analysis must not get
    its own tab). Neither is produced by `build_aggregate` — the chainer stamps them from
    the node defs, so this pins that they survive into the published artifact.

    An empty list must be written EXPLICITLY: `[]` means "no dependencies", which the
    consumer must be able to distinguish from a missing field ("graph unknown" -> fail open).
    """
    s3 = _S3(manifest={
        "outputs": {
            "t1": {"status": "completed", "value": "s3://b/proxy/transcoded.mp4"},
            "t2": {"status": "completed", "value": {"s3uri": "s3://b/shots.json"}},
        },
    }, objects={"shots.json": {"n_shots": 7}})
    jobs = _JobTable()
    _wire(monkeypatch, s3, jobs)

    node_defs = [
        {"id": "t1", "tools": ["media-processing___transcode"], "depends_on": []},
        {"id": "t2", "tools": ["shot-segmentation___segment_shots"], "depends_on": ["t1"]},
    ]
    pc._mark_run_complete("sess", "plan-9", node_defs,
                          {"t1": "completed", "t2": "completed"})

    agg = json.loads(s3.puts[0]["body"].decode())
    by_id = {n["node_id"]: n for n in agg["nodes"]}
    assert by_id["t1"]["tool"] == "media-processing___transcode"
    assert by_id["t2"]["tool"] == "shot-segmentation___segment_shots"
    # The edge that makes t1 plumbing:
    assert by_id["t2"]["depends_on"] == ["t1"]
    # Explicit empty list, not an absent key:
    assert by_id["t1"]["depends_on"] == []


def test_the_published_edges_are_consumable_and_fail_open(monkeypatch):
    """The consumer's verdict on the artifact this chainer just wrote.

    Ties the halves together: the aggregate must carry edges the render hook can reason
    about. This fixture resolves only ONE node to a renderable artifact, so dropping the
    consumed transcode would leave nothing to show — and the hook's fail-open guard must
    refuse. That is the safety property that matters more than the drop itself: this layer
    only ever REMOVES tabs, so an aggregate it cannot fully interpret must lose nothing.

    (The drop itself, on realistic multi-artifact nodes, is covered by
    agent/tests/test_intermediate_nodes.py.)
    """
    import sys
    from pathlib import Path
    for up in (3, 4):
        sys.path.insert(0, str(Path(__file__).resolve().parents[up] / "agent" / "src"))
    try:
        from hooks.provider import intermediate_node_ids
    except Exception:  # pragma: no cover - agent src not importable in this runner
        import pytest
        pytest.skip("agent/src not importable here")

    s3 = _S3(manifest={
        "outputs": {
            "t1": {"status": "completed", "value": "s3://b/proxy/transcoded.mp4"},
            "t2": {"status": "completed", "value": {"s3uri": "s3://b/shots.json"}},
        },
    }, objects={"shots.json": {"n_shots": 7}})
    _wire(monkeypatch, s3, _JobTable())
    pc._mark_run_complete("sess", "plan-10", [
        {"id": "t1", "tools": ["media-processing___transcode"], "depends_on": []},
        {"id": "t2", "tools": ["shot-segmentation___segment_shots"], "depends_on": ["t1"]},
    ], {"t1": "completed", "t2": "completed"})

    agg = json.loads(s3.puts[0]["body"].decode())
    by_id = {n["node_id"]: n for n in agg["nodes"]}
    # The edge IS published, so the hook can see the graph...
    assert by_id["t2"]["depends_on"] == ["t1"]
    renderable = [nid for nid, n in by_id.items() if (n.get("artifacts") or [])]
    assert renderable == ["t1"], f"fixture resolves one renderable node: {renderable}"
    # ...and because dropping it would leave nothing, nothing is dropped.
    assert intermediate_node_ids(agg["nodes"]) == set()
