"""One node with TWO distinct views needs two manifest keys, not one that overwrites.

The open question left by Rule 1. "Identity is the job" gives one job ⇒ one manifest key, and
that is right until a single node legitimately produces two views that cannot share a template.
Audio stem separation is the real case: it emits the stems (`speech.wav`, `music.wav`, `sfx.wav`)
AND a data sidecar. An audio timeline and a data table are different templates, so they are
different tabs.

`_buffer_render` already knows this — it keys slots on `(render_key or label, media class)`, so
that node yields TWO slots and `flush_render_batch` makes TWO `dispatch_render` calls. But both
carry the same `render_key` (the node id), so `_render_manifest_id` returns the same string, both
write `artifacts/<sid>/render/<node>.json`, and **the second silently overwrites the first**. One
of the two views just disappears; nothing errors.

So the batching layer and the identity layer disagree about what a tab is. The fix keeps the
batching layer's answer, because it is the correct one: the unit of a tab is the SLOT, so the
manifest key should be the slot's identity, not the job's.

Deliverable-anchored coalescing runs FIRST: when a tool declares its `deliverable:` and that
file is present in the call, its byproducts ride the deliverable's single slot, so a mapped tool
no longer reaches the two-slot case at all. The sub-key below therefore serves output with NO
deliverable contract (unmapped tools) that still emits two genuinely different views of one node.

Sub-key rule (deliberately narrow):
  * one slot for a node  -> the key stays the bare node id, exactly as today. This is the common
    case and MUST NOT change, or every existing manifest key moves and every live session
    re-renders its tabs once.
  * two or more slots for one node -> each key is suffixed with that slot's media class,
    `<node>--audio` / `<node>--data`. Deterministic, and derived from the very thing that already
    separated the slots rather than from anything new.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from hooks import actions  # noqa: E402

CDN = "https://cdn.cloudfront.net"
SID = "sess-1"
PLAN = "plan-1"
NODE = "sg_stems"
OUT = f"{CDN}/artifacts/{SID}/output/{PLAN}/{NODE}"


@pytest.fixture(autouse=True)
def _clean():
    actions._render_batch.clear()
    actions._batch_open = False
    yield
    actions._render_batch.clear()
    actions._batch_open = False


def _flush_capturing(monkeypatch):
    """Run a real flush, capturing the render_key handed to each dispatch."""
    seen: list[tuple[str, int]] = []

    def _fake_dispatch(urls, source_urls=None, label=None, render_request_id=None,
                       render_key="", tool_name=""):
        seen.append((render_key, len(urls)))
        return f"artifacts/{SID}/render/{render_key or 'hash'}.json"

    import render.artifacts as render_artifacts
    monkeypatch.setattr(render_artifacts, "dispatch_render", _fake_dispatch)
    actions.flush_render_batch()
    return seen


def test_two_views_from_one_node_get_distinct_keys(monkeypatch):
    """THE BUG: both slots key on the node, so the second manifest overwrote the first.

    No tool_name here on purpose: this is the UNMAPPED case the sub-key now serves. A tool that
    DECLARES a deliverable coalesces its byproducts into the deliverable's one slot instead (see
    test_a_mapped_tools_byproduct_rides_the_deliverable), so the split below is exercised on
    output with no deliverable contract — two genuinely different views (audio + data) of one node
    that legitimately cannot share a template.
    """
    actions.open_render_batch()
    actions._buffer_render([f"{OUT}/speech.wav", f"{OUT}/music.wav"], [], "Audio Stems",
                           render_key=NODE)
    actions._buffer_render([f"{OUT}/stems.json"], [], "Audio Stems", render_key=NODE)
    seen = _flush_capturing(monkeypatch)

    assert len(seen) == 2, f"expected two dispatches (audio + data), got {seen!r}"
    keys = [k for k, _ in seen]
    assert len(set(keys)) == 2, (
        f"both views wrote the same manifest key {keys!r} — the second overwrites the first "
        "and one view vanishes")
    # And each must still be recognisably the node's.
    assert all(k.startswith(NODE) for k in keys), keys


def test_a_mapped_tools_byproduct_rides_the_deliverable(monkeypatch):
    """A mapped tool's non-deliverable output must NOT open its own tab (redundant-tabs fix).

    shot-segmentation declares ``deliverable: shots.json``; the proxy ``segment_0000.mp4`` it also
    emits is a byproduct, not a second view. Before this, the two media classes (data + video)
    split into two slots → two manifests → a redundant "Video, 0 markers" tab beside the marker
    view. With the deliverable contract read at buffer time (through the SAME catalog parser
    select_render_skill_by_tool uses), the byproduct rides the deliverable's single slot, so
    exactly ONE dispatch is made and it keeps the bare node key (no media-class collision).

    Riding is safe rather than dropping because the render is deliverable-focused: it projects
    shots.json onto the source and ignores the extra candidate.
    """
    actions.open_render_batch()
    actions._buffer_render([f"{OUT}/shots.json", f"{OUT}/segment_0000.mp4"], [f"{CDN}/src.mp4"],
                           "Shot Segmentation", render_key=NODE,
                           tool_name="shot-segmentation___segment_shots")
    seen = _flush_capturing(monkeypatch)

    assert len(seen) == 1, f"the byproduct opened its own tab instead of riding: {seen!r}"
    key, n = seen[0]
    assert key == NODE, f"single slot must keep the bare node key, got {key!r}"
    assert n == 2, "the byproduct should ride the deliverable's slot (present, not dropped)"


def test_the_suffix_is_the_media_class(monkeypatch):
    actions.open_render_batch()
    actions._buffer_render([f"{OUT}/speech.wav"], [], "Audio Stems", render_key=NODE)
    actions._buffer_render([f"{OUT}/stems.json"], [], "Audio Stems", render_key=NODE)
    keys = sorted(k for k, _ in _flush_capturing(monkeypatch))
    assert keys == [f"{NODE}--audio", f"{NODE}--other"], keys


def test_a_single_view_node_keeps_its_bare_key(monkeypatch):
    """Back-compat, and it is not optional.

    Suffixing unconditionally would move EVERY existing manifest key, so every live session
    would re-render its tabs once and old manifests would stop deduping against new ones.
    """
    actions.open_render_batch()
    actions._buffer_render([f"{OUT}/shots.json"], [], "Shot Segmentation", render_key="sg_shots")
    keys = [k for k, _ in _flush_capturing(monkeypatch)]
    assert keys == ["sg_shots"], keys


def test_two_different_nodes_are_untouched(monkeypatch):
    """Only a COLLISION on one node triggers the suffix; distinct nodes already differ."""
    actions.open_render_batch()
    actions._buffer_render([f"{OUT}/shots.json"], [], "Shots", render_key="sg_shots")
    actions._buffer_render([f"{CDN}/artifacts/{SID}/output/{PLAN}/sg_diarize/spk.json"], [],
                           "Diarize", render_key="sg_diarize")
    keys = sorted(k for k, _ in _flush_capturing(monkeypatch))
    assert keys == ["sg_diarize", "sg_shots"], keys


def test_slots_with_no_render_key_are_untouched(monkeypatch):
    """An empty render_key means "fall back to the content hash" — never suffix that.

    `--audio` on an empty key would produce the key `--audio`, shared by every keyless audio
    render in the session.
    """
    actions.open_render_batch()
    actions._buffer_render([f"{OUT}/a.wav"], [], "A", render_key="")
    actions._buffer_render([f"{OUT}/b.json"], [], "B", render_key="")
    keys = [k for k, _ in _flush_capturing(monkeypatch)]
    assert keys == ["", ""], keys


def test_the_suffix_is_stable_across_flushes(monkeypatch):
    """Same node, same classes ⇒ same keys, so a re-render converges instead of adding tabs."""
    def _run():
        actions._render_batch.clear()
        actions.open_render_batch()
        actions._buffer_render([f"{OUT}/speech.wav"], [], "Audio Stems", render_key=NODE)
        actions._buffer_render([f"{OUT}/stems.json"], [], "Audio Stems", render_key=NODE)
        return sorted(k for k, _ in _flush_capturing(monkeypatch))
    assert _run() == _run()


# ------------------------------------------------------------------ the tab TITLE
# Splitting one node into two manifests means the user now sees two tabs where they saw one.
# If both carry the node's single `label` they read as identical duplicates — which looks like the
# duplicate-tab bug this whole effort has been removing. The split has to be legible.
def test_the_two_tabs_get_distinguishable_labels(monkeypatch):
    seen: list[tuple[str, str]] = []

    def _fake_dispatch(urls, source_urls=None, label=None, render_request_id=None,
                       render_key="", tool_name=""):
        seen.append((render_key, label or ""))
        return f"artifacts/{SID}/render/{render_key}.json"

    import render.artifacts as render_artifacts
    monkeypatch.setattr(render_artifacts, "dispatch_render", _fake_dispatch)
    actions.open_render_batch()
    actions._buffer_render([f"{OUT}/speech.wav"], [], "Audio Stems", render_key=NODE)
    actions._buffer_render([f"{OUT}/stems.json"], [], "Audio Stems", render_key=NODE)
    actions.flush_render_batch()

    labels = [lbl for _, lbl in seen]
    assert len(set(labels)) == 2, f"two tabs with the same title {labels!r} read as duplicates"
    # Still recognisably the same work, not renamed out of recognition.
    assert all(l.startswith("Audio Stems") for l in labels), labels


def test_a_single_view_label_is_untouched(monkeypatch):
    seen: list[str] = []

    def _fake_dispatch(urls, source_urls=None, label=None, render_request_id=None,
                       render_key="", tool_name=""):
        seen.append(label or "")
        return "k"

    import render.artifacts as render_artifacts
    monkeypatch.setattr(render_artifacts, "dispatch_render", _fake_dispatch)
    actions.open_render_batch()
    actions._buffer_render([f"{OUT}/shots.json"], [], "Shot Segmentation", render_key="sg_shots")
    actions.flush_render_batch()
    assert seen == ["Shot Segmentation"], seen
