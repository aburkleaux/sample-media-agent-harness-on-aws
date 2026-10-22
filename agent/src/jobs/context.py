"""Per-request context binding for the per-tool job-tracking wrap.

The wrap's actor/session resolvers (R3.1) close over the per-request
identity that ``main.py`` resolves from the AgentCore context +
JWT-stamped payload. Because Strands tool calls run in the same
Python ``contextvars`` context as the request handler, binding
actor/session ids on a contextvar gives every tool dispatch in the
same request a way to read them without threading callable
arguments through the entire stack.

Mirrors the existing ``main_lab_tools.set_current_lab_service``
pattern. The contextvars default to ``None`` so a tool dispatch that
runs outside a request (e.g. a unit test, or a startup hook invoking
a tool before the entrypoint binds context) sees a clean "unset"
signal — the resolvers raise :class:`RuntimeError` rather than fall
back silently, so misuse surfaces immediately. The
:class:`~agent.src.jobs.strands_tool_wrap.JobTrackingMCPAgentTool`
catches that exception in its own defensive ``try/except`` so a
missing binding declines registration / gating rather than crashing
the underlying tool call.

Requirements: 3.1
"""
from __future__ import annotations

import contextvars
import logging
from typing import Optional, Tuple

_logger = logging.getLogger(__name__)

# Module-level contextvars. Default ``None`` so :func:`current_actor_id`
# / :func:`current_session_id` can distinguish "not yet bound" from
# "bound to an empty string" — only the former triggers the loud
# RuntimeError; the latter is treated by the resolver as still-unset
# because empty strings are never legal actor/session ids.
_current_actor_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "jobs.current_actor_id", default=None
)
_current_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "jobs.current_session_id", default=None
)
_current_run_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "jobs.current_run_id", default=None
)
# The user request (one agent invocation) that caused this work. Distinct from
# run_id: run_id identifies a PLAN execution and is only bound while a DAG runs,
# so a standalone dispatch has none. turn_id is bound for every invocation, which
# is what lets "show me the results of the thing I just asked for" scope to one
# request instead of the whole session — a session accumulates several unrelated
# intents, and grouping by it drags all of them into one retrieval.
_current_turn_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "jobs.current_turn_id", default=None
)

# Token pair returned by :func:`set_request_context`. Aliased so call
# sites can declare a typed local without importing the underlying
# ``contextvars.Token`` directly.
RequestContextTokens = Tuple[contextvars.Token, contextvars.Token]


def set_request_context(
    *,
    actor_id: str,
    session_id: str,
) -> RequestContextTokens:
    """Bind the (actor_id, session_id) pair for the current request.

    Returns a pair of reset tokens so :func:`reset_request_context`
    can restore the prior values when the request finishes. Tokens
    are returned in registration order (actor, session) and the
    reset helper releases them in reverse order to mirror "exit
    context" semantics.

    Args:
        actor_id: The validated Actor identifier (Cognito ``sub`` claim
            or proxy-stamped ``__actor_id``). Must be a non-empty
            string; callers that can't resolve an actor MUST NOT
            invoke this function — leave the binding unset so the
            wrap's own defensive path declines gracefully.
        session_id: The chat session id (frontend UUID or synthesised
            fallback). Empty strings are accepted by the contextvar
            but :func:`current_session_id` treats them as unset.
    """
    actor_token = _current_actor_id.set(actor_id)
    session_token = _current_session_id.set(session_id)
    return actor_token, session_token


def reset_request_context(tokens: RequestContextTokens) -> None:
    """Reset both contextvars to their pre-request state.

    Reset order is the reverse of registration so the contextvars
    behave like a properly-nested context manager. Callers that wrap
    this in a try/except can safely swallow exceptions raised by the
    underlying primitive (e.g. if a token is reused twice).
    """
    actor_token, session_token = tokens
    _current_session_id.reset(session_token)
    _current_actor_id.reset(actor_token)


_current_user_token: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user_token", default=""
)


def set_user_token(token: str) -> None:
    """Bind the caller's forwarded JWT for the duration of this invocation.

    Kept separate from the actor id because it is a CREDENTIAL, not an identity:
    the actor id is safe to log and persist, this is not. Nothing writes it to a
    payload log, a job record or the registry.
    """
    _current_user_token.set(token or "")


