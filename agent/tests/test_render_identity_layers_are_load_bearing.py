"""The render "identity layers" are NOT dead code — pin why, before someone deletes them.

`docs/handoff-and-render-redesign.md` §7 carries a delete-ledger naming `_manifest_id_for`
(content hashing), `candidate_keys`, `_media_identity` and the url-dedup as layers to remove
once identity is "the job id". That ledger is premised on the job/node id being available for
EVERY render. It is not: a single-dispatch tool result carries no node id, so `render_key` is
empty and the content hash IS the identity. The local E2E harness demonstrates this every run —
its plan-flow case yields `sg_shots.json` / `sg_diarize.json` (node-keyed) while its
single-dispatch and inherited-tool cases yield hash-keyed manifests.

Deleting these would therefore regress four separately-fixed bugs at once:

  1. identity for every single-dispatch render         -> random uuid per dispatch -> two tabs
  2. cross-turn / cold-start dedup                     -> the "3x duplicate tabs" report
  3. `s3://` vs presigned-https of the SAME object     -> re-render under a different url form
  4. the M2C proxy uri vs its imported CloudFront copy -> the duplicate "Video" tab

This file pins each one as an executable invariant, so a future reader who finds the ledger
gets a failing test instead of a plausible-looking deletion. If the design genuinely moves to
universal job-id identity, these tests should be changed DELIBERATELY, with the replacement in
hand — not removed as collateral.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from render.artifacts import _manifest_id_for, _render_manifest_id  # noqa: E402
from render.review import _media_identity, _norm_url  # noqa: E402

BUCKET = "m2c-outputs"
EXEC = "6f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f"


# ---------------------------------------------------------------- 1. single dispatch
def test_a_render_with_no_node_id_still_gets_a_stable_identity():
    """The content hash is not a legacy fallback — it is the ONLY identity single dispatch has."""
    urls = [f"s3://{BUCKET}/micro-workflow/shot-segmentation/{EXEC}/shots.json"]
    first = _render_manifest_id("", urls)
    assert first, "a render with no render_key must still get an id"
    assert first == _render_manifest_id("", urls), "identity must be stable across dispatches"
    # ...and it must be the hash, not a uuid: two dispatches of the same set converge on ONE
    # S3 key, which is what makes the second write idempotently overwrite the first (one tab).
    assert first == _manifest_id_for(urls)


def test_the_node_id_wins_when_there_is_one():
    # Both schemes coexist by design; the job/node id is preferred where it exists.
    urls = [f"s3://{BUCKET}/x/{EXEC}/shots.json"]
    assert _render_manifest_id("sg_shots", urls) == "sg_shots"


def test_different_artifact_sets_do_not_collide():
    a = _manifest_id_for([f"s3://{BUCKET}/a/{EXEC}/one.json"])
    b = _manifest_id_for([f"s3://{BUCKET}/a/{EXEC}/two.json"])
    assert a != b


def test_identity_is_order_independent():
    u = [f"s3://{BUCKET}/a/{EXEC}/one.json", f"s3://{BUCKET}/a/{EXEC}/two.json"]
    assert _manifest_id_for(u) == _manifest_id_for(list(reversed(u)))


# ---------------------------------------------------------------- 3. presigned vs s3://
def test_norm_url_collapses_s3_and_presigned_forms_of_one_object():
    key = f"micro-workflow/media-processing/{EXEC}/proxy/transcoded.mp4"
    s3 = f"s3://{BUCKET}/{key}"
    presigned = (f"https://{BUCKET}.s3.us-west-2.amazonaws.com/{key}"
                 "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=deadbeef")
    assert _norm_url(s3) == _norm_url(presigned) == key


def test_norm_url_keeps_the_whole_key_so_two_jobs_do_not_collide():
    """Truncating to the last segments manufactured a cross-job collision the data lacks.

    Every media-processing job emits `proxy/transcoded.mp4`; the exec id in the prefix is the
    only thing distinguishing them.
    """
    other = "9999aaaa-bbbb-cccc-dddd-eeeeffff0000"
    a = _norm_url(f"s3://{BUCKET}/micro-workflow/media-processing/{EXEC}/proxy/transcoded.mp4")
    b = _norm_url(f"s3://{BUCKET}/micro-workflow/media-processing/{other}/proxy/transcoded.mp4")
    assert a != b


# ---------------------------------------------------------------- 4. proxy vs imported copy
def test_media_identity_collapses_the_proxy_uri_and_its_imported_copy():
    """These are the same file under two key shapes, so `_norm_url` alone cannot dedup them.

    This is the layer that killed the duplicate "Video" tab.
    """
    proxy = f"s3://{BUCKET}/micro-workflow/media-processing/{EXEC}/proxy/transcoded.mp4"
    imported = (f"https://cdn.cloudfront.net/artifacts/sess-1/output/plan-1/{EXEC}/"
                "proxy/transcoded.mp4")
    assert _norm_url(proxy) != _norm_url(imported), "premise: the object keys really do differ"
    assert _media_identity(proxy) == _media_identity(imported) != ""


def test_media_identity_refuses_to_collapse_unrelated_files():
    """It returns "" with no exec uuid, precisely so it cannot manufacture a collision."""
    assert _media_identity("s3://b/some/plain/transcoded.mp4") == ""
    assert _media_identity("https://cdn/artifacts/x/output/report.pdf") == ""
    # Same basename, different exec -> different identity, never collapsed.
    other = "9999aaaa-bbbb-cccc-dddd-eeeeffff0000"
    assert (_media_identity(f"s3://b/w/{EXEC}/proxy/transcoded.mp4")
            != _media_identity(f"s3://b/w/{other}/proxy/transcoded.mp4"))


# ---------------------------------------------------------------- 2. candidate_keys
def test_candidate_keys_are_recorded_on_the_manifest_payload():
    """`candidate_keys` carries the PRE-mirror candidates, and that is the whole point.

    A manifest's `tracks[].src` is the RESOLVED/mirrored copy. Without the pre-mirror
    candidates recorded alongside it, a later re-dispatch of the original `s3://` looks novel
    to the durable dedup and renders a second tab. `_already_rendered_urls` reads this field
    back out of each persisted manifest for exactly that reason.
    """
    import render.artifacts as render_artifacts

    captured: dict = {}

    def _fake_invoke(client, **kw):
        import json as _json
        captured.update(_json.loads(kw["payload"].decode()))
        raise RuntimeError("stop after payload capture")

    src = f"s3://{BUCKET}/micro-workflow/shot-segmentation/{EXEC}/shots.json"
    import jobs.context as jc
    orig_invoke = jc.invoke_agent_runtime_traced
    orig_arn = render_artifacts.RENDER_AGENT_ARN
    jc.invoke_agent_runtime_traced = _fake_invoke
    # dispatch_render bails before building a payload when the render runtime is unconfigured.
    render_artifacts.RENDER_AGENT_ARN = (
        "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/render-TEST")
    try:
        try:
            render_artifacts.dispatch_render([src], label="Shot Segmentation")
        except RuntimeError:
            pass
    finally:
        jc.invoke_agent_runtime_traced = orig_invoke
        render_artifacts.RENDER_AGENT_ARN = orig_arn
        render_artifacts._rendered_url_sets.clear()

    assert "candidate_keys" in captured, (
        "the dispatch payload must carry candidate_keys; _already_rendered_urls depends on the "
        "render agent stamping them onto the manifest")
    # Normalised object keys, not raw urls -- that is what the dedup set compares against.
    assert _norm_url(src) in captured["candidate_keys"]


# ------------------------------------------------------------------ the dedup layers, after Rule 1
# The two paths that can render the SAME job now AGREE on a key:
#
#   HOOK render      -> dispatch_render -> payload["manifest_id"] =
#                       _render_manifest_id(canonical_render_key(job_id), urls)   -> node-keyed
#   EXPLICIT render  -> dispatch_tool(target="render") -> a prompt with NO manifest_id ->
#                       build_render_manifest derives the key from the artifact PATH
#                       (`artifacts/<sid>/output/<plan>/<node>/<file>`)             -> node-keyed
#
# That convergence is what makes the url-dedup layers deletable, so it is worth an assertion
# through the REAL code rather than a comparison of primitives. An earlier version of this test
# compared `_render_manifest_id("sg_shots", urls)` against `_manifest_id_for(urls)` directly and
# so could never observe the convergence it existed to watch for -- it stayed green straight
# through the change that fixed the split.
def test_the_two_render_paths_now_agree_on_one_key(monkeypatch):
    """Both paths must derive the same key for one job, through their real entry points."""
    import hooks.provider as hp
    import render_agent
    from render.artifacts import _render_manifest_id

    sid, plan, node = "sess-1", "plan-1", "sg_shots"
    urls = [f"https://cdn.cloudfront.net/artifacts/{sid}/output/{plan}/{node}/shots.json"]
    arn = f"arn:aws:states:us-west-2:1234:execution:mw-shots:{plan}-{node}"

    # HOOK path: job id -> registry row's node_id -> manifest id.
    monkeypatch.setattr(hp, "_job_row_lookup",
                        lambda jid: {"jobId": arn, "planNodeId": node, "runId": plan},
                        raising=False)
    hook_key = _render_manifest_id(hp.canonical_render_key(arn), urls)

    # EXPLICIT path: no manifest_id, so build_render_manifest derives from the path. Capture the
    # id it actually writes under rather than re-deriving it here.
    written = {}
    monkeypatch.setattr(render_agent, "_write_manifest_to_s3",
                        lambda body, s, m: written.setdefault("mid", m) and None,
                        raising=False)
    render_agent.build_render_manifest(json.dumps(urls), "", session_id=sid)

    assert written.get("mid") == hook_key == f"{plan}--{node}", (
        f"paths disagree: explicit={written.get('mid')!r} hook={hook_key!r}")


def test_a_render_with_no_job_and_no_node_still_falls_back_to_the_hash(monkeypatch):
    """The hash is now the LAST resort, and it must still be reachable.

    A synchronous `artifact` call has no job id and its url carries no node segment, so nothing
    durable identifies it but its contents. This is the case that keeps `_manifest_id_for` alive.
    """
    import render_agent
    from render.artifacts import _manifest_id_for

    urls = ["https://cdn.cloudfront.net/artifacts/1787074782850-chart.png"]  # legacy flat key
    written = {}
    monkeypatch.setattr(render_agent, "_write_manifest_to_s3",
                        lambda body, s, m: written.setdefault("mid", m) and None,
                        raising=False)
    render_agent.build_render_manifest(json.dumps(urls), "", session_id="sess-1")
    assert written.get("mid") == _manifest_id_for(urls)
