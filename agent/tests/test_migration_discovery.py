"""Unit tests for the legacy-skill discovery helper.

Validates: Requirement 14.1 — paginate ``ListObjectsV2`` with
``Prefix='skills/'`` / ``Delimiter='/'`` and yield each
``CommonPrefixes`` entry as a :class:`LegacySkill`.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

# Make ``lab.migration`` importable without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.migration.discovery import LegacySkill, list_legacy_skills


def _make_s3(pages: list[dict]) -> MagicMock:
    """Build a ``MagicMock`` s3 client whose paginator returns ``pages``."""
    s3 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = iter(pages)
    s3.get_paginator.return_value = paginator
    return s3


def test_empty_bucket_yields_no_skills():
    """An ``s3://<bucket>/skills/`` tree with no children yields nothing."""
    # A single empty page is the realistic shape boto3 returns for an
    # empty prefix — ``CommonPrefixes`` is omitted entirely rather than
    # being an empty list.
    s3 = _make_s3([{}])

    result = list(list_legacy_skills(s3, "my-bucket"))

    assert result == []
    s3.get_paginator.assert_called_once_with("list_objects_v2")
    s3.get_paginator.return_value.paginate.assert_called_once_with(
        Bucket="my-bucket", Prefix="skills/", Delimiter="/"
    )


def test_three_skill_prefixes_yield_in_order():
    """Three sibling skill prefixes round-trip into three LegacySkill rows."""
    pages = [
        {
            "CommonPrefixes": [
                {"Prefix": "skills/alpha/"},
                {"Prefix": "skills/beta/"},
                {"Prefix": "skills/gamma/"},
            ]
        }
    ]
    s3 = _make_s3(pages)

    result = list(list_legacy_skills(s3, "my-bucket"))

    assert result == [
        LegacySkill(legacy_key_prefix="skills/alpha/", name="alpha"),
        LegacySkill(legacy_key_prefix="skills/beta/", name="beta"),
        LegacySkill(legacy_key_prefix="skills/gamma/", name="gamma"),
    ]
    # ``name`` strips both the ``skills/`` prefix and the trailing slash.
    assert all(not s.name.endswith("/") for s in result)
    assert all(not s.name.startswith("skills/") for s in result)


def test_pagination_across_multiple_pages():
    """Driver receives every CommonPrefixes entry across pages."""
    pages = [
        {
            "CommonPrefixes": [
                {"Prefix": "skills/one/"},
                {"Prefix": "skills/two/"},
            ]
        },
        {
            "CommonPrefixes": [
                {"Prefix": "skills/three/"},
            ]
        },
    ]
    s3 = _make_s3(pages)

    result = list(list_legacy_skills(s3, "my-bucket"))

    assert [s.name for s in result] == ["one", "two", "three"]
    assert [s.legacy_key_prefix for s in result] == [
        "skills/one/",
        "skills/two/",
        "skills/three/",
    ]


def test_page_with_only_contents_is_tolerated():
    """A page that contains object keys but no ``CommonPrefixes`` yields nothing."""
    pages = [
        {"Contents": [{"Key": "skills/loose-object.json"}]},
        {"CommonPrefixes": [{"Prefix": "skills/real/"}]},
    ]
    s3 = _make_s3(pages)

    result = list(list_legacy_skills(s3, "my-bucket"))

    assert result == [LegacySkill(legacy_key_prefix="skills/real/", name="real")]


def test_is_a_lazy_generator():
    """``list_legacy_skills`` is a generator — calling it does not paginate."""
    s3 = _make_s3([{"CommonPrefixes": [{"Prefix": "skills/a/"}]}])

    gen = list_legacy_skills(s3, "my-bucket")

    # Until the caller iterates, the paginator is not invoked.
    s3.get_paginator.return_value.paginate.assert_not_called()

    first = next(gen)

    assert first == LegacySkill(legacy_key_prefix="skills/a/", name="a")
