"""A gateway's tools/list is paginated; every page must be collected.

Strands' ``list_tools_sync`` returns ONE page and leaves the cursor to the
caller. When we ignored it, the harness gateway reported a constant 13 tools
against a true ~20 and fal's ``run_model`` -- one page past the boundary --
became invisible. The agent then told the user no image-generation tool
existed. A truncated tool list looks exactly like a short one, so nothing
errored; these tests are the only thing standing between that and a repeat.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strands.types.collections import PaginatedList  # noqa: E402

import mcp_manager  # noqa: E402


class _Tool:
    """Minimal stand-in carrying just the name the manager reads."""

    def __init__(self, name):
        self.tool_name = name


class _PagingClient:
    """Serves a fixed set of pages, recording the cursors it was handed."""

    def __init__(self, pages):
        self._pages = pages
        self.cursors_seen = []

    def list_tools_sync(self, pagination_token=None):
        self.cursors_seen.append(pagination_token)
        names, next_token = self._pages[pagination_token]
        return PaginatedList([_Tool(n) for n in names], token=next_token)


def _conn(client):
    gw = mcp_manager.GatewayConnection.__new__(mcp_manager.GatewayConnection)
    gw.name = "test-gateway"
    gw._client = client
    return gw


def test_follows_cursor_across_pages():
    client = _PagingClient({
        None: (["search_models", "check_job"], "c1"),
        "c1": (["run_model", "cancel_job"], "c2"),
        "c2": (["get_job_result"], None),
    })
    names = [mcp_manager._extract_tool_name(t) for t in _conn(client)._list_all_tools()]

    # run_model lives on page 2 -- the exact tool the live regression lost.
    assert "run_model" in names
    assert names == [
        "search_models", "check_job", "run_model", "cancel_job", "get_job_result",
    ]
    assert client.cursors_seen == [None, "c1", "c2"]


def test_single_page_makes_exactly_one_call():
    """The common case must not pay for a second round trip."""
    client = _PagingClient({None: (["only_tool"], None)})
    assert len(_conn(client)._list_all_tools()) == 1
    assert client.cursors_seen == [None]


def test_stalled_cursor_terminates():
    """A server that returns the same cursor forever must not hang the runtime."""
    client = _PagingClient({None: (["a", "b"], None), "stuck": (["a"], "stuck")})
    client._pages[None] = (["a", "b"], "stuck")
    tools = _conn(client)._list_all_tools()

    # Dedupe by name stops unbounded growth; the page cap stops the loop.
    assert [mcp_manager._extract_tool_name(t) for t in tools] == ["a", "b"]
    assert len(client.cursors_seen) <= mcp_manager.GatewayConnection._MAX_TOOL_PAGES + 1


def test_paginated_list_token_survives_only_without_list_wrapping():
    """Guards the trap that hid this bug: list() silently drops the token."""
    page = PaginatedList([_Tool("x")], token="more")
    assert page.pagination_token == "more"
    assert getattr(list(page), "pagination_token", None) is None


def test_per_target_summary_exposes_a_truncated_target():
    """The connect log must show the per-target split, not just a total.

    "connected with 13 tools" is indistinguishable from a healthy gateway when
    one target has silently synchronised 3 of its 11 tools.
    """
    gw = mcp_manager.GatewayConnection.__new__(mcp_manager.GatewayConnection)
    gw._tool_metadata = [
        {"name": "usr-abc___cancel_job", "description": ""},
        {"name": "usr-abc___check_job", "description": ""},
        {"name": "usr-abc___get_job_result", "description": ""},
        {"name": "aws-mcp___aws___read_documentation", "description": ""},
        {"name": "x_amz_bedrock_agentcore_search", "description": ""},
    ]
    out = gw._per_target_summary()
    assert "usr-abc=3" in out
    assert "aws-mcp=1" in out
    assert "(un-namespaced)=1" in out


def test_per_target_summary_quiet_for_single_target():
    """No noise when the split adds nothing."""
    gw = mcp_manager.GatewayConnection.__new__(mcp_manager.GatewayConnection)
    gw._tool_metadata = [{"name": "only___a", "description": ""}]
    assert gw._per_target_summary() == ""
