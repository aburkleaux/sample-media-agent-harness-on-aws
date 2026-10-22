"""Property-based test for the Custom_Agent non-empty-name / draft-state create rule.

Feature: custom-agents, Property 2: Non-empty-name requirement

Validates: Requirements 1.5, 1.8

Property 2 (design.md): For any candidate name, creating a Custom_Agent is
rejected when the name is empty or all-whitespace, and accepted otherwise; an
accepted create yields a record whose lifecycle state is ``draft``.

``SpecialtyCRUD.create`` enforces a non-empty (non-whitespace) name — it raises
``ValueError("Name must not be empty")`` when ``name.strip()`` is empty — before
any S3/AssetIndex write, and records the asset with ``status="draft"``. ``create``
is async and writes ``persona.md`` to S3 and a typed ``SpecialtyAgent`` row to the
AssetIndex, so both collaborators are mocked (mirroring the Property 3 / Property 4
tests). A non-empty persona is always supplied so persona validation passes and
``name`` is the sole variable under test; the persisted asset is captured via the
mocked ``index.put`` call to assert its lifecycle state.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.context import LabRequestContext  # noqa: E402
from lab.specialty_crud import SpecialtyCRUD  # noqa: E402

_ACTOR = "actor-p2"
# A non-empty persona so persona validation always passes; ``name`` is the
# variable under test.
_PERSONA = "You are a helpful research assistant."

# Unicode whitespace characters that ``str.strip()`` removes; used to build
# whitespace-only names that must be rejected.
_WHITESPACE = " \t\n\r\f\v\u00a0\u2003"


@st.composite
def _candidate_name(draw):
    """Generate an arbitrary candidate name and its expected accept verdict.

    Deliberately biases toward the empty / whitespace-only branch so both
    outcomes are well exercised:

      * the empty string ``""``,
      * whitespace-only strings (``"   "``, ``"\\t\\n"``, ...),
      * arbitrary text (which may still be whitespace-only, handled exactly).

    The expected verdict is computed exactly: a create is accepted iff the name
    is non-empty after stripping surrounding whitespace.
    """
    kind = draw(st.sampled_from(("empty", "whitespace", "text")))
    if kind == "empty":
        name = ""
    elif kind == "whitespace":
        name = draw(
            st.text(alphabet=_WHITESPACE, min_size=1, max_size=8)
        )
    else:
        name = draw(st.text(min_size=0, max_size=32))

    expected_accept = bool(name.strip())
    return name, expected_accept


def _make_crud() -> tuple[SpecialtyCRUD, MagicMock, MagicMock]:
    """Build a SpecialtyCRUD with mocked async S3/index collaborators."""
    s3 = MagicMock(name="s3")
    index = MagicMock(name="index")
    # ``create`` awaits both of these; persona now writes via the canonical
    # absolute method (BUG-2).
    s3.write = AsyncMock(name="s3.write")
    s3.write_absolute = AsyncMock(name="s3.write_absolute")
    index.put = AsyncMock(name="index.put")
    return SpecialtyCRUD(s3=s3, index=index), s3, index


# ---------------------------------------------------------------------------
# Property 2: Non-empty-name requirement
# ---------------------------------------------------------------------------

# Feature: custom-agents, Property 2: Non-empty-name requirement
@settings(max_examples=200, deadline=None)
@given(data=_candidate_name())
def test_property2_non_empty_name_and_draft_state(data):
    """create is rejected iff name is empty/whitespace; accepted create is draft.

    ``create`` is async; each Hypothesis example drives it to completion on a
    fresh event loop (avoids the fragile ``@given`` + ``pytest.mark.asyncio``
    interaction while still exercising the real coroutine).

    Validates: Requirements 1.5, 1.8
    """
    name, expected_accept = data

    crud, s3, index = _make_crud()
    ctx = LabRequestContext(
        actor_id=_ACTOR,
        session_id="session-p2",
        request_id="req-p2",
    )

    if expected_accept:
        # Non-empty (post-strip) name -> create succeeds.
        result = asyncio.run(crud.create(ctx, name, _PERSONA))

        # The persisted SpecialtyAgent (first positional arg to index.put) must
        # carry the initial lifecycle state ``draft`` (Requirement 1.8).
        index.put.assert_awaited_once()
        persisted_agent = index.put.await_args.args[0]
        assert persisted_agent.status == "draft"
        assert persisted_agent.name == name
        # A result is returned (create completed rather than raising).
        assert result.asset_id
    else:
        # Empty or whitespace-only name -> rejected with ValueError, and the
        # rejection happens BEFORE any write (validate-then-write ordering).
        with pytest.raises(ValueError):
            asyncio.run(crud.create(ctx, name, _PERSONA))

        s3.write.assert_not_awaited()
        index.put.assert_not_awaited()
