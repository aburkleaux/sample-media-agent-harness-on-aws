"""AgentCore Web Search: the per-turn gate (agent/src/web_search.py) and where it is enforced.

Three layers (media-harness-web-search-toggle, 2026-10-20 update): the deployment flag (WEB_SEARCH_ENABLED), the
chat's toggle (turn context `web_search`), and enforcement here — the WebSearch tool is left out of the toolset,
stripped from anything dispatched, and refused by the gateway wrapper on a turn that may not search.
"""
from __future__ import annotations

import contextvars
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import web_search as ws  # noqa: E402

TOOL = "mediaworks-harness-web-search___WebSearch"


def _tool(name):
    return types.SimpleNamespace(tool_name=name)


def _in_ctx(fn):
    return contextvars.copy_context().run(fn)


@pytest.fixture
def deployed(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")


def test_the_tool_is_recognised_namespaced_or_bare():
    assert ws.is_web_search_tool(TOOL)
    assert ws.is_web_search_tool("WebSearch")
    assert not ws.is_web_search_tool("mediaworks-harness-aws-mcp___aws___search_documentation")
    assert not ws.is_web_search_tool("hybrid-search___search_content")
    assert not ws.is_web_search_tool(None)


def test_a_turn_searches_only_when_deployed_and_its_toggle_is_on(monkeypatch):
    def run():
        monkeypatch.delenv("WEB_SEARCH_ENABLED", raising=False)
        ws.set_turn_allowed(True)
        assert not ws.turn_allows()                       # not deployed: never
        monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
        assert ws.turn_allows()
        ws.set_turn_allowed(False)
        assert not ws.turn_allows()                       # the chat has it off
        ws.set_turn_allowed(None)
        assert not ws.turn_allows()                       # nothing said: fail closed
    _in_ctx(run)


def test_the_toolset_drops_web_search_unless_the_turn_may_search(deployed):
    tools = [_tool(TOOL), _tool("mediaworks-harness-aws-mcp___aws___read_documentation")]

    def run():
        ws.set_turn_allowed(False)
        assert [t.tool_name for t in ws.filter_tools(tools)] == ["mediaworks-harness-aws-mcp___aws___read_documentation"]
        ws.set_turn_allowed(True)
        assert len(ws.filter_tools(tools)) == 2
    _in_ctx(run)


def test_dispatched_tool_names_are_stripped_when_off(deployed):
    def run():
        ws.set_turn_allowed(False)
        assert ws.strip_tool_names([TOOL, "fal___submit_job"]) == ["fal___submit_job"]
        ws.set_turn_allowed(True)
        assert ws.strip_tool_names([TOOL, "fal___submit_job"]) == [TOOL, "fal___submit_job"]
        assert ws.strip_tool_names([TOOL], allowed=False) == []
        assert ws.strip_tool_names(None) is None
    _in_ctx(run)


def test_the_wrapper_refuses_a_web_search_call_on_a_turn_that_may_not_search(deployed):
    from jobs.strands_tool_wrap import _web_search_refusal

    def run():
        ws.set_turn_allowed(False)
        r = _web_search_refusal(TOOL, {"toolUseId": "u1", "input": {"query": "x"}})
        assert r["status"] == "error" and r["toolUseId"] == "u1"
        assert "turned off for this chat" in r["content"][0]["text"] and "Nothing was searched" in r["content"][0]["text"]
        assert _web_search_refusal("fal___submit_job", {"toolUseId": "u1"}) is None   # other tools untouched
        ws.set_turn_allowed(True)
        assert _web_search_refusal(TOOL, {"toolUseId": "u1"}) is None                 # on: the call runs
    _in_ctx(run)


def test_the_refusal_says_why_when_not_deployed(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_ENABLED", raising=False)
    from jobs.strands_tool_wrap import _web_search_refusal
    r = _in_ctx(lambda: (ws.set_turn_allowed(True), _web_search_refusal(TOOL, {"toolUseId": "u"}))[1])
    assert "not available in this deployment" in r["content"][0]["text"]


def test_the_prompt_tells_the_main_agent_how_to_cite_web_results_apart(deployed):
    def run():
        ws.set_turn_allowed(False)
        assert ws.prompt_lines() == [ws.CITATION_RULE]       # AWS-docs results are cited even with search off
        ws.set_turn_allowed(True)
        text = "\n".join(ws.prompt_lines())
        assert "[<title>](<https url>)" in text and "apart from project citations" in text
        assert "untrusted" in text
    _in_ctx(run)


def test_the_planner_never_sees_web_search_and_plan_nodes_never_carry_it():
    from orchestration.plan_and_execute_tool import _without_web_search
    cat = {"tools": [{"name": TOOL}, {"name": "fal___submit_job"}, "WebSearch"],
           "gateway_tools": [{"name": TOOL}, {"name": "x___y"}]}
    out = _without_web_search(cat)
    assert out["tools"] == [{"name": "fal___submit_job"}]
    assert out["gateway_tools"] == [{"name": "x___y"}]
    assert cat["tools"][0] == {"name": TOOL}                      # the original is not mutated


def test_the_task_agent_resolves_web_search_only_for_work_from_a_turn_with_it_on(monkeypatch, deployed):
    import task_agent

    class _Gw:
        is_connected = True
        auth_type = "aws_iam"

        def get_all_tools(self):
            return [types.SimpleNamespace(mcp_tool=types.SimpleNamespace(name=TOOL)),
                    types.SimpleNamespace(mcp_tool=types.SimpleNamespace(name="mediaworks-harness-aws-mcp___aws___list_regions"))]

    monkeypatch.setattr(task_agent.mcp_manager, "_gateways", {"platform": _Gw()})

    def run(flag):
        ws.set_turn_allowed(flag)
        return sorted(task_agent._all_gateway_tools_by_name())

    assert _in_ctx(lambda: run(False)) == ["mediaworks-harness-aws-mcp___aws___list_regions"]
    assert _in_ctx(lambda: run(None)) == ["mediaworks-harness-aws-mcp___aws___list_regions"]
    assert TOOL in _in_ctx(lambda: run(True))


def test_the_wiring_is_in_place_main_dispatch_and_task_agent():
    """Asserted by TEXT (the entrypoints need live runtimes): main sets the turn from the turn context and
    filters its toolset; dispatch_tool strips names and tells the sub-agent; the task agent reads it."""
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    main = open(os.path.join(src, "main.py")).read()
    assert '_web_search.set_turn_allowed(bool(getattr(_turn_ctx, "web_search", False))' in main
    assert "gw_tools = _web_search.filter_tools(mcp_manager.get_all_added_tools())" in main
    dispatch = open(os.path.join(src, "orchestration", "dispatch_tool.py")).read()
    assert "tools_list = _web_search.strip_tool_names(tools_list)" in dispatch
    assert '"web_search": _web_search.turn_allows()' in dispatch
    task = open(os.path.join(src, "task_agent.py")).read()
    assert '_web_search.set_turn_allowed(payload.get("web_search") is True' in task


def test_web_citations_never_parse_as_project_citations():
    """Requirement 1 criterion 8 (citation separation): a `[title](https://url)` web citation is not a Project
    citation, while a `[name @ mm:ss–mm:ss]` beside it still is."""
    from project_search.citations import parse_citations
    text = ("Per the announcement [Web Search expands to 3 Regions](https://aws.amazon.com/about-aws/whats-new/x) "
            "and the clip [Meridian.mp4 @ 00:23–00:30].")
    cites = parse_citations(text)
    assert len(cites) == 1
    assert "Meridian" in str(cites[0])
    assert parse_citations("[Release notes](https://docs.aws.amazon.com/x) and [Title @ home](https://e.com/a)") == []



def test_every_web_or_aws_doc_result_tells_its_caller_to_cite_the_sources():
    """User rule (2026-10-20): every web search or AWS documentation search, direct or dispatched, gives its sources.
    The note rides on the result, so a dispatched sub-agent (whose prompt has no citation rule) gets it too."""
    from strands.types._events import ToolResultEvent
    from jobs.strands_tool_wrap import _note_citable_sources
    ok = lambda: ToolResultEvent({"toolUseId": "t", "status": "success", "content": [{"text": '{"results": []}'}]})
    for name in ("mediaworks-harness-web-search___WebSearch", "mediaworks-harness-aws-mcp___aws___search_documentation",
                 "mediaworks-harness-aws-mcp___aws___read_documentation", "mediaworks-harness-aws-mcp___aws___recommend"):
        out = _note_citable_sources(name, [ok()])[0].get("tool_result")["content"]
        assert out[-1]["text"].startswith("SOURCES:") and "[<title>](<https url>)" in out[-1]["text"], name
        assert "answering for another agent" in out[-1]["text"]
    for name in ("fal___submit_job", "text-to-speech___get_voice", "project-search___search"):
        assert len(_note_citable_sources(name, [ok()])[0].get("tool_result")["content"]) == 1, name
    err = ToolResultEvent({"toolUseId": "t", "status": "error", "content": [{"text": "boom"}]})
    assert len(_note_citable_sources("x___WebSearch", [err])[0].get("tool_result")["content"]) == 1


def test_the_main_agent_always_cites_sources_a_sub_agent_returned():
    def run():
        for allowed in (False, True):
            ws.set_turn_allowed(allowed)
            assert "dispatched agent's reply" in "\n".join(ws.prompt_lines())
    _in_ctx(run)


def test_with_search_on_the_agent_searches_instead_of_guessing_urls(deployed):
    """User rule (2026-10-20): always use search unless a particular site is given (case 2 of the live test fetched
    a guessed anthropic.com URL with http_request instead of searching)."""
    def run():
        ws.set_turn_allowed(True)
        text = "\n".join(ws.prompt_lines())
        assert "never guess a URL" in text and "http_request" in text and "domainFilter" in text
        ws.set_turn_allowed(False)
        assert "never guess a URL" not in "\n".join(ws.prompt_lines())
    _in_ctx(run)


def test_the_main_agent_binds_web_search_directly_on_a_turn_that_may_search(deployed):
    """Staging 2026-10-21: the prompt said 'search with WebSearch' but the orchestrator binds no gateway tools, so
    its WebSearch calls came back 'Unknown tool' and one turn told the user search was down."""
    tool = types.SimpleNamespace(tool_name="mediaworks-harness-web-search___WebSearch")
    other = types.SimpleNamespace(tool_name="mediaworks-harness-aws-mcp___aws___search_documentation")
    find = lambda pred: [t for t in (tool, other) if pred(t.tool_name)]

    def run():
        ws.set_turn_allowed(True)
        assert ws.direct_tools(find) == [tool]
        ws.set_turn_allowed(False)
        assert ws.direct_tools(find) == []
        def boom(pred): raise RuntimeError("no gateway")
        ws.set_turn_allowed(True)
        assert ws.direct_tools(boom) == []
    _in_ctx(run)
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "main.py")).read()
    assert "_web_search.direct_tools(mcp_manager.find_tools)" in src


def test_search_results_are_sources_never_mirrored_or_rendered(monkeypatch):
    """Staging ca859a24 (2026-10-21): a PDF in a search result was copied into artifacts/external and opened as a
    'Document' tab on a web-research question. Source-tool results are cited, not rendered or mirrored."""
    import asyncio
    from strands.types._events import ToolResultEvent
    from hooks.actions import render_action
    from jobs.strands_tool_wrap import _ResilientMCPMixin

    for name in ("mediaworks-harness-web-search___WebSearch", "mediaworks-harness-aws-mcp___aws___read_documentation"):
        res = render_action({"urls": "result.urls"}, {"tool": {"name": name},
                                                      "result": {"urls": ["https://example.org/paper.pdf"]}})
        assert res.status == "skipped" and "source to cite" in res.detail, name

    obj = types.SimpleNamespace(tool_name="mediaworks-harness-web-search___WebSearch")
    ev = ToolResultEvent({"toolUseId": "t", "status": "success",
                          "content": [{"text": '{"results":[{"url":"https://example.org/paper.pdf"}]}'}]})
    out = asyncio.run(_ResilientMCPMixin._mirror_result_media(obj, ev))
    assert out is ev                                             # untouched: nothing copied
