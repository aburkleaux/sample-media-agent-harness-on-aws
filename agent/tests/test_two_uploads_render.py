"""Two uploaded videos in one turn must not cross their renders (staging session fd4ce1bd).

The user attached `tidal_line_trailer_enUS.mp4` and `Netflix_Open_Content_Meridian.mp4` and asked
for technical metadata of both. The agent saved `output/meridian_metadata.json` with the `artifact`
tool, and the end-of-turn render opened a "Render Output" tab drawing TIDAL's video under
MERIDIAN's metadata: the render slot's sources were the union of both uploads, and the render took
the first as its canvas.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hooks import provider  # noqa: E402
from hooks.actions import _unambiguous_source_urls  # noqa: E402

CDN = "https://dexamplecdn002.cloudfront.net/artifacts/fd4ce1bd-256e-4dba-867c-4b25d33a99e0"
TIDAL = f"{CDN}/input/1791375006988-tidal_line_trailer_enUS.mp4"
MERIDIAN = f"{CDN}/input/1791375015168-Netflix_Open_Content_Meridian.mp4"


# ---------------------------------------------------------------- the probe answer is not a tab
def _artifact_result(url):
    return {"status": "success", "content": [{"text": json.dumps(
        {"status": "uploaded", "key": url.split(".net/", 1)[1], "url": url})}]}


def _ctx(result, name="artifact"):
    event = SimpleNamespace(tool_use={"name": name, "toolUseId": "t1", "input": {}}, result=result)
    return provider.ctx_after_tool_result(event)["result"]


def test_a_metadata_answer_the_agent_saved_is_not_a_render_candidate():
    for base in ("meridian_metadata.json", "tidal-probe.json", "metadata.json"):
        r = _ctx(_artifact_result(f"{CDN}/output/{base}"))
        assert r["new_artifacts"] == [], base


def test_other_agent_json_and_workflow_metadata_still_render():
    # A real JSON deliverable the agent saved.
    assert _ctx(_artifact_result(f"{CDN}/output/storyboard.json"))["new_artifacts"]
    # A workflow output outside the agent's output/ folder keeps the existing rules.
    assert not provider._is_probe_answer_json(
        "https://dexamplecdn002.cloudfront.net/micro-workflow/run/analysis_metadata.json")
    assert provider._is_probe_answer_json(f"{CDN}/output/meridian_metadata.json?X-Amz-Signature=1")


# ---------------------------------------------------------------- no guessed canvas
def test_one_source_video_is_kept_as_is():
    srcs = [TIDAL, f"{CDN}/input/tidal.vtt"]
    assert _unambiguous_source_urls([f"{CDN}/output/shots.json"], srcs) == srcs


def test_two_source_videos_keep_only_the_one_the_output_names():
    out = [f"{CDN}/output/meridian_shots.json"]
    assert _unambiguous_source_urls(out, [TIDAL, MERIDIAN]) == [MERIDIAN]


def test_two_source_videos_with_no_named_owner_keep_neither():
    out = [f"{CDN}/output/report.json"]
    assert _unambiguous_source_urls(out, [TIDAL, MERIDIAN, f"{CDN}/input/notes.txt"]) == [
        f"{CDN}/input/notes.txt"]


def test_presigned_copies_of_one_video_are_one_source():
    srcs = [TIDAL + "?X-Amz-Signature=a", TIDAL + "?X-Amz-Signature=b"]
    assert _unambiguous_source_urls([f"{CDN}/output/x.json"], srcs) == srcs
