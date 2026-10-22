"""Tests for jobs.context.invoke_runtime_session_kwargs.

Sub-agents (task / render / planner) run in separate AgentCore runtimes. Before
this, dispatch invoked them without a ``runtimeSessionId``, so each sub-agent
got a fresh, uncorrelated runtime session and observability could not trace the
full main→sub-agent path. This helper propagates the chat session id (and, when
the SDK supports it, W3C trace context) so the whole path shares one session.

Also covers the ids that have to satisfy the AgentCore minimum for any of that to
happen — BOTH synthesized fallbacks, since either can be the id that reaches the
wire: ``main._synthesize_session_id`` (bound as the request session, and the one
that wins) and ``plan_and_execute_tool._new_plan_session_id``.
The plan chainer's half of the same defect is tested in
``infra/lib/lambdas/plan-chainer/test_node_session_propagation.py`` (distinct
basename deliberately: pytest cannot import two same-named test modules in one
run when neither directory is a package).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orchestration import plan_and_execute_tool as pae
from jobs import context as jobs_context


UUID_SESSION = "4eeaeb40-82b2-4bee-ab70-d3b62f316506"  # 36 chars, real chat id


def test_explicit_uuid_session_sets_runtime_session_id():
    kw = jobs_context.invoke_runtime_session_kwargs(client=None, session_id=UUID_SESSION)
    assert kw.get("runtimeSessionId") == UUID_SESSION
    # No client → no trace-context params, and no crash.
    assert "traceParent" not in kw


def test_short_session_id_is_skipped():
    # Anything under the AgentCore minimum is dropped rather than sent (it would
    # fail the whole invoke with ValidationException). The old synthesized
    # fallbacks — "session-20260730..." (28 chars, main.py) and
    # "plan-session-1786817632" (23) — both looked like this, which is how
    # propagation came to be skipped everywhere it was most needed.
    kw = jobs_context.invoke_runtime_session_kwargs(client=None, session_id="session-123")
    assert "runtimeSessionId" not in kw


def test_plan_session_fallback_id_qualifies_for_propagation():
    """The plan-session fallback id must be long enough to be propagated.

    ``plan-session-<epoch>`` was 23 chars — below AgentCore's minimum — so
    propagation was skipped in exactly the sessions with no chat session id to
    correlate to. The prefix stays (span_trajectory.PLAN_SESSION_PREFIX and the
    persist_workflow surface both depend on it); only the suffix grew.
    """
    sid = pae._new_plan_session_id()
    assert sid.startswith("plan-session-")
    assert len(sid) >= jobs_context._MIN_RUNTIME_SESSION_ID_LEN
    # And it actually reaches the wire now, which is the point.
    kw = jobs_context.invoke_runtime_session_kwargs(client=None, session_id=sid)
    assert kw.get("runtimeSessionId") == sid
    # Two plans in the same second must not collide on one session id.
    assert sid != pae._new_plan_session_id()


def test_synthesized_request_session_id_qualifies_for_propagation():
    """The OTHER short fallback, and the one that wins.

    ``main._synthesize_session_id`` is bound as the request session, so
    ``plan_and_execute``'s ``effective_session = chat_session_id or session_id``
    prefers it over the plan-session id — a 28-char value here therefore bypassed
    the lengthened plan-session id entirely and still skipped propagation, all the
    way down to the chainer's per-node dispatch.
    """
    import main  # heavy import; kept local to this test

    sid = main._synthesize_session_id()
    assert sid.startswith("session-")
    assert len(sid) >= jobs_context._MIN_RUNTIME_SESSION_ID_LEN
    kw = jobs_context.invoke_runtime_session_kwargs(client=None, session_id=sid)
    assert kw.get("runtimeSessionId") == sid
    assert sid != main._synthesize_session_id()


def test_request_session_id_wins_over_plan_session_id_and_is_long_enough():
    # Pins the precedence that made finding 1 a BYPASS rather than a gap: whichever
    # of the two ids reaches the chainer, it must clear the minimum.
    import main  # heavy import; kept local to this test

    for sid in (main._synthesize_session_id(), pae._new_plan_session_id()):
        assert len(sid) >= jobs_context._MIN_RUNTIME_SESSION_ID_LEN, sid


def test_falls_back_to_bound_context_session():
    tokens = jobs_context.set_request_context(actor_id="actor-x", session_id=UUID_SESSION)
    try:
        kw = jobs_context.invoke_runtime_session_kwargs(client=None)
        assert kw.get("runtimeSessionId") == UUID_SESSION
    finally:
        jobs_context.reset_request_context(tokens)


def test_unbound_context_yields_empty_without_raising():
    # No session bound and no override → current_session_id() raises internally,
    # helper must swallow and return no runtimeSessionId (graceful degrade).
    kw = jobs_context.invoke_runtime_session_kwargs(client=None)
    assert "runtimeSessionId" not in kw


class _FakeShape:
    def __init__(self, members):
        self.members = members


class _FakeOpModel:
    def __init__(self, members):
        self.input_shape = _FakeShape(members)


class _FakeServiceModel:
    def __init__(self, members):
        self._members = members

    def operation_model(self, name):
        assert name == "InvokeAgentRuntime"
        return _FakeOpModel(self._members)


class _FakeMeta:
    def __init__(self, members):
        self.service_model = _FakeServiceModel(members)


class _FakeClient:
    def __init__(self, members):
        self.meta = _FakeMeta(members)


def test_trace_params_gated_on_sdk_capability():
    # SDK that does NOT model trace members → never attempts to add them.
    client_no_trace = _FakeClient({"agentRuntimeArn": {}, "payload": {}})
    kw = jobs_context.invoke_runtime_session_kwargs(client=client_no_trace, session_id=UUID_SESSION)
    assert kw.get("runtimeSessionId") == UUID_SESSION
    assert "traceParent" not in kw and "traceState" not in kw and "baggage" not in kw

    # SDK that DOES model them, but no active OTEL span in the test → inject
    # yields an empty carrier, so still no trace params, and no crash.
    client_trace = _FakeClient({"traceParent": {}, "traceState": {}, "baggage": {}})
    kw2 = jobs_context.invoke_runtime_session_kwargs(client=client_trace, session_id=UUID_SESSION)
    assert kw2.get("runtimeSessionId") == UUID_SESSION
    # Trace-context params are intentionally NOT sent (they collide with OTEL's
    # post-signing header injection and break SigV4 on the runtime).
    assert "traceParent" not in kw2 and "traceState" not in kw2 and "baggage" not in kw2


class _RecordingClient:
    def __init__(self, fail_with_session: bool = False):
        self.calls: list = []
        self.fail_with_session = fail_with_session

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_with_session and "runtimeSessionId" in kwargs:
            raise RuntimeError("signature does not match")
        return {"response": b""}


def test_traced_invoke_passes_runtime_session_id():
    c = _RecordingClient()
    jobs_context.invoke_agent_runtime_traced(c, session_id=UUID_SESSION, agentRuntimeArn="arn", payload=b"{}")
    assert len(c.calls) == 1
    assert c.calls[0].get("runtimeSessionId") == UUID_SESSION


def test_traced_invoke_retries_without_session_on_failure():
    # Regression guard: if the session-scoped call fails (as the SigV4/trace
    # collision did), dispatch must still succeed via a retry WITHOUT the kwarg.
    c = _RecordingClient(fail_with_session=True)
    jobs_context.invoke_agent_runtime_traced(c, session_id=UUID_SESSION, agentRuntimeArn="arn", payload=b"{}")
    assert len(c.calls) == 2
    assert "runtimeSessionId" in c.calls[0]
    assert "runtimeSessionId" not in c.calls[1]


def test_traced_invoke_no_session_calls_once_plainly():
    c = _RecordingClient()
    jobs_context.invoke_agent_runtime_traced(c, session_id="short", agentRuntimeArn="arn", payload=b"{}")
    assert len(c.calls) == 1
    assert "runtimeSessionId" not in c.calls[0]


# ---------------------------------------------------------------------------
# Read timeout must NOT be retried — a retry dispatches the work twice.
#
# The propagation retry exists for one failure mode: AgentCore refusing the
# ``runtimeSessionId`` kwarg. Every member of that mode is rejected up front,
# before any work runs. A read timeout is the opposite evidence — the request was
# accepted and the sub-agent spent the client's whole read timeout (600s on the
# dispatch/render client, 900s on the plan client) working — so retrying it ran
# the work a SECOND time. On the render path, whose stream budget
# (RENDER_DISPATCH_BUDGET_SECONDS) is 90s, that rendered twice.
# ---------------------------------------------------------------------------

def _botocore_read_timeout():
    from botocore.exceptions import ReadTimeoutError

    return ReadTimeoutError(endpoint_url="https://bedrock-agentcore.us-west-2.amazonaws.com/")


def _urllib3_read_timeout():
    from urllib3.exceptions import ReadTimeoutError

    return ReadTimeoutError(None, "https://bedrock-agentcore.us-west-2.amazonaws.com/",
                            "read operation timed out")


class _RaisingClient:
    """Records every invoke and raises ``exc`` on the session-scoped one."""

    def __init__(self, exc: BaseException):
        self.calls: list = []
        self.exc = exc

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if "runtimeSessionId" in kwargs:
            raise self.exc
        return {"response": b""}


@pytest.mark.parametrize("make_exc", [_botocore_read_timeout, _urllib3_read_timeout],
                         ids=["botocore", "urllib3"])
def test_read_timeout_is_not_retried(make_exc):
    c = _RaisingClient(make_exc())
    with pytest.raises(Exception):
        jobs_context.invoke_agent_runtime_traced(
            c, session_id=UUID_SESSION, agentRuntimeArn="arn", payload=b"{}")
    # ONE dispatch, and it kept the session kwarg. Two calls here is the bug:
    # the second one runs the render/plan/task a second time.
    assert len(c.calls) == 1
    assert "runtimeSessionId" in c.calls[0]


def test_rejected_session_kwarg_still_retries_alongside_the_guard():
    """The helper's core guarantee, re-asserted next to the new guard.

    The read-timeout guard must not have narrowed this: a ValidationException on
    the id, or the SigV4 collision that once broke every dispatch, still has to
    fall back to an invoke WITHOUT the kwarg.
    """
    c = _RaisingClient(RuntimeError("ValidationException: runtimeSessionId"))
    jobs_context.invoke_agent_runtime_traced(
        c, session_id=UUID_SESSION, agentRuntimeArn="arn", payload=b"{}")
    assert len(c.calls) == 2
    assert "runtimeSessionId" in c.calls[0]
    assert "runtimeSessionId" not in c.calls[1]


def test_is_read_timeout_typed_beats_the_name():
    """Typed first is the point: a renamed subclass must still be recognised.

    The plan chainer's copy of this rule is string-only and would MISS this case.
    That is a subset, not a disagreement — both real classes are literally named
    ``ReadTimeoutError`` — but the typed arm is why this predicate can carry a
    correctness property rather than only a log line.
    """
    from botocore.exceptions import ReadTimeoutError as BotoReadTimeout

    class WrappedTimeout(BotoReadTimeout):  # deliberately NOT named *ReadTimeoutError*
        pass

    exc = WrappedTimeout(endpoint_url="https://x/")
    assert "ReadTimeoutError" not in type(exc).__name__  # the string arm alone fails here
    assert jobs_context._is_read_timeout(exc)


def test_is_read_timeout_string_fallback_matches_the_chainers_shapes():
    """Both string shapes the chainer matches on are still matched here.

    Keeps the two copies answering identically for anything either actually sees,
    so ``handler._is_read_timeout`` and this one stay in lock-step.
    """
    class ReadTimeoutError(Exception):  # neither botocore's nor urllib3's
        pass

    assert jobs_context._is_read_timeout(ReadTimeoutError("whatever"))  # by class name
    assert jobs_context._is_read_timeout(RuntimeError("read operation timed out"))  # by message
    assert not jobs_context._is_read_timeout(RuntimeError("ValidationException"))


def test_both_read_timeout_types_resolve_and_neither_covers_the_other():
    """Why the tuple has two entries rather than one.

    botocore's ReadTimeoutError inherits from the class in
    ``botocore.vendored.requests.packages.urllib3``, not from the installed
    urllib3 one, so an isinstance check against either alone misses the other.
    """
    from botocore.exceptions import ReadTimeoutError as BotoReadTimeout
    from urllib3.exceptions import ReadTimeoutError as Urllib3ReadTimeout

    assert BotoReadTimeout in jobs_context._READ_TIMEOUT_TYPES
    assert Urllib3ReadTimeout in jobs_context._READ_TIMEOUT_TYPES
    assert not issubclass(BotoReadTimeout, Urllib3ReadTimeout)


def test_is_read_timeout_degrades_to_strings_when_no_type_imports(monkeypatch):
    # Neither class importable (a slimmed runtime) → the string arm still works,
    # which is the chainer's behaviour exactly.
    monkeypatch.setattr(jobs_context, "_READ_TIMEOUT_TYPES", ())
    assert jobs_context._is_read_timeout(RuntimeError("read operation timed out"))
    assert not jobs_context._is_read_timeout(RuntimeError("ValidationException"))


# ---------------------------------------------------------------------------
# Per-call-site pinning. The guard lives in the shared helper and is UNIFORM, so
# all four agent-side dispatch seams get exactly one invoke on a read timeout —
# including the three that need the response back. Driven through the real call
# sites (fake boto3 client, real helper) because "the helper is right" is not the
# property that matters; "no call site dispatches twice" is.
# ---------------------------------------------------------------------------

def _bound_session():
    """Bind a request context so the session kwarg is actually attempted."""
    return jobs_context.set_request_context(actor_id="actor-x", session_id=UUID_SESSION)


def test_dispatch_tool_call_site_does_not_double_dispatch(monkeypatch):
    # dispatch_tool consumes the response stream to stream back to the user, so a
    # read timeout DOES cost it its answer. It is still not retried: the retry
    # could not have recovered the answer either (a second invoke needs another
    # full 600s read timeout to reply, long after the turn's budget). It degrades
    # to a tool-error result instead — see dispatch_tool.dispatch_tool's except.
    from orchestration import dispatch_tool

    c = _RaisingClient(_botocore_read_timeout())
    monkeypatch.setattr(dispatch_tool, "_get_client", lambda: c)
    tokens = _bound_session()
    try:
        with pytest.raises(Exception):
            dispatch_tool._invoke_runtime_stream("arn:aws:bedrock-agentcore:::runtime/task",
                                                 {"prompt": "x", "session_id": UUID_SESSION})
    finally:
        jobs_context.reset_request_context(tokens)
    assert len(c.calls) == 1


def test_plan_and_execute_call_site_does_not_double_dispatch(monkeypatch):
    # Planning / Design are request-response: a read timeout means no plan. The
    # planning and design callers each catch it and return "…Agent failed: …",
    # which is a legible dead end rather than a second 900s planning run.
    c = _RaisingClient(_urllib3_read_timeout())
    monkeypatch.setattr(pae, "_get_client", lambda: c)
    tokens = _bound_session()
    try:
        with pytest.raises(Exception):
            pae._invoke_runtime("arn:aws:bedrock-agentcore:::runtime/planning",
                                {"goal": "x", "session_id": UUID_SESSION})
    finally:
        jobs_context.reset_request_context(tokens)
    assert len(c.calls) == 1


def test_render_call_site_does_not_render_twice(monkeypatch):
    # The costliest site: RENDER_DISPATCH_BUDGET_SECONDS abandons the stream at
    # 90s, so a retried read timeout rendered the same artifacts a second time.
    import render.artifacts as render_artifacts

    c = _RaisingClient(_botocore_read_timeout())
    monkeypatch.setattr(render_artifacts, "_get_client", lambda: c)
    monkeypatch.setattr(
        render_artifacts, "RENDER_AGENT_ARN",
        "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/render-TEST")
    tokens = _bound_session()
    try:
        with pytest.raises(Exception):
            render_artifacts.dispatch_render(["https://cdn.example/artifacts/x/output/report.pdf"])
    finally:
        jobs_context.reset_request_context(tokens)
        render_artifacts._rendered_url_sets.clear()
    assert len(c.calls) == 1


def test_hook_dispatch_action_does_not_double_dispatch(monkeypatch):
    # Synchronous dispatch from inside a hook callback. The hook engine's fire()
    # already records a raised exception as an error ActionResult without breaking
    # the turn, so re-raising here costs nothing and avoids a second sub-agent run
    # from a hook that is explicitly warned about recursive fan-out.
    from orchestration import dispatch_tool
    from hooks.actions import dispatch_action

    c = _RaisingClient(_urllib3_read_timeout())
    monkeypatch.setattr(dispatch_tool, "_get_client", lambda: c)
    monkeypatch.setattr(dispatch_tool, "TASK_AGENT_ARN",
                        "arn:aws:bedrock-agentcore:::runtime/task")
    tokens = _bound_session()
    try:
        with pytest.raises(Exception):
            dispatch_action({"prompt": "summarise this"}, {"event": "after_tool"})
    finally:
        jobs_context.reset_request_context(tokens)
    assert len(c.calls) == 1


def test_hook_engine_turns_the_raised_read_timeout_into_an_error_result(monkeypatch):
    """The hook site's degradation, pinned: raised != turn broken.

    ``HookEngine.fire`` is documented "Never raises", so the re-raise surfaces as
    an error ActionResult. This is what makes the uniform guard safe at the one
    call site that has no try/except of its own around the invoke.
    """
    from orchestration import dispatch_tool
    from hooks.engine import HookEngine, HookSpec

    c = _RaisingClient(_botocore_read_timeout())
    monkeypatch.setattr(dispatch_tool, "_get_client", lambda: c)
    monkeypatch.setattr(dispatch_tool, "TASK_AGENT_ARN",
                        "arn:aws:bedrock-agentcore:::runtime/task")
    engine = HookEngine([
        HookSpec(name="h", event="after_tool", condition="", action_type="dispatch",
                 action_params={"prompt": "summarise this"}),
    ])
    tokens = _bound_session()
    try:
        results = engine.fire("after_tool", {})
    finally:
        jobs_context.reset_request_context(tokens)
    assert [r.status for r in results] == ["error"]
    assert len(c.calls) == 1
