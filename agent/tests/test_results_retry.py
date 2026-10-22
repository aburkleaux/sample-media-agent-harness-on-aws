"""Results-tool transient-404 retry (#3, session a3099b52).

A nested fal model (fal-ai/minimax/h3-max/image-to-video) reaches queue_status COMPLETED, but
`get_job_result` intermittently returns HTTP 404 on the result body (eventual consistency between
COMPLETED status and result availability). The node used to give up → no video URL → the plan
failed. JobTrackingMCPAgentTool now retries the (idempotent) result fetch with bounded backoff.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strands.types._events import ToolResultEvent  # noqa: E402
import jobs.strands_tool_wrap as stw  # noqa: E402


def _r(status: str, text: str) -> dict:
    return {"status": status, "content": [{"text": text}], "toolUseId": "t1"}


# --- classifier -----------------------------------------------------------------------------

def test_transient_missing_result_detects_404():
    assert stw._is_transient_missing_result(_r("error", "HTTP 404 on result body")) is True


def test_transient_missing_result_not_found_error():
    assert stw._is_transient_missing_result(_r("error", "result body not found")) is True


def test_transient_missing_result_false_on_success():
    assert stw._is_transient_missing_result(_r("success", "video https://cdn/x.mp4")) is False


# --- retry loop -----------------------------------------------------------------------------

class _FakeResults:
    """Minimal stand-in exposing exactly what _results_stream_with_retry uses."""
    tool_name = "usr-x___get_job_result"

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def _resilient_base_stream(self, tool_use, invocation_state, kwargs):
        r = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        yield ToolResultEvent(r)

    async def _mirror_result_media(self, ev):
        return ev


async def _noop_sleep(_d):
    return None


def _collect(fake, monkeypatch):
    monkeypatch.setattr(stw.asyncio, "sleep", _noop_sleep)  # no real backoff in tests

    async def run():
        out = []
        async for ev in stw.JobTrackingMCPAgentTool._results_stream_with_retry(
            fake, {"toolUseId": "t1", "input": {"request_id": "req-1"}}, {}, {}
        ):
            out.append(ev)
        return out

    return asyncio.run(run())


def test_retry_recovers_from_transient_404(monkeypatch):
    fake = _FakeResults([_r("error", "HTTP 404"), _r("error", "HTTP 404"),
                         _r("success", "video https://cdn/v.mp4")])
    events = _collect(fake, monkeypatch)
    assert fake.calls == 3                                   # 404, 404, then success
    assert events[-1].tool_result["status"] == "success"


def test_retry_gives_up_after_bounded_attempts(monkeypatch):
    fake = _FakeResults([_r("error", "HTTP 404")] * 10)      # always 404
    events = _collect(fake, monkeypatch)
    assert fake.calls == 4                                   # 1 initial + 3 retries, then surface
    assert events[-1].tool_result["status"] == "error"


def test_no_retry_on_first_success(monkeypatch):
    fake = _FakeResults([_r("success", "video https://cdn/v.mp4")])
    events = _collect(fake, monkeypatch)
    assert fake.calls == 1                                   # success on first try → no retry
    assert events[-1].tool_result["status"] == "success"
