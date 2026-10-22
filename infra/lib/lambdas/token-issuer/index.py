"""Token_Issuer Lambda: first issuance and Token_Refresh of a Project_Token (design Component 3, D2, D15;
tasks 3.3, 3.4).

One code asset, two functions on one role: ``issue_handler`` (``TokenIssuerFn``) and ``refresh_handler``
(``TokenRefreshFn``, below ``make_refresh_handler``).

``issue_handler`` is ``TokenIssuerFn``'s handler, invoked only by the proxy task role. The event is
``{"userToken": "<Cognito access token>", "projectId": "p…"}``; every other key is ignored, so an actor id
in the event cannot select the Actor (Req 1.18). Responses:

- ``{"status": "ok", "token", "exp", "jti"}``: the User_Token verified and ``check_access`` passed.
- ``{"status": "not_found"}``: no access (absent, removed, deleted, tombstoned, or a malformed id). One shape
  for every denial (Req 1.17).
- ``{"status": "unauthenticated"}``: the User_Token failed verification. Nothing is read or signed.
- ``{"status": "error"}``: the issuer could not answer (KMS ``Sign`` failure, an incomplete or failed
  membership read, an unreachable JWKS, or missing configuration). Also writes a ``TokenIssuanceFailure``
  CloudWatch Embedded Metric Format line; the proxy treats ``error`` like a timeout (Req 4.17).

The handler logic lives in ``make_issue_handler(verifier, table, signer, iss, aud, lifetime)`` so a local
dev wrapper can reuse it with its own pieces (a moto table, a local RSA signer, and
``dev_login.DevLoginUserTokenVerifier``). ``issue_handler`` builds the production pieces lazily on first
invocation from ``PROJECTS_TABLE``, ``SIGNING_KEY_ID``, ``TOKEN_ISSUER_URL``, ``SEARCH_GATEWAY_AUDIENCE``,
``USER_POOL_ID``, ``USER_POOL_CLIENT_ID``, ``TOKEN_LIFETIME_SECONDS``, and ``AWS_REGION``, and always uses the
Cognito verifier. This module does not import ``dev_login``.

Logging: one JSON line per call with ``jti``, ``project_id``, ``sub``, and ``latency_ms``, never a token.
``project_id`` is logged only when it is well formed, so a caller cannot get arbitrary text (or a token
pasted into the wrong field) into the log.
"""

from __future__ import annotations

import functools
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any

import access
import cognito
import tokens

ISSUE_FAILURE_METRIC = "TokenIssuanceFailure"
REFRESH_FAILURE_METRIC = "TokenRefreshFailure"
METRICS_NAMESPACE_DEFAULT = "MediaworksHarness/Projects"

IssueHandler = Callable[[Any], dict[str, Any]]

_NOT_FOUND = {"status": "not_found"}
_UNAUTHENTICATED = {"status": "unauthenticated"}
_INVALID_TOKEN = {"status": "invalid_token"}
_ACCESS_ENDED = {"status": "access_ended"}
_ERROR = {"status": "error"}


def _write_line(obj: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def emit_failure_metric(name: str, stage: str, *, namespace: str | None = None) -> None:
    """Write one EMF line counting ``name``; ``stage`` names the failed step (never error text)."""
    _write_line(
        {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": namespace or os.environ.get("METRICS_NAMESPACE") or METRICS_NAMESPACE_DEFAULT,
                        "Dimensions": [[]],
                        "Metrics": [{"Name": name, "Unit": "Count"}],
                    }
                ],
            },
            name: 1,
            "stage": stage,
        }
    )


