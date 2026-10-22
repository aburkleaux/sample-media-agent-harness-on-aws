"""What an ingestion run does next (design Component 8 rule table). Pure: no I/O, no clock reads.

``IngestTick`` observes (polls the running steps, reads outputs on success), asks this module what to do, does
it, and writes the row. Every rule that decides a status, a submission, or a failure lives here, so the rule
table is tested without M2C, DynamoDB, or time.

A step record (``steps[<step>]`` on the ASSET row)::

    {status: SUBMITTING|RUNNING|SUCCEEDED|NO_AUDIO|FAILED|TIMED_OUT|ABORTED|REJECTED,
     runSeq, jobId?, submittedAt, finishedAt?, outputs: {label: uri}, outputPrefix?, error?,
     inputs: [upstream jobIds], facts: {durationMs?, hasSpeech?}, callFailures, notBefore}

``SUBMITTING`` is written (conditionally on ``rowVersion``) BEFORE the M2C call, so a crash between the call and
the next write resubmits identical arguments, which M2C absorbs into the run it already started.
``inputs`` names the upstream job ids a record consumed: a record is reusable only while they are unchanged, which
is how a rerun step forces every consumer to rerun (Req 7.11).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping, Optional

from . import workflows as W

SUBMITTING = "SUBMITTING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
NO_AUDIO = "NO_AUDIO"
REJECTED = "REJECTED"
DONE_OK = frozenset({SUCCEEDED, NO_AUDIO})
DONE_BAD = frozenset({"FAILED", "TIMED_OUT", "ABORTED", REJECTED})
IN_FLIGHT = frozenset({SUBMITTING, RUNNING})

# ``indexSkippedReason`` on an audio asset kept as ``stored`` because it holds no speech.
NO_SPEECH_REASON = "no_speech"
# ``indexSkippedReason`` on a document (PDF) kept as ``stored`` because it has no extractable text layer
# (a scan, an encrypted/corrupt file), or is too large to extract (Requirement 16). Never a failure.
NO_TEXT_REASON = "no_text_layer"
DOC_TOO_LARGE_REASON = "document_too_large"

CALL_FAILURES_TO_FAIL = 3
CALL_RETRY_GAP_S = 30

VIDEO_WAVE1 = (W.SHOT_SEGMENTATION, W.SPEECH_DIARIZATION, W.FRAME_CAPTURE)
VIDEO_STEPS = VIDEO_WAVE1 + (W.ENRICHMENT, W.CONVERSATION_ANALYSIS)
AUDIO_STEPS = (W.SPEECH_DIARIZATION, W.CONVERSATION_ANALYSIS)


@dataclass(frozen=True)
class Config:
    step_timeout_s: int = 6 * 3600
    index_timeout_s: int = 3600
    frames: W.FrameConfig = W.FrameConfig()
    image_model_chain: tuple[str, ...] = ()  # empty → M2C's deployment default
    image_thinking: str = ""  # run_inference ``thinking`` ("disabled" | "adaptive"); empty → M2C's default


@dataclass(frozen=True)
class Plan:
    submit: tuple[str, ...] = ()           # steps to submit now (in this order)
    fail: Optional[tuple[str, str]] = None  # (step, message) → the asset fails
    build: bool = False                    # every analysis step done: build the documents
    render: bool = False                   # image: make the Search_Rendition now (in the tick)
    extract: bool = False                  # document: extract the PDF text now (in the tick)
    # (step, reason code) -> the asset is kept but NOT indexed (``stored`` + ``indexSkippedReason``), never failed.
    skip: Optional[tuple[str, str]] = None


def _get(steps: Mapping, step: str) -> Mapping:
    rec = steps.get(step)
    return rec if isinstance(rec, Mapping) else {}


def _num(v, default=0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def inputs_for(step: str, steps: Mapping, speech: bool) -> tuple[str, ...]:
    """The upstream job ids a submission of ``step`` consumes now."""
    shots = str(_get(steps, W.SHOT_SEGMENTATION).get("jobId") or "")
    diar = str(_get(steps, W.SPEECH_DIARIZATION).get("jobId") or "")
    if step == W.ENRICHMENT:
        return (shots, diar) if speech else (shots,)
    if step == W.CONVERSATION_ANALYSIS:
        return (diar,)
    if step == W.IMAGE_DESCRIPTION:
        return (str(_get(steps, W.IMAGE_RENDITION).get("sha256") or ""),)
    return ()


def _current(rec: Mapping, want_inputs: tuple[str, ...]) -> bool:
    """A record that counts for this run: not failed, and built from the inputs we would pass now."""
    if not rec or rec.get("status") in DONE_BAD:
        return False
    return tuple(rec.get("inputs") or ()) == want_inputs


def token_for(step: str, prev: Mapping, run_seq: int, asset_id: str) -> Optional[str]:
    """No token on a step's first run; a stable, run-unique one on every rerun (Req 7.15, 8.8)."""
    if not prev:
        return None
    h = hashlib.sha256(f"{asset_id}|{step}|{run_seq}".encode()).hexdigest()[:16]
    return W.idempotency_token(run_seq, h)


