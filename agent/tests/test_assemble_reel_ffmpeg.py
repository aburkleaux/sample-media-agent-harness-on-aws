"""assemble_reel.py (agent-olive) uses the harness-built LGPL ffmpeg: libopenh264, no external source."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "skills/agent-olive/scripts/assemble_reel.py"
_spec = importlib.util.spec_from_file_location("assemble_reel", _SCRIPT)
reel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reel)

CLIPS = [{"path": "a.mp4", "duration": 2.0, "has_audio": True}, {"path": "b.mp4", "duration": 2.0, "has_audio": True}]


def test_video_codec_args():
    assert reel.video_codec_args("libopenh264") == ["-c:v", "libopenh264", "-b:v", "8000k", "-pix_fmt", "yuv420p"]
    assert reel.video_codec_args("libx264") == ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    with pytest.raises(ValueError, match="no H.264 encoder.*redeploy"):
        reel.video_codec_args(None)


@pytest.mark.parametrize("mode", ["concat", "crossfade"])
def test_build_command_defaults_to_openh264_and_honours_explicit_args(mode):
    cmd, _ = reel.build_command(CLIPS, ffmpeg="ffmpeg", output=Path("o.mp4"), fps=24,
                                dimensions=(1280, 720), stitch_mode=mode)
    assert cmd[cmd.index("-c:v") + 1] == "libopenh264" and "libx264" not in cmd
    cmd, _ = reel.build_command(CLIPS, ffmpeg="ffmpeg", output=Path("o.mp4"), fps=24,
                                dimensions=(1280, 720), stitch_mode=mode,
                                video_args=reel.video_codec_args("libx264"))
    assert cmd[cmd.index("-c:v") + 1] == "libx264"


def test_ensure_without_a_mirror_never_downloads(monkeypatch, tmp_path):
    monkeypatch.setattr(reel.shutil, "which", lambda b: None)
    monkeypatch.delenv("FFMPEG_S3_URI", raising=False)
    monkeypatch.delenv("FFMPEG_STATIC_URL", raising=False)
    import tempfile as _tf, urllib.request as _u
    monkeypatch.setattr(_tf, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(_u, "urlretrieve", lambda *a, **k: pytest.fail("must not download"))
    with pytest.raises(ValueError, match="no ffmpeg source configured.*redeploy"):
        reel._ensure_ffmpeg_ffprobe()


def test_path_ffmpeg_with_openh264_is_accepted(monkeypatch):
    monkeypatch.setattr(reel.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(reel, "_ffmpeg_has_encoder", lambda p, enc="": enc == "libopenh264")
    assert reel._ensure_ffmpeg_ffprobe() == ("/usr/bin/ffmpeg", "/usr/bin/ffprobe")
