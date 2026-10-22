"""Durable, session-scoped render dedup (F4b).

The in-process _rendered_url_sets only dedups within one warm runtime, so a cold start or a
later turn re-rendered the same artifact -- the 3x duplicate tabs. F4b derives the
already-rendered set from the session's persisted render manifests (skills bucket, agent-
readable), so it survives process recycling.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("SKILLS_S3_BUCKET", "b")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _FakeS3:
    def __init__(self, manifests):
        self._m = manifests  # {key: manifest_dict}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=100):
        return {"Contents": [{"Key": k} for k in self._m if k.startswith(Prefix)]}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(json.dumps(self._m[Key]).encode())}


def _with_s3(manifests):
    import render.artifacts as ra
    ra._s3_client_cache = _FakeS3(manifests)
    # Set the bucket EXPLICITLY. `_already_rendered_urls` early-returns an empty set when
    # `_SKILLS_BUCKET` is falsy, and locally it is (`SKILLS_S3_BUCKET` is unset), so these tests
    # were silently relying on ambient module state left behind by whatever ran before.
    # Running test_render_composing_marker.py first broke four of them: its monkeypatch sets
    # `_SKILLS_BUCKET` and then RESTORES it to "", so the dedup set came back empty. The same
    # file passed alone AND in the full suite, so the breakage only showed for that one pair in
    # that one order — order-dependent and invisible until you happened to run it.
    ra._SKILLS_BUCKET = "skills-bkt"
    return ra


def test_prior_render_is_recognized_across_presign_and_host():
    ra = _with_s3({"artifacts/S/render/m1.json":
                   {"tracks": [{"src": "s3://proxy/wf/EXEC/proxy/transcoded.mp4"}]}})
    from render.review import _norm_url
    seen = ra._already_rendered_urls("S")
    # same object, different host + presign query -> still deduped
    assert _norm_url("https://h.s3.amazonaws.com/wf/EXEC/proxy/transcoded.mp4?X-Amz=z") in seen


def test_a_different_jobs_same_filename_is_not_deduped():
    ra = _with_s3({"artifacts/S/render/m1.json":
                   {"tracks": [{"src": "s3://proxy/wf/A/proxy/transcoded.mp4"}]}})
    from render.review import _norm_url
    seen = ra._already_rendered_urls("S")
    assert _norm_url("s3://proxy/wf/B/proxy/transcoded.mp4") not in seen


def test_artifacts_urls_are_also_collected():
    ra = _with_s3({"artifacts/S/render/m1.json":
                   {"artifacts": [{"url": "s3://b/wf/EXEC/audio_music.wav"}]}})
    from render.review import _norm_url
    assert _norm_url("s3://b/wf/EXEC/audio_music.wav") in ra._already_rendered_urls("S")


def test_no_session_or_bucket_returns_empty():
    ra = _with_s3({})
    assert ra._already_rendered_urls("") == set()


def test_s3_failure_returns_empty_never_raises():
    import render.artifacts as ra
    class Boom:
        def list_objects_v2(self, **k):
            raise Exception("denied")
    ra._s3_client_cache = Boom()
    assert ra._already_rendered_urls("S") == set()


def test_candidate_keys_dedup_the_raw_source_across_mirroring():
    # The regression behind duplicate tabs: the manifest track src is the RESOLVED/mirrored copy
    # (a durable digest or CloudFront import), whose key differs from the raw s3:// candidate, so
    # a later dispatch of the SAME source did not dedup and re-rendered. build_render_manifest now
    # records the raw candidate keys; _already_rendered_urls must consume them so the raw source
    # is recognised even though the visible track src is the mirrored form.
    from render.review import _norm_url
    ra = _with_s3({
        "artifacts/S/render/m1.json": {
            # track shows only the mirrored/durable copy...
            "tracks": [{"src": "https://cdn.example.net/artifacts/durable/deadbeef.mp4"}],
            # ...but the raw candidate was the M2C proxy s3:// URI.
            "candidate_keys": [_norm_url("s3://proxy/micro-workflow/media/EXEC/out.mp4")],
        },
    })
    seen = ra._already_rendered_urls("S")
    # A re-dispatch presenting the RAW proxy s3:// (or any presigned form of it) is deduped.
    assert _norm_url("s3://proxy/micro-workflow/media/EXEC/out.mp4") in seen
    assert _norm_url("https://h.s3.amazonaws.com/micro-workflow/media/EXEC/out.mp4?X-Amz=z") in seen
    # The mirrored copy is also present (unchanged behaviour).
    assert _norm_url("https://cdn.example.net/artifacts/durable/deadbeef.mp4") in seen


def test_manifest_id_is_content_addressed_and_closes_the_race():
    # Two CONCURRENT dispatches of the same artifact set must converge on ONE manifest id (one
    # S3 key = one tab) instead of each minting a random uuid (two tabs). The id is order- and
    # presign-independent, and differs for a genuinely different set.
    import render.artifacts as ra
    a = ra._manifest_id_for([
        "s3://proxy/wf/EXEC/audio_music.wav",
        "https://h.s3.amazonaws.com/wf/EXEC/audio_speech.wav?X-Amz=z",
    ])
    # same set, different order + a presigned form of the same objects
    b = ra._manifest_id_for([
        "https://cdn/wf/EXEC/audio_speech.wav",
        "s3://proxy/wf/EXEC/audio_music.wav?X-Amz=other",
    ])
    assert a == b and len(a) == 32
    # a different artifact set gets a different id (no over-collapse)
    c = ra._manifest_id_for(["s3://proxy/wf/OTHER/audio_music.wav"])
    assert c != a
    # empty set never collides deterministically (random fallback)
    assert ra._manifest_id_for([]) != ra._manifest_id_for([])


def test_render_agent_uses_content_addressed_id_from_payload():
    # The real duplicate-tabs bug: the render agent minted its OWN uuid manifest_id and ignored
    # the content-addressed id dispatch_render passed, so every render wrote a distinct S3 key.
    # _resolve_manifest_id must honor payload["manifest_id"]; when ABSENT it now returns "" (not a
    # uuid) so build_render_manifest derives a DETERMINISTIC content-hash from the rendered urls
    # (an explicit render then converges on the hook's tab instead of minting a fresh uuid).
    import importlib
    ra = importlib.import_module("render_agent")
    mid = "a" * 32
    assert ra._resolve_manifest_id({"manifest_id": mid}) == mid           # uses the passed id
    assert ra._resolve_manifest_id({"manifest_id": "", "x": 1}) == ""      # blank -> "" (build derives)
    assert ra._resolve_manifest_id({}) == ""                              # missing -> "" (no uuid)
    assert ra._resolve_manifest_id(None) == ""                            # None -> "" (no uuid)


def test_media_identity_collapses_proxy_and_imported_forms():
    # Faithful reproduction of session 738786a0: one trim rendered TWICE — once as the M2C proxy
    # uri, once as the imported copy — two different normalized keys => two "Video" tabs. Both
    # carry the SAME execution uuid + basename, so _media_identity must collapse them.
    from render.review import _media_identity, _norm_url
    proxy = "s3://m2cv6-proxy/micro-workflow/media-processing/b755f3d3-ac2d-5f25-9891-c2da9f84629f/proxy/transcoded.mp4"
    imported = ("https://dexamplecdn002.cloudfront.net/artifacts/"
                "arn:aws:states:us-west-2:1:execution:m2cv6-media-processing:"
                "b755f3d3-ac2d-5f25-9891-c2da9f84629f/output/x/transcoded.mp4")
    # _norm_url sees them as DIFFERENT (the bug), but _media_identity sees them as the SAME media.
    assert _norm_url(proxy) != _norm_url(imported)
    assert _media_identity(proxy) == _media_identity(imported) == \
        "b755f3d3-ac2d-5f25-9891-c2da9f84629f/transcoded.mp4"
    # A different job's transcoded.mp4 must NOT collapse (guards cross-job collision).
    other = "s3://p/micro-workflow/media-processing/99999999-0000-0000-0000-000000000000/proxy/transcoded.mp4"
    assert _media_identity(other) != _media_identity(proxy)
    # A url with no exec uuid yields no identity (never collapses unrelated files).
    assert _media_identity("https://cdn/artifacts/report.pdf") == ""


def test_already_rendered_collapses_via_media_identity():
    # The durable dedup must recognise the imported form as already-rendered because the FIRST
    # manifest recorded the proxy candidate (same exec uuid + basename).
    from render.review import _media_identity
    ra = _with_s3({
        "artifacts/S/render/first.json": {
            "tracks": [{"src": "https://cdn/artifacts/durable/deadbeef.mp4"}],
            "candidate_keys": ["micro-workflow/media-processing/"
                               "b755f3d3-ac2d-5f25-9891-c2da9f84629f/proxy/transcoded.mp4"],
        },
    })
    seen = ra._already_rendered_urls("S")
    imported_identity = _media_identity(
        "artifacts/arn:aws:states:...:media-processing:"
        "b755f3d3-ac2d-5f25-9891-c2da9f84629f/output/x/transcoded.mp4")
    assert imported_identity in seen  # the re-dispatch of the imported form is deduped


def test_durable_dedup_drop_resolves_the_composing_marker(monkeypatch):
    # Session a1dfec9c: a single trim showed TWO tabs. The dedup correctly dropped the second
    # render (only one manifest persisted), but the dropped render's "Composing…" marker was
    # never resolved -> a phantom spinning tab. The drop path must resolve the marker.
    import render.artifacts as ra
    from render.review import _norm_url
    from jobs import context as jc
    ra._rendered_url_sets.clear()
    url = "https://cdn.example.net/artifacts/durable/abc123.mp4"
    monkeypatch.setattr(ra, "_already_rendered_urls", lambda s: {_norm_url(url)})
    pushed = []

    class _Ch:
        def push(self, status, msg="", **k):
            pushed.append({"status": status, "extra": k.get("extra", {})})

    monkeypatch.setattr(ra, "get_active_channel", lambda: _Ch())
    tokens = jc.set_request_context(actor_id="a", session_id="S1")
    try:
        res = ra.dispatch_render([url], render_request_id="rid-xyz")
    finally:
        jc.reset_request_context(tokens)
    assert res is None  # correctly dropped as a duplicate
    # the composing marker for rid-xyz was resolved (so the UI removes the spinner)
    assert any(e["extra"].get("render_request_id") == "rid-xyz" for e in pushed), \
        f"composing marker not resolved on dedup-drop: {pushed}"


def test_render_keyed_by_job_id_one_tab_per_job():
    # Design §4 B (repro of a1dfec9c 13-tab): render identity = the job/node id, so each analysis
    # job renders ONCE and a re-render (a later "retrieve/summarise" turn, or the same media under
    # a different url form) converges on the SAME manifest key — one tab per job, deterministically.
    import render.artifacts as ra
    proxy = "s3://proxy/micro-workflow/speech-diarization/EXEC/final_transcript.json"
    imported = "https://cdn/artifacts/S/output/PLAN/diarization/final_transcript.json"
    assert ra._render_manifest_id("diarization", [proxy]) == "diarization"
    assert ra._render_manifest_id("diarization", [imported]) == "diarization"
    assert ra._render_manifest_id("diarization", [proxy]) == ra._render_manifest_id("diarization", [imported])
    ids = {ra._render_manifest_id(n, [proxy]) for n in
           ("stem_separation", "diarization", "shot_segmentation", "face_recognition")}
    assert len(ids) == 4
    assert "/" not in ra._render_manifest_id("plan/node:1", [proxy])
    h = ra._render_manifest_id("", [proxy])
    assert len(h) == 32 and h == ra._render_manifest_id("", [proxy])


def test_a_file_shown_in_a_gallery_is_not_auto_rendered_again():
    # show_media_gallery's named audition gallery (staging fe200e49): the presigned previews it shows must not come
    # back as a second, file-id-labelled auto-render tab. Its cards hold the STABLE reference after stabilization;
    # the original links travel as candidate_keys (what show_media_gallery records).
    from render.review import _norm_url
    presigned = "https://proxy.s3.amazonaws.com/micro-workflow/tts/voices/85bc/preview.wav?X-Amz-Signature=s"
    ra = _with_s3({"artifacts/S/render/gallery-1.json": {
        "template": "clip-gallery",
        "galleryClips": [{"label": "agent_olive_voice_v4", "src": "https://cdn.example/x/other.wav", "kind": "audio"}],
        "candidate_keys": [_norm_url(presigned)]}})
    seen = ra._already_rendered_urls("S")
    assert _norm_url(presigned) in seen                              # the original link: skipped
    assert _norm_url("https://cdn.example/x/other.wav") in seen      # a card's own src: skipped
