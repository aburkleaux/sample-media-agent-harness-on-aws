"""M2C gateway client for the Ingestion_Pipeline (design Component 9).

Stateless JSON-RPC ``tools/call`` / ``tools/list`` over ``urllib`` with the environment's M2C OAuth client
(client credentials), the pattern of ``infra/lib/lambdas/polling-worker/gateway_invoker.py``. No MCP
session and no ``mcp`` SDK, so the Lambda package stays stdlib + boto3.

Errors are typed so the reducer can apply the retry policy of Req 4.20 / 7.8 without parsing text:

- :class:`Transient` — HTTP 429/5xx, connection errors, the per-call timeout. Retried by policy.
- :class:`ToolError` — the tool refused or failed: ``isError: true``, or M2C's own
  ``{"error": true, "errorCode", "message"}`` body. ``code`` carries ``errorCode`` (e.g. ``INVALID_PARAMETER``).
- :class:`Validation` — the gateway refused the request itself (a JSON-RPC error, a 4xx). Not retried.

The token is cached per container until 300 s before expiry. ``tools/list`` is cached for 5 minutes,
on success only (design Component 9, catalog).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

CALL_TIMEOUT_S = 60
TOKEN_SKEW_S = 300
TOOLS_CACHE_S = 300
_PROTOCOL_VERSION = "2025-03-26"


class M2CError(Exception):
    """Base of every M2C client failure."""


class Transient(M2CError):
    """Expected to clear on a later attempt (throttle, 5xx, connection, timeout)."""


class Validation(M2CError):
    """The gateway refused the request (JSON-RPC error or a non-retryable HTTP status)."""


class ToolError(M2CError):
    """The tool answered ``isError: true``. ``code`` is a JSON ``error.code``/``code`` when the text has one."""

    def __init__(self, text: str, code: str = ""):
        super().__init__(text)
        self.text = text
        self.code = code


@dataclass(frozen=True)
class M2CConfig:
    gateway_url: str
    client_id: str
    token_endpoint: str
    secret_arn: str
    scope: str = "media-workflows/invoke"

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "M2CConfig":
        e = os.environ if env is None else env
        cfg = cls(
            gateway_url=e.get("M2C_GATEWAY_URL", ""),
            client_id=e.get("M2C_CLIENT_ID", ""),
            token_endpoint=e.get("M2C_TOKEN_ENDPOINT", ""),
            secret_arn=e.get("M2C_SECRET_ARN", ""),
            scope=e.get("M2C_SCOPE", "") or "media-workflows/invoke",
        )
        missing = [k for k, v in (("M2C_GATEWAY_URL", cfg.gateway_url), ("M2C_CLIENT_ID", cfg.client_id),
                                  ("M2C_TOKEN_ENDPOINT", cfg.token_endpoint),
                                  ("M2C_SECRET_ARN", cfg.secret_arn)) if not v]
        if missing:
            raise M2CError(f"M2C is not configured: missing {', '.join(missing)}")
        return cfg


def client_secret_from(secret_string: str) -> str:
    """The M2C credentials secret is JSON ``{clientId, clientSecret, ...}``; a bare literal is accepted too."""
    try:
        parsed = json.loads(secret_string)
    except ValueError:
        return secret_string
    if isinstance(parsed, dict):
        return str(parsed.get("clientSecret") or parsed.get("client_secret") or "")
    return secret_string


def parse_tool_result(result: Any) -> dict:
    """A ``tools/call`` result → the tool's JSON object. Raises :class:`ToolError` for ``isError``."""
    if not isinstance(result, dict):
        raise Validation(f"tools/call result is not an object: {str(result)[:200]}")
    text = "\n".join(b.get("text", "") for b in result.get("content") or []
                     if isinstance(b, dict) and b.get("type") == "text")
    if result.get("isError"):
        code = ""
        try:
            body = json.loads(text)
            if isinstance(body, dict):
                err = body.get("error")
                code = str((err.get("code") if isinstance(err, dict) else None)
                           or body.get("errorCode") or body.get("code") or "")
                text = str(body.get("message") or text)
        except ValueError:
            pass
        raise ToolError(text or "tool reported an error with no text", code)
    if isinstance(result.get("structuredContent"), dict):
        body = result["structuredContent"]
        if body.get("error") is True:
            raise ToolError(str(body.get("message") or text), str(body.get("errorCode") or ""))
        return body
    try:
        body = json.loads(text)
    except ValueError:
        raise Validation(f"tool result is not JSON: {text[:200]}") from None
    if not isinstance(body, dict):
        raise Validation(f"tool result is not a JSON object: {text[:200]}")
    # M2C reports its own refusals as a NORMAL result, not isError (measured on the staging gateway):
    #   {"error": true, "errorCode": "INVALID_PARAMETER", "message": "asset_s3uri must be a valid S3 URI ..."}
    if body.get("error") is True:
        raise ToolError(str(body.get("message") or text), str(body.get("errorCode") or ""))
    return body


