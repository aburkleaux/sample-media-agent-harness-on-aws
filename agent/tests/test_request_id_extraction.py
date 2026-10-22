"""fal queue tools (submit_job) return the id as request_id, not job_id.

Without accepting request_id, a detected fal start tool extracted no id and the job was never
registered -> invisible in the Jobs panel (the "I don't see this job" report).
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jobs.interceptor import _extract_job_id
from jobs.strands_tool_wrap import _extract_job_id_from_strands_result as sx


def test_mcp_extracts_request_id_top_level_and_structured_and_text():
    assert _extract_job_id({"request_id": "019fee1a"}) == "019fee1a"
    assert _extract_job_id({"structuredContent": {"request_id": "019fee1a"}}) == "019fee1a"
    assert _extract_job_id(
        {"content": [{"type": "text", "text": json.dumps({"request_id": "019fee1a"})}]}
    ) == "019fee1a"


def test_job_id_is_preferred_over_request_id():
    assert _extract_job_id({"job_id": "J", "request_id": "R"}) == "J"


def test_strands_extracts_request_id_json_and_text():
    assert sx({"content": [{"json": {"request_id": "019fee1a"}}]}) == "019fee1a"
    assert sx({"content": [{"text": json.dumps({"request_id": "019fee1a"})}]}) == "019fee1a"


def test_none_when_no_id_present():
    assert _extract_job_id({"foo": "bar"}) is None
    assert sx({"content": [{"json": {"foo": "bar"}}]}) is None


def test_endpoint_id_is_the_queue_app_from_status_url_not_the_submit_model():
    # REGRESSION: fal submit returns endpoint_id = full model path
    # (bytedance/seedance-2.5/text-to-video), but the QUEUE lives under the base app
    # (queue.fal.run/bytedance/seedance-2.5/requests/...). Polling with the full path 405s.
    # The status_url is authoritative and must win.
    from jobs.strands_tool_wrap import (_endpoint_id_from_status_url as ep,
                                        _extract_endpoint_id_from_strands_result as ex)
    u = ("https://queue.fal.run/bytedance/seedance-2.5/requests/"
         "019fee98-2aa3-75d1-897d-7c9efe8753b5/status")
    assert ep(u) == "bytedance/seedance-2.5"
    # real submit response shape: explicit full-path endpoint_id + the base-app status_url
    resp = {"content": [{"json": {"request_id": "r",
                                  "endpoint_id": "bytedance/seedance-2.5/text-to-video",
                                  "status_url": u}}]}
    assert ex(resp) == "bytedance/seedance-2.5"   # base app, NOT the 405-causing full path
    # text-block form
    assert ex({"content": [{"text": json.dumps({"request_id": "r", "status_url": u})}]}) == "bytedance/seedance-2.5"
    # explicit field used ONLY when there is no status_url
    assert ex({"content": [{"json": {"endpoint_id": "fal-ai/x"}}]}) == "fal-ai/x"
    assert ex({"content": [{"json": {"job_id": "J"}}]}) is None
    assert ep("https://example.com/not-a-queue") is None
