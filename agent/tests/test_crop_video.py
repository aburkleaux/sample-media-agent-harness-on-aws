"""Tests for the bundled crop_video.py script.

Pure-math coverage (no ffmpeg, no network) for the normalized/pixel -> even, in-frame
crop conversion and the ffmpeg command contract, plus a --dry-run end-to-end with the
ffprobe/provisioning calls patched out. Mirrors the test_media_probe discipline: the
script never shells out to a media binary in these tests.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "skills/video-processing/scripts/crop_video.py"
_spec = importlib.util.spec_from_file_location("crop_video", _SCRIPT)
crop_video = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crop_video)


# --- parse_box -------------------------------------------------------------

def test_parse_box_ok():
    assert crop_video.parse_box("0.1,0.2,0.3,0.4") == (0.1, 0.2, 0.3, 0.4)


@pytest.mark.parametrize("bad", ["1,2,3", "1,2,3,4,5", "a,b,c,d", "0.1,0.2,0,0.4", ""])
def test_parse_box_rejects_malformed(bad):
    with pytest.raises(ValueError):
        crop_video.parse_box(bad)


# --- resolve_crop: normalized ---------------------------------------------

@pytest.mark.parametrize("W,H", [(1280, 720), (1920, 1080), (1281, 721), (640, 480)])
def test_resolve_normalized_is_even_and_in_frame(W, H):
    crop = crop_video.resolve_crop((0.2, 0.15, 0.55, 0.6), "normalized", W, H)
    assert crop["w"] % 2 == 0 and crop["h"] % 2 == 0          # yuv420p needs even dims
    assert crop["x"] >= 0 and crop["y"] >= 0
    assert crop["x"] + crop["w"] <= W                          # stays inside the frame
    assert crop["y"] + crop["h"] <= H
    # ~55% x 60% of the frame, within an even-rounding tolerance
    assert abs(crop["w"] - 0.55 * W) <= 2
    assert abs(crop["h"] - 0.60 * H) <= 2


def test_resolve_normalized_full_height_strip():
    # a 9:16-ish full-height strip (the mock's preset case)
    crop = crop_video.resolve_crop((0.34, 0.0, 0.32, 1.0), "normalized", 1280, 720)
    assert crop["h"] == 720 and crop["y"] == 0
    assert crop["w"] % 2 == 0 and crop["x"] + crop["w"] <= 1280


def test_resolve_916_box_yields_true_916_not_square():
    """Regression anchor for the efa3569d crop bug.

    The render UI emits w=0.316, h=1.0 for a 9:16 crop of a 960x540 (16:9) clip. That box
    MUST resolve to a tall 9:16 rectangle (302x540), never a square. The bug was the agent
    reversing the concept — feeding the aspect RATIO (9/16 = 0.5625) as the WIDTH fraction,
    which crops a 540x540 square. Both boxes are exercised here so the difference is locked.
    """
    good = crop_video.resolve_crop((0.230, 0.0, 0.316, 1.0), "normalized", 960, 540)
    assert good["h"] == 540 and good["y"] == 0            # full height
    assert good["w"] == 302                               # 0.316*960 = 303.4 -> even floor 302
    assert abs(good["w"] / good["h"] - 9 / 16) < 0.02     # a true 9:16 portrait

    # The reversal: 0.5625 (= 9/16) as the width fraction is what shipped a square.
    reversed_box = crop_video.resolve_crop((0.230, 0.0, 0.5625, 1.0), "normalized", 960, 540)
    assert reversed_box["w"] == 540 and reversed_box["h"] == 540   # the 1:1 square bug
    assert good["w"] != reversed_box["w"]                          # the correct box is NOT the square


def test_resolve_clamps_overflowing_box():
    # x+w and y+h exceed the frame -> clamped so the crop stays inside
    crop = crop_video.resolve_crop((0.8, 0.8, 0.5, 0.5), "normalized", 1000, 1000)
    assert crop["x"] + crop["w"] <= 1000
    assert crop["y"] + crop["h"] <= 1000


def test_resolve_rejects_subminimum():
    with pytest.raises(ValueError):
        crop_video.resolve_crop((0.0, 0.0, 0.005, 0.005), "normalized", 1280, 720)


# --- resolve_crop: pixels --------------------------------------------------

def test_resolve_pixels_odd_snaps_even_and_clamps():
    crop = crop_video.resolve_crop((100.0, 50.0, 641.0, 361.0), "pixels", 1280, 720)
    assert crop["w"] % 2 == 0 and crop["h"] % 2 == 0
    assert crop["w"] == 640 and crop["h"] == 360          # 641/361 floored to even
    assert crop["x"] == 100 and crop["y"] == 50


def test_resolve_pixels_clamps_into_frame():
    crop = crop_video.resolve_crop((1200.0, 0.0, 400.0, 200.0), "pixels", 1280, 720)
    assert crop["x"] + crop["w"] <= 1280                  # x clamped so 400-wide crop fits


def test_resolve_bad_mode():
    with pytest.raises(ValueError):
        crop_video.resolve_crop((0, 0, 1, 1), "bogus", 1280, 720)


# --- build_ffmpeg_cmd ------------------------------------------------------

def test_cmd_has_crop_and_websafe_flags():
    cmd = crop_video.build_ffmpeg_cmd("ffmpeg", "in.mp4", "out.mp4",
                                      {"w": 640, "h": 360, "x": 100, "y": 50}, has_audio=True)
    s = " ".join(cmd)
    assert "crop=640:360:100:50" in s
    # Default: the harness-built ffmpeg's encoder (LGPL, libopenh264, bitrate-driven).
    assert "-c:v libopenh264 -b:v 5000k" in s and "-pix_fmt yuv420p" in s
    assert "-movflags +faststart" in s
    assert "-c:a aac" in s and "-an" not in cmd


def test_cmd_drops_audio_when_absent():
    cmd = crop_video.build_ffmpeg_cmd("ffmpeg", "in.mp4", "out.mp4",
                                      {"w": 2, "h": 2, "x": 0, "y": 0}, has_audio=False)
    assert "-an" in cmd and "aac" not in " ".join(cmd)


def test_cmd_fuses_trim_before_input():
    cmd = crop_video.build_ffmpeg_cmd("ffmpeg", "in.mp4", "out.mp4",
                                      {"w": 2, "h": 2, "x": 0, "y": 0}, trim=(1.5, 4.0))
    assert "-ss" in cmd and "-to" in cmd
    assert cmd.index("-ss") < cmd.index("-i")             # input-side seek


def test_parse_trim():
    assert crop_video.parse_trim("1.5,4") == (1.5, 4.0)
    for bad in ["1", "4,1", "-1,2", "a,b"]:
        with pytest.raises(ValueError):
            crop_video.parse_trim(bad)


# --- main --dry-run (ffprobe + provisioning patched) -----------------------

def test_dry_run_emits_command_contract(monkeypatch, capsys):
    monkeypatch.setattr(crop_video, "_ensure_ffmpeg_ffprobe", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(crop_video, "probe_dimensions", lambda fp, src: (1280, 720))
    monkeypatch.setattr(crop_video, "probe_has_audio", lambda fp, src: True)
    # Hermetic: never ask the machine's own ffmpeg which encoders it has (CI's has no H.264 one).
    monkeypatch.setattr(crop_video, "h264_encoder", lambda ff: "libopenh264")
    monkeypatch.setattr(crop_video, "probe_video_bitrate", lambda fp, src: 2_000_000)
    monkeypatch.setattr(crop_video, "localize_input", lambda src, d, i=0: "/tmp/in.mp4")

    rc = crop_video.main(["https://cdn/x.mp4", "out.mp4", "--box", "0.2,0.15,0.55,0.6"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dryRun"] is True
    assert report["sourceDims"] == [1280, 720]
    assert f"crop={report['crop']['w']}:{report['crop']['h']}" in " ".join(report["command"])
    assert "/tmp/in.mp4" in report["command"] and "https://cdn/x.mp4" not in report["command"]  # ffmpeg gets the local copy
    assert "-pix_fmt" in report["command"] and "yuv420p" in report["command"]


def test_main_rejects_bad_box_exit2(monkeypatch, capsys):
    monkeypatch.setattr(crop_video, "_ensure_ffmpeg_ffprobe", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(crop_video, "probe_dimensions", lambda fp, src: (1280, 720))
    monkeypatch.setattr(crop_video, "probe_has_audio", lambda fp, src: True)
    rc = crop_video.main(["x.mp4", "out.mp4", "--box", "0,0,0.004,0.004"])
    assert rc == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_main_probe_failure_exit3(monkeypatch, capsys):
    monkeypatch.setattr(crop_video, "_ensure_ffmpeg_ffprobe", lambda: ("ffmpeg", "ffprobe"))
    def boom(fp, src):
        raise ValueError("no video stream found in source")
    monkeypatch.setattr(crop_video, "probe_dimensions", boom)
    rc = crop_video.main(["x.mp4", "out.mp4", "--box-px", "0,0,100,100"])
    assert rc == 3
    assert "error" in json.loads(capsys.readouterr().out)



# --- libx264 encoder verification (#2, session 00ef689c: "Unknown encoder 'libx264'") ------

def test_ffmpeg_has_encoder_detects_libx264(monkeypatch):
    class _R:
        stdout = " V..... libx264   libx264 H.264 / AVC\n V..... libx265 ...\n"
    monkeypatch.setattr(crop_video.subprocess, "run", lambda *a, **k: _R())
    assert crop_video._ffmpeg_has_encoder("/x/ffmpeg", "libx264") is True


def test_ffmpeg_has_encoder_false_when_absent(monkeypatch):
    class _R:
        stdout = " V..... mpeg4 ...\n V..... vp9 ...\n"  # a stripped build, no libx264
    monkeypatch.setattr(crop_video.subprocess, "run", lambda *a, **k: _R())
    assert crop_video._ffmpeg_has_encoder("/x/ffmpeg", "libx264") is False


def test_ffmpeg_has_encoder_false_on_probe_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("cannot exec")
    monkeypatch.setattr(crop_video.subprocess, "run", boom)
    assert crop_video._ffmpeg_has_encoder("/x/ffmpeg", "libx264") is False


def test_ensure_accepts_path_build_with_libx264(monkeypatch):
    monkeypatch.setattr(crop_video.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(crop_video, "_ffmpeg_has_encoder", lambda p, enc="libx264": True)
    assert crop_video._ensure_ffmpeg_ffprobe() == ("/usr/bin/ffmpeg", "/usr/bin/ffprobe")


def test_ensure_rejects_path_build_without_libx264_and_self_provisions(monkeypatch, tmp_path):
    # A PATH ffmpeg exists but has no H.264 encoder → must NOT early-return; it falls through to
    # the staged-build download (stubbed here to raise, proving the fallthrough happened).
    monkeypatch.setattr(crop_video.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(crop_video, "_ffmpeg_has_encoder", lambda p, enc="libx264": False)
    monkeypatch.delenv("FFMPEG_S3_URI", raising=False)
    monkeypatch.setenv("FFMPEG_STATIC_URL", "https://bucket.s3.us-west-2.amazonaws.com/tools/ffmpeg/x.tar.xz?X-Amz-Signature=s")
    import tempfile as _tf, urllib.request as _u
    monkeypatch.setattr(_tf, "gettempdir", lambda: str(tmp_path))  # clean dir → no reuse
    def boom(*a, **k):
        raise RuntimeError("DOWNLOAD_ATTEMPTED")
    monkeypatch.setattr(_u, "urlretrieve", boom)
    with pytest.raises(ValueError, match="DOWNLOAD_ATTEMPTED"):
        crop_video._ensure_ffmpeg_ffprobe()


# --- the harness's own LGPL build: libopenh264, no external download source ---------------------

def test_video_codec_args_openh264_uses_clamped_source_bitrate():
    assert crop_video.video_codec_args("libopenh264", 3_200_000) == [
        "-c:v", "libopenh264", "-b:v", "3200k", "-pix_fmt", "yuv420p"]
    assert "1000k" in crop_video.video_codec_args("libopenh264", 50_000)       # floor
    assert "20000k" in crop_video.video_codec_args("libopenh264", 90_000_000)  # ceiling
    assert "5000k" in crop_video.video_codec_args("libopenh264", None)         # unknown → default


def test_video_codec_args_libx264_keeps_crf_defaults():
    assert crop_video.video_codec_args("libx264", 3_000_000) == ["-c:v", "libx264", "-pix_fmt", "yuv420p"]


def test_video_codec_args_without_an_encoder_is_a_clear_error():
    with pytest.raises(ValueError, match="no H.264 encoder.*redeploy"):
        crop_video.video_codec_args(None)


def test_h264_encoder_prefers_libx264_then_openh264(monkeypatch):
    have = {"libopenh264"}
    monkeypatch.setattr(crop_video, "_ffmpeg_has_encoder", lambda p, enc="": enc in have)
    assert crop_video.h264_encoder("/x/ffmpeg") == "libopenh264"
    have.add("libx264")
    assert crop_video.h264_encoder("/x/ffmpeg") == "libx264"
    have.clear()
    assert crop_video.h264_encoder("/x/ffmpeg") is None


def test_ffmpeg_has_encoder_matches_whole_names(monkeypatch):
    class _R:
        stdout = " V....D libopenh264          OpenH264 H.264 / AVC (codec h264)\n V..... libx264rgb x\n"
    monkeypatch.setattr(crop_video.subprocess, "run", lambda *a, **k: _R())
    assert crop_video._ffmpeg_has_encoder("/x/ffmpeg", "libopenh264") is True
    assert crop_video._ffmpeg_has_encoder("/x/ffmpeg", "libx264") is False  # not libx264rgb


def test_cmd_uses_explicit_video_args():
    cmd = crop_video.build_ffmpeg_cmd("ffmpeg", "in.mp4", "out.mp4", {"w": 2, "h": 2, "x": 0, "y": 0},
                                      video_args=["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    assert "libx264" in cmd and "libopenh264" not in cmd


def test_ensure_without_a_mirror_never_downloads(monkeypatch, tmp_path):
    """No FFMPEG_S3_URI / FFMPEG_STATIC_URL configured → a clear error, never a fetch from anywhere."""
    monkeypatch.setattr(crop_video.shutil, "which", lambda b: None)
    monkeypatch.delenv("FFMPEG_S3_URI", raising=False)
    monkeypatch.delenv("FFMPEG_STATIC_URL", raising=False)
    import tempfile as _tf, urllib.request as _u
    monkeypatch.setattr(_tf, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(_u, "urlretrieve", lambda *a, **k: pytest.fail("must not download"))
    with pytest.raises(ValueError, match="no ffmpeg source configured.*redeploy"):
        crop_video._ensure_ffmpeg_ffprobe()


def _arch_tarball():
    return ("ffmpeg-release-arm64-static.tar.xz" if crop_video.os.uname().machine in ("arm64", "aarch64")
            else "ffmpeg-release-amd64-static.tar.xz")


def test_ensure_reads_the_staged_build_by_s3_key_with_the_sandbox_role(monkeypatch, tmp_path):
    """FFMPEG_S3_URI → boto3 GetObject of <prefix>/<arch tarball> (no URL, public or signed)."""
    import sys as _sys, tempfile as _tf, types, urllib.request as _u
    monkeypatch.setattr(crop_video.shutil, "which", lambda b: None)
    monkeypatch.setenv("FFMPEG_S3_URI", "s3://skills-bkt/tools/ffmpeg/")
    monkeypatch.delenv("FFMPEG_STATIC_URL", raising=False)
    monkeypatch.setattr(_tf, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(_u, "urlretrieve", lambda *a, **k: pytest.fail("must not use a URL"))
    calls = []

    class _S3:
        def download_file(self, bucket, key, dest):
            calls.append((bucket, key))
            raise RuntimeError("AccessDenied")

    monkeypatch.setitem(_sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: _S3()))
    with pytest.raises(ValueError, match="could not download ffmpeg from s3://skills-bkt/tools/ffmpeg/.*AccessDenied.*redeploy"):
        crop_video._ensure_ffmpeg_ffprobe()
    assert calls == [("skills-bkt", "tools/ffmpeg/" + _arch_tarball())]


def test_ensure_rejects_a_non_s3_ffmpeg_s3_uri(monkeypatch, tmp_path):
    monkeypatch.setattr(crop_video.shutil, "which", lambda b: None)
    monkeypatch.setenv("FFMPEG_S3_URI", "https://cdn.example/tools/ffmpeg")
    with pytest.raises(ValueError, match="must be an s3:// URI"):
        crop_video._ensure_ffmpeg_ffprobe()


def test_ensure_presigned_url_denied_is_a_clear_error(monkeypatch, tmp_path):
    import tempfile as _tf, urllib.error as _ue, urllib.request as _u
    monkeypatch.setattr(crop_video.shutil, "which", lambda b: None)
    monkeypatch.delenv("FFMPEG_S3_URI", raising=False)
    signed = "https://b.s3.us-west-2.amazonaws.com/tools/ffmpeg/t.tar.xz?X-Amz-Signature=s"
    monkeypatch.setenv("FFMPEG_STATIC_URL", signed)
    monkeypatch.setattr(_tf, "gettempdir", lambda: str(tmp_path))
    def denied(url, *a, **k):
        assert url == signed
        raise _ue.HTTPError(url, 403, "Forbidden", {}, None)
    monkeypatch.setattr(_u, "urlretrieve", denied)
    with pytest.raises(ValueError, match="could not download ffmpeg.*403.*redeploy"):
        crop_video._ensure_ffmpeg_ffprobe()
