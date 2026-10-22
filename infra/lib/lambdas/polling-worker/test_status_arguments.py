"""Status-call arguments must match the tool being invoked.

Every fal poll failed validation, and the recorded error named exactly why:

    Missing required field(s): 'endpoint_id'
    Missing required field(s): 'request_id'
    property '__actor_id' is not defined in the schema
    property 'job_id' is not defined in the schema

So the poller was sending {job_id, __actor_id} to a tool that requires
{request_id, endpoint_id} and forbids additional properties. These tests pin both
shapes so neither can regress into the other.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

os.environ.setdefault("JOB_REGISTRY_TABLE", "t")
os.environ.setdefault("GATEWAY_REGISTRY_TABLE", "g")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

# Loaded by path under a unique name: this Lambda and the plan chainer both define
# modules called handler and gateway_invoker, so a sys.path insert lets whichever
# imports first shadow the other.
_spec = importlib.util.spec_from_file_location(
    "pw_gateway_invoker", Path(__file__).resolve().parent / "gateway_invoker.py"
)
gi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gi)


def test_queue_tool_gets_request_id_and_endpoint_id():
    args = gi._status_arguments(
        status_tool="usr-abc___check_job", job_id="req-1", actor_id="a1",
        endpoint_id="fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
    )
    assert args == {
        "request_id": "req-1",
        "endpoint_id": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
    }


def test_queue_tool_never_sends_the_forbidden_fields():
    # The schema forbids additionalProperties, so job_id and __actor_id are rejected
    # outright -- sending either fails the whole call.
    args = gi._status_arguments(
        status_tool="usr-abc___check_job", job_id="req-1", actor_id="a1", endpoint_id="e"
    )
    assert "__actor_id" not in args
    assert "job_id" not in args


def test_queue_tool_omits_endpoint_id_when_unknown():
    # Better to send a call missing an optional-looking field and get a clear
    # validation error than to send an empty string the server may accept as a real
    # endpoint name.
    args = gi._status_arguments(
        status_tool="usr-abc___check_job", job_id="req-1", actor_id="a1"
    )
    assert args == {"request_id": "req-1"}


def test_trio_tool_shape_is_unchanged():
    # R11.3: a normal trio status tool still receives the asserted actor so the
    # gateway's downstream authorization does not see the Lambda's own role.
    args = gi._status_arguments(
        status_tool="media-processing___get_media_status", job_id="j1", actor_id="a1"
    )
    assert args == {"job_id": "j1", "__actor_id": "a1"}


def test_bare_name_is_matched_through_two_separators():
    # Tool names can carry TWO ___ separators (target___aws___call_aws).
    args = gi._status_arguments(
        status_tool="t___aws___check_job", job_id="r", actor_id="a", endpoint_id="e"
    )
    assert args == {"request_id": "r", "endpoint_id": "e"}


def test_m2c_shared_status_tool_keeps_the_trio_shape():
    """get_job_status is M2C's OWN status tool, not a queue tool.

    M2C workflows are shared-status trios: a submit tool plus one shared
    get_job_status, with no *_results tool. Listing that name as a queue tool sent
    them {request_id} instead of {job_id, __actor_id}. It was masked only because
    M2C job ids are Step Functions ARNs, so handler._invoke_status short-circuits
    to DescribeExecution before reaching here -- a branch taken for an unrelated
    reason, which is not something to depend on.
    """
    args = gi._status_arguments(
        status_tool="media-processing___get_job_status", job_id="j1", actor_id="a1"
    )
    assert args == {"job_id": "j1", "__actor_id": "a1"}


def test_generic_names_take_the_queue_shape_only_with_an_endpoint():
    # An endpointId is positive evidence of a queue job: only the queue detector
    # records one, and a queue status call requires it. Without it there is nothing
    # separating these from a trio tool.
    for name in ("get_job_status", "job_status"):
        with_endpoint = gi._status_arguments(
            status_tool=f"usr-x___{name}", job_id="r", actor_id="a", endpoint_id="e"
        )
        assert with_endpoint == {"request_id": "r", "endpoint_id": "e"}, name

        without_endpoint = gi._status_arguments(
            status_tool=f"usr-x___{name}", job_id="r", actor_id="a"
        )
        assert without_endpoint == {"job_id": "r", "__actor_id": "a"}, name


def test_fal_check_job_needs_no_endpoint_to_be_recognised():
    # check_job is fal's own name and is used by nothing else here, so it is matched
    # unconditionally -- and the resulting error names the missing endpoint_id, which
    # is more diagnostic than silently falling back to a shape fal rejects anyway.
    args = gi._status_arguments(
        status_tool="usr-abc___check_job", job_id="req-1", actor_id="a1"
    )
    assert args == {"request_id": "req-1"}
