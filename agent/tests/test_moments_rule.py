"""Unit tests for the Moments_Rule wiring (design Component 6; Req 3.3, 3.4, 3.5, 3.25).

Where ``test_moments_select.py`` / ``test_moments_manifest.py`` pin the pure selection and
manifest logic, this file pins the *rule* around it:

- the built-in registration in ``hooks.defaults.default_hook_specs`` — ``render-artifacts`` left
  byte-identical to today, with ``render-moments`` appended (Req 3.25);
- the lock in ``hooks.loader.build_engine`` / ``hooks.loader.LOCKED_BUILTINS`` — a user hook named
  ``render-moments`` can neither replace nor disable the built-in (Req 3.4, 3.5);
- ``lab.hook_crud`` refusing ``moments_render`` as a user action (Req 3.4, 3.5);
- ``hooks.actions.moments_render`` returning ``skipped`` for each Req 3.3 condition (Actor or
  Active_Project unknown, registry unreadable) plus the other guards, and running at most once per
  turn;
- the buffer/flush side effects: the composing marker emitted at buffer time and the batch flush
  pushing ``render_manifest_key`` on the SAME ``render_request_id``, a failed PutObject pushing the
  ``render_failed`` signal instead, and the moments slot flushing even when the artifact render
  batch is empty;
- the Search_Ledger ``source`` never reaches a Moment (the manifest is the same whatever a ledger
  entry's ``source`` is).

**Validates: Requirements 3.3, 3.4, 3.5, 3.25**
"""
from __future__ import annotations

import contextlib
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hooks import actions  # noqa: E402
from hooks.defaults import default_hook_specs  # noqa: E402
from hooks.engine import HookSpec  # noqa: E402
from hooks.loader import LOCKED_BUILTINS, build_engine  # noqa: E402
from lab import hook_crud  # noqa: E402
from orchestration import progress_channel  # noqa: E402
from project_search import turn_end  # noqa: E402
from project_search.assets import ResolvedAsset  # noqa: E402
from project_search.context import (  # noqa: E402
    ActiveProject,
    TurnContext,
    current_turn_context,
)
from project_search.ledger import SearchLedgerEntry  # noqa: E402
from project_search.session_hits import SessionHit  # noqa: E402
from project_search.turn_end import TurnOutcome  # noqa: E402
from render import moments as moments_mod  # noqa: E402

import render.artifacts as render_artifacts  # noqa: E402

