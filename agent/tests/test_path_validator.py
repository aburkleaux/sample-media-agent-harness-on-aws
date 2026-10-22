"""Unit tests for path_validator module.

Validates: Requirement 1.5 — reject any component containing `..`, a leading `/`,
or a path that resolves outside the `{Actor_Id}/` prefix.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from lab.path_validator import validate_path, PathTraversalError


class TestValidPaths:
    """Paths that should pass validation."""

    def test_simple_filename(self):
        assert validate_path("file.txt") == "file.txt"

    def test_nested_path(self):
        assert validate_path("references/guide.md") == "references/guide.md"

    def test_deeply_nested(self):
        assert validate_path("a/b/c/d/file.py") == "a/b/c/d/file.py"

    def test_filename_with_dots(self):
        assert validate_path("my.file.name.txt") == "my.file.name.txt"

    def test_filename_starting_with_dot(self):
        assert validate_path(".hidden") == ".hidden"

    def test_path_with_dashes_and_underscores(self):
        assert validate_path("my-dir/my_file.md") == "my-dir/my_file.md"

    def test_unicode_filename(self):
        assert validate_path("données/résumé.md") == "données/résumé.md"

    def test_skill_md_path(self):
        assert validate_path("SKILL.md") == "SKILL.md"

    def test_supporting_file_path(self):
        assert validate_path("references/api-guide.md") == "references/api-guide.md"


class TestDotDotRejection:
    """Paths with `..` segments must be rejected."""

    def test_simple_dotdot(self):
        with pytest.raises(PathTraversalError, match="'\\.\\.' segment"):
            validate_path("..")

    def test_dotdot_at_start(self):
        with pytest.raises(PathTraversalError, match="'\\.\\.' segment"):
            validate_path("../etc/passwd")

    def test_dotdot_in_middle(self):
        with pytest.raises(PathTraversalError, match="'\\.\\.' segment"):
            validate_path("a/../b")

    def test_dotdot_at_end(self):
        with pytest.raises(PathTraversalError, match="'\\.\\.' segment"):
            validate_path("a/b/..")

    def test_multiple_dotdots(self):
        with pytest.raises(PathTraversalError, match="'\\.\\.' segment"):
            validate_path("../../secret")


class TestLeadingSlashRejection:
    """Paths starting with `/` must be rejected."""

    def test_absolute_path(self):
        with pytest.raises(PathTraversalError, match="must not start with '/'"):
            validate_path("/etc/passwd")

    def test_root_only(self):
        with pytest.raises(PathTraversalError, match="must not start with '/'"):
            validate_path("/")

    def test_absolute_nested(self):
        with pytest.raises(PathTraversalError, match="must not start with '/'"):
            validate_path("/home/user/file.txt")


class TestNullByteRejection:
    """Paths containing null bytes must be rejected."""

    def test_null_in_middle(self):
        with pytest.raises(PathTraversalError, match="null byte"):
            validate_path("file\x00.txt")

    def test_null_at_start(self):
        with pytest.raises(PathTraversalError, match="null byte"):
            validate_path("\x00file.txt")

    def test_null_at_end(self):
        with pytest.raises(PathTraversalError, match="null byte"):
            validate_path("file.txt\x00")


class TestEmptyAndWhitespace:
    """Empty or whitespace-only paths must be rejected."""

    def test_empty_string(self):
        with pytest.raises(PathTraversalError, match="empty"):
            validate_path("")

    def test_whitespace_only(self):
        with pytest.raises(PathTraversalError, match="empty"):
            validate_path("   ")


class TestDoubleSlashRejection:
    """Double slashes (empty components) must be rejected."""

    def test_double_slash(self):
        with pytest.raises(PathTraversalError, match="empty segment"):
            validate_path("a//b")

    def test_trailing_slash(self):
        with pytest.raises(PathTraversalError, match="empty segment"):
            validate_path("a/b/")


class TestDotSegmentRejection:
    """Single dot segments must be rejected."""

    def test_dot_segment(self):
        with pytest.raises(PathTraversalError, match="'\\.' segment"):
            validate_path("a/./b")

    def test_dot_only(self):
        with pytest.raises(PathTraversalError, match="'\\.' segment"):
            validate_path(".")


class TestWindowsAbsolutePaths:
    """Windows-style absolute paths must be rejected."""

    def test_c_drive(self):
        with pytest.raises(PathTraversalError, match="absolute path"):
            validate_path("C:\\Windows\\System32")

    def test_d_drive_forward_slash(self):
        with pytest.raises(PathTraversalError, match="absolute path"):
            validate_path("D:/data/file.txt")


class TestDangerousUnicode:
    """Dangerous unicode characters must be rejected."""

    def test_rtl_override(self):
        with pytest.raises(PathTraversalError, match="dangerous unicode"):
            validate_path("file\u202ename.txt")

    def test_zero_width_space(self):
        with pytest.raises(PathTraversalError, match="dangerous unicode"):
            validate_path("file\u200bname.txt")

    def test_zero_width_joiner(self):
        with pytest.raises(PathTraversalError, match="dangerous unicode"):
            validate_path("dir\u200d/file.txt")

    def test_bom_character(self):
        with pytest.raises(PathTraversalError, match="dangerous unicode"):
            validate_path("\ufefffile.txt")

    def test_soft_hyphen(self):
        with pytest.raises(PathTraversalError, match="dangerous unicode"):
            validate_path("fi\u00adle.txt")


class TestPathTraversalErrorAttributes:
    """PathTraversalError should carry the path and reason."""

    def test_error_has_path(self):
        try:
            validate_path("../secret")
        except PathTraversalError as e:
            assert e.path == "../secret"
            assert "'..' segment" in e.reason

    def test_error_has_reason(self):
        try:
            validate_path("/absolute")
        except PathTraversalError as e:
            assert e.path == "/absolute"
            assert "must not start with '/'" in e.reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
