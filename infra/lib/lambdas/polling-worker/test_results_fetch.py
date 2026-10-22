"""A fal queue job reports COMPLETED on its status tool but the deliverable (video.url) is in a
separate get_job_result payload. reconcile must fetch it so artifactUrls is captured and the
video auto-renders. M2C trios (no resultsTool) must be unaffected.
"""
import sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import handler as h


class _Reg:
    def __init__(self): self.terminal = None
    def set_terminal(self, job_id, **kw): self.terminal = {"job_id": job_id, **kw}
    def set_active_status(self, *a, **k): pass
    def bump_error(self, *a, **k): return 1


def _fal_job():
    return {"jobId": "019fee73", "actorId": "a", "gatewayId": "harness-user-mcp",
            "statusTool": "usr___check_job", "resultsTool": "usr___get_job_result",
            "resultsGatewayId": "harness-user-mcp", "endpointId": "bytedance/seedance-2.5/image-to-video"}


def test_terminal_fetches_result_and_captures_video_url():
    reg = _Reg()
    status_resp = {"status": "COMPLETED"}  # status body has NO video url
    result_resp = {"video": {"url": "https://v3b.fal.media/files/b/x/y_video.mp4",
                             "content_type": "video/mp4"}, "seed": 123}
    h.reconcile(_fal_job(), status_resp, registry=reg, fetch_results=lambda: result_resp)
    assert reg.terminal["status"] == "SUCCEEDED"
    assert reg.terminal["has_renderable_artifacts"] is True
    assert any("y_video.mp4" in u for u in reg.terminal["artifact_urls"])


def test_result_fetch_failure_falls_back_to_status_body():
    reg = _Reg()
    def boom(): raise RuntimeError("fal 500")
    h.reconcile(_fal_job(), {"status": "COMPLETED"}, registry=reg, fetch_results=boom)
    assert reg.terminal["status"] == "SUCCEEDED"  # terminal still written
    assert reg.terminal["artifact_urls"] == []    # no crash, just no artifacts


def test_m2c_job_without_results_tool_never_fetches():
    reg = _Reg()
    calls = []
    job = {"jobId": "arn:...:exec", "actorId": "a", "gatewayId": "g",
           "statusTool": "svc___get_job_status", "resultsTool": "", "endpointId": ""}
    h.reconcile(job, {"status": "SUCCEEDED", "outputs": [{"url": "s3://b/out.wav", "mime": "audio/wav"}]},
                registry=reg, fetch_results=lambda: calls.append(1) or {"x": 1})
    assert calls == []  # resultsTool empty -> no fetch
    assert reg.terminal["status"] == "SUCCEEDED"
