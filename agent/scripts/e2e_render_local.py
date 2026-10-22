#!/usr/bin/env python
"""Local end-to-end render harness — real code path, real artifacts, no deploy.

Drives the WHOLE render chain with nothing stubbed except the two things that cannot run
locally:

    ctx_after_tool_result          (real hook)
      -> render_action             (real grouping + intermediate filtering)
        -> flush_render_batch      (real batching)
          -> dispatch_render       (real; invokes the render runtime)
            -> Render Agent        (LOCAL, via LOCAL_AGENT_ENDPOINTS)
              -> manifest in S3    (real bucket)

Not in the loop (documented boundaries, not silent gaps):
  * the Plan Chainer — a deployed Lambda on a DynamoDB stream. Its OUTPUT (the plan-result
    aggregate) is what this harness supplies, shaped exactly as the fixed chainer emits it
    (`tool` + `depends_on` per node), so everything downstream of it is genuinely exercised.
  * the media tools themselves — we replay the artifacts they already produced, which live
    in S3 from the captured sessions.

Usage:  scripts/e2e_render_up.sh     <- brings the local render agent up, sets env, runs this
        (or run this directly with SKILLS_S3_BUCKET + LOCAL_AGENT_ENDPOINTS already set)
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import boto3  # noqa: E402

from hooks import actions  # noqa: E402
from hooks.provider import ctx_after_tool_result, intermediate_node_ids  # noqa: E402

CF = f"https://{os.environ.get('ARTIFACTS_DOMAIN', 'dexamplecdn002.cloudfront.net')}"
BUCKET = os.environ["SKILLS_S3_BUCKET"]
# Real outputs from session a2ebd1e9 (still in S3) — the COPY SOURCE for fixtures.
FIXTURE_SESS = "a2ebd1e9-d141-4c2b-8f68-8a80e8665cfe"
PLAN = "85f29ba8-09ae-43e4-a992-d882a72f7732"
FIXTURE_OUT = f"artifacts/{FIXTURE_SESS}/output/{PLAN}"
# Per-case, session-scoped output prefix. `_stage_session_outputs` copies the fixture outputs
# into the CURRENT case session and repoints OUT here, so every fixture URL a case builds lives
# under `artifacts/<this-session>/` and survives the render auto-harvester's foreign-session guard
# (`_drop_foreign_session_urls`). Without this, cases that reference the capture session's outputs
# are dropped as "another session's assets" and dispatch 0 manifests. Starts at the fixture prefix
# so pre-run references resolve before the first case stages its own copy.
OUT = FIXTURE_OUT
# The flat, session-less key uploads used to land at. Kept as the COPY SOURCE only.
LEGACY_UPLOAD_KEY = "artifacts/1787074782850-Netflix_Open_Content_Meridian.mp4"
# Filled in by _stage_session_input(): the source video at its new session-scoped INPUT
# location, addressed by CloudFront https — the shape production now produces.
SOURCE_VIDEO = ""
SOURCE_VIDEO_KEY = ""
# One session PER CASE (see _new_case_session): `<_RUN_ID>-c<n>`. `_SESSION_ID` is the
# current one, and every key the harness writes must live under `artifacts/<_SESSION_ID>/` —
# asserted per case, because a wrong session silently clobbers whatever real session the
# fixture urls happen to name.
_RUN_ID = ""
_SESSION_ID = ""
_CASE = [0]

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


class _Ev:
    def __init__(self, name, payload):
        self.tool_use = {"name": name, "toolUseId": "u1"}
        self.result = {"content": [{"text": json.dumps(payload)}]}


def _manifest(key: str) -> dict | None:
    try:
        body = boto3.client("s3").get_object(Bucket=BUCKET, Key=key)["Body"].read()
        return json.loads(body)
    except Exception:
        return None


def _new_case_session():
    """Open a genuinely INDEPENDENT session for the next case.

    The cases replay the SAME real artifacts, so they must not share a session or the
    dedup guards correctly suppress every case after the first. Those guards are three
    layers deep and clearing only the in-process ones is not enough:

      * `provider._dispatched_artifacts` / `_session_source_urls` — in-process, per session
      * `render_artifacts._rendered_url_sets`                     — in-process
      * `_already_rendered_urls(session)`                         — DURABLE, derived by
        LISTING `artifacts/<session>/render/` in S3, so it cannot be cleared in memory at all

    An earlier version cleared only the first two and reused one session id. It appeared to
    work purely because the session was never bound: `dispatch_render` silently fell back to
    an empty durable set. Binding the session exposed it — case 2 dispatched 0 manifests,
    correctly deduped against case 1. So each case now gets its OWN session id, which is what
    "independent session" always meant. The guards themselves stay armed and are asserted by
    test_render_batching / test_durable_render_dedup.
    """
    # (verified: with the session unbound, the render agent falls back to
    #  `payload.get("session_id", str(uuid.uuid4()))` -- render_agent.py:2823 -- so each run
    #  LEAKED its manifests into a fresh random session, e.g. b2856c4f-.... It did NOT overwrite
    #  the captured fixture session; a2ebd1e9's manifests still hold the original wrong
    #  render-image/image-annotated evidence. The damage was orphaned writes and assertions
    #  that could not see what they were validating, not data loss.)
    global _SESSION_ID
    _CASE[0] += 1
    _SESSION_ID = f"{_RUN_ID}-c{_CASE[0]}"
    from jobs import context as jobs_context
    from hooks import provider
    import render.artifacts as render_artifacts
    # `dispatch_render` reads the session from this contextvar, NOT from any env var (setting
    # `SESSION_ID` in the environment did nothing). With it unbound, `current_session_id()`
    # raises, the surrounding except swallows it, `payload["session_id"]` is absent, and the
    # render agent invents one (`payload.get("session_id", str(uuid.uuid4()))`). Every run then
    # scattered its manifests into a random orphan session that the harness's own assertions,
    # its durable-dedup guard and its cleanup line all pointed away from. Bind it.
    jobs_context.set_request_context(actor_id="e2e-harness", session_id=_SESSION_ID)
    provider._dispatched_artifacts.clear()
    provider._session_source_urls.clear()
    render_artifacts._rendered_url_sets.clear()
    _stage_session_input(_SESSION_ID)
    _stage_session_outputs(_SESSION_ID)


def _stage_session_outputs(sid: str) -> None:
    """Copy the fixture analysis outputs into ``artifacts/<sid>/output/<PLAN>/`` and repoint OUT.

    The cases replay a captured session's outputs (shots.json, speaker_manifest.json, per-shot
    stills, speaker WAVs). Those URLs live under the CAPTURE session's prefix, but each case runs
    under its own fresh session — so the render auto-harvester's foreign-session guard
    (`_drop_foreign_session_urls`) correctly drops them as "another session's assets" and the case
    dispatches 0 manifests. Staging real copies under THIS session's output prefix (the same move
    `_stage_session_input` makes for the source video) makes the fixture URLs current-session, so
    the guard passes and the render actually runs. Server-side copies only — no bytes flow locally.
    """
    global OUT
    s3 = boto3.client("s3")
    dest = f"artifacts/{sid}/output/{PLAN}"
    listed = s3.list_objects_v2(Bucket=BUCKET, Prefix=FIXTURE_OUT + "/").get("Contents", [])
    for obj in listed:
        rel = obj["Key"][len(FIXTURE_OUT) + 1:]
        s3.copy_object(Bucket=BUCKET, Key=f"{dest}/{rel}",
                       CopySource={"Bucket": BUCKET, "Key": obj["Key"]})
    OUT = dest
    print(f"  staged {len(listed)} fixture outputs -> {dest}/")


def _stage_session_input(sid: str) -> None:
    """Copy the source video to ``artifacts/<sid>/input/`` — what an upload now does.

    This is the whole point of the input-prefix change: the source is addressed by a plain
    CloudFront https url and is recognised as a SOURCE because of its PATH. Previously the
    harness had to hand the source in as an ``s3://`` uri, because `extract_source_uris`
    tested the SCHEME — an https source was classified as an output and rendered its own tab.
    Staging a real object (rather than a made-up url) keeps the render agent able to fetch it.
    """
    global SOURCE_VIDEO, SOURCE_VIDEO_KEY
    SOURCE_VIDEO_KEY = f"artifacts/{sid}/input/{LEGACY_UPLOAD_KEY.split('/')[-1]}"
    SOURCE_VIDEO = f"{CF}/{SOURCE_VIDEO_KEY}"
    boto3.client("s3").copy_object(
        Bucket=BUCKET, Key=SOURCE_VIDEO_KEY,
        CopySource={"Bucket": BUCKET, "Key": LEGACY_UPLOAD_KEY},
    )
    print(f"  staged source input: {SOURCE_VIDEO_KEY}")


def run_case(title, tool_name, build_payload, expect_keys, asserts):
    """Drive one turn through the real chain and assert on the manifests it produced.

    `build_payload(source_video)` is a CALLABLE, not a dict, because the source url is only
    known after this case's own session input is staged.
    """
    print(f"\n=== {title} ===")
    _new_case_session()
    ctx = ctx_after_tool_result(_Ev(tool_name, build_payload(SOURCE_VIDEO)))
    actions.open_render_batch()
    # The REAL params the built-in render-artifacts hook passes (hooks/defaults.py). Using
    # `{}` here would fall back to `result.urls` — every url anywhere in the result, INCLUDING
    # the source media — and render the input as its own tab. `new_artifacts` is the filtered
    # set: renderable mime, not source, not already dispatched.
    actions.render_action(
        {"urls": "result.new_artifacts", "source_urls": "result.source_urls"}, ctx)
    keys = actions.flush_render_batch()  # real dispatch -> local render agent -> S3
    print(f"  dispatched {len(keys)} manifest(s)")
    check(f"{title}: manifest count == {expect_keys}", len(keys) == expect_keys,
          f"got {len(keys)}: {[k.rsplit('/',1)[-1] for k in keys if k]}")
    # HERMETICITY. Without this the harness scattered manifests into a random orphan session and
    # still reported 19/19 — it read them back fine, because it read exactly the keys it had just
    # written; nothing checked those keys were anywhere sane. Assert the blast radius, not just
    # the content.
    sid = _SESSION_ID
    stray = [k for k in keys if k and not k.startswith(f"artifacts/{sid}/")]
    check(f"{title}: wrote only inside artifacts/{sid}/", not stray, f"stray={stray}")
    manifests = {k: _manifest(k) for k in keys if k}
    for fn in asserts:
        fn(title, manifests)
    return manifests


def main() -> int:
    global _RUN_ID
    _RUN_ID = f"e2e-{uuid.uuid4().hex[:8]}"
    sid = _RUN_ID
    print(f"session prefix: artifacts/{sid}-c*/  (one session per case)")

    # ---------------------------------------------------------------- case 1
    # PLAN FLOW, shaped as the FIXED chainer emits: per-node `tool` + `depends_on`.
    # transcode feeds both analyses -> it is plumbing and must NOT get a tab.
    def aggregate(src):
        return {
            "schema": "plan-result/v1", "plan_id": _SESSION_ID, "status": "SUCCEEDED",
            "nodes": [
                {"node_id": "transcode", "kind": "media", "status": "completed",
                 "tool": "media-processing___transcode", "depends_on": [],
                 "artifacts": [{"url": src, "media_kind": "video", "role": "output"}]},
                {"node_id": "sg_shots", "kind": "analysis", "status": "completed",
                 "tool": "shot-segmentation___segment_shots", "depends_on": ["transcode"],
                 "artifacts": [
                     {"url": f"{CF}/{OUT}/sg_shots/shots.json", "media_kind": "data",
                      "role": "analysis", "name": "shots.json"},
                     {"url": f"{CF}/{OUT}/sg_shots/shot_000000.jpg", "media_kind": "image",
                      "role": "analysis"},
                     {"url": src, "media_kind": "video", "role": "source"},
                 ]},
                {"node_id": "sg_diarize", "kind": "analysis", "status": "completed",
                 "tool": "speech-diarization___diarize_speech", "depends_on": ["transcode"],
                 "artifacts": [
                     {"url": f"{CF}/{OUT}/sg_diarize/speaker_manifest.json", "media_kind": "data",
                      "role": "analysis", "name": "speaker_manifest.json"},
                     {"url": f"{CF}/{OUT}/sg_diarize/final_transcript.json", "media_kind": "data",
                      "role": "analysis", "name": "final_transcript.json"},
                     {"url": src, "media_kind": "video", "role": "source"},
                 ]},
            ],
        }

    # Layer 2 verdict on this exact aggregate, before any rendering. A placeholder source url
    # is fine here: intermediate detection reads `depends_on`, never the urls.
    _nodes = aggregate("https://placeholder/src.mp4")["nodes"]
    check("layer2: transcode identified as plumbing",
          intermediate_node_ids(_nodes) == {"transcode"}, str(intermediate_node_ids(_nodes)))

    def a_no_transcode_tab(title, mans):
        labels = {(m or {}).get("renderSkill") for m in mans.values()}
        check(f"{title}: no bare video/transcode tab",
              "render-video" not in labels, f"skills={labels}")

    def a_shots_is_marker_view(title, mans):
        hit = [m for m in mans.values() if (m or {}).get("renderSkill") == "render-shot-segmentation"]
        check(f"{title}: shots -> render-shot-segmentation", bool(hit))
        if hit:
            m = hit[0]
            check(f"{title}: shots template == nle-timeline",
                  m.get("template") == "nle-timeline", f"got {m.get('template')}")
            check(f"{title}: shots has markers", len(m.get("markers") or []) > 0,
                  f"{len(m.get('markers') or [])} markers")
            tracks = m.get("tracks") or []
            check(f"{title}: shots has a video canvas",
                  any(t.get("type") == "video" for t in tracks), f"{len(tracks)} tracks")

    def a_diarize_is_speaker_view(title, mans):
        hit = [m for m in mans.values()
               if (m or {}).get("renderSkill") == "render-speaker-diarization"]
        check(f"{title}: diarize -> render-speaker-diarization", bool(hit))
        if hit:
            m = hit[0]
            check(f"{title}: diarize is NOT a bare audio timeline",
                  m.get("renderSkill") != "render-audio", f"skill={m.get('renderSkill')}")
            has_lanes = bool(m.get("lanes") or m.get("segments") or m.get("captions"))
            check(f"{title}: diarize carries speaker structure", has_lanes,
                  f"lanes={len(m.get('lanes') or [])} segments={len(m.get('segments') or [])} "
                  f"captions={len(m.get('captions') or [])}")

    run_case("PLAN FLOW (3 nodes, 1 plumbing)", "get_plan_results", aggregate,
             expect_keys=2,
             asserts=[a_no_transcode_tab, a_shots_is_marker_view, a_diarize_is_speaker_view])

    # ---------------------------------------------------------------- case 2
    # SINGLE DISPATCH: no envelope, no groups. One tool, one output, tool-keyed view.
    # The source is a plain CloudFront https url under `artifacts/<sid>/input/`. This used to
    # require an `s3://` uri instead: `extract_source_uris` tested the SCHEME as a proxy for
    # the role, so an https source was classified as an OUTPUT and rendered its own tab. Role
    # is now read from the PATH, so the canonical https form works — which is what lets
    # references become CloudFront-only.
    def single(src):
        return {"status": "SUCCEEDED",
                "output": {"s3uri": f"{CF}/{OUT}/sg_shots/shots.json"},
                "input": {"s3uri": src}}

    def a_single_is_marker_view(title, mans):
        m = next(iter(mans.values()), None)
        check(f"{title}: skill == render-shot-segmentation",
              (m or {}).get("renderSkill") == "render-shot-segmentation",
              f"got {(m or {}).get('renderSkill')}")
        check(f"{title}: template == nle-timeline",
              (m or {}).get("template") == "nle-timeline", f"got {(m or {}).get('template')}")

    def a_https_input_is_not_a_deliverable(title, mans):
        # The regression this guards: the input rendering as its own extra tab.
        urls = {u for m in mans.values() for u in ((m or {}).get("urls") or [])}
        check(f"{title}: https /input/ source did NOT become a deliverable",
              SOURCE_VIDEO not in urls, f"deliverable urls={len(urls)}")

    def a_https_input_kept_its_source_role(title, mans):
        # ...and the other direction: it must still reach the render AS the source, or the
        # marker view has no video canvas to draw on. Assert the URL ITSELF appears, not just
        # that some track exists — the manifest carries the source as `src` (top level and
        # per track), NOT `sourceUrl`, so a `m.get("sourceUrl")` check would read None forever
        # and a bare track-count check passes even with an empty src.
        m = next(iter(mans.values()), None) or {}
        tracks = m.get("tracks") or []
        vid = [t for t in tracks if t.get("type") == "video"]
        check(f"{title}: the /input/ url IS the video canvas src",
              any(t.get("src") == SOURCE_VIDEO for t in vid),
              f"video tracks={[t.get('src') for t in vid]}")

    run_case("SINGLE DISPATCH (shot-segmentation, https /input/ source)",
             "shot-segmentation___get_job_status", single, expect_keys=1,
             asserts=[a_single_is_marker_view, a_https_input_is_not_a_deliverable,
                      a_https_input_kept_its_source_role])

    # ---------------------------------------------------------------- case 3
    # INHERITED TOOL: a tool we have never seen, emitting a PNG. Must render as an
    # image view from MEDIUM alone — no tool map entry, no content declaration.
    def unknown_png(_src):
        return {"status": "SUCCEEDED",
                "output": {"s3uri": f"{CF}/{OUT}/sg_shots/shot_000001.jpg"}}

    def a_unknown_gets_image_view(title, mans):
        m = next(iter(mans.values()), None)
        check(f"{title}: unknown tool still renders", m is not None)
        if m:
            check(f"{title}: image medium view",
                  m.get("template") == "image-annotated", f"got {m.get('template')}")
            check(f"{title}: NOT a specialised face view",
                  m.get("renderSkill") != "render-face-people", f"skill={m.get('renderSkill')}")

    run_case("INHERITED TOOL (never-seen, emits an image)",
             "brand-new-vendor___run_model", unknown_png,
             expect_keys=1, asserts=[a_unknown_gets_image_view])

    # ---------------------------------------------------------------- case 4
    # RULE 1: a single dispatch that carries a JOB ID must be keyed on the job, not on a url
    # content hash. Case 2 above deliberately carries no job id and still hashes — that is the
    # documented fallback for a synchronous tool. This case is the difference.
    #
    # With no registry row present (nothing wrote one in this harness), canonical_render_key
    # degrades to the sanitized job id — which is exactly the guarantee that matters: the key is
    # derived from something DURABLE, so an out-of-turn re-render of the same job converges on
    # this manifest instead of minting a second tab.
    JOB_ARN = ("arn:aws:states:us-west-2:100000000002:execution:"
               "micro-workflow-shot-segmentation:e2e-rule1-exec")

    def with_job(src):
        return {"status": "SUCCEEDED",
                "executionArn": JOB_ARN,
                "output": {"s3uri": f"{CF}/{OUT}/sg_shots/shots.json"},
                "input": {"s3uri": src}}

    def a_manifest_is_job_keyed(title, mans):
        keys = [k.rsplit("/", 1)[-1] for k in mans]
        expected = re.sub(r"[^A-Za-z0-9._-]", "_", JOB_ARN)[:120] + ".json"
        check(f"{title}: manifest key is derived from the JOB, not a url hash",
              keys == [expected], f"got {keys} want [{expected}]")

    run_case("RULE 1 (single dispatch WITH a job id)",
             "shot-segmentation___get_job_status", with_job,
             expect_keys=1, asserts=[a_manifest_is_job_keyed])

    # ---------------------------------------------------------------- case 5
    # MULTI-DELIVERABLE (sub-key): ONE UNMAPPED node with TWO incompatible views. The sub-key
    # exists for output with NO deliverable contract — a tool the agent has never seen that emits
    # two different media classes (here an audio track + a video), two templates that cannot
    # share a tab.
    #
    # NOTE: this used to pair an audio track with a JSON data sidecar, but a lone/unprojectable
    # JSON slot is now SUPPRESSED by design (render_agent `_json_only`, commit e2235198 — "we do
    # not display JSON by itself"), so it no longer splits into a surviving second tab. An IMAGE
    # second view does not work either — `_buffer_render`'s "secondary stills" rule folds an image
    # into the node's primary non-image slot. So the sub-key is exercised with audio + video (two
    # non-image classes that keep separate slots); the JSON-suppression itself is covered by
    # test_render_deterministic_json_only_emits_no_tab.
    #
    # A MAPPED tool no longer reaches this: its `deliverable:` contract makes byproducts RIDE the
    # deliverable's one slot (the redundant-tabs fix). That is exactly why the PLAN FLOW diarize
    # and shots cases above each collapse to ONE tab even though their nodes ship extra WAVs/stills.
    # So this case uses an unmapped tool ON PURPOSE, to keep exercising the sub-key on the only
    # output that still splits.
    #
    # `_buffer_render` keys slots on `(render_key or label, media class)`, so the node yields two
    # slots; both carried the same render_key (the node id), so both manifests wrote
    # `render/<node>.json` and the second SILENTLY OVERWROTE the first. The sub-key suffixes each
    # with its media class so both survive. Real artifacts, actual production shape.
    multi = lambda _src: {
        "schema": "plan-result/v1", "plan_id": "multi", "status": "SUCCEEDED",
        "nodes": [
            {"node_id": "multiview_node", "kind": "analysis", "status": "completed",
             "tool": "unmapped-multiview___run", "depends_on": [],
             "artifacts": [
                 {"url": f"{CF}/{OUT}/sg_diarize/SPEAKER_00.wav", "media_kind": "audio",
                  "role": "analysis", "name": "SPEAKER_00.wav"},
                 {"url": f"{CF}/{OUT}/sg_shots/segment_0000.mp4", "media_kind": "video",
                  "role": "analysis", "name": "segment_0000.mp4"},
             ]},
        ],
    }

    def a_two_distinct_node_keys(title, mans):
        names = sorted(k.rsplit("/", 1)[-1] for k in mans)
        check(f"{title}: two DISTINCT manifests, neither overwriting the other",
              len(set(names)) == 2, f"got {names}")
        check(f"{title}: both keys derive from the node",
              all(n.startswith("multiview_node") for n in names), f"got {names}")
        check(f"{title}: keys are sub-keyed by media class",
              all("--" in n for n in names), f"got {names}")

    run_case("MULTI-DELIVERABLE (unmapped node, audio + video views)",
             "get_plan_results", multi,
             expect_keys=2, asserts=[a_two_distinct_node_keys])

    # ---------------------------------------------------------------- case 6
    # LIP-SYNC REVIEW FAN-OUT (the 99246b15 bug): a plan emits N sibling segment nodes with the
    # SAME tool, each carrying ONE video clip. Per-node keying opened N single-video tabs instead
    # of the ONE multiview review the skill asks for. The fix gives a repeated video tool a SHARED
    # render_key so the whole set collapses to ONE gallery. (The fan-out is now the lip-sync nodes
    # themselves — the per-segment validate nodes were removed.)
    # Real, distinct lip-sync clips (session 99246b15) so the render agent actually fetches them.
    LIPSYNC_CLIPS = [
        f"{CF}/artifacts/external/7385c20d7e3aa03e3654b280.mp4",
        f"{CF}/artifacts/external/6b43877359037830e30ecef2.mp4",
        f"{CF}/artifacts/external/b664003838c8ca4f5d02bc91.mp4",
        f"{CF}/artifacts/external/0b5610530cf8de4e7184f1fe.mp4",
        f"{CF}/artifacts/external/8bc57771e22e2354e398a2ac.mp4",
        f"{CF}/artifacts/external/4b3ccbc3d3d6aeecf1c73b4e.mp4",
        f"{CF}/artifacts/external/302bd30e90549cadc40afc23.mp4",
        f"{CF}/artifacts/external/7c100089dbc97ffb972b67cf.mp4",
    ]

    def lipsync_review(_src):
        return {
            "schema": "plan-result/v1", "plan_id": _SESSION_ID, "status": "SUCCEEDED",
            "nodes": [
                {"node_id": f"seg{i:02d}_lipsync", "kind": "media", "status": "completed",
                 "tool": "usr-8ddf64af24be45c894a7a8c1f254c83f___submit_job", "depends_on": [],
                 "artifacts": [{"url": u, "media_kind": "video", "role": "output"}]}
                for i, u in enumerate(LIPSYNC_CLIPS, 1)
            ],
        }

    def a_one_multiview_gallery(title, mans):
        m = next(iter(mans.values()), None) or {}
        clips = m.get("galleryClips") or []
        check(f"{title}: template == clip-gallery (multiview)",
              m.get("template") == "clip-gallery", f"got {m.get('template')}")
        check(f"{title}: gallery holds all 8 segment clips",
              len(clips) == 8, f"got {len(clips)}")

    run_case("LIP-SYNC REVIEW FAN-OUT (8 sibling video segments -> ONE gallery)",
             "get_plan_results", lipsync_review,
             expect_keys=1, asserts=[a_one_multiview_gallery])

    # NO-FLAG multi-clip default (session 20f7315e). When N independent clips are gathered ACROSS
    # plan nodes (or otherwise rendered together) they carry NO fan-out gallery flag — the coalesce
    # only fires for a same-tool fan-out WITHIN one invoke. ONE node emitting 4 clips reproduces
    # that: no shared seggallery-- key, so the render arrives WITHOUT payload["gallery"]. Those
    # used to stack into one 4-track nle-timeline; Fix 3 defaults >=2 independent peer video clips
    # to a clip-gallery multiview regardless of the flag.
    def multi_clip_one_node(_src):
        return {
            "schema": "plan-result/v1", "plan_id": _SESSION_ID, "status": "SUCCEEDED",
            "nodes": [
                {"node_id": "gen_clips", "kind": "media", "status": "completed",
                 "tool": "some_generic_gen", "depends_on": [],
                 "artifacts": [{"url": u, "media_kind": "video", "role": "output"}
                               for u in LIPSYNC_CLIPS[:4]]}
            ],
        }

    def a_no_flag_defaults_to_gallery(title, mans):
        m = next(iter(mans.values()), None) or {}
        check(f"{title}: template == clip-gallery (multiview, no flag)",
              m.get("template") == "clip-gallery", f"got {m.get('template')}")
        check(f"{title}: gallery holds all 4 clips",
              len(m.get("galleryClips") or []) == 4, f"got {len(m.get('galleryClips') or [])}")

    run_case("MULTI-CLIP NO FLAG (N independent clips -> clip-gallery, session 20f7315e)",
             "some_generic_gen", multi_clip_one_node,
             expect_keys=1, asserts=[a_no_flag_defaults_to_gallery])

    # ---------------------------------------------------------------- summary
    print("\n" + "=" * 62)
    failed = [r for r in results if r[0] == FAIL]
    print(f"E2E: {len(results) - len(failed)}/{len(results)} checks passed")
    for _, name, detail in failed:
        print(f"  FAIL {name} — {detail}")
    # One session per case, so the run spans `<sid>-c1..cN` -- print what actually exists.
    print(f"\nmanifests under: s3://{BUCKET}/artifacts/{sid}-c*/render/")
    print("cleanup: " + " ".join(
        f"aws s3 rm --recursive s3://{BUCKET}/artifacts/{sid}-c{i}/;" for i in range(1, _CASE[0] + 1)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
