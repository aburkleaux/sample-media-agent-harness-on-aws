"""Regression: a gateway MCP transport/session failure (the client session is not running)
must NOT be retried by the chainer — it is deterministic infra, so re-dispatch just re-hits the
same dead session and loops (observed: plan 0e134284 fal Kling/MiniMax submit nodes churned
failed->running every ~1-2s for >1h). It must fail fast to terminal, exactly like a content-policy
block, regardless of the retries counter or the (varying) error-signature.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE", "SKILLS_S3_BUCKET"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_mcp", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_mcp"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()

# The real error text seen in the incident — note the varying LLM-reasoning prefix that defeated
# the error-signature stop, which is why the cap+signature guard did NOT terminate the loop.
REAL_ERR_1 = ("I'll retry the submit_job call now.The submit_job call failed again with the same "
              "MCP client transport-layer error (the client session is not running, "
              "MCPClientInitializationError).")
REAL_ERR_2 = ("I'll retry, starting with the schema check.The schema call failed with the same "
              "MCPClientInitializationError: the client session is not running.")


def test_is_mcp_transport_error_matches_real_strings():
    assert pc._is_mcp_transport_error(REAL_ERR_1) is True
    assert pc._is_mcp_transport_error(REAL_ERR_2) is True
    assert pc._is_mcp_transport_error("some wrong parameter value") is False
    assert pc._is_mcp_transport_error("") is False


def test_mcp_transport_error_fails_fast_even_at_retries_zero():
    # retries=0, no last_retry_error → a normal error WOULD be retried; an MCP transport error
    # must NOT be (this is exactly the loop condition that ran for >1h).
    row = {"error": REAL_ERR_1, "retries": 0}
    assert pc._should_retry_node(row) is False

    # And with a DIFFERENT-worded repeat (varying prefix) — the signature stop would miss it, but
    # the transport fail-fast still holds.
    row2 = {"error": REAL_ERR_2, "retries": 0, "last_retry_error": REAL_ERR_1}
    assert pc._should_retry_node(row2) is False


def test_normal_correctable_error_still_retries():
    # Guard against over-matching: an ordinary correctable parameter error is still retried once.
    row = {"error": "invalid value for field 'duration': expected integer", "retries": 0}
    assert pc._should_retry_node(row) is True
