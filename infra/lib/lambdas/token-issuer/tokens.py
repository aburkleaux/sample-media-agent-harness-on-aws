"""Project_Token encoding, signing, and verification (design Component 3, D10).

A Project_Token is an RS256 JWS-signed JWT:

    header  {"typ": "JWT", "alg": "RS256", "kid": "<signing key id>"}
    payload {"iss", "aud", "sub", "project_id", "iat", "exp" = iat + lifetime, "jti" (uuid4)}

exactly those seven claims, for first issuance and Token_Refresh alike (Req 4.2, 4.3).

This module is dependency-free: standard library only (``base64``/``json``/``hashlib`` and
:mod:`jwslite`), so the ``TokenIssuerFn`` / ``TokenRefreshFn`` asset deploys with no layer and no
bundling step. The RSA private key lives in KMS and never leaves it:

- Signing goes through a ``Signer``; in Lambda that is ``KmsSigner`` — ``kms:Sign`` with
  ``MessageType=DIGEST`` over the SHA-256 of ``b64url(header).b64url(payload)`` and
  ``RSASSA_PKCS1_V1_5_SHA_256`` (Req 4.4). The result is a standard RS256 JWT, so the external RS256
  verifiers that read the JWK the OIDC publisher publishes (``kms:GetPublicKey`` → JWK) keep working.
- Verification goes through a ``Verifier``; in Lambda that is ``KmsVerifier`` — ``kms:Verify`` with
  the same ``MessageType=DIGEST`` and algorithm. No public key is fetched or handled locally.

``verify`` is the Token_Refresh check (Req 4.21, 4.23): header ``typ``/``alg``/``kid``, the signature
(via the injected ``Verifier``), ``iss``, ``aud``, ``sub``, ``project_id``, an integer ``iat`` not
after ``now``, and an integer ``exp`` with ``now - exp <= REFRESH_GRACE_S``. The grace applies to
``exp`` ALONE: ``iat`` and ``nbf`` are checked against ``now`` with no leeway.

This module never logs. A token is a bearer credential; callers log ``jti`` and claims, never the token.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import jwslite

# 8 h: the runtimes' maxLifetime (requirements decision 16). A presented token whose `exp` is at most
# this far in the past is still refreshable; one second more is invalid_token.
REFRESH_GRACE_S = 28800

JWT_TYP = "JWT"
JWT_ALG = "RS256"
KMS_SIGNING_ALGORITHM = "RSASSA_PKCS1_V1_5_SHA_256"

# Allowed configured lifetime (Req 4.3). Synth enforces the same range; this is the runtime backstop.
MIN_LIFETIME_S = 60
MAX_LIFETIME_S = 3600

# A Project_Token is < 1 KB (prototype V2). Refusing anything far larger bounds the work an arbitrary
# presented string can cause before the signature check rejects it.
MAX_TOKEN_CHARS = 8192

_PROJECT_ID_RE = re.compile(r"p[0-9a-f]{32}")


# --------------------------------------------------------------------------------------------------
# Configuration (read once per container, shared by issue_handler and refresh_handler)
# --------------------------------------------------------------------------------------------------


def parse_lifetime_seconds(raw: str | None) -> int:
    """Parse ``TOKEN_LIFETIME_SECONDS``; raise naming the value and the allowed range."""
    try:
        value = int(str(raw).strip(), 10)
    except (TypeError, ValueError):
        raise ValueError(
            f"TOKEN_LIFETIME_SECONDS={raw!r} is not an integer; allowed range is "
            f"{MIN_LIFETIME_S}-{MAX_LIFETIME_S} seconds"
        ) from None
    if not MIN_LIFETIME_S <= value <= MAX_LIFETIME_S:
        raise ValueError(
            f"TOKEN_LIFETIME_SECONDS={value} is outside the allowed range "
            f"{MIN_LIFETIME_S}-{MAX_LIFETIME_S} seconds"
        )
    return value


def parse_published_key_ids(raw: str | None) -> frozenset[str]:
    """Parse ``PUBLISHED_KEY_IDS`` (comma-separated key ids, D10 ``publishedKeys``)."""
    ids = frozenset(part.strip() for part in (raw or "").split(",") if part.strip())
    if not ids:
        raise ValueError("PUBLISHED_KEY_IDS is empty; at least one signing key id must be published")
    return ids


@functools.cache
def configured_lifetime_seconds() -> int:
    """``TOKEN_LIFETIME_SECONDS``, read once per container for both handlers."""
    return parse_lifetime_seconds(os.environ.get("TOKEN_LIFETIME_SECONDS"))


@functools.cache
def configured_published_key_ids() -> frozenset[str]:
    """``PUBLISHED_KEY_IDS``, read once per container."""
    return parse_published_key_ids(os.environ.get("PUBLISHED_KEY_IDS"))


# --------------------------------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------------------------------


class Signer(Protocol):
    """Signs a SHA-256 digest with RSASSA-PKCS1-v1_5. ``kid`` goes into the token header."""

    @property
    def kid(self) -> str: ...

    def sign_digest(self, digest: bytes) -> bytes: ...


class KmsSigner:
    """Signs through an injected KMS client (``boto3.client("kms")`` or a fake with ``sign``)."""

    def __init__(self, kms_client: Any, key_id: str) -> None:
        if not isinstance(key_id, str) or not key_id:
            raise ValueError("KmsSigner needs a non-empty KMS key id")
        self._kms = kms_client
        self._key_id = key_id

    @property
    def kid(self) -> str:
        return self._key_id

    def sign_digest(self, digest: bytes) -> bytes:
        resp = self._kms.sign(
            KeyId=self._key_id,
            Message=digest,
            MessageType="DIGEST",
            SigningAlgorithm=KMS_SIGNING_ALGORITHM,
        )
        return resp["Signature"]


def _b64url(data: bytes) -> str:
    return jwslite.b64url_encode(data)


def _compact_json(obj: Mapping[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class IssuedToken:
    token: str
    exp: int
    jti: str
    iat: int


def build_claims(*, iss: str, aud: str, sub: str, project_id: str, iat: int, lifetime: int) -> dict[str, Any]:
    """The seven Project_Token claims, ``exp = iat + lifetime``, a fresh uuid4 ``jti``."""
    for name, value in (("iss", iss), ("aud", aud), ("sub", sub)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
    if not is_project_id(project_id):
        raise ValueError("project_id must match ^p[0-9a-f]{32}$")
    if type(iat) is not int:
        raise ValueError("iat must be an integer")
    if type(lifetime) is not int or not MIN_LIFETIME_S <= lifetime <= MAX_LIFETIME_S:
        raise ValueError(f"lifetime must be an integer in {MIN_LIFETIME_S}-{MAX_LIFETIME_S}")
    return {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "project_id": project_id,
        "iat": iat,
        "exp": iat + lifetime,
        "jti": str(uuid.uuid4()),
    }


def encode(claims: Mapping[str, Any], signer: Signer) -> str:
    """Serialize and sign: ``b64url(header).b64url(payload).b64url(sig over SHA256(signing input))``."""
    header = {"typ": JWT_TYP, "alg": JWT_ALG, "kid": signer.kid}
    signing_input = f"{_b64url(_compact_json(header))}.{_b64url(_compact_json(claims))}"
    signature = signer.sign_digest(hashlib.sha256(signing_input.encode("ascii")).digest())
    return f"{signing_input}.{_b64url(signature)}"


def issue(
    signer: Signer, *, iss: str, aud: str, sub: str, project_id: str, now: float, lifetime: int
) -> IssuedToken:
    """Sign a new Project_Token with ``iat = floor(now)``."""
    claims = build_claims(iss=iss, aud=aud, sub=sub, project_id=project_id, iat=int(now), lifetime=lifetime)
    return IssuedToken(token=encode(claims, signer), exp=claims["exp"], jti=claims["jti"], iat=claims["iat"])


# --------------------------------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------------------------------


def is_project_id(value: object) -> bool:
    return isinstance(value, str) and _PROJECT_ID_RE.fullmatch(value) is not None


class InvalidProjectToken(Exception):
    """The presented token fails a check of Req 4.23. ``reason`` is a short code, safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Verifier(Protocol):
    """Decides whether a ``kid`` is published and whether a signature over a SHA-256 digest is valid.

    ``kid in verifier`` gates on the published-id set WITHOUT a remote call (an unpublished kid is
    refused for free). ``verify_digest`` returns a bool; a verifier failure that is NOT a signature
    mismatch (e.g. KMS unavailable) propagates, so the caller answers ``error`` rather than judging
    the token invalid.
    """

    def __contains__(self, kid: object) -> bool: ...

    def verify_digest(self, kid: str, digest: bytes, signature: bytes) -> bool: ...


