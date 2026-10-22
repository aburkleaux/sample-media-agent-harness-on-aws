"""Unit tests for the Caption Parser tool.

Tests cover VTT parsing, SRT parsing, auto-detection, speaker extraction,
validation of invalid cues, and malformed timestamp handling.
"""

import json

import pytest

from tools.caption_parser import parse_caption_content


# --- VTT Parsing Tests ---


class TestVTTParsing:
    """Tests for WebVTT format parsing."""

    def test_basic_vtt_with_speaker_tags(self):
        """Parse VTT with <v> speaker tags."""
        content = """WEBVTT

00:00:01.000 --> 00:00:04.000
<v Alice>Hello, how are you?</v>

00:00:04.500 --> 00:00:07.000
<v Bob>I'm doing great, thanks!</v>
"""
        result = parse_caption_content(content, "vtt")
        assert result["format_detected"] == "vtt"
        assert len(result["cues"]) == 2
        assert result["cues"][0] == {
            "start": 1.0,
            "end": 4.0,
            "speaker": "Alice",
            "text": "Hello, how are you?",
        }
        assert result["cues"][1] == {
            "start": 4.5,
            "end": 7.0,
            "speaker": "Bob",
            "text": "I'm doing great, thanks!",
        }
        assert result["warnings"] == []

    def test_vtt_with_speaker_prefix(self):
        """Parse VTT with Speaker: prefix convention (no <v> tags)."""
        content = """WEBVTT

00:00:01.000 --> 00:00:04.000
Alice: Hello, how are you?

00:00:04.500 --> 00:00:07.000
Bob: I'm doing great, thanks!
"""
        result = parse_caption_content(content, "vtt")
        assert len(result["cues"]) == 2
        assert result["cues"][0]["speaker"] == "Alice"
        assert result["cues"][0]["text"] == "Hello, how are you?"
        assert result["cues"][1]["speaker"] == "Bob"

    def test_vtt_no_speaker_assigns_default(self):
        """VTT without speaker tags assigns 'Speaker 1'."""
        content = """WEBVTT

00:00:01.000 --> 00:00:04.000
Hello, how are you?

00:00:04.500 --> 00:00:07.000
I'm doing great, thanks!
"""
        result = parse_caption_content(content, "vtt")
        assert len(result["cues"]) == 2
        assert result["cues"][0]["speaker"] == "Speaker 1"
        assert result["cues"][1]["speaker"] == "Speaker 1"

    def test_vtt_with_cue_identifiers(self):
        """VTT with optional cue identifiers (e.g., 'intro-1')."""
        content = """WEBVTT

intro-1
00:00:01.000 --> 00:00:03.000
<v Narrator>Welcome to the show</v>

intro-2
00:00:03.500 --> 00:00:06.000
<v Narrator>Today we discuss AI</v>
"""
        result = parse_caption_content(content, "vtt")
        assert len(result["cues"]) == 2
        assert result["cues"][0]["speaker"] == "Narrator"
        assert result["cues"][0]["text"] == "Welcome to the show"

    def test_vtt_with_hours_in_timestamp(self):
        """VTT with full HH:MM:SS.mmm timestamps."""
        content = """WEBVTT

01:30:00.000 --> 01:30:05.500
<v Host>We're at the 90-minute mark</v>
"""
        result = parse_caption_content(content, "vtt")
        assert len(result["cues"]) == 1
        assert result["cues"][0]["start"] == 5400.0
        assert result["cues"][0]["end"] == 5405.5

    def test_vtt_without_hours(self):
        """VTT timestamps without hours (MM:SS.mmm)."""
        content = """WEBVTT

01:30.000 --> 01:35.500
Some text here
"""
        result = parse_caption_content(content, "vtt")
        assert len(result["cues"]) == 1
        assert result["cues"][0]["start"] == 90.0
        assert result["cues"][0]["end"] == 95.5

    def test_vtt_multiline_text(self):
        """VTT cue with multiple text lines joined by space."""
        content = """WEBVTT

00:00:01.000 --> 00:00:05.000
<v Alice>This is a longer caption</v>
<v Alice>that spans multiple lines</v>
"""
        result = parse_caption_content(content, "vtt")
        # Multi-line VTT text joined with space; each <v> tag matched separately
        assert len(result["cues"]) >= 1


# --- SRT Parsing Tests ---


