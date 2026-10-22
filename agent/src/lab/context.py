"""LabRequestContext — the single seam through which actor identity flows.

Identity is derived ONLY from the AgentCore Runtime invocation context (validated
JWT claims). Payload-supplied actor_id fields are never read.

Requirements: 1.1, 1.2, 1.6, 16.1, 16.2, 16.3
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from .errors import AuthenticationError


def _new_request_id() -> str:
    """Generate a unique request ID for log correlation."""
    return str(uuid.uuid4())


def _extract_group_ids(agentcore_context: Any) -> list[str]:
    """Extract group membership from the validated JWT claims.

    Reads the ``cognito:groups`` claim from the AgentCore Runtime context. The
    claim is normalized to ``list[str]``. When the claim is absent (or the
    context has no ``claims`` attribute), an empty list is returned per
    Requirement 16.2.

    Cognito typically delivers ``cognito:groups`` as a JSON array of strings,
    but some upstreams encode it as a comma-separated string; both shapes are
    accepted defensively.

    Args:
        agentcore_context: The AgentCore Runtime context object. May expose
            claims either via a ``claims`` mapping attribute or via a direct
            ``cognito_groups`` / ``groups`` attribute.

    Returns:
        The actor's group IDs as a list of strings, or an empty list when no
        claim is present.
    """
    # Preferred shape: a `claims` dict carrying the validated JWT payload.
    claims = getattr(agentcore_context, "claims", None)
    raw: Any = None
    if isinstance(claims, dict):
        raw = claims.get("cognito:groups")

    # Fallback shapes for runtimes that surface the claim as a direct attribute.
    if raw is None:
        raw = getattr(agentcore_context, "cognito_groups", None)
    if raw is None:
        raw = getattr(agentcore_context, "group_ids", None)

    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(g) for g in raw if g is not None and str(g).strip()]
    if isinstance(raw, tuple):
        return [str(g) for g in raw if g is not None and str(g).strip()]
    if isinstance(raw, str):
        # Strip JSON-array brackets if a stringified list slipped through, then
        # split on commas. Empty / whitespace-only entries are dropped.
        stripped = raw.strip().strip("[]")
        return [g.strip() for g in stripped.split(",") if g.strip()]
    return []


@dataclass(frozen=True)
class LabRequestContext:
    """Immutable request context bound at the Lab_Service entrypoint.

    Attributes:
        actor_id: The authenticated user identity from the JWT sub claim.
        session_id: The runtime session identifier from the runtimeSessionId header.
        request_id: A server-assigned UUID4 for log correlation.
        group_ids: The actor's Cognito group memberships from the
            ``cognito:groups`` JWT claim. Empty list when the claim is absent
            (Requirement 16.1, 16.2). Visibility resolution and the canonical
            operations consume this list directly via ``ctx.group_ids``
            (Requirement 16.3) so that downstream code never re-decodes the
            JWT.
    """

    actor_id: str
    session_id: str
    request_id: str
    group_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_agentcore_context(cls, agentcore_context) -> "LabRequestContext":
        """Create a LabRequestContext from the AgentCore Runtime invocation context.

        This is the SOLE entry point for identity binding. The actor_id is extracted
        from the validated context object only — never from request payloads, chat
        messages, or tool arguments. Group membership is similarly derived from
        the validated ``cognito:groups`` JWT claim attached to the context.

        Args:
            agentcore_context: The AgentCore Runtime context object containing
                actor_id (from JWT sub claim), session_id (from runtimeSessionId
                header), and the validated JWT claims.

        Returns:
            A frozen LabRequestContext instance.

        Raises:
            AuthenticationError: If actor_id is missing or empty in the context.
        """
        actor_id = getattr(agentcore_context, "actor_id", None)
        if not actor_id or not str(actor_id).strip():
            # Identity is derived strictly from validated JWT claims (Requirement
            # 1.2). No silent fallback to a development user — production must
            # never degrade to a shared identity, and tests rely on this strict
            # behavior.
            raise AuthenticationError("missing actor_id in AgentCore context")

        session_id = getattr(agentcore_context, "session_id", None) or ""
        group_ids = _extract_group_ids(agentcore_context)

        return cls(
            actor_id=actor_id,
            session_id=session_id,
            request_id=_new_request_id(),
            group_ids=group_ids,
        )
