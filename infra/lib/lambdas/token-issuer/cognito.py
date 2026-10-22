"""User_Token verification for first issuance (design Component 3 "How first issuance is authenticated").

The issuer event carries the caller's Cognito **access** token, not an actor id. ``CognitoUserTokenVerifier``
checks it against the user pool JWKS:

- header ``alg = RS256`` and a ``kid`` present in the pool JWKS,
- the RS256 signature,
- ``iss = https://cognito-idp.<region>.amazonaws.com/<userPoolId>``,
- an integer ``exp`` with ``now < exp`` (no leeway, matching the proxy's ``validateJwt``),
- ``token_use = access``,
- ``client_id`` = the LabUIClient id (Cognito access tokens carry no ``aud``; the app client is under
  ``client_id``),
- a non-empty ``sub``, which is the only identity the handler uses (Req 1.18).

This module is dependency-free: standard library only (:mod:`jwslite` does the base64url/JSON parse and
the RSASSA-PKCS1-v1_5 SHA-256 signature check with the JWK's ``(n, e)``). The pool keys live at Cognito,
not in our KMS, so the signature is checked locally with the public key — a public-key operation that
needs no private material and no native crypto library.

The JWKS is fetched once per container and held by a ``CognitoJwks`` instance. An unknown ``kid`` triggers
at most one refetch per ``MIN_REFETCH_INTERVAL_S`` (a pool key rotation), so a stream of forged ``kid``s
cannot turn every call into a JWKS fetch.

A verification failure raises ``UserTokenInvalid`` (the handler answers ``unauthenticated``). A JWKS that
cannot be fetched raises ``JwksUnavailable`` instead: that is the issuer's failure, not the caller's, and
the handler answers ``error``.

This module never logs and never puts token text into an exception message.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import jwslite

COGNITO_ALG = "RS256"

# Same bound as tokens.MAX_TOKEN_CHARS: refuse oversized input before any parsing.
MAX_TOKEN_CHARS = 8192

# A pool key rotation is rare; one refetch per 30 s on an unknown kid is enough to pick it up.
MIN_REFETCH_INTERVAL_S = 30.0

# The proxy waits 2 s for the issuer; a JWKS fetch must leave room for the read and the KMS call.
JWKS_FETCH_TIMEOUT_S = 1.5


class UserTokenInvalid(Exception):
    """The User_Token fails a check. ``reason`` is a short code, safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class JwksUnavailable(Exception):
    """The pool JWKS could not be fetched or parsed; verification has no answer."""


class UserTokenVerifier(Protocol):
    """What ``index.make_issue_handler`` needs: verified claims (with ``sub``) or ``UserTokenInvalid``."""

    def verify(self, token: object, *, now: float) -> dict[str, Any]: ...


def cognito_issuer(region: str, user_pool_id: str) -> str:
    return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"


def fetch_jwks_json(url: str) -> Mapping[str, Any]:
    """GET the JWKS document. Module-level so a test or dev wrapper can substitute the transport."""
    with urllib.request.urlopen(url, timeout=JWKS_FETCH_TIMEOUT_S) as resp:  # noqa: S310 (https URL we build)
        return json.loads(resp.read())


class CognitoJwks:
    """``kid -> RSA public numbers (n, e)`` for one pool, fetched lazily and cached for the container."""

    def __init__(
        self,
        url: str,
        *,
        fetch: Callable[[str], Mapping[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        min_refetch_interval_s: float = MIN_REFETCH_INTERVAL_S,
    ) -> None:
        self.url = url
        self._fetch = fetch
        self._clock = clock
        self._min_interval = min_refetch_interval_s
        self._keys: dict[str, tuple[int, int]] = {}
        self._fetched_at: float | None = None
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        fetch = self._fetch or fetch_jwks_json
        try:
            doc = fetch(self.url)
            entries = doc["keys"]
        except Exception as exc:  # network, HTTP, JSON, or shape: all mean "no JWKS"
            raise JwksUnavailable(type(exc).__name__) from None
        keys: dict[str, tuple[int, int]] = {}
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, Mapping) or entry.get("kty") != "RSA":
                continue
            kid = entry.get("kid")
            if not isinstance(kid, str) or entry.get("alg", COGNITO_ALG) != COGNITO_ALG:
                continue
            try:
                keys[kid] = jwslite.rsa_numbers_from_jwk(entry)
            except ValueError:
                continue
        if not keys:
            raise JwksUnavailable("no usable RSA keys")
        self._keys = keys
        self._fetched_at = self._clock()

    def key(self, kid: str) -> tuple[int, int]:
        """The ``(n, e)`` for ``kid``; raise ``UserTokenInvalid("kid")`` if the pool does not have it."""
        with self._lock:
            if self._fetched_at is None:
                self._refresh()
            key = self._keys.get(kid)
            if key is None and self._clock() - self._fetched_at >= self._min_interval:
                self._refresh()
                key = self._keys.get(kid)
        if key is None:
            raise UserTokenInvalid("kid")
        return key


class CognitoUserTokenVerifier:
    """Verifies a Cognito access token for one user pool and app client. See the module docstring."""

    def __init__(
        self,
        *,
        region: str,
        user_pool_id: str,
        client_id: str,
        jwks: CognitoJwks | None = None,
    ) -> None:
        for name, value in (("region", region), ("user_pool_id", user_pool_id), ("client_id", client_id)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"CognitoUserTokenVerifier needs a non-empty {name}")
        self.issuer = cognito_issuer(region, user_pool_id)
        self.client_id = client_id
        self.jwks = jwks or CognitoJwks(f"{self.issuer}/.well-known/jwks.json")

    def verify(self, token: object, *, now: float) -> dict[str, Any]:
        if not isinstance(token, str) or not token:
            raise UserTokenInvalid("absent")
        if len(token) > MAX_TOKEN_CHARS or token.count(".") != 2:
            raise UserTokenInvalid("malformed")
        try:
            header, claims, signing_input, signature = jwslite.split_compact(token)
        except jwslite.JwtMalformed:
            raise UserTokenInvalid("malformed") from None
        if header.get("alg") != COGNITO_ALG:
            raise UserTokenInvalid("alg")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise UserTokenInvalid("kid")
        n, e = self.jwks.key(kid)

        if not jwslite.rsa_pkcs1v15_sha256_verify(n, e, signing_input, signature):
            raise UserTokenInvalid("signature")

        for required in ("iss", "sub", "exp", "token_use", "client_id"):
            if required not in claims:
                raise UserTokenInvalid(f"missing_{required}")
        if claims.get("iss") != self.issuer:
            raise UserTokenInvalid("iss")
        exp = claims.get("exp")
        if type(exp) is not int or now >= exp:
            raise UserTokenInvalid("exp")
        nbf = claims.get("nbf")
        if nbf is not None and (type(nbf) is not int or nbf > now):
            raise UserTokenInvalid("nbf")
        if claims.get("token_use") != "access":
            raise UserTokenInvalid("token_use")
        if claims.get("client_id") != self.client_id:
            raise UserTokenInvalid("client_id")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise UserTokenInvalid("sub")
        return claims


__all__ = [
    "CognitoJwks",
    "CognitoUserTokenVerifier",
    "JwksUnavailable",
    "UserTokenInvalid",
    "UserTokenVerifier",
    "cognito_issuer",
    "fetch_jwks_json",
]
