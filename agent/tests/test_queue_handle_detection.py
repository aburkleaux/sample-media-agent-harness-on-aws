"""Queue-submission detection, pinned to REAL fal responses.

fal names these fields inconsistently. All four shapes below were observed in live
plan manifests, two of them in the SAME plan:

    {"request_id": ..., "model": "fal-ai/stable-audio-25/text-to-audio", "status_url": ...}
    {"request_id": ..., "model": "bytedance/seedance-2.0/mini/text-to-video"}   # no status_url
    {"request_id": ..., "endpoint_id": "fal-ai/stable-audio-3/small/music/text-to-audio", ...}
    {"job_id": ...,     "model_id": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video", ...}

The endpoint is required by check_job -- "Missing required field(s): 'endpoint_id'" --
so failing to capture it means the job registers and can never be polled.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import task_agent as t  # noqa: E402


def test_model_field_is_accepted():
    h = t._extract_queue_handle(
        {"request_id": "r1", "model": "fal-ai/stable-audio-25/text-to-audio",
         "status_url": "https://queue.fal.run/x/status"}
    )
    assert h["job_id"] == "r1"
    # Truncated to the queue app id -- see test_queue_app_id_* below.
    assert h["endpoint_id"] == "fal-ai/stable-audio-25"


def test_status_url_is_not_required():
    """seedance returns request_id + model and NO status_url.

    Requiring status_url meant that video's job was never registered at all, so it
    could never be polled or notified. The ENDPOINT is the discriminator.
    """
    h = t._extract_queue_handle(
        {"request_id": "r2", "model": "bytedance/seedance-2.0/mini/text-to-video"}
    )
    assert h is not None
    assert h["endpoint_id"] == "bytedance/seedance-2.0"
    assert h["status_url"] == ""


def test_endpoint_id_and_model_id_still_work():
    for key in ("endpoint_id", "model_id"):
        h = t._extract_queue_handle({"request_id": "r", key: "fal-ai/x/y", "status_url": "u"})
        # "u" is not a queue url, so the model path is truncated to its app id.
        assert h["endpoint_id"] == "fal-ai/x", key


def test_job_id_is_accepted_as_the_id():
    h = t._extract_queue_handle({"job_id": "j1", "model": "fal-ai/x"})
    assert h["job_id"] == "j1"


def test_an_id_without_an_endpoint_is_not_a_handle():
    # Ambiguous: any identifier could look like this. Registering it would create a
    # row that can never be polled, which is worse than not registering.
    assert t._extract_queue_handle({"request_id": "r"}) is None
    assert t._extract_queue_handle({"job_id": "j", "status_url": "u"}) is None


def test_a_finished_result_is_not_a_handle():
    assert t._extract_queue_handle({"url": "https://cdn/final.mp4"}) is None
    assert t._extract_queue_handle({"images": [{"url": "https://cdn/a.png"}]}) is None


def test_a_blocked_node_value_is_not_a_handle():
    assert t._extract_queue_handle({"error": "missing_dependency", "reason": "..."}) is None


def test_non_dicts_are_safe():
    for value in (None, "s3://b/k", 42, [], ["r1"]):
        assert t._extract_queue_handle(value) is None


def test_empty_endpoint_is_rejected():
    assert t._extract_queue_handle({"request_id": "r", "model": ""}) is None
    assert t._extract_queue_handle({"request_id": "r", "model": None}) is None


def test_queue_app_id_comes_from_status_url_when_present():
    """The queue addresses only the first two segments, not the full model path.

    fal's own status_url shows the expected form, and sending the full model path
    produced "Status check failed (405)" -- the queue built a url that does not exist.
    """
    h = t._extract_queue_handle({
        "request_id": "r",
        "model": "fal-ai/stable-audio-3/small/music/text-to-audio",
        "status_url": "https://queue.fal.run/fal-ai/stable-audio-3/requests/019f/status",
    })
    assert h["endpoint_id"] == "fal-ai/stable-audio-3"


def test_queue_app_id_falls_back_to_first_two_segments():
    # seedance supplies no status_url, so the model path must be truncated the same way.
    h = t._extract_queue_handle(
        {"request_id": "r", "model": "bytedance/seedance-2.0/text-to-video"}
    )
    assert h["endpoint_id"] == "bytedance/seedance-2.0"


def test_queue_app_id_leaves_a_two_segment_model_alone():
    h = t._extract_queue_handle({"request_id": "r", "model": "fal-ai/flux"})
    assert h["endpoint_id"] == "fal-ai/flux"


def test_queue_app_id_handles_a_single_segment_model():
    h = t._extract_queue_handle({"request_id": "r", "model": "someapp"})
    assert h["endpoint_id"] == "someapp"


def test_status_url_alone_is_enough_to_identify_the_endpoint():
    """stable-audio returned NO model field -- only ids and urls.

    Requiring an explicit endpoint field rejected it, so its job was never registered or
    polled, and the merge node was left holding an unresolvable handle:
    "soundtrack_job output has no resolved audio URL". The status_url names the endpoint
    perfectly well; either source is sufficient.
    """
    h = t._extract_queue_handle({
        "job_id": "019fdf46",
        "request_id": "019fdf46",
        "status_url": "https://queue.fal.run/fal-ai/stable-audio-25/requests/019fdf46/status",
        "response_url": "https://queue.fal.run/fal-ai/stable-audio-25/requests/019fdf46",
    })
    assert h is not None
    assert h["endpoint_id"] == "fal-ai/stable-audio-25"


def test_status_url_wins_when_both_are_present():
    h = t._extract_queue_handle({
        "request_id": "r", "model": "fal-ai/x/y",
        "status_url": "https://queue.fal.run/fal-ai/zz/requests/1/status",
    })
    assert h["endpoint_id"] == "fal-ai/zz"


def test_neither_source_means_no_handle():
    # Registering without an endpoint would create a row the poller can never use.
    assert t._extract_queue_handle({"request_id": "r"}) is None
    assert t._extract_queue_handle({"request_id": "r", "status_url": "not-a-queue-url"}) is None
