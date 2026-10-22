"""Validation rules for imported agentskills.io package folders."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .path_validator import validate_path

ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "license",
    "allowed-tools",
    "metadata",
}
ALLOWED_PACKAGE_DIRECTORIES = {"references", "scripts", "assets"}
MAX_PACKAGE_FILES = 100
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PACKAGE_BYTES = 5 * 1024 * 1024
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(?P<yaml>.*?)\r?\n---(?:\r?\n|\Z)",
    re.DOTALL,
)


class SkillPackageValidationError(ValueError):
    """Raised when an uploaded Skill package does not satisfy the schema."""


# Cap matching the strict validator's own limit, so a description recorded on the
# index row can never exceed what an imported package is allowed to declare.
_MAX_DESCRIPTION_CHARS = 1024


def extract_skill_description(skill_md: str) -> str:
    """The frontmatter ``description``, or ``""`` when there isn't a usable one.

    LENIENT on purpose, and deliberately not `validate_skill_document`. That one
    REQUIRES a description and raises without it, which is right for an import of a
    finished package but wrong on the save path: a draft is saved repeatedly while it
    is still being written, and a validating extractor would refuse to store a skill
    whose description the author has not typed yet. The caller wants "record what is
    there", so malformed or partial frontmatter yields an empty string rather than an
    exception.

    Truncates rather than rejecting for the same reason: an over-long description
    should not be able to fail a save, and the row is a display projection of the
    SKILL.md, which remains the source of truth in S3.
    """
    if not isinstance(skill_md, str) or not skill_md.strip():
        return ""

    match = _FRONTMATTER_RE.match(skill_md)
    if match is None:
        return ""

    # Imported here for the reason given in `validate_skill_document`: the projection
    # Lambda imports `lab` on a runtime with boto3 but no PyYAML.
    import yaml

    try:
        frontmatter = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError:
        return ""

    if not isinstance(frontmatter, Mapping):
        return ""

    description = frontmatter.get("description")
    if not isinstance(description, str):
        return ""
    return description.strip()[:_MAX_DESCRIPTION_CHARS]


def validate_skill_document(skill_md: str, *, expected_name: str) -> dict:
    """Validate SKILL.md frontmatter and return the parsed mapping."""
    if not isinstance(skill_md, str) or not skill_md.strip():
        raise SkillPackageValidationError("SKILL.md must be a non-empty UTF-8 text file")

    match = _FRONTMATTER_RE.match(skill_md)
    if match is None:
        raise SkillPackageValidationError(
            "SKILL.md must begin with YAML frontmatter enclosed by --- delimiters"
        )

    # PyYAML is imported here, not at module scope, so the whole `lab` package
    # stays importable with nothing beyond boto3 installed. The projection
    # Lambda (`capability.projector`) imports `lab` for the AssetIndex schema
    # and runs on the managed python3.13 runtime, which bundles boto3 but not
    # PyYAML — a module-level import here made that cold start fail outright
    # (R1, migration Task 6).
    import yaml

    try:
        frontmatter = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError as exc:
        raise SkillPackageValidationError(f"SKILL.md frontmatter is invalid YAML: {exc}") from exc

    if not isinstance(frontmatter, Mapping):
        raise SkillPackageValidationError("SKILL.md frontmatter must be a YAML mapping")

    unexpected = sorted(set(frontmatter) - ALLOWED_FRONTMATTER_KEYS)
    if unexpected:
        raise SkillPackageValidationError(
            "Unsupported SKILL.md frontmatter field(s): " + ", ".join(unexpected)
        )

    name = frontmatter.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SkillPackageValidationError("SKILL.md frontmatter requires a string name")
    name = name.strip()
    if len(name) > 64 or not _NAME_RE.fullmatch(name):
        raise SkillPackageValidationError(
            "SKILL.md name must be 1-64 lowercase letters, numbers, and single hyphens"
        )
    if name != expected_name:
        raise SkillPackageValidationError(
            f"SKILL.md name '{name}' must match the imported Skill name '{expected_name}'"
        )

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillPackageValidationError("SKILL.md frontmatter requires a string description")
    if len(description.strip()) > 1024:
        raise SkillPackageValidationError("SKILL.md description must be 1024 characters or fewer")
    if "<" in description or ">" in description:
        raise SkillPackageValidationError("SKILL.md description cannot contain angle brackets")

    license_value = frontmatter.get("license")
    if license_value is not None and not isinstance(license_value, str):
        raise SkillPackageValidationError("SKILL.md license must be a string")

    allowed_tools = frontmatter.get("allowed-tools")
    if allowed_tools is not None:
        valid_tools = isinstance(allowed_tools, str) or (
            isinstance(allowed_tools, Sequence)
            and not isinstance(allowed_tools, (str, bytes))
            and all(isinstance(item, str) for item in allowed_tools)
        )
        if not valid_tools:
            raise SkillPackageValidationError(
                "SKILL.md allowed-tools must be a string or list of strings"
            )

    metadata = frontmatter.get("metadata")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise SkillPackageValidationError("SKILL.md metadata must be a mapping")

    return dict(frontmatter)


def validate_skill_package(
    *,
    name: str,
    skill_md: str,
    supporting_files: list[tuple[str, bytes]],
) -> list[tuple[str, bytes]]:
    """Validate and normalize a complete folder import before any storage write."""
    if not isinstance(skill_md, str):
        raise SkillPackageValidationError("SKILL.md must be a non-empty UTF-8 text file")
    skill_md_bytes = skill_md.encode("utf-8")
    if len(skill_md_bytes) > MAX_FILE_BYTES:
        raise SkillPackageValidationError(
            f"SKILL.md exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit"
        )
    validate_skill_document(skill_md, expected_name=name)

    if len(supporting_files) + 1 > MAX_PACKAGE_FILES:
        raise SkillPackageValidationError(
            f"Skill package may contain at most {MAX_PACKAGE_FILES} files"
        )

    normalized_files: list[tuple[str, bytes]] = []
    seen_paths = {"skill.md"}
    total_bytes = len(skill_md.encode("utf-8"))

    for path, content in supporting_files:
        normalized = validate_path(path)
        path_key = normalized.casefold()
        if path_key in seen_paths:
            raise SkillPackageValidationError(f"Duplicate package path: {normalized}")
        seen_paths.add(path_key)

        components = normalized.split("/")
        if components[0] not in ALLOWED_PACKAGE_DIRECTORIES or len(components) < 2:
            raise SkillPackageValidationError(
                f"Supporting file '{normalized}' must be under references/, scripts/, or assets/"
            )
        if components[0] == "references" and len(components) != 2:
            raise SkillPackageValidationError(
                f"Reference '{normalized}' must be directly under references/"
            )
        if any(component.startswith(".") for component in components):
            raise SkillPackageValidationError(
                f"Hidden package path is not allowed: {normalized}"
            )
        if not isinstance(content, bytes):
            raise SkillPackageValidationError(f"Package file '{normalized}' has invalid content")
        if len(content) > MAX_FILE_BYTES:
            raise SkillPackageValidationError(
                f"Package file '{normalized}' exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit"
            )

        total_bytes += len(content)
        normalized_files.append((normalized, content))

    if total_bytes > MAX_PACKAGE_BYTES:
        raise SkillPackageValidationError(
            f"Skill package exceeds the {MAX_PACKAGE_BYTES // (1024 * 1024)} MB limit"
        )

    return normalized_files
