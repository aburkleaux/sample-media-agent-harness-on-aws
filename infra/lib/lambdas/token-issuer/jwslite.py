"""Pure-stdlib JWS/token primitives for the token-issuer asset (no third-party crypto or token library).

Everything here is standard-library only, so the ``TokenIssuerFn`` / ``TokenRefreshFn`` code asset
(and the ``Asset_Service`` that bundles ``cognito.py``/``tokens.py``) deploys and runs on the Lambda
Python 3.13 runtime with no layer and no bundling step.

What this module does, and deliberately does NOT do:

- ``b64url_encode`` / ``b64url_decode`` — base64url without padding, the JOSE encoding.
- ``encode_segment`` — a compact-JSON object as one base64url segment (for building a signing input).
- ``split_compact`` — split a compact JWS into (header, payload, signing-input bytes, raw signature),
  decoding the two JSON-object segments. Raises :class:`JwtMalformed` on anything else.
- ``rsa_pkcs1v15_sha256_verify`` — verify an RS256 signature with the public ``(n, e)`` ONLY. RSA
  verification is a public-key operation: one modular exponentiation (:func:`pow`) plus the
  EMSA-PKCS1-v1_5 DigestInfo comparison (RFC 8017 §8.2.2 / §9.2). No private key, no native code.
- ``hmac_sha256_verify`` — verify an HS256 signature (the dev-only Dev Login token).
- ``rsa_numbers_from_jwk`` — RSA ``(n, e)`` big-ints from a JWK.

It does NOT sign a Project_Token and it does NOT verify one: a Project_Token is signed by KMS
(``kms:Sign``) and verified by KMS (``kms:Verify``) in :mod:`tokens`, because that key lives in KMS.
The RS256 verify here is for *foreign* tokens whose keys KMS does not hold — the Cognito pool JWKS
(:mod:`cognito`) — and the HMAC verify is for the dev HS256 token (:mod:`dev_login`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

# EMSA-PKCS1-v1_5 DigestInfo DER prefix for SHA-256 (RFC 8017 §9.2): the 19 bytes that precede the
# 32-byte digest inside the padded encoded message.
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


class JwtMalformed(Exception):
    """A compact JWS could not be split into three base64url segments of JSON-object header/payload."""


def b64url_encode(data: bytes) -> str:
    """base64url-encode ``data`` with the padding stripped (JOSE convention)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(segment: str) -> bytes:
    """Decode a base64url segment, restoring stripped padding. Raise ``ValueError`` on bad input."""
    if not isinstance(segment, str):
        raise ValueError("base64url segment must be a string")
    return base64.urlsafe_b64decode(segment + ("=" * (-len(segment) % 4)))


def encode_segment(obj: Mapping[str, Any]) -> str:
    """Serialize ``obj`` as compact JSON and base64url-encode it (one JWS segment)."""
    return b64url_encode(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def split_compact(token: str) -> tuple[dict, dict, bytes, bytes]:
    """Return ``(header, payload, signing_input, signature)`` for a compact JWS.

    ``signing_input`` is the ASCII bytes of ``header_segment.payload_segment`` (what the signature is
    computed over); ``signature`` is the raw, still-unverified signature bytes. Raises
    :class:`JwtMalformed` unless the token is three base64url segments whose header and payload each
    decode to a JSON object.
    """
    try:
        header_seg, payload_seg, sig_seg = token.split(".")
    except (AttributeError, ValueError):
        raise JwtMalformed("not three segments") from None
    try:
        header = json.loads(b64url_decode(header_seg))
        payload = json.loads(b64url_decode(payload_seg))
        signature = b64url_decode(sig_seg)
    except ValueError:  # bad base64url, non-ASCII, or invalid JSON (JSONDecodeError is a ValueError)
        raise JwtMalformed("bad base64url or JSON") from None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise JwtMalformed("header/payload is not a JSON object")
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    return header, payload, signing_input, signature


def unverified_header(token: str) -> dict:
    """The compact JWS header as a dict, without verifying the signature."""
    return split_compact(token)[0]


def rsa_pkcs1v15_sha256_verify(n: int, e: int, message: bytes, signature: bytes) -> bool:
    """True iff ``signature`` is a valid RSASSA-PKCS1-v1_5 signature over ``SHA-256(message)``.

    Public-key operation only (RFC 8017): compute ``s^e mod n``, then compare, in constant time,
    against the EMSA-PKCS1-v1_5 encoding ``0x00 0x01 0xFF…0xFF 0x00 || DigestInfo || hash``. Any
    malformed input or mismatch returns ``False`` rather than raising.
    """
    if not isinstance(n, int) or not isinstance(e, int) or n <= 0 or e <= 0:
        return False
    k = (n.bit_length() + 7) // 8
    if k == 0 or len(signature) != k:
        return False
    sig_int = int.from_bytes(signature, "big")
    if sig_int >= n:
        return False
    encoded = pow(sig_int, e, n).to_bytes(k, "big")
    digest_info = _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    if k < len(digest_info) + 11:  # RFC 8017: PS is at least 8 bytes of 0xFF
        return False
    expected = b"\x00\x01" + (b"\xff" * (k - len(digest_info) - 3)) + b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)


def hmac_sha256_verify(secret: bytes, signing_input: bytes, signature: bytes) -> bool:
    """True iff ``signature`` is ``HMAC-SHA256(secret, signing_input)`` (constant-time compare)."""
    expected = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return hmac.compare_digest(expected, signature)


def rsa_numbers_from_jwk(entry: Mapping[str, Any]) -> tuple[int, int]:
    """RSA ``(n, e)`` big-ints from a JWK's base64url ``n``/``e``; raise ``ValueError`` otherwise."""
    try:
        n = int.from_bytes(b64url_decode(entry["n"]), "big")
        e = int.from_bytes(b64url_decode(entry["e"]), "big")
    except (KeyError, TypeError, ValueError):
        raise ValueError("JWK is missing a valid RSA n/e pair") from None
    if n <= 0 or e <= 0:
        raise ValueError("JWK n/e must be positive integers")
    return n, e


__all__ = [
    "JwtMalformed",
    "b64url_decode",
    "b64url_encode",
    "encode_segment",
    "hmac_sha256_verify",
    "rsa_numbers_from_jwk",
    "rsa_pkcs1v15_sha256_verify",
    "split_compact",
    "unverified_header",
]