def _log_issue(*, jti: str | None, project_id: object, sub: str | None, started: float) -> None:
    _write_line(
        {
            "jti": jti,
            "project_id": project_id if tokens.is_project_id(project_id) else None,
            "sub": sub,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    )


def make_issue_handler(
    verifier: cognito.UserTokenVerifier,
    table: Any,
    signer: tokens.Signer,
    iss: str,
    aud: str,
    lifetime: int,
    *,
    clock: Callable[[], float] = time.time,
    emit_metric: Callable[[str, str], None] = emit_failure_metric,
) -> IssueHandler:
    """Build ``handler(event) -> response`` over injected pieces. See the module docstring."""
    if type(lifetime) is not int or not tokens.MIN_LIFETIME_S <= lifetime <= tokens.MAX_LIFETIME_S:
        raise ValueError(f"lifetime must be an integer in {tokens.MIN_LIFETIME_S}-{tokens.MAX_LIFETIME_S}")
    for name, value in (("iss", iss), ("aud", aud)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"make_issue_handler needs a non-empty {name}")

    def handler(event: Any) -> dict[str, Any]:
        started = time.perf_counter()
        body = event if isinstance(event, Mapping) else {}
        user_token = body.get("userToken")
        project_id = body.get("projectId")
        sub: str | None = None
        jti: str | None = None
        try:
            now = clock()
            try:
                claims = verifier.verify(user_token, now=now)
            except cognito.UserTokenInvalid:
                return dict(_UNAUTHENTICATED)
            except cognito.JwksUnavailable:
                emit_metric(ISSUE_FAILURE_METRIC, "jwks")
                return dict(_ERROR)
            sub = claims["sub"]  # the only identity input (Req 1.18)

            try:
                result = access.check_access(table, sub, project_id)
            except Exception:  # AccessCheckIncomplete or a DynamoDB error: no answer, not a denial
                emit_metric(ISSUE_FAILURE_METRIC, "access")
                return dict(_ERROR)
            if not result.ok:
                return dict(_NOT_FOUND)

            try:
                issued = tokens.issue(signer, iss=iss, aud=aud, sub=sub, project_id=project_id, now=now,
                                      lifetime=lifetime)
            except Exception:
                emit_metric(ISSUE_FAILURE_METRIC, "sign")
                return dict(_ERROR)
            jti = issued.jti
            return {"status": "ok", "token": issued.token, "exp": issued.exp, "jti": issued.jti}
        except Exception:
            emit_metric(ISSUE_FAILURE_METRIC, "internal")
            return dict(_ERROR)
        finally:
            _log_issue(jti=jti, project_id=project_id, sub=sub, started=started)

    return handler


# --------------------------------------------------------------------------------------------------
# Token_Refresh (task 3.4; design Component 3 "Token_Refresh", D15)
# --------------------------------------------------------------------------------------------------


def _log_refresh(
    *, prev_jti: object, jti: str | None, project_id: object, sub: object, outcome: str, started: float
) -> None:
    # Every identifier here comes from verified claims (or is None), never from the raw event.
    _write_line(
        {
            "prev_jti": prev_jti if isinstance(prev_jti, str) else None,
            "jti": jti,
            "project_id": project_id if tokens.is_project_id(project_id) else None,
            "sub": sub if isinstance(sub, str) else None,
            "outcome": outcome,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    )


def make_refresh_handler(
    verifier: tokens.Verifier,
    table: Any,
    signer: tokens.Signer,
    iss: str,
    aud: str,
    lifetime: int,
    *,
    clock: Callable[[], float] = time.time,
    emit_metric: Callable[[str, str], None] = emit_failure_metric,
) -> IssueHandler:
    """Build the Token_Refresh ``handler(event) -> response`` over injected pieces.

    ``verifier`` knows exactly the published key ids and verifies a signature (``tokens.KmsVerifier`` in
    Lambda, a local RSA verifier for a dev wrapper). The event is ``{"token": "<Project_Token>"}``;
    every other key is ignored, so the identity and Project come only from the verified token (Req 4.22).
    Responses: ``ok`` with ``token``/``exp``/``jti``, ``access_ended`` (verified, but ``check_access`` is
    not-found), ``invalid_token`` (any ``tokens.verify`` failure, including an ``exp`` more than
    ``REFRESH_GRACE_S`` in the past), or ``error`` plus a ``TokenRefreshFailure`` metric when the issuer
    could not answer (a ``kms:Verify``, DynamoDB, or ``Sign`` failure). Nothing is signed unless ``ok``.
    """
    if type(lifetime) is not int or not tokens.MIN_LIFETIME_S <= lifetime <= tokens.MAX_LIFETIME_S:
        raise ValueError(f"lifetime must be an integer in {tokens.MIN_LIFETIME_S}-{tokens.MAX_LIFETIME_S}")
    for name, value in (("iss", iss), ("aud", aud)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"make_refresh_handler needs a non-empty {name}")

    def handler(event: Any) -> dict[str, Any]:
        started = time.perf_counter()
        presented = event.get("token") if isinstance(event, Mapping) else None
        claims: Mapping[str, Any] = {}
        jti: str | None = None
        outcome = "error"
        try:
            now = clock()
            try:
                claims = tokens.verify(presented, verifier, iss, aud, now)
            except tokens.InvalidProjectToken:
                outcome = "invalid_token"
                return dict(_INVALID_TOKEN)
            except Exception:  # kms:Verify failed: the issuer has no answer, the token is not judged
                emit_metric(REFRESH_FAILURE_METRIC, "verify")
                return dict(_ERROR)
            sub = claims["sub"]
            project_id = claims["project_id"]

            try:
                result = access.check_access(table, sub, project_id)
            except Exception:  # AccessCheckIncomplete or a DynamoDB error: no answer, not access_ended
                emit_metric(REFRESH_FAILURE_METRIC, "access")
                return dict(_ERROR)
            if not result.ok:
                outcome = "access_ended"
                return dict(_ACCESS_ENDED)

            try:
                issued = tokens.issue(signer, iss=iss, aud=aud, sub=sub, project_id=project_id, now=now,
                                      lifetime=lifetime)
            except Exception:
                emit_metric(REFRESH_FAILURE_METRIC, "sign")
                return dict(_ERROR)
            jti = issued.jti
            outcome = "ok"
            return {"status": "ok", "token": issued.token, "exp": issued.exp, "jti": issued.jti}
        except Exception:
            emit_metric(REFRESH_FAILURE_METRIC, "internal")
            return dict(_ERROR)
        finally:
            _log_refresh(prev_jti=claims.get("jti"), jti=jti, project_id=claims.get("project_id"),
                         sub=claims.get("sub"), outcome=outcome, started=started)

    return handler


# --------------------------------------------------------------------------------------------------
# Delegated issuance (knowledge-graph tools on the Search_Gateway, for the Task Agent / plan nodes)
# --------------------------------------------------------------------------------------------------


def make_delegated_issue_handler(
    verify_grant: Callable[[object], str],
    table: Any,
    signer: tokens.Signer,
    iss: str,
    aud: str,
    lifetime: int,
    *,
    clock: Callable[[], float] = time.time,
    emit_metric: Callable[[str, str], None] = emit_failure_metric,
) -> IssueHandler:
    """``DelegatedTokenIssuerFn``: a Project_Token for a dispatched sub-agent, from a delegation grant.

    A Task Agent / plan node runs in another runtime, often long after the turn's browser token is gone, so it
    cannot use ``issue_handler``. It presents the delegation grant the Main_Agent minted for the actor (KMS-signed
    ``{actor_id, plan_id, exp}``). The event is ``{"grant": "<delegation grant>", "projectId": "p…"}``; every other
    key is ignored. The actor comes ONLY from the verified grant, membership is re-checked exactly like first
    issuance, and the token is the same Project_Token (same iss/aud/claims) — so the Search_Gateway's Cedar policy
    treats a sub-agent's graph call exactly like the Main_Agent's. Responses mirror ``issue_handler``:
    ``ok`` / ``not_found`` / ``unauthenticated`` (grant failed verification or expired) / ``error``.
    """
    if type(lifetime) is not int or not tokens.MIN_LIFETIME_S <= lifetime <= tokens.MAX_LIFETIME_S:
        raise ValueError(f"lifetime must be an integer in {tokens.MIN_LIFETIME_S}-{tokens.MAX_LIFETIME_S}")

    def handler(event: Any) -> dict[str, Any]:
        started = time.perf_counter()
        body = event if isinstance(event, Mapping) else {}
        project_id = body.get("projectId")
        sub: str | None = None
        jti: str | None = None
        try:
            sub = verify_grant(body.get("grant")) or None
            if not sub:
                return dict(_UNAUTHENTICATED)
            try:
                result = access.check_access(table, sub, project_id)
            except Exception:
                emit_metric(ISSUE_FAILURE_METRIC, "access")
                return dict(_ERROR)
            if not result.ok:
                return dict(_NOT_FOUND)
            try:
                issued = tokens.issue(signer, iss=iss, aud=aud, sub=sub, project_id=project_id, now=clock(),
                                      lifetime=lifetime)
            except Exception:
                emit_metric(ISSUE_FAILURE_METRIC, "sign")
                return dict(_ERROR)
            jti = issued.jti
            return {"status": "ok", "token": issued.token, "exp": issued.exp, "jti": issued.jti}
        except Exception:
            emit_metric(ISSUE_FAILURE_METRIC, "internal")
            return dict(_ERROR)
        finally:
            _log_issue(jti=jti, project_id=project_id, sub=sub, started=started)

    return handler


# --------------------------------------------------------------------------------------------------
# Lambda wiring (production pieces only)
# --------------------------------------------------------------------------------------------------


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is not set")
    return value


def _region() -> str:
    region = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "").strip()
    if not region:
        raise ValueError("AWS_REGION is not set")
    return region


def _kms_client(region: str) -> Any:
    import boto3

    return boto3.client("kms", region_name=region)


@functools.cache
def _production_issue_handler() -> IssueHandler:
    """Built once per container on first invocation. A configuration error is raised (not cached)."""
    region = _region()
    verifier = cognito.CognitoUserTokenVerifier(
        region=region,
        user_pool_id=_required_env("USER_POOL_ID"),
        client_id=_required_env("USER_POOL_CLIENT_ID"),
    )
    return make_issue_handler(
        verifier,
        access.open_table(_required_env("PROJECTS_TABLE"), region_name=region),
        tokens.KmsSigner(_kms_client(region), _required_env("SIGNING_KEY_ID")),
        _required_env("TOKEN_ISSUER_URL"),
        _required_env("SEARCH_GATEWAY_AUDIENCE"),
        tokens.configured_lifetime_seconds(),
    )


def issue_handler(event: Any, context: Any = None) -> dict[str, Any]:
    """``TokenIssuerFn`` entry point."""
    try:
        handler = _production_issue_handler()
    except Exception:
        emit_failure_metric(ISSUE_FAILURE_METRIC, "config")
        return dict(_ERROR)
    return handler(event)


@functools.cache
def _production_delegated_issue_handler() -> IssueHandler:
    import delegation

    region = _region()
    kms = _kms_client(region)
    return make_delegated_issue_handler(
        delegation.make_grant_verifier(kms, _required_env(delegation.KEY_ENV)),
        access.open_table(_required_env("PROJECTS_TABLE"), region_name=region),
        tokens.KmsSigner(kms, _required_env("SIGNING_KEY_ID")),
        _required_env("TOKEN_ISSUER_URL"),
        _required_env("SEARCH_GATEWAY_AUDIENCE"),
        tokens.configured_lifetime_seconds(),
    )


def delegated_issue_handler(event: Any, context: Any = None) -> dict[str, Any]:
    """``DelegatedTokenIssuerFn`` entry point (Task Agent runtime-only caller)."""
    try:
        handler = _production_delegated_issue_handler()
    except Exception:
        emit_failure_metric(ISSUE_FAILURE_METRIC, "config")
        return dict(_ERROR)
    return handler(event)


@functools.cache
def _production_refresh_handler() -> IssueHandler:
    """Built once per container on first invocation. The ``KmsVerifier`` lives in this closure and
    verifies each presented token with ``kms:Verify`` (no public key is fetched or held). A
    configuration error is raised (not cached)."""
    region = _region()
    kms = _kms_client(region)
    return make_refresh_handler(
        tokens.KmsVerifier(kms, tokens.configured_published_key_ids()),
        access.open_table(_required_env("PROJECTS_TABLE"), region_name=region),
        tokens.KmsSigner(kms, _required_env("SIGNING_KEY_ID")),
        _required_env("TOKEN_ISSUER_URL"),
        _required_env("SEARCH_GATEWAY_AUDIENCE"),
        tokens.configured_lifetime_seconds(),
    )


def refresh_handler(event: Any, context: Any = None) -> dict[str, Any]:
    """``TokenRefreshFn`` entry point."""
    try:
        handler = _production_refresh_handler()
    except Exception:
        emit_failure_metric(REFRESH_FAILURE_METRIC, "config")
        return dict(_ERROR)
    return handler(event)


def _prewarm() -> None:
    """Build this function's handler during Lambda init, not on the first call.

    The init phase runs at full CPU and before the proxy's 2 s budget starts, so the client setup no
    longer lands inside the first chat turn after an idle period (session 66f2b85b: a cold issuance took
    6.8 s and that turn ran without search). Only in Lambda, only the handler this function serves, and
    never fatal: a failure here leaves the lazy path to report it on the first call as before.
    """
    target = os.environ.get("_HANDLER", "")
    if not os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or not target:
        return
    try:
        if target.endswith("delegated_issue_handler"):
            _production_delegated_issue_handler()
        elif target.endswith("issue_handler"):
            _production_issue_handler()
        elif target.endswith("refresh_handler"):
            _production_refresh_handler()
    except Exception:  # noqa: BLE001 - not cached; the first call retries and reports
        pass


_prewarm()