def current_user_token() -> str:
    """Return the forwarded token, or "" when there is none.

    Returns empty rather than raising, unlike current_actor_id: a missing token
    is a normal condition (management calls carry none) and callers degrade by
    skipping the user_jwt gateway rather than failing.
    """
    return _current_user_token.get()


_current_model_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_model_id", default=""
)


def set_model_id(model_id: str) -> None:
    """Bind the model resolved for THIS turn, so sub-agents can inherit it.

    The user's model selection reaches the orchestrator only. A sub-agent's model
    arrives as an LLM-chosen ``dispatch_tool`` argument picked off the catalogue
    in the system prompt, and nothing told the model to prefer the session's
    choice -- so selecting Sonnet 5 could, and did, dispatch the actual work to
    claude-sonnet-4-6. Config the user set should propagate by construction, not
    depend on the model restating it correctly.
    """
    _current_model_id.set(model_id or "")


def current_model_id() -> str:
    """Return this turn's resolved model id, or "" if none was bound.

    Empty rather than raising: management calls and tests run without a bound
    turn, and callers treat "" as "let the callee choose its own default".
    """
    return _current_model_id.get()


# Active skill DEFINITIONS for THIS turn, so a dispatched sub-agent inherits the
# skills the actor is actually running with. Same failure mode as model_id: the
# actor's skills reached the orchestrator's per-request skills plugin, but a
# sub-agent's skills arrived only as an LLM-filled ``dispatch_tool`` argument the
# model left empty -- observed as ``Dispatching to task-agent: 1 tools, 0
# skills`` while that same turn narrated using the aws-research skill. Config the
# actor has must propagate by construction, not depend on the model
# re-serializing whole skill bodies (which it does not).
_current_active_skills: contextvars.ContextVar[list] = contextvars.ContextVar(
    "current_active_skills", default=[]
)


def set_active_skills(skills: list) -> None:
    """Bind this turn's active skill definitions for sub-agent inheritance.

    Stores full definition dicts (``{name, description, instructions}``), NOT
    names, on purpose. By-reference (forward names, let the sub-agent load them)
    is infeasible here: the deployed Task Agent builds ``Skill`` objects straight
    from ``payload["skills"]`` dicts and has no per-actor name->definition
    resolver on the internal dispatch path (its only name-based load,
    ``task_agent._fetch_external_custom_agent_config``, is scoped to published
    Custom Agents on the external invoke path and keyed on the owner, not an
    arbitrary actor's active/owned skills). Bare name strings would hit that
    runtime's ``isinstance(s, dict)`` guard and be silently dropped, so the
    orchestrator -- which already holds the resolved defs in its request skills
    plugin, including owned drafts the global store lacks -- forwards the defs.
    """
    _current_active_skills.set(list(skills) if skills else [])


def current_active_skills() -> list:
    """Return this turn's active skill definitions, or ``[]`` if none was bound.

    Empty rather than raising, like :func:`current_model_id`: management calls
    and tests run with no turn bound, and callers treat ``[]`` as "inherit
    nothing" so a dispatch with nothing bound sends no skills.
    """
    return _current_active_skills.get()


def current_actor_id() -> str:
    """Return the bound actor id, or raise if unset.

    Used by the per-tool job-tracking wrap's actor resolver. The
    "raise on unset" contract mirrors the analogous helper in
    ``main_lab_tools._service`` — failing loud is better than binding
    a placeholder identity. The wrap catches the exception in its
    own defensive ``try/except`` and declines registration / gating
    cleanly.
    """
    actor_id = _current_actor_id.get()
    if not isinstance(actor_id, str) or not actor_id:
        raise RuntimeError(
            "No actor_id bound for the current request. "
            "Call jobs.context.set_request_context before invoking tools."
        )
    return actor_id


def current_session_id() -> str:
    """Return the bound session id, or raise if unset.

    Symmetric with :func:`current_actor_id`. Empty strings are
    treated as unset because no legal session id is empty, and a
    loud failure is preferable to recording an empty ``sessionId``
    on a job record.
    """
    session_id = _current_session_id.get()
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError(
            "No session_id bound for the current request. "
            "Call jobs.context.set_request_context before invoking tools."
        )
    return session_id


