"""Tests for the MCP connection probe.

The probe is deliberately just initialize + tools/list. It must prove
reachability, credentials and what the server exposes -- and must never invoke a
tool, because that would cause side effects on someone else's system.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import gateway.user_mcp as user_mcp  # noqa: E402

URL = "https://mcp.example.com/mcp"


@pytest.fixture(autouse=True)
def _public_dns():
    """No network in unit tests: every name resolves to a public address via the shared
    SSRF guard's resolver hook, unless a test installs its own."""
    from security import url_guard
    previous = url_guard.set_resolver(lambda host, port: ["93.184.215.14"])
    yield
    url_guard.set_resolver(previous)


def _rpc(result: dict, rid: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


class _Server:
    """Scripted endpoint. Records the JSON-RPC methods it was asked for."""

    def __init__(self, replies, headers=None, raise_on=None):
        self.replies = list(replies)
        self.headers = headers or {}
        self.raise_on = raise_on
        self.methods: list[str] = []
        self.sent_headers: list[dict] = []

    def __call__(self, url, headers, payload):
        self.methods.append(payload.get("method", ""))
        self.sent_headers.append(dict(headers))
        if self.raise_on and payload.get("method") == self.raise_on[0]:
            raise self.raise_on[1]
        reply = self.replies.pop(0) if self.replies else {}
        return 200, reply, self.headers


@pytest.fixture
def serve(monkeypatch):
    def _install(server):
        monkeypatch.setattr(user_mcp, "_mcp_post", server)
        return server
    return _install


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_reports_discovered_tools(serve):
    serve(_Server([
        _rpc({"protocolVersion": "2025-03-26"}),
        _rpc({"tools": [{"name": "read_note"}, {"name": "search_notes"}]}, 2),
    ]))
    out = user_mcp.probe_mcp_endpoint(URL, token="tok")
    assert out["ok"] is True
    assert out["tool_count"] == 2
    assert out["tools"] == ["read_note", "search_notes"]
    assert out["detail"] == ""


def test_only_initialize_and_list_are_called(serve):
    """A probe must never invoke a tool."""
    srv = serve(_Server([_rpc({}), _rpc({"tools": []}, 2)]))
    user_mcp.probe_mcp_endpoint(URL, token="tok")
    assert srv.methods == ["initialize", "tools/list"]
    assert "tools/call" not in srv.methods


def test_credential_is_sent_on_the_configured_header(serve):
    srv = serve(_Server([_rpc({}), _rpc({"tools": []}, 2)]))
    user_mcp.probe_mcp_endpoint(
        URL, token="k", header_name="X-API-Key", value_prefix="",
    )
    assert srv.sent_headers[0]["X-API-Key"] == "k"


def test_streamable_accept_header_is_sent(serve):
    """Streamable-HTTP servers reject a POST that does not accept both types."""
    srv = serve(_Server([_rpc({}), _rpc({"tools": []}, 2)]))
    user_mcp.probe_mcp_endpoint(URL, token="tok")
    assert srv.sent_headers[0]["Accept"] == "application/json, text/event-stream"


def test_session_id_is_carried_into_the_listing(serve):
    srv = serve(_Server(
        [_rpc({}), _rpc({"tools": []}, 2)],
        headers={"mcp-session-id": "sess-9"},
    ))
    user_mcp.probe_mcp_endpoint(URL, token="tok")
    assert srv.sent_headers[1]["Mcp-Session-Id"] == "sess-9"


def test_no_token_sends_no_auth_header(serve):
    srv = serve(_Server([_rpc({}), _rpc({"tools": []}, 2)]))
    user_mcp.probe_mcp_endpoint(URL)
    assert "Authorization" not in srv.sent_headers[0]


# ---------------------------------------------------------------------------
# Outcomes that are not failures of ours
# ---------------------------------------------------------------------------


def test_zero_tools_is_reported_not_celebrated(serve):
    serve(_Server([_rpc({}), _rpc({"tools": []}, 2)]))
    out = user_mcp.probe_mcp_endpoint(URL, token="tok")
    assert out["ok"] is True
    assert out["tool_count"] == 0
    assert "exposes no tools" in out["detail"]


def test_non_mcp_response_suggests_the_path(serve):
    """The commonest mistake: the site root instead of the MCP endpoint."""
    serve(_Server([{"hello": "world"}]))
    out = user_mcp.probe_mcp_endpoint(URL, token="tok")
    assert out["ok"] is False
    assert "doesn't look like an MCP endpoint" in out["detail"]


def test_jsonrpc_error_is_surfaced_verbatim(serve):
    serve(_Server([{"jsonrpc": "2.0", "id": 1,
                    "error": {"code": -32600, "message": "unsupported version"}}]))
    out = user_mcp.probe_mcp_endpoint(URL, token="tok")
    assert out["ok"] is False
    assert out["detail"] == "unsupported version"


def test_401_names_the_likely_cause(serve):
    err = urllib.error.HTTPError(URL, 401, "Unauthorized", {}, None)
    serve(_Server([], raise_on=("initialize", err)))
    out = user_mcp.probe_mcp_endpoint(URL, token="bad")
    assert out["ok"] is False
    assert "401" in out["detail"] and "credential" in out["detail"]


def test_transport_failure_shows_the_real_message(serve):
    serve(_Server([], raise_on=("initialize", OSError("Name or service not known"))))
    out = user_mcp.probe_mcp_endpoint(URL, token="tok")
    assert out["ok"] is False
    assert "Name or service not known" in out["detail"]


def test_listing_failure_distinguishes_itself(serve):
    serve(_Server([_rpc({})], raise_on=("tools/list", OSError("reset"))))
    out = user_mcp.probe_mcp_endpoint(URL, token="tok")
    assert out["ok"] is False
    assert "listing tools failed" in out["detail"]


# ---------------------------------------------------------------------------
# The probe must not be an SSRF tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "http://mcp.example.com/mcp",
    "https://169.254.169.254/mcp",
    "https://10.0.0.1/mcp",
    "https://localhost/mcp",
    "https://thing.internal/mcp",
])
def test_probe_applies_the_same_host_rules_as_registration(bad, monkeypatch):
    """Otherwise Test connection reaches hosts registration would refuse."""
    called = []
    monkeypatch.setattr(user_mcp, "_mcp_post", lambda *a, **k: called.append(a))
    out = user_mcp.probe_mcp_endpoint(bad, token="tok")
    assert out["ok"] is False
    assert called == [], "no request should be made to a refused host"


