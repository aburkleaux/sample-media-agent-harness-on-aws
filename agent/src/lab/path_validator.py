"""Path-component validation for the MediaWorks Lab.

Security-critical module that prevents path-traversal attacks against the
actor-scoped S3 prefix. Used by S3UserFilesTool.resolve_key() before any
S3 call is issued.

Validates: Requirement 1.5 — reject any component containing `..`, a leading
`/`, or a path that resolves outside the `{Actor_Id}/` prefix.

This module also exposes the canonical S3-prefix round-trip helpers used
by every platform write path:

    * :func:`derive_record_prefix`  — render the canonical prefix from the
      record's scope/owner/category/id/version tuple per the grammar in the
      Unified MediaWorks Platform design.
    * :func:`parse_record_prefix`   — parse a canonical prefix back into
      that tuple.
    * :func:`validate_record_prefix` — assert that a record's cached
      ``s3Prefix`` equals the prefix derived from its fields, raising
      :class:`PrefixMismatchError` on mismatch.

These three helpers satisfy Requirements 1.1–1.6 and support Properties 1
and 12.
"""

import unicodedata
from typing import Any, Literal, NamedTuple, Optional

from .errors import PrefixMismatchError


class PathTraversalError(Exception):
    """Raised when a path fails validation due to traversal or injection risk."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Path traversal rejected: {reason} (path={path!r})")


def validate_path(path: str) -> str:
    """Validate and normalize a user-supplied path component.

    Returns the normalized path on success. Raises PathTraversalError if the
    path contains traversal sequences, absolute references, null bytes, or
    dangerous unicode characters.

    Args:
        path: A relative path string supplied by the user (e.g. "references/guide.md").

    Returns:
        The normalized, validated path string.

    Raises:
        PathTraversalError: If the path is unsafe for use as an S3 key suffix.
    """
    if not isinstance(path, str):
        raise PathTraversalError(str(path), "path must be a string")

    # Reject null bytes — these can truncate paths in C-based systems
    if "\x00" in path:
        raise PathTraversalError(path, "path contains null byte")

    # Reject empty path
    if not path or not path.strip():
        raise PathTraversalError(path, "path is empty")

    # Reject absolute paths (leading /)
    if path.startswith("/"):
        raise PathTraversalError(path, "path must not start with '/'")

    # Reject Windows-style absolute paths (e.g. C:\, D:/)
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        raise PathTraversalError(path, "absolute path not allowed")

    # Normalize unicode to NFC to prevent homoglyph/normalization attacks
    normalized = unicodedata.normalize("NFC", path)

    # Reject dangerous unicode characters that could manipulate path display
    # or processing: RTL override, zero-width chars, etc.
    _reject_dangerous_unicode(normalized)

    # Split on / and validate each component
    components = normalized.split("/")

    for component in components:
        # Reject parent-directory traversal
        if component == "..":
            raise PathTraversalError(path, "path contains '..' segment")

        # Reject empty components (double slashes like "a//b")
        if component == "":
            raise PathTraversalError(path, "path contains empty segment (double slash)")

        # Reject components that are just dots (current-dir reference is noise)
        if component == ".":
            raise PathTraversalError(path, "path contains '.' segment")

    # Final check: after normalization, verify the path doesn't resolve upward.
    # posixpath.normpath would collapse "../" but we already rejected ".." above.
    # This is a belt-and-suspenders check.
    import posixpath

    normed = posixpath.normpath(normalized)
    if normed.startswith("..") or normed.startswith("/"):
        raise PathTraversalError(path, "normalized path escapes prefix")

    return normalized


# Characters that are dangerous in path contexts — can manipulate display,
# cause normalization issues, or inject control sequences.
_DANGEROUS_CODEPOINTS = {
    "\u202e",  # RTL override — can reverse displayed path
    "\u202d",  # LTR override
    "\u202a",  # LTR embedding
    "\u202b",  # RTL embedding
    "\u202c",  # Pop directional formatting
    "\u2066",  # LTR isolate
    "\u2067",  # RTL isolate
    "\u2068",  # First strong isolate
    "\u2069",  # Pop directional isolate
    "\u200b",  # Zero-width space
    "\u200c",  # Zero-width non-joiner
    "\u200d",  # Zero-width joiner
    "\ufeff",  # BOM / zero-width no-break space
    "\u00ad",  # Soft hyphen (invisible in many renderers)
}


def _reject_dangerous_unicode(path: str) -> None:
    """Reject paths containing unicode characters that could bypass validation.

    Raises PathTraversalError if any dangerous codepoint is found.
    """
    for char in path:
        if char in _DANGEROUS_CODEPOINTS:
            raise PathTraversalError(
                path,
                f"path contains dangerous unicode character U+{ord(char):04X}",
            )
        # Also reject any character in the "Format" (Cf) category that we
        # haven't explicitly allowed — these are invisible control characters.
        if unicodedata.category(char) == "Cf" and char not in _DANGEROUS_CODEPOINTS:
            raise PathTraversalError(
                path,
                f"path contains invisible format character U+{ord(char):04X}",
            )


# ---------------------------------------------------------------------------
# Canonical S3-prefix round-trip
# ---------------------------------------------------------------------------
#
# Grammar (from design.md "S3 prefix layout"):
#
#   <draft_prefix>    ::= "users/" <userId> "/drafts/" <category_plural>
#                         "/" <draftId> "/"
#   <priv_pub_prefix> ::= "users/" <userId> "/published/" <category_plural>
#                         "/" <publishedId> "/v" <version> "/"
#   <grp_pub_prefix>  ::= "groups/" <groupId> "/published/" <category_plural>
#                         "/" <publishedId> "/v" <version> "/"
#   <org_pub_prefix>  ::= "org/" <orgId> "/published/" <category_plural>
#                         "/" <publishedId> "/v" <version> "/"
#   <tc_prefix>       ::= "tool-collections/" <id> "/"
#
# The AssetIndex `category` attribute is singular (`skill`,
# `specialty-agent`, `workflow`, `tool-collection`); the URL-friendly
# plural is what appears in S3 (`skills`, `specialty-agents`, `workflows`).
# Tool collections live at a flat prefix and do not carry a version.

Scope = Literal["private", "group", "org"]
Category = Literal["skill", "specialty-agent", "workflow", "tool-collection"]


# Bidirectional category map. Keep these as the single source of truth.
_CATEGORY_TO_PLURAL: dict[str, str] = {
    "skill": "skills",
    "specialty-agent": "specialty-agents",
    "workflow": "workflows",
    "tool-collection": "tool-collections",
    "hook": "hooks",
}
_PLURAL_TO_CATEGORY: dict[str, str] = {
    plural: singular for singular, plural in _CATEGORY_TO_PLURAL.items()
}


class RecordPrefixTuple(NamedTuple):
    """Parsed components of a canonical S3 record prefix.

    Exactly one of ``owner_user_id``/``owner_group_id``/``owner_org_id`` is
    populated, mirroring the AssetIndex row shape:

        - draft and ``scope="private"`` published rows  → ``owner_user_id``
        - ``scope="group"`` published rows              → ``owner_group_id``
        - ``scope="org"`` published rows and the flat
          ``tool-collections/<id>/`` prefix              → ``owner_org_id``
          (tool-collections are owned by a user in DDB but render under a
          flat top-level prefix; this tuple uses ``owner_user_id`` for them
          to match the design's ``USER#<u>#TOOL_COLLECTIONS`` PK).

    For drafts, ``asset_id`` is the ``draftId`` and ``version`` is ``None``.
    For published rows of a versioned category, ``asset_id`` is the
    ``publishedId`` and ``version`` is a positive int. For
    ``tool-collection``, ``version`` is ``None``.
    """

    scope: str  # "private" | "group" | "org" | "draft"
    owner_user_id: Optional[str]
    owner_group_id: Optional[str]
    owner_org_id: Optional[str]
    category: str  # singular
    asset_id: str  # draftId or publishedId
    version: Optional[int]


def derive_record_prefix(
    *,
    scope: str,
    owner_user_id: Optional[str],
    owner_group_id: Optional[str],
    owner_org_id: Optional[str],
    category: str,
    asset_id: str,
    version: Optional[int],
    status: Optional[str] = None,
) -> str:
    """Render the canonical S3 prefix for an AssetIndex record.

    The mapping from the singular AssetIndex ``category`` value to the
    plural URL segment is the only string-level transformation; everything
    else is straight interpolation per the grammar.

    Required combinations:

    +----------------+--------------------------+--------+--------------+
    | scope          | owner field required     | version| status hint  |
    +================+==========================+========+==============+
    | "private"      | owner_user_id            | int>0  | published    |
    | "private"      | owner_user_id            | None   | draft (when  |
    |                |                          |        | status=draft)|
    | "group"        | owner_group_id           | int>0  | published    |
    | "org"          | owner_org_id             | int>0  | published    |
    +----------------+--------------------------+--------+--------------+

    For ``category == "tool-collection"`` the prefix is the flat
    ``tool-collections/<asset_id>/`` form and ``version`` MUST be ``None``.

    The optional ``status`` hint disambiguates draft vs private-published
    when ``scope == "private"``: drafts have ``version is None`` and a
    ``status="draft"`` row, while a v1 publish would also pass through
    here. ``status`` defaults to ``None``; the caller is expected to pass
    it for clarity, but it is inferred from ``version`` when omitted
    (``version is None`` ⇒ draft).

    Args:
        scope: ``"private"``, ``"group"``, or ``"org"``.
        owner_user_id: User owner; required for drafts and ``private`` rows
            and for ``tool-collection``.
        owner_group_id: Group owner; required for ``scope="group"``.
        owner_org_id: Org owner; required for ``scope="org"``.
        category: Singular AssetIndex category.
        asset_id: ``draftId`` for drafts, ``publishedId`` for published rows,
            or the tool-collection id for ``category="tool-collection"``.
        version: Positive int for versioned published rows; ``None`` for
            drafts and for ``tool-collection``.
        status: Optional explicit status (``"draft"``, ``"published"``,
            ``"deprecated"``). When provided, it overrides the
            ``version is None`` heuristic for draft detection.

    Returns:
        The canonical, slash-terminated S3 prefix string.

    Raises:
        ValueError: When the scope/owner/category/version combination is
            structurally invalid.
    """
    if category not in _CATEGORY_TO_PLURAL:
        raise ValueError(f"unknown category: {category!r}")
    plural = _CATEGORY_TO_PLURAL[category]

    if not asset_id or "/" in asset_id:
        raise ValueError(f"invalid asset_id: {asset_id!r}")

    # Tool collections are flat — no scope/version sub-tier.
    if category == "tool-collection":
        if version is not None:
            raise ValueError(
                "tool-collection prefix must not carry a version"
            )
        # The owner attributes do not appear in the rendered prefix for
        # tool-collections (the prefix is flat) but we still require an
        # owner_user_id on the AssetIndex side; we simply do not emit it
        # here.
        return f"{plural}/{asset_id}/"

    # Decide draft vs published by status hint, falling back to version.
    is_draft = (status == "draft") if status is not None else (version is None)

    if is_draft:
        # Drafts are always under the user's actor root with no version.
        if scope != "private":
            raise ValueError(
                f"drafts must have scope='private', got scope={scope!r}"
            )
        if owner_user_id is None:
            raise ValueError("draft prefix requires owner_user_id")
        if version is not None:
            raise ValueError("draft prefix must not carry a version")
        return f"users/{owner_user_id}/drafts/{plural}/{asset_id}/"

    # Published rows: version is required and positive.
    if not isinstance(version, int) or version <= 0:
        raise ValueError(
            f"published prefix requires positive int version, got {version!r}"
        )

    if scope == "private":
        if owner_user_id is None:
            raise ValueError("scope='private' requires owner_user_id")
        return (
            f"users/{owner_user_id}/published/{plural}/"
            f"{asset_id}/v{version}/"
        )
    if scope == "group":
        if not owner_group_id:
            raise ValueError("scope='group' requires owner_group_id")
        return (
            f"groups/{owner_group_id}/published/{plural}/"
            f"{asset_id}/v{version}/"
        )
    if scope == "org":
        if not owner_org_id:
            raise ValueError("scope='org' requires owner_org_id")
        return (
            f"org/{owner_org_id}/published/{plural}/"
            f"{asset_id}/v{version}/"
        )
    raise ValueError(f"unknown scope: {scope!r}")


def parse_record_prefix(s3_prefix: str) -> RecordPrefixTuple:
    """Parse a canonical S3 record prefix into its components.

    The inverse of :func:`derive_record_prefix`. Accepts only the exact
    canonical forms produced by that function (trailing slash required,
    plural category segment, ``v<n>`` version segment).

    Returns a :class:`RecordPrefixTuple` where ``scope`` is one of
    ``"private"``, ``"group"``, ``"org"``, or — for draft prefixes —
    ``"draft"``. (The AssetIndex ``scope`` field uses ``"private"`` for
    drafts, but at the prefix level the draft path is structurally
    distinct from a private-published path, so we surface that here.) The
    plural URL segment is converted back to the singular AssetIndex
    category value.

    Raises:
        ValueError: If the prefix does not match any canonical grammar
            production.
    """
    if not isinstance(s3_prefix, str) or not s3_prefix:
        raise ValueError(f"empty s3_prefix: {s3_prefix!r}")
    if not s3_prefix.endswith("/"):
        raise ValueError(f"s3_prefix must end with '/': {s3_prefix!r}")

    # Strip the trailing slash; reject any leading slash up-front.
    if s3_prefix.startswith("/"):
        raise ValueError(f"s3_prefix must not start with '/': {s3_prefix!r}")
    parts = s3_prefix[:-1].split("/")

    # Reject empty segments produced by "//" inside the prefix.
    if any(p == "" for p in parts):
        raise ValueError(
            f"s3_prefix has empty segment: {s3_prefix!r}"
        )

    # tool-collections/<id>/  → flat (2 segments)
    if parts[0] == "tool-collections":
        if len(parts) != 2:
            raise ValueError(
                f"malformed tool-collection prefix: {s3_prefix!r}"
            )
        return RecordPrefixTuple(
            scope="org",  # surfaced as "org" for symmetry with the design
            owner_user_id=None,
            owner_group_id=None,
            owner_org_id=None,
            category="tool-collection",
            asset_id=parts[1],
            version=None,
        )

    if parts[0] == "users":
        # users/<userId>/drafts/<plural>/<draftId>/             (5 segments)
        # users/<userId>/published/<plural>/<publishedId>/v<n>/ (6 segments)
        if len(parts) < 5:
            raise ValueError(f"malformed users/ prefix: {s3_prefix!r}")
        owner_user_id = parts[1]
        kind = parts[2]
        plural = parts[3]
        category = _PLURAL_TO_CATEGORY.get(plural)
        if category is None:
            raise ValueError(
                f"unknown category segment {plural!r} in {s3_prefix!r}"
            )
        if kind == "drafts":
            if len(parts) != 5:
                raise ValueError(
                    f"malformed draft prefix: {s3_prefix!r}"
                )
            return RecordPrefixTuple(
                scope="draft",
                owner_user_id=owner_user_id,
                owner_group_id=None,
                owner_org_id=None,
                category=category,
                asset_id=parts[4],
                version=None,
            )
        if kind == "published":
            if len(parts) != 6:
                raise ValueError(
                    f"malformed user-published prefix: {s3_prefix!r}"
                )
            asset_id = parts[4]
            version = _parse_version_segment(parts[5], s3_prefix)
            return RecordPrefixTuple(
                scope="private",
                owner_user_id=owner_user_id,
                owner_group_id=None,
                owner_org_id=None,
                category=category,
                asset_id=asset_id,
                version=version,
            )
        raise ValueError(
            f"unknown users/ sub-tier {kind!r} in {s3_prefix!r}"
        )

    if parts[0] == "groups":
        # groups/<groupId>/published/<plural>/<publishedId>/v<n>/  (6 segments)
        if len(parts) != 6 or parts[2] != "published":
            raise ValueError(f"malformed groups/ prefix: {s3_prefix!r}")
        plural = parts[3]
        category = _PLURAL_TO_CATEGORY.get(plural)
        if category is None:
            raise ValueError(
                f"unknown category segment {plural!r} in {s3_prefix!r}"
            )
        version = _parse_version_segment(parts[5], s3_prefix)
        return RecordPrefixTuple(
            scope="group",
            owner_user_id=None,
            owner_group_id=parts[1],
            owner_org_id=None,
            category=category,
            asset_id=parts[4],
            version=version,
        )

    if parts[0] == "org":
        # org/<orgId>/published/<plural>/<publishedId>/v<n>/      (6 segments)
        if len(parts) != 6 or parts[2] != "published":
            raise ValueError(f"malformed org/ prefix: {s3_prefix!r}")
        plural = parts[3]
        category = _PLURAL_TO_CATEGORY.get(plural)
        if category is None:
            raise ValueError(
                f"unknown category segment {plural!r} in {s3_prefix!r}"
            )
        version = _parse_version_segment(parts[5], s3_prefix)
        return RecordPrefixTuple(
            scope="org",
            owner_user_id=None,
            owner_group_id=None,
            owner_org_id=parts[1],
            category=category,
            asset_id=parts[4],
            version=version,
        )

    raise ValueError(f"unknown root segment in s3_prefix: {s3_prefix!r}")


def _parse_version_segment(seg: str, full_prefix: str) -> int:
    """Parse a ``v<n>`` segment into a positive integer.

    Rejects leading zeros (e.g., ``v01``) and non-positive values to keep
    ``derive_record_prefix(parse_record_prefix(p)) == p`` true: the
    derive side emits ``v<int>`` with no padding.
    """
    if not seg.startswith("v") or len(seg) < 2:
        raise ValueError(
            f"malformed version segment {seg!r} in {full_prefix!r}"
        )
    digits = seg[1:]
    if not digits.isdigit():
        raise ValueError(
            f"non-numeric version segment {seg!r} in {full_prefix!r}"
        )
    # Reject leading zero padding so the round-trip is canonical.
    if len(digits) > 1 and digits[0] == "0":
        raise ValueError(
            f"version segment must not have leading zeros: "
            f"{seg!r} in {full_prefix!r}"
        )
    n = int(digits)
    if n <= 0:
        raise ValueError(
            f"version must be positive: {seg!r} in {full_prefix!r}"
        )
    return n


def validate_record_prefix(
    record: Any,
    *,
    owner_user_id: Optional[str] = None,
    owner_group_id: Optional[str] = None,
    owner_org_id: Optional[str] = None,
) -> None:
    """Assert that ``record.s3_prefix`` matches the prefix derived from
    the record's scope/owner/category/asset_id/version tuple.

    The owner attributes are passed in by the caller because they live on
    :class:`~lab.context.LabRequestContext`, not on the record itself.
    Exactly one of the three keyword arguments should be populated for any
    given record:

        - drafts and ``scope="private"`` published rows ⇒ ``owner_user_id``
        - ``scope="group"`` published rows              ⇒ ``owner_group_id``
        - ``scope="org"`` published rows                ⇒ ``owner_org_id``

    For ``category == "tool-collection"``, all three may be ``None`` (the
    flat prefix does not embed an owner segment).

    Args:
        record: An :class:`~lab.models.AssetCommon` (or compatible duck
            type) carrying ``s3_prefix``, ``scope``, ``category``,
            ``asset_id``, ``version``, and ``status``.
        owner_user_id: Owner user id from the request context.
        owner_group_id: Owner group id from the request context.
        owner_org_id: Owner org id from the request context.

    Raises:
        PrefixMismatchError: When the record's stored ``s3_prefix`` is not
            equal to the derived canonical prefix. Includes both expected
            and actual values for diagnosability.
    """
    expected = derive_record_prefix(
        scope=record.scope,
        owner_user_id=owner_user_id,
        owner_group_id=owner_group_id,
        owner_org_id=owner_org_id,
        category=record.category,
        asset_id=record.asset_id,
        version=record.version,
        status=getattr(record, "status", None),
    )
    actual = record.s3_prefix
    if expected != actual:
        raise PrefixMismatchError(expected=expected, actual=actual)
