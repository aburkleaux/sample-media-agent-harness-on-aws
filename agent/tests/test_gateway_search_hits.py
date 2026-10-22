"""Gateway search returns an envelope, not a bare list.

The live response from ``x_amz_bedrock_agentcore_search`` on
media-workflow-gateway-staging is ``{"tools": [{name, description,
inputSchema}, ...]}``. Returning that dict to callers made them iterate it,
which in Python yields its KEYS — so a search "succeeded" with the single
literal name ``"tools"``, matched no real tool, and tool narrowing fell back to
the full catalogue on every request. It failed open, so nothing broke, but the
narrowing never engaged either.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_manager import _search_hits  # noqa: E402


# The real response, trimmed to two entries.
LIVE_RESPONSE = {
    "tools": [
        {"name": "shot-segmentation___segment_shots",
         "description": "Detect shot boundaries",
         "inputSchema": {"type": "object"}},
        {"name": "shot-segmentation___get_job_status",
         "description": "Poll a job",
         "inputSchema": {"type": "object"}},
    ]
}


class TestSearchHits:
    def test_envelope_is_unwrapped_to_the_tool_list(self):
        hits = _search_hits(LIVE_RESPONSE)
        assert [h["name"] for h in hits] == [
            "shot-segmentation___segment_shots",
            "shot-segmentation___get_job_status",
        ]

    def test_keys_are_never_returned_as_tool_names(self):
        """The regression: iterating the raw dict produced ["tools"]."""
        assert "tools" not in _search_hits(LIVE_RESPONSE)

    def test_bare_list_passes_through(self):
        """The envelope is not contractual, so accept a plain list too."""
        entries = [{"name": "a___b"}]
        assert _search_hits(entries) == entries

    def test_alternate_envelope_keys(self):
        assert _search_hits({"results": [{"name": "x"}]}) == [{"name": "x"}]
        assert _search_hits({"capabilities": [{"name": "y"}]}) == [{"name": "y"}]

    def test_unrecognised_payloads_yield_nothing_to_narrow_on(self):
        """Empty means "could not narrow" → the caller uses every tool."""
        assert _search_hits({}) == []
        assert _search_hits({"tools": "not-a-list"}) == []
        assert _search_hits(None) == []
        assert _search_hits("text") == []
        assert _search_hits(7) == []
