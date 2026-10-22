"""Cognito access-token verification for the Capability API (R5).

A faithful Python port of ``ui/auth/validateJwt.ts`` so the two front doors
agree about who a caller is. Divergence here would be a security bug, not a
style difference, so the checks are kept in the same order and with the same
reasoning:

* signature against the pool's JWKS (RS256 only),
* ``iss`` equals ``USER_POOL_ISSUER``,
* ``exp`` / ``nbf`` within skew,
* ``token_use == "access"``,
* ``client_id == USER_POOL_CLIENT_ID``.

**Cognito quirk:** access tokens carry no ``aud`` claim — the app client id
appears under the non-standard ``client_id`` claim instead; only *ID* tokens
have ``aud``. The SPA sends access tokens (so downstream scopes are right), so
audience verification is disabled in PyJWT and ``client_id`` is asserted
manually. Verifying ``aud`` instead would reject every real request.

Identity comes from the token and nowhere else. The request body is never
consulted for actor, groups, organization, or owner ids — a caller must not be
able to widen their own visibility by asserting membership.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

#: Clock skew tolerated on ``exp``/``nbf``, matching typical Cognito guidance.
LEEWAY_SECONDS: int = 60

#: How long a fetched JWKS is reused. Cognito rotates signing keys rarely; a
#: 10-minute cache matches the proxy's ``jwks-rsa`` configuration and keeps a
#: burst of tab loads from hammering the well-known endpoint.
JWKS_TTL_SECONDS: int = 600

#: Claim carrying Cognito group membership. Present on access tokens only when
#: the user is in at least one group, so "absent" and "empty" must be treated
#: identically.
GROUPS_CLAIM: str = "cognito:groups"

#: Optional claim mapping a user to an organization. Read from the token rather
#: than a request field because the org tier of the visibility union is an
#: authorization boundary (R5).
ORG_CLAIMS: tuple[str, ...] = ("custom:org_id", "custom:orgId", "org_id")


class AuthError(Exception):
    """Token missing, malformed, or not valid for this pool.

    Deliberately one exception class with a short ``code``: the response tells
    the caller *that* authentication failed and nothing about why beyond a
    stable code, so a probe cannot use the error text to enumerate pool
    configuration.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail or code)


class _JwksCache:
    """Thread-safe, TTL'd JWKS holder.

    A cache miss under concurrent load fetches once; the lock is held across
    the fetch deliberately, since the alternative is N simultaneous requests to
    the same well-known endpoint on every cold start.
    """

    def __init__(self, issuer: str, ttl: int = JWKS_TTL_SECONDS) -> None:
        self._issuer = issuer.rstrip("/")
        self._ttl = ttl
        self._lock = threading.Lock()
        self._client: Any = None
        self._fetched_at: float = 0.0

    @property
    def uri(self) -> str:
        return f"{self._issuer}/.well-known/jwks.json"

    def signing_key(self, token: str) -> Any:
        import jwt as pyjwt

        with self._lock:
            expired = (time.time() - self._fetched_at) > self._ttl
            if self._client is None or expired:
                self._client = pyjwt.PyJWKClient(self.uri, cache_keys=True)
                self._fetched_at = time.time()
            client = self._client
        try:
            return client.get_signing_key_from_jwt(token).key
        except Exception as err:  # noqa: BLE001
            # An unknown `kid` is the expected symptom of a rotated key against
            # a stale cache. Drop the client so the next call refetches rather
            # than failing every request until the TTL lapses.
            with self._lock:
                self._client = None
                self._fetched_at = 0.0
            raise AuthError("invalid_token", "no signing key for token") from err


