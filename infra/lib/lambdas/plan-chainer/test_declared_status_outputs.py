"""M2C jobs whose execution output names no file must still hand off their files.

Staging session b8a59e30: the ``shot-metadata-enrichment`` and ``conversation-analysis`` state
machines finish with an execution output of only ``{"uuid", "total_estimated_cost"}``. Their files
are declared solely by the shared status tool (``<svc>___get_job_status`` -> ``outputs[{label,
uri}]``), which the job-registry row names (``statusTool`` on ``resultsGatewayId``). The chainer
stored the execution output verbatim, so every Phase 2 node value was a bare uuid, the Phase 3
``external_inputs`` block had no DELIVERABLE FILES, and the report nodes guessed keys
(AccessDenied on the missing ones).
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE",
                 "SKILLS_S3_BUCKET", "TASK_AGENT_RUNTIME_ARN", "PLAN_RESULT_QUEUE_URL"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_declared", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_declared"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()

PROXY = "m2cv6-0e0000000002-100000000002-us-west-2-proxy"
ARN = ("arn:aws:states:us-west-2:100000000002:execution:shot-metadata-enrichment:"
       "6e060617-adb4-5327-a362-0dcd5733489f")
SFN_OUTPUT = '{"uuid":"6e060617-adb4-5327-a362-0dcd5733489f","total_estimated_cost":0.0421725}'
JSONL = (f"s3://{PROXY}/micro-workflow/shot-metadata-enrichment/"
         "6e060617-adb4-5327-a362-0dcd5733489f/shot_metadata.jsonl")
STATUS = {"status": "SUCCEEDED", "outputs": [{"label": "shots", "uri": JSONL}],
          "output_s3_prefix": JSONL.rsplit("/", 1)[0]}
JOB_ITEM = {"jobId": ARN, "gatewayId": "__aws_sfn__", "resultsTool": "",
            "statusTool": "shot-metadata-enrichment___get_job_status",
            "resultsGatewayId": "media-workflow-gateway", "actorId": "actor-1"}


class _CCF(Exception):
    pass


def _meta():
    return type("M", (), {"client": type("C", (), {"exceptions": type(
        "E", (), {"ConditionalCheckFailedException": _CCF})})})


class FakeTable:
    def __init__(self, item=None):
        self.meta = _meta()
        self.item = item or {}
        self.updates = []

    def update_item(self, **kw):
        self.updates.append(kw)
        return {}

    def get_item(self, Key):
        return {"Item": self.item} if self.item else {}


class FakeS3:
    def __init__(self):
        self.puts = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts[Key] = json.loads(Body.decode("utf-8"))
        return {}

    def get_object(self, **kw):
        raise RuntimeError("no manifest yet")


class FakeSfn:
    def __init__(self, output=SFN_OUTPUT):
        self.output = output

    def describe_execution(self, executionArn=None):
        return {"status": "SUCCEEDED", "output": self.output}


def _wire(monkeypatch, invoke, output=SFN_OUTPUT, job_item=None):
    s3 = FakeS3()
    monkeypatch.setattr(pc, "_plan_table", FakeTable(), raising=False)
    monkeypatch.setattr(pc, "_job_table", FakeTable(job_item), raising=False)
    monkeypatch.setattr(pc, "_gateway_table", object(), raising=False)
    monkeypatch.setattr(pc, "_s3", s3, raising=False)
    monkeypatch.setattr(pc, "_sfn_client_for_arn", lambda *a, **k: FakeSfn(output), raising=False)
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "skills-bkt", raising=False)
    monkeypatch.setattr(pc, "invoke_mcp_tool", invoke, raising=False)
    return s3


def test_results_fetch_folds_in_the_status_tools_declared_outputs(monkeypatch):
    calls = []
    s3 = _wire(monkeypatch, lambda **kw: calls.append(kw) or STATUS)
    pc._dispatch_sfn_output_as_result("sess", "P2", "sg3", ARN, "2026-10-09T17:03:20+00:00",
                                      job_item=JOB_ITEM, actor_id="actor-1")
    assert [c["tool_name"] for c in calls] == ["shot-metadata-enrichment___get_job_status"]
    assert calls[0]["gateway_id"] == "media-workflow-gateway"
    assert calls[0]["job_id"] == ARN
    value = s3.puts["plans/sess/P2/manifest.json"]["outputs"]["sg3"]["value"]
    assert value["outputs"] == [{"label": "shots", "uri": JSONL}]
    assert value["uuid"] == "6e060617-adb4-5327-a362-0dcd5733489f"
    assert s3.puts["plans/sess/P2/sg3/output"]["outputs"][0]["uri"] == JSONL


def test_status_tool_failure_keeps_the_sfn_value_and_warns(monkeypatch, caplog):
    def boom(**kw):
        raise pc.GatewayInvocationError("gateway down")
    s3 = _wire(monkeypatch, boom)
    with caplog.at_level(logging.WARNING):
        pc._dispatch_sfn_output_as_result("sess", "P2", "sg3", ARN, "now", job_item=JOB_ITEM)
    value = s3.puts["plans/sess/P2/manifest.json"]["outputs"]["sg3"]["value"]
    assert value == json.loads(SFN_OUTPUT)
    assert s3.puts["plans/sess/P2/manifest.json"]["outputs"]["sg3"]["status"] == "completed"
    assert any("shot-metadata-enrichment___get_job_status" in r.getMessage()
               and r.levelno == logging.WARNING for r in caplog.records)


def test_a_value_that_already_has_an_output_prefix_is_not_re_read(monkeypatch):
    calls = []
    out = json.dumps({"output": {"s3uri": f"s3://{PROXY}/micro-workflow/shot-segmentation/x"}})
    s3 = _wire(monkeypatch, lambda **kw: calls.append(kw) or STATUS, output=out)
    pc._dispatch_sfn_output_as_result("sess", "P1", "sg1", ARN, "now", job_item=JOB_ITEM)
    assert calls == []
    assert "outputs" not in s3.puts["plans/sess/P1/manifest.json"]["outputs"]["sg1"]["value"]


def test_on_demand_value_for_an_absorbed_job_also_merges(monkeypatch):
    _wire(monkeypatch, lambda **kw: STATUS, job_item=JOB_ITEM)
    value = pc._on_demand_job_value(ARN)
    assert value["outputs"][0]["uri"] == JSONL


def _injection(monkeypatch, value):
    class _Body:
        def __init__(self, b): self._b = b
        def read(self): return self._b

    class _S3:
        def get_object(self, Bucket, Key):
            return {"Body": _Body(json.dumps(
                {"outputs": {"sg3": {"status": "completed", "value": value}}}).encode())}

    class _Plan:
        def get_item(self, Key):
            return {"Item": {"result_s3_key": "plans/sess/P2/sg3/output", "status": "completed"}}

        def query(self, **kw):
            return {"Items": []}

    monkeypatch.setattr(pc, "_s3", _S3())
    monkeypatch.setattr(pc, "_plan_table", _Plan())
    monkeypatch.setattr(pc, "_session_rows", lambda sid: [])
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "skills-bkt")
    return pc._external_injection_text(
        {"id": "sg5", "external_inputs": [{"plan": "plan-content-moderation-analysis",
                                           "node": "sg3"}]}, session_id="sess")


def test_external_injection_lists_the_declared_jsonl(monkeypatch):
    merged = pc.result_resolution.merge_status_outputs(json.loads(SFN_OUTPUT), STATUS)
    text = _injection(monkeypatch, merged)
    assert "DELIVERABLE FILES" in text
    assert JSONL in text


def test_bare_uuid_value_has_no_deliverable_files(monkeypatch):
    # Pins what Phase 3 was handed before the fix: a uuid and nothing to read.
    text = _injection(monkeypatch, json.loads(SFN_OUTPUT))
    assert "DELIVERABLE FILES" not in text


def test_m2c_credentials_bundle_yields_its_client_secret():
    # media-workflow-gateway's registry row points client_secret_arn at the M2C JSON bundle; sending
    # the whole bundle as the client secret fails the token exchange, so the status call never ran.
    import gateway_invoker as gi

    class _Secrets:
        def __init__(self, s): self.s = s
        def get_secret_value(self, SecretId): return {"SecretString": self.s}
    bundle = json.dumps({"endpointUrl": "https://gw", "tokenEndpoint": "https://t", "clientId": "c",
                         "clientSecret": "s3cr3t", "scope": "x"})
    assert gi._resolve_client_secret(_Secrets(bundle), "arn") == "s3cr3t"
    assert gi._resolve_client_secret(_Secrets("raw-secret"), "arn") == "raw-secret"