# Minimum length AgentCore enforces on runtimeSessionId. Chat session ids are
# UUIDs (36 chars) and comfortably qualify; anything shorter is skipped rather
# than sent, because it trips a ValidationException and would fail the invoke
# outright.
#
# TWO synthesized fallback ids used to sit under that minimum, so propagation was
# silently skipped in exactly the sessions with no chat session id to correlate
# to. Both are now minted long enough to qualify, and BOTH must stay that
# way — they are the ids that reach this check:
#   * ``main._synthesize_session_id`` — ``session-<YmdHMSf>``, was 28 chars. Bound
#     as the request session, so it also wins over the one below via
#     plan_and_execute's ``effective_session``.
#   * ``plan_and_execute_tool._new_plan_session_id`` — ``plan-session-<epoch>``,
#     was 23 chars.
# The plan chainer keeps its OWN copy of this constant (it is a separate Lambda
# artifact and cannot import agent/src); the two MUST stay in lock-step.
_MIN_RUNTIME_SESSION_ID_LEN = 33


def invoke_runtime_session_kwargs(client=None, session_id: Optional[str] = None) -> dict:
    """Build extra ``invoke_agent_runtime`` kwargs that stitch a dispatched
    sub-agent (task / render / planner) into the SAME observability session as
    the calling main agent, via ``runtimeSessionId``.

    AgentCore keys per-session runtime log streams (``runtime-logs-<sessionId>``)
    on this value, so passing the chat session id makes the sub-agent's runtime
    logs/metrics land under the same session as the orchestrator instead of a
    fresh, uncorrelated session per invoke.

    NOTE: we deliberately do NOT pass W3C trace-context params
    (``traceParent``/``traceState``/``baggage``). Those map to ``traceparent`` /
    ``baggage`` HTTP headers that the runtime's OpenTelemetry botocore
    auto-instrumentation ALSO sets — after SigV4 signing — which corrupts the
    request signature and makes ``InvokeAgentRuntime`` fail with
    ``AccessDeniedException: signature does not match``, breaking dispatch
    entirely. OTEL already propagates trace context natively, so span
    correlation still works without them; ``runtimeSessionId`` is a distinct
    AgentCore header the instrumentation does not touch.

    Best-effort: an unbound session (async workers, unit tests) degrades to no
    kwarg silently. The ``client`` arg is accepted for signature compatibility
    but no longer used.
    """
    _ = client  # retained for call-site compatibility; not needed anymore
    kwargs: dict = {}
    sid = session_id
    if not sid:
        try:
            sid = current_session_id()
        except Exception:
            sid = None
    if isinstance(sid, str) and len(sid) >= _MIN_RUNTIME_SESSION_ID_LEN:
        kwargs["runtimeSessionId"] = sid
    return kwargs


class _LocalInvokeBody:
    """Wraps a local HTTP response so callers that expect a boto3 invoke_agent_runtime
    ``response["response"]`` StreamingBody (``.iter_lines()`` / ``.read()``) work unchanged."""
    def __init__(self, http_resp):
        self._r = http_resp

    def iter_lines(self):
        for line in self._r:
            yield line.rstrip(b"\r\n")

    def read(self):
        return self._r.read()


def _local_agent_endpoint(arn):
    """Return a local base URL for ``arn`` when LOCAL_AGENT_ENDPOINTS maps it, else None.

    LOCAL_AGENT_ENDPOINTS is a JSON object {agentRuntimeArn: "http://localhost:PORT"}. Set ONLY
    for the local E2E harness so a locally-run runtime dispatches to LOCALLY-run sub-runtimes.
    Unset in every deployed environment -> this whole path is skipped (no behavior change)."""
    import os
    import json
    raw = os.environ.get("LOCAL_AGENT_ENDPOINTS", "").strip()
    if not raw or not arn:
        return None
    try:
        return (json.loads(raw) or {}).get(arn)
    except (ValueError, TypeError):
        return None


def _invoke_agent_runtime_local(base_url, invoke_kwargs):
    """POST the invoke payload to a local runtime's /invocations and return a boto3-shaped dict."""
    import urllib.request
    payload = invoke_kwargs.get("payload") or b""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    url = base_url.rstrip("/") + "/invocations"
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": invoke_kwargs.get("contentType", "application/json")},
    )
    resp = urllib.request.urlopen(req, timeout=900)  # long: a render/plan turn can run minutes
    _logger.info("LOCAL_AGENT_ENDPOINTS: routed invoke -> %s", url)
    return {"response": _LocalInvokeBody(resp)}


