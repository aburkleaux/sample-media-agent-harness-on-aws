"""Job_Detector tests for the M2C media-workflow shape (shared get_job_status,
no *_results) plus a legacy start/status/results regression check."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jobs.detector import ToolDescriptor, classify_and_group  # noqa: E402


def _t(name: str) -> ToolDescriptor:
    return ToolDescriptor(name=name)


def test_m2c_shared_status_shape_forms_start_trios():
    tools = [
        _t("speech-diarization___diarize_speech"),
        _t("speech-diarization___get_job_status"),
        _t("speech-diarization___get_tool_version"),
        _t("audio-stem___separate_audio_stems"),
        _t("audio-stem___get_job_status"),
        _t("media-processing___process_media"),
        _t("media-processing___get_job_status"),
    ]
    res = classify_and_group(tools, gateway_id="media-workflow-gateway")
    starts = {tr.start_tool: tr for tr in res.trios}

    # Each async start tool is detected + points at its target's shared status.
    for start, status in [
        ("speech-diarization___diarize_speech", "speech-diarization___get_job_status"),
        ("audio-stem___separate_audio_stems", "audio-stem___get_job_status"),
        ("media-processing___process_media", "media-processing___get_job_status"),
    ]:
        assert start in starts, f"{start} not detected as a start tool"
        assert starts[start].status_tool == status
        assert starts[start].results_tool == ""  # no results gate installed
        assert res.find_trio_with_start(start) is starts[start]

    # get_job_status / get_tool_version are NOT start tools.
    assert "speech-diarization___get_job_status" not in starts
    assert "speech-diarization___get_tool_version" not in starts
    # No results tool → nothing registered as a results gate.
    assert res.find_trio_with_results("speech-diarization___get_job_status") is None


def test_legacy_convention_trio_unaffected():
    """The classic start_*/*_status/*_results trio still forms; a gateway with
    no get_job_status yields no M2C trios (no false positives)."""
    tools = [
        _t("gw___start_widget"),
        _t("gw___get_widget_status"),
        _t("gw___get_widget_results"),
    ]
    res = classify_and_group(tools, gateway_id="gw")
    trio = next((t for t in res.trios if t.start_tool == "gw___start_widget"), None)
    assert trio is not None
    assert trio.status_tool == "gw___get_widget_status"
    assert trio.results_tool == "gw___get_widget_results"  # real results gate


def test_no_status_tool_yields_no_m2c_trio():
    tools = [_t("svc___do_thing"), _t("svc___get_tool_version")]
    res = classify_and_group(tools, gateway_id="svc")
    assert res.trios == []


def test_describe_tool_excluded_from_start_tools():
    """describe_tool (renamed from get_tool_version) must not be detected as a start tool."""
    tools = [
        _t("audio-stem___separate_audio_stems"),
        _t("audio-stem___get_job_status"),
        _t("audio-stem___describe_tool"),
    ]
    res = classify_and_group(tools, gateway_id="media-workflow-gateway")
    starts = {tr.start_tool for tr in res.trios}
    assert "audio-stem___separate_audio_stems" in starts
    assert "audio-stem___describe_tool" not in starts


def test_shot_segmentation_trio_detected():
    """shot-segmentation___segment_shots forms a trio with shared get_job_status."""
    tools = [
        _t("shot-segmentation___segment_shots"),
        _t("shot-segmentation___get_job_status"),
        _t("shot-segmentation___describe_tool"),
    ]
    res = classify_and_group(tools, gateway_id="media-workflow-gateway")
    starts = {tr.start_tool: tr for tr in res.trios}
    assert "shot-segmentation___segment_shots" in starts
    assert starts["shot-segmentation___segment_shots"].status_tool == "shot-segmentation___get_job_status"
    assert "shot-segmentation___describe_tool" not in starts


def test_media_outputs_target_yields_no_trio():
    """media-outputs target has no get_job_status — must produce no trios."""
    tools = [
        _t("media-outputs___get_download_url"),
    ]
    res = classify_and_group(tools, gateway_id="media-workflow-gateway")
    assert res.trios == []


def test_full_gateway_toolset():
    """All 5 targets together: 4 trios (stem, diarize, media, shot), media-outputs skipped."""
    tools = [
        _t("audio-stem___separate_audio_stems"),
        _t("audio-stem___get_job_status"),
        _t("audio-stem___describe_tool"),
        _t("speech-diarization___diarize_speech"),
        _t("speech-diarization___get_job_status"),
        _t("speech-diarization___describe_tool"),
        _t("media-processing___process_media"),
        _t("media-processing___get_job_status"),
        _t("media-processing___describe_tool"),
        _t("shot-segmentation___segment_shots"),
        _t("shot-segmentation___get_job_status"),
        _t("shot-segmentation___describe_tool"),
        _t("media-outputs___get_download_url"),
    ]
    res = classify_and_group(tools, gateway_id="media-workflow-gateway")
    starts = {tr.start_tool for tr in res.trios}
    assert starts == {
        "audio-stem___separate_audio_stems",
        "speech-diarization___diarize_speech",
        "media-processing___process_media",
        "shot-segmentation___segment_shots",
    }
    assert len(res.trios) == 4
