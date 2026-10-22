"""Tests for render batching — one task, one render tab.

The render hook fires per artifact-producing tool RESULT, so a request needing two
calls (two image options from two models) produced two tabs, while a request served by
a single call (the media gateway returning all outputs at once) produced one. The unit
was the tool result, which only coincided with the task. These tests pin the batching
that makes the unit the invocation instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

import asyncio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hooks import actions  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    actions._render_batch.clear()
    actions._batch_open = False
    yield
    actions._render_batch.clear()
    actions._batch_open = False


class _Dispatch:
    """Records dispatch_render calls instead of invoking the Render Agent."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, urls, source_urls=None, label=None, render_request_id=None, render_key=None, tool_name=None):
        self.calls.append({"urls": list(urls), "source_urls": source_urls, "label": label})
        if self.fail:
            raise RuntimeError("render agent unavailable")
        return f"manifest-{len(self.calls)}"


def _patch_dispatch(monkeypatch, dispatch):
    import render.artifacts as render_artifacts

    monkeypatch.setattr(render_artifacts, "dispatch_render", dispatch, raising=False)


def test_two_image_results_become_one_render(monkeypatch):
    """THE case: two run_model calls in one turn must yield ONE tab."""
    d = _Dispatch()
    _patch_dispatch(monkeypatch, d)
    actions.open_render_batch()
    actions._buffer_render(["https://cdn/a.png"], [], "Run Model")
    actions._buffer_render(["https://cdn/b.png"], [], "Run Model")
    actions.flush_render_batch()
    assert len(d.calls) == 1
    assert d.calls[0]["urls"] == ["https://cdn/a.png", "https://cdn/b.png"]


def test_unlike_media_do_not_share_a_manifest(monkeypatch):
    # One manifest maps to one template: images can share a view, a video and a data
    # table cannot, so they get separate tabs.
    d = _Dispatch()
    _patch_dispatch(monkeypatch, d)
    actions.open_render_batch()
    actions._buffer_render(["https://cdn/a.png"], [], "Images")
    actions._buffer_render(["https://cdn/v.mp4"], [], "Video")
    actions.flush_render_batch()
    assert len(d.calls) == 2
    grouped = {tuple(c["urls"]) for c in d.calls}
    assert ("https://cdn/a.png",) in grouped
    assert ("https://cdn/v.mp4",) in grouped


def test_distinct_analyses_of_the_same_media_class_get_their_own_tab(monkeypatch):
    """Two video views from DIFFERENT work must not merge into one tab.

    Keyed on media class alone, every video in a turn landed in one slot titled
    after whichever tool fired first. Session 842bb417 showed the cost: the source
    video, the transcoded proxy and the face-analysis overlay merged, and the face
    view -- video-annotations with 5 identities and 40 boxes, the richest output in
    the run -- never surfaced while three empty generic-preview tabs did.
    """
    d = _Dispatch()
    _patch_dispatch(monkeypatch, d)
    actions.open_render_batch()
    actions._buffer_render(["https://cdn/faces.mp4"], [], "Detect Faces")
    actions._buffer_render(["https://cdn/shots.mp4"], [], "Segment Shots")
    actions.flush_render_batch()
    assert len(d.calls) == 2
    by_label = {c["label"]: c["urls"] for c in d.calls}
    assert by_label["Detect Faces"] == ["https://cdn/faces.mp4"]
    assert by_label["Segment Shots"] == ["https://cdn/shots.mp4"]


def test_same_work_still_batches_into_one_tab(monkeypatch):
    # The label is the grouping axis, so repeated results from the SAME work still
    # collapse -- the original one-task-one-tab guarantee is unchanged.
    d = _Dispatch()
    _patch_dispatch(monkeypatch, d)
    actions.open_render_batch()
    actions._buffer_render(["https://cdn/a.mp4"], [], "Detect Faces")
    actions._buffer_render(["https://cdn/b.mp4"], [], "Detect Faces")
    actions.flush_render_batch()
    assert len(d.calls) == 1
    assert d.calls[0]["urls"] == ["https://cdn/a.mp4", "https://cdn/b.mp4"]


def test_duplicate_urls_are_collapsed(monkeypatch):
    d = _Dispatch()
    _patch_dispatch(monkeypatch, d)
    actions.open_render_batch()
    actions._buffer_render(["https://cdn/a.png"], [], "x")
    actions._buffer_render(["https://cdn/a.png"], [], "x")
    actions.flush_render_batch()
    assert d.calls[0]["urls"] == ["https://cdn/a.png"]


def test_first_label_wins(monkeypatch):
    # The first label names the work that STARTED the task, which titles the tab better
    # than whatever tool happened to finish last.
    d = _Dispatch()
    _patch_dispatch(monkeypatch, d)
    actions.open_render_batch()
    actions._buffer_render(["https://cdn/a.png"], [], "Run Model")
    actions._buffer_render(["https://cdn/b.png"], [], "Something Else")
    actions.flush_render_batch()
    assert d.calls[0]["label"] == "Run Model"


def test_source_urls_are_merged_and_deduped(monkeypatch):
    d = _Dispatch()
    _patch_dispatch(monkeypatch, d)
    actions.open_render_batch()
    actions._buffer_render(["https://cdn/a.png"], ["https://src/orig.mp4"], "x")
    actions._buffer_render(["https://cdn/b.png"], ["https://src/orig.mp4"], "x")
    actions.flush_render_batch()
    assert d.calls[0]["source_urls"] == ["https://src/orig.mp4"]


