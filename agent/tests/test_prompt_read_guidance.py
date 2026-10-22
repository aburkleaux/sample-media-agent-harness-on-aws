"""The system prompt must not prescribe a per-file presign-then-fetch dance.

It used to say, naming a federated tool outright:

    "Output URIs from `get_job_status` point here; use
     `media-outputs___get_download_url` to get HTTP URLs for them."

Two problems. It hardcoded a tool owned by another team into the harness prompt,
so a rename there silently misdirects the agent. And it prescribed exactly the
expensive shape: one presign call per file, each its own model round-trip. A
verified turn on the deployed build followed that instruction and dispatched a
sub-agent purely to presign two files before reading them.

Guidance now describes the OUTCOME (read result files directly, in one batch)
rather than naming the tool that achieves it, so the reader tool's own
description can carry the mechanism.
"""
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

MAIN = (SRC / "prompts" / "orchestrator.py").read_text()


def _prompt_region() -> str:
    """The prompt-building region of main.py.

    The prompt is assembled from many adjacent f-string fragments, so the
    assertions below scan the whole module and would also catch a fragment
    moved elsewhere.
    """
    return MAIN


class TestNoFederatedToolNamesInGuidance:
    def test_download_url_tool_is_not_named(self):
        assert "media-outputs___get_download_url" not in _prompt_region()

    def test_no_federated_tool_name_pattern_in_prompt_strings(self):
        """`target___tool` is AgentCore's federated naming convention."""
        prompt_lines = [
            line for line in _prompt_region().splitlines()
            if re.search(r'^\s*[f]?"', line)          # a prompt fragment
        ]
        offenders = [
            line.strip() for line in prompt_lines
            if re.search(r"[a-z0-9\-]+___[a-z0-9_]+", line)
        ]
        assert not offenders, f"federated tool name(s) in prompt: {offenders[:3]}"


class TestReadGuidanceDescribesOutcome:
    def test_still_insists_on_reading_contents(self):
        """The behaviour that made results usable must survive the rewording."""
        assert "RETRIEVE AND READ" in _prompt_region()

    def test_prefers_reading_s3_uris_directly(self):
        region = _prompt_region()
        assert "read those s3:// URIs directly" in region

    def test_asks_for_one_batch_rather_than_per_file(self):
        region = _prompt_region()
        assert "in one batch" in region
        assert "round trip per file" in region

    def test_still_allows_resolving_a_link_when_the_user_wants_one(self):
        """Presigning is not wrong — it is wrong as a precondition for reading."""
        assert "download or share" in _prompt_region()
