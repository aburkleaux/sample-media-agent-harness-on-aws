"""Task 2 — safe expression evaluator tests.

Covers correct matching semantics AND explicit rejection of unsafe syntax,
since this is the security-sensitive piece of the hooks feature.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hooks.expr import ExpressionError, evaluate, validate  # noqa: E402


CTX = {
    "event": "after_tool_result",
    "tool": {"name": "mediaworks-harness-speech-diarization___get_diarization_results", "role": "results"},
    "result": {"urls": ["https://cf/x.wav"], "mimes": ["audio/wav"], "new_artifacts": ["https://cf/x.wav"]},
    "job": {"status": "SUCCEEDED"},
    "session": {"id": "s-1"},
    "actor": {"id": "local-dev-user"},
}


# ---- matching semantics -----------------------------------------------------

def test_empty_condition_is_true():
    assert evaluate("", CTX) is True
    assert evaluate("   ", CTX) is True


def test_flagship_render_on_results_condition():
    expr = 'event == "after_tool_result" and tool.role == "results" and result.urls'
    assert evaluate(expr, CTX) is True


def test_render_hook_gates_status_role_on_job_status():
    """The render-artifacts hook only fires for status-role tools when
    job_status == SUCCEEDED — not on every intermediate RUNNING poll."""
    from hooks.defaults import default_hook_specs
    condition = default_hook_specs()[0].condition

    # status-role + SUCCEEDED → fires
    ctx_succeeded = {
        "event": "after_tool_result",
        "tool": {"name": "audio-stem___get_job_status", "role": "status"},
        "result": {"urls": ["s3://b/a.wav"], "mimes": ["audio/wav"],
            "new_artifacts": ["s3://b/a.wav"], "job_status": "SUCCEEDED"},
        "run_id": "",
    }
    assert evaluate(condition, ctx_succeeded) is True

    # status-role + RUNNING → does NOT fire (the loop fix)
    ctx_running = {
        "event": "after_tool_result",
        "tool": {"name": "audio-stem___get_job_status", "role": "status"},
        "result": {"urls": ["s3://b/a.wav"], "mimes": ["audio/wav"],
            "new_artifacts": ["s3://b/a.wav"], "job_status": "RUNNING"},
        "run_id": "",
    }
    assert evaluate(condition, ctx_running) is False

    # status-role + no mimes → does NOT fire
    ctx_no_mimes = {
        "event": "after_tool_result",
        "tool": {"name": "audio-stem___get_job_status", "role": "status"},
        "result": {"urls": [], "mimes": [], "job_status": "SUCCEEDED", "new_artifacts": []},
        "run_id": "",
    }
    assert evaluate(condition, ctx_no_mimes) is False

    # SUCCEEDED but part of a plan run (run_id set) → suppressed (plan nodes
    # are rendered by the Plan Chainer, not the auto-render hook).
    ctx_plan_node = {
        "event": "after_tool_result",
        "tool": {"name": "audio-stem___get_job_status", "role": "status"},
        "result": {"urls": ["s3://b/a.wav"], "mimes": ["audio/wav"],
            "new_artifacts": ["s3://b/a.wav"], "job_status": "SUCCEEDED"},
        "run_id": "plan-123",
    }
    assert evaluate(condition, ctx_plan_node) is False

    # results-role (legacy) → fires regardless of job_status
    ctx_results_role = {
        "event": "after_tool_result",
        "tool": {"name": "x___get_results", "role": "results"},
        "result": {"urls": ["https://cf/x.wav"], "mimes": ["audio/wav"], "job_status": None, "new_artifacts": ["https://cf/x.wav"]},
        "run_id": "",
    }
    assert evaluate(condition, ctx_results_role) is True

    # artifact tool → fires regardless of job_status
    ctx_artifact = {
        "event": "after_tool_result",
        "tool": {"name": "artifact", "role": "other"},
        "result": {"urls": ["https://cf/x.mp4"], "mimes": ["video/mp4"], "job_status": None, "new_artifacts": ["https://cf/x.mp4"]},
        "run_id": "",
    }
    assert evaluate(condition, ctx_artifact) is True


def test_condition_false_when_role_differs():
    ctx = {**CTX, "tool": {"name": "x", "role": "start"}}
    expr = 'tool.role == "results" and result.urls'
    assert evaluate(expr, ctx) is False


def test_condition_false_when_no_urls():
    ctx = {**CTX, "result": {"urls": [], "mimes": [], "new_artifacts": []}}
    assert evaluate("result.urls", ctx) is False


def test_in_operator():
    assert evaluate('job.status in ["SUCCEEDED", "FAILED"]', CTX) is True
    assert evaluate('job.status in ["RUNNING"]', CTX) is False


def test_matches_function():
    assert evaluate('matches(tool.name, "diariz")', CTX) is True
    assert evaluate('matches(tool.name, "^nope$")', CTX) is False


def test_not_and_comparisons():
    assert evaluate('not (job.status == "RUNNING")', CTX) is True
    assert evaluate('len(result.urls) >= 1', CTX) is True


def test_missing_attribute_is_none_not_error():
    # result.nonexistent → None → falsy, no crash
    assert evaluate("result.nonexistent", CTX) is False
    assert evaluate("nonexistent.deep.path", CTX) is False


# ---- safety: reject dangerous syntax ---------------------------------------

@pytest.mark.parametrize("bad", [
    "__import__('os').system('echo hi')",
    "().__class__.__bases__",
    "result.__class__",
    "open('/etc/passwd')",
    "(lambda: 1)()",
    "[x for x in result.urls]",
    "result.urls[0]",              # subscript not allowed
    "1 + 1",                        # arithmetic not allowed
    "exec('x=1')",
    "getattr(result, 'urls')",     # non-whitelisted call
    "tool.name.upper()",           # method call on attribute (func is Attribute, not Name)
])
def test_rejects_unsafe(bad):
    with pytest.raises(ExpressionError):
        validate(bad)
    with pytest.raises(ExpressionError):
        evaluate(bad, CTX)


def test_validate_accepts_good_expressions():
    validate('tool.role == "results" and result.urls')
    validate('matches(tool.name, "diariz") or job.status in ["SUCCEEDED"]')
    validate("")  # empty ok


def test_syntax_error_raises_expression_error():
    with pytest.raises(ExpressionError):
        validate('tool.role == ')