def _check(step: str, rec: Mapping, now: float, cfg: Config, run_seq: int) -> Optional[tuple[str, str]]:
    """A failure rule for one record, or ``None``.

    A record that failed in an EARLIER run is not a failure of this one: Retry and Re-add rerun it.
    """
    st = rec.get("status")
    if st in DONE_BAD:
        if int(_num(rec.get("runSeq"))) == run_seq:
            return step, str(rec.get("error") or st)
        return None
    if st in IN_FLIGHT:
        if int(_num(rec.get("callFailures"))) >= CALL_FAILURES_TO_FAIL:
            return step, str(rec.get("error") or "the M2C call kept failing")
        sub = rec.get("submittedAt")
        if st == RUNNING and sub is not None and now - _num(sub) > cfg.step_timeout_s:
            return step, f"timed out after {cfg.step_timeout_s // 3600} h"
    return None


def has_speech(steps: Mapping) -> bool:
    d = _get(steps, W.SPEECH_DIARIZATION)
    return d.get("status") == SUCCEEDED and bool((d.get("facts") or {}).get("hasSpeech"))


def _stage(steps: Mapping, stage: tuple[str, ...], wants: Mapping[str, tuple], now: float, cfg: Config,
           run_seq: int) -> tuple[Optional[tuple[str, str]], tuple[str, ...], bool]:
    """One wave: ``(failure, to_submit, all_done_ok)``. A record counts only if built from ``wants[step]``."""
    submit: list[str] = []
    for step in stage:
        rec = _get(steps, step)
        if rec and tuple(rec.get("inputs") or ()) == wants[step]:
            bad = _check(step, rec, now, cfg, run_seq)
            if bad:
                return bad, (), False
        if not _current(rec, wants[step]) or rec.get("status") == SUBMITTING:
            submit.append(step)
    done = not submit and all(_get(steps, s).get("status") in DONE_OK for s in stage)
    return None, tuple(submit), done


def _probe_says_silent(rec: Mapping) -> Optional[bool]:
    """Frame capture's probe: ``True`` = a video stream and no audio stream, ``False`` = audio, ``None`` = unknown."""
    tech = (rec.get("facts") or {}).get("technical") if rec.get("status") == SUCCEEDED else None
    if not isinstance(tech, Mapping) or not tech.get("video"):
        return None
    return not tech.get("audio")


def _silent_video_steps(steps: Mapping, run_seq: int) -> Mapping:
    """A diarization that FAILED this run on a source the probe shows has no audio stream is ``NO_AUDIO``.

    Decided from the video's own probe, not from M2C's error text: alpha.20261014a reports a silent source only
    as "media-processing run <id> ended FAILED: see the run prefix" (and the prefix is empty), which none of
    ``W.is_no_audio_stream``'s patterns can match. While frame capture is still running the failure is held
    (read as RUNNING) so it is not reported before the probe can explain it; a probe that shows audio, or none
    at all, leaves the failure standing.
    """
    diar = _get(steps, W.SPEECH_DIARIZATION)
    if diar.get("status") != "FAILED" or int(_num(diar.get("runSeq"))) != run_seq:
        return steps
    fc = _get(steps, W.FRAME_CAPTURE)
    if fc.get("status") in IN_FLIGHT:
        return {**steps, W.SPEECH_DIARIZATION: {**diar, "status": RUNNING, "submittedAt": None}}
    if _probe_says_silent(fc):
        return {**steps, W.SPEECH_DIARIZATION: {**diar, "status": NO_AUDIO}}
    return steps


def plan_video(steps: Mapping, now: float, run_seq: int, cfg: Config = Config()) -> Plan:
    """Rule table rows for kind ``video``: wave 1 together, then enrichment (+ conversation-analysis)."""
    steps = _silent_video_steps(steps, run_seq)
    bad, submit, done = _stage(steps, VIDEO_WAVE1, {s: () for s in VIDEO_WAVE1}, now, cfg, run_seq)
    if bad or submit or not done:
        return Plan(submit=submit, fail=bad)
    speech = has_speech(steps)
    need = (W.ENRICHMENT, W.CONVERSATION_ANALYSIS) if speech else (W.ENRICHMENT,)
    bad, submit, done = _stage(steps, need, {s: inputs_for(s, steps, speech) for s in need}, now, cfg, run_seq)
    return Plan(submit=submit, fail=bad, build=done and not bad)


def plan_audio(steps: Mapping, now: float, run_seq: int, cfg: Config = Config()) -> Plan:
    """Rule table rows for kind ``audio`` (Req 12.5–12.11)."""
    bad, submit, done = _stage(steps, (W.SPEECH_DIARIZATION,), {W.SPEECH_DIARIZATION: ()}, now, cfg, run_seq)
    if bad or submit or not done:
        return Plan(submit=submit, fail=bad)
    if not has_speech(steps):
        # Music / sound effects: nothing to transcribe. Kept, not indexed, and not a failure (Req 12.7).
        return Plan(skip=(W.SPEECH_DIARIZATION, NO_SPEECH_REASON))
    want = {W.CONVERSATION_ANALYSIS: inputs_for(W.CONVERSATION_ANALYSIS, steps, True)}
    bad, submit, done = _stage(steps, (W.CONVERSATION_ANALYSIS,), want, now, cfg, run_seq)
    return Plan(submit=submit, fail=bad, build=done and not bad)


