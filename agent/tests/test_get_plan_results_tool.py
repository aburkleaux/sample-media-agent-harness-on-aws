"""Tests for ``get_plan_results`` — reading a DAG plan's node outputs.

Regression context: a plan whose nodes all reported SUCCEEDED had no readable
results. Coordinator job rows carry an empty ``results_tool`` by design, and the
plan manifest holding the outputs was readable only by ``task_agent``, so asked to
"retrieve the results" the orchestrator looped through model round-trips looking
for file references nothing would supply.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobs import context as jobs_context  # noqa: E402
from jobs import get_plan_results_tool as mod  # noqa: E402


class _Rec:
    def __init__(self, session_id="sess-1", status="SUCCEEDED"):
        self.session_id = session_id
        self.status = status


class _Registry:
    """Stands in for JobRegistry.get, which enforces actor scoping itself."""

    def __init__(self, rec=None, raises=False):
        self._rec = rec
        self._raises = raises
        self.asked = []
        self.actors = []

    def get(self, job_id, *, actor_id):
        # Signature MUST mirror JobRegistry.get(job_id, *, actor_id): a looser stub
        # let a call missing actor_id pass the suite and fail against the real
        # registry, which is exactly how this bug reached a live run.
        self.asked.append(job_id)
        self.actors.append(actor_id)
        if self._raises:
            raise RuntimeError("ddb down")
        return self._rec


MANIFEST = {
    "display_name": "plan-dogvideo-01",
    "goal": "video with soundtrack",
    "status": "completed",
    "nodes": {
        "generate_video": {"output_key": "v", "output_type": "json"},
        "combine": {"output_key": "f", "output_type": "s3_uri"},
    },
    "outputs": {
        "generate_video": {
            "status": "completed",
            "output_type": "json",
            "value": {"request_id": "req-1", "status_url": "https://queue/status"},
            "completed_at": "2026-08-06T18:56:15Z",
        },
        "combine": {
            "status": "completed",
            "output_type": "s3_uri",
            "value": "s3://bucket/final.mp4",
            "completed_at": "2026-08-06T18:56:50Z",
        },
    },
}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    jobs_context.set_request_context(actor_id="actor-1", session_id="sess-caller")
    mod.set_registry(None)
    mod.set_manifest_location(bucket="", region="us-west-2")
    yield
    mod.set_registry(None)
    mod.set_manifest_location(bucket="", region="us-west-2")


def _stub_manifest(monkeypatch, data, captured=None):
    class _M:
        def __init__(self, *, bucket, session_id, plan_id, region):
            if captured is not None:
                captured.update(
                    bucket=bucket, session_id=session_id, plan_id=plan_id, region=region
                )

        def read(self):
            return data

    import planning.plan_manifest as plan_manifest

    monkeypatch.setattr(plan_manifest, "PlanManifest", _M)


def test_registry_unavailable_when_not_wired():
    out = json.loads(mod.get_plan_results("p1"))
    assert out["error"] == "registry_unavailable"


def test_manifest_unavailable_when_bucket_missing():
    mod.set_registry(_Registry(_Rec()))
    out = json.loads(mod.get_plan_results("p1"))
    assert out["error"] == "manifest_unavailable"


def test_cross_actor_and_missing_are_indistinguishable():
    # JobRegistry.get returns None for BOTH a missing job and another actor's job.
    mod.set_registry(_Registry(None))
    mod.set_manifest_location(bucket="b")
    out = json.loads(mod.get_plan_results("p1"))
    assert out == {"error": "not_found", "plan_id": "p1"}


def test_unbound_actor_is_a_config_error_not_a_missing_plan(monkeypatch):
    monkeypatch.setattr(
        jobs_context, "current_actor_id", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    mod.set_registry(_Registry(_Rec()))
    mod.set_manifest_location(bucket="b")
    assert json.loads(mod.get_plan_results("p1"))["error"] == "registry_unavailable"


def test_lookup_is_scoped_to_the_bound_actor(monkeypatch):
    reg = _Registry(_Rec())
    mod.set_registry(reg)
    mod.set_manifest_location(bucket="b")
    _stub_manifest(monkeypatch, MANIFEST)
    mod.get_plan_results("p1")
    assert reg.actors == ["actor-1"]


def test_registry_error_does_not_leak():
    mod.set_registry(_Registry(raises=True))
    mod.set_manifest_location(bucket="b")
    out = json.loads(mod.get_plan_results("p1"))
    assert out["error"] == "registry_unavailable"


def test_returns_node_outputs(monkeypatch):
    mod.set_registry(_Registry(_Rec()))
    mod.set_manifest_location(bucket="b")
    _stub_manifest(monkeypatch, MANIFEST)
    out = json.loads(mod.get_plan_results("p1"))
    assert out["display_name"] == "plan-dogvideo-01"
    assert set(out["nodes"]) == {"generate_video", "combine"}
    assert out["nodes"]["combine"]["value"] == "s3://bucket/final.mp4"
    # An async submission must survive verbatim so the agent can report it as
    # still running rather than as a missing artifact.
    assert out["nodes"]["generate_video"]["value"]["status_url"] == "https://queue/status"


def test_manifest_path_uses_the_records_session_not_the_callers(monkeypatch):
    # Scoping AND usability: the session comes from the verified job record, so the
    # plan is readable from a later session than the one that created it.
    captured: dict = {}
    mod.set_registry(_Registry(_Rec(session_id="sess-original")))
    mod.set_manifest_location(bucket="b", region="us-west-2")
    _stub_manifest(monkeypatch, MANIFEST, captured)
    mod.get_plan_results("p1")
    assert captured["session_id"] == "sess-original"


def test_node_row_id_is_reduced_to_its_plan(monkeypatch):
    reg = _Registry(_Rec())
    mod.set_registry(reg)
    mod.set_manifest_location(bucket="b")
    _stub_manifest(monkeypatch, MANIFEST)
    out = json.loads(mod.get_plan_results("p1#generate_video"))
    assert reg.asked == ["p1"]
    assert out["plan_id"] == "p1"


def test_missing_manifest_reports_job_status(monkeypatch):
    mod.set_registry(_Registry(_Rec(status="SUCCEEDED")))
    mod.set_manifest_location(bucket="b")
    _stub_manifest(monkeypatch, None)
    out = json.loads(mod.get_plan_results("p1"))
    assert out["error"] == "manifest_missing"
    assert out["job_status"] == "SUCCEEDED"


def test_declared_node_without_output_is_still_listed(monkeypatch):
    data = {**MANIFEST, "outputs": {}}
    mod.set_registry(_Registry(_Rec()))
    mod.set_manifest_location(bucket="b")
    _stub_manifest(monkeypatch, data)
    out = json.loads(mod.get_plan_results("p1"))
    assert out["nodes"]["generate_video"]["status"] == "pending"
    assert out["nodes"]["generate_video"]["value"] is None


# ---------------------------------------------------------------------------
# The tool is now a THIN READER of the chainer's resolved aggregate.
# ---------------------------------------------------------------------------

AGGREGATE = {
    "schema": "plan-result/v1",
    "plan_id": "plan-1",
    "status": "SUCCEEDED",
    "counts": {"media": 1, "analysis": 1},
    "artifact_count": 1,
    "nodes": [
        {"node_id": "t1", "status": "completed", "kind": "media",
         "artifacts": [{"url": "https://cdn/a.mp4", "media_kind": "video", "role": "output"}],
         "findings": None, "error": "", "handle": {}},
        {"node_id": "t2", "status": "completed", "kind": "analysis",
         "artifacts": [], "findings": {"n_shots": 94}, "error": "", "handle": {}},
    ],
}


def test_the_resolved_aggregate_is_returned_without_touching_the_manifest(monkeypatch):
    """One call, fully resolved, zero follow-ups -- the point of the change."""
    mod.set_registry(_Registry(_Rec()))
    mod.set_manifest_location(bucket="b", region="us-west-2")
    monkeypatch.setattr(jobs_context, "current_actor_id", lambda: "actor-1")
    monkeypatch.setattr(mod, "_read_plan_result", lambda s, p: dict(AGGREGATE))

    def _boom(*a, **k):
        raise AssertionError("manifest must not be read when an aggregate exists")

    monkeypatch.setattr(mod, "PlanManifest", _boom, raising=False)

    out = json.loads(mod.get_plan_results("plan-1"))
    assert out["schema"] == "plan-result/v1"
    assert out["job_status"] == "SUCCEEDED"
    kinds = {n["node_id"]: n["kind"] for n in out["nodes"]}
    assert kinds == {"t1": "media", "t2": "analysis"}
    # Resolved means resolved: a url, not an s3 ref or a request_id.
    assert out["nodes"][0]["artifacts"][0]["url"] == "https://cdn/a.mp4"


def test_downloads_lists_every_artifact_url_including_data_and_audio(monkeypatch):
    # #2/#3: metadata JSON + audio must be downloadable, not just video. The tool
    # flattens every node's artifacts into a deduped `downloads` list the agent
    # renders as links. Generic — driven by whatever artifacts resolved, no
    # filename allowlist.
    agg = {
        "schema": "plan-result/v1",
        "status": "SUCCEEDED",
        "nodes": [
            {"node_id": "trim", "kind": "media",
             "artifacts": [{"url": "https://cdn/clip.mp4", "media_kind": "video", "role": "output"}]},
            {"node_id": "shots", "kind": "analysis",
             "artifacts": [{"url": "https://cdn/shots.json", "media_kind": "data", "role": "analysis"}],
             "findings": {"n_shots": 15}},
            {"node_id": "stems", "kind": "media",
             "artifacts": [
                 {"url": "https://cdn/speech.wav", "media_kind": "audio", "role": "output"},
                 {"url": "https://cdn/clip.mp4", "media_kind": "video", "role": "output"},  # dup
             ]},
        ],
    }
    mod.set_registry(_Registry(_Rec()))
    mod.set_manifest_location(bucket="b", region="us-west-2")
    monkeypatch.setattr(jobs_context, "current_actor_id", lambda: "actor-1")
    monkeypatch.setattr(mod, "_read_plan_result", lambda s, p: dict(agg))

    out = json.loads(mod.get_plan_results("plan-1"))
    urls = [d["url"] for d in out["downloads"]]
    # Every distinct artifact url, deduped, media AND data/analysis AND audio.
    assert urls == ["https://cdn/clip.mp4", "https://cdn/shots.json", "https://cdn/speech.wav"]
    by_url = {d["url"]: d for d in out["downloads"]}
    assert by_url["https://cdn/shots.json"]["media_kind"] == "data"
    assert by_url["https://cdn/speech.wav"]["media_kind"] == "audio"


def test_a_plan_without_an_aggregate_falls_back_to_the_manifest(monkeypatch):
    # Plans that settled before the aggregate existed must keep working.
    mod.set_registry(_Registry(_Rec()))
    mod.set_manifest_location(bucket="b", region="us-west-2")
    monkeypatch.setattr(jobs_context, "current_actor_id", lambda: "actor-1")
    monkeypatch.setattr(mod, "_read_plan_result", lambda s, p: None)

    class _Manifest:
        def __init__(self, **kw):
            pass

        def read(self):
            return MANIFEST

    monkeypatch.setitem(sys.modules, "planning.plan_manifest", type(
        "M", (), {"PlanManifest": _Manifest})())

    out = json.loads(mod.get_plan_results("plan-1"))
    assert "nodes" in out
    assert out.get("schema") is None
    assert set(out["nodes"]) == {"generate_video", "combine"}


def test_an_aggregate_without_a_schema_marker_is_ignored(monkeypatch):
    # Guards against a truncated or half-written object being trusted.
    mod.set_registry(_Registry(_Rec()))
    mod.set_manifest_location(bucket="b", region="us-west-2")
    monkeypatch.setattr(jobs_context, "current_actor_id", lambda: "actor-1")
    called = {"n": 0}

    def _reader(session_id, plan_id):
        called["n"] += 1
        return None

    monkeypatch.setattr(mod, "_read_plan_result", _reader)

    class _Manifest:
        def __init__(self, **kw):
            pass

        def read(self):
            return MANIFEST

    monkeypatch.setitem(sys.modules, "planning.plan_manifest", type(
        "M", (), {"PlanManifest": _Manifest})())

    out = json.loads(mod.get_plan_results("plan-1"))
    assert called["n"] == 1
    assert out.get("schema") is None


class _RecFull:
    def __init__(self, job_id, trio_id, session_id="sess-1", status="SUCCEEDED"):
        self.job_id = job_id
        self.trio_id = trio_id
        self.session_id = session_id
        self.status = status


class _ResolvingRegistry:
    """get() misses on a display name; list_for_actor lets us resolve it."""

    def __init__(self, rows):
        self._rows = rows
        self.listed = []

    def get(self, job_id, *, actor_id):
        return next((r for r in self._rows if r.job_id == job_id), None)

    def list_for_actor(self, actor_id, *, since=None, limit=None):
        self.listed.append((actor_id, limit))
        return list(self._rows)


def test_display_name_resolves_to_plan_job_id(monkeypatch):
    """The model very often passes the plan DISPLAY NAME ("plan-awsmcp01"), not the
    uuid. A direct get() misses; resolving via trio_id "plan|<name>" avoids the
    not_found that sent the agent into a long manual retrieval loop."""
    plan = _RecFull(job_id="15c95441", trio_id="plan|plan-awsmcp01")
    reg = _ResolvingRegistry([plan])
    mod.set_registry(reg)
    mod.set_manifest_location(bucket="b")
    captured: dict = {}
    _stub_manifest(monkeypatch, MANIFEST, captured)
    out = json.loads(mod.get_plan_results("plan-awsmcp01"))
    assert out.get("error") != "not_found"          # resolved, not treated as missing
    assert out["display_name"] == "plan-dogvideo-01"  # went on to read the results
    assert reg.listed                                # display-name resolution was attempted
    assert captured["plan_id"] == "15c95441"         # results read with the RESOLVED id


def test_unknown_display_name_still_not_found(monkeypatch):
    reg = _ResolvingRegistry([_RecFull(job_id="15c95441", trio_id="plan|other-plan")])
    mod.set_registry(reg)
    mod.set_manifest_location(bucket="b")
    out = json.loads(mod.get_plan_results("nonexistent-plan"))
    assert out["error"] == "not_found"               # no matching trio_id -> still missing
