"""Polling-worker MCP gateway invocation helper.

Resolves a gateway descriptor from the gateway-registry table, performs
the OAuth client-credentials handshake against the gateway's IdP, and
issues an MCP ``tools/call`` over HTTP. Stdlib only — ``urllib.request``
+ ``json`` plus ``boto3`` for DDB / Secrets Manager.

Why not reuse the agent runtime's ``MCPManager``:
    The polling worker is a separate Lambda artifact; bundling
    ``agent/src`` would drag in Strands, the MCP SDK, and httpx. The
    polling cycle only needs one verb (``tools/call`` against the
    status tool), so a hand-rolled JSON-RPC client over ``urllib`` is
    smaller than the dependency surface of ``streamable_http`` and
    avoids a long-lived MCP session.

The OAuth token cache is module-global so consecutive jobs against the
same gateway share a single token until expiry. The polling worker
runs as a fresh Lambda invocation every minute by default, so the cache
naturally resets between ticks; within a single invocation it pays
once per gateway rather than once per job.

Actor identity passthrough (R11.3): the job's ``actorId`` is included
in the ``tools/call`` arguments under ``__actor_id``, mirroring how the
proxy stamps the actor onto AgentCore Runtime payloads (see
``agent/src/main.py::_resolve_actor_id``). Status tools that don't
honour the field ignore it; tools that do see the original Actor's id
rather than the polling worker's role identity.

Requirements: 4.1, 4.2, 11.3
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

# Same partition value as the runtime's gateway registry — matches the
# constant ``GATEWAY_PK`` in ``agent/src/gateway_registry.py``. Inlined
# here to keep this Lambda independent of the agent package.
_GATEWAY_PK = "GATEWAY#REGISTERED"

# Refresh OAuth tokens 5 minutes before the IdP-reported expiry so a
# token doesn't expire mid-call. Same skew the agentcore-proxy uses.
_TOKEN_REFRESH_SKEW_SECONDS = 300


class GatewayInvocationError(Exception):
    """Raised when an MCP gateway invocation cannot complete.

    The polling worker's ``reconcile`` translates these into the
    transient/non-transient classification by inspecting the wrapped
    cause's class name (``ThrottlingException``, ``ServiceUnavailable``,
    ``TimeoutError``). Subclasses below tag the cases that are always
    one or the other so the classifier doesn't have to guess.
    """


class TransientGatewayError(GatewayInvocationError):
    """The error is expected to clear on the next polling tick (R4.4)."""


class GatewayDescriptorMissing(GatewayInvocationError):
    """The gateway-registry has no row for the gateway id on this job.

    Not transient: a missing descriptor means the trio's gateway was
    deleted while the job was in flight. Treated as non-transient so
    it counts toward the threshold and eventually fails the job (R4.5).
    """


class _TokenCache:
    """Per-gateway OAuth bearer token cache.

    Keyed by ``(token_endpoint, client_id, scope)`` so two trios on
    the same IdP but different scopes don't collide. Locked because a
    future parallelized polling worker may dispatch concurrent jobs to
    the same gateway in one tick.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str, str], tuple[str, float]] = {}

    def get(self, key: tuple[str, str, str]) -> str | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            token, expires_at = entry
            if time.time() >= expires_at:
                return None
            return token

    def put(self, key: tuple[str, str, str], token: str, expires_in: int) -> None:
        with self._lock:
            self._cache[key] = (
                token,
                time.time() + max(0, expires_in - _TOKEN_REFRESH_SKEW_SECONDS),
            )


_TOKEN_CACHE = _TokenCache()


# ---------------------------------------------------------------------------
# Gateway descriptor lookup
# ---------------------------------------------------------------------------


# Machine client for reaching a user_jwt gateway, and the grant header the interceptor
# reads. Same mechanism the polling worker uses: the token gets past the front door, and
# WHOSE work this is comes from the signed delegation grant, never from the token (which
# has no `sub`).
ASYNC_PLAN_TOKEN_ENDPOINT = os.environ.get("ASYNC_PLAN_TOKEN_ENDPOINT", "")
ASYNC_PLAN_CLIENT_ID = os.environ.get("ASYNC_PLAN_CLIENT_ID", "")
ASYNC_PLAN_CLIENT_SECRET = os.environ.get("ASYNC_PLAN_CLIENT_SECRET", "")
ASYNC_PLAN_SCOPE = os.environ.get("ASYNC_PLAN_SCOPE", "")
DELEGATION_GRANT_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-DelegationGrant"

