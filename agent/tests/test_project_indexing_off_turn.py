"""Project settings reach the Main_Agent turn: the description, and indexing OFF (no semantic search).

With indexing off, filename lookup (`project_status(name)`) still works; the Search_Tool answers
`indexing_off` without calling the gateway, and the prompt tells the agent why and what to do instead.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from test_project_search_runtime import SPEC, FakeClient, _stream  # noqa: E402

from project_search import errors  # noqa: E402
from project_search.context import TurnContext  # noqa: E402
from project_search.errors import SearchOutcome  # noqa: E402
from project_search.token_ref import ProjectTokenRef  # noqa: E402
from project_search.tool import ProjectSearchTool  # noqa: E402
from project_search.turn import _prompt_block  # noqa: E402

PID = "p" + "a" * 32


def _tok():
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    now = int(time.time())
    return f"{seg({'alg': 'RS256'})}.{seg({'sub': 'u', 'project_id': PID, 'iat': now, 'exp': now + 3600})}.sig"


def _tc(**ap):
    return TurnContext.parse({"turn_id": "t", "active_project": {"id": PID, "name": "Promo", "collection_id": PID, **ap},
                              "web_search": False, "project_access": "ok"})


def test_parse_reads_indexing_and_cleans_the_description():
    assert _tc().active_project.indexing is True                     # an older proxy sends no flag → ON
    assert _tc(indexing=False).active_project.indexing is False
    tc = _tc(description="Meridian\n promo  cuts\x07 for Q4 " + "x" * 600)
    assert tc.active_project.description.startswith("Meridian promo cuts for Q4 ")
    assert "\n" not in tc.active_project.description and len(tc.active_project.description) == 500
    assert _tc(description=["not", "text"]).active_project.description == ""


def test_prompt_block_carries_description_and_the_indexing_off_rule():
    block = _prompt_block(_tc(indexing=False, description="Trailer cuts for the Meridian launch"), "search_tool")
    assert '- Project description (written by the project\'s members): "Trailer cuts for the Meridian launch"' in block
    assert "Indexing is OFF for this project" in block and "`project_status` (pass `name`)" in block
    on = _prompt_block(_tc(), "search_tool")
    assert "Indexing is OFF" not in on and "Project description" not in on


def test_prompt_block_tells_the_agent_to_show_files_with_show_asset():
    """A link in the reply opens nothing (staging: 'show me sam's picture' pasted another session's URL)."""
    for tc in (_tc(), _tc(indexing=False)):
        for binding in ("search_tool", "search_tool_unavailable"):
            block = _prompt_block(tc, binding)
            assert "link in your reply does not open it" in block
            # Explicit open/play/show only -- never to answer a content question (session 484a9d5e).
            assert "explicitly asks to open, play or show a FILE" in block and "Do NOT call `show_asset`" in block
            # The lookup comes first, even when memory mentions the file (staging 2026-10-07: "sam's picture").
            assert "FIRST call `project_status`" in block and "even if memory mentions" in block
        off = _prompt_block(tc, None)
        assert ("Find files by name with `project_status` (pass `name`); open one with `show_asset` only when "
                "the user asks to open, play or show it." in off)
        assert off.count("`show_asset`") == 1
    # No Project_Tools bound this turn → no instruction to call them.
    assert "show_asset" not in _prompt_block(_tc(), "search_tool", project_tools=False)
    assert "show_asset" not in _prompt_block(_tc(), None, project_tools=False)


def test_search_tool_answers_indexing_off_without_calling_the_gateway():
    c = FakeClient(SearchOutcome("call", "ok", result="{}"))
    t = ProjectSearchTool(ProjectTokenRef(_tok()), SPEC, project_id=PID, turn_id="t", client=c, indexing_off=True)
    r = _stream(t, {"collection_id": PID, "search_term": "what is it about"})
    assert c.calls == []
    assert r["status"] == "error" and errors.INDEXING_OFF in json.dumps(r)


def test_search_tool_still_searches_when_indexing_is_on():
    from project_search import document_recall

    document_recall.reset_documents_seen()
    c = FakeClient(SearchOutcome("call", "ok", result=json.dumps({"results": []})))
    t = ProjectSearchTool(ProjectTokenRef(_tok()), SPEC, project_id=PID, turn_id="t", client=c)
    _stream(t, {"collection_id": PID, "search_term": "a wolf"})
    # The query alone: "a wolf" names no document and the project holds none, so the `#scene` document
    # companion is deferred and never needed (project_search.document_recall.companion_plan).
    assert [args["search_term"] for _tok_, args in c.calls] == ["a wolf"]


def test_prompt_block_tells_the_agent_to_browse_with_browse_project():
    for tc in (_tc(), _tc(indexing=False)):
        for binding in ("search_tool", "search_tool_unavailable", None):
            block = _prompt_block(tc, binding)
            assert block.count("`browse_project`") == 1 and "grid in the Renderer" in block
    assert "browse_project" not in _prompt_block(_tc(), "search_tool", project_tools=False)
    assert "browse_project" not in _prompt_block(_tc(), None, project_tools=False)
