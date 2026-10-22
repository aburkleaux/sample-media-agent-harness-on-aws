"""Property-based test for Custom_Agent model validation.

Feature: custom-agents, Property 3: Model validation is catalog membership

Validates: Requirements 2.2, 2.3

Property 3 (design.md): For any model identifier, model validation passes if
and only if the identifier is present in ``Model_Catalog.discover_models()``;
a non-member is rejected without any write.

``SpecialtyCRUD._validate_model`` is pure catalog-membership logic: it imports
``discover_models`` from ``model_catalog`` (patched here to return a generated
catalog of ``{"id": ...}`` dicts), and raises ``ValueError`` naming the id when
the candidate is not a member. Because ``_validate_model`` never touches S3 or
the AssetIndex itself, "no write occurs on rejection" is demonstrated by
asserting the mocked S3/index collaborators receive no calls on any path.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import model.catalog as model_catalog  # noqa: E402  (import after sys.path tweak)
from lab.specialty_crud import SpecialtyCRUD  # noqa: E402


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Bedrock inference-profile-ish identifiers: non-empty, from a stable alphabet
# that includes the ``.``/``:``/``-`` separators real profile ids use.
_model_id = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters=".:-_",
    ),
    min_size=1,
    max_size=24,
)


@st.composite
def _catalog_and_candidate(draw):
    """Generate a (catalog, candidate_id, is_member) triple.

    The catalog is a list of ``{"id": ...}`` dicts (sometimes carrying an
    extra ``name`` key, matching the real ``discover_models`` shape). About
    half the time the candidate is drawn from the catalog (a member) and the
    rest of the time it is an independently generated id (usually a non-member,
    but membership is computed exactly rather than assumed).
    """
    ids = draw(st.lists(_model_id, unique=True, min_size=0, max_size=8))

    # Bias toward exercising the member branch when the catalog is non-empty.
    if ids and draw(st.booleans()):
        candidate = draw(st.sampled_from(ids))
    else:
        candidate = draw(_model_id)

    with_name = draw(st.booleans())
    catalog = [
        ({"id": i, "name": f"Model {i}"} if with_name else {"id": i})
        for i in ids
    ]
    is_member = candidate in set(ids)
    return catalog, candidate, is_member


def _make_crud() -> tuple[SpecialtyCRUD, MagicMock, MagicMock]:
    """Build a SpecialtyCRUD with mocked S3/index collaborators."""
    s3 = MagicMock(name="s3")
    index = MagicMock(name="index")
    return SpecialtyCRUD(s3=s3, index=index), s3, index


# ---------------------------------------------------------------------------
# Property 3: Model validation is catalog membership
# ---------------------------------------------------------------------------

# Feature: custom-agents, Property 3: Model validation is catalog membership
@settings(max_examples=200, deadline=None)
@given(data=_catalog_and_candidate())
def test_property3_model_validation_is_catalog_membership(data):
    """_validate_model passes iff the id is a catalog member; no write on reject.

    Validates: Requirements 2.2, 2.3
    """
    catalog, candidate, is_member = data

    crud, s3, index = _make_crud()

    # Patch the catalog lookup that ``_validate_model`` imports lazily. A
    # context-manager patch (rather than the monkeypatch fixture) is reset on
    # every generated input, keeping each Hypothesis example isolated.
    with patch.object(model_catalog, "discover_models", lambda *a, **k: catalog):
        if is_member:
            # Membership holds -> validation passes (no exception).
            crud._validate_model(candidate)
        else:
            # Non-member (including the empty-catalog case) -> rejected.
            with pytest.raises(ValueError):
                crud._validate_model(candidate)

    # No write occurs on any path: _validate_model must not touch S3 or the
    # AssetIndex, so a rejection can never leave a partial write behind.
    assert s3.mock_calls == []
    assert index.mock_calls == []
