"""Signed delegation grants — the verify side, for the delegated Project_Token issuance.

Same wire format and security contract as ``../gateway-interceptor/delegation.py`` (the two are separate
deployment units with separate KMS permissions, so they share no code): ``verify_grant`` returns an actor id
ONLY when ``kms:Verify`` confirms the orchestrator's signature AND the grant is unexpired; every other outcome
returns ``""``. The actor comes exclusively from the verified payload.

Wire format (``agent/src/security/delegation.py``): ``<b64url(header)>.<b64url(payload)>.<b64url(signature)>``,
payload ``{"actor_id", "plan_id", "iat", "exp"}``, signature = ``kms:Sign`` (ECDSA_SHA_256) over the ASCII
``<header>.<payload>``.
"""
from __future__ import annotations

import base64
import binascii
import json
import time
from typing import Any, Callable

KEY_ENV = "DELEGATION_KEY_ARN"
SIGNING_ALGORITHM = "ECDSA_SHA_256"  # must match agent/src/security/delegation.py


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def make_grant_verifier(kms: Any, key_arn: str, *, clock: Callable[[], float] = time.time) -> Callable[[object], str]:
    """``verify(grant) -> actor_id | ""`` over an injected KMS client (fail-closed by construction)."""

    def verify(token: object) -> str:
        if not isinstance(token, str) or not token or not key_arn:
            return ""
        parts = token.split(".")
        if len(parts) != 3 or not all(parts):
            return ""
        header_b64, payload_b64, sig_b64 = parts
        try:
            signature = _b64url_decode(sig_b64)
        except (ValueError, TypeError, binascii.Error):
            return ""
        if not signature:
            return ""
        # 1) Signature FIRST: nothing in the payload is trusted until KMS confirms it.
        try:
            resp = kms.verify(KeyId=key_arn, Message=f"{header_b64}.{payload_b64}".encode("ascii"),
                              MessageType="RAW", Signature=signature, SigningAlgorithm=SIGNING_ALGORITHM)
        except Exception:  # noqa: BLE001 — invalid signature, throttling, permissions: all deny
            return ""
        if not resp.get("SignatureValid"):
            return ""
        # 2) The authenticated payload.
        try:
            payload = json.loads(_b64url_decode(payload_b64))
        except (ValueError, TypeError, binascii.Error):
            return ""
        if not isinstance(payload, dict):
            return ""
        # 3) Expiry.
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)) or isinstance(exp, bool) or clock() >= exp:
            return ""
        # 4) The actor, from the verified payload only.
        actor = payload.get("actor_id")
        return actor if isinstance(actor, str) and actor else ""

    return verify
