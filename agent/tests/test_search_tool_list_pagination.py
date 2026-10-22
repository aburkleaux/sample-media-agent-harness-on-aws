"""The Search_Gateway's `tools/list` is paged; `search_content` must be found on any page.

Since the 2026-10-06 search-target sync (65 tools behind one Cedar permit) every turn logged
`search tool spec fetch failed (LookupError); no search tool this turn`: the client read page 1 only,
and `search_content` was not on it. Search was unbound in every session from then on. Same failure
mode as fal's `run_model` on page 2 (test_gateway_tool_pagination.py).
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from project_search import client as client_mod  # noqa: E402
from project_search import tool as tool_mod  # noqa: E402

NAME = "mediaworks-harness-m2c-search___hybrid-search___search_content"


class _PagedSession:
    def __init__(self, pages):
        self.pages, self.cursors = pages, []

    async def list_tools(self, params=None):
        cursor = getattr(params, "cursor", None) if params is not None else None
        self.cursors.append(cursor)
        names, nxt = self.pages[cursor]
        return SimpleNamespace(tools=[SimpleNamespace(name=n, description="d", input_schema={"type": "object"},
                                                      inputSchema={"type": "object"})
                                      for n in names], next_cursor=nxt)


def _client_over(session):
    c = client_mod.SearchGatewayClient(url="https://gw", tool_name=NAME)

    async def fake_session(token, body):
        return {"v": "call"}, (lambda: body(session))
    c._session = fake_session  # type: ignore[assignment]
    return c


def test_list_tools_follows_the_cursor_to_the_last_page():
    s = _PagedSession({None: ([], "c1"), "c1": (["other___x"], "c2"), "c2": ([NAME], None)})
    tools = asyncio.run(_client_over(s).list_tools("tok"))
    assert [t.name for t in tools] == ["other___x", NAME]
    assert s.cursors == [None, "c1", "c2"]


def test_fetch_spec_finds_the_tool_on_a_later_page():
    tool_mod._spec_cache.clear()
    s = _PagedSession({None: ([], "c1"), "c1": ([NAME], None)})

    class Ref:
        async def current(self):
            return "tok"
    spec = asyncio.run(tool_mod.fetch_spec(_client_over(s), Ref()))
    assert spec["name"] == NAME


def test_list_tools_stops_at_the_page_cap():
    pages = {None: ([], "c0")}
    pages.update({f"c{i}": ([], f"c{i + 1}") for i in range(100)})
    s = _PagedSession(pages)
    asyncio.run(_client_over(s).list_tools("tok"))
    assert len(s.cursors) == client_mod._MAX_TOOL_PAGES
