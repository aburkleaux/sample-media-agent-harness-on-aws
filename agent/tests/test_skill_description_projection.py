"""A skill's frontmatter description must reach the AssetIndex row.

Skills are the only category whose description lives inside a FILE rather than a
form field: the author writes it in SKILL.md, which is stored in S3, while the
inventory lists AssetIndex rows. Nothing copied one to the other, so every Skills
row — draft and published — showed a name and no statement of what the skill was
for, and no UI change could fix it because the value was not in the record being
listed. Observed in staging: 0 of 7 asset rows carried a `description` attribute
while the published `ai-storyboarding` SKILL.md in S3 had a 500-character one.

The row is a display PROJECTION of the file; S3 stays the source of truth. So the
tests below are about the projection staying faithful — including on update, where a
row that refreshed only `updatedAt` would keep describing the previous body.

Extraction is deliberately lenient (`extract_skill_description`, not
`validate_skill_document`): a draft is saved repeatedly while it is still being
written, and a validating extractor would refuse to save a skill whose description
the author has not typed yet.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.context import LabRequestContext  # noqa: E402
from lab.models import Skill  # noqa: E402
from lab.skill_crud import SkillCRUD  # noqa: E402
from lab.skill_package_validator import extract_skill_description  # noqa: E402


DESCRIPTION = "Turn a story idea into a storyboard. Use when the user wants panels."

SKILL_MD = f"""---
name: ai-storyboarding
description: "{DESCRIPTION}"
---

# AI Storyboarding

Body.
"""


def _ctx(actor_id: str = "u-jane") -> LabRequestContext:
    return LabRequestContext(actor_id=actor_id, session_id="s-1", request_id="r-1")


def _crud():
    s3 = MagicMock()
    s3.write_absolute = AsyncMock()
    s3.read_absolute = AsyncMock()
    s3.list_absolute = AsyncMock(return_value=[])
    s3.delete_absolute = AsyncMock()
    s3.delete_prefix_absolute = AsyncMock()
    index = MagicMock()
    index.put = AsyncMock()
    index.update = AsyncMock()
    # `import_package` checks for a name collision first, so the fake must answer the
    # listing query — a MagicMock here fails with "can't be used in await".
    index.query_pk = AsyncMock(return_value=([], None))
    return SkillCRUD(s3=s3, index=index), s3, index


# --- the extractor ---------------------------------------------------------


def test_extractor_reads_the_frontmatter_description():
    assert extract_skill_description(SKILL_MD) == DESCRIPTION


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty"),
        pytest.param("# No frontmatter at all\n", id="no-frontmatter"),
        pytest.param("---\nname: x\n---\n\nbody\n", id="no-description-key"),
        pytest.param("---\nname: x\ndescription:\n---\n", id="null-description"),
        pytest.param("---\nname: x\ndescription: 42\n---\n", id="non-string"),
        pytest.param("---\n  : : broken yaml\n---\n", id="invalid-yaml"),
    ],
)
def test_extractor_never_raises_and_answers_empty(body):
    """A half-written draft must still save.

    This is why the save path does not reuse `validate_skill_document`, which raises
    on every one of these.
    """
    assert extract_skill_description(body) == ""


def test_extractor_truncates_rather_than_rejecting():
    # An over-long description must not be able to fail a save; SKILL.md keeps the
    # full text either way.
    long_md = "---\nname: x\ndescription: \"" + ("a" * 2000) + "\"\n---\n"
    assert len(extract_skill_description(long_md)) == 1024


# --- create ----------------------------------------------------------------


def test_create_records_the_description_on_the_row():
    crud, _s3, index = _crud()

    asyncio.run(crud.create(ctx=_ctx(), name="ai-storyboarding", skill_md=SKILL_MD))

    (recorded,), kwargs = index.put.call_args
    assert isinstance(recorded, Skill)
    assert recorded.description == DESCRIPTION
    assert kwargs["owner_user_id"] == "u-jane"


def test_create_without_a_description_records_empty_and_still_succeeds():
    # `to_item` omits a falsy description, so the attribute is simply absent —
    # "unknown", which the row renders as no description line rather than as a blank
    # one the author is supposed to have written.
    crud, _s3, index = _crud()

    result = asyncio.run(
        crud.create(ctx=_ctx(), name="x", skill_md="---\nname: x\n---\n\nbody\n")
    )

    assert result.asset_id
    (recorded,), _ = index.put.call_args
    assert recorded.description == ""


# --- update ----------------------------------------------------------------


def test_update_reprojects_the_description():
    """The row must describe the body that was just written, not the previous one."""
    crud, _s3, index = _crud()
    record = MagicMock()
    record.status = "draft"
    record.s3_prefix = "users/u-jane/drafts/skills/a-1/"
    index.get = AsyncMock(return_value=record)

    edited = SKILL_MD.replace(DESCRIPTION, "Completely different purpose now.")
    asyncio.run(crud.update_skill_md(ctx=_ctx(), asset_id="a-1", skill_md=edited))

    _args, kwargs = index.update.call_args
    assert "description = :description" in kwargs["update_expression"]
    assert kwargs["expression_attribute_values"][":description"] == (
        "Completely different purpose now."
    )
    # And the timestamp is still bumped — this replaced that write, not removed it.
    assert ":now" in kwargs["expression_attribute_values"]


def test_update_that_removes_the_description_clears_the_row():
    # A SET of the extracted value, so deleting the frontmatter line does not leave
    # the old description behind on the row forever.
    crud, _s3, index = _crud()
    record = MagicMock()
    record.status = "draft"
    record.s3_prefix = "users/u-jane/drafts/skills/a-1/"
    index.get = AsyncMock(return_value=record)

    asyncio.run(
        crud.update_skill_md(
            ctx=_ctx(), asset_id="a-1", skill_md="---\nname: x\n---\n\nbody\n"
        )
    )

    _args, kwargs = index.update.call_args
    assert kwargs["expression_attribute_values"][":description"] == ""


def test_update_of_a_missing_draft_still_returns_none():
    """The added write must not change the not-found contract."""
    crud, _s3, index = _crud()
    index.get = AsyncMock(return_value=None)

    result = asyncio.run(
        crud.update_skill_md(ctx=_ctx(), asset_id="gone", skill_md=SKILL_MD)
    )

    assert result is None
    index.update.assert_not_awaited()


# --- import ----------------------------------------------------------------


def test_import_package_records_the_description_too():
    """Two create paths, one projection — they must not record it differently."""
    crud, _s3, index = _crud()

    asyncio.run(
        crud.import_package(
            ctx=_ctx(),
            name="ai-storyboarding",
            skill_md=SKILL_MD,
            supporting_files=[("references/guide.md", b"# Guide\n")],
        )
    )

    (recorded,), _ = index.put.call_args
    assert recorded.description == DESCRIPTION
