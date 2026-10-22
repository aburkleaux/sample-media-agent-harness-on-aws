"""Gateway authorization — header assembly across the five auth modes.

Extracted from ``GatewayConnection`` (Wave 2 transport boundary). The auth-mode
branching (sigv4 / none / bearer / user_jwt-interactive / user_jwt-delegated /
default OAuth) lives here as a single pure, independently-testable unit. The
*stateful* pieces — OAuth token minting, its bounded retry, and the circuit
breaker — remain on the connection and are supplied via ``oauth_token_fn``.

Behavior is preserved exactly, including the SigV4 path whose static-body
signature is corrected in Wave 3 (this module is where that fix will land).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class GatewayAuthConfig:
    """Immutable snapshot of the auth-relevant connection fields."""

    auth_type: str
    url: str
    gateway_name: str
    sigv4_region: str = ""
    sigv4_service: str = ""
    bearer_token: str = ""
    header_name: str = "Authorization"
    value_prefix: str = "Bearer "
    user_token: str = ""
    delegation_grant: str = ""
    client_id: str = ""
    client_secret: str = ""
    token_endpoint: str = ""


def build_auth_headers(
    cfg: GatewayAuthConfig,
    *,
    oauth_token_fn: Callable[[], str],
    delegation_grant_header: str,
) -> dict:
    """Compute the base auth headers for a gateway connection by auth type.

    The caller merges the actor header on top. ``oauth_token_fn`` mints (and
    caches/retries) the machine token; it is only invoked on the OAuth and
    delegated paths, exactly as before.
    """
    if cfg.auth_type == "sigv4":
        # SigV4 gateway — sign with current AWS credentials.
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        import boto3 as _boto3

        _session = _boto3.Session(region_name=cfg.sigv4_region)
        _creds = _session.get_credentials().get_frozen_credentials()
        # NOTE: static-body signature preserved from the original; Wave 3
        # replaces this with per-request signing of the exact outgoing request.
        req = AWSRequest(
            method="POST",
            url=cfg.url,
            data="{}",
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(_creds, cfg.sigv4_service, cfg.sigv4_region).add_auth(req)
        return dict(req.headers)
    elif cfg.auth_type == "none":
        return {}
    elif cfg.auth_type == "bearer":
        if not cfg.bearer_token:
            raise RuntimeError(
                f"Gateway '{cfg.gateway_name}' connect failed: auth_type is "
                "'bearer' but no token was resolved (check token_arn "
                "and Secrets Manager access)"
            )
        return {cfg.header_name: f"{cfg.value_prefix}{cfg.bearer_token}"}
    elif cfg.auth_type == "user_jwt":
        if cfg.user_token:
            # Interactive path — a live user token always wins.
            return {"Authorization": f"Bearer {cfg.user_token}"}
        elif (
            cfg.delegation_grant
            and cfg.client_id
            and cfg.client_secret
            and cfg.token_endpoint
        ):
            # Async plan node (option E): machine token + signed delegation
            # grant; the interceptor derives the actor from the grant, and
            # fails closed if the grant header is stripped.
            token = oauth_token_fn()
            return {
                "Authorization": f"Bearer {token}",
                delegation_grant_header: cfg.delegation_grant,
            }
        else:
            raise RuntimeError(
                f"Gateway '{cfg.gateway_name}' connect failed: auth_type is "
                "'user_jwt' but no user token was forwarded (check the "
                "runtime's request header allowlist and the proxy)"
            )
    else:
        # JWT/OAuth (default)
        token = oauth_token_fn()
        return {"Authorization": f"Bearer {token}"}


__all__ = ["GatewayAuthConfig", "build_auth_headers"]
