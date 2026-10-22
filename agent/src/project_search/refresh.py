"""Token_Refresh client (design Component 7 `refresh.py`, Req 4.20–4.27).

A boto3 Lambda Invoke of PROJECT_TOKEN_REFRESH_FN, run off the event loop. botocore retries are off:
the single retry belongs to ProjectTokenRef.

DEV ONLY: when `LOCAL_TOKEN_REFRESH_URL` is set (e.g. `http://127.0.0.1:8098`), `default_refresher()`
POSTs the same `{"token": raw}` event as JSON to `${LOCAL_TOKEN_REFRESH_URL}/refresh` instead, for the
local wrapper around the issuer's `make_refresh_handler` (`agent/scripts/local_projects_stack.py`). Same
2 s budget, same status mapping, no retries. Only a loopback http(s) URL is accepted, so a stray setting
cannot send a Project_Token off the machine; anything else yields no refresher (as if unset). The Lambda
path is unchanged and is used whenever the local URL is absent.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

from .token_ref import RefreshOutcome

_client = None

LOCAL_TOKEN_REFRESH_URL_ENV = "LOCAL_TOKEN_REFRESH_URL"
LOCAL_REFRESH_TIMEOUT_S = 2.0
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_RESPONSE_BYTES = 64 * 1024


def _lambda():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config
        _client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "us-west-2"),
                               config=Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 0}))
    return _client


def _map_body(body: object) -> RefreshOutcome:
    """The issuer's `{status: ...}` body -> outcome. Shared by the Lambda and local paths."""
    if not isinstance(body, dict):
        return RefreshOutcome.failed("error")
    status = body.get("status")
    if status == "ok" and isinstance(body.get("token"), str):
        return RefreshOutcome.ok(body["token"])
    if status == "access_ended":
        return RefreshOutcome.access_ended()
    return RefreshOutcome.failed("invalid_token" if status == "invalid_token" else "error")


def _invoke(function_name: str, raw: str) -> RefreshOutcome:
    try:
        resp = _lambda().invoke(FunctionName=function_name, Payload=json.dumps({"token": raw}).encode())
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        return RefreshOutcome.failed("throttled" if "Throttl" in name or "TooManyRequests" in name
                                     else "timeout" if "Timeout" in name else "error")
    if resp.get("FunctionError"):
        return RefreshOutcome.failed("error")
    try:
        body = json.loads(resp["Payload"].read())
    except Exception:  # noqa: BLE001
        return RefreshOutcome.failed("error")
    return _map_body(body)


class TokenRefreshClient:
    def __init__(self, function_name: str | None = None):
        self.function_name = function_name or os.environ.get("PROJECT_TOKEN_REFRESH_FN", "")

    async def __call__(self, raw: str) -> RefreshOutcome:
        if not self.function_name:
            return RefreshOutcome.failed("error")
        return await asyncio.to_thread(_invoke, self.function_name, raw)


# ---- DEV ONLY: local Token_Refresh over HTTP ---------------------------------------------------

def local_refresh_endpoint(base: str | None) -> str | None:
    """`${base}/refresh` for a loopback http(s) base URL, else None."""
    base = (base or "").strip()
    if not base:
        return None
    try:
        parsed = urllib.parse.urlsplit(base)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "") not in _LOOPBACK_HOSTS:
        return None
    return base.rstrip("/") + "/refresh"


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (socket.timeout, TimeoutError))


def _post_local(url: str, raw: str, timeout: float) -> RefreshOutcome:
    req = urllib.request.Request(url, data=json.dumps({"token": raw}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (loopback only)
            payload = resp.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:  # non-2xx: the wrapper had no answer
        return RefreshOutcome.failed("throttled" if exc.code == 429 else "error")
    except Exception as exc:  # noqa: BLE001
        return RefreshOutcome.failed("timeout" if _is_timeout(exc) else "error")
    if len(payload) > _MAX_RESPONSE_BYTES:
        return RefreshOutcome.failed("error")
    try:
        body = json.loads(payload)
    except Exception:  # noqa: BLE001
        return RefreshOutcome.failed("error")
    return _map_body(body)


class LocalTokenRefreshClient:
    """DEV ONLY refresher: POST `{"token": raw}` to a local `/refresh`."""

    def __init__(self, url: str, timeout: float = LOCAL_REFRESH_TIMEOUT_S):
        self.url, self.timeout = url, timeout

    async def __call__(self, raw: str) -> RefreshOutcome:
        return await asyncio.to_thread(_post_local, self.url, raw, self.timeout)


def default_refresher():
    """The deployed refresher, the DEV ONLY local one when `LOCAL_TOKEN_REFRESH_URL` is set, or None
    where Token_Refresh is not configured."""
    if os.environ.get(LOCAL_TOKEN_REFRESH_URL_ENV):
        url = local_refresh_endpoint(os.environ.get(LOCAL_TOKEN_REFRESH_URL_ENV))
        return LocalTokenRefreshClient(url) if url else None
    return TokenRefreshClient() if os.environ.get("PROJECT_TOKEN_REFRESH_FN") else None