def plan_image(steps: Mapping, now: float, run_seq: int, cfg: Config = Config()) -> Plan:
    """Rule table rows for kind ``image`` (Req 8.5–8.10): rendition in the tick, then the description run."""
    rend = _get(steps, W.IMAGE_RENDITION)
    if rend.get("status") in DONE_BAD:
        if int(_num(rend.get("runSeq"))) == run_seq:
            return Plan(fail=(W.IMAGE_RENDITION, str(rend.get("error") or "failed")))
        return Plan(render=True)
    if rend.get("status") != SUCCEEDED:
        return Plan(render=True)
    want = {W.IMAGE_DESCRIPTION: inputs_for(W.IMAGE_DESCRIPTION, steps, False)}
    bad, submit, done = _stage(steps, (W.IMAGE_DESCRIPTION,), want, now, cfg, run_seq)
    return Plan(submit=submit, fail=bad, build=done and not bad)


def plan_document(steps: Mapping, now: float, run_seq: int, cfg: Config = Config()) -> Plan:
    """Rule table for kind ``document`` (Requirement 16): extract the PDF text in the tick, then build.

    A document never hard-fails on its CONTENT: a PDF with no text layer (or one too large to extract) is
    recorded by the tick as ``SUCCEEDED`` with ``hasText: False`` and a ``skipReason`` -> kept as ``stored``
    with ``indexSkippedReason``, exactly like an audio asset with no speech. Only a genuine infra error
    (an unreadable S3 object) fails, through the state machine's Catch (``failure_step_for``).
    """
    ex = _get(steps, W.DOCUMENT_EXTRACT)
    st = ex.get("status")
    if st in DONE_BAD:
        # An extract that failed in THIS run fails the asset; an earlier run's failure is re-extracted.
        if int(_num(ex.get("runSeq"))) == run_seq:
            return Plan(fail=(W.DOCUMENT_EXTRACT, str(ex.get("error") or "failed")))
        return Plan(extract=True)
    if st != SUCCEEDED:
        return Plan(extract=True)
    if not ex.get("hasText"):
        return Plan(skip=("document text", str(ex.get("skipReason") or NO_TEXT_REASON)))
    return Plan(build=True)


def submit_arguments(step: str, steps: Mapping, source_uri: str, token: Optional[str],
                     cfg: Config = Config()) -> dict:
    if step == W.SHOT_SEGMENTATION:
        return W.shot_segmentation_args(source_uri, token)
    if step == W.SPEECH_DIARIZATION:
        return W.speech_diarization_args(source_uri, token)
    if step == W.FRAME_CAPTURE:
        return W.frame_capture_args(source_uri, cfg.frames, token)
    transcript = (_get(steps, W.SPEECH_DIARIZATION).get("outputs") or {}).get("transcript")
    if step == W.ENRICHMENT:
        shots = _get(steps, W.SHOT_SEGMENTATION)
        prefix = str(shots.get("outputPrefix") or "")
        return W.enrichment_args(prefix, transcript if has_speech(steps) else None, token)
    if step == W.CONVERSATION_ANALYSIS:
        return W.conversation_analysis_args(str(transcript or ""), token)
    raise ValueError(f"no submission for step {step!r}")


def wait_seconds(steps: Mapping, now: float, index_submitted_at: Optional[float] = None) -> int:
    """15 s while anything was submitted in the last 3 minutes, else 30 s (design §8 event-history bound)."""
    recent = [_num(r.get("submittedAt"), -1e18) for r in steps.values() if isinstance(r, Mapping)]
    if index_submitted_at is not None:
        recent.append(index_submitted_at)
    return 15 if any(now - t < 180 for t in recent) else 30


def poll_due(rec: Mapping, now: float) -> bool:
    return rec.get("status") == RUNNING and bool(rec.get("jobId")) and now >= _num(rec.get("notBefore"))


def submit_due(rec: Mapping, now: float) -> bool:
    return now >= _num(rec.get("notBefore"))


def failure_step_for(kind: str, status: str, steps: Mapping) -> str:
    """Which step an unexpected internal error is reported under (Req 7.3 needs one of the named steps)."""
    if status == "indexing":
        return "indexing"
    if kind == "image":
        return W.IMAGE_DESCRIPTION if _get(steps, W.IMAGE_RENDITION).get("status") == SUCCEEDED \
            else W.IMAGE_RENDITION
    if kind == "document":
        return "document build" if _get(steps, W.DOCUMENT_EXTRACT).get("status") == SUCCEEDED \
            else W.DOCUMENT_EXTRACT
    order = VIDEO_STEPS if kind == "video" else AUDIO_STEPS
    for s in order:
        if _get(steps, s).get("status") in IN_FLIGHT or not _get(steps, s):
            return s
    return "document build"
