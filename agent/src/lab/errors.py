"""Lab-specific exceptions."""


class AuthenticationError(Exception):
    """Raised when actor identity cannot be established from the request context."""

    pass


class AuthorityDenied(Exception):
    """Raised when an Actor attempts to mutate a Published_Record without
    being listed in its ``publishPolicy.ownerIds`` or
    ``publishPolicy.contributorIds`` (Requirement 3.2, 3.3, 3.4).

    Authority is independent of visibility: an Actor may be able to read a
    record (because its ``scope`` is ``org`` and the Actor is in the org)
    but still be denied a mutation because ``publishPolicy`` does not list
    them. Maps to HTTP 403 at the handler boundary.

    Attributes:
        published_id: The lineage id of the target row, if known.
        version: The version of the target row, if known.
    """

    def __init__(
        self,
        published_id: str | None = None,
        version: int | None = None,
        message: str | None = None,
    ) -> None:
        self.published_id = published_id
        self.version = version
        super().__init__(
            message
            or (
                f"actor not authorized to mutate "
                f"{published_id}@v{version}"
                if published_id is not None and version is not None
                else "actor not authorized for this mutation"
            )
        )


class NotFoundOrForbidden(Exception):
    """Raised when a target record either does not exist or is not visible
    to the requesting Actor (Requirement 4.5, 7.4).

    Combined into a single error so the platform never discloses existence
    of records the Actor cannot read. Maps to HTTP 404 at the handler
    boundary.
    """

    def __init__(self, message: str = "not found or forbidden") -> None:
        super().__init__(message)


class ConflictError(Exception):
    """Raised when a DynamoDB ``TransactWriteItems`` cancels because a
    condition expression failed — typically a concurrent Promote on the
    same lineage (Requirement 2.9, 2.10) or a stale current pointer.

    Maps to HTTP 409 at the handler boundary; callers retry with a
    re-read.

    Attributes:
        cancellation_reasons: Optional list of per-item cancellation
            codes returned by DynamoDB, useful for diagnosing which row
            in a multi-row transaction caused the cancel.
    """

    def __init__(
        self,
        message: str = "transaction cancelled",
        cancellation_reasons: list[dict] | None = None,
    ) -> None:
        self.cancellation_reasons = cancellation_reasons or []
        super().__init__(message)


class CompensationFailedError(Exception):
    """Raised when a compensating action (e.g. ``delete_prefix(target)``
    after a Promote transaction cancels) itself fails, leaving an orphan
    S3 prefix that no AssetIndex row references.

    The orphan prefix is logged so a janitor can reconcile it later
    (out of scope for this sprint, per the design's "Failure modes"
    section). The original error that triggered the compensation is
    preserved on ``original_error`` so the caller can decide whether to
    retry or surface a service-unavailable response.

    Attributes:
        orphan_prefix: The S3 prefix that the compensating delete failed
            to remove and that now exists with no AssetIndex backing.
        original_error: The error that triggered the compensation
            attempt (typically a transaction cancel or a service-level
            failure).
        compensation_error: The error raised by the compensating action
            itself.
    """

    def __init__(
        self,
        *,
        orphan_prefix: str,
        original_error: BaseException,
        compensation_error: BaseException,
    ) -> None:
        self.orphan_prefix = orphan_prefix
        self.original_error = original_error
        self.compensation_error = compensation_error
        super().__init__(
            f"compensating action failed for orphan prefix {orphan_prefix!r}: "
            f"original={original_error!r}, compensation={compensation_error!r}"
        )


class ServiceUnavailableError(Exception):
    """Raised when a required backend service (e.g. DynamoDB) is unreachable.

    The Lab_Service never falls back to alternative data sources when the
    Asset_Index is unavailable (Requirement 9.8). This error signals that
    the caller should retry or surface a service-unavailable response.
    """

    pass


class GatewayUnavailableError(Exception):
    """Raised when the AgentCore Gateway is unreachable or times out.

    Applies when tool discovery fails and no prior cached projection is
    available to serve as a stale fallback (Requirement 4.2).
    """

    pass


class PrefixMismatchError(Exception):
    """Raised when a record's cached `s3Prefix` does not match the prefix
    derived from its scope/owner/category/id/version per the canonical
    grammar (Requirement 1.6).

    This is a programming error: every platform write must derive and
    validate the prefix before issuing the boto3 call. Reaching this state
    at runtime indicates a bug in the CRUD layer or migration code.
    """

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"S3 prefix mismatch: expected {expected!r}, got {actual!r}"
        )


