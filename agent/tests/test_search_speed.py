"""Search-turn speed (2026-10-16): pooled Search_Gateway sessions, a smaller model view of the hits, the
search-once / no-URL prompt lines -- and the golden answer they must not change.

Measured in staging for "find moment when Meridian title page show up" (project p49b9cc6c…): three M2C runs per
question (two model searches plus a document companion), a fresh MCP session per call (~1.5 s harness->M2C each),
13.6k tokens of results in the second model call, and answers that spelled out CloudFront URLs.

The golden test replays a RECORDED staging result (``fixtures/search_meridian_title_card.json``) through the
real Search_Tool, turn-end verification and Moments selection: the model still reads the title-card hit at
23.09-29.70 s, the answer's ``[… @ 00:23–00:30]`` citation still verifies against it, and the Moments tab still
gets that card (with its highlight terms, read from the full Hit the ledger keeps).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from project_search import client as client_mod  # noqa: E402
from project_search import compact, document_recall, errors, ledger, turn_end  # noqa: E402
from project_search.assets import ResolvedAsset  # noqa: E402
from project_search.citations import format_citation  # noqa: E402
from project_search.context import ActiveProject, TurnContext, current_turn_context  # noqa: E402
from project_search.errors import SearchOutcome  # noqa: E402
from project_search.session_hits import MemorySessionHitStore  # noqa: E402
from project_search.token_ref import ProjectTokenRef  # noqa: E402
from project_search.tool import ProjectSearchTool  # noqa: E402
from project_search.turn import _prompt_block  # noqa: E402
from project_search.verify import whole_second_range  # noqa: E402
from render.moments import build_moments_manifest, select_moments  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "search_meridian_title_card.json"
PID = "p49b9cc6c23264491b8548569ed228220"
TITLE_CARD = "video:d06a0ced-3021-4c27-ad19-ade0f4bae035"
TITLE_CARD_NAME = "netflix_open_content_meridian — process media (meridian_trim).mp4 (2)"
GOLDEN_CITATION = f"[{TITLE_CARD_NAME} @ 00:23–00:30]"
SPEC = {"name": "tgt___hybrid-search___search_content", "description": "d", "inputSchema": {"json": {"type": "object"}}}


def _tok(pid: str = PID, life: int = 3600) -> str:
    iat = int(time.time())
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{seg({'alg': 'RS256'})}.{seg({'sub': 'u1', 'project_id': pid, 'iat': iat, 'exp': iat + life, 'jti': uuid.uuid4().hex})}.sig"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


class _FakeClient:
    tool_name = SPEC["name"]

    def __init__(self, result):
        self.result, self.calls = result, []

    async def call(self, token, args):
        self.calls.append(dict(args))
        return SearchOutcome("call", "ok", result=json.dumps(self.result, ensure_ascii=False))


class _Resolver:
    def __init__(self, hits):
        self.by_id = {h["asset_id"]: ResolvedAsset(asset_id=h["asset_id"], kind=h["asset_id"].split(":")[0],
                                                   display_name=h["title"], status="indexed") for h in hits}

    def resolve(self, project_id, asset_ids):
        return {a: self.by_id[a] for a in asset_ids if a in self.by_id}


def _old_model_text(result: dict, k: int = 10) -> str:
    """The pre-2026-10-16 model view, re-stated: five fields + title/text(1500)/named_entities/highlights/frame key."""
    out = []
    for h in result["results"][:k]:
        c = {f: h[f] for f in ("asset_id", "start_ms", "end_ms", "source_index", "final_score") if f in h}
        for f in ("title", "text", "named_entities", "highlights", "frame_s3_key"):
            if h.get(f) is not None:
                c[f] = h[f]
        out.append(c)
    return json.dumps({"results": out, "skipped_indices": result.get("skipped_indices")}, ensure_ascii=False)


# --------------------------------------------------------------------------------------------------------
# The golden answer
# --------------------------------------------------------------------------------------------------------

def test_golden_meridian_title_card_still_cites_00_23_to_00_30():
    fx = _fixture()
    hits = fx["result"]["results"]
    document_recall.reset_documents_seen()
    client = _FakeClient(fx["result"])
    turn_id = "golden-" + uuid.uuid4().hex
    tool = ProjectSearchTool(ProjectTokenRef(_tok()), SPEC, project_id=PID, turn_id=turn_id, client=client)
    tc = TurnContext(turn_id=turn_id, active_project=ActiveProject(id=PID, name="", collection_id=PID, personal=False),
                     web_search=False, project_access="ok")
    ctx = current_turn_context.set(tc)
    try:
        async def go():
            return [ev async for ev in tool.stream(
                {"toolUseId": "tu-1", "name": SPEC["name"],
                 "input": {"collection_id": PID, "search_term": fx["search_term"]}}, {})]
        result = asyncio.run(go())[-1].tool_result
        assert result["status"] == "success"
        # A scoped query (#shot #scene): one M2C run, no document companion.
        assert len(client.calls) == 1

        text = result["content"][0]["text"]
        payload = json.loads(text)
        card = [h for h in payload["results"] if h["asset_id"] == TITLE_CARD and h["source_index"] == "scene"]
        assert len(card) == 1
        card = card[0]
        # Everything the model needs to answer and cite survives the smaller view.
        assert (card["start_ms"], card["end_ms"], card["title"]) == (23090, 29696, TITLE_CARD_NAME)
        assert "title card reading Meridian appears" in card["text"] and "On-screen text: Meridian" in card["text"]
        assert card["matched_terms"] == ["Meridian"]
        assert "highlights" not in card  # they only repeated `text` with <em> marks
        assert format_citation(card["title"], *whole_second_range(card)) == GOLDEN_CITATION
        shot = [h for h in payload["results"] if h["source_index"] == "shot"][0]
        assert shot["frame_s3_key"].endswith("frame_0000720.jpg")  # still a valid Find-similar query image
        # ...in a third fewer bytes than before (here 3.6 KB vs 5.6 KB; highlights repeated `text`).
        assert len(text) < 0.7 * len(_old_model_text(fx["result"]))
        assert "doc_id" not in text and "rrf_score" not in text and "match_sources" not in text

        # The ledger keeps the FULL hits: citations and the Moments tab read them, never the model view.
        [entry] = ledger.results_for(turn_id)
        assert entry.hits == hits

        answer = (f"The Meridian title card appears over a 1940s Los Angeles street corner {GOLDEN_CITATION}.")
        turn_end.note_answer_text(turn_id, answer)
        outcome = turn_end.finish_turn(store=MemorySessionHitStore(), resolver=_Resolver(hits))
        [record] = outcome.citations
        assert record.verified and record.ref.asset_id == TITLE_CARD
        assert (record.start_ms, record.end_ms) == (23000, 30000)

        sel = select_moments(outcome)
        assert sel is not None and sel.basis == "cited"
        first = sel.items[0]
        assert first.key == (TITLE_CARD, 23090, 29696) and first.citation == GOLDEN_CITATION
        assert first.highlight_terms == ["Meridian"]  # from the ledger's full `highlights`
        manifest = build_moments_manifest(sel, session_id="s1", turn_id=turn_id, created_at="2026-10-16T00:00:00Z")
        gm = manifest["galleryMoments"]
        assert gm["basis"] == "cited"
        assert "http" not in json.dumps(manifest)
    finally:
        current_turn_context.reset(ctx)
        turn_end.release(turn_id)
        ledger.clear_turn(turn_id)


def test_highlights_past_the_text_cap_survive_as_a_snippet():
    long_text = "x " * 600 + "the rare word appears here " + "y " * 100
    hit = {"asset_id": "video:a", "start_ms": 0, "end_ms": 1, "source_index": "scene", "final_score": 0.123456,
           "title": "a.mp4", "text": long_text,
           "highlights": {"text": long_text.replace("rare", "<em>rare</em>")}}
    out = compact.compact_hit(hit)
    assert out["final_score"] == 0.123
    assert len(out["text"]) == compact.TEXT_MAX + 1 and "rare" not in out["text"]
    assert out["matched_terms"] == ["rare"]
    [snippet] = out["highlights"]
    assert "the rare word appears here" in snippet and len(snippet) <= compact.HIGHLIGHT_SNIPPET_MAX + 2
    # A document page keeps the longer cap: its text IS the answer.
    doc = compact.compact_hit({**hit, "asset_id": "document:a", "start_ms": 4, "end_ms": 5})
    assert doc["text"] == long_text and "highlights" not in doc and doc["page"] == 1


# --------------------------------------------------------------------------------------------------------
# Prompt lines
# --------------------------------------------------------------------------------------------------------

def test_the_search_prompt_asks_for_one_search_and_no_urls():
    tc = TurnContext(turn_id="t", active_project=ActiveProject(id=PID, name="P", collection_id=PID, personal=False),
                     web_search=False, project_access="ok")
    block = _prompt_block(tc, "search_tool")
    # One STEP (parallel calls), not one call: a single call missed the source video in the golden test.
    assert "Search in ONE step" in block and "in parallel" in block
    assert "cite the ORIGINAL first" in block
    assert "MSQ-C7a" in block
    assert "never by a URL" in block and "`[name @ mm:ss–mm:ss]`" in block
    assert "Search in ONE step" not in _prompt_block(tc, None)


# --------------------------------------------------------------------------------------------------------
# Session pool (fake opener)
# --------------------------------------------------------------------------------------------------------

class _FakeSession:
    def __init__(self, n: int, fail_first_with: BaseException | None = None):
        self.n, self.calls, self._fail = n, [], fail_first_with

    async def call_tool(self, name, arguments):
        self.calls.append(arguments)
        await asyncio.sleep(0.01)
        if self._fail is not None:
            exc, self._fail = self._fail, None
            raise exc
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"session": self.n}))], isError=False)


class _Opener:
    def __init__(self, fail_reused_once: bool = False, open_error: BaseException | None = None):
        self.opened, self.closed, self.tokens = 0, 0, []
        self.fail_reused_once, self.open_error = fail_reused_once, open_error
        self.sessions: list[_FakeSession] = []

    @contextlib.asynccontextmanager
    async def __call__(self, url, token, timeout):
        self.opened += 1
        self.tokens.append(token)
        await asyncio.sleep(0.01)
        if self.open_error is not None:
            raise self.open_error
        s = _FakeSession(self.opened)
        self.sessions.append(s)
        try:
            yield s
        finally:
            self.closed += 1


@pytest.fixture
def opener():
    o = _Opener()
    client_mod.set_session_opener(o)
    yield o
    client_mod.reset_session_pools()


def _client() -> client_mod.SearchGatewayClient:
    return client_mod.SearchGatewayClient(url="https://gw.example/mcp", tool_name="t")


def test_parallel_and_sequential_calls_share_one_session(opener):
    async def go():
        c, tok = _client(), _tok()
        a, b = await asyncio.gather(c.call(tok, {"q": 1}), c.call(tok, {"q": 2}))
        d = await c.call(tok, {"q": 3})
        return a, b, d
    a, b, d = asyncio.run(go())
    assert [o.kind for o in (a, b, d)] == ["ok"] * 3
    assert opener.opened == 1 and len(opener.sessions[0].calls) == 3


def test_a_session_is_never_shared_across_tokens(opener):
    async def go():
        c = _client()
        await c.call(_tok(), {})
        await c.call(_tok("p" + "b" * 32), {})
    asyncio.run(go())
    assert opener.opened == 2 and opener.tokens[0] != opener.tokens[1]


def test_idle_expired_and_near_exp_sessions_are_reopened(opener, monkeypatch):
    async def go():
        c, tok = _client(), _tok()
        await c.call(tok, {})
        pool = client_mod._pool_for_loop()
        [conn] = pool._conns.values()
        conn.last_used -= client_mod.POOL_IDLE_S + 1  # idle too long
        await c.call(tok, {})
        await asyncio.sleep(0)
        # A token about to expire is not reused past its exp.
        short = _tok(life=10)
        await c.call(short, {})
        await c.call(short, {})
    asyncio.run(go())
    assert opener.opened == 4
    assert opener.closed >= 1


def test_a_gone_reused_session_is_reopened_once(opener):
    gone = RuntimeError("Session terminated")

    async def go():
        c, tok = _client(), _tok()
        await c.call(tok, {})
        opener.sessions[0]._fail = gone
        return await c.call(tok, {"again": True})
    out = asyncio.run(go())
    assert out.kind == "ok" and json.loads(out.result) == {"session": 2}
    assert opener.opened == 2


def test_a_call_error_on_a_session_is_not_retried(opener):
    class Denied(Exception):
        def __init__(self):
            super().__init__("Tool Execution Denied")
            self.error = SimpleNamespace(message="Tool Execution Denied: policy enforcement", code=-32002)

    async def go():
        c, tok = _client(), _tok()
        await c.call(tok, {})
        opener.sessions[0]._fail = Denied()
        return await c.call(tok, {})
    out = asyncio.run(go())
    assert out.phase == "call" and errors.classify(out)[0] == errors.POLICY_DENIED
    assert opener.opened == 1


def test_an_open_failure_classifies_as_initialize_and_is_not_pooled():
    class Rejected(Exception):
        response = SimpleNamespace(status_code=401)

    o = _Opener(open_error=Rejected("401"))
    client_mod.set_session_opener(o)
    try:
        async def go():
            c, tok = _client(), _tok()
            first = await c.call(tok, {})
            o.open_error = None
            second = await c.call(tok, {})
            return first, second
        first, second = asyncio.run(go())
        assert first.phase == "initialize" and first.kind == "http" and first.status == 401
        assert errors.classify(first)[0] == errors.PROJECT_ACCESS_UNAVAILABLE
        assert second.kind == "ok" and o.opened == 2
    finally:
        client_mod.reset_session_pools()


# --------------------------------------------------------------------------------------------------------
# Session pool over a REAL MCP streamable-HTTP server (mcp's own client and server, loopback only)
# --------------------------------------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_pooled_sessions_over_a_real_streamable_http_server():
    uvicorn = pytest.importorskip("uvicorn")
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("search-emulator")

    @server.tool()
    async def search_content(search_term: str, collection_id: str = "") -> str:
        await asyncio.sleep(0.05)
        return json.dumps({"results": [{"asset_id": "video:a", "start_ms": 0, "end_ms": 1, "title": search_term}]})

    app = server.streamable_http_app(host="127.0.0.1")
    handshakes: list[str] = []
    auth: set[str] = set()

    async def counting(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        chunks, more = [], True
        while more:
            msg = await receive()
            chunks.append(msg)
            more = msg.get("more_body", False) and msg["type"] == "http.request"
        body = b"".join(m.get("body", b"") for m in chunks)
        headers = dict(scope.get("headers") or [])
        auth.add(headers.get(b"authorization", b"").decode())
        try:
            method = json.loads(body).get("method") if body else None
        except ValueError:
            method = None
        if method in ("initialize", "server/discover"):
            handshakes.append(method)
        replay = iter(chunks)

        async def rx():
            try:
                return next(replay)
            except StopIteration:
                return await receive()
        return await app(scope, rx, send)

    port = _free_port()
    config = uvicorn.Config(counting, host="127.0.0.1", port=port, log_level="warning", lifespan="on", ws="none")
    srv = uvicorn.Server(config)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started, "loopback MCP server did not start"
    client_mod.reset_session_pools()
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        tok = _tok()

        async def go():
            c = client_mod.SearchGatewayClient(url=url, tool_name="search_content", timeout=10)
            outs = await asyncio.gather(*(c.call(tok, {"search_term": f"q{i}"}) for i in range(3)))
            outs.append(await c.call(tok, {"search_term": "later"}))
            tools = await c.list_tools(tok)
            client_mod._pool_for_loop().close_all()
            await asyncio.sleep(0.2)  # let the session task exit cleanly
            return outs, tools
        outs, tools = asyncio.run(go())
        assert [o.kind for o in outs] == ["ok"] * 4, [o.message for o in outs]
        assert [json.loads(o.result)["results"][0]["title"] for o in outs] == ["q0", "q1", "q2", "later"]
        assert [t.name for t in tools] == ["search_content"]
        # Four calls and a tools/list over ONE handshake (it was one per call), with the turn's bearer token.
        handshake_count = sum(1 for m in handshakes if m == "initialize") or len(handshakes)
        assert handshake_count == 1, handshakes
        assert {a for a in auth if a} == {f"Bearer {tok}"}
    finally:
        client_mod.reset_session_pools()
        srv.should_exit = True
        th.join(timeout=5)
