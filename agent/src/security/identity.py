"""Shared identity resolution and AgentCore-Memory keying.

This module is the single home for two pieces of logic that were previously
inlined in ``main.py`` (the orchestrator) and now need to be reused by
``task_agent.py`` as well:

1. **Actor-id resolution** (:func:`resolve_actor_id`) — how the runtime
   recovers the caller's platform identity on the cloud-managed AgentCore
   runtime, which strips every header except ``Authorization`` and
   ``Baggage``. The proxy stamps the resolved id into the request body as
   ``__actor_id`` after validating the bearer token at the edge.

2. **AgentCore-Memory keying** (:func:`resolve_memory_actor_id`,
   :func:`derive_memory_keys`, :func:`build_memory_config`) — how the
   durable-memory actor id, session id, and (future) namespace are computed
   before constructing an :class:`AgentCoreMemoryConfig`.

Factoring these here (Component 5 in the custom-agents design) lets both the
main orchestrator and the Task Agent share one proven implementation instead
of re-deriving identity independently. Task 12.2 extends the memory-keying
helpers with the segregated external ``EXT#`` keyspace for
issuer-qualified external callers; this module deliberately keeps the
key-derivation logic centralized so that extension has a single seam.

_Requirements: 6.1, 6.2, 10.1_
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)

logger = logging.getLogger(__name__)


# Fallback actor id used for durable memory when no JWT-derived actor is
# bound (e.g. local development). Mirrors the constant historically defined
# in ``main.py``; sourced from the same env var so behavior is unchanged.
LAB_DEFAULT_ACTOR_ID = os.environ.get("LAB_DEFAULT_ACTOR_ID", "local-dev-user")


def decode_jwt_sub(token: str) -> Optional[str]:
    """Extract the ``sub`` claim from a JWT bearer token without verifying.

    The AgentCore Runtime authorizer already validated the signature, issuer,
    audience, and expiry before the request reached this handler — by the
    time we see the token its claims are trusted. We only need to read
    ``sub`` (the Cognito user-pool subject) to bind the request's actor id.

    Tolerates the ``Bearer `` prefix and JWS variants that include or omit
    base64 padding. Returns ``None`` for any malformed input so the caller
    can fall back to the next resolution step.
    """
    if not token:
        return None
    raw = token.strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    parts = raw.split(".")
    if len(parts) < 2:
        return None
    payload_segment = parts[1]
    # Pad to a multiple of 4 — JWS strips padding by spec.
    pad = "=" * (-len(payload_segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_segment + pad)
        claims = json.loads(decoded)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    sub = claims.get("sub") if isinstance(claims, dict) else None
    if isinstance(sub, str) and sub.strip():
        return sub.strip()
    return None


def resolve_actor_id(agentcore_context, payload: Optional[dict] = None) -> str:
    """Resolve the actor_id for the current request.

    The cloud-managed AgentCore runtime strips every header except
    ``Authorization`` and ``Baggage`` (plus the workload-identity token)
    before invoking the handler, so the SDK's
    ``X-Amzn-Bedrock-AgentCore-Runtime-Custom-*`` allowlist documented in
    ``bedrock_agentcore/runtime/app.py`` doesn't actually take effect on
    cloud deployments — empirical: the runtime's `request_headers`
    consistently contains only ``baggage`` and ``workloadaccesstoken``,
    even when the proxy emits a custom-prefixed actor-id header. The
    Authorization header is also stripped by the time the SDK builds the
    context, so JWT decoding from headers is moot too.

    The proxy works around this by stamping the resolved actor id into
    the JSON request body as ``__actor_id`` (after JWT validation at the
    edge). The body always passes through the proxy, so the trust
    boundary is preserved: only the proxy can write this field, and only
    after validating the bearer token and binding ``req.actorId`` to the
    JWT ``sub`` claim.

    Resolution order (first non-empty wins):
      1. ``payload['__actor_id']`` — the proxy's primary path. Used by
         every authenticated /lab and /api request.
      2. ``context.actor_id`` — forward-compat for SDK builds that
         eventually populate it from the JWT.
      3. JWT ``sub`` from a forwarded ``Authorization`` header — only
         useful if the platform stops stripping the header.
      4. Plain ``x-actor-id`` / custom-prefixed header — kept for
         hypothetical future SDKs that relax the filter.

    Stage 2 of the user-auth rollout (see user-auth/design.md) makes
    JWT-derived identity mandatory: there is no longer an env-var
    fallback. If none of the sources produce a non-empty actor_id, this
    function raises so callers fail loudly instead of silently binding
    shared identity.

    Raises:
        RuntimeError: When the actor id cannot be resolved from any
            source. Indicates the proxy isn't stamping ``__actor_id``
            and no other identity-bearing channel survived the platform.
    """
    if isinstance(payload, dict):
        candidate = payload.get("__actor_id")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    direct = getattr(agentcore_context, "actor_id", None)
    if direct and str(direct).strip():
        return str(direct).strip()

    headers = getattr(agentcore_context, "request_headers", None) or {}
    if isinstance(headers, dict):
        for key in (
            "x-amzn-bedrock-agentcore-runtime-custom-actor-id",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id",
        ):
            v = headers.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()

        for key in ("authorization", "Authorization"):
            auth = headers.get(key)
            if isinstance(auth, str) and auth.strip():
                sub = decode_jwt_sub(auth)
                if sub:
                    return sub

        for key in ("x-actor-id", "X-Actor-Id"):
            v = headers.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()

    raise RuntimeError(
        "Unable to resolve actor_id: payload missing '__actor_id', "
        "context.actor_id is empty, and no identity headers survived. "
        "Verify the AgentCore proxy is stamping __actor_id into the "
        "request body after JWT validation."
    )


# ---------------------------------------------------------------------------
# AgentCore-Memory keying
#
# These helpers centralize how the durable-memory (actor_id, session_id,
# namespace) triple is computed before an AgentCoreMemoryConfig is built.
# Today this is an identity mapping for the internal/main path (the resolved
# actor id and the runtime session id are used verbatim). Task 12.2 extends
# these to compute the segregated external ``EXT#`` keyspace for
# issuer-qualified external callers — hence the single seam here.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryKey:
    """The derived durable-memory key for a request.

    Attributes:
        actor_id: The AgentCore Memory actor id (the memory subject).
        session_id: The AgentCore Memory session id.
        namespace: Optional namespace segment. ``None`` for the internal
            main-agent path today; reserved for the external ``EXT#``
            keyspace introduced in task 12.2.
    """

    actor_id: str
    session_id: str
    namespace: Optional[str] = None


def resolve_memory_actor_id(ctx_actor_id: Optional[str]) -> str:
    """Resolve the actor id used to key the durable memory store.

    Prefers the JWT-derived actor id (when the caller bound one, e.g. via
    ``LabService.ctx.actor_id``) and falls back to
    :data:`LAB_DEFAULT_ACTOR_ID` when none is available. This preserves the
    exact fallback behavior previously inlined in ``main.py``.
    """
    if isinstance(ctx_actor_id, str) and ctx_actor_id.strip():
        return ctx_actor_id
    return LAB_DEFAULT_ACTOR_ID


def derive_memory_keys(
    actor_id: str,
    session_id: str,
    *,
    namespace: Optional[str] = None,
) -> MemoryKey:
    """Pure derivation of the durable-memory key for a request.

    For the internal main-agent path this is an identity mapping: the
    resolved actor id and runtime session id are used verbatim, with no
    namespace. Centralizing it here gives task 12.2 a single place to add
    the segregated external ``EXT#`` keyspace (keyed on
    ``(agentId, issuer, sub)``) without touching call sites.
    """
    return MemoryKey(actor_id=actor_id, session_id=session_id, namespace=namespace)


# Long-term memory namespaces. These MUST match the CfnMemory strategy
# `namespaces` in infra/lib/agent-infra-stack.ts (summary + userPreference):
# the retrieval side (Strands session manager) resolves these
# `{actorId}`/`{sessionId}` templates, and the extraction side routes records
# into the same namespaces. If the two drift, retrieval silently returns nothing
# (no error). Leading+trailing slashes follow the AWS "Strands SDK + AgentCore
# Memory" doc convention. Kept in one place and asserted by test_identity.
# `preferences` is actor-scoped (cross-session); `summaries` is per-session
# (conversation compression / cross-session summary).
#
# There is deliberately NO facts namespace. Long-term FACTS (`/facts/{actorId}/`) were removed
# (2026-10): they were actor-wide, carried no project label and never expired, so remembered
# file locations and media claims leaked across projects (staging session 07703a35 pasted other
# sessions' `artifacts/<uuid>/input` URIs). Projects own files (project_status/show_asset) and
# content (per-project search); memory keeps only preferences and session summaries.
SESSION_SUMMARY_NAMESPACE = "/summaries/{actorId}/{sessionId}/"
USER_PREFERENCE_NAMESPACE = "/preferences/{actorId}/"


def build_memory_config(
    memory_id: str,
    session_id: str,
    actor_id: str,
    *,
    namespace: Optional[str] = None,
    enable_retrieval: bool = True,
    retrieval_top_k: int = 10,
    retrieval_relevance: float = 0.4,
    include_session_summary: bool = False,
    batch_size: int = 1,
) -> AgentCoreMemoryConfig:
    """Build an :class:`AgentCoreMemoryConfig` from the derived memory key.

    ``batch_size`` (default 1: one synchronous ``CreateEvent`` per message, the SDK default)
    buffers that many history writes. The main agent passes > 1 together with
    ``memory_context.FramedMemorySessionManager(async_writes=True)``, which sends the buffer from
    a background writer and drains it at turn end; on its own the SDK sends at turn end only.

    This centralizes the keying used when constructing the durable-memory
    session manager. The ``namespace`` argument is threaded through
    :func:`derive_memory_keys` for future external-keyspace use (task 12.2);
    for the current main-agent path it is ``None`` and the config is built
    with the actor id and session id exactly as before.

    Long-term retrieval (``enable_retrieval=True``, the default) attaches a
    ``retrieval_config`` for the PREFERENCE namespace only (there is no facts
    namespace; see the constants above). The Strands session manager then, on
    each user turn, semantic-searches it using the user's message as the query
    and prepends the matches as a ``<user_context>`` block — this is what
    surfaces cross-session preferences to the model. Retrieval degrades gracefully: with
    no records yet (or ``MEMORY_ID`` unset) nothing is injected. Set
    ``enable_retrieval=False`` to build a write-only config (history persists,
    no injection) — the ops kill-switch for the injection path.

    The same-session SUMMARY namespace (``/summaries/<actor>/<sid>/``) is NOT retrieved by
    default: the main agent already loads this session's history, so summaries of it only
    duplicated what the model could see, ahead of the user's text (session 764a71c0: a bare
    "2" pulled 23 records / 18 KB). ``include_session_summary=True`` restores it for a caller
    without history. ``retrieval_relevance`` defaults to 0.4 (was 0.2) for the same reason:
    at 0.2 nearly any record matched a short message.
    """
    key = derive_memory_keys(actor_id, session_id, namespace=namespace)
    retrieval_config = None
    if enable_retrieval:
        # One RetrievalConfig shared across the namespaces (no per-namespace
        # strategy_id, since the namespace templates carry no {memoryStrategyId}).
        # Preferences only: facts are no longer read or written (see the constants above).
        rc = RetrievalConfig(top_k=retrieval_top_k, relevance_score=retrieval_relevance)
        retrieval_config = {
            USER_PREFERENCE_NAMESPACE: rc,
        }
        if include_session_summary:
            retrieval_config[SESSION_SUMMARY_NAMESPACE] = rc
    return AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=key.session_id,
        actor_id=key.actor_id,
        retrieval_config=retrieval_config,
        # Clamped to the SDK's accepted range (1..100).
        batch_size=max(1, min(100, int(batch_size or 1))),
    )


# ---------------------------------------------------------------------------
# External (issuer-qualified caller) memory keyspace — task 12.2
#
# External HTTP invocations of a Custom Agent target the shared Task Agent
# with the *Owner* stamped as the resource actor (``actor_id``) but a
# separate ``principal = {issuer, sub}`` stamped for memory/session/audit.
# The caller-scoped durable memory added to the Task Agent MUST be keyed on
# the full ``(agentId, issuer, sub)`` triple in a keyspace that is
# **segregated** from the internal platform actor keyspace so no external
# caller can ever alias an internal user (design Component 5, Property 12 &
# 17; Requirements 10.1-10.5, 19.2).
#
# The two invariants this enforces:
#   * **Keyspace segregation** — every external key carries the
#     :data:`EXTERNAL_KEYSPACE_PREFIX` (``EXT#``). Internal actor ids are raw
#     platform subjects (Cognito ``sub`` / ``LAB_DEFAULT_ACTOR_ID``) that
#     never begin with ``EXT#``, so the external and internal keyspaces are
#     disjoint by construction regardless of what ``sub`` an owner-supplied
#     IdP mints (Property 17 / Req 19.2).
#   * **Collision-free triple keying** — the key is a pure function of the
#     full ``(agentId, issuer, sub)`` triple: identical triples yield
#     identical keys and any triple differing in *any* component yields a
#     distinct key (Property 12 / Req 10.5). The issuer is always folded in,
#     so two issuers minting the same ``sub`` never collide (never
#     ``sub``-alone); the Owner is never an input (never Owner-alone).
# ---------------------------------------------------------------------------

# Prefix marking the segregated external caller keyspace. Kept importable so
# the Property 12/17 tests (tasks 12.4/12.5) can assert disjointness from the
# internal keyspace.
#
# Delimiter note: AgentCore Memory's actor_id/session_id accept only
# ``[a-zA-Z0-9][a-zA-Z0-9-_/]*(?::...)`` — ``#`` is REJECTED with a
# ValidationException on ListEvents/CreateEvent. The key therefore uses ``_`` as
# both the prefix terminator and the segment separator (``EXT_<h>_<h>``). ``EXT_``
# is still disjoint from the internal ``USER#`` keyspace (a raw platform subject
# never begins with ``EXT_``), so segregation (Property 17) is preserved.
EXTERNAL_KEYSPACE_PREFIX = "EXT_"

# Length of the short, human-readable issuer-hash segment embedded in the
# external key for operability (issuer URLs are long and unbounded). The
# collision-free guarantee does NOT rely on this truncated segment — it rests
# on the full-triple digest below — so truncation here is purely cosmetic.
_EXTERNAL_ISSUER_HASH_LEN = 16


def _sha256_hex(value: str) -> str:
    """Hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_external_material(
    agent_id: str, issuer: str, sub: str, conversation_id: str = ""
) -> str:
    """Injective, length-prefixed encoding of the external identity tuple.

    Encodes each component as ``<len>:<value>`` and concatenates. Because
    every component is prefixed by its exact length, the decoding is
    unambiguous no matter what bytes a component contains — including the
    delimiter used in the readable key, empty strings, or a ``sub`` crafted to
    look like another tuple. This is what makes the derived key a
    *collision-free* function of its inputs: distinct tuples can never encode
    to the same material string, so their digests differ (Property 12 /
    Req 10.5).

    ``conversation_id`` is an OPTIONAL 4th component that sub-partitions a
    single caller's memory WITHIN their own ``(agentId, issuer, sub)`` space —
    it lets the caller start a fresh conversation without ever reaching another
    caller's memory (issuer/sub come from the verified token, not the client).
    The empty-string default reproduces the original 3-component key byte-for-
    byte, so an absent conversation id is equivalent to "the caller's default
    conversation" and is backward-compatible with existing keys.
    """
    parts = (agent_id, issuer, sub, conversation_id)
    return "".join(f"{len(part)}:{part}" for part in parts)


