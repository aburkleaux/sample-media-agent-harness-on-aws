"""The Capability API half of the ALB origin lock (security finding #2, interim).

The UiProxy ALB is internet-facing and its listener rule (priority 10) forwards ``/control-plane/*``
straight here, so this service must enforce the lock itself: the ALB's security group admits only
CloudFront's origin-facing prefix list — which is EVERY CloudFront distribution — and our distribution
adds ``X-Origin-Verify: <secret>`` (Secrets Manager, injected as ``ORIGIN_VERIFY_SECRET``). A request
without the right value is refused before routing. ``/health`` (the ALB target-group check) is exempt.
Unset secret (local runs, unit tests) → no enforcement. Mirrors ``ui/server/originVerify.ts``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Awaitable, Callable

ORIGIN_VERIFY_HEADER = b"x-origin-verify"
EXEMPT_PATHS = frozenset({"/health"})


def _matches(presented: bytes, expected: bytes) -> bool:
    return hmac.compare_digest(hashlib.sha256(presented).digest(), hashlib.sha256(expected).digest()) \
        and len(presented) == len(expected)


class OriginVerifyMiddleware:
    """Pure ASGI middleware (so it also covers 404s and FastAPI's own error paths)."""

    def __init__(self, app: Callable[..., Awaitable[Any]], secret: str | None = None) -> None:
        self.app = app
        value = os.environ.get("ORIGIN_VERIFY_SECRET", "") if secret is None else secret
        self.expected = value.strip().encode("utf-8")

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if not self.expected or scope.get("type") != "http" or scope.get("path") in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        presented = b""
        for name, value in scope.get("headers") or []:
            if name.lower() == ORIGIN_VERIFY_HEADER:
                presented = value
                break
        if presented and _matches(presented, self.expected):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"error": {"code": "origin_not_allowed", "message": "Use the CloudFront endpoint"}}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
