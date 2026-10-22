"""A plan node whose EVERY tool call was rejected has not completed, whatever its prose says.

Staging session 0d71f16b (guided moderation, 3 videos): each Phase 1 node was handed an asset id
instead of an s3:// uri. ``shot-segmentation___segment_shots`` rejected every call — inside a
toolResult whose Strands status was ``"success"``:

    {"error": true, "errorCode": "INVALID_PARAMETER",
     "message": "asset_s3uri must be a valid S3 URI (s3://bucket/key)"}

The node replied in prose ("ERROR: The job could not be submitted ..."). It was a TEXT node, so it
had no JSON block to declare ``{"error": ...}`` in, and all six were stored ``completed``; the plan
SUCCEEDED and its result carried ``s3://bucket/key`` scraped from the error message.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
REJECTED = json.dumps({"error": True, "errorCode": "INVALID_PARAMETER",
                       "message": "asset_s3uri must be a valid S3 URI (s3://bucket/key)"})
SUBMITTED = json.dumps({"executionArn": "arn:aws:states:us-west-2:1:execution:m2c-shot:abc",
                        "status": "RUNNING"})


def _use(tid):
    return {"role": "assistant", "content": [{"toolUse": {"toolUseId": tid, "name": "segment_shots",
                                                          "input": {"asset_s3uri": "video:06dcaa9a"}}}]}


def _result(tid, text, status="success"):
    return {"role": "user", "content": [{"toolResult": {"toolUseId": tid, "status": status,
                                                        "content": [{"text": text}]}}]}


def test_every_rejected_call_fails_the_node_with_the_tool_s_reason():
    from task_agent import _all_tool_calls_failed

    msgs = [{"role": "user", "content": [{"text": "submit"}]},
            _use("t1"), _result("t1", REJECTED), _use("t2"), _result("t2", REJECTED),
            {"role": "assistant", "content": [{"text": "ERROR: The job could not be submitted."}]}]
    why = _all_tool_calls_failed(msgs)
    assert why.startswith("all 2 tool call(s) returned an error: INVALID_PARAMETER")
    assert "asset_s3uri must be a valid S3 URI" in why
    assert why.count("INVALID_PARAMETER") == 1  # repeated identical errors are named once


def test_one_successful_call_leaves_the_node_alone():
    from task_agent import _all_tool_calls_failed

    msgs = [_use("t1"), _result("t1", REJECTED), _use("t2"), _result("t2", SUBMITTED)]
    assert _all_tool_calls_failed(msgs) == ""


def test_no_tool_calls_is_not_this_check_s_business():
    from task_agent import _all_tool_calls_failed

    assert _all_tool_calls_failed([{"role": "assistant", "content": [{"text": "a summary"}]}]) == ""
    assert _all_tool_calls_failed(None) == ""


def test_a_strands_error_status_counts_too():
    from task_agent import _all_tool_calls_failed

    msgs = [_use("t1"), _result("t1", "Tool raised: timeout", status="error")]
    assert "Tool raised: timeout" in _all_tool_calls_failed(msgs)


def test_a_result_that_merely_mentions_error_is_not_an_error():
    from task_agent import _all_tool_calls_failed

    ok = json.dumps({"status": "SUCCEEDED", "error": None, "errors": [], "note": "no error"})
    assert _all_tool_calls_failed([_use("t1"), _result("t1", ok)]) == ""


def test_execute_plan_node_applies_it_as_a_failure_note():
    body = (SRC / "task_agent.py").read_text()
    body = body[body.index("async def _execute_plan_node"):body.index("async def _fetch_plan_node_results")]
    i = body.index("_all_tool_calls_failed(")
    tail = body[i:i + 600]
    assert 'status = "failed"' in tail and "failure_notes.append(" in tail
    # Before the async-job detection, so a node with no job never parks in "processing".
    assert i < body.index("sfn_arn_match = re.search(")
