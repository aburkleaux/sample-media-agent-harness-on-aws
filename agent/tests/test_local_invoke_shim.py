"""Local E2E harness: the single inter-runtime seam (invoke_agent_runtime_traced) routes to a
LOCAL runtime when LOCAL_AGENT_ENDPOINTS maps the target ARN, and is a NO-OP (boto3 path) when
the env is unset — so nothing changes in a deployed environment.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jobs import context as jc  # noqa: E402


class _FakeClient:
    def __init__(self):
        self.calls = 0

    def invoke_agent_runtime(self, **kw):
        self.calls += 1
        return {"response": b"boto3-path"}


def test_endpoint_unset_uses_boto3(monkeypatch):
    monkeypatch.delenv("LOCAL_AGENT_ENDPOINTS", raising=False)
    assert jc._local_agent_endpoint("arn:aws:...:runtime/render") is None
    c = _FakeClient()
    out = jc.invoke_agent_runtime_traced(c, session_id="s", agentRuntimeArn="arn:aws:...:runtime/render",
                                         payload=b"{}", contentType="application/json")
    assert c.calls == 1 and out["response"] == b"boto3-path"   # deployed path unchanged


def test_endpoint_mapped_routes_local(monkeypatch):
    arn = "arn:aws:bedrock-agentcore:us-west-2:1:runtime/render"
    monkeypatch.setenv("LOCAL_AGENT_ENDPOINTS", json.dumps({arn: "http://localhost:8082"}))
    assert jc._local_agent_endpoint(arn) == "http://localhost:8082"
    routed = {}
    monkeypatch.setattr(jc, "_invoke_agent_runtime_local",
                        lambda base, kw: routed.update(base=base, kw=kw) or {"response": b"local"})
    c = _FakeClient()
    out = jc.invoke_agent_runtime_traced(c, session_id="s", agentRuntimeArn=arn,
                                         payload=b'{"x":1}', contentType="application/json")
    assert c.calls == 0                                        # boto3 NOT used
    assert routed["base"] == "http://localhost:8082"
    assert out["response"] == b"local"


def test_other_arn_not_mapped_still_boto3(monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_ENDPOINTS", json.dumps({"arn:render": "http://localhost:8082"}))
    c = _FakeClient()
    jc.invoke_agent_runtime_traced(c, session_id="s", agentRuntimeArn="arn:task", payload=b"{}")
    assert c.calls == 1                                        # unmapped arn -> boto3
