"""``ingestion.reducer`` — the rule table of design Component 8, without I/O (Properties 11–14)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import reducer as R  # noqa: E402
from ingestion import workflows as W  # noqa: E402

NOW = 10_000.0


def ok(job, **kw):
    return {"status": "SUCCEEDED", "jobId": job, "runSeq": 1, "inputs": [], "submittedAt": 0, **kw}


def running(job, **kw):
    return {"status": "RUNNING", "jobId": job, "runSeq": 1, "inputs": [], "submittedAt": NOW - 10, **kw}


SPEECH = {"facts": {"hasSpeech": True}, "outputs": {"transcript": "s3://m/t.json"}}


def wave1(diar=None):
    return {W.SHOT_SEGMENTATION: ok("s", outputPrefix="s3://m/shots-run"),
            W.SPEECH_DIARIZATION: diar or ok("d", **SPEECH), W.FRAME_CAPTURE: ok("f")}


def test_start_submits_wave1_together():
    assert R.plan_video({}, NOW, 1) == R.Plan(submit=R.VIDEO_WAVE1)


def test_wave1_running_waits():
    steps = {s: running(s) for s in R.VIDEO_WAVE1}
    assert R.plan_video(steps, NOW, 1) == R.Plan()


def test_speech_submits_enrichment_with_transcript_and_conversation_analysis():
    plan = R.plan_video(wave1(), NOW, 1)
    assert plan.submit == (W.ENRICHMENT, W.CONVERSATION_ANALYSIS)
    args = R.submit_arguments(W.ENRICHMENT, wave1(), "s3://src", None)
    assert args == {"shot_segmentation_s3uri": "s3://m/shots-run", "use_batch": False,
                    "transcript_s3uri": "s3://m/t.json"}
    assert R.submit_arguments(W.CONVERSATION_ANALYSIS, wave1(), "s3://src", None) == {
        "transcript_s3uri": "s3://m/t.json"}


def test_no_speech_and_no_audio_submit_enrichment_only_without_transcript():
    silent = ok("d", facts={"hasSpeech": False}, outputs={"transcript": "s3://m/t.json"})
    no_audio = {"status": R.NO_AUDIO, "jobId": "d", "runSeq": 1, "inputs": [], "error": "no audio stream"}
    for diar in (silent, no_audio):
        steps = wave1(diar)
        assert R.plan_video(steps, NOW, 1).submit == (W.ENRICHMENT,)
        assert "transcript_s3uri" not in R.submit_arguments(W.ENRICHMENT, steps, "s3://src", None)


def test_build_when_wave2_done():
    steps = {**wave1(), W.ENRICHMENT: ok("e", inputs=["s", "d"]), W.CONVERSATION_ANALYSIS: ok("c", inputs=["d"])}
    assert R.plan_video(steps, NOW, 1) == R.Plan(build=True)


def test_rerun_upstream_invalidates_consumers():
    steps = {**wave1(), W.ENRICHMENT: ok("e", inputs=["s-old", "d"]), W.CONVERSATION_ANALYSIS: ok("c", inputs=["d"])}
    assert R.plan_video(steps, NOW, 2).submit == (W.ENRICHMENT,)


def test_failure_in_this_run_fails_with_step_and_error():
    steps = wave1(diar={"status": "FAILED", "jobId": "d", "runSeq": 1, "inputs": [], "error": "boom"})
    assert R.plan_video(steps, NOW, 1).fail == (W.SPEECH_DIARIZATION, "boom")


# alpha.20261014a reports a silent source only as an opaque media-processing failure (staging project
# p49b9cc6, 2026-10-14): the verdict comes from frame capture's probe instead.
OPAQUE = {"status": "FAILED", "jobId": "d", "runSeq": 1, "inputs": [],
          "error": "media-processing run c98934e4 ended FAILED: see the run prefix"}
SILENT_PROBE = {"facts": {"technical": {"video": {"codec": "h264", "width": 512}, "durationSec": 51.7}}}


def test_probe_with_no_audio_stream_turns_a_diarization_failure_into_no_audio():
    steps = {**wave1(diar=OPAQUE), W.FRAME_CAPTURE: ok("f", **SILENT_PROBE)}
    plan = R.plan_video(steps, NOW, 1)
    assert plan.fail is None and plan.submit == (W.ENRICHMENT,)
    assert "transcript_s3uri" not in R.submit_arguments(W.ENRICHMENT, steps, "s3://src", None)
    # runSeq read back from DynamoDB is a string
    assert R.plan_video({**steps, W.SPEECH_DIARIZATION: {**OPAQUE, "runSeq": "1"}}, NOW, 1).fail is None


def test_diarization_failure_waits_for_the_probe():
    steps = {**wave1(diar=OPAQUE), W.FRAME_CAPTURE: running("f")}
    assert R.plan_video(steps, NOW, 1) == R.Plan()


def test_diarization_failure_stands_when_the_probe_shows_audio_or_nothing():
    with_audio = {"facts": {"technical": {"video": {"codec": "h264"}, "audio": {"codec": "aac", "channels": 2}}}}
    for fc in (ok("f", **with_audio), ok("f"), {"status": "FAILED", "jobId": "f", "runSeq": 1, "inputs": [],
                                                   "error": "probe failed", **SILENT_PROBE}):
        steps = {**wave1(diar=OPAQUE), W.FRAME_CAPTURE: fc}
        assert R.plan_video(steps, NOW, 1).fail is not None, fc


def test_an_earlier_runs_diarization_failure_is_still_rerun_on_a_silent_probe():
    steps = {**wave1(diar=OPAQUE), W.FRAME_CAPTURE: ok("f", **SILENT_PROBE)}
    assert R.plan_video(steps, NOW, 2).submit == (W.SPEECH_DIARIZATION,)


def test_failure_from_an_earlier_run_is_rerun_with_a_token():
    failed = {"status": "FAILED", "jobId": "d", "runSeq": 1, "inputs": [], "error": "boom"}
    steps = wave1(diar=failed)
    assert R.plan_video(steps, NOW, 2) == R.Plan(submit=(W.SPEECH_DIARIZATION,))
    tok = R.token_for(W.SPEECH_DIARIZATION, failed, 2, "video:x")
    assert tok and tok.startswith("r2-") and tok == R.token_for(W.SPEECH_DIARIZATION, failed, 2, "video:x")
    assert tok != R.token_for(W.SPEECH_DIARIZATION, failed, 3, "video:x")
    assert R.token_for(W.SPEECH_DIARIZATION, {}, 1, "video:x") is None


def test_timeout_and_call_failures():
    late = running("s", submittedAt=NOW - 6 * 3600 - 1)
    assert R.plan_video({W.SHOT_SEGMENTATION: late}, NOW, 1).fail == (W.SHOT_SEGMENTATION, "timed out after 6 h")
    flaky = running("s", callFailures=3, error="Transient: 503")
    assert R.plan_video({W.SHOT_SEGMENTATION: flaky}, NOW, 1).fail == (W.SHOT_SEGMENTATION, "Transient: 503")


def test_submitting_record_is_resubmitted_after_its_gap():
    rec = {"status": R.SUBMITTING, "runSeq": 1, "inputs": [], "notBefore": NOW + 5, "callFailures": 1}
    plan = R.plan_video({W.SHOT_SEGMENTATION: rec}, NOW, 1)
    assert W.SHOT_SEGMENTATION in plan.submit
    assert not R.submit_due(rec, NOW) and R.submit_due(rec, NOW + 5)


def test_audio_rules():
    assert R.plan_audio({}, NOW, 1) == R.Plan(submit=(W.SPEECH_DIARIZATION,))
    silent = {W.SPEECH_DIARIZATION: ok("d", facts={"hasSpeech": False})}
    # Audio with no speech is kept but NOT indexed -- never a failure (Req 12.7, 2026-10).
    plan = R.plan_audio(silent, NOW, 1)
    assert plan.fail is None
    assert plan.skip == (W.SPEECH_DIARIZATION, R.NO_SPEECH_REASON) == (W.SPEECH_DIARIZATION, "no_speech")
    # A diarization that failed for lack of speech is recorded NO_AUDIO by the tick: same outcome.
    no_vad = {W.SPEECH_DIARIZATION: {"status": R.NO_AUDIO, "runSeq": 1, "inputs": [],
                                     "error": "No VAD turns found after flattening"}}
    assert R.plan_audio(no_vad, NOW, 1) == R.Plan(skip=(W.SPEECH_DIARIZATION, "no_speech"))
    # A genuine diarization failure on audio still fails the asset.
    broke = {W.SPEECH_DIARIZATION: {"status": "FAILED", "runSeq": 1, "inputs": [], "error": "boom"}}
    assert R.plan_audio(broke, NOW, 1).fail == (W.SPEECH_DIARIZATION, "boom")
    talk = {W.SPEECH_DIARIZATION: ok("d", **SPEECH)}
    assert R.plan_audio(talk, NOW, 1) == R.Plan(submit=(W.CONVERSATION_ANALYSIS,))
    done = {**talk, W.CONVERSATION_ANALYSIS: ok("c", inputs=["d"])}
    assert R.plan_audio(done, NOW, 1) == R.Plan(build=True)


def test_wait_seconds():
    assert R.wait_seconds({"a": {"submittedAt": NOW - 60}}, NOW) == 15
    assert R.wait_seconds({"a": {"submittedAt": NOW - 600}}, NOW) == 30
    assert R.wait_seconds({}, NOW, NOW - 10) == 15


def test_document_rules():
    # Nothing extracted yet → extract in the tick.
    assert R.plan_document({}, NOW, 1) == R.Plan(extract=True)
    # Text found → build.
    done = {W.DOCUMENT_EXTRACT: {"status": "SUCCEEDED", "runSeq": 1, "hasText": True, "key": "k"}}
    assert R.plan_document(done, NOW, 1) == R.Plan(build=True)
    # No text layer → kept but NOT indexed (stored + indexSkippedReason), never a failure.
    none = {W.DOCUMENT_EXTRACT: {"status": "SUCCEEDED", "runSeq": 1, "hasText": False,
                                 "skipReason": R.NO_TEXT_REASON}}
    plan = R.plan_document(none, NOW, 1)
    assert plan.fail is None and plan.skip == ("document text", "no_text_layer")
    # Too large to extract → its own reason, still a skip.
    big = {W.DOCUMENT_EXTRACT: {"status": "SUCCEEDED", "runSeq": 1, "hasText": False,
                                "skipReason": R.DOC_TOO_LARGE_REASON}}
    assert R.plan_document(big, NOW, 1).skip == ("document text", "document_too_large")
    # An extract that failed in THIS run fails the asset (infra); an earlier run's failure re-extracts.
    failed = {W.DOCUMENT_EXTRACT: {"status": "FAILED", "runSeq": 1, "error": "AccessDenied"}}
    assert R.plan_document(failed, NOW, 1).fail == (W.DOCUMENT_EXTRACT, "AccessDenied")
    assert R.plan_document({W.DOCUMENT_EXTRACT: {**failed[W.DOCUMENT_EXTRACT], "runSeq": 1}}, NOW, 2) == \
        R.Plan(extract=True)


def test_failure_step_for_document():
    assert R.failure_step_for("document", "analyzing", {}) == W.DOCUMENT_EXTRACT
    done = {W.DOCUMENT_EXTRACT: {"status": "SUCCEEDED"}}
    assert R.failure_step_for("document", "analyzing", done) == "document build"