def derive_external_memory_key(
    agent_id: str, issuer: str, sub: str, conversation_id: str = ""
) -> MemoryKey:
    """Derive the segregated external durable-memory key for a caller.

    Produces a :class:`MemoryKey` whose ``actor_id``, ``session_id``, and
    ``namespace`` are all a pure, collision-free function of the full
    ``(agentId, issuer, sub)`` triple, living in the ``EXT#`` keyspace that
    is disjoint from the internal platform actor keyspace.

    The key has the shape::

        EXT#<sha256(issuer)[:16]>#<sha256(canonical(agentId, issuer, sub))>

    The leading ``EXT#`` guarantees keyspace segregation (Property 17 /
    Req 19.2): internal actor ids are raw platform subjects that never carry
    this prefix, so an external caller can never alias an internal
    ``USER#``/Cognito actor even if its IdP mints a matching ``sub``. The
    trailing full-triple digest guarantees collision-freeness across the
    whole triple (Property 12 / Req 10.5), and the embedded issuer hash keeps
    the key operable/greppable while ensuring the issuer is always part of the
    key (never ``sub``-alone, never Owner-alone).

    All three fields (actor id / session id / namespace) share the same
    derived value so the caller's memory subject, conversation session, and
    namespace are scoped identically and cannot drift apart.

    Args:
        agent_id: The Custom Agent id (``agentId``) being invoked.
        issuer: The validated JWT ``iss`` claim (OIDC issuer). MUST be
            present — it is what prevents two issuers minting the same
            ``sub`` from colliding.
        sub: The validated JWT ``sub`` claim (the external caller subject).

    Raises:
        ValueError: If any of ``agent_id``, ``issuer``, or ``sub`` is not a
            non-empty string. Failing loudly here prevents silently deriving
            an under-qualified (e.g. issuer-less) key that would violate the
            keying invariants.
    """
    if not (isinstance(agent_id, str) and agent_id.strip()):
        raise ValueError("derive_external_memory_key: agent_id must be a non-empty string")
    if not (isinstance(issuer, str) and issuer.strip()):
        # The issuer is mandatory: a bare `sub` is not globally unique under
        # bring-your-own OIDC, so omitting the issuer would break Req 10.4.
        raise ValueError("derive_external_memory_key: issuer must be a non-empty string")
    if not (isinstance(sub, str) and sub.strip()):
        raise ValueError("derive_external_memory_key: sub must be a non-empty string")

    issuer_hash = _sha256_hex(issuer)[:_EXTERNAL_ISSUER_HASH_LEN]
    tuple_hash = _sha256_hex(
        _canonical_external_material(agent_id, issuer, sub, conversation_id)
    )
    # ``_``-separated (not ``#``) so the value satisfies AgentCore Memory's
    # actor_id/session_id character constraint; see EXTERNAL_KEYSPACE_PREFIX.
    external_id = f"{EXTERNAL_KEYSPACE_PREFIX}{issuer_hash}_{tuple_hash}"
    return MemoryKey(actor_id=external_id, session_id=external_id, namespace=external_id)


def build_external_memory_config(
    memory_id: str,
    agent_id: str,
    issuer: str,
    sub: str,
    conversation_id: str = "",
) -> AgentCoreMemoryConfig:
    """Build an :class:`AgentCoreMemoryConfig` for an external caller.

    Thin wrapper over :func:`derive_external_memory_key` that constructs the
    durable-memory config keyed on the segregated ``EXT_`` external key
    derived from ``(agentId, issuer, sub[, conversation_id])`` — never the
    Owner ``actor_id``. Mirrors :func:`build_memory_config` (used by the
    internal main-agent path) so the Task Agent constructs its session manager
    the same proven way, only with the external key. ``conversation_id``
    sub-partitions the caller's own memory so "start a new conversation"
    yields a fresh thread without crossing caller boundaries.
    """
    key = derive_external_memory_key(agent_id, issuer, sub, conversation_id)
    return AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=key.session_id,
        actor_id=key.actor_id,
    )
