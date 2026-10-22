"""A code cell's writes to the session output folders are reported in its own result (session e167d3f2).

The cell uploaded its ffmpeg trim with an inline boto3 call that printed only ``ok``, so no tool result carried a
url, the render hook had nothing to open, and the user had to ask for the tab.
"""
import asyncio

import pytest
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool

import sandbox_outputs as so
import tool_input_normalize as tin
from hooks.provider import extract_urls

SID = "e167d3f2-2a2e-4429-a84f-b503f9ff40de"
PID = "paa97084ce1224386a60336bc3f52dddb"
ROOT = f"projects/{PID}/sessions/{SID}/output/"


class _FakeS3:
    def __init__(self):
        self.objects: dict[str, str] = {}
        self.fail = False

    def list_objects_v2(self, Bucket, Prefix, **kw):
        if self.fail:
            raise RuntimeError("AccessDenied")
        return {"Contents": [{"Key": k, "ETag": e} for k, e in sorted(self.objects.items()) if k.startswith(Prefix)]}


class _CellTool(AgentTool):
    """Stands in for code_interpreter: running the cell writes `writes` into the fake bucket."""

    def __init__(self, s3, writes, status="success"):
        super().__init__()
        self.s3, self.writes, self.status = s3, writes, status

    tool_name = property(lambda self: "code_interpreter")
    tool_spec = property(lambda self: {"name": "code_interpreter", "inputSchema": {}})
    tool_type = property(lambda self: "function")

    def get_display_properties(self):
        return {}

    async def stream(self, tool_use, invocation_state, **kwargs):
        self.s3.objects.update(self.writes)
        yield ToolResultEvent({"toolUseId": "t1", "status": self.status, "content": [{"text": "ok"}]})


@pytest.fixture
def s3(monkeypatch):
    fake = _FakeS3()
    monkeypatch.setenv("SKILLS_S3_BUCKET", "skills-bucket")
    monkeypatch.setenv("ARTIFACTS_DOMAIN", "cdn.example.net")
    monkeypatch.setattr(so, "_s3", lambda: fake)
    monkeypatch.setattr(so, "output_roots", lambda: [f"artifacts/{SID}/output/", ROOT])
    monkeypatch.setattr(tin.NormalizedCodeInterpreterTool, "_prepare", lambda self, tu: tu)
    monkeypatch.setattr(tin.NormalizedCodeInterpreterTool, "_stage_skill_scripts", lambda self, tu: tu)
    return fake


def _run(tool, action_type="executeCommand"):
    tu = {"toolUseId": "t1", "name": "code_interpreter",
          "input": {"code_interpreter_input": {"action": {"type": action_type, "command": "python upload.py"}}}}

    async def go():
        return [ev async for ev in tin.NormalizedCodeInterpreterTool(tool).stream(tu, {})]
    return asyncio.run(go())[-1].tool_result


def test_new_output_is_reported_as_a_url_the_render_hook_finds(s3):
    s3.objects[ROOT + "old.mp4"] = '"a"'
    result = _run(_CellTool(s3, {ROOT + "Meridian_clip_0014_151939.mp4": '"b"'}))
    text = result["content"][-1]["text"]
    assert f"https://cdn.example.net/{ROOT}Meridian_clip_0014_151939.mp4" in text
    assert "old.mp4" not in text  # already there before the cell ran
    assert f"https://cdn.example.net/{ROOT}Meridian_clip_0014_151939.mp4" in extract_urls(result)
    assert result["content"][0] == {"text": "ok"}  # the tool's own output is kept, first


def test_a_rewritten_file_counts(s3):
    s3.objects[ROOT + "clip.mp4"] = '"a"'
    result = _run(_CellTool(s3, {ROOT + "clip.mp4": '"b"'}))
    assert "clip.mp4" in result["content"][-1]["text"]


def test_nothing_written_leaves_the_result_untouched(s3):
    assert _run(_CellTool(s3, {}))["content"] == [{"text": "ok"}]


def test_failed_cell_is_not_annotated(s3):
    assert _run(_CellTool(s3, {ROOT + "x.mp4": '"b"'}, status="error"))["content"] == [{"text": "ok"}]


def test_listing_failure_leaves_the_result_untouched(s3):
    s3.fail = True
    assert _run(_CellTool(s3, {ROOT + "x.mp4": '"b"'}))["content"] == [{"text": "ok"}]


def test_non_code_actions_are_not_watched(s3):
    assert _run(_CellTool(s3, {ROOT + "x.mp4": '"b"'}), action_type="listLocalSessions")["content"] == [{"text": "ok"}]


def test_no_session_bound_means_no_listing(s3, monkeypatch):
    monkeypatch.setattr(so, "output_roots", lambda: [])
    assert _run(_CellTool(s3, {ROOT + "x.mp4": '"b"'}))["content"] == [{"text": "ok"}]


def test_report_is_capped(s3):
    writes = {f"{ROOT}f{n:03}.png": '"b"' for n in range(so.MAX_REPORTED + 5)}
    text = _run(_CellTool(s3, writes))["content"][-1]["text"]
    assert text.count("https://") == so.MAX_REPORTED and "and 5 more" in text


def test_inline_outputs_are_reported_for_the_answer_but_never_auto_rendered(s3):
    from hooks.provider import is_inline_only, new_artifact_urls
    key = ROOT + "inline/frames_strip.jpg"
    text = _run(_CellTool(s3, {key: '"b"'}))["content"][-1]["text"]
    url = f"https://cdn.example.net/{key}"
    assert url in text and "show it in your reply" in text
    assert is_inline_only(url) and is_inline_only(url + "?X-Amz-Signature=abc")
    other = f"https://cdn.example.net/{ROOT}clip.mp4"
    assert new_artifact_urls([url, other], {url: "image/jpeg", other: "video/mp4"}) == [other]
