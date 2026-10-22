"""Example test for the Custom_Agent model-validation catalog-unreachable branch.

Feature: custom-agents, Task 2.9 (example test)

Validates: Requirements 2.4

Requirement 2.4: when the Model_Catalog cannot be reached, model validation
must surface a *distinct* "temporarily unavailable" error (so the UI can tell
it apart from a genuinely unknown identifier), and — like every other
validator — it must run before any write, so a rejection leaves S3 and the
AssetIndex untouched.

``SpecialtyCRUD._validate_model`` imports ``discover_models`` lazily from
``model_catalog``. This test patches that lookup to (a) raise outright and
(b) return an empty list — the two ways the catalog is considered
unreachable — and asserts both surface the "temporarily unavailable" message
with no calls landing on the mocked S3/index collaborators.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add agent/src to path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import model.catalog as model_catalog  # noqa: E402  (import after sys.path tweak)
from lab.specialty_crud import SpecialtyCRUD  # noqa: E402


def _make_crud() -> tuple[SpecialtyCRUD, MagicMock, MagicMock]:
    """Build a SpecialtyCRUD with mocked S3/index collaborators."""
    s3 = MagicMock(name="s3")
    index = MagicMock(name="index")
    return SpecialtyCRUD(s3=s3, index=index), s3, index


def test_validate_model_catalog_raises_surfaces_temporarily_unavailable():
    """A raising ``discover_models`` -> distinct "temporarily unavailable" error.

    Validates: Requirements 2.4
    """
    crud, s3, index = _make_crud()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("bedrock is down")

    with patch.object(model_catalog, "discover_models", _boom):
        with pytest.raises(ValueError) as excinfo:
            crud._validate_model("some-model")

    # Distinct signal the UI maps to "validation temporarily unavailable",
    # not the "unknown identifier" message.
    assert "temporarily unavailable" in str(excinfo.value)

    # Validate-before-write: a rejection must never touch S3 or the AssetIndex.
    assert s3.mock_calls == []
    assert index.mock_calls == []


def test_validate_model_empty_catalog_surfaces_temporarily_unavailable():
    """An empty catalog is also treated as unreachable (fail closed).

    Validates: Requirements 2.4
    """
    crud, s3, index = _make_crud()

    with patch.object(model_catalog, "discover_models", lambda *a, **k: []):
        with pytest.raises(ValueError) as excinfo:
            crud._validate_model("some-model")

    assert "temporarily unavailable" in str(excinfo.value)
    assert s3.mock_calls == []
    assert index.mock_calls == []