def test_images_with_distinct_job_labels_coalesce_into_one_gallery(monkeypatch):
    # Each fal image-gen job carries a unique job-id label; keying image slots on the label
    # gave one tab per image (19 tabs for a 12-image storyboard, session ccbc9559). Plain image
    # deliverables must coalesce into ONE gallery regardless of their (per-job) labels.
    d = _Dispatch()
    _patch_dispatch(monkeypatch, d)
    actions.open_render_batch()
    actions._buffer_render(["https://cdn/panel1.jpg"], [], "01a0011c-1111")
    actions._buffer_render(["https://cdn/panel2.jpg"], [], "01a0011c-2222")
    actions._buffer_render(["https://cdn/panel3.jpg"], [], "01a0011c-3333")
    actions.flush_render_batch()
    assert len(d.calls) == 1
    assert d.calls[0]["urls"] == [
        "https://cdn/panel1.jpg", "https://cdn/panel2.jpg", "https://cdn/panel3.jpg"]


def test_images_coalesce_but_distinct_video_analyses_stay_separate(monkeypatch):
    # The image-coalescing must NOT leak into video: two distinct video analyses keep their tabs.
    d = _Dispatch()
    _patch_dispatch(monkeypatch, d)
    actions.open_render_batch()
    actions._buffer_render(["https://cdn/sheetA.png"], [], "job-a")
    actions._buffer_render(["https://cdn/sheetB.png"], [], "job-b")
    actions._buffer_render(["https://cdn/faces.mp4"], [], "Detect Faces")
    actions._buffer_render(["https://cdn/shots.mp4"], [], "Segment Shots")
    actions.flush_render_batch()
    url_sets = sorted(tuple(c["urls"]) for c in d.calls)
    assert ("https://cdn/faces.mp4",) in url_sets
    assert ("https://cdn/shots.mp4",) in url_sets
    assert ("https://cdn/sheetA.png", "https://cdn/sheetB.png") in url_sets
    assert len(d.calls) == 3

    # An async job whose result lands OUTSIDE a turn must render immediately rather
    # than wait for a flush that will never come.
    actions.open_render_batch()
    assert actions._batch_is_open() is True
    actions.flush_render_batch()
    assert actions._batch_is_open() is False


def test_flush_with_nothing_buffered_is_a_no_op(monkeypatch):
    d = _Dispatch()
    _patch_dispatch(monkeypatch, d)
    actions.open_render_batch()
    assert actions.flush_render_batch() == []
    assert d.calls == []


def test_one_failing_dispatch_does_not_strand_the_others(monkeypatch):
    # Marking happens at buffer time, so a raising flush would leave artifacts marked
    # and never shown. Each media class is dispatched independently.
    class _Flaky(_Dispatch):
        def __call__(self, urls, source_urls=None, label=None, render_request_id=None, render_key=None, tool_name=None):
            self.calls.append({"urls": list(urls), "source_urls": source_urls, "label": label})
            if len(self.calls) == 1:
                raise RuntimeError("first one fails")
            return "manifest-ok"

    d = _Flaky()
    _patch_dispatch(monkeypatch, d)
    actions.open_render_batch()
    actions._buffer_render(["https://cdn/a.png"], [], "Images")
    actions._buffer_render(["https://cdn/v.mp4"], [], "Video")
    actions.flush_render_batch()          # must not raise
    assert len(d.calls) == 2              # both attempted
    assert actions._render_batch == {}    # buffer cleared either way


def test_opening_a_new_batch_discards_a_stale_one():
    actions.open_render_batch()
    actions._buffer_render(["https://cdn/stale.png"], [], "x")
    actions.open_render_batch()
    assert actions._render_batch == {}


def test_media_class_buckets():
    assert actions._media_class("https://c/x.PNG") == "image"
    assert actions._media_class("https://c/x.mp4?sig=abc") == "video"
    assert actions._media_class("https://c/x.wav") == "audio"
    assert actions._media_class("https://c/x.json") == "other"


def test_flush_survives_a_failure_in_context_building(monkeypatch):
    """The flush must not depend on _base() or on user hooks succeeding.

    Marking happens at BUFFER time, so anything that prevents the flush leaves
    artifacts recorded-as-dispatched and never shown -- a silently blank Renderer,
    which is worse than the duplicate tabs this replaced. Local validation caught
    exactly this: the flush originally sat after ctx.update(self._base()), so a
    failure there stranded the whole batch.
    """
    from hooks.provider import HarnessHookProvider

    d = _Dispatch()
    _patch_dispatch(monkeypatch, d)
    monkeypatch.setattr(actions, "_mirror_external_media", lambda u: u)

    # Deliberately unconstructed: _base() will raise AttributeError.
    provider = HarnessHookProvider.__new__(HarnessHookProvider)

    with pytest.raises(AttributeError):
        provider._on_session_start(None)
    assert actions._batch_is_open() is True, "batch must open despite _base() failing"

    ctx = {
        "event": "after_tool_result",
        "tool": {"name": "usr-x___run_model"},
        "result": {
            "urls": ["https://cdn/a.png"],
            "source_urls": [],
            "new_artifacts": ["https://cdn/a.png"],
        },
    }
    actions.render_action({}, ctx)
    actions.render_action({}, {**ctx, "result": {**ctx["result"], "urls": ["https://cdn/b.png"]}})
    assert d.calls == [], "nothing should dispatch mid-turn"

    # _on_session_end is async (Strands awaits AfterInvocationEvent via
    # invoke_callbacks_async); the blocking flush is offloaded with asyncio.to_thread,
    # so the AttributeError from _base() still propagates out of the awaited coroutine
    # and the finally still flushes.
    with pytest.raises(AttributeError):
        asyncio.run(provider._on_session_end(None))
    assert len(d.calls) == 1, "the finally must flush even when _base() raises"
    assert d.calls[0]["urls"] == ["https://cdn/a.png", "https://cdn/b.png"]
