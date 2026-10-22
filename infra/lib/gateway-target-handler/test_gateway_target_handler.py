"""Unit tests for the gateway-target custom resource handler (no AWS, no network).

Run: `uv run --with pytest --with boto3 pytest infra/lib/gateway-target-handler/test_gateway_target_handler.py`

Covers the three deploy defects fixed on 2026-10-05:
  * the sync retry used `POST /targets/synchronize` (the real binding is `PUT /synchronizeTargets`);
  * an external MCP-server target (RuntimeId "") burned 180s waiting for a runtime that does not exist;
  * delete returned before the async target delete finished, so CloudFormation's gateway delete failed
    with "Gateway ... has targets associated with it".
"""

import importlib.util
import os
import sys

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
_spec = importlib.util.spec_from_file_location(
    "gateway_target_handler", os.path.join(os.path.dirname(__file__), "index.py")
)
handler_mod = importlib.util.module_from_spec(_spec)
sys.modules["gateway_target_handler"] = handler_mod
_spec.loader.exec_module(handler_mod)


class Recorder:
    def __init__(self, script):
        self.script = script  # list of (method_prefix, status, body)
        self.calls = []

    def __call__(self, method, url, body=None):
        self.calls.append((method, url, body))
        for i, (m, status, text) in enumerate(self.script):
            if m == method:
                self.script.pop(i)
                return handler_mod.SimpleResponse(status, text)
        return handler_mod.SimpleResponse(200, "{}")


def test_create_skips_runtime_wait_for_external_mcp_target(monkeypatch):
    waited = []
    monkeypatch.setattr(handler_mod, "wait_for_runtime", lambda rid, max_wait=180: waited.append(rid) or True)
    monkeypatch.setattr(handler_mod.time, "sleep", lambda s: None)
    rec = Recorder([
        ("POST", 201, '{"targetId": "T1"}'),
        ("GET", 200, '{"status": "READY"}'),
    ])
    monkeypatch.setattr(handler_mod, "signed_request", rec)
    out = handler_mod.handle_create({
        "GatewayId": "gw", "RuntimeId": "", "TargetName": "t", "Endpoint": "https://m2c/mcp",
        "CredProviderArn": "arn:cp", "Scopes": "[]",
    })
    assert out["PhysicalResourceId"] == "T1"
    assert waited == []  # no runtime to wait for


def test_create_retry_uses_put_synchronize_targets(monkeypatch):
    monkeypatch.setattr(handler_mod.time, "sleep", lambda s: None)
    rec = Recorder([
        ("POST", 201, '{"targetId": "T1"}'),
        ("GET", 200, '{"status": "SYNCHRONIZE_UNSUCCESSFUL"}'),
        ("GET", 200, '{"status": "READY"}'),
    ])
    monkeypatch.setattr(handler_mod, "signed_request", rec)
    handler_mod.handle_create({
        "GatewayId": "gw", "RuntimeId": "", "TargetName": "t", "Endpoint": "e",
        "CredProviderArn": "arn:cp", "Scopes": "[]",
    })
    syncs = [(m, u) for (m, u, _b) in rec.calls if "synchron" in u.lower()]
    assert syncs, "expected a sync retry"
    for m, u in syncs:
        assert m == "PUT"
        assert u.endswith("/gateways/gw/synchronizeTargets")


def test_update_in_place_uses_put_synchronize_targets(monkeypatch):
    monkeypatch.setattr(handler_mod.time, "sleep", lambda s: None)
    rec = Recorder([("GET", 200, '{"status": "READY"}')])
    monkeypatch.setattr(handler_mod, "signed_request", rec)
    props = {"GatewayId": "gw", "Endpoint": "e", "TargetName": "t", "Revision": "1"}
    handler_mod.handle_update({"PhysicalResourceId": "T1", "OldResourceProperties": dict(props)}, props)
    method, url, body = rec.calls[0]
    assert method == "PUT"
    assert url.endswith("/gateways/gw/synchronizeTargets")
    assert body == {"targetIdList": ["T1"]}


def test_delete_waits_until_target_is_gone(monkeypatch):
    rec = Recorder([
        ("DELETE", 202, "{}"),
        ("GET", 200, '{"status": "DELETING"}'),
        ("GET", 200, '{"status": "DELETING"}'),
        ("GET", 404, ""),
    ])
    monkeypatch.setattr(handler_mod, "signed_request", rec)
    monkeypatch.setattr(handler_mod.time, "sleep", lambda s: None)
    out = handler_mod.handle_delete({"PhysicalResourceId": "T1"}, {"GatewayId": "gw"})
    assert out == {"PhysicalResourceId": "T1"}
    gets = [c for c in rec.calls if c[0] == "GET"]
    assert len(gets) == 3  # kept polling until the 404


def test_wait_for_target_deleted_gives_up_after_max_wait(monkeypatch):
    monkeypatch.setattr(handler_mod, "signed_request", lambda m, u, b=None: handler_mod.SimpleResponse(200, "{}"))
    assert handler_mod.wait_for_target_deleted("us-west-2", "gw", "T1", max_wait=10, interval=5, sleep=lambda s: None) is False