def _is_invalid_signature_error(exc: BaseException) -> bool:
    """True for KMS's ``KMSInvalidSignatureException`` (a bad signature), by class name or error code."""
    if type(exc).__name__ == "KMSInvalidSignatureException":
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        return response.get("Error", {}).get("Code") == "KMSInvalidSignatureException"
    return False


class KmsVerifier:
    """Verifies a Project_Token signature with ``kms:Verify`` over the published key ids.

    Hold one instance per container (module scope). Membership is the published-id set, so an
    unpublished kid is refused without a KMS call. ``kms:Verify`` reports a bad signature either as a
    ``False`` ``SignatureValid`` or as a ``KMSInvalidSignatureException`` — both map to ``False`` here;
    any other error (throttle, unavailable) propagates and is the caller's failure, not an invalid token.
    """

    def __init__(self, kms_client: Any, published_key_ids: Iterable[str]) -> None:
        self._kms = kms_client
        self._published = frozenset(published_key_ids)

    def __contains__(self, kid: object) -> bool:
        return isinstance(kid, str) and kid in self._published

    def verify_digest(self, kid: str, digest: bytes, signature: bytes) -> bool:
        try:
            resp = self._kms.verify(
                KeyId=kid,
                Message=digest,
                MessageType="DIGEST",
                Signature=signature,
                SigningAlgorithm=KMS_SIGNING_ALGORITHM,
            )
        except Exception as exc:
            if _is_invalid_signature_error(exc):
                return False
            raise
        return bool(resp.get("SignatureValid", False))