_GATEWAY_NAMESPACE_SEPARATOR = "___"
# Results tools taking a queue-shaped argument set rather than {job_id, __actor_id}.
_QUEUE_RESULT_TOOLS = frozenset({"get_job_result"})

# Generic names a queue server MAY use for a results tool. Unlike get_job_result
# (fal's own, unused elsewhere here), these are plausible trio names too, so they
# only take the queue shape when the job carries an endpointId -- which only the
# queue detector records, and which a queue results call requires regardless.
_AMBIGUOUS_QUEUE_RESULT_TOOLS = frozenset({"get_job_results", "job_result"})


def _resolve_gateway_descriptor(
    gateway_id: str,
    *,
    gateway_registry_table: Any,
) -> dict[str, Any]:
    """Read one gateway descriptor row from the gateway-registry table.

    Returns the raw DDB item rather than a typed ``GatewayDescriptor``
    so this module stays independent of ``agent/src/gateway_registry``.
    Raises :class:`GatewayDescriptorMissing` if the row doesn't exist.
    """
    resp = gateway_registry_table.get_item(
        Key={"PK": _GATEWAY_PK, "SK": gateway_id}
    )
    item = resp.get("Item")
    if not item:
        raise GatewayDescriptorMissing(
            f"gateway descriptor not found for id={gateway_id!r}"
        )
    return item


# ---------------------------------------------------------------------------
# OAuth handshake
# ---------------------------------------------------------------------------


