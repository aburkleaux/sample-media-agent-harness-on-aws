"""Unit tests for S3UserFilesTool.

Validates: Requirements 1.3, 1.5, 9.3, 9.4
Properties: 1, 4, 20
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncio

import boto3
import pytest
from moto import mock_aws

from lab.s3_user_files_tool import S3UserFilesTool
from lab.path_validator import PathTraversalError

TEST_BUCKET = "test-artifacts-bucket"
TEST_ACTOR = "actor-123"


@pytest.fixture
def s3_setup():
    """Set up mocked S3 bucket and return the S3UserFilesTool instance."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_BUCKET)
        tool = S3UserFilesTool(bucket=TEST_BUCKET, actor_id=TEST_ACTOR)
        yield tool


def run(coro):
    """Helper to run async coroutines in tests."""
    return asyncio.run(coro)


class TestConstructor:
    """Constructor validation."""

    def test_rejects_empty_bucket(self):
        with pytest.raises(ValueError, match="bucket must not be empty"):
            S3UserFilesTool(bucket="", actor_id="actor-1")

    def test_rejects_empty_actor_id(self):
        with pytest.raises(ValueError, match="actor_id must not be empty"):
            S3UserFilesTool(bucket="bucket", actor_id="")

    @mock_aws
    def test_stores_properties(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket")
        tool = S3UserFilesTool(bucket="test-bucket", actor_id="actor-1")
        assert tool.bucket == "test-bucket"
        assert tool.actor_id == "actor-1"


class TestResolveKey:
    """resolve_key validates paths and prepends actor prefix."""

    @mock_aws
    def test_simple_path(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket")
        tool = S3UserFilesTool(bucket="test-bucket", actor_id="actor-1")
        assert tool.resolve_key("drafts/skills/abc/SKILL.md") == "actor-1/drafts/skills/abc/SKILL.md"

    @mock_aws
    def test_rejects_dotdot(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket")
        tool = S3UserFilesTool(bucket="test-bucket", actor_id="actor-1")
        with pytest.raises(PathTraversalError):
            tool.resolve_key("../escape")

    @mock_aws
    def test_rejects_leading_slash(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket")
        tool = S3UserFilesTool(bucket="test-bucket", actor_id="actor-1")
        with pytest.raises(PathTraversalError):
            tool.resolve_key("/absolute/path")

    @mock_aws
    def test_rejects_null_byte(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket")
        tool = S3UserFilesTool(bucket="test-bucket", actor_id="actor-1")
        with pytest.raises(PathTraversalError):
            tool.resolve_key("file\x00.txt")

    @mock_aws
    def test_rejects_empty_path(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket")
        tool = S3UserFilesTool(bucket="test-bucket", actor_id="actor-1")
        with pytest.raises(PathTraversalError):
            tool.resolve_key("")


class TestWrite:
    """write() stores objects under actor prefix."""

    def test_write_creates_object(self, s3_setup):
        tool = s3_setup
        run(tool.write("drafts/skills/abc/SKILL.md", b"# My Skill"))

        # Verify directly via boto3
        s3 = boto3.client("s3", region_name="us-east-1")
        obj = s3.get_object(Bucket=TEST_BUCKET, Key=f"{TEST_ACTOR}/drafts/skills/abc/SKILL.md")
        assert obj["Body"].read() == b"# My Skill"

    def test_write_rejects_traversal(self, s3_setup):
        tool = s3_setup
        with pytest.raises(PathTraversalError):
            run(tool.write("../other-actor/secret.txt", b"data"))

    def test_write_rejects_absolute_path(self, s3_setup):
        tool = s3_setup
        with pytest.raises(PathTraversalError):
            run(tool.write("/etc/passwd", b"data"))


class TestRead:
    """read() retrieves objects from actor prefix."""

    def test_read_existing_object(self, s3_setup):
        tool = s3_setup
        run(tool.write("test-file.txt", b"hello world"))
        content = run(tool.read("test-file.txt"))
        assert content == b"hello world"

    def test_read_nonexistent_raises_not_found(self, s3_setup):
        tool = s3_setup
        with pytest.raises(FileNotFoundError):
            run(tool.read("nonexistent.txt"))

    def test_read_rejects_traversal(self, s3_setup):
        tool = s3_setup
        with pytest.raises(PathTraversalError):
            run(tool.read("../../secret"))


class TestList:
    """list() returns paths relative to actor prefix."""

    def test_list_objects_under_prefix(self, s3_setup):
        tool = s3_setup
        run(tool.write("drafts/skills/abc/SKILL.md", b"content"))
        run(tool.write("drafts/skills/abc/refs/guide.md", b"guide"))
        run(tool.write("drafts/skills/xyz/SKILL.md", b"other"))

        results = run(tool.list("drafts/skills/abc/"))
        assert sorted(results) == [
            "drafts/skills/abc/SKILL.md",
            "drafts/skills/abc/refs/guide.md",
        ]

    def test_list_empty_prefix_returns_empty(self, s3_setup):
        tool = s3_setup
        results = run(tool.list("drafts/skills/nonexistent/"))
        assert results == []

    def test_list_rejects_traversal_in_prefix(self, s3_setup):
        tool = s3_setup
        with pytest.raises(PathTraversalError):
            run(tool.list("../other-actor/"))

    def test_list_returns_relative_paths(self, s3_setup):
        tool = s3_setup
        run(tool.write("lab-runs/run1/manifest.json", b"{}"))

        results = run(tool.list("lab-runs/run1/"))
        assert results == ["lab-runs/run1/manifest.json"]


class TestDelete:
    """delete() removes a single object."""

    def test_delete_existing_object(self, s3_setup):
        tool = s3_setup
        run(tool.write("file-to-delete.txt", b"data"))
        run(tool.delete("file-to-delete.txt"))

        with pytest.raises(FileNotFoundError):
            run(tool.read("file-to-delete.txt"))

    def test_delete_nonexistent_succeeds(self, s3_setup):
        """S3 delete is idempotent — deleting a non-existent key does not error."""
        tool = s3_setup
        run(tool.delete("nonexistent.txt"))  # Should not raise

    def test_delete_rejects_traversal(self, s3_setup):
        tool = s3_setup
        with pytest.raises(PathTraversalError):
            run(tool.delete("../other-actor/file.txt"))


class TestDeletePrefix:
    """delete_prefix() removes all objects under a prefix."""

    def test_delete_prefix_removes_all_objects(self, s3_setup):
        tool = s3_setup
        run(tool.write("drafts/skills/abc/SKILL.md", b"content"))
        run(tool.write("drafts/skills/abc/refs/guide.md", b"guide"))
        run(tool.write("drafts/skills/abc/refs/api.md", b"api"))

        run(tool.delete_prefix("drafts/skills/abc/"))

        results = run(tool.list("drafts/skills/abc/"))
        assert results == []

    def test_delete_prefix_does_not_affect_other_prefixes(self, s3_setup):
        tool = s3_setup
        run(tool.write("drafts/skills/abc/SKILL.md", b"abc"))
        run(tool.write("drafts/skills/xyz/SKILL.md", b"xyz"))

        run(tool.delete_prefix("drafts/skills/abc/"))

        # xyz should still exist
        content = run(tool.read("drafts/skills/xyz/SKILL.md"))
        assert content == b"xyz"

    def test_delete_prefix_empty_prefix_succeeds(self, s3_setup):
        """Deleting an empty prefix (no objects) should succeed."""
        tool = s3_setup
        run(tool.delete_prefix("drafts/skills/nonexistent/"))

    def test_delete_prefix_rejects_traversal(self, s3_setup):
        tool = s3_setup
        with pytest.raises(PathTraversalError):
            run(tool.delete_prefix("../other-actor/"))

    def test_delete_prefix_is_idempotent(self, s3_setup):
        """Calling delete_prefix twice should succeed both times."""
        tool = s3_setup
        run(tool.write("drafts/skills/abc/SKILL.md", b"content"))

        run(tool.delete_prefix("drafts/skills/abc/"))
        run(tool.delete_prefix("drafts/skills/abc/"))  # Should not raise

        results = run(tool.list("drafts/skills/abc/"))
        assert results == []


class TestAbsolutePrefixOperations:
    """Canonical lifecycle prefixes resolve to the physical draft layout."""

    def test_copy_and_cleanup_translate_private_draft_prefixes(self, s3_setup):
        tool = s3_setup
        source_relative = "drafts/skills/source/SKILL.md"
        run(tool.write(source_relative, b"# Source"))

        canonical_source = f"users/{TEST_ACTOR}/drafts/skills/source/"
        published_prefix = f"users/{TEST_ACTOR}/published/skills/source/v1/"
        run(tool.copy_prefix_absolute(canonical_source, published_prefix))

        s3 = boto3.client("s3", region_name="us-east-1")
        published = s3.get_object(
            Bucket=TEST_BUCKET,
            Key=f"{published_prefix}SKILL.md",
        )
        assert published["Body"].read() == b"# Source"

        canonical_fork = f"users/{TEST_ACTOR}/drafts/skills/fork/"
        run(tool.copy_prefix_absolute(published_prefix, canonical_fork))
        assert run(tool.read("drafts/skills/fork/SKILL.md")) == b"# Source"

        run(tool.delete_prefix_absolute(canonical_fork))
        with pytest.raises(FileNotFoundError):
            run(tool.read("drafts/skills/fork/SKILL.md"))

    def test_read_absolute_resolves_a_legacy_actor_scoped_object(self, s3_setup):
        """``read_absolute`` must see objects the actor-scoped ``write`` placed.

        Both conventions exist in deployed data, and every other absolute helper
        already handles that: ``list_absolute`` searches both prefixes and
        ``copy_prefix_absolute`` probes per object via ``_physical_key``.
        ``read_absolute`` read the literal key only, which made it the one helper
        blind to its siblings' objects — so a skill draft written before the
        canonical-path fix could not be read back by canonical key.
        """
        tool = s3_setup
        run(tool.write("drafts/skills/legacy/SKILL.md", b"# Legacy"))

        canonical = f"users/{TEST_ACTOR}/drafts/skills/legacy/SKILL.md"
        assert run(tool.read_absolute(canonical)) == b"# Legacy"

    def test_delete_absolute_removes_a_legacy_actor_scoped_object(self, s3_setup):
        """Deleting by canonical key must not orphan a legacy-placed object."""
        tool = s3_setup
        run(tool.write("drafts/skills/legacy-del/notes.md", b"# Notes"))

        canonical = f"users/{TEST_ACTOR}/drafts/skills/legacy-del/notes.md"
        run(tool.delete_absolute(canonical))
        with pytest.raises(FileNotFoundError):
            run(tool.read("drafts/skills/legacy-del/notes.md"))

    def test_delete_prefix_absolute_sweeps_both_conventions(self, s3_setup):
        """A prefix may hold objects written either way; reclaim must clear both.

        Deleting only the translated prefix left canonically-written objects
        behind, which stopped a failed skill-package import from reclaiming its
        own artifacts.
        """
        tool = s3_setup
        canonical_prefix = f"users/{TEST_ACTOR}/drafts/skills/mixed/"
        run(tool.write("drafts/skills/mixed/legacy.md", b"# Legacy"))        # translated
        run(tool.write_absolute(f"{canonical_prefix}canonical.md", b"# Canon"))  # literal

        run(tool.delete_prefix_absolute(canonical_prefix))

        assert run(tool.list_absolute(canonical_prefix)) == []

    def test_foreign_canonical_draft_prefix_is_rejected(self, s3_setup):
        tool = s3_setup
        foreign_prefix = "users/other-actor/drafts/skills/private/"
        target_prefix = f"users/{TEST_ACTOR}/published/skills/private/v1/"

        with pytest.raises(PathTraversalError, match="different actor"):
            run(tool.copy_prefix_absolute(foreign_prefix, target_prefix))
        with pytest.raises(PathTraversalError, match="different actor"):
            run(tool.delete_prefix_absolute(foreign_prefix))


class TestTenantIsolation:
    """Verify that operations cannot escape the actor's prefix.

    Validates Property 1: all resolved S3 keys start with `{actor_id}/`.
    """

    @mock_aws
    def test_different_actors_isolated(self):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_BUCKET)

        tool_a = S3UserFilesTool(bucket=TEST_BUCKET, actor_id="actor-a")
        tool_b = S3UserFilesTool(bucket=TEST_BUCKET, actor_id="actor-b")

        run(tool_a.write("secret.txt", b"actor-a-secret"))

        # actor-b cannot read actor-a's file
        with pytest.raises(FileNotFoundError):
            run(tool_b.read("secret.txt"))

    @mock_aws
    def test_list_only_shows_own_files(self):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_BUCKET)

        tool_a = S3UserFilesTool(bucket=TEST_BUCKET, actor_id="actor-a")
        tool_b = S3UserFilesTool(bucket=TEST_BUCKET, actor_id="actor-b")

        run(tool_a.write("drafts/file-a.txt", b"a"))
        run(tool_b.write("drafts/file-b.txt", b"b"))

        results_a = run(tool_a.list("drafts/"))
        results_b = run(tool_b.list("drafts/"))

        assert results_a == ["drafts/file-a.txt"]
        assert results_b == ["drafts/file-b.txt"]


class TestAbsoluteMethods:
    """Bucket-absolute single-object I/O (BUG-2 fix).

    The lab-asset CRUD layer writes/reads artifacts at the canonical grammar
    prefix (``users/<owner>/...``) recorded on the AssetIndex row — NOT the
    actor-scoped ``<actor>/`` prefix — so the physical location matches what
    ``promote_asset`` copies and the runtime consumers read.
    """

    @mock_aws
    def test_write_absolute_lands_at_exact_key_no_actor_prefix(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_BUCKET)
        tool = S3UserFilesTool(bucket=TEST_BUCKET, actor_id=TEST_ACTOR)
        key = f"users/{TEST_ACTOR}/drafts/specialty-agents/a1/persona.md"

        run(tool.write_absolute(key, b"You are Bot."))

        # Object exists at the EXACT canonical key (no `<actor>/` head prepended).
        raw = boto3.client("s3", region_name="us-east-1").get_object(
            Bucket=TEST_BUCKET, Key=key
        )
        assert raw["Body"].read() == b"You are Bot."

    @mock_aws
    def test_write_then_read_absolute_round_trips(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_BUCKET)
        tool = S3UserFilesTool(bucket=TEST_BUCKET, actor_id=TEST_ACTOR)
        key = f"users/{TEST_ACTOR}/drafts/skills/s1/SKILL.md"

        run(tool.write_absolute(key, b"# skill"))
        assert run(tool.read_absolute(key)) == b"# skill"

    @mock_aws
    def test_read_absolute_missing_raises_file_not_found(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_BUCKET)
        tool = S3UserFilesTool(bucket=TEST_BUCKET, actor_id=TEST_ACTOR)
        with pytest.raises(FileNotFoundError):
            run(tool.read_absolute("users/x/drafts/skills/nope/SKILL.md"))

    @mock_aws
    def test_list_absolute_returns_fully_qualified_keys(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_BUCKET)
        tool = S3UserFilesTool(bucket=TEST_BUCKET, actor_id=TEST_ACTOR)
        prefix = f"users/{TEST_ACTOR}/drafts/skills/s2/"
        run(tool.write_absolute(f"{prefix}SKILL.md", b"a"))
        run(tool.write_absolute(f"{prefix}refs/g.md", b"b"))

        keys = run(tool.list_absolute(prefix))
        assert sorted(keys) == [f"{prefix}SKILL.md", f"{prefix}refs/g.md"]

    @mock_aws
    def test_list_absolute_empty_prefix_returns_empty(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_BUCKET)
        tool = S3UserFilesTool(bucket=TEST_BUCKET, actor_id=TEST_ACTOR)
        assert run(tool.list_absolute("users/x/drafts/skills/empty/")) == []

    @mock_aws
    def test_delete_absolute_removes_exact_key(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_BUCKET)
        tool = S3UserFilesTool(bucket=TEST_BUCKET, actor_id=TEST_ACTOR)
        key = f"users/{TEST_ACTOR}/drafts/skills/s3/extra.md"
        run(tool.write_absolute(key, b"x"))
        run(tool.delete_absolute(key))
        with pytest.raises(FileNotFoundError):
            run(tool.read_absolute(key))

    @mock_aws
    def test_absolute_methods_reject_traversal(self):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_BUCKET)
        tool = S3UserFilesTool(bucket=TEST_BUCKET, actor_id=TEST_ACTOR)
        with pytest.raises(PathTraversalError):
            run(tool.read_absolute("users/../../etc/passwd"))
        with pytest.raises(PathTraversalError):
            run(tool.list_absolute("users/x/../y/"))


class TestCrudPhysicalKeyRoundTrip:
    """End-to-end: a written draft artifact is readable at its recorded prefix.

    This is the coverage that BUG-2 lacked — the write-side and the
    recorded-prefix reader are cross-checked against the SAME object, so a
    ``<actor>/`` vs ``users/<actor>/`` divergence can never hide again.
    """

    @mock_aws
    def test_specialty_persona_readable_at_recorded_prefix(self):
        import time as _t
        from lab.specialty_crud import SpecialtyCRUD
        from lab.asset_index import AssetIndex
        from lab.context import LabRequestContext

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_BUCKET)
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = _make_asset_index_table(ddb)
        s3 = S3UserFilesTool(bucket=TEST_BUCKET, actor_id=TEST_ACTOR)
        index = AssetIndex(ddb_table=table, actor_id=TEST_ACTOR)
        crud = SpecialtyCRUD(s3=s3, index=index)
        ctx = LabRequestContext(actor_id=TEST_ACTOR, session_id="s", request_id="r")

        created = run(crud.create(ctx=ctx, name="Bot", persona="You are Bot."))
        recorded_prefix = created.s3_prefix

        # The recorded prefix must physically hold persona.md (BUG-2).
        raw = boto3.client("s3", region_name="us-east-1").get_object(
            Bucket=TEST_BUCKET, Key=f"{recorded_prefix}persona.md"
        )
        assert raw["Body"].read().decode("utf-8") == "You are Bot."
        # And CRUD.read round-trips it back.
        read_back = run(crud.read(ctx, created.asset_id))
        assert read_back.persona == "You are Bot."


def _make_asset_index_table(ddb):
    """Create a minimal AssetIndex-compatible table for round-trip tests."""
    table = ddb.create_table(
        TableName="test-asset-index-roundtrip",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "byPublishedId",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return table


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