def verify(
    token: object,
    verifier: Verifier,
    iss: str,
    aud: str,
    now: float,
) -> dict[str, Any]:
    """Verify a presented Project_Token for Token_Refresh; return its claims or raise ``InvalidProjectToken``.

    ``verifier`` knows the published key ids and verifies a signature (``KmsVerifier`` in Lambda).
    Checks, in order: header ``typ``/``alg``/``kid``, the RS256 signature, the required claims,
    ``iss``, ``aud``, ``sub``, ``project_id``, ``nbf`` (if present, not after ``now``), an integer
    ``iat`` not after ``now``, and an integer ``exp`` with ``now - exp <= REFRESH_GRACE_S``. An
    unexpired token passes the same ``exp`` check.
    """
    if not isinstance(token, str) or not token:
        raise InvalidProjectToken("absent")
    if len(token) > MAX_TOKEN_CHARS or token.count(".") != 2:
        raise InvalidProjectToken("malformed")

    try:
        header, claims, signing_input, signature = jwslite.split_compact(token)
    except jwslite.JwtMalformed:
        raise InvalidProjectToken("malformed") from None
    if header.get("typ") != JWT_TYP:
        raise InvalidProjectToken("typ")
    if header.get("alg") != JWT_ALG:
        raise InvalidProjectToken("alg")
    kid = header.get("kid")
    if not isinstance(kid, str) or kid not in verifier:
        raise InvalidProjectToken("kid")

    if not verifier.verify_digest(kid, hashlib.sha256(signing_input).digest(), signature):
        raise InvalidProjectToken("signature")

    for required in ("iss", "aud", "sub", "iat", "exp"):
        if required not in claims:
            raise InvalidProjectToken(f"missing_{required}")
    if claims.get("iss") != iss:
        raise InvalidProjectToken("iss")
    # strict audience: the claim must be the exact audience string (a list containing it is not it).
    if claims.get("aud") != aud:
        raise InvalidProjectToken("aud")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise InvalidProjectToken("sub")
    if not is_project_id(claims.get("project_id")):
        raise InvalidProjectToken("project_id")
    nbf = claims.get("nbf")
    if nbf is not None and (type(nbf) is not int or nbf > now):
        raise InvalidProjectToken("nbf")
    iat = claims.get("iat")
    if type(iat) is not int or iat > now:
        raise InvalidProjectToken("iat")
    exp = claims.get("exp")
    if type(exp) is not int:
        raise InvalidProjectToken("exp")
    if now - exp > REFRESH_GRACE_S:
        raise InvalidProjectToken("expired_beyond_grace")
    return claims
