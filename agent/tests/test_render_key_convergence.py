"""One logical job must resolve to ONE render key, whichever path reaches it.

This is the prerequisite for `handoff-and-render-redesign.md` §4 Rule 1 ("identity is the job
id"), and it has to be established BEFORE keying single dispatch by job id — otherwise Rule 1
manufactures the very duplicate it exists to prevent.

The hazard is concrete. A media job can be reached by two different paths in the same session:

  * IN-TURN, via the plan-result aggregate -> `render_groups` -> `render_key = node_id`
    e.g. `sg_shots`  ->  artifacts/<sid>/render/sg_shots.json
  * OUT-OF-TURN, via `autoRenderOnComplete` re-firing the turn, which lands on a status tool for
    one job -> single dispatch -> the result carries only `job_id` / `executionArn`

Session `a2ebd1e9` shows exactly this split in production: manifests keyed
`arn_aws_states_…face-recognition_…` alongside `sg_shots.json`, for one plan's jobs.

Today those two paths converge only ACCIDENTALLY, and not by key: single dispatch falls back to
the url content hash, and the durable `_already_rendered_urls` dedup suppresses the second
render because the url sets match. That is worth naming plainly, because it means the §7
deletions are MUTUALLY dependent — the dedup layer cannot be deleted until keys converge, and
keys do not converge until a canonical job id exists. Deleting the dedup first re-opens the
duplicate tab.

So: resolve a canonical key from a job id, using the registry row's own `node_id`, so an
ARN-reached render lands on the SAME key as the node-reached one.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import hooks.provider as provider  # noqa: E402

PLAN = "85f29ba8-09ae-43e4-a992-d882a72f7732"
NODE = "sg_shots"
ARN = (f"arn:aws:states:us-west-2:100000000002:execution:"
       f"micro-workflow-shot-segmentation:{PLAN}-{NODE}")


def _registry(rows):
    """Install a fake job-registry lookup keyed by jobId, like the real get_item."""
    return lambda job_id: rows.get(job_id)


def test_an_arn_resolves_to_the_plan_node_key(monkeypatch):
    """The whole point: both paths must produce one key.

    The execution row carries `node_id` (the linkage written at SFN registration), so an
    ARN-reached render can be mapped onto the node-keyed manifest instead of minting its own.
    """
    rows = {ARN: {"jobId": ARN, "planNodeId": NODE, "runId": PLAN}}
    monkeypatch.setattr(provider, "_job_row_lookup", _registry(rows), raising=False)
    assert provider.canonical_render_key(ARN) == f"{PLAN}--{NODE}"  # plan-scoped (render.keys)


def test_a_composite_plan_node_key_resolves_to_the_same_thing(monkeypatch):
    # The Plan Chainer files a row under `<plan>#<node>`; it must agree with the ARN row.
    key = f"{PLAN}#{NODE}"
    rows = {key: {"jobId": key, "planNodeId": NODE, "runId": PLAN}}
    monkeypatch.setattr(provider, "_job_row_lookup", _registry(rows), raising=False)
    assert provider.canonical_render_key(key) == f"{PLAN}--{NODE}"


def test_a_standalone_job_with_no_node_uses_its_own_id(monkeypatch):
    """A direct dispatch that was never part of a plan still gets a STABLE key.

    This is the half of Rule 1 that was missing: previously `render_key` was empty for single
    dispatch, so identity fell to the url content hash.
    """
    rows = {ARN: {"jobId": ARN}}  # no node_id -- not a plan node
    monkeypatch.setattr(provider, "_job_row_lookup", _registry(rows), raising=False)
    got = provider.canonical_render_key(ARN)
    assert got, "a standalone job must still yield a key"
    assert "/" not in got and ":" not in got, f"must be S3-key safe, got {got!r}"
    # Stable across calls, so a re-render converges rather than minting a new tab.
    assert got == provider.canonical_render_key(ARN)


def test_no_job_id_yields_no_key(monkeypatch):
    """A truly synchronous tool has no job. Return "" so the caller keeps the content hash.

    Inventing a key here would be worse than the hash: it would not be derived from anything
    durable, so two renders of the same artifacts would not converge.
    """
    monkeypatch.setattr(provider, "_job_row_lookup", _registry({}), raising=False)
    assert provider.canonical_render_key(None) == ""
    assert provider.canonical_render_key("") == ""


def test_an_unknown_job_still_yields_its_own_id(monkeypatch):
    # The registry may not have the row yet (eventual consistency). The job id itself is still
    # a stable, durable identifier -- far better than falling back to a url hash.
    monkeypatch.setattr(provider, "_job_row_lookup", _registry({}), raising=False)
    assert provider.canonical_render_key(ARN)


def test_a_registry_failure_degrades_to_the_job_id(monkeypatch):
    # Identity must never depend on a DDB call succeeding.
    def _boom(job_id):
        raise RuntimeError("throughput exceeded")
    monkeypatch.setattr(provider, "_job_row_lookup", _boom, raising=False)
    assert provider.canonical_render_key(ARN)


def test_two_different_jobs_do_not_collide(monkeypatch):
    other = ARN.replace(NODE, "sg_diarize")
    rows = {
        ARN: {"jobId": ARN, "planNodeId": NODE, "runId": PLAN},
        other: {"jobId": other, "planNodeId": "sg_diarize", "runId": PLAN},
    }
    monkeypatch.setattr(provider, "_job_row_lookup", _registry(rows), raising=False)
    assert provider.canonical_render_key(ARN) != provider.canonical_render_key(other)


# ------------------------------------------------------------------ wiring, not just the rule
# The rule above is pure and easy to get green. The WIRING is where this went wrong: my first
# attempt recomputed the job id inside `render_action` from `ctx["result"]` — which is the
# DERIVED dict (urls/mimes/new_artifacts), not the raw tool result — so it silently produced no
# key and identity quietly stayed on the url hash. Exactly the failure mode this whole change
# exists to remove. These drive the real hook so that cannot pass unnoticed.
import json  # noqa: E402

from hooks import actions  # noqa: E402
from hooks.provider import ctx_after_tool_result  # noqa: E402


class _Ev:
    def __init__(self, name, payload, tool_use_extra=None):
        self.tool_use = {"name": name, "toolUseId": "u1", **(tool_use_extra or {})}
        self.result = {"content": [{"text": json.dumps(payload)}]}


def test_ctx_carries_the_job_id_from_the_raw_result():
    ev = _Ev("shot-segmentation___get_job_status",
             {"status": "SUCCEEDED", "executionArn": ARN,
              "output": {"s3uri": "s3://b/out/shots.json"}})
    ctx = ctx_after_tool_result(ev)
    assert ctx["result"]["job_id"] == ARN, (
        "the job id must be resolved where the RAW result is in scope; downstream only sees "
        "the derived dict")


def test_single_dispatch_buffers_with_a_job_derived_render_key(monkeypatch):
    """End to end: the slot must carry a render_key, so the manifest is job-keyed."""
    rows = {ARN: {"jobId": ARN, "planNodeId": NODE, "runId": PLAN}}
    monkeypatch.setattr(provider, "_job_row_lookup", _registry(rows), raising=False)
    captured = {}

    def _spy(urls, source_urls, label, render_key="", tool_name=""):
        captured.update(render_key=render_key, tool_name=tool_name)

    monkeypatch.setattr(actions, "_buffer_render", _spy)
    monkeypatch.setattr(actions, "_batch_is_open", lambda: True)
    ev = _Ev("shot-segmentation___get_job_status",
             {"status": "SUCCEEDED", "executionArn": ARN,
              "output": {"s3uri": "https://cdn.cloudfront.net/artifacts/s1/output/shots.json"}})
    ctx = ctx_after_tool_result(ev)
    actions.render_action({"urls": "result.urls"}, ctx)
    assert captured.get("render_key") == f"{PLAN}--{NODE}", (
        f"single dispatch must be keyed on the job, got {captured!r}")


def test_single_dispatch_with_no_job_keeps_the_content_hash(monkeypatch):
    # A synchronous tool has no job; render_key must stay empty so identity falls to the hash.
    monkeypatch.setattr(provider, "_job_row_lookup", _registry({}), raising=False)
    captured = {}
    monkeypatch.setattr(actions, "_buffer_render",
                        lambda u, s, l, render_key="", tool_name="": captured.update(
                            render_key=render_key))
    monkeypatch.setattr(actions, "_batch_is_open", lambda: True)
    ev = _Ev("artifact", {"url": "https://cdn.cloudfront.net/artifacts/s1/output/chart.png"})
    actions.render_action({"urls": "result.urls"}, ctx_after_tool_result(ev))
    assert captured.get("render_key") == "", f"expected no key, got {captured!r}"
