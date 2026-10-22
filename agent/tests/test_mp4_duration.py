"""Duration from the container header, so a 30s clip stops drawing on a 600s timeline.

`media_probe` returns `duration: None` by design (no ffprobe in the render runtime)
and peak extraction only covers WAV, so video renders fell back to a fixed 600s hint.
The value is already in the file's `mvhd` box.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tools.mp4_duration import find_mvhd_duration, parse_mvhd_duration  # noqa: E402


def mvhd_v0(timescale: int, duration: int) -> bytes:
    return (b"mvhd" + bytes([0]) + b"\x00\x00\x00"
            + struct.pack(">I", 0) + struct.pack(">I", 0)
            + struct.pack(">I", timescale) + struct.pack(">I", duration)
            + b"\x00" * 80)


def mvhd_v1(timescale: int, duration: int) -> bytes:
    return (b"mvhd" + bytes([1]) + b"\x00\x00\x00"
            + struct.pack(">Q", 0) + struct.pack(">Q", 0)
            + struct.pack(">I", timescale) + struct.pack(">Q", duration)
            + b"\x00" * 80)


class TestParse:
    def test_reads_a_version0_duration(self):
        # 30.03s at the 1000-tick timescale these tools emit
        assert parse_mvhd_duration(b"\x00" * 64 + mvhd_v0(1000, 30030)) == 30.03

    def test_reads_a_version1_duration(self):
        assert parse_mvhd_duration(mvhd_v1(90000, 90000 * 300)) == 300.0

    def test_handles_the_common_600_and_44100_timescales(self):
        assert parse_mvhd_duration(mvhd_v0(600, 18018)) == 30.03
        assert parse_mvhd_duration(mvhd_v0(44100, 44100 * 12)) == 12.0

    def test_absent_box_is_none_not_an_error(self):
        assert parse_mvhd_duration(b"ftypisom" + b"\x00" * 200) is None
        assert parse_mvhd_duration(b"") is None
        assert parse_mvhd_duration(b"mvhd") is None

    def test_rejects_a_truncated_box(self):
        assert parse_mvhd_duration(b"mvhd\x00\x00\x00\x00\x00\x00") is None

    def test_rejects_implausible_and_sentinel_values(self):
        assert parse_mvhd_duration(mvhd_v0(1000, 0)) is None          # zero
        assert parse_mvhd_duration(mvhd_v0(0, 30030)) is None         # zero timescale
        assert parse_mvhd_duration(mvhd_v0(1, 0xFFFFFFFF)) is None    # unknown sentinel
        assert parse_mvhd_duration(mvhd_v0(1, 10 * 24 * 3600)) is None  # 10 days

    def test_keeps_scanning_past_a_coincidental_match(self):
        """The bytes "mvhd" can occur in media data; a real box must still be found."""
        junk = b"mvhd" + b"\x00" * 2          # too short to parse
        assert parse_mvhd_duration(junk + mvhd_v0(1000, 5000)) == 5.0


class TestRangedLookup:
    def test_finds_it_in_a_faststart_head(self):
        head = b"\x00\x00\x00\x18ftypisom" + mvhd_v0(1000, 30030)

        def fetch(start, length):
            assert start == 0
            return head
        assert find_mvhd_duration(fetch) == 30.03

    def test_falls_back_to_the_tail_when_moov_is_last(self):
        calls = []

        def fetch(start, length):
            calls.append(start)
            return b"\x00" * 4096 if start == 0 else mvhd_v0(600, 18018)
        assert find_mvhd_duration(fetch) == 30.03
        assert calls[0] == 0 and calls[1] < 0, "suffix range expected for the tail"

    def test_returns_none_when_neither_end_has_it(self):
        assert find_mvhd_duration(lambda s, l: b"\x00" * 1024) is None

    def test_a_failing_fetch_does_not_raise(self):
        def boom(start, length):
            raise OSError("network")
        assert find_mvhd_duration(boom) is None

    def test_tolerates_empty_reads(self):
        assert find_mvhd_duration(lambda s, l: None) is None