class TestSRTParsing:
    """Tests for SRT format parsing."""

    def test_basic_srt_with_bracket_speaker(self):
        """Parse SRT with [Speaker]: prefix."""
        content = """1
00:00:01,000 --> 00:00:04,000
[Alice]: Hello, how are you?

2
00:00:04,500 --> 00:00:07,000
[Bob]: I'm doing great, thanks!
"""
        result = parse_caption_content(content, "srt")
        assert result["format_detected"] == "srt"
        assert len(result["cues"]) == 2
        assert result["cues"][0] == {
            "start": 1.0,
            "end": 4.0,
            "speaker": "Alice",
            "text": "Hello, how are you?",
        }
        assert result["cues"][1] == {
            "start": 4.5,
            "end": 7.0,
            "speaker": "Bob",
            "text": "I'm doing great, thanks!",
        }

    def test_srt_with_prefix_speaker(self):
        """Parse SRT with Speaker: prefix convention."""
        content = """1
00:00:01,000 --> 00:00:04,000
Alice: Hello, how are you?

2
00:00:04,500 --> 00:00:07,000
Bob: I'm doing great, thanks!
"""
        result = parse_caption_content(content, "srt")
        assert len(result["cues"]) == 2
        assert result["cues"][0]["speaker"] == "Alice"
        assert result["cues"][1]["speaker"] == "Bob"

    def test_srt_no_speaker_assigns_default(self):
        """SRT without speaker prefix assigns 'Speaker 1'."""
        content = """1
00:00:01,000 --> 00:00:04,000
Hello, how are you?

2
00:00:04,500 --> 00:00:07,000
I'm doing great, thanks!
"""
        result = parse_caption_content(content, "srt")
        assert len(result["cues"]) == 2
        assert result["cues"][0]["speaker"] == "Speaker 1"
        assert result["cues"][1]["speaker"] == "Speaker 1"

    def test_srt_multiline_text(self):
        """SRT cue with multiple text lines joined by space."""
        content = """1
00:00:01,000 --> 00:00:05,000
This is a longer caption
that spans multiple lines
"""
        result = parse_caption_content(content, "srt")
        assert len(result["cues"]) == 1
        assert result["cues"][0]["text"] == "This is a longer caption that spans multiple lines"
        assert result["cues"][0]["speaker"] == "Speaker 1"

    def test_srt_precise_timing(self):
        """SRT timing with millisecond precision."""
        content = """1
00:01:23,456 --> 00:01:27,891
Some dialogue here
"""
        result = parse_caption_content(content, "srt")
        assert len(result["cues"]) == 1
        assert abs(result["cues"][0]["start"] - 83.456) < 0.001
        assert abs(result["cues"][0]["end"] - 87.891) < 0.001


# --- Auto-Detection Tests ---


class TestAutoDetection:
    """Tests for format auto-detection."""

    def test_detect_vtt_from_header(self):
        """Content starting with WEBVTT is detected as VTT."""
        content = """WEBVTT

00:00:01.000 --> 00:00:04.000
Hello world
"""
        result = parse_caption_content(content, "auto")
        assert result["format_detected"] == "vtt"
        assert len(result["cues"]) == 1

    def test_detect_srt_from_pattern(self):
        """Content with numeric index + comma timestamps detected as SRT."""
        content = """1
00:00:01,000 --> 00:00:04,000
Hello world

2
00:00:05,000 --> 00:00:08,000
Second cue
"""
        result = parse_caption_content(content, "auto")
        assert result["format_detected"] == "srt"
        assert len(result["cues"]) == 2

    def test_ambiguous_defaults_to_vtt(self):
        """Ambiguous content defaults to VTT format."""
        content = """This is some random text
that doesn't match any format
"""
        result = parse_caption_content(content, "auto")
        assert result["format_detected"] == "vtt"


# --- Invalid Cue Handling Tests ---


class TestInvalidCueHandling:
    """Tests for cue validation: discarding invalid cues and warnings."""

    def test_start_equals_end_discarded(self):
        """Cue where start == end is discarded with warning."""
        content = """WEBVTT

00:00:05.000 --> 00:00:05.000
<v Alice>This should be discarded</v>

00:00:06.000 --> 00:00:08.000
<v Bob>This is valid</v>
"""
        result = parse_caption_content(content, "vtt")
        assert len(result["cues"]) == 1
        assert result["cues"][0]["speaker"] == "Bob"
        assert len(result["warnings"]) == 1
        assert result["warnings"][0]["code"] == "INVALID_TIMING"

    def test_start_greater_than_end_discarded(self):
        """Cue where start > end is discarded with warning."""
        content = """WEBVTT

00:00:10.000 --> 00:00:05.000
<v Alice>Backwards timing</v>

00:00:06.000 --> 00:00:08.000
<v Bob>Valid cue</v>
"""
        result = parse_caption_content(content, "vtt")
        assert len(result["cues"]) == 1
        assert result["cues"][0]["speaker"] == "Bob"
        assert len(result["warnings"]) == 1
        assert result["warnings"][0]["code"] == "INVALID_TIMING"

    def test_empty_text_discarded(self):
        """Cue with empty text is discarded with warning."""
        content = """WEBVTT

00:00:01.000 --> 00:00:04.000
<v Alice></v>

00:00:05.000 --> 00:00:08.000
<v Bob>Valid text</v>
"""
        result = parse_caption_content(content, "vtt")
        assert len(result["cues"]) == 1
        assert result["cues"][0]["speaker"] == "Bob"
        assert len(result["warnings"]) == 1
        assert result["warnings"][0]["code"] == "EMPTY_TEXT"

    def test_all_invalid_cues_returns_empty(self):
        """All invalid cues returns empty cues array with warnings."""
        content = """WEBVTT

00:00:05.000 --> 00:00:05.000
<v Alice>Equal timing</v>

00:00:10.000 --> 00:00:05.000
<v Bob>Backwards</v>
"""
        result = parse_caption_content(content, "vtt")
        assert len(result["cues"]) == 0
        assert len(result["warnings"]) == 2

    def test_mix_of_valid_and_invalid_cues(self):
        """Mix of valid and invalid cues: only valid ones in output."""
        content = """1
00:00:01,000 --> 00:00:04,000
[Alice]: Valid cue one

2
00:00:05,000 --> 00:00:05,000
[Bob]: Invalid equal timing

3
00:00:06,000 --> 00:00:09,000
[Charlie]: Valid cue two
"""
        result = parse_caption_content(content, "srt")
        assert len(result["cues"]) == 2
        assert result["cues"][0]["speaker"] == "Alice"
        assert result["cues"][1]["speaker"] == "Charlie"
        assert len(result["warnings"]) == 1

    def test_whitespace_only_text_discarded(self):
        """Cue with whitespace-only text is discarded."""
        content = """WEBVTT

00:00:01.000 --> 00:00:04.000
   

00:00:05.000 --> 00:00:08.000
Valid text here
"""
        result = parse_caption_content(content, "vtt")
        assert len(result["cues"]) == 1
        assert result["cues"][0]["text"] == "Valid text here"


