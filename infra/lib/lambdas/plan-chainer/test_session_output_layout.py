"""Session outputs under projects/<pid>/sessions/<sid>/output/ (asset-lineage Phase 3 (a), design §9.1).

- `_layout_output_prefix` (this bundle's copy of the agent helper) agrees with every writer case of
  agent/tests/fixtures/session_layout_cases.json;
- with SESSION_OUTPUT_LAYOUT=project, an imported deliverable lands under the plan's project, read from
  the plan row's `producingProjectId`, and every node dispatch forwards it as `producing_project_id`;
- without the flag nothing changes: legacy prefix, no plan-row read, no new payload key.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_model_propagation import _setup, pc

CASES = json.loads((Path(__file__).resolve().parents[4] / "agent" / "tests" / "fixtures"
                    / "session_layout_cases.json").read_text())
PID, SID = CASES["pid"], CASES["sid"]
NODE = {"id": "t1", "prompt": "do"}


class _JobTable:
    def __init__(self, item=None, fail=False):
        self.item, self.fail, self.reads = item, fail, []

    def get_item(self, Key):
        self.reads.append(Key)
        if self.fail:
            raise RuntimeError("throttled")
        return {"Item": self.item} if self.item else {}


class _RecordingS3:
    def __init__(self):
        self.calls = []

    def copy_object(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(pc, "_PLAN_PROJECT_CACHE", {})


@pytest.mark.parametrize("case", CASES["writers"], ids=lambda c: f"{c['flag'] or 'unset'}-{c['pid']}-{c['sid']}")
def test_layout_copy_matches_the_shared_fixture(case, monkeypatch):
    monkeypatch.setenv("SESSION_OUTPUT_LAYOUT", case["flag"])
    assert pc._layout_output_prefix(case["sid"], case["pid"]) == case["expectedPrefix"]


def test_import_lands_under_the_plans_project(monkeypatch):
    monkeypatch.setenv("SESSION_OUTPUT_LAYOUT", "project")
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "harness-bucket")
    monkeypatch.setattr(pc, "ARTIFACTS_DOMAIN", "d.cloudfront.net")
    jt = _JobTable({"jobId": "plan-1", "producingProjectId": PID})
    monkeypatch.setattr(pc, "_job_table", jt)
    s3 = _RecordingS3()
    monkeypatch.setattr(pc, "_s3", s3)
    url = pc._make_import_artifact(SID, pc._plan_producing_project("plan-1"))("m2c", "a/b.mp4", "p1/n1/b.mp4")
    key = f"projects/{PID}/sessions/{SID}/output/p1/n1/b.mp4"
    assert s3.calls[0]["Key"] == key
    assert url == f"https://d.cloudfront.net/{key}"  # an identifier; the CDN is private
    assert jt.reads == [{"jobId": "plan-1"}]
    pc._plan_producing_project("plan-1")
    assert len(jt.reads) == 1, "a plan's project is cached"


def test_without_the_flag_the_legacy_prefix_and_no_read(monkeypatch):
    monkeypatch.delenv("SESSION_OUTPUT_LAYOUT", raising=False)
    jt = _JobTable({"jobId": "plan-1", "producingProjectId": PID})
    monkeypatch.setattr(pc, "_job_table", jt)
    assert pc._plan_producing_project("plan-1") == ""
    assert jt.reads == []
    assert pc._session_output_prefix(SID, PID) == f"artifacts/{SID}/output/"


def test_unreadable_or_malformed_plan_row_means_legacy(monkeypatch):
    monkeypatch.setenv("SESSION_OUTPUT_LAYOUT", "project")
    monkeypatch.setattr(pc, "_job_table", _JobTable(fail=True))
    assert pc._plan_producing_project("plan-1") == ""
    assert "plan-1" not in pc._PLAN_PROJECT_CACHE, "a failed read is not cached"
    monkeypatch.setattr(pc, "_job_table", _JobTable({"jobId": "plan-2", "producingProjectId": "personal"}))
    assert pc._plan_producing_project("plan-2") == ""


def test_dispatch_forwards_the_producing_project(monkeypatch):
    ac = _setup(monkeypatch)
    monkeypatch.setenv("SESSION_OUTPUT_LAYOUT", "project")
    monkeypatch.setattr(pc, "_job_table", _JobTable({"jobId": "p1", "producingProjectId": PID}))
    pc._dispatch_node("s", "p1", "a", NODE, [NODE], {})
    assert json.loads(ac.calls[0]["payload"].decode())["producing_project_id"] == PID


def test_dispatch_omits_it_without_the_flag(monkeypatch):
    ac = _setup(monkeypatch)
    monkeypatch.delenv("SESSION_OUTPUT_LAYOUT", raising=False)
    pc._dispatch_node("s", "p1", "a", NODE, [NODE], {})
    assert "producing_project_id" not in json.loads(ac.calls[0]["payload"].decode())


def test_first_dispatch_has_the_project_before_the_registry_row_exists(monkeypatch):
    """Staging plan 73d92d46: the chainer fired on the plan-state rows at 23:33:06.9 and dispatched both
    sg5 nodes; the job-registry plan row carrying producingProjectId landed at 23:33:10.1. The absent row
    was cached as "no project", the nodes got project_id=none sandbox credentials, and every write to
    projects/<pid>/sessions/<sid>/output/ was AccessDenied. The PLAN row now carries the project itself."""
    ac = _setup(monkeypatch)
    monkeypatch.setenv("SESSION_OUTPUT_LAYOUT", "project")
    jt = _JobTable(None)  # the registry plan row is not written yet
    monkeypatch.setattr(pc, "_job_table", jt)
    plan_record = {"node_id": "PLAN#p1", "plan_id": "p1", "actor_id": "a", "turn_id": "",
                   "delegation_grant": "", "nodes_json": json.dumps([NODE]),
                   "producing_project_id": PID}
    items = [plan_record, {"node_id": "p1#t1", "plan_id": "p1", "node": "t1", "status": "pending"}]
    pc._advance_plan(SID, plan_record, items)
    assert ac.calls, "the ready node was dispatched"
    assert json.loads(ac.calls[0]["payload"].decode())["producing_project_id"] == PID


def test_an_absent_registry_row_is_not_cached_as_no_project(monkeypatch):
    monkeypatch.setenv("SESSION_OUTPUT_LAYOUT", "project")
    monkeypatch.setattr(pc, "_job_table", _JobTable(None))
    assert pc._plan_producing_project("plan-9") == ""
    assert "plan-9" not in pc._PLAN_PROJECT_CACHE
    monkeypatch.setattr(pc, "_job_table", _JobTable({"jobId": "plan-9", "producingProjectId": PID}))
    assert pc._plan_producing_project("plan-9") == PID


def test_a_malformed_plan_row_project_seeds_nothing(monkeypatch):
    monkeypatch.setenv("SESSION_OUTPUT_LAYOUT", "project")
    pc._seed_plan_producing_project("plan-8", "personal")
    pc._seed_plan_producing_project("plan-8", None)
    assert "plan-8" not in pc._PLAN_PROJECT_CACHE
