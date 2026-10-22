"""Unit tests for peak_extractor.py — validates resample_peaks and extraction logic."""
import io
import json
import struct
import wave
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from unittest.mock import patch

import pytest

from tools.peak_extractor import (
    resample_peaks,
    _detect_extension,
    _extract_wav_peaks,
    peak_extractor,
)


# ---------------------------------------------------------------------------
# resample_peaks tests
# ---------------------------------------------------------------------------


class TestResamplePeaks:
    """Tests for the linear interpolation resampling function."""

    def test_empty_input_returns_zeros(self):
        result = resample_peaks([], 5)
        assert result == [0.0, 0.0, 0.0, 0.0, 0.0]

    def test_empty_input_single_target(self):
        result = resample_peaks([], 1)
        assert result == [0.0]

    def test_target_zero_returns_empty(self):
        result = resample_peaks([0.5, 0.8], 0)
        assert result == []

    def test_same_length_returns_copy(self):
        peaks = [0.1, 0.5, 0.9]
        result = resample_peaks(peaks, 3)
        assert result == peaks
        # Should be a new list, not the same reference
        assert result is not peaks

    def test_single_input_repeated(self):
        result = resample_peaks([0.7], 4)
        assert result == [0.7, 0.7, 0.7, 0.7]

    def test_upsample_two_to_three(self):
        # [0.0, 1.0] → 3 samples: [0.0, 0.5, 1.0]
        result = resample_peaks([0.0, 1.0], 3)
        assert len(result) == 3
        assert abs(result[0] - 0.0) < 1e-10
        assert abs(result[1] - 0.5) < 1e-10
        assert abs(result[2] - 1.0) < 1e-10

    def test_downsample_four_to_two(self):
        # [0.0, 0.33, 0.67, 1.0] → 2 samples: [0.0, 1.0]
        result = resample_peaks([0.0, 0.33, 0.67, 1.0], 2)
        assert len(result) == 2
        assert abs(result[0] - 0.0) < 1e-10
        assert abs(result[1] - 1.0) < 1e-10

    def test_output_bounded_by_input_range(self):
        """Output values should be within [min(input), max(input)]."""
        peaks = [0.2, 0.8, 0.4, 0.6]
        result = resample_peaks(peaks, 10)
        assert len(result) == 10
        for v in result:
            assert v >= min(peaks) - 1e-10
            assert v <= max(peaks) + 1e-10

    def test_output_exact_length(self):
        peaks = [0.1 * i for i in range(20)]
        result = resample_peaks(peaks, 7)
        assert len(result) == 7

    def test_upsample_preserves_endpoints(self):
        peaks = [0.2, 0.4, 0.6, 0.8]
        result = resample_peaks(peaks, 100)
        assert abs(result[0] - 0.2) < 1e-10
        assert abs(result[-1] - 0.8) < 1e-10

    def test_large_upsample(self):
        peaks = [0.0, 1.0]
        result = resample_peaks(peaks, 1001)
        assert len(result) == 1001
        # Should be monotonically increasing
        for i in range(len(result) - 1):
            assert result[i] <= result[i + 1] + 1e-10


# ---------------------------------------------------------------------------
# _detect_extension tests
# ---------------------------------------------------------------------------


class TestDetectExtension:
    def test_simple_wav(self):
        assert _detect_extension("https://cdn.example.com/audio.wav") == "wav"

    def test_with_query_params(self):
        assert _detect_extension("https://cdn.example.com/audio.mp3?token=abc") == "mp3"

    def test_with_fragment(self):
        assert _detect_extension("https://cdn.example.com/song.flac#t=10") == "flac"

    def test_uppercase_extension(self):
        assert _detect_extension("https://cdn.example.com/AUDIO.WAV") == "wav"

    def test_no_extension(self):
        assert _detect_extension("https://cdn.example.com/audio") == ""

    def test_nested_path(self):
        assert _detect_extension("https://cdn.example.com/path/to/file.wav") == "wav"


# ---------------------------------------------------------------------------
# WAV generation helper
# ---------------------------------------------------------------------------


