"""``ingestion.m2c`` — stateless JSON-RPC client (design Component 9).

Response shapes are the ones measured on the staging M2C gateway: no ``initialize`` needed; tool refusals come
back as a NORMAL result whose JSON body is ``{"error": true, "errorCode", "message"}``; an unknown tool is a
JSON-RPC error.
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import m2c  # noqa: E402
from ingestion.m2c import M2CClient, M2CConfig, ToolError, Transient, Validation  # noqa: E402

CFG = M2CConfig(gateway_url="https://gw/mcp", client_id="cid", token_endpoint="https://idp/token",
                secret_arn="arn:aws:secretsmanager:us-west-2:111122223333:secret:s")


class Resp:
    def __init__(self, status, body, ctype="application/json"):
        self.status, self._b, self.headers = status, body if isinstance(body, bytes) else body.encode(), \
            {"Content-Type": ctype}

    def read(self, *_):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Secrets:
    def __init__(self, s='{"clientId":"cid","clientSecret":"shh"}'):
        self.s, self.calls = s, 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        return {"SecretString": self.s}


class Opener:
    """Answers the token endpoint itself; gateway answers come from ``script`` in order."""

    def __init__(self, script, expires_in=3600):
        self.script, self.requests, self.tokens, self.exp = list(script), [], 0, expires_in

    def __call__(self, req, timeout):
        if req.full_url == CFG.token_endpoint:
            self.tokens += 1
            return Resp(200, json.dumps({"access_token": f"tok{self.tokens}", "expires_in": self.exp}))
        self.requests.append(req)
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def ok(body: dict, *, is_error=False):
    res = {"content": [{"type": "text", "text": json.dumps(body)}]}
    if is_error:
        res["isError"] = True
    return Resp(200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": res}))


def client(script, clock=lambda: 1000.0, **kw):
    op = Opener(script, **kw)
    return M2CClient(CFG, secrets=Secrets(), opener=op, clock=clock), op


def test_call_tool_parses_json_and_sends_bearer():
    c, op = client([ok({"job_id": "arn:j", "status": "RUNNING"})])
    assert c.call_tool("t___x", {"a": 1}) == {"job_id": "arn:j", "status": "RUNNING"}
    req = op.requests[0]
    assert req.get_header("Authorization") == "Bearer tok1"
    body = json.loads(req.data)
    assert body["method"] == "tools/call" and body["params"] == {"name": "t___x", "arguments": {"a": 1}}


def test_m2c_error_body_is_tool_error_with_code():
    c, _ = client([ok({"error": True, "errorCode": "INVALID_PARAMETER", "message": "asset_s3uri bad"})])
    with pytest.raises(ToolError) as e:
        c.call_tool("t___x", {})
    assert (e.value.code, e.value.text) == ("INVALID_PARAMETER", "asset_s3uri bad")


def test_is_error_result_is_tool_error():
    c, _ = client([ok({"errorCode": "X", "message": "boom"}, is_error=True)])
    with pytest.raises(ToolError) as e:
        c.call_tool("t___x", {})
    assert (e.value.code, e.value.text) == ("X", "boom")


def test_jsonrpc_error_is_validation_or_transient():
    c, _ = client([Resp(200, '{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"Unknown tool: x"}}'),
                   Resp(200, '{"jsonrpc":"2.0","id":2,"error":{"message":"ThrottlingException: slow"}}')])
    with pytest.raises(Validation):
        c.call_tool("x", {})
    with pytest.raises(Transient):
        c.call_tool("x", {})


@pytest.mark.parametrize("status,exc", [(429, Transient), (503, Transient), (400, Validation), (403, Validation)])
def test_http_status_mapping(status, exc):
    err = urllib.error.HTTPError(CFG.gateway_url, status, "x", {"Content-Type": "text/plain"}, io.BytesIO(b"no"))
    c, _ = client([err])
    with pytest.raises(exc):
        c.call_tool("t", {})


def test_401_drops_cached_token():
    err = urllib.error.HTTPError(CFG.gateway_url, 401, "x", {}, io.BytesIO(b""))
    c, op = client([err, ok({"ok": 1})])
    with pytest.raises(Transient):
        c.call_tool("t", {})
    assert c.call_tool("t", {}) == {"ok": 1}
    assert op.tokens == 2


def test_connection_error_and_timeout_are_transient():
    c, _ = client([urllib.error.URLError("refused"), TimeoutError("slow")])
    for _ in range(2):
        with pytest.raises(Transient):
            c.call_tool("t", {})


def test_sse_response_is_parsed():
    sse = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\\"a\\":1}"}]}}\n\n'
    c, _ = client([Resp(200, sse, "text/event-stream")])
    assert c.call_tool("t", {}) == {"a": 1}


def test_token_cached_until_skew():
    now = [1000.0]
    c, op = client([ok({}), ok({}), ok({})], clock=lambda: now[0], expires_in=3600)
    c.call_tool("t", {})
    now[0] += 3600 - m2c.TOKEN_SKEW_S - 1
    c.call_tool("t", {})
    assert op.tokens == 1
    now[0] += 2
    c.call_tool("t", {})
    assert op.tokens == 2


def test_list_tools_paginates_and_caches_on_success_only():
    page1 = Resp(200, json.dumps({"result": {"tools": [{"name": "a"}], "nextCursor": "c1"}}))
    page2 = Resp(200, json.dumps({"result": {"tools": [{"name": "b"}]}}))
    now = [0.0]
    c, op = client([urllib.error.URLError("x"), page1, page2], clock=lambda: now[0])
    with pytest.raises(Transient):
        c.list_tools()
    assert c.list_tools() == {"a", "b"}
    assert json.loads(op.requests[-1].data)["params"] == {"cursor": "c1"}
    n = len(op.requests)
    assert c.list_tools() == {"a", "b"} and len(op.requests) == n  # cached


def test_secret_formats_and_config_from_env():
    assert m2c.client_secret_from('{"clientSecret":"x"}') == "x"
    assert m2c.client_secret_from("literal") == "literal"
    with pytest.raises(m2c.M2CError, match="M2C_GATEWAY_URL"):
        M2CConfig.from_env({"M2C_CLIENT_ID": "c", "M2C_TOKEN_ENDPOINT": "t", "M2C_SECRET_ARN": "s"})
    cfg = M2CConfig.from_env({"M2C_GATEWAY_URL": "g", "M2C_CLIENT_ID": "c", "M2C_TOKEN_ENDPOINT": "t",
                              "M2C_SECRET_ARN": "s"})
    assert cfg.scope == "media-workflows/invoke"

