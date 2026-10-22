"""DEV ONLY local Token_Refresh path in project_search.refresh (LOCAL_TOKEN_REFRESH_URL).

A real loopback HTTP server stands in for agent/scripts/local_projects_stack.py's `/refresh`; the test
asserts the status mapping matches the Lambda path's and that the request body is `{"token": raw}`.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from project_search import refresh
from project_search.refresh import LocalTokenRefreshClient, TokenRefreshClient, default_refresher


class _Server:
    """Answers each POST with `(status, body bytes)` from `self.reply`; records request bodies."""

    def __init__(self):
        self.reply: tuple[int, bytes] = (200, b"{}")
        self.delay = 0.0
        self.seen: list[tuple[str, dict]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                outer.seen.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
                if outer.delay:
                    time.sleep(outer.delay)
                status, body = outer.reply
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.close()


def _call(refresher, raw="raw.tok.en"):
    return asyncio.run(refresher(raw))


@pytest.mark.parametrize("status,body,kind,reason,token", [
    (200, {"status": "ok", "token": "new.tok.en", "exp": 1, "jti": "j"}, "ok", "", "new.tok.en"),
    (200, {"status": "access_ended"}, "access_ended", "", None),
    (200, {"status": "invalid_token"}, "failed", "invalid_token", None),
    (200, {"status": "error"}, "failed", "error", None),
    (200, {"status": "ok"}, "failed", "error", None),               # ok without a token
    (200, {"status": "surprise"}, "failed", "error", None),
    (200, ["not", "an", "object"], "failed", "error", None),
    (500, {"status": "ok", "token": "x"}, "failed", "error", None),  # non-2xx is never trusted
    (429, {}, "failed", "throttled", None),
])
def test_local_refresh_status_mapping(server, monkeypatch, status, body, kind, reason, token):
    server.reply = (status, json.dumps(body).encode())
    monkeypatch.setenv("LOCAL_TOKEN_REFRESH_URL", server.base + "/")
    r = default_refresher()
    assert isinstance(r, LocalTokenRefreshClient)
    out = _call(r)
    assert (out.kind, out.reason, out.token) == (kind, reason, token)
    assert server.seen == [("/refresh", {"token": "raw.tok.en"})]


def test_local_refresh_non_json_is_error(server):
    server.reply = (200, b"<html>")
    assert _call(LocalTokenRefreshClient(server.base + "/refresh")).reason == "error"


def test_local_refresh_timeout(server):
    server.delay = 0.5
    server.reply = (200, b'{"status":"ok","token":"late"}')
    out = _call(LocalTokenRefreshClient(server.base + "/refresh", timeout=0.1))
    assert (out.kind, out.reason) == ("failed", "timeout")


def test_local_refresh_unreachable_is_error():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    out = _call(LocalTokenRefreshClient(f"http://127.0.0.1:{port}/refresh"))
    assert (out.kind, out.reason) == ("failed", "error")


def test_local_url_wins_and_lambda_path_unchanged(monkeypatch):
    monkeypatch.setenv("PROJECT_TOKEN_REFRESH_FN", "fn")
    monkeypatch.delenv("LOCAL_TOKEN_REFRESH_URL", raising=False)
    assert isinstance(default_refresher(), TokenRefreshClient)
    monkeypatch.setenv("LOCAL_TOKEN_REFRESH_URL", "http://localhost:8098")
    r = default_refresher()
    assert isinstance(r, LocalTokenRefreshClient) and r.url == "http://localhost:8098/refresh"
    monkeypatch.delenv("PROJECT_TOKEN_REFRESH_FN")
    monkeypatch.delenv("LOCAL_TOKEN_REFRESH_URL")
    assert default_refresher() is None


@pytest.mark.parametrize("url", ["https://example.com", "http://10.0.0.5:8098", "file:///tmp/x", "not a url"])
def test_local_url_must_be_loopback_http(monkeypatch, url):
    monkeypatch.setenv("LOCAL_TOKEN_REFRESH_URL", url)
    assert refresh.local_refresh_endpoint(url) is None
    assert default_refresher() is None
