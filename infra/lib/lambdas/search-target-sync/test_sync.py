"""Unit tests for the Search target sync/verify (media-harness-projects-access task 8.8).

Run: `uv run --with pytest pytest infra/lib/lambdas/search-target-sync/test_sync.py`.
Covers sync failure, missing search_content, non-string collection_id/search_image_s3uri, the
refused pre-rename search_image_uri, and success.
"""

from datetime import datetime, timezone

import pytest

import sync

TARGET = "mediaworks-harness-m2c-search"
EXPECTED_NAME = f"{TARGET}___hybrid-search___search_content"


def _tool(cid="string", image=None):
    props = {"collection_id": {"type": cid}} if cid else {}
    if image is not None:
        props["search_image_s3uri"] = {"type": image}
    return {"name": "hybrid-search___search_content", "inputSchema": {"properties": props}}


def test_success_returns_tool_name_and_count():
    name, count = sync.verify_tools([_tool(), {"name": "other"}], TARGET)
    assert name == EXPECTED_NAME
    assert count == 2


def test_success_with_string_search_image_s3uri():
    name, _ = sync.verify_tools([_tool(image="string")], TARGET)
    assert name == EXPECTED_NAME


def test_missing_search_content():
    with pytest.raises(sync.SyncError) as e:
        sync.verify_tools([{"name": "something_else"}], TARGET)
    assert e.value.step == "missing_tool"


def test_nonstring_collection_id():
    with pytest.raises(sync.SyncError) as e:
        sync.verify_tools([_tool(cid="number")], TARGET)
    assert e.value.step == "missing_property"


def test_nonstring_search_image_s3uri():
    with pytest.raises(sync.SyncError) as e:
        sync.verify_tools([_tool(image="number")], TARGET)
    assert e.value.step == "missing_property"


def test_poll_fails_on_terminal_status():
    with pytest.raises(sync.SyncError) as e:
        sync.poll_target_ready(
            lambda: {"status": "FAILED"},
            datetime.now(timezone.utc),
            attempts=1,
            sleep=lambda _s: None,
        )
    assert e.value.step == "sync"


def test_sync_and_verify_happy_path():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = datetime(2026, 1, 2, tzinfo=timezone.utc)
    calls = {"sync": 0}

    def synchronize():
        calls["sync"] += 1

    def get_target():
        return {"status": "READY", "lastSynchronizedAt": later}

    result = sync.sync_and_verify(
        synchronize=synchronize,
        get_target=get_target,
        list_tools=lambda: [_tool()],
        target_name=TARGET,
        started_at=started,
        sleep=lambda _s: None,
    )
    assert calls["sync"] == 1
    assert result["SearchToolName"] == EXPECTED_NAME
    assert result["ToolCount"] == 1
    assert result["LastSynchronizedAt"] == later.isoformat()


# ---- Real upstream schema shape: properties carry a description beside the type ----------------

def test_string_property_with_description_is_accepted():
    # The shape M2C's live hybrid-search___search_content returns (alpha.20261014a).
    tool = {
        "name": "hybrid-search___search_content",
        "inputSchema": {"properties": {
            "collection_id": {"description": "Collection to search (omit for 'default')", "type": "string"},
            "search_image_s3uri": {"description": "s3:// image uri for visual similarity search", "type": "string"},
        }},
    }
    name, _ = sync.verify_tools([tool], TARGET)
    assert name == EXPECTED_NAME


def test_property_with_description_but_wrong_type_is_rejected():
    tool = {"name": "hybrid-search___search_content",
            "inputSchema": {"properties": {"collection_id": {"description": "x", "type": "integer"}}}}
    with pytest.raises(sync.SyncError) as e:
        sync.verify_tools([tool], TARGET)
    assert e.value.step == "missing_property"


# ---- Upstream tools/list (GetGatewayTarget does not return tools) -------------------------------

class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        return self.responses.pop(0)


def _page(tools, cursor=None, sse=False):
    import json as _json
    result = {"tools": tools}
    if cursor:
        result["nextCursor"] = cursor
    msg = _json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})
    if sse:
        return 200, "text/event-stream", f"event: message\ndata: {msg}\n\n"
    return 200, "application/json", msg


def test_list_upstream_tools_follows_next_cursor():
    http = FakeHttp([_page([{"name": "a"}], cursor="c1"), _page([{"name": "b"}], cursor="c2"), _page([{"name": "c"}])])
    tools = sync.list_upstream_tools("https://m2c/mcp", "tok", http=http)
    assert [t["name"] for t in tools] == ["a", "b", "c"]
    assert len(http.calls) == 3
    assert b'"cursor": "c1"' in http.calls[1][1]
    assert http.calls[0][2]["Authorization"] == "Bearer tok"


def test_list_upstream_tools_parses_sse():
    http = FakeHttp([_page([{"name": "a"}], sse=True)])
    assert [t["name"] for t in sync.list_upstream_tools("u", "t", http=http)] == ["a"]


def test_list_upstream_tools_http_error_names_the_step():
    http = FakeHttp([(403, "application/json", '{"error":"insufficient_scope"}')])
    with pytest.raises(sync.SyncError) as e:
        sync.list_upstream_tools("u", "t", http=http)
    assert e.value.step == "tools_list"
    assert "403" in str(e.value)


def test_list_upstream_tools_stops_on_runaway_cursor():
    http = FakeHttp([_page([], cursor="same")] * (sync.MAX_TOOL_PAGES + 1))
    with pytest.raises(sync.SyncError) as e:
        sync.list_upstream_tools("u", "t", http=http)
    assert e.value.step == "tools_list"


def test_token_grant_success_and_failure():
    ok = FakeHttp([(200, "application/json", '{"access_token":"abc","expires_in":3600}')])
    assert sync.fetch_client_credentials_token("https://idp/token", "cid", "sec", "s/invoke", http=ok) == "abc"
    body = ok.calls[0][1].decode()
    assert "grant_type=client_credentials" in body and "scope=s%2Finvoke" in body

    bad = FakeHttp([(400, "application/json", '{"error":"invalid_client"}')])
    with pytest.raises(sync.SyncError) as e:
        sync.fetch_client_credentials_token("https://idp/token", "cid", "sec", "", http=bad)
    assert e.value.step == "token"
    assert "invalid_client" in str(e.value)


def test_terminal_status_message_carries_status_reasons():
    with pytest.raises(sync.SyncError) as e:
        sync.poll_target_ready(
            lambda: {"status": "SYNCHRONIZE_UNSUCCESSFUL", "statusReasons": ["Access denied when retrieving secret"]},
            datetime.now(timezone.utc),
            attempts=1,
            sleep=lambda _s: None,
        )
    assert "Access denied when retrieving secret" in str(e.value)


# ---- The pre-rename spelling is refused: the Search_Policy constrains only search_image_s3uri ----

def test_an_upstream_with_the_pre_rename_image_input_is_refused():
    tool = {"name": "hybrid-search___search_content", "inputSchema": {"properties": {
        "collection_id": {"type": "string"}, "search_image_uri": {"type": "string"}}}}
    with pytest.raises(sync.SyncError) as e:
        sync.verify_tools([tool], TARGET)
    assert e.value.step == "unsupported_schema"
    assert "search_image_uri" in str(e.value)
