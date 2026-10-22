"""Signed delegation grants — the *verify* side (gateway interceptor only).

Wave 1 / option E. Counterpart to ``agent/src/delegation.py`` (the mint side).
For async plan traffic the caller carries no user JWT ``sub`` for the
interceptor to derive an actor from; instead it presents a delegation grant —
a short, KMS-signed capability over ``{actor_id, plan_id, exp}`` that only the
orchestrator could have produced. This module verifies it.

Security contract (this is the whole point of option E)
--------------------------------------------------------
``verify_grant`` returns an actor id **only** when the signature verifies via
KMS *and* the grant has not expired. Every other outcome — malformed token,
bad signature, expired, missing key config, KMS error — returns ``""``. There
is deliberately no parameter, branch, or return path by which an unverified
value (such as an accompanying ``actor_id`` header) becomes the result. The
caller (``index._actor_id``) treats ``""`` as "no identity" and therefore
fails closed for user-owned tools. The actor is derived from the signed
payload alone; the ``actor_id`` field the chainer also stamps on the request is
untrusted decoration and is never read here.

Key management: verification is ``kms:Verify`` against the same asymmetric key
the orchestrator signs with. This asset holds no key material and never signs.

Wire format: see ``agent/src/delegation.py``. Both sides agree on the format
but share no code (separate deployment units / KMS permissions).
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time

import boto3

logger = logging.getLogger()

# KMS key id/ARN used to verify grants. Set on the interceptor Lambda only.
# Absent => verification cannot succeed, so every grant fails closed (denies).
KEY_ENV = "DELEGATION_KEY_ARN"

# Must match agent/src/delegation.SIGNING_ALGORITHM.
SIGNING_ALGORITHM = "ECDSA_SHA_256"

REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))

_kms = None


def _kms_client():
    """Lazily create the KMS client so import performs no I/O (test-friendly)."""
    global _kms
    if _kms is None:
        _kms = boto3.client("kms", region_name=REGION)
    return _kms


def _key_arn() -> str:
    return os.environ.get(KEY_ENV, "") or ""


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _now() -> float:
    return time.time()


def verify_grant(token: str) -> str:
    """Verify a delegation grant and return its actor id, or "" on ANY failure.

    Fail-closed by construction: the ONLY way this returns a non-empty string
    is the KMS signature verifying valid AND the grant being unexpired. Signature
    is checked BEFORE the payload is trusted for anything, and the returned actor
    comes exclusively from the verified payload.
    """
    if not token:
        return ""
    key_arn = _key_arn()
    if not key_arn:
        # No verification key configured: cannot establish authenticity, so a
        # grant proves nothing. Deny rather than trust an unverifiable token.
        logger.warning("delegation: %s not configured; grant denied", KEY_ENV)
        return ""

    parts = token.split(".")
    if len(parts) != 3:
        return ""
    header_b64, payload_b64, sig_b64 = parts
    if not (header_b64 and payload_b64 and sig_b64):
        return ""

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        signature = _b64url_decode(sig_b64)
    except (ValueError, TypeError, binascii.Error):
        return ""
    if not signature:
        return ""

    # 1) Signature FIRST. The payload is not trusted for anything until KMS
    #    confirms the orchestrator produced this exact signing input.
    try:
        resp = _kms_client().verify(
            KeyId=key_arn,
            Message=signing_input,
            MessageType="RAW",
            Signature=signature,
            SigningAlgorithm=SIGNING_ALGORITHM,
        )
    except Exception as exc:  # noqa: BLE001 — any KMS/verify failure denies
        # KMSInvalidSignatureException, throttling, permission, connectivity:
        # all indistinguishable to the caller and all deny.
        logger.warning("delegation: KMS verify failed; grant denied (%s)", type(exc).__name__)
        return ""
    if not resp.get("SignatureValid"):
        return ""

    # 2) Only now parse the (authenticated) payload.
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, TypeError, binascii.Error):
        return ""
    if not isinstance(payload, dict):
        return ""

    # 3) Expiry. A signed-but-stale grant denies.
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return ""
    if _now() >= exp:
        logger.info("delegation: grant expired (exp=%s); denied", exp)
        return ""

    # 4) Actor derived exclusively from the verified payload.
    actor = payload.get("actor_id")
    if not isinstance(actor, str) or not actor:
        return ""
    return actor
