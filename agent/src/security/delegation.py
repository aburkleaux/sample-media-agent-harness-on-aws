"""Signed delegation grants — the *mint* side (orchestrator only).

Wave 1 / option E. A plan node executes long after the request that created it
has ended, in a separate process (the plan-chainer Lambda → Task Agent), where
the user's ~1h JWT is gone and cannot be persisted (see ``jobs/context``). The
gateway interceptor derives the actor from a *verifiable* identity, never from a
caller's assertion. A delegation grant restores that property for async plan
traffic: a short, signed capability over ``{actor_id, plan_id, exp}`` that only
the orchestrator can produce and that the interceptor verifies before deriving
the actor from it.

Key management
--------------
The grant is signed with a KMS **asymmetric** key (ECDSA_SHA_256). The private
key never leaves KMS: this module calls ``kms:Sign`` and the interceptor calls
``kms:Verify``. No key material is in source, on the plan row, or in any
payload — only the resulting signature travels. IAM scopes signing to the
orchestrator role and verification to the interceptor role (see
``infra/lib/agent-infra-stack.ts``).

This module is import-only for the *mint* verb. Verification lives in the
interceptor asset (``infra/lib/lambdas/gateway-interceptor/delegation.py``); the
two agree on the wire format documented below but do not share code (different
deployment units, different KMS permission).

Wire format
-----------
``<b64url(header)>.<b64url(payload)>.<b64url(signature)>`` where

- ``header``  = ``{"alg": "KMS_ECDSA_SHA_256", "typ": "delegation-grant", "v": 1}``
- ``payload`` = ``{"actor_id", "plan_id", "iat", "exp"}`` (exp is unix seconds)
- ``signature`` = ``kms:Sign`` over the ASCII ``<header>.<payload>`` signing input

Padding is stripped from every base64url segment, matching JWT convention.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time

import boto3

logger = logging.getLogger(__name__)

# Env var carrying the KMS key id/ARN used to sign grants. Set on the Main
# Agent runtime only (the orchestrator). Absent => grants cannot be minted,
# which is a first-class, fail-closed state: option C rejects the plan rather
# than accepting one whose nodes could never authenticate.
SIGNING_KEY_ENV = "DELEGATION_SIGNING_KEY_ARN"

# Rollout gate. Minting is only *attempted* when this is truthy. It stays off
# until the async credential delivery path (the caller crossing the harness
# gateway's front door and forwarding the grant as a request header) is
# deployed — otherwise a minted grant would let option C stand down and the
# plan would be accepted only to stall, reintroducing the silent hang C exists
# to prevent. With the gate off, ``can_delegate`` is False, so C keeps
# rejecting user_jwt-gated plans loudly.
ENABLED_ENV = "DELEGATION_GRANTS_ENABLED"

# KMS signing algorithm. ECDSA over NIST P-256 keeps signatures small
# (DER-encoded, ~70 bytes) so the grant fits comfortably in a request header.
SIGNING_ALGORITHM = "ECDSA_SHA_256"

_HEADER = {"alg": "KMS_ECDSA_SHA_256", "typ": "delegation-grant", "v": 1}

# Grant lifetime. Tied to the PLAN's lifetime (the plan-state row uses a 24h
# TTL), NOT to the user's ~1h JWT — a long media DAG can outlive the login
# token by hours, so binding exp to the JWT would expire the grant mid-plan.
DEFAULT_TTL_SECONDS = int(os.environ.get("DELEGATION_GRANT_TTL_SECONDS", str(24 * 60 * 60)))

REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))

_kms = None


def _kms_client():
    """Lazily create the KMS client so import performs no I/O (test-friendly)."""
    global _kms
    if _kms is None:
        _kms = boto3.client("kms", region_name=REGION)
    return _kms


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def signing_key_arn() -> str:
    """The configured signing key id/ARN, or "" when unset."""
    return os.environ.get(SIGNING_KEY_ENV, "") or ""


def grants_enabled() -> bool:
    """Whether delegation-grant minting is switched on for this deployment."""
    return os.environ.get(ENABLED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def can_delegate(actor_id: str) -> bool:
    """Whether a usable grant can be minted for ``actor_id`` right now.

    True requires all of: the rollout gate on, a signing key configured, and a
    non-empty actor to name. Any missing precondition is a reason for option C
    to reject the plan rather than accept one that cannot run.
    """
    return bool(actor_id) and grants_enabled() and bool(signing_key_arn())


def mint_grant(
    actor_id: str,
    plan_id: str,
    *,
    ttl_seconds: int | None = None,
    key_id: str | None = None,
) -> str:
    """Mint a signed delegation grant over ``{actor_id, plan_id, exp}``.

    Raises on any failure (missing key, empty actor, KMS error) — the caller
    treats an exception the same as "no grant" and falls back to option C, so
    there is no path where a mint failure yields an unsigned/trusted token.
    """
    if not actor_id:
        raise ValueError("actor_id is required to mint a delegation grant")
    if not plan_id:
        raise ValueError("plan_id is required to mint a delegation grant")
    key_id = key_id or signing_key_arn()
    if not key_id:
        raise RuntimeError(f"{SIGNING_KEY_ENV} not configured; cannot mint grant")

    ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    now = int(time.time())
    payload = {
        "actor_id": actor_id,
        "plan_id": plan_id,
        "iat": now,
        "exp": now + int(ttl),
    }

    header_b64 = _b64url(json.dumps(_HEADER, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")

    resp = _kms_client().sign(
        KeyId=key_id,
        Message=signing_input,
        MessageType="RAW",
        SigningAlgorithm=SIGNING_ALGORITHM,
    )
    signature = resp["Signature"]
    sig_b64 = _b64url(signature)

    token = f"{header_b64}.{payload_b64}.{sig_b64}"
    logger.info(
        "delegation: minted grant for actor=%s plan=%s exp=%d",
        actor_id[:8] + "..." if actor_id else "-", plan_id[:8], payload["exp"],
    )
    return token