def _resolve_client_secret(secrets_client: Any, secret_arn: str) -> str:
    """Pull the OAuth client secret literal out of Secrets Manager.

    Two shapes, as ``agent/src/mcp_manager._fetch_secret_string`` accepts: a bare string (the raw
    secret), or a JSON bundle carrying ``clientSecret`` / ``client_secret`` -- the M2C
    ``media-workflow-gateway`` credentials ``{endpointUrl, tokenEndpoint, clientId, clientSecret,
    scope}``, which that gateway's registry row points ``client_secret_arn`` at directly. Sending the
    whole bundle as the secret fails the token exchange.
    """
    resp = secrets_client.get_secret_value(SecretId=secret_arn)
    secret = resp.get("SecretString")
    if not secret:
        raise GatewayInvocationError(
            f"empty SecretString for {secret_arn!r}"
        )
    try:
        data = json.loads(secret)
    except (ValueError, TypeError):
        return secret
    if isinstance(data, dict):
        for key in ("clientSecret", "client_secret"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
    return secret


def _fetch_oauth_token(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    scope: str,
) -> str:
    """OAuth2 client_credentials grant via stdlib ``urllib``."""
    cache_key = (token_endpoint, client_id, scope)
    cached = _TOKEN_CACHE.get(cache_key)
    if cached is not None:
        return cached

    body = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        token_endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 — token endpoint URL comes from the gateway descriptor (admin-controlled), not user input
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise TransientGatewayError(f"OAuth token endpoint unreachable: {exc}") from exc

    token = payload.get("access_token")
    if not token:
        raise GatewayInvocationError(
            f"OAuth response missing access_token: {payload}"
        )
    expires_in = int(payload.get("expires_in", 3600))
    _TOKEN_CACHE.put(cache_key, token, expires_in)
    return token


# ---------------------------------------------------------------------------
# MCP tools/call dispatch
# ---------------------------------------------------------------------------


def _http_json_post(
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout: int = 30,
) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    """POST a JSON body and return ``(status, response_headers, parsed_body)``.

    A non-2xx response still returns the parsed body where possible so
    the caller can inspect a JSON-RPC error envelope. Raises
    :class:`TransientGatewayError` on connection-level failures so the
    classifier hits the transient branch.
    """
    encoded = json.dumps(body).encode("utf-8")
    merged_headers = {"Content-Type": "application/json", "Accept": "application/json", **headers}
    req = urllib.request.Request(url, data=encoded, method="POST", headers=merged_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 — gateway URL comes from the gateway descriptor (admin-controlled)
            raw = resp.read()
            return (
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                json.loads(raw.decode("utf-8")) if raw else None,
            )
    except urllib.error.HTTPError as exc:
        # 5xx and 429 are mapped to transient; the JSON-RPC layer will
        # surface a typed cause for callers that need finer detail.
        body_bytes = exc.read() if exc.fp else b""
        body_text = body_bytes.decode("utf-8", errors="replace")
        if exc.code == 429 or exc.code >= 500:
            raise TransientGatewayError(
                f"gateway returned {exc.code}: {body_text[:200]}"
            ) from exc
        raise GatewayInvocationError(
            f"gateway returned {exc.code}: {body_text[:200]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TransientGatewayError(f"gateway unreachable: {exc}") from exc


def _tool_arguments(*, tool_name: str, job_id: str, actor_id: str,
                    endpoint_id: str = "") -> dict[str, Any]:
    """Arguments for an MCP tool call, shaped for the tool being invoked.

    Default is ``{job_id, __actor_id}``; ``__actor_id`` forwards the job's real actor so
    the gateway's downstream authorization does not see this Lambda's role (R11.3).

    A queue results tool needs ``{request_id, endpoint_id}`` and its schema forbids
    additional properties, so the default shape fails validation outright. Dropping
    ``__actor_id`` there is safe: that path carries a signed delegation grant from which
    the interceptor derives a VERIFIED actor, which is stronger than an asserted field.
    """
    bare = tool_name.split(_GATEWAY_NAMESPACE_SEPARATOR)[-1]
    is_queue = bare in _QUEUE_RESULT_TOOLS or (
        bool(endpoint_id) and bare in _AMBIGUOUS_QUEUE_RESULT_TOOLS
    )
    if is_queue:
        args: dict[str, Any] = {"request_id": job_id}
        if endpoint_id:
            args["endpoint_id"] = endpoint_id
        return args
    return {"job_id": job_id, "__actor_id": actor_id}


def invoke_mcp_tool(
    *,
    gateway_id: str,
    tool_name: str,
    job_id: str,
    actor_id: str,
    gateway_registry_table: Any,
    secrets_client: Any,
    delegation_grant: str = "",
    endpoint_id: str = "",
) -> dict[str, Any]:
    """Invoke ``tool_name(job_id=..., __actor_id=...)`` on the gateway.

    Generic MCP tool call — used for both status tools (by the polling
    worker) and results tools (by the Plan Chainer's direct results-fetch,
    which bypasses the LLM entirely for this mechanical step). Returns the
    parsed MCP tool result body. Raises :class:`GatewayInvocationError`
    (or a subclass) on any failure.

    Forwards the job's ``actor_id`` as ``__actor_id`` in the tool args
    (R11.3) so downstream gateway-side authorization sees the original
    Actor identity rather than the calling Lambda's role identity.
    """
    descriptor = _resolve_gateway_descriptor(
        gateway_id, gateway_registry_table=gateway_registry_table
    )

    auth_type = descriptor.get("auth_type", "none")
    auth_config = descriptor.get("auth_config", {}) or {}

    # The ``Authorization`` header construction varies by ``auth_type``.
    # Only JWT is wired here because every reference gateway in the
    # codebase (including the audiostem trio's MCP target) uses
    # OAuth2/JWT. SigV4 and ``none`` cases are documented but raise so
    # a misconfigured trio fails loudly rather than silently sending
    # unsigned requests.
    if auth_type == "jwt":
        token_endpoint = auth_config.get("token_endpoint", "")
        client_id = auth_config.get("client_id", "")
        scope = auth_config.get("scope", "")
        secret_arn = auth_config.get("client_secret_arn")
        if not (token_endpoint and client_id and secret_arn):
            raise GatewayInvocationError(
                f"gateway {gateway_id!r} has incomplete jwt auth_config"
            )
        client_secret = _resolve_client_secret(secrets_client, secret_arn)
        token = _fetch_oauth_token(
            token_endpoint=token_endpoint,
            client_id=client_id,
            client_secret=client_secret,
            scope=scope,
        )
        auth_headers = {"Authorization": f"Bearer {token}"}
    elif auth_type == "user_jwt":
        # No user is present when results are fetched: the turn ended and its JWT is
        # unpersistable and short-lived. Authenticate as a MACHINE and prove whose work
        # this is with the plan's signed delegation grant -- identical to the polling
        # worker's path. Without this the fetch failed with "unsupported gateway
        # auth_type 'user_jwt'" and the node stayed in results_ready forever, so the
        # dependent node was never dispatched.
        if not delegation_grant:
            raise GatewayInvocationError(
                f"gateway {gateway_id!r} is user_jwt but no delegation grant was supplied"
            )
        if not (ASYNC_PLAN_TOKEN_ENDPOINT and ASYNC_PLAN_CLIENT_ID and ASYNC_PLAN_CLIENT_SECRET):
            raise GatewayInvocationError(
                f"gateway {gateway_id!r} is user_jwt but no machine client is configured"
            )
        token = _fetch_oauth_token(
            token_endpoint=ASYNC_PLAN_TOKEN_ENDPOINT,
            client_id=ASYNC_PLAN_CLIENT_ID,
            client_secret=ASYNC_PLAN_CLIENT_SECRET,
            scope=ASYNC_PLAN_SCOPE,
        )
        auth_headers = {
            "Authorization": f"Bearer {token}",
            DELEGATION_GRANT_HEADER: delegation_grant,
        }
    else:
        # TODO(async-job-tracking, post-7.1): wire SigV4 and ``none``
        # auth types when a non-JWT MCP gateway joins the catalog.
        # Until then, classify as non-transient so the threshold trips
        # and the affected jobs FAIL rather than spinning forever.
        raise GatewayInvocationError(
            f"unsupported gateway auth_type {auth_type!r}"
        )

    url = descriptor.get("url")
    if not url:
        raise GatewayInvocationError(
            f"gateway descriptor {gateway_id!r} missing url"
        )

    # MCP requires an ``initialize`` -> ``notifications/initialized``
    # -> ``tools/call`` triplet on a fresh session. The gateway returns
    # a session id in the ``mcp-session-id`` response header on
    # ``initialize``; subsequent requests carry it as a request header.
    session_headers: dict[str, str] = {}

    init_status, init_resp_headers, _ = _http_json_post(
        url,
        {
            "jsonrpc": "2.0",
            "id": "init-1",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "polling-worker",
                    "version": "1.0.0",
                },
            },
        },
        headers=auth_headers,
    )
    if init_status != 200:
        raise GatewayInvocationError(
            f"MCP initialize returned status {init_status}"
        )
    session_id = init_resp_headers.get("mcp-session-id", "")
    if session_id:
        session_headers["mcp-session-id"] = session_id

    # The ``initialized`` notification has no response body; we ignore
    # the parse result.
    _http_json_post(
        url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**auth_headers, **session_headers},
    )

    call_status, _, call_body = _http_json_post(
        url,
        {
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": _tool_arguments(
                    tool_name=tool_name,
                    job_id=job_id,
                    actor_id=actor_id,
                    endpoint_id=endpoint_id,
                ),
            },
        },
        headers={**auth_headers, **session_headers},
    )
    if call_status != 200 or not isinstance(call_body, dict):
        raise GatewayInvocationError(
            f"MCP tools/call returned status {call_status}"
        )

    # JSON-RPC error envelope -> non-transient unless the message text
    # carries a known transient marker.
    if "error" in call_body:
        err = call_body["error"]
        message = (err or {}).get("message", str(err))
        if any(marker in message for marker in ("Throttling", "ServiceUnavailable", "Timeout")):
            raise TransientGatewayError(message)
        raise GatewayInvocationError(message)

    result = call_body.get("result")
    if not isinstance(result, dict):
        raise GatewayInvocationError(
            f"MCP tools/call response missing result dict: {call_body!r}"
        )

    # MCP ``tools/call`` results carry ``content`` as a list of typed
    # blocks. Status tools by convention return a single ``text`` block
    # whose body is JSON; parse it so ``reconcile`` can read the
    # ``status`` field directly. If the tool returns the result as a
    # ``structuredContent`` field instead (newer MCP), prefer that.
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]

    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                try:
                    parsed = json.loads(block["text"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(parsed, dict):
                    return parsed

    # No structured content and no JSON-shaped text block. Surface the
    # raw result so the caller can decide whether the missing
    # ``status`` field is a transient parsing miss or a permanent
    # contract break.
    return result
