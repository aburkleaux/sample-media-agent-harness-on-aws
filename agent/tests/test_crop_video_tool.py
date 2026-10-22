"""Tests for the first-class ``crop_video`` @tool (agent/src/tools/crop_video.py).

The tool now runs ffmpeg in a code_interpreter SANDBOX (not the agent runtime). No live sandbox,
network, ffmpeg, or S3 is touched here. Two layers of coverage:

  1. Tool-level: validation, SSRF, the deterministic sandbox PROGRAM it builds (box VERBATIM), and
     parsing the sandbox's sentinel JSON — with ``_run_in_sandbox`` patched.
  2. Program-level: the built sandbox program is EXEC'd in-process with ONLY its I/O mocked
     (urllib fetch, ffprobe/ffmpeg subprocess, boto3 put_object), exercising the REAL bundled
     ``resolve_crop``/``build_ffmpeg_cmd``/provision logic end to end — the efa3569d regression and
     the download->probe->crop->upload->JSON contract, without a real sandbox.

Wiring is asserted by TEXT against both runtime modules.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import types
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import tools.crop_video as cvt  # noqa: E402


# ---------------------------------------------------------------------------------------------
# Tool level — _run_in_sandbox patched
# ---------------------------------------------------------------------------------------------

@pytest.fixture
def sandbox(monkeypatch):
    """Patch the sandbox seam; capture the built program and return a canned sentinel result."""
    cap: dict = {}

    def fake_run_in_sandbox(code: str, timeout: float) -> str:
        cap["code"] = code
        cap["timeout"] = timeout
        result = cap.get("result") or {
            "status": "cropped",
            "url": "https://cdn.example/artifacts/s/output/crop_x.mp4",
            "key": "artifacts/s/output/crop_x.mp4",
            "crop": {"w": 302, "h": 540, "x": 220, "y": 0},
            "sourceDims": [960, 540],
            "trim": None,
            "audio": True,
        }
        return f"some ffmpeg chatter\n{cvt._SENTINEL}{json.dumps(result)}\ntrailing noise"

    monkeypatch.setattr("media.persist_media.validate_fetchable", lambda u: None)
    monkeypatch.setattr(cvt, "_run_in_sandbox", fake_run_in_sandbox)
    monkeypatch.setenv("SKILLS_S3_BUCKET", "skills-bucket")
    monkeypatch.setenv("ARTIFACTS_DOMAIN", "cdn.example")
    monkeypatch.setenv("ARTIFACTS_PREFIX", "artifacts/")
    return cap


def _call(**kw) -> dict:
    return json.loads(cvt.crop_video(**kw))


def test_returns_the_sandbox_result_and_builds_a_verbatim_program(sandbox):
    res = _call(source_url="https://cdn.example/meridian.mp4", box="0.229,0.0,0.316,1.0")
    assert res["status"] == "cropped"
    assert res["crop"] == {"w": 302, "h": 540, "x": 220, "y": 0}
    # the built sandbox program carries the box VERBATIM + the source + a session-scoped output key
    code = sandbox["code"]
    assert '"0.229,0.0,0.316,1.0"' in code            # box, verbatim, as a JSON literal
    assert '"https://cdn.example/meridian.mp4"' in code
    assert '"skills-bucket"' in code                  # bucket injected
    assert '"cdn.example"' in code                    # domain injected
    assert "artifacts/" in code and "crop_" in code and ".mp4" in code  # output key injected
    # the bundled script source is injected (single definition of the crop math)
    assert "def resolve_crop(" in code and "def build_ffmpeg_cmd(" in code


def test_pixel_box_and_trim_reach_the_program(sandbox):
    _call(source_url="https://cdn.example/m.mp4", box_px="100,50,640,360", trim="1.0,4.0")
    code = sandbox["code"]
    assert '"100,50,640,360"' in code
    assert '"pixels"' in code
    assert '"1.0,4.0"' in code


def test_sandbox_error_is_returned_verbatim(sandbox):
    sandbox["result"] = {"error": "ffmpeg failed: Unknown encoder 'libx264'"}
    res = _call(source_url="https://cdn.example/m.mp4", box="0,0,0.5,0.5")
    assert "ffmpeg failed" in res["error"]


def test_no_sentinel_line_is_an_actionable_error(monkeypatch):
    monkeypatch.setattr("media.persist_media.validate_fetchable", lambda u: None)
    monkeypatch.setattr(cvt, "_run_in_sandbox", lambda code, timeout: "boom, no result here")
    res = _call(source_url="https://cdn.example/m.mp4", box="0,0,0.5,0.5")
    assert "no crop result" in res["error"]


# --- validation / error paths (fail BEFORE any sandbox) ----------------------------------------

def test_requires_source_url(sandbox):
    assert "source_url is required" in _call(source_url="", box="0,0,0.5,0.5")["error"]
    assert "code" not in sandbox  # never reached the sandbox


def test_rejects_both_box_and_box_px(sandbox):
    err = _call(source_url="https://cdn.example/m.mp4", box="0,0,0.5,0.5", box_px="0,0,10,10")["error"]
    assert "EXACTLY ONE" in err


def test_rejects_neither_box(sandbox):
    assert "EXACTLY ONE" in _call(source_url="https://cdn.example/m.mp4")["error"]


def test_rejects_malformed_box(sandbox):
    assert "error" in _call(source_url="https://cdn.example/m.mp4", box="1,2,3")
    assert "code" not in sandbox


def test_ssrf_guard_refuses_non_public_source(monkeypatch, sandbox):
    from media.persist_media import PersistError
    def boom(url):
        raise PersistError("host resolves to non-public 10.0.0.1")
    monkeypatch.setattr("media.persist_media.validate_fetchable", boom)
    err = _call(source_url="https://internal.evil/x.mp4", box="0,0,0.5,0.5")["error"]
    assert "source not fetchable" in err


# --- _extract_stdout parses the invoke stream --------------------------------------------------

def test_extract_stdout_reads_structured_and_text():
    resp = {"stream": [
        {"result": {"structuredContent": {"stdout": "line-A\n", "exitCode": 0}}},
        {"result": {"content": [{"type": "text", "text": "line-B"}]}},
        {"other": "ignored"},
    ]}
    out = cvt._extract_stdout(resp)
    assert "line-A" in out and "line-B" in out


def test_extract_stdout_handles_no_stream():
    assert cvt._extract_stdout({}) == ""


# --- the tool reuses the bundled script's math (one implementation) ----------------------------

def test_load_script_exposes_the_shared_helpers():
    mod = cvt._load_script()
    for fn in ("parse_box", "resolve_crop", "build_ffmpeg_cmd", "parse_trim",
               "probe_dimensions", "probe_has_audio", "_ensure_ffmpeg_ffprobe"):
        assert hasattr(mod, fn), f"bundled script missing {fn}"
    assert mod.resolve_crop((0.229, 0.0, 0.316, 1.0), "normalized", 960, 540) == {
        "w": 302, "h": 540, "x": 220, "y": 0}


# ---------------------------------------------------------------------------------------------
# Program level — EXEC the built sandbox program with only I/O mocked (no live sandbox)
# ---------------------------------------------------------------------------------------------

def _exec_program(code: str, monkeypatch, *, dims=(960, 540), has_audio=True, ffmpeg_rc=0,
                  ffmpeg_stderr="", module_name="sandbox_exec"):
    """Run the built sandbox program in-process, mocking only urllib/ffprobe/ffmpeg/boto3/shutil.
    Returns (parsed_result_dict, put_calls).

    ``module_name`` is the ``__name__`` the program is exec'd under; the real code interpreter runs a
    cell as ``"__main__"`` (see the name-guard test below)."""
    put_calls: list[dict] = []

    def fake_which(_name):
        return f"/usr/bin/{_name}"

    def fake_urlretrieve(url, dest):
        with open(dest, "wb") as fh:
            fh.write(b"SRCBYTES")
        return dest, {}

    def fake_run(cmd, *a, **k):
        parts = [str(c) for c in cmd]
        joined = " ".join(parts)
        if "-encoders" in parts:                      # _ffmpeg_has_encoder(libx264)
            return types.SimpleNamespace(returncode=0, stdout=" V..... libx264 H.264\n", stderr="")
        if "stream=width,height" in joined:           # probe_dimensions
            w, h = dims
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps({"streams": [{"width": w, "height": h}]}), stderr="")
        if "stream=index" in joined:                  # probe_has_audio
            return types.SimpleNamespace(returncode=0, stdout=("0\n" if has_audio else ""), stderr="")
        # otherwise: the ffmpeg crop command — create the output file (last arg) and return rc
        if ffmpeg_rc == 0:
            with open(parts[-1], "wb") as fh:
                fh.write(b"CROPPEDMP4")
        return types.SimpleNamespace(returncode=ffmpeg_rc, stdout="", stderr=ffmpeg_stderr)

    class _S3:
        def put_object(self, **kw):
            put_calls.append(kw)

    fake_boto3 = types.SimpleNamespace(client=lambda *a, **k: _S3())

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(urllib.request, "urlretrieve", fake_urlretrieve)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(code, "<sandbox>", "exec"), {"__name__": module_name})
    out = buf.getvalue()
    line = next((ln for ln in out.splitlines() if ln.startswith(cvt._SENTINEL)), None)
    assert line is not None, f"no sentinel in program output: {out[-300:]}"
    return json.loads(line[len(cvt._SENTINEL):]), put_calls


def _build(**kw):
    defaults = dict(src="https://cdn.example/m.mp4", box="0.229,0.0,0.316,1.0", box_px="",
                    mode="normalized", trim="", bucket="skills-bucket",
                    key="artifacts/s/output/crop_ab.mp4", domain="cdn.example", region="us-west-2",
                    timeout=300.0)
    defaults.update(kw)
    return cvt._build_sandbox_code(cvt._script_source(), **defaults)


def test_program_crops_a_true_916_and_uploads(monkeypatch):
    """efa3569d at the PROGRAM level: a 9:16 box on a 960x540 source yields a tall 9:16 crop
    (302x540), NOT a 540x540 square, and uploads to the exact key with the CloudFront URL."""
    result, puts = _exec_program(_build(), monkeypatch)
    assert result["status"] == "cropped"
    assert result["sourceDims"] == [960, 540]
    assert result["crop"] == {"w": 302, "h": 540, "x": 220, "y": 0}
    assert result["crop"]["w"] != result["crop"]["h"]
    assert abs(result["crop"]["w"] / result["crop"]["h"] - 9 / 16) < 0.02
    assert result["audio"] is True
    assert result["url"] == "https://cdn.example/artifacts/s/output/crop_ab.mp4"
    assert len(puts) == 1
    assert puts[0]["Bucket"] == "skills-bucket"
    assert puts[0]["Key"] == "artifacts/s/output/crop_ab.mp4"
    assert puts[0]["ContentType"] == "video/mp4"


def test_program_passes_box_through_verbatim_no_aspect_recompute(monkeypatch):
    """Faithfulness: feeding the reversed box (ratio 0.5625 as width) reproduces the 540x540
    square that shipped in efa3569d — the program crops EXACTLY what it is handed."""
    result, _ = _exec_program(_build(box="0.229,0.0,0.5625,1.0"), monkeypatch)
    assert result["crop"] == {"w": 540, "h": 540, "x": 220, "y": 0}


def test_program_pixel_box_and_trim(monkeypatch):
    result, _ = _exec_program(_build(box="", box_px="100,50,640,360", mode="pixels", trim="1.0,4.0"),
                              monkeypatch)
    assert result["crop"] == {"w": 640, "h": 360, "x": 100, "y": 50}
    assert result["trim"] == [1.0, 4.0]


def test_program_reports_ffmpeg_failure(monkeypatch):
    result, puts = _exec_program(_build(), monkeypatch, ffmpeg_rc=1,
                                 ffmpeg_stderr="Unknown encoder 'libx264'")
    assert "ffmpeg failed" in result["error"]
    assert "libx264" in result["error"]
    assert puts == []  # nothing uploaded on failure


def test_program_crops_when_the_cell_runs_as___main__(monkeypatch):
    """Staging 2026-10-08: the code interpreter executes a cell in an IPython kernel, where
    ``__name__`` is ``"__main__"``. The bundled script's ``if __name__ == "__main__"`` CLI then ran on
    the KERNEL's argv and killed the program with ``SystemExit: 2``
    (``error: the following arguments are required: out``), which the tool surfaced as "sandbox
    produced no crop result" — so EVERY crop and Clip failed. The program neutralises ``__name__``,
    so it crops identically under either name."""
    as_main, puts = _exec_program(_build(), monkeypatch, module_name="__main__")
    assert as_main["status"] == "cropped"
    assert as_main["crop"] == {"w": 302, "h": 540, "x": 220, "y": 0}
    assert len(puts) == 1

    as_module, _ = _exec_program(_build(), monkeypatch, module_name="sandbox_exec")
    assert as_module == as_main


def test_built_program_neutralises_the_scripts_main_guard():
    """The injected copy is a LIBRARY: the `__main__` guard cannot fire, while `main` stays defined
    (the script is a standalone CLI too, and its own file is never changed)."""
    code = _build()
    assert 'if __name__ == "__main__":' not in code
    assert "if False:  # sandbox:" in code
    assert "def main(" in code
    compile(code, "<guard-test>", "exec")  # the disabled guard keeps the program syntactically valid
    # The guard is disabled in the COPY only.
    assert 'if __name__ == "__main__":' in cvt._script_source()


def test_program_drops_audio_flag_when_source_is_silent(monkeypatch):
    result, _ = _exec_program(_build(), monkeypatch, has_audio=False)
    assert result["audio"] is False


# --- wiring: registered in BOTH runtimes (text assertions, no heavy init) -----------------------

MAIN = (SRC / "main.py").read_text()
TASK = (SRC / "task_agent.py").read_text()


def test_registered_in_main_runtime():
    assert "from tools.crop_video import crop_video" in MAIN
    assert '"crop_video": crop_video' in MAIN
    assert MAIN.count('"crop_video"') >= 2


def test_registered_in_task_runtime():
    assert "from tools.crop_video import crop_video" in TASK
    assert '"crop_video": crop_video' in TASK


def test_private_cdn_project_asset_source_is_presigned_for_the_trusted_project(sandbox, monkeypatch):
    # Session 61deed9b: crop_video of a project video 403'd twice on the private CDN -- only artifact keys were
    # presigned for the sandbox. A project ASSET of the turn's TRUSTED project now is; another project's is not.
    from jobs import context as jc
    from media import artifact_fetch as af

    pid = "p" + "a" * 32
    src = f"https://cdn.example/projects/{pid}/video:11111111-1111-4111-8111-111111111111/trailer.mp4"
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "1")
    monkeypatch.setattr(af, "_presign_key", lambda key, exp, method="GET": f"https://skills-bucket.s3.amazonaws.com/{key}?X-Amz-Signature=s")
    monkeypatch.setattr(jc, "trusted_project_id", lambda: pid)
    _call(source_url=src, box="0,0,0.5,1")
    assert "X-Amz-Signature=s" in sandbox["code"]
    monkeypatch.setattr(jc, "trusted_project_id", lambda: "p" + "b" * 32)
    _call(source_url=src, box="0,0,0.5,1")
    assert "X-Amz-Signature" not in sandbox["code"] and f'"{src}"' in sandbox["code"]


def test_program_fetches_ffmpeg_by_s3_key_never_the_cdn():
    """The private artifacts CDN serves nothing: the sandbox reads the staged ffmpeg with its IAM role."""
    code = _build()
    assert '"s3://" + _BUCKET + "/tools/ffmpeg"' in code
    assert 'os.environ["FFMPEG_S3_URI"]' in code
    assert "FFMPEG_STATIC_BASE_URL" not in code
    assert '_DOMAIN + "/tools/ffmpeg"' not in code


@pytest.mark.parametrize("aspect,dims,expect", [
    ("1:1", (1280, 720), {"w": 720, "h": 720, "x": 280, "y": 0}),
    ("9:16", (1280, 720), {"w": 404, "h": 720, "x": 438, "y": 0}),
    ("16:9", (720, 1280), {"w": 720, "h": 404, "x": 0, "y": 438}),
])
def test_program_centers_an_aspect_crop_on_the_probed_size(monkeypatch, aspect, dims, expect):
    """Staging 28391ced: "crop to 1:1, centered" — the model guessed a 1920x1080 source and passed
    x=0.25,w=0.5; on the real 1280x720 that is 640x720, not a square. `aspect` is sized from the probe."""
    result, _ = _exec_program(_build(box="", aspect=aspect, mode="aspect"), monkeypatch, dims=dims)
    crop = result["crop"]
    assert abs(crop["w"] - expect["w"]) <= 2 and abs(crop["h"] - expect["h"]) <= 2
    assert abs(crop["x"] - expect["x"]) <= 2 and abs(crop["y"] - expect["y"]) <= 2


def test_the_tool_takes_exactly_one_of_box_box_px_or_aspect(sandbox):
    out = json.loads(asyncio_run(cvt.crop_video(source_url="https://cdn.example/a.mp4", box="0,0,1,1", aspect="1:1")))
    assert "EXACTLY ONE" in out["error"]
    out = json.loads(asyncio_run(cvt.crop_video(source_url="https://cdn.example/a.mp4", aspect="square")))
    assert "aspect must look like" in out["error"]
    json.loads(asyncio_run(cvt.crop_video(source_url="https://cdn.example/a.mp4", aspect="1:1")))
    assert '_MODE = "aspect"' in sandbox["code"] and '_BOX = "1:1"' in sandbox["code"]


def asyncio_run(v):
    import asyncio, inspect
    return asyncio.run(v) if inspect.isawaitable(v) else v
