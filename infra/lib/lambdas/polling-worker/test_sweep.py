"""Offline polling-worker tests; no AWS calls.

Run with:
    agent/.venv/bin/python -m pytest infra/lib/lambdas/polling-worker/test_sweep.py -q
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("JOB_REGISTRY_TABLE", "test-table")
os.environ["MAX_JOB_AGE_SECONDS"] = "3600"

import handler  # noqa: E402


class FakeRegistry:
    def __init__(self):
        self.terminal = []
        self.active = []

    def set_terminal(
        self,
        job_id,
        *,
        status,
        error=None,
        has_renderable_artifacts=False,
        artifact_urls=None,
        result_key="",
    ):
        # Signature MUST mirror JobRegistry.set_terminal, keyword-only args
        # included. It had drifted -- missing `artifact_urls` and accepting
        # positionally -- so these two tests failed on a TypeError from the fake
        # rather than on anything about the code under test, and stayed red long
        # enough to stop being informative.
        self.terminal.append((job_id, status, has_renderable_artifacts))
        self.artifact_urls = list(artifact_urls or [])
        self.result_key = result_key

    def set_active_status(self, job_id, status, expected_status):
        self.active.append((job_id, status))
        return True

    def bump_error(self, job_id, message):
        return 1


def _iso(seconds_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)).isoformat()


def test_job_age_seconds_parses_iso():
    assert handler._job_age_seconds({"startAt": _iso(120)}) >= 119
    assert handler._job_age_seconds({}) is None
    assert handler._job_age_seconds({"startAt": "not-a-date"}) is None


def test_sweep_times_out_overage_job(monkeypatch):
    polled = []
    monkeypatch.setattr(handler, "_invoke_status", lambda job: polled.append(job["jobId"]) or {"status": "RUNNING"})
    reg = FakeRegistry()
    old = {"jobId": "old", "gatewayId": "__aws_sfn__", "statusTool": "s",
           "actorId": "a", "status": "RUNNING", "startAt": _iso(4000)}
    counters = handler._poll_cycle([old], registry=reg)
    assert reg.terminal == [("old", "TIMED_OUT", False)]
    assert polled == []
    assert counters["timed_out_by_age"] == 1


def test_succeeded_job_with_artifacts_sets_capability(monkeypatch):
    monkeypatch.setattr(handler, "_invoke_status", lambda job: {
        "status": "SUCCEEDED",
        "executionOutput": {"output": {"s3uri": "s3://bucket/runs/new-tool"}},
    })
    reg = FakeRegistry()
    fresh = {"jobId": "fresh", "gatewayId": "__aws_sfn__", "statusTool": "s",
             "actorId": "a", "status": "RUNNING", "startAt": _iso(60)}
    counters = handler._poll_cycle([fresh], registry=reg)
    assert counters["timed_out_by_age"] == 0
    assert reg.terminal == [("fresh", "SUCCEEDED", True)]


def test_succeeded_job_without_artifacts_fails_closed(monkeypatch):
    monkeypatch.setattr(handler, "_invoke_status", lambda job: {
        "status": "SUCCEEDED",
        "executionOutput": {"input": {"s3uri": "s3://bucket/source.mp4"}},
    })
    reg = FakeRegistry()
    fresh = {"jobId": "fresh", "gatewayId": "__aws_sfn__", "statusTool": "s",
             "actorId": "a", "status": "RUNNING", "startAt": _iso(60)}
    handler._poll_cycle([fresh], registry=reg)
    assert reg.terminal == [("fresh", "SUCCEEDED", False)]


def test_describe_sfn_execution_preserves_structured_output(monkeypatch):
    class FakeSfn:
        def describe_execution(self, **kwargs):
            assert kwargs["executionArn"].startswith("arn:aws:states:")
            return {
                "status": "SUCCEEDED",
                "output": '{"output":{"s3uri":"s3://bucket/runs/123"}}',
            }

    monkeypatch.setattr(handler, "_sfn_client_for_arn", lambda _arn: FakeSfn())
    response = handler._describe_sfn_execution(
        "arn:aws:states:us-west-2:123456789012:execution:future-workflow:run-123"
    )
    assert response == {
        "status": "SUCCEEDED",
        "error": None,
        "executionOutput": {"output": {"s3uri": "s3://bucket/runs/123"}},
    }


def test_the_fake_registry_still_mirrors_the_real_signature():
    """Guard against the drift that has now bitten twice.

    FakeRegistry.set_terminal has fallen behind JobRegistry.set_terminal twice --
    first missing `artifact_urls`, then `result_key` -- and each time two tests
    failed on a TypeError from the FAKE rather than on the code under test. A
    signature comparison turns that into an explicit, self-describing failure.
    """
    import inspect

    from registry import JobRegistry

    real = set(inspect.signature(JobRegistry.set_terminal).parameters) - {"self"}
    fake = set(inspect.signature(FakeRegistry.set_terminal).parameters) - {"self"}
    assert real == fake, f"fake is missing {real - fake}, has extra {fake - real}"


# An SFN-polled M2C job whose execution output names no file (shot-metadata-enrichment /
# conversation-analysis end with {uuid, total_estimated_cost}): the status tool's declared
# outputs must reach artifactUrls and result.json (staging, enrichment 6e060617: artifactUrls []).
_PROXY = "m2cv6-0e0000000002-100000000002-us-west-2-proxy"
_JSONL = (f"s3://{_PROXY}/micro-workflow/shot-metadata-enrichment/"
          "6e060617-adb4-5327-a362-0dcd5733489f/shot_metadata.jsonl")
_ENRICH_JOB = {"jobId": "arn:aws:states:us-west-2:1:execution:shot-metadata-enrichment:6e060617",
               "gatewayId": "__aws_sfn__", "statusTool": "shot-metadata-enrichment___get_job_status",
               "resultsGatewayId": "media-workflow-gateway", "resultsTool": "",
               "actorId": "a", "status": "RUNNING", "startAt": _iso(60)}


def _capture_publish(monkeypatch):
    published = {}

    def fake_publish(job_id, status, payload, error, artifact_urls=None, **kw):
        published["payload"] = payload
        published["artifact_urls"] = list(artifact_urls or [])
        return "jobs/x/result.json"
    monkeypatch.setattr(handler, "_publish_job_result", fake_publish)
    return published


def test_sfn_job_without_output_prefix_gets_the_status_tools_declared_outputs(monkeypatch):
    monkeypatch.setattr(handler, "_invoke_status", lambda job: {
        "status": "SUCCEEDED", "error": None,
        "executionOutput": {"uuid": "6e060617", "total_estimated_cost": 0.04}})
    calls = []
    monkeypatch.setattr(handler, "_invoke_declared_status", lambda job: calls.append(job) or {
        "status": "SUCCEEDED", "outputs": [{"label": "shots", "uri": _JSONL}],
        "output_s3_prefix": _JSONL.rsplit("/", 1)[0]})
    published = _capture_publish(monkeypatch)
    reg = FakeRegistry()
    handler._poll_cycle([dict(_ENRICH_JOB)], registry=reg)
    assert len(calls) == 1
    assert published["payload"]["executionOutput"]["outputs"][0]["uri"] == _JSONL
    assert reg.terminal[0][1] == "SUCCEEDED"


def test_sfn_job_with_output_prefix_does_not_call_the_status_tool(monkeypatch):
    out = {"output": {"s3uri": f"s3://{_PROXY}/micro-workflow/shot-segmentation/4d701de3/"}}
    monkeypatch.setattr(handler, "_invoke_status", lambda job: {
        "status": "SUCCEEDED", "error": None, "executionOutput": out})
    calls = []
    monkeypatch.setattr(handler, "_invoke_declared_status", lambda job: calls.append(job) or {})
    published = _capture_publish(monkeypatch)
    handler._poll_cycle([dict(_ENRICH_JOB)], registry=FakeRegistry())
    assert calls == []
    assert published["payload"]["executionOutput"] == out


def test_status_tool_failure_keeps_the_execution_output(monkeypatch):
    monkeypatch.setattr(handler, "_invoke_status", lambda job: {
        "status": "SUCCEEDED", "error": None, "executionOutput": {"uuid": "6e060617"}})

    def boom(job):
        raise handler.GatewayInvocationError("down")
    monkeypatch.setattr(handler, "_invoke_declared_status", boom)
    published = _capture_publish(monkeypatch)
    reg = FakeRegistry()
    handler._poll_cycle([dict(_ENRICH_JOB)], registry=reg)
    assert published["payload"]["executionOutput"] == {"uuid": "6e060617"}
    assert reg.terminal[0][1] == "SUCCEEDED"


def test_m2c_credentials_bundle_yields_its_client_secret():
    import json as _json
    import gateway_invoker as gi

    class _Secrets:
        def __init__(self, s): self.s = s
        def get_secret_value(self, SecretId): return {"SecretString": self.s}
    bundle = _json.dumps({"tokenEndpoint": "https://t", "clientId": "c", "clientSecret": "s3cr3t"})
    assert gi._resolve_client_secret(_Secrets(bundle), "arn") == "s3cr3t"
    assert gi._resolve_client_secret(_Secrets("raw-secret"), "arn") == "raw-secret"


def test_real_publish_of_a_merged_enrichment_job_lists_declared_files_not_an_error(monkeypatch):
    """The REAL _publish_job_result + aggregate_json on the poller's wrapped payload.

    The merge lands in payload["executionOutput"]; the resolver must read the declared outputs
    there. Before the fix it scraped the .jsonl uri and failed "was not readable json"."""
    import json as _json

    written = {}

    class _S3:
        def put_object(self, Bucket, Key, Body, ContentType):
            written[Key] = _json.loads(Body)

        def copy_object(self, **kw):
            return {}

    monkeypatch.setattr(handler, "SKILLS_S3_BUCKET", "skills-bucket")
    monkeypatch.setattr(handler, "ARTIFACTS_DOMAIN", "cdn.example")
    monkeypatch.setattr(handler, "_s3", _S3())
    # Realistic fakes: the .jsonl is not JSON, and the poller cannot list the proxy bucket.
    monkeypatch.setattr(handler, "_read_s3_json", lambda b, k, max_bytes=None: (None, 10360))
    monkeypatch.setattr(handler, "_list_output_prefix", lambda b, p: [])
    usage = _JSONL.rsplit("/", 1)[0] + "/usage_tracking.json"
    payload = {"status": "SUCCEEDED", "error": None, "executionOutput": {
        "uuid": "6e060617", "total_estimated_cost": 0.04,
        "outputs": [{"label": "shots", "uri": _JSONL}, {"label": "usage", "uri": usage}],
        "output": {"s3uri": _JSONL.rsplit("/", 1)[0]}}}
    key = handler._publish_job_result(_ENRICH_JOB["jobId"], "SUCCEEDED", payload, None)
    assert key
    body = written[key]
    text = _json.dumps(body)
    assert "not readable json" not in text
    assert "shot_metadata.jsonl" in text
    assert "usage_tracking.json" not in _json.dumps(
        [n.get("artifacts") for n in body.get("nodes", [])] if isinstance(body.get("nodes"), list)
        else body)
    assert body.get("counts", {}).get("error", 0) == 0
