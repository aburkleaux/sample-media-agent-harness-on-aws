"""Peak Extractor tool — extract normalized amplitude peaks from audio.

Streams audio from a URL, decodes WAV files using the standard library `wave`
module, and computes per-window maximum amplitude peaks normalized to [0.0, 1.0].
For non-WAV formats that require ffmpeg, returns a CODEC_ERROR so the agent can
fall back to code_interpreter.

Includes a `resample_peaks` utility that uses linear interpolation to resize
peak arrays to a target length.
"""
from __future__ import annotations

import io
import json
import logging
import struct
import time
import wave
from typing import Any
import requests
from strands import tool

from security import url_guard

logger = logging.getLogger(__name__)

# Maximum time budget for the entire peak extraction pipeline (seconds).
_TIMEOUT_SECONDS = 30

# Upper bound on a downloaded audio body (memory guard; ~1h of 48 kHz 24-bit stereo WAV).
_MAX_AUDIO_BYTES = 1024 * 1024 * 1024

# Supported audio extensions that can be decoded with stdlib `wave`.
_WAV_EXTENSIONS = {"wav"}

# Extensions that require external codecs (ffmpeg) — not available in agent process.
_CODEC_REQUIRED_EXTENSIONS = {"mp3", "flac", "ogg", "aac", "m4a", "wma"}


# ---------------------------------------------------------------------------
# Resampling utility
# ---------------------------------------------------------------------------


def resample_peaks(peaks: list[float], target_length: int) -> list[float]:
    """Resample a peaks array to a target length using linear interpolation.

    Args:
        peaks: Input array of normalized amplitude values.
        target_length: Desired output length (must be >= 1).

    Returns:
        A new list of length `target_length` with values bounded within
        [min(peaks), max(peaks)]. If input is empty, returns a list of zeros.
    """
    if target_length < 1:
        return []
    if not peaks:
        return [0.0] * target_length
    if len(peaks) == 1:
        return [peaks[0]] * target_length
    if len(peaks) == target_length:
        return list(peaks)

    n = len(peaks)
    result = []
    for i in range(target_length):
        # Map output index to input position (floating point)
        if target_length == 1:
            pos = 0.0
        else:
            pos = i * (n - 1) / (target_length - 1)

        # Linear interpolation between adjacent input samples
        idx_low = int(pos)
        idx_high = min(idx_low + 1, n - 1)
        frac = pos - idx_low

        value = peaks[idx_low] * (1.0 - frac) + peaks[idx_high] * frac
        result.append(value)

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _detect_extension(url: str) -> str:
    """Extract file extension from URL (lowercased, without dot)."""
    # Strip query params and fragments
    path = url.split("?")[0].split("#")[0]
    # Get just the last path segment (filename)
    segment = path.rsplit("/", 1)[-1] if "/" in path else path
    if "." in segment:
        return segment.rsplit(".", 1)[-1].lower()
    return ""


def _download_audio(url: str, timeout: float) -> tuple[bytes, float]:
    """Download audio bytes from URL within the given timeout.

    Returns:
        Tuple of (audio_bytes, elapsed_seconds).

    Raises:
        TimeoutError: If download exceeds timeout.
        ConnectionError: If URL is unreachable.
    """
    start = time.monotonic()
    from media.artifact_fetch import is_own_artifact, read_artifact_bytes

    if is_own_artifact(url):
        # Our artifact -> get_object by key (the CDN may be private to unsigned GETs).
        try:
            data = read_artifact_bytes(url)
        except Exception as e:  # noqa: BLE001 - same contract as the HTTP path below
            raise ConnectionError(f"Artifact unreadable from S3: {e}") from e
        return data, time.monotonic() - start
    # Model-supplied url: through the shared SSRF guard (https, public addresses only, pinned).
    try:
        fetched = url_guard.fetch(
            url, max_bytes=_MAX_AUDIO_BYTES, timeout=min(timeout, 10.0), deadline_s=timeout,
            headers={"User-Agent": "RenderAgent/1.0"})
        return fetched.body, time.monotonic() - start
    except TimeoutError as e:
        raise TimeoutError(f"Download exceeded {timeout}s budget") from e
    except url_guard.BlockedURLError as e:
        raise ConnectionError(f"URL refused: {e}") from e
    except requests.HTTPError as e:
        code = getattr(e.response, "status_code", "?")
        reason = getattr(e.response, "reason", "")
        raise ConnectionError(f"HTTP {code}: {reason}") from e
    except requests.Timeout as e:
        raise TimeoutError(f"Download exceeded {timeout}s budget") from e
    except (requests.RequestException, ValueError) as e:
        raise ConnectionError(f"URL unreachable: {e}") from e
    except OSError as e:
        raise ConnectionError(f"Network error: {e}") from e