def _resolve_read_timeout_types() -> tuple:
    """Collect whichever ``ReadTimeoutError`` classes this runtime can import.

    Both ``botocore.exceptions`` and ``urllib3.exceptions`` define one, and WHICH
    surfaces out of an ``invoke_agent_runtime`` depends on how deep the failure
    was wrapped, so we take whichever are importable rather than betting on a
    layer. BOTH are needed, not one as a shortcut for the other: botocore's class
    inherits from the class in ``botocore.vendored.requests.packages.urllib3``,
    NOT from the installed ``urllib3.exceptions.ReadTimeoutError``, so neither is
    an ``isinstance`` of the other. Resolved once at import time; an ImportError
    (a slimmed runtime, a future botocore that drops the alias) just yields a
    shorter tuple and :func:`_is_read_timeout` degrades to its string form.
    """
    types: list = []
    for module_name, attr in (
        ("botocore.exceptions", "ReadTimeoutError"),
        ("urllib3.exceptions", "ReadTimeoutError"),
    ):
        try:
            module = __import__(module_name, fromlist=[attr])
        except Exception:  # noqa: BLE001 - absence is a normal outcome here
            continue
        candidate = getattr(module, attr, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            types.append(candidate)
    return tuple(types)


_READ_TIMEOUT_TYPES = _resolve_read_timeout_types()


def _is_read_timeout(exc) -> bool:
    """True when an invoke failed only on READING the response.

    A read timeout is raised after the request was signed, sent and ACCEPTED —
    the target runtime is already running the work — so it is the one failure
    that must never be retried (see :func:`invoke_agent_runtime_traced`).

    Typed first, string second. The typed check is what makes this correct
    rather than merely diagnostic: this predicate now decides whether we
    dispatch a second time, so it should not hinge on a class name a dependency
    is free to rename. The string arm stays as the fallback for a wrapper class
    that is neither of the two importable types (and for the case where neither
    imported at all).

    The plan chainer keeps its OWN copy of this rule
    (``infra/lib/lambdas/plan-chainer/handler._is_read_timeout``) because it
    cannot import ``agent/src`` — the same arrangement as
    ``_MIN_RUNTIME_SESSION_ID_LEN`` above and ``SFN_EXECUTION_ARN_RE``. That copy
    is string-only, which is a strict SUBSET of this one and not a disagreement:
    both botocore's and urllib3's classes are literally named
    ``ReadTimeoutError``, so the two predicates return the same answer for every
    exception either actually sees. Keep them in lock-step if the rule changes.
    """
    if _READ_TIMEOUT_TYPES and isinstance(exc, _READ_TIMEOUT_TYPES):
        return True
    return "ReadTimeoutError" in type(exc).__name__ or "read operation timed out" in str(exc)


def invoke_agent_runtime_traced(client, session_id: Optional[str] = None, **invoke_kwargs):
    """Call ``client.invoke_agent_runtime`` with best-effort runtimeSessionId
    propagation that can NEVER break dispatch — and never dispatch twice.

    Attempts the session-scoped call first (so sub-agent runtime logs correlate
    to the caller's session); if that is REJECTED, retries once WITHOUT the
    session kwarg and logs a warning. This guards against a regression like the
    trace-header/SigV4 collision that previously made every dispatch fail —
    propagation is a nice-to-have, a working dispatch is not.

    The retry deliberately does NOT cover a read timeout, which is re-raised.
    The retry exists for exactly one failure mode — AgentCore refusing the
    ``runtimeSessionId`` kwarg (a ValidationException on the id, an older
    botocore that does not model the param, the SigV4 collision) — and every
    member of that mode is rejected up front, before any work is dispatched. A
    read timeout is the opposite evidence: the request was accepted and the
    sub-agent spent the client's whole read timeout (600s on the dispatch/render
    client, 900s on the plan client) working. Retrying it dispatched the work a
    SECOND time — on the render path, where ``RENDER_DISPATCH_BUDGET_SECONDS``
    abandons the stream at 90s, that meant rendering twice and paying for both.

    Uniform across all four call sites on purpose, including the three that need
    the response (``dispatch_tool._invoke_runtime_stream``,
    ``plan_and_execute_tool._invoke_runtime``, ``hooks.actions.dispatch_action``)
    rather than only the fire-and-forget-shaped one. Never-retrying costs those
    callers nothing, because the retry could not have recovered their response
    either: the second invoke would need another full read timeout to answer,
    long after the turn's budget is gone. So there is no propagation failure
    being silently converted into a dead turn — a read timeout was never a
    propagation failure. Each site already degrades legibly on the raised
    exception: dispatch_tool returns a tool-error result, the planning/design
    paths return "…Agent failed: …", ``render_artifacts`` returns "Render failed:
    …", and the hook engine's ``fire()`` records an error ActionResult without
    breaking the turn.

    There is deliberately no ``retry_on_read_timeout`` opt-in: no call site has a
    legitimate use for it, and a flag whose only effect is to double-dispatch is
    a flag someone eventually sets.

    Local E2E override (env-gated, no-op in prod): if the target ``agentRuntimeArn`` maps to a
    local endpoint via LOCAL_AGENT_ENDPOINTS, POST the payload to that runtime's local
    /invocations instead of calling boto3 — so a locally-run runtime dispatches to LOCALLY-run
    sub-runtimes (validating uncommitted code end-to-end with no deploy).
    """
    _local = _local_agent_endpoint(invoke_kwargs.get("agentRuntimeArn"))
    if _local:
        return _invoke_agent_runtime_local(_local, invoke_kwargs)
    extra = invoke_runtime_session_kwargs(client, session_id)
    if extra:
        try:
            return client.invoke_agent_runtime(**invoke_kwargs, **extra)
        except Exception as e:
            if _is_read_timeout(e):
                # Accepted, not rejected: the work is already running. Re-raise so
                # the caller sees one dispatch that timed out instead of two that ran.
                raise
            _logger.warning(
                "invoke_agent_runtime with runtimeSessionId failed (%s); "
                "retrying without session propagation", e,
            )
    return client.invoke_agent_runtime(**invoke_kwargs)


def current_run_id() -> Optional[str]:
    """Return the bound run_id, or None if unset.

    Unlike actor/session, run_id is OPTIONAL — most tool calls happen
    outside a plan execution context. Returns None (not raise) when
    unbound so the wrap can cleanly skip run_id on the JobRecord.
    """
    run_id = _current_run_id.get()
    if not isinstance(run_id, str) or not run_id:
        return None
    return run_id


def current_turn_id() -> Optional[str]:
    """Return the bound turn_id, or None if unset.

    Optional like run_id: returns None rather than raising so a job record is
    still written when the binding is missing (an older client, or an internal
    call path that never bound one). Absent turn_id degrades scoping back to
    session-wide, which is the behaviour that existed before it.
    """
    turn_id = _current_turn_id.get()
    if not isinstance(turn_id, str) or not turn_id:
        return None
    return turn_id


def set_turn_id(turn_id: str) -> contextvars.Token:
    """Bind a turn_id for the current invocation.

    Returns a reset token so the caller can restore the prior value when the
    invocation finishes.
    """
    return _current_turn_id.set(turn_id)


def reset_turn_id(token: contextvars.Token) -> None:
    """Reset the turn_id contextvar to its prior state."""
    _current_turn_id.reset(token)


def set_run_id(run_id: str) -> contextvars.Token:
    """Bind a run_id for the current execution context.

    Returns a reset token so the caller can restore the prior value
    when the plan execution finishes.
    """
    return _current_run_id.set(run_id)


def reset_run_id(token: contextvars.Token) -> None:
    """Reset the run_id contextvar to its prior state."""
    _current_run_id.reset(token)


# The plan NODE a task-agent invocation is executing, so a gateway job the node submits is
# registered with its node. The changefeed bridge maps a finished job back to its plan node by
# this; without it, it matched by the node row's `job_id` — which the node writes only when it
# FINISHES — and a job that completed while its node was still running fell back to "the first
# running node" with no job id, and the plan hung (session c3447913).
_current_node_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "jobs.current_node_id", default=None
)


