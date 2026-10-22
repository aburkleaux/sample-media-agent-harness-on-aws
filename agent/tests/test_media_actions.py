"""Direct timeline actions (media_actions.py): deterministic ffmpeg in a warm code-interpreter sandbox, no model."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import media_actions as ma  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src"
PID = "p" + "a" * 32
SID = "11111111-2222-4333-8444-555555555555"


def _raw(op="frame", **kw):
    base = {"action_id": "act-1234abcd", "op": op,
            "source": {"url": "https://bucket.s3.amazonaws.com/k/src.mp4?X-Amz-Signature=x",
                       "s3uri": "s3://bucket/k/src.mp4", "name": "trailer.mp4", "asset_id": "video:1"}}
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    ma._reset_for_tests()
    monkeypatch.setenv("SKILLS_S3_BUCKET", "skills-bucket")
    monkeypatch.setenv("ARTIFACTS_DOMAIN", "cdn.example")
    yield
    ma._reset_for_tests()


# ------------------------------------------------------------------ parsing + caps
def test_a_long_clip_is_allowed():  # a clip is a stream copy, so its length costs nothing
    assert ma.parse_action(_raw("clip", ranges=[{"inMs": 0, "outMs": 600_000}])).ranges_ms == [(0, 600_000)]


def test_parse_frame_clip_crop():
    f = ma.parse_action(_raw("frame", points=[23000, 26500]))
    assert f.points_ms == [23000, 26500]
    c = ma.parse_action(_raw("clip", ranges=[{"inMs": 23000, "outMs": 30000}]))
    assert c.ranges_ms == [(23000, 30000)]
    k = ma.parse_action(_raw("crop", box={"x": 0.2, "y": 0, "w": 0.5625, "h": 1}))
    assert k.box == (0.2, 0.0, 0.5625, 1.0)


@pytest.mark.parametrize("raw,why", [
    (_raw("crop", box={"x": 0, "y": 0, "w": 0.5, "h": 1}, ranges=[{"inMs": 0, "outMs": 121_000}]), "120"),  # a long CROP re-encodes: agent
    (_raw("clip", ranges=[]), "at least one range"),
    (_raw("frame", points=[]), "points"),
    (_raw("crop", box={"x": 0, "y": 0, "w": 1.5, "h": 1}), "fractions"),
    (_raw("trim"), "op must be"),
    ({**_raw("frame", points=[1]), "source": {"url": "http://x", "s3uri": "s3://b/k"}}, "https"),
])
def test_parse_refuses(raw, why):
    with pytest.raises(ma.MediaActionError, match=why):
        ma.parse_action(raw)


def test_plan_outputs_are_deterministic_and_under_the_session_output_prefix():
    act = ma.parse_action(_raw("frame", points=[23000, 26500]))
    jobs = ma.plan_outputs(act, f"projects/{PID}/sessions/{SID}/output/")
    assert [j["key"] for j in jobs] == [
        f"projects/{PID}/sessions/{SID}/output/trailer_frame_0023_000_34abcd0.jpg",
        f"projects/{PID}/sessions/{SID}/output/trailer_frame_0026_500_34abcd1.jpg",
    ]
    clip = ma.plan_outputs(ma.parse_action(_raw("clip", ranges=[{"inMs": 23000, "outMs": 30000}])), "p/")
    assert clip == [{"kind": "video", "in": 23.0, "out": 30.0, "key": "p/trailer_clip_0023-0030_34abcd0.mp4"}]


def test_sandbox_program_injects_params_verbatim_and_disables_the_script_cli():
    act = ma.parse_action(_raw("crop", box={"x": 0.2, "y": 0, "w": 0.5625, "h": 1}))
    code = ma.build_sandbox_code("from __future__ import annotations\nif __name__ == '__main__':\n    raise SystemExit(1)\n",
                                 act, ma.plan_outputs(act, "p/"), bucket="b", domain="d", region="us-west-2")
    assert "if False:" in code and "__name__ == '__main__'" not in code
    assert '[0.2, 0.0, 0.5625, 1.0]' in code
    assert "-preset" in code and "superfast" in code  # a crop re-encodes, fast


# ------------------------------------------------------------------ warm sandbox
class FakeCI:
    started = 0

    def __init__(self, fail_first=False):
        FakeCI.started += 1
        self.codes: list[str] = []
        self.invokes: list[str] = []
        self.fail_first = fail_first
        self.stopped = False

    def invoke(self, name, args):
        self.invokes.append(args["code"])

    def execute_code(self, code):
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("session expired")
        self.codes.append(code)
        # what the runner prints: the outputs it uploaded
        keys = eval(code.split("_JOBS = ", 1)[1].split("\n", 1)[0], {"_json": json})  # noqa: S307 - our own header
        out = {"ok": True, "outputs": [{"key": j["key"], "kind": j["kind"]} for j in keys],
               "sourceCached": len(self.codes) > 1}
        return {"stream": [{"result": {"structuredContent": {"stdout": ma._SENTINEL + json.dumps(out)}}}]}

    def stop(self):
        self.stopped = True


def test_one_warm_sandbox_per_session_is_reused(monkeypatch):
    FakeCI.started = 0
    made: list[FakeCI] = []
    monkeypatch.setattr(ma, "_new_code_interpreter", lambda: made.append(FakeCI()) or made[-1])
    monkeypatch.setattr(ma, "_script_source", lambda: "from __future__ import annotations\n")
    a = ma.run_action(ma.parse_action(_raw("frame", points=[1000])), session_id=SID, project_id=PID)
    b = ma.run_action(ma.parse_action(_raw("clip", ranges=[{"inMs": 0, "outMs": 5000}])), session_id=SID, project_id=PID)
    assert a["ok"] and b["ok"]
    assert FakeCI.started == 1 and len(made[0].codes) == 2   # the second action reused the sandbox
    assert b["sourceCached"] is True                         # ...and its downloaded source
    other = ma.run_action(ma.parse_action(_raw("frame", points=[1])), session_id="22222222-2222-4333-8444-555555555555",
                          project_id=PID)
    assert other["ok"] and FakeCI.started == 2               # another chat: its own sandbox
    assert a["outputs"][0]["s3uri"].startswith(f"s3://skills-bucket/projects/{PID}/sessions/{SID}/output/") or \
        a["outputs"][0]["s3uri"].startswith("s3://skills-bucket/artifacts/")
    assert a["outputs"][0]["url"].startswith("https://cdn.example/")


def test_a_dead_sandbox_is_replaced_once(monkeypatch):
    FakeCI.started = 0
    made: list[FakeCI] = []

    def new():
        made.append(FakeCI(fail_first=not made))
        return made[-1]

    monkeypatch.setattr(ma, "_new_code_interpreter", new)
    monkeypatch.setattr(ma, "_script_source", lambda: "")
    r = ma.run_action(ma.parse_action(_raw("frame", points=[1])), session_id=SID, project_id=PID)
    assert r["ok"] and FakeCI.started == 2 and made[0].stopped


def test_scoped_credentials_are_injected_once_per_refresh_window(monkeypatch):
    calls = []
    monkeypatch.setattr(ma, "_new_code_interpreter", lambda: FakeCI())
    monkeypatch.setattr(ma, "_script_source", lambda: "")
    monkeypatch.setattr(ma, "_inject_credentials", lambda ci, pid, sid: calls.append((pid, sid)))
    for _ in range(3):
        ma.run_action(ma.parse_action(_raw("frame", points=[1])), session_id=SID, project_id=PID)
    assert calls == [(PID, SID)]


def test_ffmpeg_is_provisioned_by_the_scripts_cached_lookup():
    # The runner calls the bundled script's _ensure_ffmpeg_ffprobe, which looks for the binary it already
    # unpacked under the sandbox temp dir before downloading anything: once per warm sandbox.
    assert "_ensure_ffmpeg_ffprobe()" in ma._RUNNER
    script = (SRC.parent / "skills" / "video-processing" / "scripts" / "crop_video.py").read_text()
    body = script.split("def _ensure_ffmpeg_ffprobe", 1)[1].split("\ndef ", 1)[0]
    assert body.index("ff_path = _find(\"ffmpeg\")") < body.index("urllib.request.urlretrieve")


def test_a_failure_is_one_result_frame_not_an_exception(monkeypatch):
    class Boom(FakeCI):
        def execute_code(self, code):
            out = {"error": "ffmpeg failed: moov atom not found"}
            return {"stream": [{"result": {"structuredContent": {"stdout": ma._SENTINEL + json.dumps(out)}}}]}

    monkeypatch.setattr(ma, "_new_code_interpreter", lambda: Boom())
    monkeypatch.setattr(ma, "_script_source", lambda: "")
    r = ma.run_action(ma.parse_action(_raw("frame", points=[1])), session_id=SID, project_id=PID)
    assert r["ok"] is False and "moov atom" in r["error"] and r["type"] == ma.RESULT_TYPE


# ------------------------------------------------------------------ the entrypoint: no model
def _collect(gen):
    async def go():
        return [f async for f in gen]
    return asyncio.run(go())


def test_handle_refuses_without_a_project(monkeypatch):
    frames = _collect(ma.handle({"__media_action": _raw("frame", points=[1]), "session_id": SID}))
    assert json.loads(frames[0])["ok"] is False


def test_handle_never_builds_an_agent_or_a_model(monkeypatch):
    import strands

    def explode(*a, **k):
        raise AssertionError("a direct media action must not build an Agent or a model")

    monkeypatch.setattr(strands, "Agent", explode)
    import model.factory as factory
    monkeypatch.setattr(factory, "build_model", explode)
    monkeypatch.setattr(ma, "_new_code_interpreter", lambda: FakeCI())
    monkeypatch.setattr(ma, "_script_source", lambda: "")
    frames = _collect(ma.handle({"__media_action": _raw("frame", points=[1000]), "session_id": SID,
                                 "active_project_id": PID, "__actor_id": "u1"}))
    out = json.loads(frames[0])
    assert len(frames) == 1 and out["ok"] is True and out["outputs"][0]["kind"] == "image"


def test_the_entrypoint_branches_before_any_agent_setup():
    src = (SRC / "main.py").read_text()
    body = src[src.index("async def invoke(payload, context):"):]
    branch = body.index('payload.get("__media_action")')
    for later in ("_TurnStartStep(", "_init_agent_subsystems(", "_ensure_gateway_connected("):
        assert branch < body.index(later), later
    mod = (SRC / "media_actions.py").read_text()
    assert "Agent(" not in mod.replace("never builds a Strands Agent", "") and "build_model" not in mod


def test_a_plain_clip_is_a_stream_copy_and_a_crop_reencodes_fast():
    from media_actions import _RUNNER
    assert '"-c", "copy"' in _RUNNER and "_copyable(_ffprobe, _local)" in _RUNNER
    assert '"-preset", "superfast"' in _RUNNER and '"veryfast"' not in _RUNNER


def test_a_stream_copy_clip_starts_on_the_exact_marked_frame(tmp_path):
    # The claim behind the copy path: -ss before -i lands on the keyframe BEFORE `in`, and the MP4 edit list
    # ffmpeg writes hides that pre-roll, so the first shown frame is the source frame at `in`.
    import shutil
    import subprocess

    import pytest

    ff = shutil.which("ffmpeg")
    if not ff:
        pytest.skip("no ffmpeg on this machine")
    src, clip = tmp_path / "src.mp4", tmp_path / "clip.mp4"
    run = lambda *a: subprocess.run([ff, "-v", "error", "-y", *a], check=True)  # noqa: E731
    # 2 s GOP (keyframes at 0, 2, 4…); a counter makes every frame distinct.
    run("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=8", "-c:v", "libx264", "-g", "48",
        "-keyint_min", "48", "-sc_threshold", "0", str(src))
    run("-ss", "3.3", "-i", str(src), "-t", "2", "-map", "0:v:0", "-c", "copy", "-movflags", "+faststart", str(clip))
    first = subprocess.run([ff, "-v", "error", "-i", str(clip), "-frames:v", "1", "-f", "rawvideo", "-"],
                           capture_output=True, check=True).stdout
    at_in = subprocess.run([ff, "-v", "error", "-i", str(src), "-ss", "3.3", "-frames:v", "1", "-f", "rawvideo", "-"],
                           capture_output=True, check=True).stdout
    keyframe = subprocess.run([ff, "-v", "error", "-i", str(src), "-ss", "2.0", "-frames:v", "1", "-f", "rawvideo", "-"],
                              capture_output=True, check=True).stdout
    assert first == at_in and first != keyframe



def test_the_injected_params_are_valid_python_for_a_crop_with_no_time_range():
    # Staging 2026-10-19: a crop with no marked range has jobs with "in": None; written as JSON (`null`) into the
    # sandbox program it raised NameError: name 'null' is not defined. Every param must load as Python.
    act = ma.parse_action(_raw("crop", box={"x": 0.317, "y": 0, "w": 0.316, "h": 1}))
    jobs = ma.plan_outputs(act, "p/")
    assert jobs[0]["in"] is None
    code = ma.build_sandbox_code("", act, jobs, bucket="b", domain="d", region="us-west-2")
    header = code.split("# --- injected params + runner ---\n", 1)[1].split("\nimport os", 1)[0]
    ns: dict = {}
    exec(header, ns)  # noqa: S102 - the header we generate
    assert ns["_JOBS"][0]["in"] is None and ns["_JOBS"][0]["box"] == [0.317, 0.0, 0.316, 1.0]
