"""Legacy-skill discovery.

Enumerates every legacy skill prefix under ``s3://<bucket>/skills/<name>/``
by paginating ``ListObjectsV2`` with ``Prefix='skills/'`` and
``Delimiter='/'`` and yielding from ``CommonPrefixes``. The driver
consumes the iterator one entry at a time and the migration treats the
order as undefined (per design — discovery order does not affect the
final state).

Task 8.1 landed the skeleton; Task 8.2 implements the body using the
boto3 paginator API. The function is intentionally a generator so
callers consume lazily; this matters because legacy buckets can hold
many thousands of skill prefixes and we never need to materialise the
full list in memory.

Requirements: 14.1
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class LegacySkill:
    """A single legacy skill prefix discovered under ``skills/``.

    Attributes:
        legacy_key_prefix: The full S3 key prefix including the trailing
            slash (e.g. ``"skills/my-skill/"``). Used as the marker key
            suffix and as the source argument to ``copy.migrate_one``.
        name: The trailing path segment (e.g. ``"my-skill"``). Surfaced
            in reports and log lines so operators recognise familiar
            skill names rather than full S3 keys.
    """

    legacy_key_prefix: str
    name: str


def list_legacy_skills(s3: Any, bucket: str) -> Iterator[LegacySkill]:
    """Yield every legacy skill prefix under ``s3://<bucket>/skills/``.

    Paginates ``ListObjectsV2`` with ``Prefix='skills/'`` and
    ``Delimiter='/'`` so each page returns a flat list of one-level-deep
    sub-prefixes in ``CommonPrefixes``. Each entry is yielded as a
    :class:`LegacySkill` whose ``legacy_key_prefix`` retains the
    trailing slash and whose ``name`` is the bare directory segment.

    Pages that contain only object keys (``Contents``) without
    ``CommonPrefixes`` are tolerated; ``CommonPrefixes`` is fetched
    via ``page.get(...)`` so a missing key yields an empty list rather
    than raising.

    A defensive guard skips any ``Prefix`` value that does not look
    like a direct child of ``skills/`` (e.g. a missing trailing slash,
    a wrong root, or an extra path component). Under normal
    ``Delimiter='/'`` semantics S3 will never return such a value, but
    the guard keeps the migration from silently emitting malformed
    :class:`LegacySkill` entries if a future S3 behaviour or a
    hand-rolled test fake misbehaves.

    Args:
        s3: Any object exposing ``get_paginator(name)``; in production
            this is a boto3 ``s3`` client, in tests it is a
            ``MagicMock`` whose paginator chain returns canned pages.
        bucket: The artifacts bucket holding the legacy ``skills/``
            tree.

    Yields:
        :class:`LegacySkill` instances, one per ``CommonPrefixes``
        entry, in the order S3 returns them.
    """
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="skills/", Delimiter="/"):
        for entry in page.get("CommonPrefixes") or ():
            prefix = entry.get("Prefix")
            if not prefix:
                continue
            # Defensive: only accept ``skills/<name>/`` (single level).
            if not prefix.startswith("skills/") or not prefix.endswith("/"):
                continue
            name = prefix[len("skills/") : -1]
            if not name or "/" in name:
                continue
            yield LegacySkill(legacy_key_prefix=prefix, name=name)
