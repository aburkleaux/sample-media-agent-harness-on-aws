"""Per-request SigV4 signing for AWS_IAM (Platform) Gateway MCP requests (Wave 3).

The legacy path signed a single static ``{}`` body once at connect and reused
that ``Authorization`` header for every request — which is invalid SigV4: the
signature covers the exact method, path, headers, and body of each request, and
reusing one signature fails against a strict endpoint and cannot survive
credential rotation.

This ``httpx.Auth`` signs EACH outgoing request with the *current* credentials,
so list / call / search / stream setup / retries after a rotation all carry a
correct signature. It is supplied to Strands' ``MCPClient(auth_provider=...)``.
"""

from __future__ import annotations

from typing import Callable

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def default_credentials_provider(region: str) -> Callable[[], object]:
    """Return a callable yielding current frozen AWS credentials for ``region``.

    Resolves through a boto3 Session on each call so an expired/rotated
    credential is refreshed rather than frozen at connect time.
    """
    import boto3

    session = boto3.Session(region_name=region)

    def _provider():
        creds = session.get_credentials()
        if creds is None:
            raise RuntimeError(
                "No AWS credentials available for SigV4 gateway signing"
            )
        return creds.get_frozen_credentials()

    return _provider


class SigV4RequestSigner(httpx.Auth):
    """Signs each outgoing httpx request with SigV4 using current credentials."""

    # SigV4 signs the request body, so httpx must materialize it before signing.
    requires_request_body = True

    def __init__(
        self,
        *,
        service: str,
        region: str,
        credentials_provider: Callable[[], object],
    ) -> None:
        self._service = service
        self._region = region
        self._credentials_provider = credentials_provider

    def auth_flow(self, request: httpx.Request):
        creds = self._credentials_provider()  # current creds per request
        aws_req = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content or b"",
            headers=dict(request.headers),
        )
        SigV4Auth(creds, self._service, self._region).add_auth(aws_req)
        # Copy the signed headers (Authorization, X-Amz-Date, X-Amz-Security-Token
        # for temporary creds) back onto the outgoing request.
        for key, value in aws_req.headers.items():
            request.headers[key] = value
        yield request


__all__ = ["SigV4RequestSigner", "default_credentials_provider"]
