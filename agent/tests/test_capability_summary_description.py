"""The description an author wrote must survive the trip to an inventory row.

The Skills surface lists a name, a badge and a set of buttons; without the
description the list says what each thing is CALLED but not what it is FOR. The
DTO carried no description at all, so the UI had nothing to render however it
was styled — hence these tests sit at the boundary the browser actually reads
(`to_json`), not on the dataclass alone.

The second property is the one that bites quietly: a source that cannot answer
must produce an ABSENT key, not `""`. Gateway tools, built-ins and Registry
records genuinely have no description, and a client that receives `""` cannot
tell "the author left it blank" from "this source does not carry it" — the same
absent-vs-empty confusion that made every Capability-API skill claim
"1 file · SKILL.md".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from capability.dto import CapabilitySummary, merge_summaries
from capability.sources import summary_from_asset


@dataclass
class _Row:
    """The shape `summary_from_asset` reads — a `lab.models` asset duck-type."""

    asset_id: str = "skill-1"
    name: str = "ai-storyboarding"
    category: str = "skill"
    description: str = ""
    version: Optional[int] = None
    deprecated: bool = False
    scope: str = "private"
    updated_at: int = 1_700_000_000_000


def test_authored_description_reaches_the_json_row() -> None:
    row = _Row(description="Turn a script into a shot-by-shot storyboard.")
    summary = summary_from_asset(row)

    assert summary.description == "Turn a script into a shot-by-shot storyboard."
    assert (
        summary.to_json()["description"]
        == "Turn a script into a shot-by-shot storyboard."
    )


def test_published_version_carries_it_too() -> None:
    """The PUBLISHED section is a different partition, not a different shape."""
    summary = summary_from_asset(_Row(version=3, description="Screen a cut."))

    assert summary.status == "published"
    assert summary.to_json()["description"] == "Screen a cut."


def test_blank_description_is_absent_not_empty_string() -> None:
    # `to_json` drops None, so a blank author field and a source that cannot
    # answer both arrive as "no description" — one branch in the client.
    assert summary_from_asset(_Row(description="")).description is None
    assert "description" not in summary_from_asset(_Row(description="")).to_json()
    # Whitespace is blank too: "   " would otherwise render an empty line whose
    # height is real but whose content is not.
    assert summary_from_asset(_Row(description="   ")).description is None


def test_sources_that_cannot_answer_omit_the_key() -> None:
    """A Gateway tool has no description field at all — it must not claim one."""
    tool = CapabilitySummary(
        id="tool:gw-1:t-1:process_media",
        category="tool",
        capability_class="gateway-mcp",
        display_name="process_media",
        scope="harness-shared",
        status="published",
        source="gateway",
    )

    assert tool.description is None
    assert "description" not in tool.to_json()


def test_merge_keeps_the_canonical_row_description() -> None:
    """The Registry twin has no description; merging must not erase the row's.

    `merge_summaries` rebuilds the winner to graft the Registry record id onto
    it. That rebuild is field-by-field over `asdict`, so a field the rebuild
    forgets is silently lost — exactly how `model`, `tool_collections` and
    `external_access_enabled` each disappeared from `AssetCommon` builders.
    """
    canonical = summary_from_asset(_Row(version=1, description="The real thing."))
    registry_twin = CapabilitySummary(
        id=canonical.id,
        category="skill",
        capability_class="asset",
        display_name="ai-storyboarding",
        scope="harness-shared",
        status="published",
        source="agent-registry",
        asset_id="skill-1",
        version=1,
        registry_record_id="rec-42",
    )

    merged = merge_summaries([canonical, registry_twin])

    assert len(merged) == 1
    assert merged[0].source == "asset-index"
    assert merged[0].registry_record_id == "rec-42"
    assert merged[0].description == "The real thing."