def _extract_wav_peaks(audio_bytes: bytes, samples_per_second: int) -> dict[str, Any]:
    """Extract peaks from WAV audio bytes.

    Returns a dict with: peaks, sample_rate, duration, samples_per_second.
    """
    buf = io.BytesIO(audio_bytes)
    with wave.open(buf, "rb") as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        frame_rate = wf.getframerate()
        n_frames = wf.getnframes()

        if n_frames == 0:
            return {
                "peaks": [],
                "sample_rate": frame_rate,
                "duration": 0.0,
                "samples_per_second": samples_per_second,
            }

        duration = n_frames / frame_rate

        # Read all frames
        raw_data = wf.readframes(n_frames)

    # Determine struct format based on sample width
    if sample_width == 1:
        # 8-bit unsigned
        fmt = "B"
        max_val = 128.0  # center at 128 for unsigned
        offset = 128
    elif sample_width == 2:
        # 16-bit signed little-endian
        fmt = "<h"
        max_val = 32768.0
        offset = 0
    elif sample_width == 3:
        # 24-bit — handle manually
        fmt = None
        max_val = 8388608.0
        offset = 0
    elif sample_width == 4:
        # 32-bit signed
        fmt = "<i"
        max_val = 2147483648.0
        offset = 0
    else:
        raise ValueError(f"Unsupported sample width: {sample_width}")

    # Convert raw bytes to mono float samples normalized to [-1.0, 1.0]
    total_samples = n_frames * n_channels
    samples: list[float] = []

    if fmt and sample_width != 3:
        # Use struct for 1, 2, or 4 byte samples
        frame_size = sample_width * n_channels
        for frame_idx in range(n_frames):
            frame_offset = frame_idx * frame_size
            # Average channels to mono
            mono_sum = 0.0
            for ch in range(n_channels):
                ch_offset = frame_offset + ch * sample_width
                sample_bytes = raw_data[ch_offset:ch_offset + sample_width]
                if len(sample_bytes) < sample_width:
                    break
                if sample_width == 1:
                    val = struct.unpack(fmt, sample_bytes)[0]
                    mono_sum += (val - offset) / max_val
                else:
                    val = struct.unpack(fmt, sample_bytes)[0]
                    mono_sum += val / max_val
            samples.append(mono_sum / n_channels)
    elif sample_width == 3:
        # 24-bit: 3 bytes little-endian signed
        frame_size = 3 * n_channels
        for frame_idx in range(n_frames):
            frame_offset = frame_idx * frame_size
            mono_sum = 0.0
            for ch in range(n_channels):
                ch_offset = frame_offset + ch * 3
                b = raw_data[ch_offset:ch_offset + 3]
                if len(b) < 3:
                    break
                # Unpack as little-endian 24-bit signed
                val = b[0] | (b[1] << 8) | (b[2] << 16)
                if val >= 0x800000:
                    val -= 0x1000000
                mono_sum += val / max_val
            samples.append(mono_sum / n_channels)

    # Compute peaks per window
    # window_size = number of audio samples per peak
    target_peak_count = max(1, int(duration * samples_per_second))
    window_size = max(1, len(samples) // target_peak_count)

    peaks: list[float] = []
    for i in range(0, len(samples), window_size):
        window = samples[i:i + window_size]
        peak = max(abs(s) for s in window)
        # Clamp to [0.0, 1.0]
        peaks.append(min(1.0, max(0.0, peak)))

    # Resample to exact target count if needed
    if len(peaks) != target_peak_count:
        peaks = resample_peaks(peaks, target_peak_count)

    return {
        "peaks": peaks,
        "sample_rate": frame_rate,
        "duration": round(duration, 3),
        "samples_per_second": samples_per_second,
    }


# ---------------------------------------------------------------------------
# Strands @tool entry point
# ---------------------------------------------------------------------------


@tool
def peak_extractor(url: str, samples_per_second: int = 100) -> str:
    """Extract normalized amplitude peaks from audio for waveform visualization.

    Downloads audio from the given URL, decodes the audio data, and computes
    amplitude peaks normalized to 0.0–1.0 at the specified resolution.

    Args:
        url: Audio file URL (CloudFront, S3 presigned, or HTTP).
        samples_per_second: Peak resolution — number of peaks per second of audio.
            Default 100 means 1 peak per 10ms. Range: 1–1000.

    Returns:
        JSON string with either:
        - Success: {"peaks": [...], "sample_rate": int, "duration": float, "samples_per_second": int}
        - Error: {"error": {"code": "TIMEOUT"|"UNREACHABLE"|"CODEC_ERROR", "message": str}}
    """
    # Validate samples_per_second
    samples_per_second = max(1, min(1000, samples_per_second))

    # Detect format from URL
    ext = _detect_extension(url)

    # Check if format requires external codec
    if ext in _CODEC_REQUIRED_EXTENSIONS:
        return json.dumps({
            "error": {
                "code": "CODEC_ERROR",
                "message": (
                    f"Format '.{ext}' requires an external audio codec (ffmpeg) "
                    "which is not available in the agent process. Use code_interpreter "
                    "for non-WAV audio formats."
                ),
            }
        })

    # Check if format is supported at all
    if ext and ext not in _WAV_EXTENSIONS and ext not in _CODEC_REQUIRED_EXTENSIONS:
        return json.dumps({
            "error": {
                "code": "CODEC_ERROR",
                "message": (
                    f"Unsupported audio format '.{ext}'. "
                    "Agent-side extraction supports WAV only. "
                    "Use code_interpreter for other formats."
                ),
            }
        })

    start_time = time.monotonic()

    # Download audio
    try:
        audio_bytes, download_elapsed = _download_audio(url, _TIMEOUT_SECONDS)
    except TimeoutError:
        return json.dumps({
            "error": {
                "code": "TIMEOUT",
                "message": "Audio download exceeded 30-second budget.",
            }
        })
    except ConnectionError as e:
        return json.dumps({
            "error": {
                "code": "UNREACHABLE",
                "message": str(e),
            }
        })

    # Check remaining time budget
    remaining = _TIMEOUT_SECONDS - (time.monotonic() - start_time)
    if remaining <= 0:
        return json.dumps({
            "error": {
                "code": "TIMEOUT",
                "message": "Time budget exhausted after download.",
            }
        })

    # Decode and extract peaks
    try:
        result = _extract_wav_peaks(audio_bytes, samples_per_second)
    except wave.Error as e:
        return json.dumps({
            "error": {
                "code": "CODEC_ERROR",
                "message": f"Failed to decode WAV audio: {e}",
            }
        })
    except (struct.error, ValueError, EOFError) as e:
        return json.dumps({
            "error": {
                "code": "CODEC_ERROR",
                "message": f"Failed to decode audio data: {e}",
            }
        })
    except Exception as e:
        logger.exception("Unexpected error during peak extraction")
        return json.dumps({
            "error": {
                "code": "CODEC_ERROR",
                "message": f"Unexpected decoding error: {type(e).__name__}: {e}",
            }
        })

    # Check if we exceeded budget during processing
    total_elapsed = time.monotonic() - start_time
    if total_elapsed > _TIMEOUT_SECONDS:
        # Return partial peaks if we have any
        if result.get("peaks"):
            result["partial"] = True
            return json.dumps(result)
        return json.dumps({
            "error": {
                "code": "TIMEOUT",
                "message": "Peak extraction exceeded 30-second budget.",
            }
        })

    return json.dumps(result)
