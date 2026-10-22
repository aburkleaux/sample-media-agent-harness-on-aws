"""The Capability API enforces the ALB origin lock itself (``/control-plane/*`` never passes the proxy).

With ``ORIGIN_VERIFY_SECRET`` set, every request except ``/health`` must carry our CloudFront
distribution's ``X-Origin-Verify`` value — routed or not, authenticated or not.

Driven as raw ASGI (the middleware is framework-free), so it runs in CI's agent-unit job, which does
not install fastapi.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from capability_api.origin_verify import OriginVerifyMiddleware  # noqa: E402

SECRET = "s3cr3t-from-secrets-manager-0123456789abcdef"


async def _inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _call(mw, path, headers=None, scope_type="http"):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        sent.append(msg)

    scope = {"type": scope_type, "path": path,
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    asyncio.run(mw(scope, receive, send))
    return sent[0]["status"] if sent else None, sent


@pytest.mark.parametrize("path", ["/control-plane/config", "/control-plane/capabilities/skill", "/nope", "/healthz"])
def test_refused_without_the_header(path):
    status, sent = _call(OriginVerifyMiddleware(_inner, secret=SECRET), path)
    assert status == 403 and b"origin_not_allowed" in sent[1]["body"]


@pytest.mark.parametrize("value", ["", "wrong", SECRET[:-1], SECRET + "x", SECRET.upper()])
def test_refused_with_a_wrong_value(value):
    assert _call(OriginVerifyMiddleware(_inner, secret=SECRET), "/control-plane/config",
                 {"X-Origin-Verify": value})[0] == 403


def test_served_with_the_header_and_health_exempt():
    mw = OriginVerifyMiddleware(_inner, secret=SECRET)
    assert _call(mw, "/control-plane/config", {"X-Origin-Verify": SECRET})[0] == 200
    assert _call(mw, "/health")[0] == 200


def test_no_secret_means_no_enforcement(monkeypatch):
    monkeypatch.delenv("ORIGIN_VERIFY_SECRET", raising=False)
    assert _call(OriginVerifyMiddleware(_inner), "/control-plane/config")[0] == 200


def test_reads_the_secret_from_the_environment(monkeypatch):
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET", SECRET)
    mw = OriginVerifyMiddleware(_inner)
    assert _call(mw, "/control-plane/config")[0] == 403
    assert _call(mw, "/control-plane/config", {"x-origin-verify": SECRET})[0] == 200


def test_non_http_scopes_pass_through():
    # ASGI lifespan events carry no headers; refusing them would stop the app from starting.
    sent = []

    async def inner(scope, receive, send):
        sent.append(scope["type"])

    asyncio.run(OriginVerifyMiddleware(inner, secret=SECRET)({"type": "lifespan"}, None, None))
    assert sent == ["lifespan"]


def test_create_app_installs_the_lock():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "capability_api", "app.py")).read()
    assert "app.add_middleware(OriginVerifyMiddleware)" in src


def test_fastapi_integration():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/control-plane/config")
    def config():
        return {"ok": True}

    app.add_middleware(OriginVerifyMiddleware, secret=SECRET)
    c = TestClient(app)
    assert c.get("/control-plane/config").status_code == 403
    assert c.get("/control-plane/config", headers={"X-Origin-Verify": SECRET}).status_code == 200
