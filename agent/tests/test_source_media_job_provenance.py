"""Design #1 end-to-end (unit level): the media a job runs on is captured on the durable
JobRecord at start and re-attached to the async COMPLETION render — consistently for
faces, shots, diarization and stems, with no per-tool logic.

Two seams are proven here:
  1. JobRecord.source_media round-trips through the DDB item mapping (durable bridge).
  2. provider.source_media_for_completion recovers it by job_id when the completion
     result carries no source of its own (the real failure: data-table with no canvas).
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from jobs.models import JobRecord  # noqa: E402
from jobs.registry import JobRegistry  # noqa: E402
import hooks.provider as provider  # noqa: E402

VIDEO = "s3://m2cv6-proxy/micro-workflow/media-processing/46a387c9/proxy/transcoded.mp4"


def _record(**kw) -> JobRecord:
    base = dict(job_id="arn:aws:states:us-west-2:1:execution:face-recognition:119db35e",
                actor_id="a1", session_id="s1", trio_id="face-recognition|recognize_faces",
                gateway_id="gw", status_tool="get_job_status", results_tool="",
                status="PENDING", start_at="2026-08-18T00:00:00Z")
    base.update(kw)
    return JobRecord(**base)


class TestSourceMediaPersistence:
    def test_round_trips_through_the_item(self):
        item = JobRegistry._to_item(_record(source_media=[VIDEO]))
        assert item["sourceMedia"] == [VIDEO]
        assert JobRegistry._from_item(item).source_media == [VIDEO]

    def test_absent_is_omitted_not_null(self):
        assert "sourceMedia" not in JobRegistry._to_item(_record())
        assert JobRegistry._from_item(JobRegistry._to_item(_record())).source_media == []


def _completion_result(job_id, **extra):
    # Shape of a get_job_status completion: a text block carrying the job's terminal
    # output. Crucially it does NOT echo the input video (the real m2c behaviour).
    import json
    payload = {"status": "SUCCEEDED", "job_id": job_id,
               "output": {"facegroup_key": "facegroup.json"}}
    payload.update(extra)
    return {"status": "success", "content": [{"text": json.dumps(payload)}]}


class TestCompletionAttach:
    def teardown_method(self):
        provider._job_source_media_lookup = None

    def test_recovers_recorded_source_by_job_id(self):
        # The registry recorded VIDEO for this job at start; the completion result has none.
        provider._job_source_media_lookup = lambda jid: [VIDEO] if "119db35e" in jid else []
        src = provider.source_media_for_completion(_completion_result("...face-recognition:119db35e"))
        assert src == [VIDEO]

    def test_same_rule_for_shots_and_diarization(self):
        # No per-tool logic: the same lookup path serves every analysis, keyed only by job_id.
        provider._job_source_media_lookup = lambda jid: [VIDEO]
        for jid in ("...shot-segmentation:aaa", "...speech-diarization:bbb", "...audio-stem:ccc"):
            assert provider.source_media_for_completion(_completion_result(jid)) == [VIDEO]

    def test_no_job_id_means_no_lookup(self):
        called = {"n": 0}
        def _spy(_jid):
            called["n"] += 1
            return [VIDEO]
        provider._job_source_media_lookup = _spy
        assert provider.source_media_for_completion({"status": "success", "content": [{"text": "{}"}]}) == []
        assert called["n"] == 0

    def test_job_id_resolved_from_status_call_input(self):
        # The result body may omit the id; the get_job_status CALL always carries it.
        provider._job_source_media_lookup = lambda jid: [VIDEO] if jid == "arn:exec:zzz" else []
        result = {"status": "success", "content": [{"text": '{"status": "SUCCEEDED"}'}]}
        tool_use = {"name": "face-recognition___get_job_status", "input": {"job_id": "arn:exec:zzz"}}
        assert provider.source_media_for_completion(result, tool_use) == [VIDEO]


class TestAggregateGroupSourceRecovery:
    """The real failure: the render_groups path uses group.source_urls (from role=source in
    the plan-result envelope), which is empty when resolve_node didn't surface it. Each group
    IS a job (node_id == execution ARN == job_id), so the group must recover its recorded
    source_media by node_id — else faces/shots/diarization render as a table with no canvas."""

    def teardown_method(self):
        provider._job_source_media_lookup = None

    def _face_plan_result(self, exec_arn):
        # A plan-result envelope: a face node with analysis artifacts and NO role=source.
        return {
            "schema": "plan-result/v1",
            "nodes": [{
                "node_id": exec_arn,
                "tool": "face-recognition___recognize_faces",
                "artifacts": [
                    {"url": f"s3://b/out/{exec_arn}/facegroup.json", "role": "analysis"},
                    {"url": f"s3://b/out/{exec_arn}/facemetadata.json", "role": "analysis"},
                ],
            }],
        }

    def test_group_recovers_source_by_node_id(self):
        exec_arn = "arn:aws:states:us-west-2:1:execution:face-recognition:894d044a"
        provider._job_source_media_lookup = lambda jid: [VIDEO] if jid == exec_arn else []
        agg = provider._aggregate_artifacts(self._face_plan_result(exec_arn))
        assert agg is not None
        group = agg["groups"][0]
        assert group["source_urls"] == [VIDEO], "group did not recover its recorded source"
        assert VIDEO in agg["source"]

    def test_group_with_its_own_source_is_untouched(self):
        exec_arn = "arn:exec:has-source"
        provider._job_source_media_lookup = lambda jid: ["s3://should/not/win.mp4"]
        payload = self._face_plan_result(exec_arn)
        payload["nodes"][0]["artifacts"].append({"url": VIDEO, "role": "source"})
        group = provider._aggregate_artifacts(payload)["groups"][0]
        assert VIDEO in group["source_urls"]
        assert "s3://should/not/win.mp4" not in group["source_urls"]

    def test_build_ctx_falls_back_to_recorded_source_when_result_has_none(self):
        # The integration point: a completion with renderable output but no per-result source
        # must surface the recorded source so the render gets its canvas.
        provider._job_source_media_lookup = lambda jid: [VIDEO]
        event = type("E", (), {
            "tool_use": {"name": "face-recognition___get_job_status", "toolUseId": "t1"},
            "result": _completion_result("...face-recognition:119db35e"),
        })()
        ctx = provider.ctx_after_tool_result(event)
        assert VIDEO in ctx["result"]["source_urls"]

    def test_build_ctx_prefers_the_results_own_source_over_the_lookup(self):
        # When the result DOES carry a source, the recorded one must not override it.
        provider._job_source_media_lookup = lambda jid: ["s3://other/should-not-win.mp4"]
        res = _completion_result("...face-recognition:119db35e", input_s3_uri=VIDEO)
        event = type("E", (), {
            "tool_use": {"name": "face-recognition___get_job_status", "toolUseId": "t2"},
            "result": res,
        })()
        ctx = provider.ctx_after_tool_result(event)
        assert VIDEO in ctx["result"]["source_urls"]
        assert "s3://other/should-not-win.mp4" not in ctx["result"]["source_urls"]