def set_node_id(node_id: str) -> contextvars.Token:
    return _current_node_id.set(node_id if isinstance(node_id, str) and node_id else None)


def reset_node_id(token: contextvars.Token) -> None:
    _current_node_id.reset(token)


def current_node_id() -> Optional[str]:
    """The bound plan node id, or None outside a plan node."""
    return _current_node_id.get()


# The fal endpoint ids the bound plan node was PLANNED to submit to (``DAGNode.endpoint_ids``).
# A node prompt said `minimax/h3-max/image-to-video` and the sub-agent submitted to
# `fal-ai/minimax/hailuo-02/...`, which 422'd on `duration` minutes later (session c3447913).
# Empty = no plan constraint (non-fal nodes, plans authored before the field).
# Holds ``(ids, retired)``: ``retired`` is a MUTABLE set shared by every context copied from the node's, so a
# tool call (which may run in a copied context) can retire an id for the rest of the node.
_current_planned_endpoints: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "jobs.current_planned_endpoints", default=((), None)
)


def set_planned_endpoints(endpoint_ids) -> contextvars.Token:
    ids = endpoint_ids if isinstance(endpoint_ids, (list, tuple)) else []
    return _current_planned_endpoints.set(
        (tuple(s.strip() for s in ids if isinstance(s, str) and s.strip()), set()))


