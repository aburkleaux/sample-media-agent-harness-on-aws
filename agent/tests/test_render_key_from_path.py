"""The EXPLICIT render path derives its key from the artifact PATH, so both paths agree.

This is the last blocker on the redesign's §7 deletions. Two paths can render the same job and
they used to key differently:

    HOOK (automatic)   dispatch_render -> payload["manifest_id"] =
                       _render_manifest_id(canonical_render_key(job_id), urls)   -> node-keyed
    EXPLICIT ("render  dispatch_tool(target="render") -> a prompt with NO manifest_id
    this for me")      -> build_render_manifest falls back to _manifest_id_for(cands) -> HASH

Different keys means two S3 objects, i.e. two tabs, and the only things preventing that are the
`_rendered_url_sets` / `_already_rendered_urls` dedup layers the ledger wants deleted. So the
dedup cannot go until the keys converge.

There is no job id in scope on the explicit path — no tool result, no registry handle, just urls
in a prompt. But the urls already carry the answer: outputs live at

    artifacts/<session>/output/<plan_id>/<node_id>/<file>

so the node id IS the 5th path segment. Deriving it from the path needs no lookup, no reverse
index and no LLM, and it lands on the SAME `node_id` that `canonical_render_key` resolves for the
hook path. Same principle as source-role derivation (`provider._has_input_path`): make identity a
property of the durable LOCATION rather than of transient context.

Where it must decline (returning "" so the content hash still applies):
  * urls from two DIFFERENT nodes -- genuinely ambiguous, do not guess
  * `/input/` urls -- an input is not a deliverable
  * legacy flat keys and anything not matching the convention
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from render_agent import render_key_from_artifact_paths  # noqa: E402

CDN = "https://dexamplecdn002.cloudfront.net"
SID = "a2ebd1e9-d141-4c2b-8f68-8a80e8665cfe"
PLAN = "85f29ba8-09ae-43e4-a992-d882a72f7732"
OUT = f"{CDN}/artifacts/{SID}/output/{PLAN}"


def test_a_single_node_output_yields_the_node_id():
    assert render_key_from_artifact_paths([f"{OUT}/sg_shots/shots.json"]) == f"{PLAN}--sg_shots"


def test_several_files_from_one_node_yield_one_key():
    urls = [f"{OUT}/sg_shots/shots.json",
            f"{OUT}/sg_shots/shot_000000.jpg",
            f"{OUT}/sg_shots/shot_000001.jpg"]
    assert render_key_from_artifact_paths(urls) == f"{PLAN}--sg_shots"


def test_it_converges_with_the_hook_path(monkeypatch):
    """The whole point: the explicit key must EQUAL what the hook would have produced.

    The hook resolves an execution ARN through the registry row's `node_id`; the explicit path
    reads the same `node_id` out of the artifact path. They must meet.
    """
    import hooks.provider as provider
    arn = f"arn:aws:states:us-west-2:1234:execution:mw-shot-segmentation:{PLAN}-sg_shots"
    monkeypatch.setattr(provider, "_job_row_lookup",
                        lambda jid: {"jobId": arn, "planNodeId": "sg_shots", "runId": PLAN},
                        raising=False)
    hook_key = provider.canonical_render_key(arn)
    explicit_key = render_key_from_artifact_paths([f"{OUT}/sg_shots/shots.json"])
    assert hook_key == explicit_key == f"{PLAN}--sg_shots"


def test_s3_uris_work_too():
    assert render_key_from_artifact_paths(
        [f"s3://bucket/artifacts/{SID}/output/{PLAN}/sg_diarize/speaker_manifest.json"]
    ) == f"{PLAN}--sg_diarize"


def test_a_presign_query_does_not_break_it():
    u = f"{OUT}/sg_shots/shots.json?X-Amz-Signature=deadbeef&X-Amz-Expires=3600"
    assert render_key_from_artifact_paths([u]) == f"{PLAN}--sg_shots"


# ---------------------------------------------------------------- must decline
def test_two_different_nodes_are_ambiguous_and_decline():
    """Do NOT guess. Picking either node would key one node's manifest onto another's tab."""
    urls = [f"{OUT}/sg_shots/shots.json", f"{OUT}/sg_diarize/speaker_manifest.json"]
    assert render_key_from_artifact_paths(urls) == ""


def test_an_input_is_not_a_deliverable():
    assert render_key_from_artifact_paths(
        [f"{CDN}/artifacts/{SID}/input/1787-clip.mp4"]) == ""


def test_a_legacy_flat_key_declines():
    # 269 uploads predate the session-scoped convention; they carry no node.
    assert render_key_from_artifact_paths(
        [f"{CDN}/artifacts/1787074782850-Netflix_Open_Content_Meridian.mp4"]) == ""


def test_a_render_manifest_path_declines():
    # Never key a render off another render's manifest.
    assert render_key_from_artifact_paths(
        [f"{CDN}/artifacts/{SID}/render/sg_shots.json"]) == ""


def test_a_foreign_url_declines():
    assert render_key_from_artifact_paths(["https://cdn.vendor.example/report.pdf"]) == ""


def test_empty_and_junk_decline():
    assert render_key_from_artifact_paths([]) == ""
    assert render_key_from_artifact_paths(["", None, 7]) == ""  # type: ignore[list-item]


def test_a_file_directly_under_output_declines():
    # `artifacts/<sid>/output/<file>` has no node segment; the 5th segment is the FILE.
    assert render_key_from_artifact_paths([f"{CDN}/artifacts/{SID}/output/summary.json"]) == ""


def test_the_key_is_s3_safe():
    weird = f"{CDN}/artifacts/{SID}/output/{PLAN}/node%20one/shots.json"
    got = render_key_from_artifact_paths([weird])
    assert got and "%" not in got and "/" not in got, got
