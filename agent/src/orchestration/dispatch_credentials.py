"""Credential a dispatched sub-agent invocation should carry to reach user_jwt gateways.

A dispatch hands work to another runtime (the task agent) that may run for minutes and reuses
a warm, per-session connection. Forwarding the browser's short-lived Cognito access token was
fragile: the sub-agent bound it once, kept reusing that connection, and the token lapsed under
it — the gateway front door then answered ``403 insufficient_scope`` (prod session 45d2139c:
three parallel panel renders failed 6 minutes after the token was bound; a retry that went
through the delegated path succeeded).

The runtime cannot refresh a user token (the refresh token lives in the browser), so prefer a
FRESHLY MINTED delegation grant on every dispatch — a signed capability over the actor with a
long TTL, presented with a machine token minted at connect. That is the same path async plan
nodes and the Slack ingress already use, and it does not depend on the browser token's
remaining life. The user token is a fallback only when no grant can be minted, and only while
it still has enough life left to cover a dispatched turn.
"""
from __future__ import annotations

import logging
import threading
import time

from mcp_manager import _user_token_is_expired

logger = logging.getLogger(__name__)

# One grant per actor, reused while it has ample life left. Parallel dispatches land in the SAME
# session-pinned sub-agent microVM; a different grant per dispatch would make the sub-agent tear
# down and rebuild its shared delegated connection on every call (it reuses the live session only
# when the grant is unchanged). Reissued once less than REISSUE_BEFORE_EXPIRY_SECONDS remain, so a
# reused grant always outlives any dispatched turn. Minted per request inside the handler (never at
# import), so this is snapshot-safe.
REISSUE_BEFORE_EXPIRY_SECONDS = 2 * 60 * 60
_grant_cache: dict[str, tuple[str, float]] = {}
_grant_cache_lock = threading.Lock()


def _cached_grant(actor_id: str, delegation) -> str:
    now = time.time()
    with _grant_cache_lock:
        cached = _grant_cache.get(actor_id)
        if cached and cached[1] - now > REISSUE_BEFORE_EXPIRY_SECONDS:
            return cached[0]
        grant = delegation.mint_grant(actor_id, f"dispatch-{actor_id}")
        _grant_cache[actor_id] = (grant, now + float(delegation.DEFAULT_TTL_SECONDS))
        return grant

# A fallback user token must outlive a dispatched turn (task-agent node budget is 600s; an
# interactive dispatch is usually far shorter). Five minutes keeps a dying token from being
# bound while still allowing the fallback for the common case.
MIN_FALLBACK_TOKEN_LIFE_SECONDS = 300.0


def dispatch_credentials(actor_id: str, user_token: str = "") -> dict:
    """Return ``{"delegation_grant": ...}``, ``{"user_token": ...}``, or ``{}``.

    1. A fresh delegation grant whenever grants are enabled and the actor is eligible.
    2. Otherwise the caller's user token, if it is not expired or about to expire.
    3. Otherwise nothing — the sub-agent then serves only non-user_jwt tools rather than
       presenting a credential the gateway will reject.
    Never raises.
    """
    try:
        from security import delegation

        if actor_id and delegation.grants_enabled() and delegation.can_delegate(actor_id):
            return {"delegation_grant": _cached_grant(actor_id, delegation)}
    except Exception as e:  # noqa: BLE001 — a mint failure falls back to the user token
        logger.warning("dispatch credentials: grant mint failed, falling back: %s", e)

    if user_token and not _user_token_is_expired(
        user_token, skew_seconds=MIN_FALLBACK_TOKEN_LIFE_SECONDS
    ):
        return {"user_token": user_token}
    if user_token:
        logger.warning(
            "dispatch credentials: user token expires within %ds and no grant is available; "
            "forwarding no user_jwt credential",
            int(MIN_FALLBACK_TOKEN_LIFE_SECONDS),
        )
    return {}