def test_bad_header_name_is_refused_before_connecting(monkeypatch):
    called = []
    monkeypatch.setattr(user_mcp, "_mcp_post", lambda *a, **k: called.append(a))
    out = user_mcp.probe_mcp_endpoint(URL, token="t", header_name="Bad Header")
    assert out["ok"] is False
    assert called == []


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_parses_plain_json():
    assert user_mcp._parse_jsonrpc_body('{"result": {"a": 1}}') == {"result": {"a": 1}}


def test_parses_sse_framing():
    """A streamable-HTTP server may answer a POST with text/event-stream."""
    body = "event: message\ndata: " + json.dumps({"result": {"tools": []}}) + "\n\n"
    assert user_mcp._parse_jsonrpc_body(body) == {"result": {"tools": []}}


def test_takes_the_last_data_frame():
    body = 'data: {"result": {"n": 1}}\n\ndata: {"result": {"n": 2}}\n\n'
    assert user_mcp._parse_jsonrpc_body(body)["result"]["n"] == 2


def test_unparseable_body_is_empty_not_an_exception():
    assert user_mcp._parse_jsonrpc_body("<html>nope</html>") == {}
    assert user_mcp._parse_jsonrpc_body("") == {}


def test_tool_list_is_capped(serve):
    many = [{"name": f"t{i}"} for i in range(200)]
    serve(_Server([_rpc({}), _rpc({"tools": many}, 2)]))
    out = user_mcp.probe_mcp_endpoint(URL, token="tok")
    # Full count reported, list truncated for display.
    assert out["tool_count"] == 200
    assert len(out["tools"]) == user_mcp._PROBE_MAX_TOOLS_SHOWN
