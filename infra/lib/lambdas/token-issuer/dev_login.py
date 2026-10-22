"""DEV ONLY. Verifier for the harness's local "Dev Login" User_Token. Never used by the Lambda.

With ``VITE_DEV_BYPASS_AUTH=true`` the SPA mints an HS256 JWT in the browser (``ui/src/auth/devAuth.ts``;
README "Local development authentication"): signed with ``VITE_DEV_JWT_SECRET`` (default
``dev-secret-do-not-use-in-prod``), ``sub = local-dev-user``, ``token_use = access``, a placeholder
Cognito-shaped ``iss``, and ``aud`` = the client id. A local dev wrapper around ``index.make_issue_handler``
can pass ``DevLoginUserTokenVerifier`` in place of ``cognito.CognitoUserTokenVerifier`` so first issuance
works against a dev session.

It is selected only by being passed in explicitly. ``index.py`` does not import this module, and no
environment variable switches the Lambda path to it: ``index.issue_handler`` always builds the Cognito
verifier, which accepts RS256 only and so rejects every dev token. The secret is public, so anyone can mint
a token this class accepts; that is why it must never be reachable from a deployed function.

Dependency-free, like the rest of the asset: the HS256 signature is checked with the standard library
(:mod:`jwslite` ``hmac_sha256_verify``), no third-party token library.
"""

from __future__ import annotations

import os
from typing import Any

import jwslite
from cognito import MAX_TOKEN_CHARS, UserTokenInvalid

DEV_ALG = "HS256"
DEFAULT_DEV_JWT_SECRET = "dev-secret-do-not-use-in-prod"


class DevLoginUserTokenVerifier:
    """DEV ONLY: accepts a Dev Login HS256 access token. Same ``verify`` contract as the Cognito verifier.

    Checks the HS256 signature with the dev secret, an integer ``exp`` after ``now``, ``token_use = access``,
    and a non-empty ``sub``. ``issuer`` and ``audience`` are checked only when given, because the SPA fills
    them from optional env vars with placeholders.
    """

    def __init__(
        self,
        *,
        secret: str | None = None,
        issuer: str | None = None,
        audience: str | None = None,
    ) -> None:
        resolved = secret if secret is not None else os.environ.get("VITE_DEV_JWT_SECRET") or DEFAULT_DEV_JWT_SECRET
        if not isinstance(resolved, str) or not resolved:
            raise ValueError("DevLoginUserTokenVerifier needs a non-empty secret")
        self._secret = resolved
        self._issuer = issuer
        self._audience = audience

    def verify(self, token: object, *, now: float) -> dict[str, Any]:
        if not isinstance(token, str) or not token:
            raise UserTokenInvalid("absent")
        if len(token) > MAX_TOKEN_CHARS or token.count(".") != 2:
            raise UserTokenInvalid("malformed")
        try:
            header, claims, signing_input, signature = jwslite.split_compact(token)
        except jwslite.JwtMalformed:
            raise UserTokenInvalid("malformed") from None
        if header.get("alg") != DEV_ALG:
            raise UserTokenInvalid("alg")

        if not jwslite.hmac_sha256_verify(self._secret.encode("utf-8"), signing_input, signature):
            raise UserTokenInvalid("signature")

        for required in ("sub", "exp", "token_use"):
            if required not in claims:
                raise UserTokenInvalid(f"missing_{required}")
        if self._issuer is not None and claims.get("iss") != self._issuer:
            raise UserTokenInvalid("iss")
        if self._audience is not None and claims.get("aud") != self._audience:
            raise UserTokenInvalid("aud")
        exp = claims.get("exp")
        if type(exp) is not int or now >= exp:
            raise UserTokenInvalid("exp")
        if claims.get("token_use") != "access":
            raise UserTokenInvalid("token_use")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise UserTokenInvalid("sub")
        return claims


__all__ = ["DEFAULT_DEV_JWT_SECRET", "DevLoginUserTokenVerifier"]