def _make_wav_bytes(
    n_frames: int = 4410,
    n_channels: int = 1,
    sample_width: int = 2,
    frame_rate: int = 44100,
    amplitude: float = 0.5,
) -> bytes:
    """Generate a simple WAV file in memory with a constant amplitude sine-like signal."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(frame_rate)

        # Generate samples: alternating positive/negative at given amplitude
        max_val = (2 ** (sample_width * 8 - 1)) - 1
        sample_val = int(amplitude * max_val)

        frames = bytearray()
        for i in range(n_frames):
            val = sample_val if i % 2 == 0 else -sample_val
            for _ch in range(n_channels):
                if sample_width == 2:
                    frames.extend(struct.pack("<h", val))
                elif sample_width == 1:
                    # 8-bit is unsigned, center at 128
                    unsigned_val = val + 128
                    frames.extend(struct.pack("B", max(0, min(255, unsigned_val))))
                elif sample_width == 4:
                    frames.extend(struct.pack("<i", val))

        wf.writeframes(bytes(frames))

    return buf.getvalue()


# ---------------------------------------------------------------------------
# _extract_wav_peaks tests
# ---------------------------------------------------------------------------


class TestExtractWavPeaks:
    def test_basic_extraction(self):
        # 1 second of audio at 44100 Hz, 100 samples per second
        wav_bytes = _make_wav_bytes(n_frames=44100, frame_rate=44100, amplitude=0.8)
        result = _extract_wav_peaks(wav_bytes, samples_per_second=100)

        assert "peaks" in result
        assert "sample_rate" in result
        assert "duration" in result
        assert "samples_per_second" in result

        assert result["sample_rate"] == 44100
        assert abs(result["duration"] - 1.0) < 0.01
        assert result["samples_per_second"] == 100
        # Should have approximately 100 peaks for 1 second
        assert len(result["peaks"]) == 100

    def test_peaks_normalized_to_unit_range(self):
        wav_bytes = _make_wav_bytes(n_frames=44100, amplitude=0.5)
        result = _extract_wav_peaks(wav_bytes, samples_per_second=50)

        for peak in result["peaks"]:
            assert 0.0 <= peak <= 1.0

    def test_empty_audio(self):
        wav_bytes = _make_wav_bytes(n_frames=0)
        result = _extract_wav_peaks(wav_bytes, samples_per_second=100)

        assert result["peaks"] == []
        assert result["duration"] == 0.0

    def test_stereo_audio(self):
        wav_bytes = _make_wav_bytes(n_frames=44100, n_channels=2, amplitude=0.6)
        result = _extract_wav_peaks(wav_bytes, samples_per_second=50)

        assert result["sample_rate"] == 44100
        assert len(result["peaks"]) == 50
        for peak in result["peaks"]:
            assert 0.0 <= peak <= 1.0

    def test_different_sample_rates(self):
        # 22050 Hz sample rate, 0.5 seconds
        wav_bytes = _make_wav_bytes(n_frames=11025, frame_rate=22050, amplitude=0.7)
        result = _extract_wav_peaks(wav_bytes, samples_per_second=100)

        assert result["sample_rate"] == 22050
        assert abs(result["duration"] - 0.5) < 0.01
        # ~50 peaks for 0.5 seconds at 100 sps
        assert len(result["peaks"]) == 50


# ---------------------------------------------------------------------------
# peak_extractor tool integration tests
# ---------------------------------------------------------------------------


class TestPeakExtractorTool:
    """Integration tests for the @tool entry point.

    Strands @tool wraps the function; access the underlying function via `._tool_func`.
    """

    def _call_tool(self, **kwargs) -> dict:
        """Call the underlying tool function and parse the JSON result."""
        result_str = peak_extractor._tool_func(**kwargs)
        return json.loads(result_str)

    def test_codec_error_for_mp3(self):
        result = self._call_tool(
            url="https://cdn.example.com/song.mp3",
            samples_per_second=100,
        )
        assert "error" in result
        assert result["error"]["code"] == "CODEC_ERROR"
        assert "mp3" in result["error"]["message"].lower()

    def test_codec_error_for_flac(self):
        result = self._call_tool(
            url="https://cdn.example.com/audio.flac",
            samples_per_second=100,
        )
        assert "error" in result
        assert result["error"]["code"] == "CODEC_ERROR"

    def test_codec_error_for_unknown_format(self):
        result = self._call_tool(
            url="https://cdn.example.com/audio.xyz",
            samples_per_second=100,
        )
        assert "error" in result
        assert result["error"]["code"] == "CODEC_ERROR"

    def test_unreachable_url(self):
        result = self._call_tool(
            url="https://nonexistent.invalid/audio.wav",
            samples_per_second=100,
        )
        assert "error" in result
        assert result["error"]["code"] == "UNREACHABLE"

    def test_successful_extraction_with_local_server(self):
        """Full pipeline: download (through the SSRF guard) -> decode -> peaks.

        Used to serve the WAV from a 127.0.0.1 HTTP server. Loopback is exactly what the
        shared guard (security.url_guard) refuses, so the local server now proves the
        refusal, and the happy path fakes the guard's transport behind a public https url.
        """
        wav_bytes = _make_wav_bytes(n_frames=44100, frame_rate=44100, amplitude=0.6)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav_bytes)))
                self.end_headers()
                self.wfile.write(wav_bytes)

            def log_message(self, format, *args):
                pass  # Suppress output

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            refused = self._call_tool(url=f"http://127.0.0.1:{port}/audio.wav",
                                      samples_per_second=100)
            assert refused["error"]["code"] == "UNREACHABLE"
        finally:
            server.shutdown()

        from security.url_guard import FetchResult

        def fake_fetch(url, *, max_bytes, **kw):
            assert url == "https://cdn.example.com/audio.wav"
            return FetchResult(status=200, headers={"Content-Type": "audio/wav"},
                               body=wav_bytes, url=url)

        with patch("tools.peak_extractor.url_guard.fetch", fake_fetch):
            result = self._call_tool(
                url="https://cdn.example.com/audio.wav",
                samples_per_second=100,
            )
        assert "error" not in result
        assert "peaks" in result
        assert result["sample_rate"] == 44100
        assert abs(result["duration"] - 1.0) < 0.01
        assert len(result["peaks"]) == 100
        for peak in result["peaks"]:
            assert 0.0 <= peak <= 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
