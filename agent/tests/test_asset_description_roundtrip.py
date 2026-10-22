"""``description`` survives the model → DDB item → model round trip.

``to_index_item`` omits optional fields that are empty rather than writing
null, matching the "absent means N/A" convention documented at
``models.py:520-522``. So an empty description must be ABSENT from the item,
and a populated one must round-trip byte-for-byte.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.models import (  # noqa: E402
    SpecialtyAgent,
    Skill,
    from_index_item,
    to_index_item,
)


def _agent(description: str) -> SpecialtyAgent:
    return SpecialtyAgent(
        asset_id="ag-1",
        category="specialty-agent",
        name="Transcript Cleaner",
        status="draft",
        scope="private",
        s3_prefix="users/u1/drafts/specialty-agents/ag-1/",
        created_at=1,
        updated_at=2,
        description=description,
    )


def test_description_defaults_to_empty_string():
    agent = SpecialtyAgent(
        asset_id="ag-1",
        category="specialty-agent",
        name="n",
        status="draft",
        scope="private",
        s3_prefix="p/",
        created_at=1,
        updated_at=2,
    )
    assert agent.description == ""


def test_populated_description_round_trips():
    item = to_index_item(
        _agent("Cleans up ASR transcripts"),
        owner_user_id="u1",
        owner_group_id=None,
        owner_org_id=None,
    )
    assert item["description"] == "Cleans up ASR transcripts"
    back = from_index_item(item)
    assert back.description == "Cleans up ASR transcripts"


def test_empty_description_is_absent_not_null():
    item = to_index_item(
        _agent(""),
        owner_user_id="u1",
        owner_group_id=None,
        owner_org_id=None,
    )
    assert "description" not in item
    # A legacy row with no attribute at all must still deserialize.
    back = from_index_item(item)
    assert back.description == ""


def test_description_is_common_to_every_category():
    skill = Skill(
        asset_id="sk-1",
        category="skill",
        name="n",
        status="draft",
        scope="private",
        s3_prefix="p/",
        created_at=1,
        updated_at=2,
        description="a skill",
    )
    item = to_index_item(
        skill, owner_user_id="u1", owner_group_id=None, owner_org_id=None
    )
    assert from_index_item(item).description == "a skill"
