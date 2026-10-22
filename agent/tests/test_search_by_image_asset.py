"""Search by an image ASSET (search_image_asset_id): the harness sends that image's Search_Rendition.

The index embeds renditions and M2C's search role may read only renditions, so a query image is always a
rendition; an original's S3 location is refused before M2C (it used to fail there with AccessDenied).
Only images can be a query: video and audio are refused as "not supported at the moment".
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from project_search import call_checks, compact, tool as tool_mod  # noqa: E402
from project_search.assets import RegistryUnavailable  # noqa: E402
from project_search.errors import SearchOutcome  # noqa: E402
from project_search.token_ref import ProjectTokenRef  # noqa: E402
from project_search.tool import ProjectSearchTool  # noqa: E402

PID = "p" + "a" * 32
IMG = f"image:{uuid.uuid4()}"
RENDITION = f"s3://ingest-bkt/projects/{PID}/{IMG}/rendition.jpg"
SPEC = compact.with_harness_inputs({"name": "tgt___hybrid-search___search_content", "description": "d",
                                    "inputSchema": {"json": {"type": "object", "properties": {
                                        "search_term": {"type": "string"},
                                        "search_image_s3uri": {"type": "string"}}}}})


def _tok() -> str:
    iat = int(time.time())
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f'{seg({"alg": "RS256"})}.{seg({"sub": "u", "project_id": PID, "iat": iat, "exp": iat + 3600})}.sig'


class FakeClient:
    tool_name = SPEC["name"]

    def __init__(self):
        self.calls = []

    async def call(self, token, args):
        self.calls.append(dict(args))
        return SearchOutcome("call", "ok", result=json.dumps({"results": []}))


class FakeImages:
    def __init__(self, entry=None, fail=False):
        self.entry, self.fail, self.calls = entry, fail, []

    def search_images(self, project_id, asset_ids, session_id):
        self.calls.append((project_id, list(asset_ids), session_id))
        if self.fail:
            raise RegistryUnavailable("down")
        return {asset_ids[0]: self.entry} if self.entry is not None else {}


def _run(args, images, session="sess-1", bound="sess-1"):
    """One tool call with ``bound`` as the request's session (``None``: none bound), like a real turn."""
    from jobs.context import reset_request_context, set_request_context

    token = set_request_context(actor_id="u", session_id=bound) if bound else None
    try:
        return _run_bound(args, images, session)
    finally:
        if token is not None:
            reset_request_context(token)


def _run_bound(args, images, session):
    client = FakeClient()
    scope = call_checks.CallScope(session_id=session, project_id=PID, ingest_bucket="ingest-bkt",
                                  proxy_bucket="proxy-bkt", frame_keys=lambda: frozenset())
    t = ProjectSearchTool(ProjectTokenRef(_tok()), SPEC, project_id=PID, turn_id="t-" + uuid.uuid4().hex,
                          client=client, scope=scope, images=images)

    async def go():
        return [ev async for ev in t.stream({"toolUseId": "tu", "name": SPEC["name"], "input": args}, {})]

    result = asyncio.run(go())[-1].tool_result
    return result, client.calls


def test_the_model_sees_the_input_and_its_rules():
    prop = SPEC["inputSchema"]["json"]["properties"]["search_image_asset_id"]
    assert prop["type"] == "string"
    assert "INSTEAD of search_term" in prop["description"]
    assert "not supported at the moment" in prop["description"]


def test_an_image_asset_is_searched_by_its_rendition():
    images = FakeImages({"uri": RENDITION, "displayName": "shot.jpg"})
    result, calls = _run({"collection_id": PID, "search_image_asset_id": IMG}, images)
    assert result["status"] == "success"
    assert images.calls == [(PID, [IMG], "sess-1")]
    assert len(calls) == 1
    assert calls[0]["search_image_s3uri"] == RENDITION
    assert "search_image_asset_id" not in calls[0]          # M2C never receives the asset id
    assert "search_term" not in calls[0]


def _error_text(result):
    return result["content"][0]["text"]


def test_video_and_audio_are_not_supported_and_never_reach_the_service_or_m2c():
    for aid in (f"video:{uuid.uuid4()}", f"audio:{uuid.uuid4()}"):
        images = FakeImages({"uri": "s3://x/y"})
        result, calls = _run({"collection_id": PID, "search_image_asset_id": aid}, images)
        assert result["status"] == "error" and "not supported at the moment" in _error_text(result)
        assert images.calls == [] and calls == []


def test_each_unusable_image_says_why():
    cases = {"not_ready": "still being prepared", "not_found": "not in the active project",
             "not_image": "not supported at the moment", "unreadable": "cannot be read as a search query"}
    for reason, text in cases.items():
        result, calls = _run({"collection_id": PID, "search_image_asset_id": IMG}, FakeImages({"reason": reason}))
        assert result["status"] == "error" and text in _error_text(result), reason
        assert calls == []
    result, calls = _run({"collection_id": PID, "search_image_asset_id": IMG}, FakeImages(fail=True))
    assert "could not be looked up" in _error_text(result) and calls == []


def test_one_query_per_call_and_a_well_formed_id():
    images = FakeImages({"uri": RENDITION})
    for args in ({"search_image_asset_id": IMG, "search_term": "cars"},
                 {"search_image_asset_id": IMG, "search_image_s3uri": RENDITION}):
        result, calls = _run({"collection_id": PID, **args}, images)
        assert "exactly one of" in _error_text(result) and calls == []
    for bad in ("shot.jpg", "s3://ingest-bkt/x.jpg", 5):
        result, calls = _run({"collection_id": PID, "search_image_asset_id": bad}, images)
        assert "must be an image asset_id" in _error_text(result) and calls == []
    assert images.calls == []


def test_an_originals_location_is_refused_before_m2c():
    original = f"s3://ingest-bkt/projects/{PID}/{IMG}/shot_03_frame.jpg"
    result, calls = _run({"collection_id": PID, "search_image_s3uri": original}, FakeImages())
    assert result["status"] == "error" and "search_image_asset_id" in _error_text(result)
    assert calls == []
    result, calls = _run({"collection_id": PID, "search_image_s3uri": RENDITION}, FakeImages())
    assert result["status"] == "success" and calls[0]["search_image_s3uri"] == RENDITION


def test_the_blocking_lookup_runs_off_the_event_loop(monkeypatch):
    seen = []
    real = asyncio.to_thread

    async def spy(fn, *a, **kw):
        seen.append(getattr(fn, "__name__", ""))
        return await real(fn, *a, **kw)

    monkeypatch.setattr(tool_mod.asyncio, "to_thread", spy)
    _run({"collection_id": PID, "search_image_asset_id": IMG}, FakeImages({"uri": RENDITION}))
    assert seen == ["search_images"]


def test_the_session_is_read_when_the_tool_runs_not_when_the_turn_is_wired():
    # main.prepare_turn builds the CallScope BEFORE set_request_context, so its session id is "" in a real
    # turn; the tool must take the session bound at call time.
    images = FakeImages({"uri": RENDITION})
    result, calls = _run({"collection_id": PID, "search_image_asset_id": IMG}, images, session="", bound="sess-bound")
    assert result["status"] == "success" and calls[0]["search_image_s3uri"] == RENDITION
    assert images.calls == [(PID, [IMG], "sess-bound")]

