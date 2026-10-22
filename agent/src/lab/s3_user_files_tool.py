"""S3UserFilesTool — tenant-safe S3 wrapper for the MediaWorks Lab.

Every read/write/list/delete operation auto-prefixes with `{actor_id}/` and
rejects any path that would escape the actor's prefix. This is the ONLY
function that constructs keys against the Artifacts_Bucket.

Requirements: 1.3, 1.5, 9.3, 9.4
Properties: 1, 4, 20
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from .path_validator import PathTraversalError, validate_path

logger = logging.getLogger(__name__)

# Retry configuration for delete_prefix (Requirement 9.10)
_MAX_DELETE_RETRIES = 3
_RETRY_BASE_DELAY_SECONDS = 0.5


class S3UserFilesTool:
    """Tenant-scoped S3 wrapper that enforces actor-prefix isolation.

    All operations prepend `{actor_id}/` to the resolved key and reject any
    path that would escape the actor's prefix. This is the single point of
    key construction for the Artifacts_Bucket.

    Args:
        bucket: The S3 bucket name (Artifacts_Bucket).
        actor_id: The authenticated actor identity from LabRequestContext.
    """

    def __init__(self, bucket: str, actor_id: str) -> None:
        if not bucket:
            raise ValueError("bucket must not be empty")
        if not actor_id:
            raise ValueError("actor_id must not be empty")

        self._bucket = bucket
        self._actor_id = actor_id
        self._prefix = f"{actor_id}/"
        self._s3 = boto3.client("s3")

    @property
    def bucket(self) -> str:
        """The S3 bucket name."""
        return self._bucket

    @property
    def actor_id(self) -> str:
        """The bound actor identity."""
        return self._actor_id

    def resolve_key(self, path: str) -> str:
        """Validate a user-supplied path and prepend the actor prefix.

        This is the ONLY function that constructs S3 keys. It validates the
        path via `path_validator.validate_path()` then prepends `{actor_id}/`.
        After construction, it verifies the resolved key starts with the
        expected prefix as a belt-and-suspenders check.

        Args:
            path: A relative path string (e.g. "drafts/skills/abc/SKILL.md").

        Returns:
            The fully-qualified S3 key (e.g. "actor123/drafts/skills/abc/SKILL.md").

        Raises:
            PathTraversalError: If the path fails validation or the resolved key
                would escape the actor's prefix.
        """
        # Validate path components (rejects .., leading /, null bytes, etc.)
        validated = validate_path(path)

        # Prepend actor prefix
        resolved = f"{self._prefix}{validated}"

        # Belt-and-suspenders: verify the resolved key starts with actor prefix
        if not resolved.startswith(self._prefix):
            raise PathTraversalError(
                path, "resolved key escapes actor prefix"
            )

        return resolved

    async def read(self, path: str) -> bytes:
        """Read an object from S3 under the actor's prefix.

        Args:
            path: Relative path within the actor's namespace.

        Returns:
            The object content as bytes.

        Raises:
            PathTraversalError: If the path fails validation.
            FileNotFoundError: If the object does not exist.
            ClientError: On other S3 errors.
        """
        key = self.resolve_key(path)

        try:
            response = await asyncio.to_thread(
                self._s3.get_object, Bucket=self._bucket, Key=key
            )
            body = await asyncio.to_thread(response["Body"].read)
            return body
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                raise FileNotFoundError(
                    f"Object not found: {path}"
                ) from e
            raise

    async def write(self, path: str, data: bytes) -> None:
        """Write an object to S3 under the actor's prefix.

        Args:
            path: Relative path within the actor's namespace.
            data: The content to write as bytes.

        Raises:
            PathTraversalError: If the path fails validation.
            ClientError: On S3 errors.
        """
        key = self.resolve_key(path)

        await asyncio.to_thread(
            self._s3.put_object, Bucket=self._bucket, Key=key, Body=data
        )

    async def list(self, prefix: str) -> list[str]:
        """List objects under a prefix within the actor's namespace.

        Returns paths relative to `{actor_id}/`.

        Args:
            prefix: Relative prefix within the actor's namespace
                (e.g. "drafts/skills/abc/").

        Returns:
            List of paths relative to `{actor_id}/`.

        Raises:
            PathTraversalError: If the prefix fails validation.
            ClientError: On S3 errors.
        """
        # Validate the prefix — strip trailing slash for validation since
        # validate_path rejects trailing slashes (empty segment), but we
        # need to support prefix listing with trailing slash.
        prefix_for_validation = prefix.rstrip("/")
        if prefix_for_validation:
            validate_path(prefix_for_validation)

        full_prefix = f"{self._prefix}{prefix}"

        # Belt-and-suspenders: verify the full prefix starts with actor prefix
        if not full_prefix.startswith(self._prefix):
            raise PathTraversalError(
                prefix, "resolved prefix escapes actor prefix"
            )

        results: list[str] = []
        continuation_token: Optional[str] = None

        while True:
            kwargs = {
                "Bucket": self._bucket,
                "Prefix": full_prefix,
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token

            response = await asyncio.to_thread(
                self._s3.list_objects_v2, **kwargs
            )

            for obj in response.get("Contents", []):
                # Return paths relative to {actor_id}/
                relative_key = obj["Key"][len(self._prefix):]
                results.append(relative_key)

            if response.get("IsTruncated"):
                continuation_token = response.get("NextContinuationToken")
            else:
                break

        return results

    async def delete(self, path: str) -> None:
        """Delete a single object from S3 under the actor's prefix.

        Args:
            path: Relative path within the actor's namespace.

        Raises:
            PathTraversalError: If the path fails validation.
            ClientError: On S3 errors.
        """
        key = self.resolve_key(path)

        await asyncio.to_thread(
            self._s3.delete_object, Bucket=self._bucket, Key=key
        )

    async def delete_prefix(self, prefix: str) -> None:
        """Delete all objects under a prefix within the actor's namespace.

        Used by the two-phase soft-delete (Requirement 9.10). Implements
        retry logic with exponential backoff (up to 3 retries).

        Args:
            prefix: Relative prefix within the actor's namespace
                (e.g. "drafts/skills/abc/").

        Raises:
            PathTraversalError: If the prefix fails validation.
            ClientError: On S3 errors after all retries exhausted.
        """
        # Validate the prefix
        prefix_for_validation = prefix.rstrip("/")
        if prefix_for_validation:
            validate_path(prefix_for_validation)

        full_prefix = f"{self._prefix}{prefix}"

        # Belt-and-suspenders: verify the full prefix starts with actor prefix
        if not full_prefix.startswith(self._prefix):
            raise PathTraversalError(
                prefix, "resolved prefix escapes actor prefix"
            )

        last_error: Optional[Exception] = None

        for attempt in range(_MAX_DELETE_RETRIES):
            try:
                await self._do_delete_prefix(full_prefix)
                return  # Success
            except ClientError as e:
                last_error = e
                if attempt < _MAX_DELETE_RETRIES - 1:
                    delay = _RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
                    logger.warning(
                        "delete_prefix attempt %d failed for prefix=%s: %s. "
                        "Retrying in %.1fs...",
                        attempt + 1,
                        full_prefix,
                        str(e),
                        delay,
                    )
                    await asyncio.sleep(delay)

        # All retries exhausted
        logger.error(
            "delete_prefix failed after %d attempts for prefix=%s",
            _MAX_DELETE_RETRIES,
            full_prefix,
        )
        raise last_error  # type: ignore[misc]

    async def _do_delete_prefix(self, full_prefix: str) -> None:
        """Execute the actual batch-delete for all objects under a prefix.

        Lists all objects under the prefix and batch-deletes them in groups
        of up to 1000 (S3 batch delete limit).
        """
        continuation_token: Optional[str] = None

        while True:
            kwargs = {
                "Bucket": self._bucket,
                "Prefix": full_prefix,
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token

            response = await asyncio.to_thread(
                self._s3.list_objects_v2, **kwargs
            )

            objects = response.get("Contents", [])
            if objects:
                # Batch delete (max 1000 per request)
                delete_keys = [{"Key": obj["Key"]} for obj in objects]

                await asyncio.to_thread(
                    self._s3.delete_objects,
                    Bucket=self._bucket,
                    Delete={"Objects": delete_keys, "Quiet": True},
                )

            if response.get("IsTruncated"):
                continuation_token = response.get("NextContinuationToken")
            else:
                break

    # ------------------------------------------------------------------
    # Platform-level (cross-actor) operations
    # ------------------------------------------------------------------
    #
    # The canonical lifecycle operations (Promote, Share, Clone, ...) move
    # artifacts between scopes that span actor boundaries — for example a
    # Promote from a user's draft into the org-published tree, or a Share
    # that widens a private record to a group prefix. The actor-scoped
    # ``delete_prefix`` / ``write`` / ``read`` methods above are not safe
    # for those operations because they auto-prepend ``{actor_id}/``.
    #
    # The methods below take canonical bucket-absolute record prefixes and
    # are intended to be called only from the canonical-operation layer in
    # ``_canonical_ops.py``. Private draft records are the one exception:
    # AssetIndex uses ``users/<actor>/drafts/...`` while actor-scoped CRUD
    # physically stores ``<actor>/drafts/...``, so those prefixes are
    # translated internally. Inputs are still validated and must retain a
    # trailing slash.

    def _storage_prefix(self, prefix: str) -> str:
        """Translate a canonical private-draft prefix to its physical key.

        AssetIndex rows use ``users/<actor>/drafts/...`` while tenant-scoped
        CRUD stores those objects under ``<actor>/drafts/...``. Canonical
        lifecycle operations pass record prefixes to the absolute helpers, so
        translate only this request actor's private draft namespace. Published,
        group, and organization prefixes are already bucket-absolute.
        """
        canonical_draft_prefix = f"users/{self._actor_id}/drafts/"
        if prefix.startswith(canonical_draft_prefix):
            return f"{self._actor_id}/drafts/{prefix[len(canonical_draft_prefix):]}"
        if prefix.startswith("users/"):
            components = prefix.split("/")
            if len(components) > 2 and components[2] == "drafts":
                raise PathTraversalError(
                    prefix,
                    "canonical draft prefix belongs to a different actor",
                )
        return prefix

    def _physical_key(self, canonical_key: str) -> str:
        """Resolve a canonical object key to the key it is actually stored at.

        A private draft object may live at the literal canonical key (written
        via ``write_absolute``) or at the translated physical key (written via
        the actor-scoped ``write``). Probe the literal key first and fall back
        to the translated one.
        """
        translated = self._storage_prefix(canonical_key)
        if translated == canonical_key:
            return canonical_key
        try:
            self._s3.head_object(Bucket=self._bucket, Key=canonical_key)
            return canonical_key
        except ClientError:
            return translated

    async def read_absolute(self, key: str) -> bytes:
        """Read a single object by its BUCKET-ABSOLUTE key (no actor prefix).

        Companion to the actor-scoped :meth:`read`, for the lab-asset CRUD
        layer that writes/reads artifacts at the canonical grammar prefix
        (``users/<owner>/…``) recorded on the AssetIndex row — the same
        prefix the canonical-op copies and the runtime consumers address.
        Genuine per-user files still go through the actor-scoped methods.

        Args:
            key: Bucket-absolute object key (e.g.
                ``users/actor123/drafts/skills/abc/SKILL.md``).

        Returns:
            The object content as bytes.

        Raises:
            PathTraversalError: If the key fails validation.
            FileNotFoundError: If the object does not exist.
            ClientError: On other S3 errors.
        """
        validate_path(key)
        # Resolve through ``_physical_key`` for the same reason
        # ``list_absolute`` searches both prefixes and ``copy_prefix_absolute``
        # probes per object: a private draft written by the actor-scoped
        # ``write`` lives at the TRANSLATED key, and both conventions exist in
        # already-deployed data. Reading the literal key only made this the one
        # absolute helper that could not see its siblings' objects.
        # A foreign actor's canonical draft prefix raises in ``_storage_prefix``
        # (cross-tenant guard); keep that a plain not-found here, as before.
        try:
            physical = await asyncio.to_thread(self._physical_key, key)
        except PathTraversalError:
            physical = key
        try:
            response = await asyncio.to_thread(
                self._s3.get_object, Bucket=self._bucket, Key=physical
            )
            return await asyncio.to_thread(response["Body"].read)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                raise FileNotFoundError(f"Object not found: {key}") from e
            raise

    async def write_absolute(self, key: str, data: bytes) -> None:
        """Write a single object by its BUCKET-ABSOLUTE key (no actor prefix).

        Companion to the actor-scoped :meth:`write`; see :meth:`read_absolute`
        for why the lab-asset CRUD layer writes at the canonical grammar
        prefix rather than the actor-scoped one.

        Args:
            key: Bucket-absolute object key.
            data: The content to write as bytes.

        Raises:
            PathTraversalError: If the key fails validation.
            ClientError: On S3 errors.
        """
        validate_path(key)
        await asyncio.to_thread(
            self._s3.put_object, Bucket=self._bucket, Key=key, Body=data
        )

    async def delete_absolute(self, key: str) -> None:
        """Delete a single object by its BUCKET-ABSOLUTE key (no actor prefix).

        Companion to the actor-scoped :meth:`delete`; used by the lab-asset
        CRUD layer to remove a supporting file addressed at the canonical
        grammar prefix recorded on the AssetIndex row. Idempotent: deleting a
        missing key is a no-op on S3.

        Args:
            key: Bucket-absolute object key.

        Raises:
            PathTraversalError: If the key fails validation.
            ClientError: On S3 errors.
        """
        validate_path(key)
        # Same dual-convention resolution as ``read_absolute``: deleting only
        # the literal canonical key would silently orphan a draft object that
        # the actor-scoped ``write`` had placed at the translated key.
        try:
            physical = await asyncio.to_thread(self._physical_key, key)
        except PathTraversalError:
            physical = key
        await asyncio.to_thread(
            self._s3.delete_object, Bucket=self._bucket, Key=physical
        )

    async def list_absolute(self, prefix: str) -> list[str]:
        """List object keys under a BUCKET-ABSOLUTE prefix (no actor prefix).

        Returns fully-qualified keys (not relativized). Used by the
        canonical-op layer to check whether a source prefix actually has
        artifacts before publishing (empty-source guard) and, generally,
        to enumerate artifacts addressed by their recorded ``s3_prefix``.

        Args:
            prefix: Bucket-absolute prefix, ending with ``"/"``.

        Returns:
            List of bucket-absolute object keys under the prefix.

        Raises:
            PathTraversalError: If the prefix fails validation or does not
                end with ``"/"``.
            ClientError: On S3 errors.
        """
        if not prefix.endswith("/"):
            raise PathTraversalError(
                prefix, "absolute prefix must end with '/'"
            )
        validate_path(prefix.rstrip("/"))

        # A private draft may be stored EITHER at the literal canonical key
        # (``write_absolute``) or at the translated physical key
        # (``write`` -> ``<actor>/drafts/…``); both conventions exist in the
        # tree and in already-deployed data. List both and return CANONICAL
        # keys either way, so callers can relativize against the prefix they
        # passed and the publish empty-source guard sees real objects.
        # A foreign actor's canonical draft prefix raises in
        # ``_storage_prefix`` (deliberate cross-tenant guard). Listing such a
        # prefix must simply find nothing rather than error, so treat the
        # untranslatable case as "literal only".
        try:
            storage_prefix = self._storage_prefix(prefix)
        except PathTraversalError:
            storage_prefix = prefix
        search_prefixes = [prefix] if storage_prefix == prefix else [prefix, storage_prefix]

        results: list[str] = []
        for search_prefix in search_prefixes:
          continuation_token: Optional[str] = None
          while True:
            kwargs = {"Bucket": self._bucket, "Prefix": search_prefix}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = await asyncio.to_thread(
                self._s3.list_objects_v2, **kwargs
            )
            for obj in response.get("Contents", []):
                key = obj["Key"]
                if search_prefix != prefix and key.startswith(search_prefix):
                    key = prefix + key[len(search_prefix):]
                if key not in results:
                    results.append(key)
            if response.get("IsTruncated"):
                continuation_token = response.get("NextContinuationToken")
            else:
                break
        return results

    async def copy_prefix_absolute(self, src_prefix: str, dst_prefix: str) -> None:
        """Copy every object under ``src_prefix`` to ``dst_prefix``.

        Both arguments are bucket-absolute prefixes (no actor scoping).
        Used by the canonical lifecycle operations that move artifacts
        between scopes (Promote into org / group / private published
        tree, Share between scopes, Clone from published into a draft).
        Each object's key is rewritten by replacing the ``src_prefix``
        head with ``dst_prefix``; the rest of the key is preserved. The
        copy is implemented as a server-side ``CopyObject`` per object so
        no bytes flow through the agent process.

        Args:
            src_prefix: Source prefix, ending with ``"/"``. Must not
                contain path-traversal sequences.
            dst_prefix: Destination prefix, ending with ``"/"``. Must not
                contain path-traversal sequences.

        Raises:
            PathTraversalError: If either prefix fails validation or does
                not end with ``"/"``.
            ClientError: On S3 errors during list or copy.
        """
        if not src_prefix.endswith("/"):
            raise PathTraversalError(
                src_prefix, "absolute prefix must end with '/'"
            )
        if not dst_prefix.endswith("/"):
            raise PathTraversalError(
                dst_prefix, "absolute prefix must end with '/'"
            )

        # Validate (strip trailing slash so validate_path's own segment
        # checks apply); the validator rejects '..', leading '/', null
        # bytes, and dangerous unicode.
        validate_path(src_prefix.rstrip("/"))
        validate_path(dst_prefix.rstrip("/"))
        # Cross-tenant guard: a foreign actor's canonical draft prefix must
        # raise here. ``list_absolute`` deliberately tolerates it (listing a
        # prefix you can't reach should yield nothing, not error), so assert it
        # explicitly for the mutating operation.
        self._storage_prefix(src_prefix)

        # Source enumeration is delegated to ``list_absolute`` so copy and the
        # publish empty-source guard resolve a draft prefix by exactly the same
        # rule — a draft may be stored at the literal canonical key OR at the
        # translated physical key, and the two must never disagree.
        # ``list_absolute`` returns CANONICAL keys; translate each back to its
        # physical form for the CopySource.
        src_keys = await self.list_absolute(src_prefix)
        for canonical_key in src_keys:
            if not canonical_key.startswith(src_prefix):
                continue
            relative = canonical_key[len(src_prefix):]
            physical_src = self._physical_key(canonical_key)
            dst_key = self._storage_prefix(dst_prefix) + relative
            await asyncio.to_thread(
                self._s3.copy_object,
                Bucket=self._bucket,
                Key=dst_key,
                CopySource={"Bucket": self._bucket, "Key": physical_src},
            )

    async def delete_prefix_absolute(self, prefix: str) -> None:
        """Delete every object under a bucket-absolute prefix.

        Used as the compensating action when a canonical operation's
        DynamoDB transaction cancels after an S3 copy has already
        landed; cleans up the orphan target prefix so no AssetIndex row
        is left without a backing record. Does NOT auto-prepend the
        actor id.

        Implements the same retry-with-exponential-backoff strategy as
        :meth:`delete_prefix` (up to 3 attempts).

        Args:
            prefix: Bucket-absolute prefix, ending with ``"/"``.

        Raises:
            PathTraversalError: If the prefix fails validation or does
                not end with ``"/"``.
            ClientError: On S3 errors after all retries are exhausted.
        """
        if not prefix.endswith("/"):
            raise PathTraversalError(
                prefix, "absolute prefix must end with '/'"
            )
        validate_path(prefix.rstrip("/"))
        # Sweep BOTH conventions, for the same reason ``list_absolute`` searches
        # both: a private draft's objects may sit at the literal canonical key
        # (``write_absolute``) or at the translated physical key (the
        # actor-scoped ``write``), and both exist in deployed data. Deleting
        # only the translated prefix silently left canonical objects behind —
        # which stopped a failed package import from reclaiming its own
        # artifacts.
        #
        # A foreign actor's canonical draft prefix RAISES here, deliberately:
        # unlike ``list_absolute`` (where finding nothing is the safe answer),
        # a cross-tenant delete must be refused loudly, not degraded into a
        # literal-prefix attempt. `test_foreign_canonical_draft_prefix_is_rejected`
        # pins that.
        storage_prefix = self._storage_prefix(prefix)
        targets = [prefix] if storage_prefix == prefix else [prefix, storage_prefix]

        for target in targets:
            last_error: Optional[Exception] = None
            for attempt in range(_MAX_DELETE_RETRIES):
                try:
                    await self._do_delete_prefix(target)
                    last_error = None
                    break
                except ClientError as e:
                    last_error = e
                    if attempt < _MAX_DELETE_RETRIES - 1:
                        delay = _RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
                        logger.warning(
                            "delete_prefix_absolute attempt %d failed for "
                            "prefix=%s: %s. Retrying in %.1fs...",
                            attempt + 1,
                            target,
                            str(e),
                            delay,
                        )
                        await asyncio.sleep(delay)
            if last_error is not None:
                logger.error(
                    "delete_prefix_absolute failed after %d attempts for prefix=%s",
                    _MAX_DELETE_RETRIES,
                    target,
                )
                raise last_error
