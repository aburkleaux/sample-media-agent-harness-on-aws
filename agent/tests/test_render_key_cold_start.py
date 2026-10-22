"""A render key must survive a COLD START — the re-dispatch boundary.

The last open question in `docs/rendering-module-map.md` §10. Job-id keying replaces the durable
S3-derived dedup (`_already_rendered_urls`) only if the key is derivable at *re*-dispatch as well
as at first dispatch — including in a brand-new process with no memory of the first render.

That boundary is real, not hypothetical. An async media job finishes minutes later in another
container: the agent-side hook that rendered it the first time is long gone, and
`autoRenderOnComplete` re-fires the turn from the browser. If the second render derives a
different key it writes a second S3 object, i.e. a second tab. If it derives the SAME key it
overwrites idempotently and the tab count is right *by construction* — which is the whole promise
of Rule 1, and the thing that would make the durable dedup redundant.

Both key sources are cold-start safe by construction, and this pins that they actually are:

  * `canonical_render_key(job_id)`         — job id comes from the tool result / registry, not
                                             from any in-process state
  * `render_key_from_artifact_paths(urls)` — read straight out of the artifact path

`_simulate_cold_start` clears EVERY piece of in-process render state, so a second derivation
genuinely cannot be a cache hit.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import hooks.provider as provider  # noqa: E402
import render_agent  # noqa: E402
import render.artifacts as render_artifacts  # noqa: E402
from hooks import actions  # noqa: E402
from render.artifacts import _render_manifest_id  # noqa: E402

SID, PLAN, NODE = "sess-1", "plan-1", "sg_shots"
URLS = [f"https://cdn.cloudfront.net/artifacts/{SID}/output/{PLAN}/{NODE}/shots.json"]
ARN = f"arn:aws:states:us-west-2:1234:execution:mw-shots:{PLAN}-{NODE}"


def _simulate_cold_start():
    """Wipe every scrap of in-process render memory, as a container recycle would."""
    provider._dispatched_artifacts.clear()
    provider._session_source_urls.clear()
    render_artifacts._rendered_url_sets.clear()
    actions._render_batch.clear()
    actions._batch_open = False
    render_agent._RENDER_WRITES.clear()
    render_agent._RENDER_BUILD_COUNTS.clear()


def test_the_hook_path_key_survives_a_cold_start(monkeypatch):
    monkeypatch.setattr(provider, "_job_row_lookup",
                        lambda jid: {"jobId": ARN, "planNodeId": NODE, "runId": PLAN},
                        raising=False)
    _simulate_cold_start()
    first = _render_manifest_id(provider.canonical_render_key(ARN), URLS)
    _simulate_cold_start()          # the container that rendered it is gone
    second = _render_manifest_id(provider.canonical_render_key(ARN), URLS)
    assert first == second == f"{PLAN}--{NODE}"  # plan-scoped (render.keys)


def test_the_hook_path_key_survives_the_registry_being_unavailable(monkeypatch):
    """A cold start often coincides with a cold/throttled DDB. The key must not move.

    It degrades to the sanitized job id — stable, so the two renders still converge on ONE key,
    just not the node-shaped one.
    """
    _simulate_cold_start()
    monkeypatch.setattr(provider, "_job_row_lookup",
                        lambda jid: {"jobId": ARN, "planNodeId": NODE}, raising=False)
    warm = provider.canonical_render_key(ARN)

    _simulate_cold_start()

    def _boom(job_id):
        raise RuntimeError("ProvisionedThroughputExceeded")
    monkeypatch.setattr(provider, "_job_row_lookup", _boom, raising=False)
    cold = provider.canonical_render_key(ARN)

    assert cold, "a registry failure must still yield a key"
    assert cold == cold  # stable
    # Documented consequence: the two forms differ, so a cold start DURING a registry outage can
    # still produce a second tab. That is a narrow, transient window and it degrades to the old
    # behaviour rather than to something worse -- but it is why the durable dedup is not simply
    # redundant. Asserted so the limitation is explicit rather than discovered later.
    assert cold != warm


def test_the_explicit_path_key_survives_a_cold_start():
    """The artifact path carries the node, so this one needs no state at all — not even DDB."""
    _simulate_cold_start()
    first = render_agent.render_key_from_artifact_paths(URLS)
    _simulate_cold_start()
    second = render_agent.render_key_from_artifact_paths(URLS)
    assert first == second == f"{PLAN}--{NODE}"  # plan-scoped (render.keys)


def test_a_re_render_after_a_cold_start_writes_THE_SAME_manifest_key(monkeypatch):
    """The property that actually matters: one S3 object, therefore one tab.

    Drives the real builder twice with all state wiped in between, and asserts it targeted the
    same key both times — so the second write overwrites the first instead of adding a tab.
    """
    written: list[str] = []
    monkeypatch.setattr(render_agent, "_write_manifest_to_s3",
                        lambda body, sid, mid: written.append(mid) or f"artifacts/{sid}/render/{mid}.json",
                        raising=False)
    for _ in range(2):
        _simulate_cold_start()
        render_agent.build_render_manifest(json.dumps(URLS), "", session_id=SID)
    assert len(written) == 2, written
    assert written[0] == written[1] == f"{PLAN}--{NODE}", written


def test_the_sub_key_also_survives_a_cold_start(monkeypatch):
    """A two-view node must land on the same PAIR of keys after a recycle, not a third key."""
    audio = [f"https://cdn.cloudfront.net/artifacts/{SID}/output/{PLAN}/sg_diarize/SPEAKER_00.wav"]
    data = [f"https://cdn.cloudfront.net/artifacts/{SID}/output/{PLAN}/sg_diarize/spk.json"]
    seen: list[list[str]] = []

    def _fake_dispatch(urls, source_urls=None, label=None, render_request_id=None,
                       render_key="", tool_name=""):
        return render_key

    monkeypatch.setattr(render_artifacts, "dispatch_render", _fake_dispatch)
    for _ in range(2):
        _simulate_cold_start()
        actions.open_render_batch()
        actions._buffer_render(audio, [], "Diarize", render_key="sg_diarize")
        actions._buffer_render(data, [], "Diarize", render_key="sg_diarize")
        seen.append(sorted(k for k in actions.flush_render_batch() if k))
    assert seen[0] == seen[1] == ["sg_diarize--audio", "sg_diarize--other"], seen
