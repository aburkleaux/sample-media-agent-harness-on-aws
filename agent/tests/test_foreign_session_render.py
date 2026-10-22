"""A tool result belonging to another session must not render into this one.

An agent hunting for the merge job read a job from a DIFFERENT session, and its media was
rendered into the current session's viewer: a 12-minute Netflix demo clip appeared as a tab
beside a 5-second cat video. The render hook fires wherever a tool result lands, so "this
result mentions media" is not the same as "this session produced media".

Job records carry sessionId, so a foreign result identifies itself.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

THIS = "44b3e54e-cd2b-4099-a34a-66df062b790d"
OTHER = "5ced0ead-90dd-4ff1-bb3d-e1e0c067e98c"


def _guard(result, current):
    import jobs.context as jc
    from hooks import actions

    tok = jc._current_session_id.set(current)
    try:
        return actions._foreign_session_in({"result": result})
    finally:
        jc._current_session_id.reset(tok)


def test_a_result_from_another_session_is_detected():
    result = {"content": [{"text": json.dumps(
        {"jobId": "abc", "sessionId": OTHER,
         "sourceKey": "artifacts/Netflix_Open_Content_Meridian.mp4"})}]}
    assert _guard(result, THIS) == OTHER


def test_a_result_from_this_session_is_not_suppressed():
    result = {"content": [{"text": json.dumps({"jobId": "abc", "sessionId": THIS})}]}
    assert _guard(result, THIS) == ""


def test_a_result_naming_both_sessions_is_ours_to_render():
    """A plan aggregate can reference a sibling; that must not suppress the render."""
    result = {"content": [{"text": json.dumps(
        {"jobs": [{"sessionId": OTHER}, {"sessionId": THIS}]})}]}
    assert _guard(result, THIS) == ""


def test_a_result_naming_no_session_renders_normally():
    """Absence of evidence must not suppress a legitimate render."""
    result = {"content": [{"text": json.dumps({"urls": ["https://cdn/x.mp4"]})}]}
    assert _guard(result, THIS) == ""


def test_an_unknown_current_session_never_suppresses():
    result = {"content": [{"text": json.dumps({"sessionId": OTHER})}]}
    assert _guard(result, "") == ""


def test_snake_case_session_id_is_also_recognised():
    result = {"content": [{"text": json.dumps({"session_id": OTHER})}]}
    assert _guard(result, THIS) == OTHER



# ---------------------------------------------------------------------------
# URL-level cross-session guard (Phase 1). The result-level `_foreign_session_in`
# above catches a result whose OWN sessionId is foreign; this catches a
# same-session result that merely QUOTES another session's artifact URL — the
# real incident where session 4f9d0d02 auto-rendered e27e5945's
# `final_reel_crossfade.mp4` (an agent-olive continuation loading prior work).
# ---------------------------------------------------------------------------

# Real incident coordinates.
CUR = "4f9d0d02-764b-4e67-923e-45ca912d3af5"
PRIOR = "e27e5945-ba47-45d7-bb0b-3f33feb2be03"
CDN = "https://dexamplecdn002.cloudfront.net"


def _filter(urls, current):
    import jobs.context as jc
    from hooks import actions

    tok = jc._current_session_id.set(current)
    try:
        return actions._drop_foreign_session_urls(urls)
    finally:
        jc._current_session_id.reset(tok)


def _is_foreign(url, current):
    from hooks import actions
    return actions._url_is_foreign_session(url, current)


def test_foreign_session_artifact_url_is_dropped():
    foreign = f"{CDN}/artifacts/{PRIOR}/output/final_reel_crossfade.mp4"
    assert _filter([foreign], CUR) == []


def test_current_session_artifact_url_is_kept():
    own = f"{CDN}/artifacts/{CUR}/output/olive_ai_joke_reel_v4.mp4"
    assert _filter([own], CUR) == [own]


def test_shared_external_namespace_is_kept():
    # fal outputs live under the shared, session-less external/ prefix — this session's
    # own generated clips (e.g. the lip-sync segments), never to be dropped.
    ext = f"{CDN}/artifacts/external/6990bbd41d396654c3e4e5c4.mp4"
    assert _filter([ext], CUR) == [ext]


def test_non_artifact_url_is_kept():
    # A presigned M2C / gateway output has no artifacts/<uuid>/ in its path — a genuine
    # this-turn output with no session in the URL; must pass.
    presigned = "https://m2c-bucket.s3.amazonaws.com/run/x.mp4?X-Amz-Signature=abc"
    assert _filter([presigned], CUR) == [presigned]


def test_non_uuid_artifact_segment_is_kept():
    # `artifacts/uploads/...` and the like are not a session prefix — leave them.
    up = f"{CDN}/artifacts/uploads/intro.mp4"
    assert _filter([up], CUR) == [up]


def test_mixed_list_drops_only_foreign_and_preserves_order():
    own = f"{CDN}/artifacts/{CUR}/output/a.mp4"
    ext = f"{CDN}/artifacts/external/b.mp4"
    foreign = f"{CDN}/artifacts/{PRIOR}/output/c.mp4"
    assert _filter([own, foreign, ext], CUR) == [own, ext]


def test_unknown_current_session_fails_open():
    foreign = f"{CDN}/artifacts/{PRIOR}/output/c.mp4"
    # No current session bound -> never suppress on absence of evidence.
    assert _filter([foreign], "") == [foreign]


def test_bare_s3_key_form_is_matched():
    # Candidate can also be an s3:// URI or a bare key; the guard must see the prefix.
    assert _is_foreign(f"s3://bucket/artifacts/{PRIOR}/output/c.mp4", CUR) is True
    assert _is_foreign(f"artifacts/{PRIOR}/output/c.mp4", CUR) is True
    assert _is_foreign(f"artifacts/{CUR}/output/c.mp4", CUR) is False


def test_uuid_match_is_case_insensitive():
    assert _is_foreign(f"{CDN}/artifacts/{PRIOR.upper()}/x.mp4", CUR) is True
    assert _is_foreign(f"{CDN}/artifacts/{CUR.upper()}/x.mp4", CUR) is False
