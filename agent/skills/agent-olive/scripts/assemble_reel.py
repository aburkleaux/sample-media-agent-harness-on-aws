#!/usr/bin/env python3
"""Build or execute one ffmpeg graph for an Agent Olive reel.

Manifest shape:
{"clips": [{"path": "clip-01.mp4", "duration": 6.2}, ...]}
Every input is probed. A supplied duration is an expected value, not trusted
metadata. Inputs may be local paths or HTTPS URLs accepted by ffmpeg. The script
never invokes a shell.

Self-provisioning: the AgentCore code interpreter has no system ffmpeg/ffprobe
(and ``imageio-ffmpeg`` ships ffmpeg only, no ffprobe). On startup this script
resolves BOTH binaries and, if either is missing, downloads the harness's OWN static
LGPL ffmpeg + ffprobe build (FFmpeg + OpenH264 from pinned sources,
``infra/lib/distribution/ffmpeg/Dockerfile``), which the deploy stages at
``s3://<skills-bucket>/tools/ffmpeg/`` — so ``FFMPEG_S3_URI=s3://<skills-bucket>/tools/ffmpeg python
assemble_reel.py <manifest> <out> --execute`` runs in the sandbox with no prior setup (the
sandbox's IAM role reads it; no public link serves it). ``FFMPEG_STATIC_URL`` takes a presigned
URL instead; there is no external download source. That build
encodes H.264 with ``libopenh264`` (bitrate-driven); a PATH ffmpeg with ``libx264`` is used as-is.

Joins (default ``--stitch-mode concat``): lip-sync clips each carry their OWN
audio and do NOT share an anchor endpoint, so this CONCATENATES them (no mux, no
crossfade — a blend would smear two different mouth poses). A short held-frame
"breath" is added to every clip's tail except the last (``--pad-seconds``, default
1.0s) so spoken lines do not run together: the video freezes the last frame
(``tpad`` clone) and the audio appends matching silence (``apad``), keeping each
segment A/V-aligned before the concat.

Legacy ``--stitch-mode crossfade`` keeps the old anchor-pinned behavior (a tiny
symmetric dissolve) for a workflow whose segments all start/end on one anchor.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

# H.264 encoders, in preference order. The harness-built ffmpeg ships libopenh264 only; libx264 is
# used when a binary already on the sandbox's PATH happens to have it (nothing here downloads one).
H264_ENCODERS = ("libx264", "libopenh264")
OPENH264_REEL_KBPS = 8000  # one target for the whole reel; its inputs are independently encoded clips
STAGED_FFMPEG_HINT = (
    "the harness deploy stages its ffmpeg build at s3://<skills-bucket>/tools/ffmpeg/; pass "
    "FFMPEG_S3_URI=s3://<skills-bucket>/tools/ffmpeg (the sandbox role reads it) or FFMPEG_STATIC_URL="
    "<presigned URL>, and redeploy the harness if it is missing"
)


def _ffmpeg_has_encoder(ffmpeg_path: str, encoder: str = "libopenh264") -> bool:
    """True if ``ffmpeg_path`` was built WITH ``encoder``. A PATH ffmpeg can have the binary but
    no H.264 encoder (e.g. imageio-ffmpeg's stripped build), which fails encoding with
    ``Unknown encoder`` (session 00ef689c). Best-effort: any probe failure returns False so the
    caller provisions the harness build."""
    try:
        result = subprocess.run([ffmpeg_path, "-hide_banner", "-encoders"],
                                capture_output=True, text=True, timeout=30)
        return f" {encoder} " in f" {(result.stdout or '')} ".replace("\n", " ")
    except (OSError, subprocess.SubprocessError):
        return False


def h264_encoder(ffmpeg_path: str) -> str | None:
    """The first available encoder of ``H264_ENCODERS`` in ``ffmpeg_path``, else None."""
    for encoder in H264_ENCODERS:
        if _ffmpeg_has_encoder(ffmpeg_path, encoder):
            return encoder
    return None


def video_codec_args(encoder: str | None) -> list[str]:
    """The ``-c:v`` arguments for ``encoder``. Pure. libopenh264 has no CRF, so it gets a fixed
    target bitrate for the reel."""
    if encoder == "libx264":
        return ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if encoder == "libopenh264":
        return ["-c:v", "libopenh264", "-b:v", f"{OPENH264_REEL_KBPS}k", "-pix_fmt", "yuv420p"]
    raise ValueError(f"ffmpeg has no H.264 encoder (need one of {', '.join(H264_ENCODERS)}); "
                     + STAGED_FFMPEG_HINT)


def _ensure_ffmpeg_ffprobe(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> tuple[str, str]:
    """Resolve BOTH ffmpeg and ffprobe, provisioning a static build if the sandbox lacks them.

    The AgentCore code interpreter is an ephemeral, managed sandbox — Python + pip + public
    network, but NO system ffmpeg/ffprobe, and there is no image to bake them into. The common
    ``imageio-ffmpeg`` pip package ships ffmpeg ONLY (no ffprobe), which is exactly why this
    script could not run there. When either binary is missing we download a STATIC release that
    ships BOTH (ffprobe included), matching the sandbox architecture, extract it to a temp dir,
    make the pair discoverable on PATH, and return their absolute paths. Using ONE static build
    for both avoids mixing an old imageio ffmpeg with a newer ffprobe.

    The source is ``FFMPEG_S3_URI`` (``s3://<skills-bucket>/tools/ffmpeg``, where the harness
    deploy stages its own build; the arch-specific ``ffmpeg-release-<arch>-static.tar.xz`` is
    appended, and the sandbox's IAM role reads it) or ``FFMPEG_STATIC_URL`` (a full, presigned
    tarball URL). There is deliberately no external fallback: a missing build is a clear error.
    """
    resolved_ff = shutil.which(ffmpeg)
    resolved_fp = shutil.which(ffprobe)
    # Require an H.264 ENCODER, not merely the binary: a PATH ffmpeg without one fails with
    # "Unknown encoder". Fall through to the staged build.
    if resolved_ff and resolved_fp and h264_encoder(resolved_ff):
        return resolved_ff, resolved_fp

    import platform
    import tarfile
    import tempfile
    import urllib.request

    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    tarball = f"ffmpeg-release-{arch}-static.tar.xz"
    s3_uri = os.environ.get("FFMPEG_S3_URI", "").strip()
    url = os.environ.get("FFMPEG_STATIC_URL", "").strip()
    bucket = key = ""
    if s3_uri:
        if not s3_uri.startswith("s3://"):
            raise ValueError(f"FFMPEG_S3_URI must be an s3:// URI, got {s3_uri!r}")
        bucket, _, key = s3_uri[len("s3://"):].partition("/")
        key = key.strip("/")
        if not key.endswith(".tar.xz"):
            key = f"{key or 'tools/ffmpeg'}/{tarball}"
        url = f"s3://{bucket}/{key}"
    if not url:
        raise ValueError("no ffmpeg source configured (FFMPEG_S3_URI / FFMPEG_STATIC_URL); "
                         + STAGED_FFMPEG_HINT)
    root = Path(tempfile.gettempdir()) / "ffmpeg-static"
    root.mkdir(parents=True, exist_ok=True)

    def _find(name: str) -> str | None:
        for candidate in root.rglob(name):
            if candidate.is_file():
                return str(candidate)
        return None

    # Reuse a prior extraction within this sandbox session before downloading again.
    ff_path = _find("ffmpeg")
    fp_path = _find("ffprobe")
    if not (ff_path and fp_path):
        archive = root / "ffmpeg-static.tar.xz"
        try:
            if bucket:
                # The sandbox's own IAM role reads the staged build: no public or signed link at all.
                import boto3
                boto3.client("s3", region_name=os.environ.get("AWS_REGION") or None).download_file(
                    bucket, key, str(archive))
            else:
                urllib.request.urlretrieve(url, archive)  # noqa: S310 — a presigned URL to our own build
        except Exception as exc:  # noqa: BLE001 - boto3 / urllib errors alike → one actionable message
            raise ValueError(f"could not download ffmpeg from {url} ({exc}); " + STAGED_FFMPEG_HINT) from exc
        with tarfile.open(archive) as tar:
            try:
                tar.extractall(root, filter="data")  # Python 3.12+ safe extraction
            except TypeError:
                tar.extractall(root)  # older Python without the filter kwarg
        ff_path = _find("ffmpeg")
        fp_path = _find("ffprobe")

    if not (ff_path and fp_path):
        raise ValueError(f"the ffmpeg archive at {url} has no ffmpeg+ffprobe; " + STAGED_FFMPEG_HINT)
    for binary in (ff_path, fp_path):
        os.chmod(binary, 0o755)
    if not h264_encoder(ff_path):
        raise ValueError(f"provisioned ffmpeg at {ff_path} has no H.264 encoder "
                         f"(need one of {', '.join(H264_ENCODERS)}); " + STAGED_FFMPEG_HINT)
    # Prepend so any child process can also resolve them by bare name.
    os.environ["PATH"] = str(Path(ff_path).parent) + os.pathsep + os.environ.get("PATH", "")
    return ff_path, fp_path


def parse_rate(value: str) -> float:
    try:
        numerator, denominator = value.split("/", 1)
        rate = float(numerator) / float(denominator)
    except (AttributeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"invalid frame rate {value!r}") from exc
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError(f"invalid frame rate {value!r}")
    return rate



# The sandbox's ffmpeg/ffprobe are a STATIC build, and a static glibc binary cannot resolve hostnames: given an
# https:// input they fail with "Failed to resolve hostname <host>: System error" for every host (S3, CloudFront),
# while Python in the same sandbox resolves them fine (verified in the harness code interpreter, 2026-10-20; prod
# session f0c50162's reel stitch failed exactly so). So every remote input is downloaded by Python FIRST and the
# binaries only ever see local paths.
_STALL_SECONDS = 60  # a download that receives NOTHING for this long has stalled (not a cap on its length)


def localize_input(source: str, dest_dir: str, index: int = 0) -> str:
    """A local path for ``source``: unchanged when local; ``s3://`` via boto3 (the sandbox role); http(s) via Python."""
    import shutil
    import tempfile
    import urllib.parse
    import urllib.request

    src = (source or "").strip()
    if not src.startswith(("http://", "https://", "s3://")):
        return src
    parsed = urllib.parse.urlparse(src)
    name = os.path.basename(urllib.parse.unquote(parsed.path)) or "input"
    os.makedirs(dest_dir or tempfile.gettempdir(), exist_ok=True)
    local = os.path.join(dest_dir or tempfile.gettempdir(), f"{index:03d}_{name}")
    if os.path.isfile(local) and os.path.getsize(local) > 0:
        return local
    try:
        if src.startswith("s3://"):
            import boto3  # noqa: PLC0415 — only a sandbox run reaches here
            boto3.client("s3", region_name=os.environ.get("AWS_REGION") or None).download_file(
                parsed.netloc, parsed.path.lstrip("/"), local)
        else:
            with urllib.request.urlopen(src, timeout=_STALL_SECONDS) as resp, open(local, "wb") as out:  # noqa: S310
                shutil.copyfileobj(resp, out, 1 << 20)
    except Exception as exc:  # noqa: BLE001 — one clear error for the caller's existing handling
        if os.path.exists(local):
            os.remove(local)
        raise OSError(f"could not download {name} ({type(exc).__name__}: {exc})") from exc
    return local


def probe_media(source: str, ffprobe: str) -> dict[str, Any]:
    command = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
        "-of", "json",
        source,
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise ValueError(f"ffprobe failed for {source!r}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
        duration = float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"ffprobe returned invalid metadata for {source!r}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"clip duration must be positive and finite for {source!r}")

    streams = payload.get("streams") or []
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise ValueError(f"expected exactly one video stream for {source!r}, found {len(videos)}")
    if not audios:
        raise ValueError(f"native-audio clip has no audio stream: {source!r}")
    video = videos[0]
    try:
        width = int(video["width"])
        height = int(video["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"video dimensions unavailable for {source!r}") from exc
    if width <= 0 or height <= 0:
        raise ValueError(f"video dimensions must be positive for {source!r}")
    raw_rate = video.get("avg_frame_rate")
    if not raw_rate or raw_rate == "0/0":
        raw_rate = video.get("r_frame_rate")
    fps = parse_rate(raw_rate)
    return {
        "path": source,
        "duration": duration,
        "fps": fps,
        "width": width,
        "height": height,
        "audio_streams": len(audios),
    }


def load_clips(
    path: Path,
    ffprobe: str,
    *,
    expected_fps: float | None,
    duration_tolerance: float,
    fps_tolerance: float,
) -> tuple[list[dict[str, Any]], tuple[int, int]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    clips = data.get("clips") if isinstance(data, dict) else data
    if not isinstance(clips, list) or not clips:
        raise ValueError("manifest must contain a non-empty clips array")
    if duration_tolerance < 0 or fps_tolerance < 0:
        raise ValueError("metadata tolerances must be non-negative")

    normalized: list[dict[str, Any]] = []
    dimensions: tuple[int, int] | None = None
    for index, clip in enumerate(clips):
        # `path` is the documented field; `url`/`src`/`uri` name the same thing (a model wrote `url`,
        # was refused, and then downloaded every clip itself -- which this script already does).
        source = next((str(clip.get(k)).strip() for k in ("path", "url", "src", "uri")
                       if isinstance(clip, dict) and str(clip.get(k) or "").strip()), "")
        if not source:
            raise ValueError(f"clip {index} must contain a path (a local file, s3:// uri or https url)")
        # ffprobe/ffmpeg get the LOCAL copy (a static binary cannot resolve any hostname).
        local = localize_input(source, os.path.join(os.path.dirname(os.path.abspath(str(path))), "_inputs"), index)
        actual = probe_media(local, ffprobe)
        if local != source:
            actual["source"] = source
        expected_duration = clip.get("duration")
        if expected_duration is not None:
            try:
                expected_duration = float(expected_duration)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"clip {index} expected duration must be numeric") from exc
            if not math.isfinite(expected_duration) or expected_duration <= 0:
                raise ValueError(f"clip {index} expected duration must be positive and finite")
            if abs(actual["duration"] - expected_duration) > duration_tolerance:
                raise ValueError(
                    f"clip {index} duration mismatch: expected {expected_duration:.3f}s, "
                    f"ffprobe measured {actual['duration']:.3f}s"
                )
        # Only an EXPLICIT --fps is a contract. Without one the reel takes the clips' own rate
        # (output_fps): the filter graph conforms every input to it, so a 25 fps lip-sync model
        # is not an error (it was, against a hard-coded 24, and the stitch refused valid clips).
        if expected_fps is not None and abs(actual["fps"] - expected_fps) > fps_tolerance:
            raise ValueError(
                f"clip {index} frame-rate mismatch: expected {expected_fps:.6f}, "
                f"ffprobe measured {actual['fps']:.6f}"
            )
        current_dimensions = (actual["width"], actual["height"])
        if dimensions is None:
            dimensions = current_dimensions
        elif current_dimensions != dimensions:
            raise ValueError(
                f"clip {index} dimensions {current_dimensions} do not match first clip {dimensions}"
            )
        normalized.append(actual)

    assert dimensions is not None
    return normalized, dimensions


def build_filter(
    clips: list[dict[str, Any]],
    crossfade: float,
    *,
    fps: float,
    dimensions: tuple[int, int] | None = None,
) -> tuple[str, str, str, float]:
    if crossfade <= 0:
        raise ValueError("crossfade duration must be positive")
    if fps <= 0:
        raise ValueError("fps must be positive")
    if any(clip["duration"] <= crossfade for clip in clips):
        raise ValueError("every clip must be longer than the crossfade duration")

    geometry = ""
    if dimensions is not None:
        width, height = dimensions
        geometry = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        )

    parts: list[str] = []
    for index, clip in enumerate(clips):
        duration = clip["duration"]
        parts.append(
            f"[{index}:v]{geometry}fps={fps:.6f},settb=1/{fps:.6f},setpts=PTS-STARTPTS,"
            f"format=yuv420p[v{index}]"
        )
        parts.append(
            f"[{index}:a]aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS,"
            f"apad,atrim=0:{duration:.6f}[a{index}]"
        )

    video_label = "v0"
    audio_label = "a0"
    total = clips[0]["duration"]
    for index in range(1, len(clips)):
        offset = total - crossfade
        next_video = f"vx{index}"
        next_audio = f"ax{index}"
        parts.append(
            f"[{video_label}][v{index}]xfade=transition=fade:duration={crossfade:.6f}:"
            f"offset={offset:.6f}[{next_video}]"
        )
        parts.append(
            f"[{audio_label}][a{index}]acrossfade=d={crossfade:.6f}:c1=tri:c2=tri[{next_audio}]"
        )
        video_label = next_video
        audio_label = next_audio
        total += clips[index]["duration"] - crossfade

    return ";".join(parts), video_label, audio_label, total


def build_concat_filter(
    clips: list[dict[str, Any]],
    *,
    fps: float,
    dimensions: tuple[int, int] | None = None,
    pad_seconds: float = 1.0,
) -> tuple[str, str, str, float]:
    """Concatenate lip-sync clips (each carrying its own audio) with a held-frame breath.

    A held-frame pad is added to every clip's tail EXCEPT the last, so spoken lines get
    a beat between them instead of running together. Video freezes the last frame
    (``tpad=stop_mode=clone``) and audio appends matching silence (``apad``) by the same
    duration, so each segment stays A/V-aligned; then all segments are concatenated. No
    crossfade — lip-sync clips do not share an anchor frame, so a blend would smear two
    different mouth poses.
    """
    if fps <= 0:
        raise ValueError("fps must be positive")
    if pad_seconds < 0:
        raise ValueError("pad-seconds must be non-negative")

    geometry = ""
    if dimensions is not None:
        width, height = dimensions
        geometry = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        )

    parts: list[str] = []
    labels: list[str] = []
    last = len(clips) - 1
    for index in range(len(clips)):
        pad = pad_seconds if index < last else 0.0
        v_tail = f",tpad=stop_mode=clone:stop_duration={pad:.6f}" if pad > 0 else ""
        a_tail = f",apad=pad_dur={pad:.6f}" if pad > 0 else ""
        parts.append(
            f"[{index}:v]{geometry}fps={fps:.6f},settb=AVTB,setpts=PTS-STARTPTS,"
            f"format=yuv420p{v_tail}[v{index}]"
        )
        parts.append(
            f"[{index}:a]aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS{a_tail}[a{index}]"
        )
        labels.append(f"[v{index}][a{index}]")

    parts.append("".join(labels) + f"concat=n={len(clips)}:v=1:a=1[vout][aout]")
    total = sum(clip["duration"] for clip in clips) + pad_seconds * max(0, last)
    return ";".join(parts), "vout", "aout", total


def build_command(
    clips: list[dict[str, Any]],
    *,
    ffmpeg: str,
    output: Path,
    fps: float,
    dimensions: tuple[int, int] | None = None,
    stitch_mode: str = "concat",
    pad_seconds: float = 1.0,
    crossfade_frames: int = 3,
    video_args: list[str] | None = None,
) -> tuple[list[str], float]:
    if fps <= 0:
        raise ValueError("fps must be positive")
    if stitch_mode == "crossfade":
        if crossfade_frames <= 0:
            raise ValueError("crossfade-frames must be positive")
        crossfade = crossfade_frames / fps
        filter_graph, video_label, audio_label, total = build_filter(
            clips, crossfade, fps=fps, dimensions=dimensions,
        )
    else:
        filter_graph, video_label, audio_label, total = build_concat_filter(
            clips, fps=fps, dimensions=dimensions, pad_seconds=pad_seconds,
        )
    command = [ffmpeg, "-y"]
    for clip in clips:
        command.extend(["-i", clip["path"]])
    command.extend([
        "-filter_complex", filter_graph,
        "-map", f"[{video_label}]",
        "-map", f"[{audio_label}]",
        "-t", f"{total:.6f}",
        # From video_codec_args for the encoder the binary has; default: the harness build's.
        *(video_args if video_args is not None else video_codec_args("libopenh264")),
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output),
    ])
    return command, total


def output_fps(clips: list[dict[str, Any]], requested: float | None) -> float:
    """The reel's frame rate: ``requested`` when given, else the clips' most common rate (first wins a tie)."""
    if requested is not None:
        return requested
    rates = [round(float(c["fps"]), 3) for c in clips if c.get("fps")]
    if not rates:
        return 24.0
    return max(dict.fromkeys(rates), key=rates.count)


def output_target(output: str) -> tuple[Path, str | None]:
    """``(local file ffmpeg writes, s3:// uri to upload it to or None)``.

    An ``s3://`` output is rendered locally and uploaded. So is a RELATIVE ``projects/...`` or
    ``artifacts/...`` path -- the session output prefix written as a key -- into the bucket that
    ``FFMPEG_S3_URI`` names (the harness's workspace bucket): left local, it never reached S3, and the
    reel URL handed to the user was dead (staging c4a4f142).
    """
    text = str(output).strip()
    if text.startswith("s3://"):
        bucket, _, key = text[len("s3://"):].partition("/")
        if not bucket or not key or key.endswith("/"):
            raise ValueError(f"output {text!r} must name an object (s3://bucket/key.mp4)")
        return Path(tempfile.gettempdir()) / "reel-out" / Path(key).name, text
    if text.startswith(("projects/", "artifacts/")):
        ffmpeg_uri = os.environ.get("FFMPEG_S3_URI", "").strip()
        if ffmpeg_uri.startswith("s3://"):
            bucket = ffmpeg_uri[len("s3://"):].split("/", 1)[0]
            if bucket:
                return Path(text), f"s3://{bucket}/{text}"
    return Path(text), None


def upload_output(local: Path, s3_uri: str) -> None:
    """Upload the rendered reel with the sandbox's own credentials (the session output prefix is writable)."""
    bucket, _, key = s3_uri[len("s3://"):].partition("/")
    try:
        import boto3
        boto3.client("s3", region_name=os.environ.get("AWS_REGION") or None).upload_file(
            str(local), bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    except Exception as exc:  # noqa: BLE001 - boto3 errors alike -> one actionable message
        raise ValueError(f"the reel rendered but could not be uploaded to {s3_uri} ({exc}); write it to "
                         "the SESSION OUTPUT prefix the S3 CONTEXT names") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", help="s3://bucket/<session output prefix>/reel.mp4 (uploaded), or a local path")
    parser.add_argument("--fps", type=float, default=None,
                        help="Output frame rate; also REQUIRED of every clip. Default: the clips' own rate")
    parser.add_argument("--fps-tolerance", type=float, default=0.05)
    parser.add_argument("--duration-tolerance", type=float, default=0.1)
    parser.add_argument("--crossfade-frames", type=int, default=3)
    parser.add_argument(
        "--stitch-mode", choices=("concat", "crossfade"), default="concat",
        help="concat (default): lip-sync clips joined with a held-frame breath pad; "
             "crossfade: legacy anchor-pinned dissolve",
    )
    parser.add_argument(
        "--pad-seconds", type=float, default=1.0,
        help="Held-frame 'breath' on each clip's tail except the last, in concat mode",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--execute", action="store_true", help="Execute ffmpeg; otherwise print the verified command contract")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.timeout <= 0:
            raise ValueError("timeout must be positive")
        # Resolve (and, in the code-interpreter sandbox, provision) ffmpeg + ffprobe.
        # ffprobe is required even for a dry-run contract, because load_clips probes.
        ffmpeg_bin, ffprobe_bin = _ensure_ffmpeg_ffprobe(args.ffmpeg, args.ffprobe)
        clips, dimensions = load_clips(
            args.manifest,
            ffprobe_bin,
            expected_fps=args.fps,
            duration_tolerance=args.duration_tolerance,
            fps_tolerance=args.fps_tolerance,
        )
        fps = output_fps(clips, args.fps)
        local_output, upload_to = output_target(args.output)
        command, total = build_command(
            clips,
            ffmpeg=ffmpeg_bin,
            output=local_output,
            fps=fps,
            dimensions=dimensions,
            stitch_mode=args.stitch_mode,
            pad_seconds=args.pad_seconds,
            crossfade_frames=args.crossfade_frames,
            video_args=video_codec_args(h264_encoder(ffmpeg_bin)),
        )
        report = {
            "clips": len(clips),
            "expected_duration": total,
            "dimensions": {"width": dimensions[0], "height": dimensions[1]},
            "fps": fps,
            "stitch_mode": args.stitch_mode,
            "pad_seconds": args.pad_seconds if args.stitch_mode == "concat" else 0.0,
            "source_metadata": clips,
            "output": upload_to or str(local_output),
            "command": command,
            "executed": bool(args.execute),
        }
        if args.execute:
            local_output.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
            if result.returncode != 0:
                raise ValueError(f"ffmpeg failed: {result.stderr[-2000:].strip()}")
            report["bytes"] = local_output.stat().st_size
            if upload_to:
                upload_output(local_output, upload_to)
                report["uploaded"] = True
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