class TokenVerifier:
    """Verifies Cognito access tokens for one user pool."""

    def __init__(
        self,
        *,
        issuer: Optional[str] = None,
        client_id: Optional[str] = None,
        dev_bypass_actor: Optional[str] = None,
    ) -> None:
        self._issuer = (issuer or "").rstrip("/")
        self._client_id = client_id or ""
        self._dev_bypass_actor = dev_bypass_actor
        self._jwks = _JwksCache(self._issuer) if self._issuer else None

    @classmethod
    def from_env(cls, environ: Optional[dict[str, str]] = None) -> "TokenVerifier":
        """Build from the same env vars ``ui/server`` uses.

        ``DEV_BYPASS_AUTH=true`` short-circuits verification for local runs, and
        is only honoured when an issuer is *not* configured as well — a
        deployed task with a real pool must never be talked into bypass mode by
        a stray env var.
        """
        env = dict(os.environ if environ is None else environ)
        issuer = (env.get("USER_POOL_ISSUER") or "").strip()
        client_id = (env.get("USER_POOL_CLIENT_ID") or "").strip()
        bypass = env.get("DEV_BYPASS_AUTH", "").strip().lower() == "true"
        dev_actor = None
        if bypass and not issuer:
            dev_actor = env.get("LAB_DEFAULT_ACTOR_ID") or "local-dev-user"
            logger.warning(
                "DEV_BYPASS_AUTH enabled — JWT validation disabled, actor=%s",
                dev_actor,
            )
        elif bypass:
            logger.error(
                "ignoring DEV_BYPASS_AUTH: USER_POOL_ISSUER is configured, so "
                "this process is serving a real user pool"
            )
        return cls(issuer=issuer, client_id=client_id, dev_bypass_actor=dev_actor)

    @property
    def configured(self) -> bool:
        return bool(self._issuer and self._client_id) or bool(self._dev_bypass_actor)

    def verify(self, token: Optional[str]) -> dict[str, Any]:
        """Return the validated claims, or raise :class:`AuthError`."""
        if self._dev_bypass_actor:
            return {
                "sub": self._dev_bypass_actor,
                GROUPS_CLAIM: ["gateway-admins"],
                "token_use": "access",
            }
        if not self._issuer or not self._client_id:
            raise AuthError("auth_misconfigured", "user pool is not configured")
        if not token:
            raise AuthError("missing_token", "Authorization: Bearer <token> required")

        import jwt as pyjwt

        assert self._jwks is not None
        key = self._jwks.signing_key(token)
        try:
            claims = pyjwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                # Cognito access tokens have no `aud`; the app client is
                # asserted below against `client_id` instead.
                options={"verify_aud": False, "require": ["exp", "iss", "sub"]},
            )
        except Exception as err:  # noqa: BLE001 — never echo the raw reason
            raise AuthError("invalid_token", type(err).__name__) from err

        if claims.get("token_use") != "access":
            raise AuthError("invalid_token", "not an access token")
        if claims.get("client_id") != self._client_id:
            raise AuthError("invalid_token", "client_id mismatch")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise AuthError("invalid_token", "missing sub")
        return claims


def bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Extract the token from an ``Authorization: Bearer <jwt>`` header.

    Unlike the proxy this accepts no ``?access_token=`` query fallback: that
    transport exists only for the browser ``EventSource`` API, which cannot set
    headers, and this API has no streaming endpoint. Not accepting it keeps
    tokens out of access logs and CloudFront cache keys.
    """
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def normalize_groups(raw: Any) -> list[str]:
    """Normalize ``cognito:groups`` to ``list[str]``.

    Cognito emits an array, but some upstreams encode it as a comma-separated
    string; both are accepted defensively, matching
    ``lab.context._extract_group_ids``.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(g) for g in raw if g is not None and str(g).strip()]
    if isinstance(raw, str):
        stripped = raw.strip().strip("[]")
        return [g.strip() for g in stripped.split(",") if g.strip()]
    return []


def org_id_from_claims(claims: dict[str, Any]) -> Optional[str]:
    """First present organization claim, or ``None``.

    ``None`` means the org tier of the visibility union is skipped entirely —
    fail closed, since guessing an org id would grant access to another
    organization's shared assets.
    """
    for claim in ORG_CLAIMS:
        value = claims.get(claim)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def actor_from_claims(
    claims: dict[str, Any], *, request_id: str = "", session_id: str = ""
) -> Any:
    """Build the trusted :class:`capability.aggregate.ActorContext`."""
    from capability.aggregate import ActorContext

    return ActorContext(
        actor_id=str(claims["sub"]),
        group_ids=normalize_groups(claims.get(GROUPS_CLAIM)),
        org_id=org_id_from_claims(claims),
        session_id=session_id,
        request_id=request_id,
    )


__all__ = [
    "AuthError",
    "GROUPS_CLAIM",
    "JWKS_TTL_SECONDS",
    "LEEWAY_SECONDS",
    "ORG_CLAIMS",
    "TokenVerifier",
    "actor_from_claims",
    "bearer_token",
    "normalize_groups",
    "org_id_from_claims",
]
