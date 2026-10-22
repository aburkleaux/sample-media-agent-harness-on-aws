"""Contract tests for the built-in video-processing skill package."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


SKILL_ROOT = Path(__file__).parents[1] / "skills" / "video-processing"
SKILL_MD = SKILL_ROOT / "SKILL.md"
# The references carry the M2C client kit's video-processing reference bodies VERBATIM (from their
# "# ... System Prompt" heading on) under a harness-owned header. Refresh: copy the new kit's bodies,
# bump UPSTREAM, re-pin. hybrid_search / prompt_registry are not copied: those tools are withheld here.
UPSTREAM = "client-kit-alpha.20261013a"
REFERENCE_SNAPSHOTS = {
    "audio_description_agent.md": "e5a6246799bdd3a1adae922369ce35af46c008ea57bfb2210eecdffd925c7da7",
    "audio_stem_agent.md": "70ebe4808418481be8968fc7184c1f708448b6ae03a4897878283132f789bfda",
    "bedrock_inference_agent.md": "5db4782b2fd5104995f466cdb8891ea2902bd5038ed67223cb68075588943b08",
    "cast_resolution_agent.md": "1e5aa38303610ff4dc187cfa281354e8a9332562a1e2ed62a0bd56707ddbcce6",
    "conversation_analysis_agent.md": "af6148323d37e96d0839d329eb38047f9c23d313ba9082ed8526d1446de2a198",
    "face_recognition_agent.md": "c3700f437fc28feddb329597cc5fd05cc330d3a560cd540ff62938f0f72161ab",
    "media_processing_agent.md": "eacd10985693c8f3243c717708299471a3d45481f812c93967d73d47108d300a",
    "program_structure_analysis_agent.md": "4119df6ab0d3be95885f67383eae521f1380bfff44c8a602dc50053d8f8cabb5",
    "shot_metadata_enrichment_agent.md": "68644df81a1e7473cc542f5f0c6ecb84ee723ec84308b7234ba9b77a6c0a486c",
    "shot_segmentation_agent.md": "8869c7995eb114b0d6df606f947ccfe2c5c3418e18b157d6d67e442a44ff9b97",
    "speech_diarization_agent.md": "e7fa6f02da8253210f0901acb74f48eedf794e20d22a4f2905ca92fefe4f65c1",
    "speech_enhancement_agent.md": "bd0c2f3a15b39022c4b41a369104a84a77c8225a6cafb8ecd17adb9342b543bc",
    "tts_agent.md": "ffe56e9645f8bcfdb46493ce132417c9f0f9bcedd658554a2299ac1e458eaa45",
}
REFERENCE_TOOLS = {
    "audio_description_agent.md": "audio-description___generate_ad",
    "audio_stem_agent.md": "audio-stem___separate_audio_stems",
    "bedrock_inference_agent.md": "bedrock-inference___run_inference",
    "cast_resolution_agent.md": "cast-resolution___resolve_cast",
    "conversation_analysis_agent.md": "conversation-analysis___analyze_conversation",
    "face_recognition_agent.md": "face-recognition___recognize_faces",
    "media_processing_agent.md": "media-processing___process_media",
    "program_structure_analysis_agent.md": "program-structure-analysis___analyze_program_structure",
    "shot_metadata_enrichment_agent.md": "shot-metadata-enrichment___enrich_shot_metadata",
    "shot_segmentation_agent.md": "shot-segmentation___segment_shots",
    "speech_diarization_agent.md": "speech-diarization___diarize_speech",
    "speech_enhancement_agent.md": "speech-enhancement___enhance_speech",
    "tts_agent.md": "text-to-speech___synthesize_speech",
}


def _upstream_body(reference: Path) -> str:
    content = reference.read_text(encoding="utf-8")
    heading = re.search(r"(?m)^# .+ System Prompt$", content)
    assert heading is not None, f"missing upstream heading in {reference.name}"
    return content[heading.start() :]


def test_independent_parallel_multitool_routes_to_one_plan() -> None:
    # Regression guard for the "multi-step ask did not create a DAG" bug (session 684bec82): the
    # skill's routing must match the orchestrator — a request needing MORE THAN ONE submit tool
    # call, even INDEPENDENT/PARALLEL analyses of one asset, is ONE plan (parallel nodes with no
    # depends_on), never several separate dispatch_tool calls. dispatch_tool is reserved for the
    # single-tool case.
    content = SKILL_MD.read_text(encoding="utf-8")
    assert "MORE THAN ONE submit tool" in content
    assert "INDEPENDENT/PARALLEL" in content
    assert "NO `depends_on`" in content
    assert "ONLY when the whole request is a SINGLE gateway submit tool" in content


def test_skill_is_a_concise_agentskills_router() -> None:
    content = SKILL_MD.read_text(encoding="utf-8")
    lines = content.splitlines()

    assert lines[0] == "---"
    assert "name: video-processing" in lines[:6]
    assert SKILL_ROOT.name == "video-processing"
    assert len(lines) < 500
    assert "Harness job lifecycle overrides reference polling defaults" in content
    assert "`dispatch_tool`" in content
    assert "`plan_and_execute_tool`" in content
    assert "The live gateway tool schema is authoritative" in content
    assert "Rendering is automatic" in content


def test_every_authoritative_reference_is_linked_one_level_from_skill_md() -> None:
    skill_content = SKILL_MD.read_text(encoding="utf-8")

    for filename, tool_name in REFERENCE_TOOLS.items():
        reference = SKILL_ROOT / "references" / filename
        assert reference.is_file()
        assert f"references/{filename}" in skill_content
        assert tool_name in skill_content
        assert f"Upstream: {UPSTREAM}" in reference.read_text(encoding="utf-8")


def test_reference_bodies_match_the_author_team_snapshots() -> None:
    for filename, expected_digest in REFERENCE_SNAPSHOTS.items():
        reference = SKILL_ROOT / "references" / filename
        body = _upstream_body(reference)
        assert hashlib.sha256(body.encode("utf-8")).hexdigest() == expected_digest
        prefix = reference.read_text(encoding="utf-8").split(body, maxsplit=1)[0]
        assert "Media Agent Harness lifecycle override" in prefix
        assert "do not follow the default polling instructions" in prefix


def test_time_window_routing_reflects_current_author_guidance() -> None:
    content = SKILL_MD.read_text(encoding="utf-8")

    assert "old blanket rule" in content
    assert "speech-diarization___diarize_speech`: supports `mark_in_out`" in content
    assert "window must exceed 10 seconds" in content
    assert "face-recognition___recognize_faces`: supports `mark_in_out`" in content
    assert "audio-stem___separate_audio_stems`: no documented time-window" in content
    assert "shot-segmentation___segment_shots`: no documented time-window" in content
    assert 'collection_id: ""' in content
    assert "Never persist or delete face data without explicit user intent" in content
    # ffmpeg-in-code_interpreter is the route for a genuine gap; the multi-clip crossfade case
    # specifically cannot use the managed compose endpoint (it rejects >1 video track), and its
    # output must land in the session-scoped prefix, not a flat key.
    assert "Use ffmpeg in `code_interpreter` for a genuine gap" in content
    assert "Multiple video tracks are not supported" in content
    assert "xfade" in content
    # The session-scoped prefix is the one S3 CONTEXT names (asset-lineage Phase 3 (a): it may be the
    # legacy `artifacts/<session>/output/` or the project layout's `projects/.../sessions/.../output/`).
    assert "session output prefix named in the S3 CONTEXT" in content
    assert "`artifacts/<session>/output/` or\n  `projects/<project>/sessions/<session>/output/`" in content
    assert "ONLY `process_media` has" not in content


def test_combined_output_requires_faststart_for_web_playback() -> None:
    """Regression guard for the stitched-video playback bug (session f485d134): an ffmpeg
    combine/mux/concat/xfade output had its `moov` atom at the END (ffmpeg default), so the
    valid MP4 would not play in a browser `<video>` or through the `/api/artifact` proxy. The
    skill must instruct `-movflags +faststart` on EVERY written MP4 — including stream-copy
    (`-c copy`) paths — and a web-safe re-encode (`libopenh264` + `yuv420p`, the harness-built
    ffmpeg's encoder) for filtergraph paths."""
    content = SKILL_MD.read_text(encoding="utf-8")
    assert "-movflags +faststart" in content
    # stream-copy paths must still relocate moov to the front
    assert "-c copy -movflags +faststart" in content
    # re-encode paths must be browser-decodable (yuv420p, not yuv444p)
    assert "-c:v libopenh264 -b:v 5M -pix_fmt yuv420p" in content
    # the sandbox's ffmpeg has no libx264 — the skill must not prescribe it
    assert "-c:v libx264" not in content
    assert "yuv444p" in content  # the anti-pattern is called out explicitly
    # the WHY is documented so the guidance survives future edits
    assert "moov" in content and "faststart" in content


def test_crossfade_stitch_moderation_rationale_is_documented() -> None:
    """Stitching already-generated shots via fal can be denied on content policy even for benign
    footage; the code_interpreter xfade/concat path avoids moderation and is preferred."""
    content = SKILL_MD.read_text(encoding="utf-8")
    assert "re-moderates every clip" in content
    assert "content_policy_violation" in content
    assert 'never fall back to a fal/gateway compose' in content


def test_probe_guidance_does_not_claim_the_status_carries_codec_or_resolution() -> None:
    """Session 7e92b95d: the skill said the probe status "already carries codec, resolution, fps" and forbade
    reading probe.json — the reference lists no such fields, so turn 1 answered half and turn 2 took 55 steps."""
    text = " ".join(SKILL_MD.read_text(encoding="utf-8").split())
    assert "the status result already carries** (codec" not in text
    assert "it does NOT carry codec, resolution, fps or bitrate" in text
    assert "ONE `read_result_files` call with the exact `s3://<bucket>/<probe_s3_key>` URIs" in text
    assert "FIRST call `project_status(name=<file name>)`" in text
    assert "never an ellipsized" in text
    ref = " ".join((SKILL_ROOT / "references" / "media_processing_agent.md").read_text(encoding="utf-8").split())
    assert "`probe_s3_key` | S3 key of `probe.json`" in ref  # the field the guidance relies on
