"""Contract tests for the bundled agent-harness-help Skill (Requirements 1-5).

Covers package shape (tool-free router, references resolve), the non-disclosure
rule (Req 3.5 — help prose carries no internal tool names or identifier shapes),
and that the Skill is told to ground availability in <public_capabilities>.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from help_capabilities import is_internal_identifier  # noqa: E402

SKILL_ROOT = Path(__file__).parents[1] / "skills" / "agent-harness-help"
SKILL_MD = SKILL_ROOT / "SKILL.md"
REFERENCES = [
    "getting-started.md",
    "files-and-results.md",
    "skills.md",
    "custom-agents.md",
    "tool-collections-connectors-workflows.md",
    "jobs.md",
    "troubleshooting.md",
]
# Unambiguous internal primitives that must never appear in user-facing help.
_FORBIDDEN_TOKENS = [
    "dispatch_tool",
    "plan_and_execute_tool",
    "code_interpreter",
    "file_read",
    "___",
    "arn:aws",
    "s3://",
    ".amazonaws.com",
]


def _all_files():
    yield SKILL_MD
    for name in REFERENCES:
        yield SKILL_ROOT / "references" / name


def test_frontmatter_name_matches_dir_and_is_tool_free():
    lines = SKILL_MD.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    assert any(line.strip() == "name: agent-harness-help" for line in lines[:6])
    assert SKILL_ROOT.name == "agent-harness-help"
    assert not (SKILL_ROOT / "tools.json").exists(), "Help Skill must be tool-free (Req 1.3)"


def test_all_references_exist_and_are_linked_from_skill_md():
    content = SKILL_MD.read_text(encoding="utf-8")
    for name in REFERENCES:
        assert (SKILL_ROOT / "references" / name).is_file(), name
        assert f"references/{name}" in content, name


def test_no_internal_names_or_identifier_shapes_in_help_prose():
    for path in _all_files():
        text = path.read_text(encoding="utf-8")
        for token in _FORBIDDEN_TOKENS:
            assert token not in text, f"{path.name} leaks internal token {token!r}"
        for lineno, line in enumerate(text.splitlines(), 1):
            assert not is_internal_identifier(line), (
                f"{path.name}:{lineno} embeds an internal identifier shape: {line!r}"
            )


def test_skill_grounds_in_live_view_and_separates_authoring():
    content = SKILL_MD.read_text(encoding="utf-8")
    assert "get_available_capabilities" in content, "must ground availability via the live tool (Req 4.1)"
    assert "skill-creator" in content, "must separate product help from Skill authoring (Req 2/3)"
    assert "answer in this turn" in content.lower()


def test_promised_help_topics_all_have_a_reference():
    """Req 2.1 topic coverage: every promised area routes somewhere."""
    content = SKILL_MD.read_text(encoding="utf-8")
    for name in REFERENCES:
        assert f"references/{name}" in content


# --- Grounding tool wiring (option B) -------------------------------------

def test_grounding_tool_projects_live_catalog_without_leaking(monkeypatch):
    """get_available_capabilities must reflect the LIVE catalog, sanitized.

    Feeds a poisoned catalog (qualified name, bucket path, a prohibited tool,
    and a disconnected-gateway partial marker) and asserts the tool's text is
    identifier-free, keeps a legitimate tool, and surfaces the partial source.
    """
    from capability.descriptors import PROHIBITED_TOOLS
    import main

    prohibited = next(iter(sorted(PROHIBITED_TOOLS)))
    poisoned = {
        "skills": [{"name": "web-research", "description": "Governs web searches."}],
        "gateway_tools": [
            {"name": "speech___diarize_speech", "description": "Transcribe audio."},
            {"name": "leaky", "description": "writes to s3://bucket/key"},
            {"name": prohibited, "description": "internal"},
        ],
        "partial_sources": ["external connectors"],
        "tools": [],
        "models": [],
    }
    monkeypatch.setattr(main, "_build_catalog", lambda: poisoned)

    text = main._public_capabilities_text()

    for bad in ("speech___diarize_speech", "s3://bucket/key", prohibited):
        assert bad not in text, f"grounding tool leaked {bad!r}"
    assert "Diarize speech" in text, "a legitimate external tool should surface (friendly name)"
    assert "could not be confirmed" in text, "disconnected gateway must surface as unconfirmed"


def test_grounding_tool_is_a_strands_tool():
    import main

    tool_obj = getattr(main, "get_available_capabilities", None)
    assert tool_obj is not None, "grounding tool must be defined in main"
    spec = getattr(tool_obj, "tool_spec", None)
    assert spec is not None, "get_available_capabilities must be a Strands @tool"
    assert spec["name"] == "get_available_capabilities"
