"""The Plan Chainer never carries a Project_Token and never logs a secret payload value.

media-harness-projects-access, design Component 7 "Dispatch and plans carry no token" (Req 4.18):
node payloads are built from the plan record, so even a node definition or a node-state row carrying a
stray ``project_token`` must not put one on the Task Agent invoke; and the payload debug line goes
through ``_scrub_payload`` so the delegation grant never reaches a log.
"""
from __future__ import annotations

import json
import logging
import os
import sys

os.environ.setdefault("PLAN_STATE_TABLE", "test-plan-state")
os.environ.setdefault("JOB_REGISTRY_TABLE", "test-job-registry")
os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")
os.environ.setdefault("SKILLS_S3_BUCKET", "")

sys.path.insert(0, os.path.dirname(__file__))

import handler  # noqa: E402
from test_dispatch_node_grant import _install  # noqa: E402

SENTINEL = "eyJhbGciOiJSUzI1NiJ9.SENTINEL-PROJECT-TOKEN.sig"
GRANT = "signed.grant.SENTINEL-GRANT"


def test_secret_payload_keys():
    assert handler.SECRET_PAYLOAD_KEYS == {"project_token", "delegation_grant", "user_token", "search_dispatch"}


def test_scrub_payload_redacts_values_and_keeps_names():
    out = handler._scrub_payload({"prompt": "x" * 200, "delegation_grant": GRANT, "user_token": "u",
                                  "project_token": SENTINEL, "search_dispatch": {"nonce": "n"}, "tools": ["a"]})
    assert out["delegation_grant"] == out["user_token"] == out["project_token"] == out["search_dispatch"] \
        == "<redacted>"
    assert out["tools"] == "<1 items>" and len(out["prompt"]) < 100
    assert SENTINEL not in json.dumps(out) and GRANT not in json.dumps(out)


def test_dispatch_node_never_forwards_project_token_and_logs_scrubbed(monkeypatch, caplog):
    fake = _install(monkeypatch)
    node = {"id": "n1", "prompt": "do it", "tools": ["t"], "depends_on": [], "project_token": SENTINEL}
    with caplog.at_level(logging.DEBUG):
        handler._dispatch_node("sess-1", "plan-1", "USER#alice", dict(node), [dict(node)],
                               {"n1": {"project_token": SENTINEL}}, turn_id="turn-1", delegation_grant=GRANT)
    assert len(fake.payloads) == 1
    sent = fake.payloads[0]
    assert "project_token" not in sent and SENTINEL not in json.dumps(sent)
    assert sent["delegation_grant"] == GRANT  # still presented to the Task Agent
    assert GRANT not in caplog.text and SENTINEL not in caplog.text
    assert "delegation_grant" in caplog.text  # the debug line names the key