# The exact ``render-artifacts`` condition shipped today (defaults.py), kept as the same three
# literal pieces so a byte-level drift in either place fails this test.
_RENDER_ARTIFACTS_CONDITION = (
    "result.new_artifacts "
    'and (tool.role != "status" or result.job_status == "SUCCEEDED") '
    'and run_id == ""'
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset every process-global the Moments_Rule touches, before and after each test."""
    def _reset():
        actions._moments_done.clear()
        actions._shown_assets.clear()
        actions._moments_slot = None
        actions._batch_open = False
        actions._render_batch.clear()
        progress_channel.set_active_channel(None)
        with turn_end._lock:
            turn_end._outcomes.clear()
            turn_end._answer_buffers.clear()

    _reset()
    yield
    _reset()


def _tc(turn_id: str = "t1", pid: str = "p1") -> TurnContext:
    return TurnContext(
        turn_id=turn_id,
        active_project=ActiveProject(id=pid, name="Proj", collection_id=pid, personal=False),
        web_search=False,
        project_access="ok",
    )


@contextlib.contextmanager
def _bound(*, turn_ctx, actor: str = "actor-1", session: str = "session-abc-123"):
    """Bind the turn context and the jobs actor/session for the duration of the block."""
    from jobs.context import reset_request_context, set_request_context

    tc_token = current_turn_context.set(turn_ctx)
    jobs_tokens = set_request_context(actor_id=actor, session_id=session)
    try:
        yield
    finally:
        reset_request_context(jobs_tokens)
        current_turn_context.reset(tc_token)


def _hit(asset_id="video:a1", start_ms=1000, end_ms=2000, final_score=0.5, **extra):
    hit = {"asset_id": asset_id, "start_ms": start_ms, "end_ms": end_ms,
           "final_score": final_score}
    hit.update(extra)
    return hit


def _session_hit(hit, *, pid="p1", i=0):
    return SessionHit(hit=hit, project_id=pid, turn_id="t1", tool_use_id=f"tu{i}", index=i,
                      recency=(1, 0, -(i + 1)))


def _outcome(*, turn_id="t1", pid="p1", session=(), turn=(), assets, citations=()):
    return TurnOutcome(
        turn_id=turn_id,
        project_id=pid,
        citations=list(citations),
        session_hits=list(session),
        turn_hits=list(turn),
        assets=assets,
        truncated=False,
    )


def _one_moment_outcome(turn_id="t1", pid="p1"):
    """A stored outcome that yields exactly one (searched-basis) Moment."""
    hit = _hit(asset_id="video:a1")
    sh = _session_hit(hit, pid=pid)
    assets = {"video:a1": ResolvedAsset("video:a1", "video", "clip.mp4", "indexed")}
    return _outcome(turn_id=turn_id, pid=pid, session=[sh], turn=[sh], assets=assets)


class _Channel(progress_channel.ProgressChannel):
    """A real channel so push/drain behave exactly as in production."""


# ===========================================================================
# 1. Built-in registration (Req 3.25)
# ===========================================================================
def test_default_specs_render_artifacts_byte_identical_plus_render_moments():
    specs = default_hook_specs()
    by_name = {s.name: s for s in specs}

    # The built-ins, render-artifacts first; render-graph (the knowledge-graph tab) follows render-moments.
    assert [s.name for s in specs] == ["render-artifacts", "render-moments", "render-graph"]

    # render-artifacts is unchanged, field for field (Req 3.25).
    expected_artifacts = HookSpec(
        name="render-artifacts",
        event="after_tool_result",
        condition=_RENDER_ARTIFACTS_CONDITION,
        action_type="render",
        action_params={"urls": "result.new_artifacts", "source_urls": "result.source_urls"},
        priority=50,
        enabled=True,
    )
    assert by_name["render-artifacts"] == expected_artifacts

    # render-moments is the second built-in, exactly as the design specifies.
    expected_moments = HookSpec(
        name="render-moments",
        event="session_end",
        condition='event == "session_end"',
        action_type="moments_render",
        action_params={},
        priority=40,
        enabled=True,
    )
    assert by_name["render-moments"] == expected_moments
    assert by_name["render-graph"] == HookSpec(
        name="render-graph", event="session_end", condition='event == "session_end"',
        action_type="graph_render", action_params={}, priority=41, enabled=True)


# ===========================================================================
# 2. The lock: a user hook may neither replace nor disable render-moments
#    (Req 3.4, 3.5)
# ===========================================================================
def _user_hook(**kwargs):
    base = dict(name="user-hook", event="session_end", condition='event == "session_end"',
                action_type="notify", action_params={}, priority=10, enabled=True,
                asset_id="aid")
    base.update(kwargs)
    return types.SimpleNamespace(**base)


def _render_moments_default() -> HookSpec:
    return next(s for s in default_hook_specs() if s.name == "render-moments")


def test_render_moments_is_a_locked_builtin():
    assert "render-moments" in LOCKED_BUILTINS


def test_enabled_user_override_of_render_moments_is_ignored():
    override = _user_hook(name="render-moments", action_type="notify",
                          action_params={"message": "hijacked"}, enabled=True)
    engine = build_engine([override])

    moments = [h for h in engine.hooks if h.name == "render-moments"]
    assert len(moments) == 1
    # The built-in survived untouched — not the user's notify spec.
    assert moments[0] == _render_moments_default()
    assert moments[0].action_type == "moments_render"
    # render-artifacts still present too.
    assert any(h.name == "render-artifacts" for h in engine.hooks)


def test_disabled_user_override_cannot_turn_render_moments_off():
    # A disabled override normally DISABLES a built-in; a locked one is dropped first, so the
    # built-in stays enabled.
    override = _user_hook(name="render-moments", enabled=False)
    engine = build_engine([override])

    moments = [h for h in engine.hooks if h.name == "render-moments"]
    assert len(moments) == 1
    assert moments[0] == _render_moments_default()
    assert moments[0].enabled is True


def test_unlocked_user_hook_is_still_added_alongside_builtins():
    # The lock only touches render-moments; an ordinary user hook is unaffected.
    engine = build_engine([_user_hook(name="my-notify")])
    names = {h.name for h in engine.hooks}
    assert {"render-artifacts", "render-moments", "my-notify"} <= names


# ===========================================================================
# 3. hook_crud refuses moments_render as a user action (Req 3.4, 3.5)
# ===========================================================================
def test_hook_crud_valid_actions_excludes_moments_render():
    assert "moments_render" not in hook_crud._VALID_ACTIONS
    assert hook_crud._VALID_ACTIONS == {"render", "notify", "dispatch"}


def test_hook_crud_validate_action_rejects_moments_render():
    with pytest.raises(ValueError):
        hook_crud._validate_action("moments_render")
    # The genuine user actions still validate.
    for action in ("render", "notify", "dispatch"):
        hook_crud._validate_action(action)


# ===========================================================================
# 4. moments_render skip reasons (Req 3.3 and the surrounding guards)
# ===========================================================================
def test_skip_no_turn_context():
    with _bound(turn_ctx=None):
        res = actions.moments_render({}, {})
    assert res.status == "skipped"
    assert res.detail == "no active-project turn context"


def test_skip_empty_turn_id():
    with _bound(turn_ctx=_tc(turn_id="")):
        res = actions.moments_render({}, {})
    assert res.status == "skipped"
    assert res.detail == "no active-project turn context"


def test_skip_unknown_active_project():
    tc = TurnContext(
        turn_id="t1",
        active_project=ActiveProject(id="", name="", collection_id="", personal=False),
        web_search=False, project_access="ok",
    )
    with _bound(turn_ctx=tc):
        res = actions.moments_render({}, {})
    assert res.status == "skipped"
    assert res.detail == "no active-project turn context"


def test_skip_actor_unknown():
    # An empty actor id makes jobs.context.current_actor_id raise -> skip (Req 3.3 "Actor unknown").
    with _bound(turn_ctx=_tc(), actor=""):
        res = actions.moments_render({}, {})
    assert res.status == "skipped"
    assert res.detail == "actor or session unknown"


def test_skip_no_turn_outcome():
    # Valid context, but finish_turn stored no outcome for this turn.
    with _bound(turn_ctx=_tc(turn_id="t-none")):
        res = actions.moments_render({}, {})
    assert res.status == "skipped"
    assert res.detail == "no turn outcome"


def test_skip_outcome_project_mismatch():
    turn_end._store_outcome("t1", _one_moment_outcome(turn_id="t1", pid="p-other"))
    with _bound(turn_ctx=_tc(turn_id="t1", pid="p1")):
        res = actions.moments_render({}, {})
    assert res.status == "skipped"
    assert res.detail == "outcome project mismatch"


def test_skip_registry_unreadable():
    # assets is None -> the Project_Registry could not be read (Req 3.3).
    turn_end._store_outcome("t1", _outcome(turn=[], assets=None))
    with _bound(turn_ctx=_tc(turn_id="t1")):
        res = actions.moments_render({}, {})
    assert res.status == "skipped"
    assert res.detail == "project registry unreadable"


def test_skip_no_moments_selected():
    # assets present but the hit's asset is absent -> no eligible Moment (Req 3.6).
    hit = _hit(asset_id="video:missing")
    sh = _session_hit(hit)
    out = _outcome(turn=[sh], assets={})  # empty registry map, not None
    turn_end._store_outcome("t1", out)
    with _bound(turn_ctx=_tc(turn_id="t1")):
        res = actions.moments_render({}, {})
    assert res.status == "skipped"
    assert res.detail == "no moments selected"


# ===========================================================================
# 4b. An image show_asset already opened is not repeated as a Moment (staging 1a5a3b78)
# ===========================================================================
def _image_and_video_outcome():
    img = _session_hit(_hit(asset_id="image:sam", start_ms=0, end_ms=1), i=0)
    vid = _session_hit(_hit(asset_id="video:a1"), i=1)
    assets = {"image:sam": ResolvedAsset("image:sam", "image", "sam.webp", "indexed"),
              "video:a1": ResolvedAsset("video:a1", "video", "clip.mp4", "indexed")}
    return _outcome(session=[img, vid], turn=[img, vid], assets=assets)


def test_a_shown_image_alone_makes_no_moments_tab():
    img = _session_hit(_hit(asset_id="image:sam", start_ms=0, end_ms=1))
    turn_end._store_outcome("t1", _outcome(
        session=[img], turn=[img], assets={"image:sam": ResolvedAsset("image:sam", "image", "sam.webp", "indexed")}))
    actions.note_shown_asset("t1", "image:sam")
    with _bound(turn_ctx=_tc(turn_id="t1")):
        res = actions.moments_render({}, {})
    assert res.status == "skipped" and res.detail == "every moment was already shown"


def test_a_shown_image_is_dropped_but_other_moments_stay(monkeypatch):
    turn_end._store_outcome("t1", _image_and_video_outcome())
    actions.note_shown_asset("t1", "image:sam")
    actions.open_render_batch()
    progress_channel.set_active_channel(_Channel())
    with _bound(turn_ctx=_tc(turn_id="t1")):
        res = actions.moments_render({}, {})
    assert res.status == "ok"
    items = actions._moments_slot["manifest"]["galleryMoments"]["items"]
    assert [i["ref"]["asset_id"] for i in items] == ["video:a1"]


def test_a_harvested_image_that_is_also_a_moment_yields_one_tab_not_two(monkeypatch):
    # ISSUE 4a (session aa9e30ba): the harvest/render path buffers a standalone image tab for a
    # picture that is ALSO a Moment. The image slot keys off a URL, not an asset id, so the old
    # step-6b dedup (fed only by note_shown_asset) never caught it and the picture showed twice.
    # Now moments_render resolves the harvested image URL to its in-place asset id and drops the
    # duplicate Moment, keeping the standalone tab and any time-based (video) Moments.
    from ingestion.generated import location_asset_id

    bucket = "mybucket"
    key = "projects/p1/sessions/s/output/pic.png"
    url = f"s3://{bucket}/{key}"
    img_aid = location_asset_id("image", "p1", bucket, key)

    img = _session_hit(_hit(asset_id=img_aid, start_ms=0, end_ms=1), i=0)
    vid = _session_hit(_hit(asset_id="video:a1"), i=1)
    assets = {img_aid: ResolvedAsset(img_aid, "image", "pic.png", "indexed"),
              "video:a1": ResolvedAsset("video:a1", "video", "clip.mp4", "indexed")}
    turn_end._store_outcome("t1", _outcome(session=[img, vid], turn=[img, vid], assets=assets))

    actions.open_render_batch()
    progress_channel.set_active_channel(_Channel())
    actions._buffer_render([url], [], "Image")  # the standalone image tab the harvest path makes
    with _bound(turn_ctx=_tc(turn_id="t1")):
        res = actions.moments_render({}, {})
    assert res.status == "ok"
    items = actions._moments_slot["manifest"]["galleryMoments"]["items"]
    # The duplicated image Moment is gone; the video Moment (a specific time) stays.
    assert [i["ref"]["asset_id"] for i in items] == ["video:a1"]


def test_a_shown_video_keeps_its_moments_and_other_turns_are_unaffected():
    turn_end._store_outcome("t1", _one_moment_outcome())
    actions.note_shown_asset("t1", "video:a1")   # a video's moments point at times: kept
    actions.note_shown_asset("t0", "image:sam")  # another turn's show has no effect here
    actions.open_render_batch()
    progress_channel.set_active_channel(_Channel())
    with _bound(turn_ctx=_tc(turn_id="t1")):
        res = actions.moments_render({}, {})
    assert res.status == "ok"
    assert actions.shown_assets_for("t1") == {"video:a1"}


# ===========================================================================
# 5. At most once per turn
# ===========================================================================
def test_moments_render_runs_at_most_once_per_turn():
    turn_end._store_outcome("t1", _one_moment_outcome())
    actions.open_render_batch()
    progress_channel.set_active_channel(_Channel())
    with _bound(turn_ctx=_tc(turn_id="t1")):
        first = actions.moments_render({}, {})
        second = actions.moments_render({}, {})
    assert first.status == "ok"
    assert "buffered" in first.detail
    assert second.status == "skipped"
    assert second.detail == "moments already handled this turn"


# ===========================================================================
# 6. Buffer + flush: composing marker, then render_manifest_key on the same rid
# ===========================================================================
def _find_event(events, label):
    return [e for e in events if e.label == label]


def test_buffer_emits_composing_marker_then_flush_pushes_manifest_key(monkeypatch):
    turn_end._store_outcome("t1", _one_moment_outcome())
    channel = _Channel()
    progress_channel.set_active_channel(channel)
    actions.open_render_batch()

    with _bound(turn_ctx=_tc(turn_id="t1"), session="session-abc-123"):
        res = actions.moments_render({}, {})
        rid = actions._moments_rid("session-abc-123", "t1")

    assert res.status == "ok"
    assert res.extra.get("render_request_id") == rid

    # The composing marker was emitted at buffer time, on the render agent, carrying the rid.
    composing = _find_event(channel.drain(), "↳ Composing render…")
    assert len(composing) == 1
    marker = composing[0]
    assert marker.status == "running"
    assert marker.agent == "render"
    assert marker.extra.get("render_request_id") == rid
    assert marker.extra.get("render_label") == "Moments"

    # Flush writes the manifest and announces it on the SAME rid.
    fake_key = "artifacts/session-abc-123/render/moments-xyz.json"
    captured = {}

    def _fake_write(session_id, manifest_id, manifest):
        captured["session_id"] = session_id
        captured["manifest_id"] = manifest_id
        captured["manifest"] = manifest
        return fake_key

    monkeypatch.setattr(render_artifacts, "write_session_manifest", _fake_write)
    artifact_keys = actions.flush_render_batch()

    # The moments slot flushes independently of the (empty) artifact batch.
    assert artifact_keys == []
    assert captured["session_id"] == "session-abc-123"
    assert captured["manifest"]["id"] == captured["manifest_id"]

    done = _find_event(channel.drain(), "↳ Moments")
    assert len(done) == 1
    step = done[0]
    assert step.status == "done"
    assert step.agent == "render"
    assert step.extra.get("render_manifest_key") == fake_key
    assert step.extra.get("render_request_id") == rid


# ===========================================================================
# 7. A failed PutObject pushes the render_failed signal (so the composing tab is removed)
# ===========================================================================
def test_failed_putobject_pushes_render_failed(monkeypatch):
    turn_end._store_outcome("t1", _one_moment_outcome())
    channel = _Channel()
    progress_channel.set_active_channel(channel)
    actions.open_render_batch()

    with _bound(turn_ctx=_tc(turn_id="t1"), session="session-abc-123"):
        actions.moments_render({}, {})
        rid = actions._moments_rid("session-abc-123", "t1")

    channel.drain()  # discard the composing marker

    # write_session_manifest returning "" models a failed/absent PutObject.
    monkeypatch.setattr(render_artifacts, "write_session_manifest",
                        lambda *a, **k: None)
    actions.flush_render_batch()

    failed = [e for e in channel.drain() if e.extra.get("render_failed")]
    assert len(failed) == 1
    step = failed[0]
    assert step.status == "failed"
    assert step.agent == "render"
    assert step.extra.get("render_request_id") == rid


# ===========================================================================
# 8. The moments slot flushes even when the artifact render batch is empty
# ===========================================================================
def test_flush_writes_moments_with_empty_render_batch(monkeypatch):
    turn_end._store_outcome("t1", _one_moment_outcome())
    channel = _Channel()
    progress_channel.set_active_channel(channel)
    actions.open_render_batch()

    with _bound(turn_ctx=_tc(turn_id="t1"), session="session-abc-123"):
        actions.moments_render({}, {})

    channel.drain()  # discard composing marker
    assert not actions._render_batch  # only a moments slot was buffered

    fake_key = "artifacts/session-abc-123/render/moments-xyz.json"
    monkeypatch.setattr(render_artifacts, "write_session_manifest",
                        lambda *a, **k: fake_key)
    artifact_keys = actions.flush_render_batch()

    assert artifact_keys == []  # no artifact tabs
    done = _find_event(channel.drain(), "↳ Moments")
    assert len(done) == 1
    assert done[0].extra.get("render_manifest_key") == fake_key


# ===========================================================================
# 9. The Search_Ledger source never reaches a Moment
# ===========================================================================
def _manifest_for_source(source: str) -> dict:
    hit = _hit(asset_id="video:a1", source_index="scene", text="a line")
    entry = SearchLedgerEntry(
        turn_id="t1", tool_use_id="tu1", project_id="p1", ok=True,
        hits=[hit], source=source,
    )
    turn_hits = turn_end._build_turn_hits([entry])
    assets = {"video:a1": ResolvedAsset("video:a1", "video", "clip.mp4", "indexed")}
    outcome = _outcome(session=turn_hits, turn=turn_hits, assets=assets)
    selection = moments_mod.select_moments(outcome)
    assert selection is not None
    return moments_mod.build_moments_manifest(
        selection, session_id="s1", turn_id="t1", created_at="2026-01-01T00:00:00.000Z"
    )


def test_ledger_source_never_reaches_a_moment():
    manifest = _manifest_for_source("search_tool")
    # The manifest is a Moments gallery with the one card; the ledger source is not carried onto it.
    assert manifest["template"] == "clip-gallery"
    assert len(manifest["galleryMoments"]["items"]) == 1
    assert "search_tool" not in json.dumps(manifest)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
