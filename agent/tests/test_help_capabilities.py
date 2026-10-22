"""Property tests for the Public Capability View (Requirements 3, 4).

Property 1 — internal names never enter public help.
Property 2 — availability is a truthful function of authorized live state
(unavailable stays visible-but-unavailable; unreadable sources are partial,
never a healthy empty absence).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from capability.descriptors import PROHIBITED_TOOLS  # noqa: E402
from help_capabilities import (  # noqa: E402
    build_public_capability_view,
    is_internal_identifier,
    render_public_capabilities,
)

# A real prohibited token, chosen dynamically so the test tracks the policy.
_PROHIBITED = next(iter(sorted(PROHIBITED_TOOLS)))

_IDENTIFIER_ADVERSARIES = [
    "media-processing___process_media",
    "arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/abc",
    "s3://mediaworks-harness-skills-store/artifacts/x.json",
    "canonical://tool/x",
    "123456789012",
    "gw.gateway.bedrock-agentcore.us-west-2.amazonaws.com",
]


def _all_surfaced_strings(view) -> list[str]:
    out: list[str] = []
    for cap in (*view.foundational, *view.extensions):
        out.extend([cap.display_name, cap.description, cap.category])
    out.extend(view.partial_sources)
    out.append(render_public_capabilities(view))
    return [s for s in out if s]


def test_prohibited_and_identifier_names_never_surface():
    """Property 1: adversarial catalog -> no prohibited/identifier string leaks."""
    skills = [
        {"name": "web-research", "description": "Governs web searches."},
        {"name": _PROHIBITED, "description": "should be dropped by name"},
        {"name": "safe-skill", "description": f"leaks {_PROHIBITED} and s3://b/k"},
    ]
    external_tools = [{"name": adv, "description": adv} for adv in _IDENTIFIER_ADVERSARIES]
    external_tools.append({"name": _PROHIBITED, "description": "internal"})

    view = build_public_capability_view(skills=skills, external_tools=external_tools)

    for s in _all_surfaced_strings(view):
        assert not is_internal_identifier(s), f"leaked internal identifier: {s!r}"
    # No raw adversarial identifier substring survives anywhere in the block.
    block = render_public_capabilities(view)
    for adv in (*_IDENTIFIER_ADVERSARIES, _PROHIBITED, "s3://b/k"):
        assert adv not in block, f"raw identifier survived in rendered block: {adv!r}"


def test_prohibited_named_capability_is_dropped_entirely():
    view = build_public_capability_view(
        skills=[{"name": _PROHIBITED, "description": "x"}],
        external_tools=[{"name": _PROHIBITED, "description": "y"}],
    )
    assert all(_PROHIBITED not in c.display_name for c in view.extensions)


def test_qualified_tool_name_reduced_to_bare_and_kept_clean():
    view = build_public_capability_view(
        external_tools=[{"name": "speech___diarize_speech", "description": "Transcribe."}],
    )
    names = [c.display_name for c in view.extensions]
    assert names, "a safe qualified tool should surface (as its bare tail)"
    assert all("___" not in n for n in names)
    assert any("Diarize speech" == n for n in names)


def test_unavailable_tool_stays_visible_but_marked_unavailable():
    """Property 2: never claim caller can use a disabled capability (Req 4.3)."""
    view = build_public_capability_view(
        external_tools=[{"name": "web_search", "description": "Search the web.", "available": False}],
    )
    web = [c for c in view.extensions if c.display_name == "Web search"]
    assert web and web[0].availability == "unavailable"


def test_partial_sources_reported_deduped_and_sorted():
    """Property 2: unreadable sources are partial, not a silent empty success."""
    view = build_public_capability_view(
        skills=[{"name": "a", "description": "d"}],
        partial_sources=["gateway:tenant", "gateway:platform", "gateway:tenant", ""],
    )
    assert view.partial_sources == ("gateway:platform", "gateway:tenant")
    assert "could not be confirmed" in render_public_capabilities(view)


def test_foundational_always_present_and_clean():
    view = build_public_capability_view()
    assert view.foundational, "foundational capabilities are always present"
    for cap in view.foundational:
        assert not is_internal_identifier(cap.display_name)
        assert not is_internal_identifier(cap.description)


def test_is_internal_identifier_shapes():
    for bad in _IDENTIFIER_ADVERSARIES + [_PROHIBITED]:
        assert is_internal_identifier(bad), bad
    for good in ["Diarize speech", "web-research", "Search the web", ""]:
        assert not is_internal_identifier(good), good
