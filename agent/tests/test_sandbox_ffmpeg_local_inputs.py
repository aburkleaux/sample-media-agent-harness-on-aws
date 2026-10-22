"""The sandbox's ffmpeg/ffprobe only ever see LOCAL files: the scripts download every remote input with Python first.

The staged ffmpeg is a static build that cannot resolve any hostname ("Failed to resolve hostname <host>: System
error" for S3 and CloudFront alike, while Python in the same sandbox resolves them; verified in the harness code
interpreter 2026-10-20), and the harness's own LGPL build is compiled with --disable-network. Prod session f0c50162's
reel stitch failed twice handing ffprobe signed URLs.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "skills")


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reel = _load("agent-olive/scripts/assemble_reel.py", "assemble_reel_local")
crop = _load("video-processing/scripts/crop_video.py", "crop_video_local")


@pytest.mark.parametrize("mod", [reel, crop])
def test_local_paths_are_untouched(mod, tmp_path):
    assert mod.localize_input("/data/clip.mp4", str(tmp_path)) == "/data/clip.mp4"


@pytest.mark.parametrize("mod", [reel, crop])
def test_https_is_downloaded_by_python_with_a_stall_timeout(mod, tmp_path, monkeypatch):
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(url, timeout=None):
        seen.update(url=url, timeout=timeout)
        return Resp(b"mp4-bytes")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    url = "https://bucket.s3.us-west-2.amazonaws.com/artifacts/s/output/shot%2001.mp4?X-Amz-Signature=x"
    local = mod.localize_input(url, str(tmp_path), 3)
    assert local == os.path.join(str(tmp_path), "003_shot 01.mp4")
    assert open(local, "rb").read() == b"mp4-bytes"
    assert seen == {"url": url, "timeout": mod._STALL_SECONDS}
    assert mod.localize_input(url, str(tmp_path), 3) == local  # reused, not fetched again


@pytest.mark.parametrize("mod", [reel, crop])
def test_s3_uses_the_sandbox_role(mod, tmp_path, monkeypatch):
    calls = []
    fake = types.SimpleNamespace(client=lambda *a, **k: types.SimpleNamespace(
        download_file=lambda b, key, dst: (calls.append((b, key)), open(dst, "wb").write(b"x"))))
    monkeypatch.setitem(sys.modules, "boto3", fake)
    local = mod.localize_input("s3://skills/projects/p/sessions/s/output/c.mp4", str(tmp_path))
    assert calls == [("skills", "projects/p/sessions/s/output/c.mp4")] and os.path.isfile(local)


@pytest.mark.parametrize("mod", [reel, crop])
def test_a_failed_download_is_one_clear_oserror(mod, tmp_path, monkeypatch):
    import urllib.request
    def boom(url, timeout=None):
        raise TimeoutError("timed out")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(OSError, match="could not download c.mp4"):
        mod.localize_input("https://h/c.mp4", str(tmp_path))
    assert not os.listdir(tmp_path)


def test_the_reel_probes_and_stitches_the_local_copies(tmp_path, monkeypatch):
    probed = []
    monkeypatch.setattr(reel, "localize_input", lambda src, d, i=0: f"/local/{i}.mp4")
    def fake_probe(source, ffprobe):
        probed.append(source)
        return {"path": source, "duration": 2.0, "fps": 24.0, "width": 1280, "height": 720, "audio_streams": 1}
    monkeypatch.setattr(reel, "probe_media", fake_probe)
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"clips": [{"path": "https://h/a.mp4"}, {"path": "https://h/b.mp4"}]}))
    clips, _ = reel.load_clips(m, "ffprobe", expected_fps=24.0, duration_tolerance=0.5, fps_tolerance=0.05)
    assert probed == ["/local/0.mp4", "/local/1.mp4"]
    assert [c["path"] for c in clips] == ["/local/0.mp4", "/local/1.mp4"]


def test_the_reel_takes_the_clips_own_frame_rate_unless_one_is_required(tmp_path, monkeypatch):
    """The lip-sync model now returns 25 fps; a hard-coded 24 fps default refused every valid set of
    clips ("clip 0 is 25 fps, expects 24") and the agent fell back to hand-rolled ffmpeg."""
    monkeypatch.setattr(reel, "localize_input", lambda src, d, i=0: f"/local/{i}.mp4")
    rates = iter([25.0, 25.0, 24.0])
    monkeypatch.setattr(reel, "probe_media", lambda s, f: {"path": s, "duration": 2.0, "fps": next(rates),
                                                           "width": 768, "height": 1024, "audio_streams": 1})
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"clips": [{"path": f"https://h/{i}.mp4"} for i in range(3)]}))
    clips, _ = reel.load_clips(m, "ffprobe", expected_fps=None, duration_tolerance=0.5, fps_tolerance=0.05)
    assert reel.output_fps(clips, None) == 25.0           # the clips' own (most common) rate
    assert reel.output_fps(clips, 30.0) == 30.0
    assert reel.build_parser().parse_args(["m.json", "o.mp4"]).fps is None
    rates = iter([25.0])
    monkeypatch.setattr(reel, "probe_media", lambda s, f: {"path": s, "duration": 2.0, "fps": next(rates),
                                                           "width": 768, "height": 1024, "audio_streams": 1})
    m.write_text(json.dumps({"clips": [{"path": "https://h/0.mp4"}]}))
    with pytest.raises(ValueError, match="frame-rate mismatch"):  # an explicit --fps is still a contract
        reel.load_clips(m, "ffprobe", expected_fps=24.0, duration_tolerance=0.5, fps_tolerance=0.05)


def test_a_clip_named_by_url_is_accepted_like_path(tmp_path, monkeypatch):
    monkeypatch.setattr(reel, "localize_input", lambda src, d, i=0: f"/local/{i}.mp4")
    monkeypatch.setattr(reel, "probe_media", lambda s, f: {"path": s, "duration": 2.0, "fps": 25.0,
                                                           "width": 768, "height": 1024, "audio_streams": 1})
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"clips": [{"url": "https://h/a.mp4"}, {"src": "https://h/b.mp4"}]}))
    clips, _ = reel.load_clips(m, "ffprobe", expected_fps=None, duration_tolerance=0.5, fps_tolerance=0.05)
    assert len(clips) == 2


def test_the_reel_is_uploaded_when_the_output_is_an_s3_uri_or_a_session_output_key(monkeypatch):
    """Staging c4a4f142: the output `projects/<pid>/sessions/<sid>/output/olive_reel_final.mp4` was a LOCAL
    path, so the reel never reached S3 and the link the user got was dead."""
    local, up = reel.output_target("s3://bkt/projects/p/sessions/s/output/reel.mp4")
    assert up == "s3://bkt/projects/p/sessions/s/output/reel.mp4" and local.name == "reel.mp4"
    monkeypatch.setenv("FFMPEG_S3_URI", "s3://skills-bkt/tools/ffmpeg")
    local, up = reel.output_target("projects/p/sessions/s/output/reel.mp4")
    assert up == "s3://skills-bkt/projects/p/sessions/s/output/reel.mp4"
    assert reel.output_target("out/reel.mp4") == (Path("out/reel.mp4"), None)
    monkeypatch.delenv("FFMPEG_S3_URI")
    assert reel.output_target("projects/p/x.mp4")[1] is None
    with pytest.raises(ValueError):
        reel.output_target("s3://bkt/")