def _parse_body(raw: bytes, content_type: str) -> dict:
    """The gateway answers JSON, or SSE when it chooses to stream; take the JSON-RPC message from either."""
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" in content_type:
        msg = None
        for line in text.split("\n"):
            if line.startswith("data:"):
                try:
                    cand = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(cand, dict) and ("result" in cand or "error" in cand):
                    msg = cand
        if msg is None:
            raise Validation("gateway stream carried no JSON-RPC response")
        return msg
    try:
        body = json.loads(text)
    except ValueError:
        raise Validation(f"gateway answered non-JSON: {text[:200]}") from None
    if not isinstance(body, dict):
        raise Validation("gateway answered a non-object")
    return body


class M2CClient:
    def __init__(self, cfg: M2CConfig, *, secrets: Any = None,
                 opener: Callable[..., Any] = urllib.request.urlopen,
                 clock: Callable[[], float] = time.time, timeout_s: int = CALL_TIMEOUT_S):
        self.cfg = cfg
        self._secrets = secrets
        self._open = opener
        self._clock = clock
        self._timeout = timeout_s
        self._lock = threading.Lock()
        self._token: Optional[tuple[str, float]] = None
        self._tools: Optional[tuple[frozenset, float]] = None
        self._ids = 0

    # ------------------------------------------------------------------ auth
    def _client_secret(self) -> str:
        if self._secrets is None:
            import boto3
            # The ARN names the secret's region; a client in any other region cannot find it.
            parts = self.cfg.secret_arn.split(":")
            region = parts[3] if len(parts) > 3 and parts[3] else None
            self._secrets = boto3.client("secretsmanager", region_name=region)
        raw = self._secrets.get_secret_value(SecretId=self.cfg.secret_arn).get("SecretString") or ""
        secret = client_secret_from(raw)
        if not secret:
            raise M2CError("M2C credentials secret carries no client secret")
        return secret

    def token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._token[1]:
                return self._token[0]
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials", "client_id": self.cfg.client_id,
            "client_secret": self._client_secret(), "scope": self.cfg.scope}).encode()
        req = urllib.request.Request(self.cfg.token_endpoint, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        status, _, raw = self._send(req)
        if status != 200:
            raise (Transient if status == 429 or status >= 500 else M2CError)(
                f"M2C token endpoint returned {status}")
        payload = json.loads(raw.decode("utf-8"))
        tok = payload.get("access_token")
        if not tok:
            raise M2CError("M2C token response has no access_token")
        exp = self._clock() + max(0, int(payload.get("expires_in", 3600)) - TOKEN_SKEW_S)
        with self._lock:
            self._token = (tok, exp)
        return tok

    # ------------------------------------------------------------- transport
    def _send(self, req: urllib.request.Request) -> tuple[int, str, bytes]:
        try:
            with self._open(req, timeout=self._timeout) as resp:  # nosec B310 — URL from deploy config
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, (exc.headers.get("Content-Type", "") if exc.headers else ""), \
                (exc.read() if exc.fp else b"")
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            raise Transient(f"M2C unreachable: {type(exc).__name__}: {exc}") from exc

    def _rpc(self, method: str, params: dict) -> Any:
        with self._lock:
            self._ids += 1
            rid = self._ids
        body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.cfg.gateway_url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token()}", "MCP-Protocol-Version": _PROTOCOL_VERSION})
        status, ctype, raw = self._send(req)
        if status == 401:
            with self._lock:
                self._token = None  # a revoked/rotated token: the next attempt fetches a fresh one
            raise Transient("M2C gateway returned 401")
        if status == 429 or status >= 500:
            raise Transient(f"M2C gateway returned {status}: {raw[:200].decode('utf-8', 'replace')}")
        if status >= 400:
            raise Validation(f"M2C gateway returned {status}: {raw[:300].decode('utf-8', 'replace')}")
        msg = _parse_body(raw, ctype)
        if "error" in msg:
            err = msg.get("error") or {}
            text = str(err.get("message") if isinstance(err, dict) else err)
            if any(m in text for m in ("Throttl", "TooManyRequests", "ServiceUnavailable", "Timeout")):
                raise Transient(text)
            raise Validation(text)
        return msg.get("result")

    # ---------------------------------------------------------------- public
    def call_tool(self, name: str, arguments: dict) -> dict:
        return parse_tool_result(self._rpc("tools/call", {"name": name, "arguments": arguments}))

    def list_tools(self) -> frozenset:
        """Every tool name on the gateway (all pages). Cached 5 minutes on success only."""
        now = self._clock()
        with self._lock:
            if self._tools and now < self._tools[1]:
                return self._tools[0]
        names: list[str] = []
        cursor: Optional[str] = None
        for _ in range(100):
            res = self._rpc("tools/list", {"cursor": cursor} if cursor else {}) or {}
            names += [t.get("name", "") for t in res.get("tools") or [] if isinstance(t, dict)]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        out = frozenset(n for n in names if n)
        with self._lock:
            self._tools = (out, self._clock() + TOOLS_CACHE_S)
        return out
