"""Property-based test for Custom_Agent tool-collection validation.

Feature: custom-agents, Property 4: Tool-collection validation is ownership-and-uniqueness

Validates: Requirements 3.2, 3.3

Property 4 (design.md): For any list of tool_collection references, validation
passes if and only if every reference resolves to a Tool_Collection owned by
the actor (an owned working copy, or a non-deprecated version in the actor's
lineage) and the list has no duplicates; otherwise the update is rejected
without any write.

``SpecialtyCRUD._validate_tool_collections`` is the Tool_Collection analogue of
``_validate_compatible_skills``. It resolves ownership two ways:

  * an OWNED working copy — ``index.get(PK=USER#<actor>#OWNED,
    SK=tool-collection#<id>)`` returns a row; or
  * a non-deprecated version in the actor's lineage — ``index.query_lineage(id)``
    returns a version that is not deprecated and whose ``s3_prefix`` begins with
    ``users/<actor>/``.

Ownership is modelled here by patching those two collaborators via generated
per-id "ownership kinds". Because ``_validate_tool_collections`` only ever
*reads* (``get`` / ``query_lineage``) and never writes, "no write occurs on
rejection" is demonstrated by asserting the mocked S3/index write methods
receive no calls on any path.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.context import LabRequestContext  # noqa: E402
from lab.specialty_crud import SpecialtyCRUD  # noqa: E402

_ACTOR = "actor-p4"
_OTHER_ACTOR = "actor-other"

# The five ownership kinds a generated id can take. The first two make the id
# resolve as actor-owned; the last three do not.
_OWNED_KINDS = ("owned_working", "owned_version")
_UNOWNED_KINDS = ("missing", "deprecated_only", "other_actor")

# Tool-collection-ish identifiers: non-empty, from a stable alphabet.
_tc_id = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters=".:-_",
    ),
    min_size=1,
    max_size=16,
)


@st.composite
def _reference_list_and_ownership(draw):
    """Generate ``(ref_list, ownership)`` and the expected accept/reject verdict.

    ``ownership`` maps each unique id to one of the ownership kinds above.
    ``ref_list`` is drawn from those ids, sometimes repeating an id to inject a
    duplicate. The expected verdict is computed exactly: accept iff the list has
    no duplicates AND every referenced id is actor-owned.
    """
    unique_ids = draw(st.lists(_tc_id, unique=True, min_size=0, max_size=6))
    ownership = {
        tc_id: draw(st.sampled_from(_OWNED_KINDS + _UNOWNED_KINDS))
        for tc_id in unique_ids
    }

    if unique_ids:
        # Draw the reference list from the known ids, allowing repeats so that
        # duplicates are exercised alongside the ownership mix.
        ref_list = draw(
            st.lists(st.sampled_from(unique_ids), min_size=0, max_size=8)
        )
        # Occasionally force a duplicate so the duplicate branch is well covered.
        if ref_list and draw(st.booleans()):
            ref_list = ref_list + [draw(st.sampled_from(ref_list))]
    else:
        ref_list = []

    has_duplicate = len(ref_list) != len(set(ref_list))
    all_owned = all(ownership[i] in _OWNED_KINDS for i in ref_list)
    expected_pass = (not has_duplicate) and all_owned
    return ref_list, ownership, expected_pass


def _version_record(*, deprecated: bool, actor: str):
    """A minimal lineage record exposing the fields the validator reads."""
    return types.SimpleNamespace(
        deprecated=deprecated,
        s3_prefix=f"users/{actor}/versions/tool-collections/x/",
    )


def _make_crud(ownership: dict[str, str]):
    """Build a SpecialtyCRUD whose index models the generated ownership map."""
    s3 = MagicMock(name="s3")
    index = MagicMock(name="index")

    def _get(pk, sk):
        # sk is ``tool-collection#<id>``; recover the id (category has no '#').
        tc_id = sk.split("#", 1)[1]
        if ownership.get(tc_id) == "owned_working":
            return types.SimpleNamespace(asset_id=tc_id)  # a truthy owned row
        return None

    def _query_lineage(published_id):
        kind = ownership.get(published_id)
        if kind == "owned_version":
            return [_version_record(deprecated=False, actor=_ACTOR)]
        if kind == "deprecated_only":
            return [_version_record(deprecated=True, actor=_ACTOR)]
        if kind == "other_actor":
            return [_version_record(deprecated=False, actor=_OTHER_ACTOR)]
        # "missing" (and the owned_working fast path never reaches here).
        return []

    index.get = AsyncMock(side_effect=_get)
    index.query_lineage = AsyncMock(side_effect=_query_lineage)
    return SpecialtyCRUD(s3=s3, index=index), s3, index


def _assert_no_writes(s3: MagicMock, index: MagicMock) -> None:
    """No write path may fire: validation only reads (get / query_lineage)."""
    index.put.assert_not_called()
    index.update.assert_not_called()
    index.transact_write.assert_not_called()
    s3.write.assert_not_called()
    s3.delete_prefix.assert_not_called()


# ---------------------------------------------------------------------------
# Property 4: Tool-collection validation is ownership-and-uniqueness
# ---------------------------------------------------------------------------

# Feature: custom-agents, Property 4: Tool-collection validation is ownership-and-uniqueness
@settings(max_examples=200, deadline=None)
@given(data=_reference_list_and_ownership())
def test_property4_tool_collection_validation_is_ownership_and_uniqueness(data):
    """_validate_tool_collections passes iff all owned AND unique; no write on reject.

    ``_validate_tool_collections`` is async; each Hypothesis example drives it
    to completion on a fresh event loop (avoids the fragile ``@given`` +
    ``pytest.mark.asyncio`` interaction while still exercising the real coroutine).

    Validates: Requirements 3.2, 3.3
    """
    ref_list, ownership, expected_pass = data

    crud, s3, index = _make_crud(ownership)
    ctx = LabRequestContext(
        actor_id=_ACTOR,
        session_id="session-p4",
        request_id="req-p4",
    )

    if expected_pass:
        # All owned and no duplicates -> validation passes (no exception).
        asyncio.run(crud._validate_tool_collections(ctx, ref_list))
    else:
        # A duplicate or an unowned/missing reference -> rejected.
        with pytest.raises(ValueError):
            asyncio.run(crud._validate_tool_collections(ctx, ref_list))

    # No write occurs on any path (accept or reject).
    _assert_no_writes(s3, index)