def reset_planned_endpoints(token: contextvars.Token) -> None:
    _current_planned_endpoints.reset(token)


def current_planned_endpoints() -> tuple:
    """The bound plan node's planned endpoint ids still in force, or () when unconstrained."""
    ids, retired = _current_planned_endpoints.get()
    return tuple(i for i in ids if not retired or i not in retired)


def retire_planned_endpoint(endpoint_id: str) -> bool:
    """Drop a planned id that does not exist (the provider answered "model not found") for the rest of the node.

    The planner can name a model that is not there (plan 07fd91bd: `fal-ai/flux-kontext/pro` for
    `fal-ai/flux-pro/kontext`); the off-plan guard then refused the node's correct id and the plan failed.
    An id nothing can run on constrains nothing. True when an id was retired.
    """
    ids, retired = _current_planned_endpoints.get()
    if retired is None or not isinstance(endpoint_id, str):
        return False
    from tool_args import _normalized_endpoint
    target = _normalized_endpoint(endpoint_id)
    hit = [i for i in ids if _normalized_endpoint(i) == target]
    retired.update(hit)
    return bool(hit)


__all__ = [
    "RequestContextTokens",
    "current_actor_id",
    "current_session_id",
    "current_run_id",
    "set_run_id",
    "reset_run_id",
    "current_node_id",
    "set_node_id",
    "reset_node_id",
    "current_planned_endpoints",
    "set_planned_endpoints",
    "reset_planned_endpoints",
    "current_turn_id",
    "set_turn_id",
    "reset_turn_id",
    "reset_request_context",
    "set_request_context",
    "set_active_skills",
    "current_active_skills",
]


# The Producing_Turn's Active_Project for work dispatched OUT of the Main_Agent (a task agent, a plan node),
# where `project_search.context.current_turn_context` is not bound. The dispatcher forwards it in the payload
# and the receiving runtime binds it here (ingestion Req 9.3).
_producing_project_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "producing_project_id", default=None
)


def set_producing_project_id(project_id: Optional[str]) -> contextvars.Token:
    return _producing_project_id.set(project_id if isinstance(project_id, str) and project_id else None)


def reset_producing_project_id(token: contextvars.Token) -> None:
    _producing_project_id.reset(token)


def current_producing_project_id() -> Optional[str]:
    """The Active_Project media made now belongs to: the Main_Agent turn's, else the forwarded one, else None."""
    try:
        from project_search.context import current_turn_context

        tc = current_turn_context.get()
        if tc is not None and getattr(tc, "active_project", None) is not None:
            return tc.active_project.id
    except Exception:  # noqa: BLE001 — project_search absent in some runtimes/tests
        pass
    return _producing_project_id.get()


def trusted_project_id() -> Optional[str]:
    """The project id a session OUTPUT may be written under (asset-lineage Phase 3 (a), design §9.1).

    Stricter than :func:`current_producing_project_id`: the Main_Agent turn's project counts only when the
    proxy verified access (``project_access == "ok"``); otherwise the id forwarded to a task agent / plan
    node by the dispatcher. Either way it must be a project id. ``None`` means "write the legacy prefix".
    """
    from projects.ids import is_project_id

    try:
        from project_search.context import current_turn_context

        tc = current_turn_context.get()
        if tc is not None:
            ap = getattr(tc, "active_project", None)
            if getattr(tc, "project_access", None) == "ok" and ap is not None and is_project_id(ap.id):
                return ap.id
            return None  # a main turn whose project is unavailable never writes under it
    except Exception:  # noqa: BLE001 — project_search absent in some runtimes/tests
        pass
    pid = _producing_project_id.get()
    return pid if is_project_id(pid) else None
