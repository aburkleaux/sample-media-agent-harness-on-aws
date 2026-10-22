"""Unit tests for per-request SigV4 signing (Wave 3)."""

from __future__ import annotations

import os
import sys

import httpx
from botocore.credentials import Credentials, ReadOnlyCredentials

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.sigv4 import SigV4RequestSigner  # noqa: E402


def _sign(signer: SigV4RequestSigner, request: httpx.Request) -> httpx.Request:
    gen = signer.auth_flow(request)
    signed = next(gen)
    gen.close()
    return signed


def _static_creds(access="AKID", secret="SECRET", token=None):
    def provider():
        return ReadOnlyCredentials(access, secret, token)

    return provider


def test_signs_each_request_and_sets_authorization():
    signer = SigV4RequestSigner(service="bedrock-agentcore", region="us-east-1",
                                credentials_provider=_static_creds())
    req = httpx.Request("POST", "https://gw.example/mcp", content=b'{"a":1}')
    signed = _sign(signer, req)
    auth = signed.headers.get("Authorization", "")
    assert auth.startswith("AWS4-HMAC-SHA256"), auth
    assert "X-Amz-Date" in signed.headers


def test_signature_varies_with_request_body_and_path():
    signer = SigV4RequestSigner(service="bedrock-agentcore", region="us-east-1",
                                credentials_provider=_static_creds())
    a = _sign(signer, httpx.Request("POST", "https://gw.example/mcp", content=b'{"a":1}'))
    b = _sign(signer, httpx.Request("POST", "https://gw.example/mcp", content=b'{"a":2}'))
    c = _sign(signer, httpx.Request("POST", "https://gw.example/other", content=b'{"a":1}'))
    assert a.headers["Authorization"] != b.headers["Authorization"], "body must change the signature"
    assert a.headers["Authorization"] != c.headers["Authorization"], "path must change the signature"


def test_credentials_fetched_per_request_for_rotation_safety():
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return ReadOnlyCredentials("AKID", "SECRET", None)

    signer = SigV4RequestSigner(service="bedrock-agentcore", region="us-east-1",
                                credentials_provider=provider)
    _sign(signer, httpx.Request("POST", "https://gw.example/mcp", content=b"{}"))
    _sign(signer, httpx.Request("POST", "https://gw.example/mcp", content=b"{}"))
    assert calls["n"] == 2, "credentials must be resolved on every request, not frozen at connect"


def test_temporary_credentials_add_security_token():
    signer = SigV4RequestSigner(service="bedrock-agentcore", region="us-east-1",
                                credentials_provider=_static_creds(token="SESSIONTOKEN"))
    signed = _sign(signer, httpx.Request("POST", "https://gw.example/mcp", content=b"{}"))
    assert signed.headers.get("X-Amz-Security-Token") == "SESSIONTOKEN"