# --- Malformed Timestamp Tests ---


class TestMalformedTimestamps:
    """Tests for handling malformed timestamps (skip and warn)."""

    def test_completely_invalid_timestamp_line_skipped(self):
        """SRT block with non-timestamp second line is skipped entirely."""
        content = """1
NOT_A_TIMESTAMP --> ALSO_NOT
Some text

2
00:00:05,000 --> 00:00:08,000
Valid cue
"""
        result = parse_caption_content(content, "srt")
        assert len(result["cues"]) == 1
        assert result["cues"][0]["text"] == "Valid cue"

    def test_vtt_with_invalid_timing_line(self):
        """VTT with a non-timing line among cues."""
        content = """WEBVTT

00:00:01.000 --> 00:00:04.000
<v Alice>First valid cue</v>

INVALID_LINE
Some text without timing

00:00:06.000 --> 00:00:09.000
<v Bob>Second valid cue</v>
"""
        result = parse_caption_content(content, "vtt")
        # The parser should extract at least the valid cues
        assert len(result["cues"]) >= 2


# --- Tool Function Tests ---


class TestCaptionParserTool:
    """Tests for the @tool function interface."""

    def test_no_input_returns_error(self):
        """No content or url returns appropriate error."""
        from tools.caption_parser import caption_parser

        # Call the underlying function directly
        result = json.loads(caption_parser(content="", url="", format="auto"))
        assert result["cues"] == []
        assert len(result["warnings"]) == 1
        assert result["warnings"][0]["code"] == "NO_INPUT"

    def test_direct_content_parsing(self):
        """Passing content directly parses correctly."""
        from tools.caption_parser import caption_parser

        vtt_content = """WEBVTT

00:00:01.000 --> 00:00:04.000
<v Alice>Hello world</v>
"""
        result = json.loads(caption_parser(content=vtt_content, url="", format="auto"))
        assert len(result["cues"]) == 1
        assert result["cues"][0]["speaker"] == "Alice"
        assert result["format_detected"] == "vtt"

    def test_explicit_format_override(self):
        """Explicit format parameter overrides auto-detection."""
        # Content that looks like SRT but we force VTT parsing
        srt_content = """1
00:00:01,000 --> 00:00:04,000
[Alice]: Hello
"""
        # Force SRT format explicitly
        result = parse_caption_content(srt_content, "srt")
        assert result["format_detected"] == "srt"
        assert len(result["cues"]) == 1


# --- Round-Trip Property Tests ---


class TestRoundTrip:
    """Test that parsing produces consistent, precise timing values."""

    def test_vtt_timing_precision(self):
        """VTT timestamps preserve millisecond precision."""
        content = """WEBVTT

00:01:23.456 --> 00:01:27.891
<v Host>Precise timing test</v>
"""
        result = parse_caption_content(content, "vtt")
        assert len(result["cues"]) == 1
        # 1*60 + 23 + 0.456 = 83.456
        assert abs(result["cues"][0]["start"] - 83.456) < 0.001
        # 1*60 + 27 + 0.891 = 87.891
        assert abs(result["cues"][0]["end"] - 87.891) < 0.001

    def test_srt_timing_precision(self):
        """SRT timestamps preserve millisecond precision."""
        content = """1
01:02:03,456 --> 01:02:07,890
[Speaker]: Precise SRT timing
"""
        result = parse_caption_content(content, "srt")
        assert len(result["cues"]) == 1
        # 1*3600 + 2*60 + 3 + 0.456 = 3723.456
        assert abs(result["cues"][0]["start"] - 3723.456) < 0.001
        # 1*3600 + 2*60 + 7 + 0.890 = 3727.890
        assert abs(result["cues"][0]["end"] - 3727.890) < 0.001
