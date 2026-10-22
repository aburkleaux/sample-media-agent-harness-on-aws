"""Search_Gateway client (design Component 7 `client.py`, D4, Req 4.14, 5.2, 7.1, 7.6, 7.7, 7.12).

MCP streamable-HTTP sessions opened with mcp 2.x's `streamable_http_client` over an `httpx2.AsyncClient`,
so nothing blocks the event loop. The only credential is `Authorization: Bearer <Project_Token>`, and the
only URL is SEARCH_GATEWAY_URL.

Sessions are POOLED (:class:`_SessionPool`, 2026-10-16): one open session per (event loop, URL, token),
shared by every call made with that token -- a turn's parallel searches, its document companion, and
later turns while the token is unchanged. Opening one per call cost a TLS handshake plus the
`server/discover` / `initialize` round trip through the gateway's JWT authorizer on EVERY search. The
key includes the token itself (hashed), so a connection never carries another actor's or project's
credential and a refreshed token gets its own session. An entry expires after :data:`POOL_IDLE_S` idle,
:data:`POOL_MAX_AGE_S` old, or shortly before its token's `exp`.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
import weakref
from typing import Any, Callable, Optional

from .errors import SearchOutcome

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT_SECONDS = 60
# `tools/list` pages to read before giving up (the search gateway lists one permitted tool, but the
# gateway pages over every target tool before Cedar filters, so the tool can sit on a later page).
_MAX_TOOL_PAGES = 20
# Session pool bounds.
POOL_IDLE_S = 120.0
POOL_MAX_AGE_S = 600.0
POOL_MAX_ENTRIES = 32
# A pooled session is not reused within this many seconds of its token's `exp`.
_TOKEN_EXP_MARGIN_S = 30.0


def _flatten(exc: BaseException):
    """Yield every exception inside anyio/ExceptionGroup wrappers."""
    stack = [exc]
    while stack:
        e = stack.pop()
        yield e
        stack.extend(getattr(e, "exceptions", ()) or ())
        if e.__cause__ is not None:
            stack.append(e.__cause__)


def _http_status(exc: BaseException) -> Optional[int]:
    for e in _flatten(exc):
        resp = getattr(e, "response", None)
        code = getattr(resp, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _rpc_error(exc: BaseException) -> Optional[tuple[Optional[int], str]]:
    for e in _flatten(exc):
        err = getattr(e, "error", None)
        msg = getattr(err, "message", None)
        if isinstance(msg, str):
            code = getattr(err, "code", None)
            return (code if isinstance(code, int) else None), msg
        if type(e).__name__ in ("McpError", "MCPError"):
            code = getattr(e, "code", None)
            return (code if isinstance(code, int) else None), str(e)
    return None


def _is_transport(exc: BaseException) -> bool:
    names = {type(e).__name__ for e in _flatten(exc)}
    return bool(names & {"ConnectError", "ConnectTimeout", "ReadError", "RemoteProtocolError",
                         "NetworkError", "gaierror", "OSError", "ConnectionRefusedError"})


def _text_of(result: Any) -> str:
    parts = []
    for c in getattr(result, "content", None) or []:
        t = getattr(c, "text", None)
        if isinstance(t, str):
            parts.append(t)
    return "\n".join(parts)


class SearchGatewayClient:
    def __init__(self, url: Optional[str] = None, tool_name: Optional[str] = None,
                 timeout: float = SEARCH_TIMEOUT_SECONDS):
        self.url = url or os.environ.get("SEARCH_GATEWAY_URL", "")
        self.tool_name = tool_name or os.environ.get("SEARCH_TOOL_NAME", "")
        self.timeout = timeout

    async def _session(self, token: str, body):
        """``(phase, run)``: ``run()`` awaits ``body(session)`` on the pooled session for ``token``.

        ``phase["v"]`` is "initialize" until a session is open, then "call", so failures classify exactly
        as they did with one session per call. A failure on a REUSED session showing that the session
        itself is gone (its transport died, or the server forgot it: "Session terminated") is retried once
        on a fresh session -- such a request never reached the tool. Any other failure is returned as is
        (there is still no search retry).
        """
        phase = {"v": "initialize"}

        async def run():
            pool = _pool_for_loop()
            for attempt in range(2):
                phase["v"] = "initialize"
                conn = await pool.acquire(self.url, token, self.timeout)
                reused = conn.uses > 0
                conn.uses += 1
                conn.inflight += 1
                phase["v"] = "call"
                try:
                    return await body(conn.session)
                except BaseException as exc:  # noqa: BLE001 - classified by the caller
                    if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                        raise
                    gone = conn.dead or _is_session_gone(exc)
                    if gone:
                        pool.discard(conn)
                    if attempt == 0 and reused and gone:
                        logger.info("pooled search session was gone; reopening once")
                        continue
                    # A dead transport resolves the waiter with a generic "connection closed": raise what
                    # actually killed it, so a ConnectError still classifies as `gateway unreachable`.
                    if conn.dead and conn.error is not None and not isinstance(conn.error, asyncio.CancelledError):
                        raise conn.error from exc
                    raise
                finally:
                    conn.inflight -= 1
                    conn.last_used = time.monotonic()
                    pool.release(conn)
            raise RuntimeError("unreachable")  # pragma: no cover
        return phase, run

    async def call(self, token: str, arguments: dict) -> SearchOutcome:
        return await self.call_tool(token, self.tool_name, arguments)

    async def call_tool(self, token: str, name: str, arguments: dict) -> SearchOutcome:
        """Call any tool on the Search_Gateway (search, or a `project-graph___` knowledge-graph tool)."""
        async def body(s):
            return await s.call_tool(name, arguments)
        phase, run = await self._session(token, body)
        try:
            res = await asyncio.wait_for(run(), self.timeout)
        except asyncio.TimeoutError:
            return SearchOutcome(phase["v"], "timeout")
        except BaseException as exc:  # noqa: BLE001  (anyio raises ExceptionGroup)
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return _failure(phase["v"], exc)
        text = _text_of(res)
        if getattr(res, "isError", False):
            return SearchOutcome("call", "tool_error", message=text)
        return SearchOutcome("call", "ok", result=text)

    async def list_tools(self, token: str) -> list:
        # Every page, not just the first: the gateway pages `tools/list`, and since the 2026-10-06 target
        # sync (65 tools) `search_content` is not on page 1, so a one-page read never found it and search
        # was unbound on every turn (LookupError in fetch_spec).
        async def body(s):
            from mcp import types
            tools: list = []
            cursor = None
            for _ in range(_MAX_TOOL_PAGES):
                page = await s.list_tools(params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
                tools.extend(page.tools)
                cursor = getattr(page, "next_cursor", None) or getattr(page, "nextCursor", None)
                if not cursor:
                    break
            return tools
        phase, run = await self._session(token, body)
        return await asyncio.wait_for(run(), self.timeout)


# --------------------------------------------------------------------------------------------------------
# Session pool
# --------------------------------------------------------------------------------------------------------

def _is_session_gone(exc: BaseException) -> bool:
    """The SESSION is unusable (rather than the call failing): the server's 404 "Session terminated", or
    the transport closed under the call."""
    for e in _flatten(exc):
        err = getattr(e, "error", None)
        msg = str(getattr(err, "message", "") or e)
        if "Session terminated" in msg or "Connection closed" in msg:
            return True
        if type(e).__name__ in ("ClosedResourceError", "BrokenResourceError", "EndOfStream"):
            return True
    return False


# (url, token, timeout) -> an async context manager that yields an initialized MCP ClientSession.
Opener = Callable[[str, str, float], Any]


@contextlib.asynccontextmanager
async def _open_session(url: str, token: str, timeout: float):
    import httpx2
    from mcp import ClientSession
    from mcp.client.client import negotiate_auto
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                  timeout=httpx2.Timeout(timeout + 5)) as hc:
        async with streamable_http_client(url, http_client=hc) as (r, w, *_):
            async with ClientSession(r, w) as s:
                # mcp 2.x connect policy: probe `server/discover` (2026-07-28) and fall back to the legacy
                # `initialize` handshake. A bare `initialize()` fails on AgentCore Gateway, which answers it
                # with 2026-07-28 (spike_v10_gateway.py).
                await negotiate_auto(s)
                yield s


def _token_deadline(token: str) -> float:
    """Monotonic time after which a session opened with ``token`` is not reused (its `exp` minus a margin)."""
    try:
        from .token_ref import parse_claims
        exp = parse_claims(token).get("exp")
    except Exception:  # noqa: BLE001 - an unparsable token just gets the default max age
        exp = None
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return float("inf")
    return time.monotonic() + (float(exp) - time.time()) - _TOKEN_EXP_MARGIN_S


class _Conn:
    """One open MCP session, owned by its own task (anyio cancel scopes must exit in the task that
    entered them). Calls from other tasks share it; the session multiplexes them by request id."""

    def __init__(self, key: tuple, url: str, token: str, timeout: float, opener: Opener):
        self.key = key
        now = time.monotonic()
        self.created = self.last_used = now
        self.deadline = min(now + POOL_MAX_AGE_S, _token_deadline(token))
        self.uses = 0
        self.inflight = 0
        self.session: Any = None
        self.error: Optional[BaseException] = None
        self.dead = False
        self.retired = False
        self._stop = asyncio.Event()
        self._ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._task = asyncio.ensure_future(self._run(url, token, timeout, opener))

    async def _run(self, url: str, token: str, timeout: float, opener: Opener) -> None:
        try:
            async with opener(url, token, timeout) as s:
                self.session = s
                if not self._ready.done():
                    self._ready.set_result(s)
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - handed to the waiters, never re-raised into the loop
            self.error = exc
            if not self._ready.done():
                self._ready.set_exception(exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self.dead = True
            if not self._ready.done():  # pragma: no cover - closed before it opened
                self._ready.set_exception(ConnectionError("search session closed"))
            # A waiter that timed out never retrieves the exception; mark it retrieved.
            if self._ready.done() and not self._ready.cancelled():
                self._ready.exception()

    async def ready(self, timeout: float):
        # shield: one waiter timing out must not cancel the open for the others.
        return await asyncio.wait_for(asyncio.shield(self._ready), timeout)

    def usable(self, now: float) -> bool:
        if self.dead or self.retired or now >= self.deadline:
            return False
        return self.inflight > 0 or now - self.last_used < POOL_IDLE_S

    def close(self) -> None:
        self.retired = True
        self._stop.set()


class _SessionPool:
    """The open sessions of ONE event loop, keyed by (url, sha256(token))."""

    def __init__(self, opener: Opener = _open_session):
        self._opener = opener
        self._conns: dict[tuple, _Conn] = {}

    @staticmethod
    def key(url: str, token: str) -> tuple:
        return (url, hashlib.sha256(token.encode()).hexdigest())

    async def acquire(self, url: str, token: str, timeout: float) -> _Conn:
        now = time.monotonic()
        self._sweep(now)
        key = self.key(url, token)
        conn = self._conns.get(key)
        if conn is None:
            conn = _Conn(key, url, token, timeout, self._opener)
            self._conns[key] = conn
            self._bound()
        try:
            await conn.ready(timeout)
        except BaseException:
            self.discard(conn)
            raise
        return conn

    def discard(self, conn: _Conn) -> None:
        if self._conns.get(conn.key) is conn:
            del self._conns[conn.key]
        conn.retired = True
        if conn.inflight <= 0:
            conn.close()

    def release(self, conn: _Conn) -> None:
        if conn.retired and conn.inflight <= 0:
            conn.close()

    def _sweep(self, now: float) -> None:
        for conn in list(self._conns.values()):
            if not conn.usable(now):
                self.discard(conn)

    def _bound(self) -> None:
        while len(self._conns) > POOL_MAX_ENTRIES:
            idle = [c for c in self._conns.values() if c.inflight <= 0] or list(self._conns.values())
            self.discard(min(idle, key=lambda c: c.last_used))

    def close_all(self) -> None:
        for conn in list(self._conns.values()):
            self.discard(conn)

    def __len__(self) -> int:
        return len(self._conns)


_pools: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _SessionPool]" = weakref.WeakKeyDictionary()
_pool_opener: Opener = _open_session


def _pool_for_loop() -> _SessionPool:
    """The pool of the running loop: a session's streams belong to the loop that opened them."""
    loop = asyncio.get_running_loop()
    pool = _pools.get(loop)
    if pool is None:
        pool = _pools[loop] = _SessionPool(_pool_opener)
    return pool


def set_session_opener(opener: Opener) -> None:
    """Tests: replace how sessions are opened, and forget every pool."""
    global _pool_opener
    _pool_opener = opener
    for pool in list(_pools.values()):
        pool.close_all()
    _pools.clear()


def reset_session_pools() -> None:
    set_session_opener(_open_session)


def _failure(phase: str, exc: BaseException) -> SearchOutcome:
    status = _http_status(exc)
    if status is not None:
        return SearchOutcome(phase, "http", status=status, message=f"HTTP {status}")
    rpc = _rpc_error(exc)
    if rpc is not None:
        return SearchOutcome(phase, "rpc_error", status=rpc[0], message=rpc[1])
    if _is_transport(exc):
        return SearchOutcome(phase, "transport", message=type(exc).__name__)
    return SearchOutcome(phase, "transport" if phase == "initialize" else "rpc_error",
                         message=f"{type(exc).__name__}: {exc}"[:300])
