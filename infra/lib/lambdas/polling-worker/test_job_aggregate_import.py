"""The polling worker's LONE-job aggregate must persist artifacts to CloudFront just like the
plan chainer — otherwise direct-dispatch download/render links are M2C presigned refs that die
after ~1 hour. Pins that _publish_job_result injects a working importer into aggregate_json.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handler as h  # noqa: E402


def test_publish_job_result_injects_working_cloudfront_importer(monkeypatch):
    captured = {}

    def fake_aggregate_json(**kw):
        captured.update(kw)
        return "{}"

    class _S3:
        def __init__(self):
            self.copies, self.puts = [], []

        def copy_object(self, **kw):
            self.copies.append(kw)

        def put_object(self, **kw):
            self.puts.append(kw)

    s3 = _S3()
    monkeypatch.setattr(h.result_resolution, "aggregate_json", fake_aggregate_json)
    monkeypatch.setattr(h, "_s3", s3)
    monkeypatch.setattr(h, "SKILLS_S3_BUCKET", "skills-bkt")
    monkeypatch.setattr(h, "ARTIFACTS_DOMAIN", "d123.cloudfront.net")

    key = h._publish_job_result("job1", "SUCCEEDED", {"ok": 1}, "", artifact_urls=[])
    assert key == "jobs/job1/result.json"                 # aggregate written
    imp = captured.get("import_artifact")
    assert callable(imp), "import_artifact must be injected so lone-job links persist"
    # the injected importer actually copies an M2C proxy artifact to a durable CloudFront url
    # under the job-scoped OUTPUT prefix (artifacts/<job_id>/output/) — consistent + durable.
    url = imp("m2c-proxy", "micro-workflow/x/out.wav", "job1/job1/out.wav")
    assert url == "https://d123.cloudfront.net/artifacts/job1/output/job1/job1/out.wav"
    assert s3.copies and s3.copies[0]["Bucket"] == "skills-bkt"


def test_publish_job_result_session_scopes_outputs_when_session_id_present(monkeypatch):
    """P1: a UI-driven job carries a sessionId, so its M2C outputs materialize under
    artifacts/<sid>/output/<job>/ — one durable, CloudFront-served place for ALL of a
    session's outputs. Session-less (external/lone) jobs keep the job-scoped fallback."""
    captured = {}

    def fake_aggregate_json(**kw):
        captured.update(kw)
        return "{}"

    class _S3:
        def __init__(self):
            self.copies = []

        def copy_object(self, **kw):
            self.copies.append(kw)

        def put_object(self, **kw):
            pass

    s3 = _S3()
    monkeypatch.setattr(h.result_resolution, "aggregate_json", fake_aggregate_json)
    monkeypatch.setattr(h, "_s3", s3)
    monkeypatch.setattr(h, "SKILLS_S3_BUCKET", "skills-bkt")
    monkeypatch.setattr(h, "ARTIFACTS_DOMAIN", "d123.cloudfront.net")

    arn = "arn:aws:states:us-west-2:100000000002:execution:wf:exec-uuid-123"
    h._publish_job_result(arn, "SUCCEEDED", {"ok": 1}, "", artifact_urls=[], session_id="sess-abc")
    imp = captured["import_artifact"]
    url = imp("m2c-proxy", "micro-workflow/x/out.mp4", "out.mp4")
    # session-scoped: artifacts/<sid>/output/<execution-uuid>/…  (no account id / region / ARN colons)
    assert url == "https://d123.cloudfront.net/artifacts/sess-abc/output/exec-uuid-123/out.mp4"


# ---- asset-lineage Phase 3 (a): session outputs under projects/<pid>/sessions/<sid>/output/ ----
import json as _json  # noqa: E402

import pytest  # noqa: E402

_CASES = _json.loads((Path(__file__).resolve().parents[4] / "agent" / "tests" / "fixtures"
                      / "session_layout_cases.json").read_text())


@pytest.mark.parametrize("case", _CASES["writers"], ids=lambda c: f"{c['flag'] or 'unset'}-{c['pid']}-{c['sid']}")
def test_layout_copy_matches_the_shared_fixture(case, monkeypatch):
    monkeypatch.setenv("SESSION_OUTPUT_LAYOUT", case["flag"])
    assert h._layout_output_prefix(case["sid"], case["pid"]) == case["expectedPrefix"]


def _capture_importer(monkeypatch, **kw):
    captured = {}
    monkeypatch.setattr(h.result_resolution, "aggregate_json", lambda **k: captured.update(k) or "{}")

    class _S3:
        def copy_object(self, **k):
            pass

        def put_object(self, **k):
            pass

    monkeypatch.setattr(h, "_s3", _S3())
    monkeypatch.setattr(h, "SKILLS_S3_BUCKET", "skills-bkt")
    monkeypatch.setattr(h, "ARTIFACTS_DOMAIN", "d123.cloudfront.net")
    arn = "arn:aws:states:us-west-2:100000000002:execution:wf:exec-uuid-123"
    h._publish_job_result(arn, "SUCCEEDED", {"ok": 1}, "", artifact_urls=[], **kw)
    return captured["import_artifact"]


def test_project_layout_puts_job_outputs_under_the_rows_project(monkeypatch):
    monkeypatch.setenv("SESSION_OUTPUT_LAYOUT", "project")
    pid, sid = _CASES["pid"], _CASES["sid"]
    imp = _capture_importer(monkeypatch, session_id=sid, project_id=pid)
    assert imp("m2c-proxy", "micro-workflow/x/out.mp4", "out.mp4") == (
        f"https://d123.cloudfront.net/projects/{pid}/sessions/{sid}/output/exec-uuid-123/out.mp4")


def test_without_the_flag_or_a_project_the_legacy_prefix(monkeypatch):
    pid, sid = _CASES["pid"], _CASES["sid"]
    monkeypatch.delenv("SESSION_OUTPUT_LAYOUT", raising=False)
    imp = _capture_importer(monkeypatch, session_id=sid, project_id=pid)
    assert imp("m2c-proxy", "a/out.mp4", "out.mp4").endswith(f"/artifacts/{sid}/output/exec-uuid-123/out.mp4")
    monkeypatch.setenv("SESSION_OUTPUT_LAYOUT", "project")
    imp = _capture_importer(monkeypatch, session_id=sid, project_id="")
    assert imp("m2c-proxy", "a/out.mp4", "out.mp4").endswith(f"/artifacts/{sid}/output/exec-uuid-123/out.mp4")


def test_reconcile_threads_the_rows_project_into_the_publish(monkeypatch):
    seen = {}
    monkeypatch.setattr(h, "_publish_job_result", lambda *a, **k: seen.update(k) or "jobs/x/result.json")

    class _Reg:
        def set_terminal(self, *a, **k): pass
        def set_active_status(self, *a, **k): pass
        def bump_error(self, *a, **k): return 1

    job = {"jobId": "arn:x:exec", "actorId": "a", "gatewayId": "g", "statusTool": "svc___s", "resultsTool": "",
           "sessionId": _CASES["sid"], "producingProjectId": _CASES["pid"]}
    h.reconcile(job, {"status": "SUCCEEDED"}, registry=_Reg())
    assert seen["session_id"] == _CASES["sid"] and seen["project_id"] == _CASES["pid"]