class PrivateScopePinError(Exception):
    """Raised when a project pin targets a Published_Record with `scope == "private"`.

    Project pins must reference assets that are visible to the
    `Project_Runtime_Role`, which has read access only to `groups/<g>/published/*`
    and `org/<orgId>/published/*` (Requirement 10.5). Pinning a private-scoped
    record would create a reference the project runtime can never resolve,
    so the Platform rejects the pin (Requirement 12.1, 12.2) and additionally
    refuses to load any version whose `scope` is `private` at resolve time
    (Requirement 12.4).

    Carries `asset_id` and `version` as instance attributes so callers can
    surface the offending target without re-parsing the error message.
    """

    def __init__(self, asset_id: str, version: int) -> None:
        self.asset_id = asset_id
        self.version = version
        super().__init__(
            f"Cannot pin asset {asset_id}@v{version}: scope='private' is "
            f"not permitted for project pins (use 'group' or 'org')"
        )


class ScopeNarrowingError(Exception):
    """Raised when a Share operation would not strictly widen scope.

    Under the strict ordering ``private < group < org`` (Requirement 5.5,
    Property 5), Share rejects any transition where the new scope is not
    strictly greater than the prior scope. This includes both narrowing
    (e.g., ``org`` to ``group``) and no-op shares (e.g., ``group`` to
    ``group``); in both cases the operation is rejected before any S3 or
    AssetIndex mutation. Maps to HTTP 400 at the handler boundary.

    Attributes:
        from_scope: The current scope of the source Published_Record.
        to_scope: The requested new scope.
    """

    def __init__(self, from_scope: str, to_scope: str) -> None:
        self.from_scope = from_scope
        self.to_scope = to_scope
        super().__init__(
            f"Cannot share from scope='{from_scope}' to scope='{to_scope}': "
            f"scope can only widen (private < group < org)"
        )


class DeprecatedReferenceError(Exception):
    """Raised when a workflow Promote walks an Asset_Ref pointing at a
    Published_Record whose ``status == "deprecated"`` (Requirement 11.3).

    Resolution at Promote time pins to the exact ``(publishedId, version)``
    named in the ref (Requirement 11.4); if that pinned version is
    deprecated, the workflow Promote is rejected before any S3 copy or
    AssetIndex transaction. Maps to HTTP 422 at the handler boundary
    (per the design's "Error Handling" table).

    Attributes:
        category: The Asset_Ref's category segment
            (``"skill"`` | ``"specialty-agent"`` | ``"tool-collection"``
            | ``"workflow"``).
        published_id: The lineage id named in the Asset_Ref.
        version: The pinned version named in the Asset_Ref.
        step_location: Optional human-readable location within the
            workflow draft (e.g. ``"step 's1' agent"``) that helps the
            author identify which ref triggered the rejection.
    """

    def __init__(
        self,
        *,
        category: str,
        published_id: str,
        version: int,
        step_location: str | None = None,
    ) -> None:
        self.category = category
        self.published_id = published_id
        self.version = version
        self.step_location = step_location
        ref_str = f"{category}/{published_id}@v{version}"
        if step_location:
            message = (
                f"{step_location}: Asset_Ref '{ref_str}' references a "
                f"deprecated published version"
            )
        else:
            message = (
                f"Asset_Ref '{ref_str}' references a deprecated published "
                f"version"
            )
        super().__init__(message)


class StillReferencedError(Exception):
    """Raised when deleting an asset something else still points at.

    Used by the flat tool-collection delete, which has no purge pre-flight to
    warn through: the working copy IS the asset, so the delete is already
    irreversible. Deleting a collection an agent still selects used to degrade
    that agent silently — the invoke succeeded, in persona, with its tools
    quietly missing — so this refuses instead and names what would break.

    Not fatal by policy: ``force=True`` on the delete proceeds anyway, which is
    what a caller cleaning up after the referencing agents are gone needs.

    ``partial`` matters as much as ``hits``. The scan sees only what the calling
    actor can see, so its absence of hits is never proof of no references; a
    caller rendering this must say "still referenced by" and not imply the list
    is exhaustive.

    Attributes:
        category: Category of the asset being deleted (``"tool-collection"``).
        asset_id: Its id.
        hits: The references found, as
            :class:`~lab.operation_results.AgentReferenceHit`.
        partial: Sources that could not be scanned.
    """

    def __init__(
        self,
        *,
        category: str,
        asset_id: str,
        hits: tuple,
        partial: tuple = (),
    ) -> None:
        self.category = category
        self.asset_id = asset_id
        self.hits = hits
        self.partial = partial
        names = ", ".join(
            f"{h.name!r} ({h.status})" for h in hits
        ) or "another asset"
        super().__init__(
            f"{category}#{asset_id} is still referenced by {len(hits)} "
            f"asset(s): {names}. Remove the reference first, or delete with "
            "force=true to proceed anyway."
        )
