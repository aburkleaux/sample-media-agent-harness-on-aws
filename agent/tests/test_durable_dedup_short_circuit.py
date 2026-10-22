"""Skip the durable S3 dedup scan when the render already has a stable key.

`_already_rendered_urls` is expensive: one `list_objects_v2` plus a `get_object` for up to 50
manifests, on EVERY dispatch. It exists because url-keyed identity could not tell that two url
forms were the same object, so the only way to know "did this session already render this" was to
read the persisted manifests back.

With a stable key that reasoning no longer applies. A render that has a `render_key` — a plan
node, `canonical_render_key(job_id)`, or a path-derived node — writes a DETERMINISTIC S3 key, so
a re-render overwrites the one manifest. The tab count is right by construction, and the scan can
only tell us something we would act on identically. Proven by experiment: disabling the scan
entirely leaves the whole suite green except one test that uses it as a FIXTURE to reach the
"dropped, resolve the marker" branch.

So the scan is kept exactly where it is still load-bearing — the KEYLESS case, where identity
falls back to a content hash — and skipped where it is not. That removes ~51 S3 calls from the
common render path without giving up the guard.

Deliberately NOT deleted outright, despite the ledger. Two narrow cases still need it:
  * a render with no job AND no derivable node (a synchronous `artifact` call, a legacy flat key)
  * a cold start DURING a registry outage, where `canonical_render_key` degrades to the sanitized
    job id instead of the node id, so the two renders key differently
    (see test_render_key_cold_start.py, which asserts that limitation explicitly)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

import render.artifacts as render_artifacts  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    render_artifacts._rendered_url_sets.clear()
    yield
    render_artifacts._rendered_url_sets.clear()


def _dispatch(monkeypatch, render_key: str):
    """Call dispatch_render far enough to see whether the durable scan ran."""
    calls: list[str] = []
    monkeypatch.setattr(render_artifacts, "_already_rendered_urls",
                        lambda sid: calls.append(sid) or set(), raising=False)
    monkeypatch.setattr(render_artifacts, "RENDER_AGENT_ARN",
                        "arn:aws:bedrock-agentcore:us-west-2:0:runtime/r-TEST", raising=False)

    import jobs.context as jc
    monkeypatch.setattr(jc, "current_session_id", lambda: "sess-1", raising=False)
    monkeypatch.setattr(jc, "current_actor_id", lambda: "actor-1", raising=False)

    def _stop(client, **kw):
        raise RuntimeError("stop before invoking the runtime")
    monkeypatch.setattr(jc, "invoke_agent_runtime_traced", _stop, raising=False)

    try:
        render_artifacts.dispatch_render(
            ["https://cdn.cloudfront.net/artifacts/s1/output/p1/n1/shots.json"],
            render_key=render_key)
    except RuntimeError:
        pass
    return calls


def test_a_keyed_render_skips_the_durable_scan(monkeypatch):
    calls = _dispatch(monkeypatch, render_key="sg_shots")
    assert calls == [], (
        "a render with a stable key writes a deterministic S3 key, so the ~51-call durable scan "
        f"is redundant; it ran anyway: {calls!r}")


def test_a_keyless_render_still_runs_the_durable_scan(monkeypatch):
    calls = _dispatch(monkeypatch, render_key="")
    assert calls == ["sess-1"], (
        "with no key, identity falls back to a content hash and the durable scan is the only "
        f"thing that can recognise a prior render; it was skipped: {calls!r}")
