"""SpecialtyCRUD — CRUD operations for Specialty_Agent drafts in the MediaWorks Lab.

Specialty agents are persona prompts layered on top of the platform's Task Agent
runtime. They declare which Skill drafts they are compatible with.

S3 key layout (actor-scoped writes prepend ``{actor_id}/``)::

    {actor_id}/drafts/specialty-agents/{assetId}/persona.md

The canonical ``s3Prefix`` recorded on the AssetIndex draft row uses the
unified-platform grammar ``users/<actor>/drafts/specialty-agents/<id>/``
so :func:`validate_record_prefix` (run inside :meth:`AssetIndex.put`)
accepts it and the canonical-op layer can address it via
``copy_prefix_absolute``.

Delete is a hard delete: a single-op ``transact_write`` removes the index
row, then ``delete_prefix`` reclaims the S3 artifacts. The legacy
``status="archived"`` soft-delete is gone — the new schema exposes only
``draft | published | deprecated``.

Cross-actor access returns ``None`` (no existence disclosure): a draft
owned by a different user lives under a different ``PK`` so the get
returns nothing.

The list operation returns user-authored drafts plus a read-only reference
to the platform's Task Agent runtime marked as a core agent (Requirement 3.7);
the core-agent reference is appended at the service layer because it does not
correspond to an AssetIndex row.

Requirements: 3.1, 3.2, 3.3, 3.6, 3.7
Properties: 5, 6, 9
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlparse

from botocore.exceptions import ClientError

from .asset_index import AssetIndex, make_pk_user_owned, make_sk_owned
from .context import LabRequestContext
from .models import (
    IntegrationConfig,
    SpecialtyAgent,
    ToolCollectionSnapshot,
    _integration_config_to_item,
)
from .path_validator import derive_record_prefix
from .s3_user_files_tool import S3UserFilesTool

if TYPE_CHECKING:
    from .models import Scope
    from .operation_results import (
        Cloned,
        Deprecated,
        ExternalAccessSet,
        Promoted,
        RolledBack,
        Shared,
    )

logger = logging.getLogger(__name__)


def _pick_published_row(rows: list, version: Optional[int]) -> Optional[Any]:
    """Pick one published row from a lineage: exact ``version``, else
    ``current``, else the highest non-deprecated version.

    Intentionally byte-for-byte equivalent to ``task_agent._pick_lineage_row``
    (the selection the external invoke path authorizes). The two are duplicated
    rather than shared because ``task_agent`` is a deployed runtime entrypoint
    and importing it from the lab package would drag the whole Strands/MCP
    import graph into every Lab CRUD call. If either changes, change both.

    ``AssetIndex.query_lineage`` returns ascending version order, so a reversed
    scan finds the highest first.
    """
    if version is not None:
        for row in rows:
            if getattr(row, "version", None) == version:
                return row
    for row in reversed(rows):
        if getattr(row, "current", False) and not getattr(row, "deprecated", False):
            return row
    for row in reversed(rows):
        if not getattr(row, "deprecated", False):
            return row
    return None


# Constants
_CATEGORY = "specialty-agent"
_SKILL_CATEGORY = "skill"
_TOOL_COLLECTION_CATEGORY = "tool-collection"
_PERSONA_FILENAME = "persona.md"
_DRAFTS_PREFIX = "drafts/specialty-agents"

# Sentinel distinguishing "argument not supplied" from an explicit ``None`` on
# update. ``model`` and ``integration_config`` both treat ``None`` as a
# meaningful stored value (unset model → runtime default; ``None`` config →
# external access not configured), so ``None`` cannot double as "leave
# unchanged". Callers omit the argument to skip the field; passing ``None``
# explicitly clears it.
_UNSET: Any = object()

@dataclass
class SpecialtyReadResult:
    """Result of reading a Specialty_Agent draft.

    Carries the extended Custom_Agent fields (``model``, ``tool_collections``,
    ``integration_config``) alongside the persona/compatible-skill bindings so
    the service/UI layer can render the full configuration.
    """

    asset_id: str
    name: str
    persona: str
    compatible_skills: list[str]
    metadata: dict
    model: Optional[str] = None
    tool_collections: list[str] = field(default_factory=list)
    # Populated only by ``read_published``: the tool refs this version was
    # published with. ``None`` on a draft (working copies resolve their
    # collections live), which is exactly the distinction a consumer needs — see
    # ``lab.models.SpecialtyAgent.tool_collection_snapshots``.
    tool_collection_snapshots: Optional[list[ToolCollectionSnapshot]] = None
    integration_config: Optional[IntegrationConfig] = None
    description: str = ""


@dataclass
class SpecialtyCreateResult:
    """Result of creating a Specialty_Agent draft.

    Carries the extended Custom_Agent fields the create call persisted so the
    service/UI layer can echo them back without a follow-up read.
    """

    asset_id: str
    s3_prefix: str
    model: Optional[str] = None
    tool_collections: list[str] = field(default_factory=list)
    integration_config: Optional[IntegrationConfig] = None


class SpecialtyCRUD:
    """CRUD operations for Specialty_Agent drafts.

    Uses S3UserFilesTool for file operations and AssetIndex for metadata.
    The AssetIndex is no longer actor-bound; this class derives the PK
    from ``ctx.actor_id`` on every call via :func:`make_pk_user_owned`.

    Args:
        s3: The S3UserFilesTool instance (already bound to actor_id).
        index: The single-table AssetIndex instance.
    """

    def __init__(self, s3: S3UserFilesTool, index: AssetIndex) -> None:
        self._s3 = s3
        self._index = index

    def _validate_model(self, model: Optional[str]) -> None:
        """Validate a Custom_Agent's ``model`` against the Model_Catalog.

        Model validation is pure catalog-membership (Property 3): the stored
        ``model`` must name a Bedrock inference profile id that
        :func:`model_catalog.discover_models` currently reports. An unset
        (``None``) model is a no-op — the platform default inference profile
        applies at invoke time (Requirement 2.5), so nothing to validate.

        The catalog is treated as an external dependency that can fail closed
        (Requirement 2.4). :func:`discover_models` already degrades a Bedrock
        outage to an empty list, and a defensive ``try`` also covers the case
        where it raises outright. Either way an *unreachable* catalog is
        surfaced as a distinct "temporarily unavailable" error so the UI can
        tell it apart from a genuinely unknown identifier (Requirement 2.3).

        This runs before any S3 or AssetIndex write (same validate-then-write
        ordering as :meth:`_validate_compatible_skills`), so a rejected model
        leaves both stores untouched.

        Args:
            model: The Bedrock inference profile id to validate, or ``None``.

        Raises:
            ValueError: If ``model`` is not a member of the catalog (message
                names the offending id), or if the catalog is unreachable
                (distinct "temporarily unavailable" message).
        """
        # Requirement 2.5: an unset model is allowed — the runtime falls back
        # to the platform default, so there is nothing to validate.
        if model is None:
            return

        # Lazy import mirrors the deferred ``_canonical_ops`` imports used
        # elsewhere in this module and keeps the top-level ``model_catalog``
        # dependency off the import path until validation actually runs.
        from model.catalog import discover_models

        # Requirement 2.4: the catalog is an external dependency. ``discover_models``
        # degrades an outage to ``[]`` internally, but guard against it raising
        # too; either way we cannot confirm membership, so fail closed with a
        # distinct signal the UI maps to "validation temporarily unavailable".
        try:
            catalog = discover_models()
        except Exception as err:  # noqa: BLE001 — any failure means "unreachable"
            raise ValueError(
                "Model validation is temporarily unavailable; "
                "the model catalog could not be reached."
            ) from err

        if not catalog:
            raise ValueError(
                "Model validation is temporarily unavailable; "
                "the model catalog could not be reached."
            )

        known_ids = {entry["id"] for entry in catalog if "id" in entry}
        if model not in known_ids:
            # Requirement 2.3: name the invalid identifier so the UI can echo it.
            raise ValueError(
                f"Model '{model}' is not a known inference profile."
            )

    def _validate_integration_config(
        self, cfg: Optional[IntegrationConfig]
    ) -> Optional[IntegrationConfig]:
        """Validate/normalize a Custom_Agent's external-invoke ``integration_config``.

        Enforces Property 5 (integration config validation): a config with
        ``enabled == True`` passes if and only if ``issuer`` is a valid HTTPS
        URL and ``audience`` is a non-empty string; disabling produces a
        stored config that is disabled/cleared.

        Three cases (mirrors the validate-then-write ordering and the
        ``None``-is-a-no-op contract of :meth:`_validate_model`):

        * ``cfg is None`` — nothing to configure; returns ``None`` unchanged.
        * ``cfg.enabled is True`` — external access is being turned on, so the
          OIDC settings must be usable (Requirement 4.2/4.3). ``issuer`` must
          parse as an ``https://`` URL with a host, and ``audience`` must be a
          non-empty (non-whitespace) string; otherwise a ``ValueError`` naming
          the offending field is raised and the config is returned unchanged.
        * ``cfg.enabled is False`` — the owner is disabling external access, so
          there is nothing to validate. Per Requirement 4.5 disabling *clears*
          the stored config, so a fresh disabled :class:`IntegrationConfig`
          (issuer/audience/allowed_client_ids reset to their defaults) is
          returned regardless of any stale fields the caller left populated.

        This runs before any S3 or AssetIndex write, so a rejected config
        leaves both stores untouched.

        Args:
            cfg: The integration config to validate, or ``None``.

        Returns:
            ``None`` when ``cfg`` is ``None``; the validated ``cfg`` when
            enabled; a cleared/disabled :class:`IntegrationConfig` when
            disabled.

        Raises:
            ValueError: When ``enabled`` is set but ``issuer`` is not a valid
                HTTPS URL or ``audience`` is empty (message names the field).
        """
        # None means external access is simply not configured — no-op, same
        # contract as an unset model in :meth:`_validate_model`.
        if cfg is None:
            return None

        # Requirement 4.5: disabling clears the stored config. Return a fresh
        # disabled config so any stale issuer/audience/client-ids cannot linger
        # and no issuer/audience validation applies on the disable path.
        if not cfg.enabled:
            return IntegrationConfig(enabled=False)

        # Requirement 4.3: an enabled config must name a valid HTTPS issuer.
        # Guard against a non-string / empty issuer before parsing so urlparse
        # never sees a None.
        issuer = cfg.issuer
        parsed = urlparse(issuer) if isinstance(issuer, str) else None
        if (
            parsed is None
            or parsed.scheme != "https"
            or not parsed.netloc
        ):
            raise ValueError(
                "Integration issuer must be a valid HTTPS URL."
            )

        # Requirement 4.3: the expected audience must be a non-empty string.
        audience = cfg.audience
        if not isinstance(audience, str) or not audience.strip():
            raise ValueError(
                "Integration audience must be a non-empty string."
            )

        return cfg

    async def _validate_compatible_skills(
        self, ctx: LabRequestContext, skill_ids: list[str]
    ) -> None:
        """Validate each compatible skill resolves to a skill the actor owns.

        v2 ownership semantics: a skill the actor owns can be either a
        working copy (``USER#<u>#OWNED``) or one of their published
        versions (``USER#<u>#VERSIONS``). Both are valid bind targets;
        post-publish, the working copy is gone but the user still
        "owns" the skill via its version lineage. Validating only OWNED
        broke skill binding for any specialty whose source skills had
        already been published.

        For VERSIONS the lineage GSI is queried (one call per skill_id)
        so deprecated and rolled-back lineages still resolve. We accept
        any non-deprecated version of the lineage as a valid reference;
        deprecated-only lineages are rejected with a clear message.

        Args:
            ctx: The request context (provides actor_id used to scope draft lookups).
            skill_ids: List of skill asset IDs to validate.

        Raises:
            ValueError: If any skill does not resolve under the same actor.
        """
        errors: list[str] = []
        owned_pk = make_pk_user_owned(ctx.actor_id)

        for skill_id in skill_ids:
            # Fast path: the actor still has a working copy of this skill.
            owned = await self._index.get(
                owned_pk, make_sk_owned(_SKILL_CATEGORY, skill_id)
            )
            if owned is not None:
                continue

            # Otherwise: the skill must live in the actor's published
            # versions partition. Hit the byPublishedId GSI to enumerate
            # every version of this asset_id, then accept the lineage if
            # any non-deprecated version exists and the actor owns it.
            lineage = await self._index.query_lineage(skill_id)
            if not lineage:
                errors.append(
                    f"Compatible skill '{skill_id}' does not exist."
                )
                continue
            actor_versions = [
                r for r in lineage
                if not r.deprecated
                and (r.s3_prefix or "").startswith(f"users/{ctx.actor_id}/")
            ]
            if not actor_versions:
                errors.append(
                    f"Compatible skill '{skill_id}' is not owned by you "
                    f"(or every version is deprecated)."
                )

        if errors:
            raise ValueError("\n".join(errors))

    async def _validate_tool_collections(
        self, ctx: LabRequestContext, ids: list[str]
    ) -> None:
        """Validate each tool_collection resolves to one the actor owns.

        This is the Tool_Collection analogue of
        :meth:`_validate_compatible_skills` and enforces Property 4
        (tool-collection validation is ownership-and-uniqueness): the list
        must contain no duplicates, and every id must resolve to a
        Tool_Collection the actor owns.

        v2 ownership semantics (identical to compatible-skill resolution):
        an owned Tool_Collection can be either a working copy
        (``USER#<u>#OWNED`` keyed by ``tool-collection#<id>``) or one of the
        actor's published versions (``USER#<u>#VERSIONS`` lineage). Both are
        valid references; post-publish the working copy is gone but the actor
        still "owns" the collection via its version lineage. For VERSIONS the
        lineage GSI is queried (one call per id) so deprecated/rolled-back
        lineages still resolve — we accept any non-deprecated version the
        actor owns and reject deprecated-only lineages with a clear message.

        All validation runs before any S3/AssetIndex write (same
        validate-then-write ordering as :meth:`_validate_compatible_skills`),
        so a rejected reference list leaves both stores untouched.

        Args:
            ctx: The request context (provides actor_id used to scope lookups).
            ids: List of Tool_Collection asset IDs to validate.

        Raises:
            ValueError: If the list contains duplicate ids (message names the
                offending id), or if any id does not resolve to a
                Tool_Collection owned by the actor (missing or not-owned;
                message names the offending id).
        """
        errors: list[str] = []
        owned_pk = make_pk_user_owned(ctx.actor_id)

        # Reject duplicate references up front (Requirement 3.3). Track the
        # first-seen ids so the message can name the offending duplicate.
        seen: set[str] = set()
        for tc_id in ids:
            if tc_id in seen:
                errors.append(
                    f"Tool collection '{tc_id}' is listed more than once."
                )
            seen.add(tc_id)

        for tc_id in ids:
            # Fast path: the actor still has a working copy of this collection.
            owned = await self._index.get(
                owned_pk, make_sk_owned(_TOOL_COLLECTION_CATEGORY, tc_id)
            )
            if owned is not None:
                continue

            # Otherwise: the collection must live in the actor's published
            # versions partition. Hit the byPublishedId GSI to enumerate every
            # version of this asset_id, then accept the lineage if any
            # non-deprecated version exists and the actor owns it.
            lineage = await self._index.query_lineage(tc_id)
            if not lineage:
                errors.append(
                    f"Tool collection '{tc_id}' does not exist."
                )
                continue
            actor_versions = [
                r for r in lineage
                if not r.deprecated
                and (r.s3_prefix or "").startswith(f"users/{ctx.actor_id}/")
            ]
            if not actor_versions:
                errors.append(
                    f"Tool collection '{tc_id}' is not owned by you "
                    f"(or every version is deprecated)."
                )

        if errors:
            raise ValueError("\n".join(errors))

    async def _validate_external_tool_collections(
        self,
        ctx: LabRequestContext,
        cfg: Optional[IntegrationConfig],
        selected_tool_collections: list[str],
    ) -> None:
        """Validate the owner's external opt-in set (Requirement 11.1).

        The external opt-in (``IntegrationConfig.external_tool_collections``)
        is the subset of the agent's selected ``tool_collections`` permitted to
        bind on the *external* invocation path. It is only meaningful when the
        config is enabled; a ``None`` or disabled config carries no external
        opt-in and needs no validation (disabling clears it upstream).

        Two invariants are enforced (the ``external_opt_in ⊆ selected_allowlist``
        half of Property 13, plus ownership as defense-in-depth):

        * **Subset of the selected allowlist.** Every opt-in id MUST appear in
          the agent's selected ``tool_collections`` (the Req 7 allowlist).
          Opting a collection in for external use that the agent does not even
          select would let a caller reach a tool outside the allowlist.
        * **Owner-owned + unique.** The opt-in ids must contain no duplicates
          and each must resolve to an actor-owned Tool_Collection — mirrored
          from :meth:`_validate_tool_collections`. (Subset-of-selected already
          implies ownership when the selected list is validated, but we assert
          it explicitly so the opt-in is sound even if invoked directly.)

        Runs before any S3/AssetIndex write (validate-then-write ordering), so
        a rejected opt-in leaves both stores untouched.

        Args:
            ctx: The request context (provides actor_id used to scope lookups).
            cfg: The (already issuer/audience-validated) integration config, or
                ``None``.
            selected_tool_collections: The agent's effective selected
                ``tool_collections`` after this create/update.

        Raises:
            ValueError: If the opt-in contains duplicates, an id not in the
                selected allowlist, or an id not owned by the actor (message
                names the offending id).
        """
        if cfg is None or not cfg.enabled:
            return
        ext = list(getattr(cfg, "external_tool_collections", []) or [])
        if not ext:
            return

        errors: list[str] = []
        selected = set(selected_tool_collections or [])
        seen: set[str] = set()
        for tc_id in ext:
            if tc_id in seen:
                errors.append(
                    f"External tool collection '{tc_id}' is listed more than once."
                )
            seen.add(tc_id)
            if tc_id not in selected:
                errors.append(
                    f"External tool collection '{tc_id}' is not in the agent's "
                    f"selected tool_collections allowlist."
                )
        if errors:
            raise ValueError("\n".join(errors))

        # Defense-in-depth: assert actor ownership of every opt-in id, mirroring
        # the tool-collection ownership validation.
        await self._validate_tool_collections(ctx, ext)

    async def create(
        self,
        ctx: LabRequestContext,
        name: str,
        persona: str,
        compatible_skills: Optional[list[str]] = None,
        model: Optional[str] = None,
        tool_collections: Optional[list[str]] = None,
        integration_config: Optional[IntegrationConfig] = None,
        description: str = "",
    ) -> SpecialtyCreateResult:
        """Create a new Specialty_Agent draft.

        Validates that ``name`` and ``persona`` are non-empty, runs all of the
        Custom_Agent validators (compatible skills, ``model``,
        ``tool_collections``, ``integration_config``) BEFORE any write, writes
        ``persona.md`` to S3, and records the draft in the AssetIndex with the
        full extended configuration.

        Args:
            ctx: The request context (provides actor_id).
            name: User-supplied specialty agent name (must be non-empty).
            persona: The persona prompt content (must be non-empty).
            compatible_skills: Optional list of Skill draft asset IDs that
                this specialty is compatible with. Each must be owned by
                the same actor.
            model: Optional Bedrock inference profile id; when provided it
                must be a member of the Model_Catalog.
            tool_collections: Optional list of Tool_Collection ids; each must
                resolve to a collection owned by the actor, with no duplicates.
            integration_config: Optional external-invoke OIDC settings; when
                enabled the issuer must be a valid HTTPS URL and the audience
                non-empty. Disabling clears the stored config.

        Returns:
            SpecialtyCreateResult with the assigned asset_id, canonical
            s3_prefix, and the persisted extended configuration.

        Raises:
            ValueError: If the name or persona is empty, or if any of the
                compatible skills, model, tool_collections, or
                integration_config fail validation.
        """
        # Requirement 1.8: require a non-empty (non-whitespace) name.
        if not name or not name.strip():
            raise ValueError("Name must not be empty")

        # Requirement 3.1: require non-empty persona prompt
        if not persona or not persona.strip():
            raise ValueError("Persona prompt must not be empty")

        # Requirements 2.1/3.1/4.1: run every validator BEFORE any S3 or
        # AssetIndex write (validate-then-write ordering) so a bad reference
        # leaves both stores untouched.
        skills_list = list(compatible_skills) if compatible_skills else []
        if skills_list:
            await self._validate_compatible_skills(ctx, skills_list)

        tool_collections_list = (
            list(tool_collections) if tool_collections else []
        )
        if tool_collections_list:
            await self._validate_tool_collections(ctx, tool_collections_list)

        self._validate_model(model)

        # ``_validate_integration_config`` returns the normalized config
        # (disabling clears it), so store what it hands back rather than the
        # raw input (Requirement 4.5).
        stored_integration = self._validate_integration_config(
            integration_config
        )

        # Requirement 11.1: the external opt-in must be a subset of the agent's
        # selected tool_collections and owned by the actor. Validated against
        # the normalized config so a disabled config (opt-in cleared) is a
        # no-op.
        await self._validate_external_tool_collections(
            ctx, stored_integration, tool_collections_list
        )

        # Assign unique asset_id
        asset_id = str(uuid.uuid4())
        now = int(time.time() * 1000)

        # Canonical S3 prefix as recorded in the AssetIndex row. The
        # actor-scoped S3UserFilesTool used below auto-prepends the actor
        # id when resolving relative keys, so writes go to
        # ``<actor_id>/drafts/specialty-agents/<asset_id>/...``; the
        # AssetIndex ``s3_prefix`` field carries the canonical
        # bucket-absolute form produced by ``derive_record_prefix``.
        s3_prefix = derive_record_prefix(
            scope="private",
            owner_user_id=ctx.actor_id,
            owner_group_id=None,
            owner_org_id=None,
            category=_CATEGORY,
            asset_id=asset_id,
            version=None,
            status="draft",
        )
        # Write persona.md at the canonical grammar prefix (bucket-absolute),
        # so the physical location matches the recorded ``s3_prefix`` that
        # ``promote_asset`` copies from and the runtime consumers read (BUG-2).
        # Actor isolation is preserved by the ``users/<actor>/`` grammar itself.
        persona_key = f"{s3_prefix}{_PERSONA_FILENAME}"

        # Write persona.md to S3 first so a failed AssetIndex.put leaves a
        # stray file but no orphan index row (matches the create-then-record
        # ordering used elsewhere in the lab CRUD layer).
        await self._s3.write_absolute(persona_key, persona.encode("utf-8"))

        # Record in AssetIndex as a typed SpecialtyAgent draft, carrying the
        # extended Custom_Agent configuration.
        agent = SpecialtyAgent(
            asset_id=asset_id,
            category=_CATEGORY,
            name=name,
            description=description,
            status="draft",
            scope="private",
            s3_prefix=s3_prefix,
            created_at=now,
            updated_at=now,
            compatible_skills=skills_list,
            model=model,
            tool_collections=tool_collections_list,
            integration_config=stored_integration,
        )
        await self._index.put(agent, owner_user_id=ctx.actor_id)

        logger.info(
            "Created specialty agent draft: asset_id=%s, name=%s, actor=%s",
            asset_id,
            name,
            ctx.actor_id,
        )

        return SpecialtyCreateResult(
            asset_id=asset_id,
            s3_prefix=s3_prefix,
            model=model,
            tool_collections=tool_collections_list,
            integration_config=stored_integration,
        )

    async def read(
        self, ctx: LabRequestContext, asset_id: str
    ) -> Optional[SpecialtyReadResult]:
        """Read a Specialty_Agent draft by asset_id.

        Returns the persona content, compatible skills list, and draft metadata.
        Returns ``None`` if the draft does not exist or belongs to a different
        actor (no existence disclosure per Requirement 3.6) — cross-actor
        access surfaces as not-found because the get is keyed on the
        requesting actor's drafts partition.
        """
        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)
        record = await self._index.get(pk, sk)
        if record is None or record.status != "draft":
            return None

        # Read persona.md from the record's canonical prefix (bucket-absolute),
        # matching where create/update now write it (BUG-2).
        persona_key = f"{record.s3_prefix}{_PERSONA_FILENAME}"
        try:
            persona_bytes = await self._s3.read_absolute(persona_key)
            persona = persona_bytes.decode("utf-8")
        except FileNotFoundError:
            # persona.md missing — shouldn't happen for a valid draft, but
            # surface an empty body rather than crashing the read.
            persona = ""

        compatible_skills = list(getattr(record, "compatible_skills", []))

        return SpecialtyReadResult(
            asset_id=record.asset_id,
            name=record.name,
            persona=persona,
            compatible_skills=compatible_skills,
            metadata={
                "category": record.category,
                "status": record.status,
                "createdAt": record.created_at,
                "updatedAt": record.updated_at,
                "s3Prefix": record.s3_prefix,
            },
            model=getattr(record, "model", None),
            tool_collections=list(getattr(record, "tool_collections", [])),
            integration_config=getattr(record, "integration_config", None),
            description=getattr(record, "description", ""),
        )

    async def read_published(
        self,
        ctx: LabRequestContext,
        asset_id: str,
        version: Optional[int] = None,
    ) -> Optional[SpecialtyReadResult]:
        """Read a *published* Specialty_Agent version by published id.

        Deliberately a sibling of :meth:`read` rather than a relaxation of it:
        ``read`` is contractually draft-only (it keys the actor's OWNED
        partition and rejects any non-``draft`` status), and several callers
        depend on that. Published versions live in a different partition and
        are immutable, so they get their own accessor.

        Version selection matches the external invoke path exactly
        (``task_agent._pick_lineage_row``): an explicit ``version`` wins, else
        the row flagged ``current``, else the highest non-deprecated version.
        Keeping the two in lockstep is the point of this method — a test
        console that resolved a *different* version than a real invocation
        would be worse than no console at all.

        Owner scoping (mirrors Requirement 6.2 as enforced on the external
        path): the resolved row's recorded prefix must belong to the requesting
        actor when it is user-scoped. Group/org-scoped rows are still
        owner-published assets, so they are only checked when the prefix
        carries the ``users/`` grammar. A cross-actor or missing lineage
        surfaces as ``None`` — no existence disclosure, same as :meth:`read`.

        Returns:
            A :class:`SpecialtyReadResult` whose ``metadata`` additionally
            carries ``version`` and ``current``, or ``None`` when no published
            version is resolvable.
        """
        if not asset_id:
            return None

        try:
            lineage = await self._index.query_lineage(asset_id)
        except Exception:  # noqa: BLE001 — a lookup failure is a read miss here
            logger.warning(
                "read_published: lineage query failed for asset_id=%s",
                asset_id,
                exc_info=True,
            )
            return None

        record = _pick_published_row(lineage, version)
        if record is None or getattr(record, "category", "") != _CATEGORY:
            return None

        prefix = getattr(record, "s3_prefix", "") or ""
        if prefix.startswith("users/") and not prefix.startswith(
            f"users/{ctx.actor_id}/"
        ):
            logger.warning(
                "read_published: owner mismatch for asset_id=%s "
                "(actor=%s, row prefix=%s)",
                asset_id,
                ctx.actor_id,
                prefix,
            )
            return None

        # Published rows are immutable, so a missing persona.md means the
        # publish was corrupt rather than mid-edit. Surface an empty body (as
        # ``read`` does) instead of crashing the caller.
        persona = ""
        if prefix:
            try:
                persona_bytes = await self._s3.read_absolute(
                    f"{prefix}{_PERSONA_FILENAME}"
                )
                persona = persona_bytes.decode("utf-8")
            except FileNotFoundError:
                logger.warning(
                    "read_published: persona.md missing under %s", prefix
                )

        return SpecialtyReadResult(
            asset_id=record.asset_id,
            name=record.name,
            persona=persona,
            compatible_skills=list(getattr(record, "compatible_skills", [])),
            metadata={
                "category": record.category,
                "status": record.status,
                "createdAt": record.created_at,
                "updatedAt": record.updated_at,
                "s3Prefix": record.s3_prefix,
                "version": getattr(record, "version", None),
                "current": bool(getattr(record, "current", False)),
            },
            model=getattr(record, "model", None),
            tool_collections=list(getattr(record, "tool_collections", [])),
            # Carried so the test console resolves the tools this version was
            # published with, matching what a real invoke binds. Reading the
            # owner's live collection rows instead is how the console could
            # confidently report a tool set no invocation would actually use.
            tool_collection_snapshots=getattr(
                record, "tool_collection_snapshots", None
            ),
            integration_config=getattr(record, "integration_config", None),
            description=getattr(record, "description", ""),
        )

    async def update(
        self,
        ctx: LabRequestContext,
        asset_id: str,
        persona: Optional[str] = None,
        compatible_skills: Optional[list[str]] = None,
        model: Any = _UNSET,
        tool_collections: Optional[list[str]] = None,
        integration_config: Any = _UNSET,
        description: Any = _UNSET,
    ) -> Optional[dict]:
        """Update an existing Specialty_Agent draft.

        Updates any of the persona prompt, compatible skills list, ``model``,
        ``tool_collections``, and ``integration_config``. Runs every relevant
        validator BEFORE any S3 or AssetIndex write (Requirements 2.1/3.1/4.1);
        on validation failure no S3 write and no AssetIndex mutation occurs.
        Supplied fields are persisted in a single SET expression so the update
        is one DynamoDB round-trip.

        Argument-presence semantics:

        * ``compatible_skills`` / ``tool_collections`` — ``None`` leaves the
          field unchanged; an empty list clears the binding.
        * ``model`` / ``integration_config`` — omitted (``_UNSET``) leaves the
          field unchanged; ``None`` cannot serve as "unchanged" because it is a
          meaningful stored value, so pass ``None`` explicitly to clear.

        Args:
            ctx: The request context (provides actor_id).
            asset_id: The unique asset identifier.
            persona: Optional new persona prompt (must be non-empty if
                provided).
            compatible_skills: Optional new list of compatible Skill draft
                asset IDs.
            model: New Bedrock inference profile id; must be a Model_Catalog
                member when non-``None``. Omit to leave unchanged.
            tool_collections: Optional new list of Tool_Collection ids; each
                must resolve to a collection owned by the actor, no duplicates.
            integration_config: New external-invoke OIDC settings; validated
                and normalized (disabling clears it). Omit to leave unchanged.

        Returns:
            Dict with ``updatedAt`` if successful, ``None`` if not found.

        Raises:
            ValueError: If persona is empty, or if any of compatible skills,
                model, tool_collections, or integration_config are invalid.
        """
        # Verify ownership: cross-actor access surfaces as not-found.
        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)
        record = await self._index.get(pk, sk)
        if record is None or record.status != "draft":
            return None

        # Validate persona if provided.
        if persona is not None and (not persona or not persona.strip()):
            raise ValueError("Persona prompt must not be empty")

        # Requirements 2.1/3.1/4.1: run every validator BEFORE any write so a
        # bad reference leaves both stores untouched. An empty list is a valid
        # update (clears the binding) and skips the ownership validation.
        if compatible_skills:
            await self._validate_compatible_skills(ctx, compatible_skills)

        if tool_collections:
            await self._validate_tool_collections(ctx, tool_collections)

        model_supplied = model is not _UNSET
        if model_supplied:
            self._validate_model(model)

        # ``_validate_integration_config`` returns the normalized config
        # (disabling clears it) — persist what it returns, not the raw input.
        integration_supplied = integration_config is not _UNSET
        stored_integration: Optional[IntegrationConfig] = None
        if integration_supplied:
            stored_integration = self._validate_integration_config(
                integration_config
            )

        # Requirement 11.1: keep the external opt-in ⊆ the agent's selected
        # tool_collections and owned by the actor. Validate against the
        # *effective* config and *effective* selection after this update so the
        # invariant still holds when either the config or the tool_collections
        # change (e.g. shrinking the selection must not orphan an opt-in). When
        # neither is supplied there is nothing new to check.
        if integration_supplied or tool_collections is not None:
            effective_cfg = (
                stored_integration
                if integration_supplied
                else getattr(record, "integration_config", None)
            )
            effective_tcs = (
                list(tool_collections)
                if tool_collections is not None
                else list(getattr(record, "tool_collections", []))
            )
            await self._validate_external_tool_collections(
                ctx, effective_cfg, effective_tcs
            )

        # Update persona.md in S3 if provided — at the record's canonical
        # prefix (bucket-absolute), matching create/read (BUG-2).
        if persona is not None:
            persona_key = f"{record.s3_prefix}{_PERSONA_FILENAME}"
            await self._s3.write_absolute(persona_key, persona.encode("utf-8"))

        # Build the AssetIndex update expression. ``updatedAt`` is always
        # bumped; each extended field is written only when the caller supplied
        # it. All parts go into one SET expression so the update is a single
        # round-trip. ``#model`` is aliased via ExpressionAttributeNames to
        # sidestep any reserved-word ambiguity; the other camelCase attribute
        # names written by ``models.to_index_item`` are not reserved words.
        now = int(time.time() * 1000)
        update_parts: list[str] = ["updatedAt = :updatedAt"]
        attr_values: dict[str, Any] = {":updatedAt": now}
        attr_names: dict[str, str] = {}

        if compatible_skills is not None:
            update_parts.append("compatibleSkills = :compatibleSkills")
            attr_values[":compatibleSkills"] = list(compatible_skills)

        if tool_collections is not None:
            update_parts.append("toolCollections = :toolCollections")
            attr_values[":toolCollections"] = list(tool_collections)

        if model_supplied:
            update_parts.append("#model = :model")
            attr_names["#model"] = "model"
            attr_values[":model"] = model

        if integration_supplied:
            update_parts.append("integrationConfig = :integrationConfig")
            attr_values[":integrationConfig"] = _integration_config_to_item(
                stored_integration
            )

        description_supplied = description is not _UNSET
        if description_supplied:
            # Aliased like #model to sidestep reserved-word ambiguity.
            update_parts.append("#description = :description")
            attr_names["#description"] = "description"
            attr_values[":description"] = str(description)

        try:
            await self._index.update(
                pk,
                sk,
                update_expression="SET " + ", ".join(update_parts),
                expression_attribute_names=attr_names or None,
                expression_attribute_values=attr_values,
                condition_expression="attribute_exists(PK)",
            )
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                # Row vanished between the get and the update; treat as
                # not-found rather than surfacing the DDB error.
                return None
            raise

        logger.info(
            "Updated specialty agent draft: asset_id=%s, actor=%s",
            asset_id,
            ctx.actor_id,
        )

        return {"updatedAt": now}

    async def delete(
        self, ctx: LabRequestContext, asset_id: str
    ) -> Optional[bool]:
        """Hard-delete a Specialty_Agent draft.

        The new schema has no ``archived`` status; the draft row is
        removed from the AssetIndex via a single-op ``transact_write``
        with an ``attribute_exists(PK)`` precondition, then the S3
        prefix is reclaimed via ``delete_prefix``.

        Returns ``True`` on success, ``None`` when the draft does not
        exist or is owned by a different actor (no existence disclosure).
        """
        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)

        # Capture the canonical prefix before deleting the row so Phase 2 can
        # reclaim the correct (bucket-absolute) location where artifacts now
        # live (BUG-2). Cross-actor access returns None here (no disclosure).
        existing = await self._index.get(pk, sk)
        prefix_to_reclaim = getattr(existing, "s3_prefix", None) if existing else None

        # Phase 1: hard-delete the AssetIndex row. Mirrors the
        # transact_write op shape used in :mod:`lab.skill_crud` and
        # :mod:`lab._canonical_ops` (single Delete entry with explicit
        # TableName + Key, no DDB-typed value wrappers — the Table
        # resource layer marshals scalar Python values for us).
        ops: list[dict[str, Any]] = [
            {
                "Delete": {
                    "TableName": self._index.table_name,
                    "Key": {"PK": pk, "SK": sk},
                    "ConditionExpression": "attribute_exists(PK)",
                }
            }
        ]
        try:
            await self._index.transact_write(ops)
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            # A cancelled transaction whose only condition was
            # ``attribute_exists(PK)`` means the row is gone. Surface
            # as not-found rather than as an error.
            if code == "TransactionCanceledException":
                reasons = err.response.get("CancellationReasons", [])
                if any(
                    isinstance(r, dict)
                    and r.get("Code") == "ConditionalCheckFailed"
                    for r in reasons
                ):
                    return None
                raise
            if code == "ConditionalCheckFailedException":
                return None
            raise

        # Phase 2: reclaim S3 prefix at the record's canonical (bucket-absolute)
        # location, matching where create/update write artifacts (BUG-2). If the
        # row was already gone we have no prefix to reclaim — skip cleanly.
        if prefix_to_reclaim:
            try:
                await self._s3.delete_prefix_absolute(prefix_to_reclaim)
            except Exception as s3_err:  # noqa: BLE001
                # Index is already gone; an S3-prefix failure leaves an
                # orphan but does not compromise correctness. Log and
                # proceed — the janitor reclaims the prefix later.
                logger.warning(
                    "Specialty delete: index removed but S3 prefix cleanup failed; "
                    "orphan_prefix=%s actor=%s asset=%s error=%r",
                    prefix_to_reclaim,
                    ctx.actor_id,
                    asset_id,
                    s3_err,
                )

        logger.info(
            "Deleted specialty agent draft: asset_id=%s, actor=%s",
            asset_id,
            ctx.actor_id,
        )

        return True

    async def list(self, ctx: LabRequestContext) -> list[SpecialtyAgent]:
        """List all Specialty_Agent drafts owned by the requesting actor.

        Queries the actor's drafts partition with an SK prefix scoped to
        the specialty-agent category and returns a list of typed
        :class:`SpecialtyAgent` records. The service-layer handler is
        responsible for converting them to the wire shape and appending
        the read-only core Task Agent reference (Requirement 3.7).

        Pagination is intentionally hidden — the lab UI lists drafts as
        a flat panel and a single user is unlikely to exceed the first
        page; if that changes, expose ``last_evaluated_key`` here.
        """
        pk = make_pk_user_owned(ctx.actor_id)
        items, _ = await self._index.query_pk(pk, sk_prefix=f"{_CATEGORY}#")
        # The drafts partition only contains drafts, but defend against a
        # malformed row leaking in via a future migration.
        return [item for item in items if isinstance(item, SpecialtyAgent)]

    async def promote(
        self,
        ctx: LabRequestContext,
        draft_id: str,
        scope: "Scope",
        group_ids: Optional[list[str]] = None,
        published_id: Optional[str] = None,
        *,
        org_id: Optional[str] = None,
        publish_policy_owner_ids: Optional[list[str]] = None,
    ) -> "Promoted":
        """Promote a Specialty_Agent draft into a new Published_Record.

        Thin wrapper over :func:`lab._canonical_ops.promote_asset` with
        the specialty-agent category constant. See that helper for the
        full sequence and error semantics.

        Requirements: 4.1, 4.2, 4.3, 4.4, 4.5
        Properties: 3, 4
        """
        from ._canonical_ops import promote_asset

        return await promote_asset(
            ctx=ctx,
            asset_index=self._index,
            s3=self._s3,
            category=_CATEGORY,
            draft_id=draft_id,
            scope=scope,
            group_ids=group_ids,
            published_id=published_id,
            org_id=org_id,
            publish_policy_owner_ids=publish_policy_owner_ids,
        )

    async def share(
        self,
        ctx: LabRequestContext,
        published_id: str,
        version: int,
        new_scope: "Scope",
        group_ids: Optional[list[str]] = None,
        *,
        org_id: Optional[str] = None,
    ) -> "Shared":
        """Widen a published Specialty_Agent_Record's scope.

        Thin wrapper over :func:`lab._canonical_ops.share_asset` with
        the specialty-agent category constant. See that helper for the
        full sequence and error semantics.

        Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 13.1, 13.2, 13.3, 13.4
        Properties: 5, 6, 8
        """
        from ._canonical_ops import share_asset

        return await share_asset(
            ctx=ctx,
            asset_index=self._index,
            s3=self._s3,
            category=_CATEGORY,
            published_id=published_id,
            version=version,
            new_scope=new_scope,
            group_ids=group_ids,
            org_id=org_id,
        )

    async def deprecate(
        self,
        ctx: LabRequestContext,
        published_id: str,
        version: int,
    ) -> "Deprecated":
        """Mark a published Specialty_Agent_Record as deprecated.

        Thin wrapper over :func:`lab._canonical_ops.deprecate_asset`
        with the specialty-agent category constant. See that helper
        for the full sequence and error semantics.

        Requirements: 6.1, 6.2, 6.3, 6.4, 6.5
        Properties: 3, 8
        """
        from ._canonical_ops import deprecate_asset

        return await deprecate_asset(
            ctx=ctx,
            asset_index=self._index,
            category=_CATEGORY,
            published_id=published_id,
            version=version,
        )

    async def clone(
        self,
        ctx: LabRequestContext,
        published_id: str,
        version: int,
    ) -> "Cloned":
        """Clone a published Specialty_Agent_Record into a new draft owned by the actor.

        Thin wrapper over :func:`lab._canonical_ops.clone_asset` with
        the specialty-agent category constant. The cloned draft
        carries the source row's ``compatible_skills`` payload so the
        actor can iterate on the persona without re-binding skills.

        Requirements: 7.1, 7.2, 7.3, 7.4
        """
        from ._canonical_ops import clone_asset

        return await clone_asset(
            ctx=ctx,
            asset_index=self._index,
            s3=self._s3,
            category=_CATEGORY,
            published_id=published_id,
            version=version,
        )

    def cascade_remove_skill(self, skill_asset_id: str) -> int:
        """No-op cascade stub for the unified-platform migration.

        Requirement 3.4 (Property 9) requires that when a Skill draft
        is deleted, every Specialty_Agent draft's ``compatibleSkills``
        list referencing that skill drops the entry. The legacy
        per-actor ``query_by_category`` implementation made this a
        single AssetIndex query, but under the new single-table schema
        the cascade fans out across every user partition that may
        reference the skill — there is no longer a global secondary
        index keyed by ``(category, status)`` to drive a cheap scan.

        Designing that fan-out (likely via the ``byPublishedId`` GSI
        plus a per-actor follow-up query, or a dedicated ``compatible
        skill back-reference`` GSI) is tracked separately and is out of
        scope for this CRUD-layer migration.

        For now this method is a no-op that returns ``0``. Callers
        (specifically :meth:`SkillCRUD.delete`) wrap the call in a
        try/except so a transitional cascade failure cannot break
        delete; here we degrade further to "silent success" so the
        caller's log line is not noisy in the common case.

        TODO(unified-platform Requirement 3.4): implement the cross-PK
        cascade — likely behind a tracker issue once the GSI strategy
        for skill back-references is decided. Tests that exercise the
        cascade should be marked ``xfail`` until that lands.

        Args:
            skill_asset_id: The asset_id of the deleted Skill draft.

        Returns:
            Always ``0`` while the cascade is stubbed out.
        """
        logger.info(
            "cascade_remove_skill: stubbed no-op for skill_asset_id=%s "
            "(unified-platform migration; Requirement 3.4 cascade pending)",
            skill_asset_id,
        )
        return 0

    async def rollback(
        self,
        ctx: LabRequestContext,
        published_id: str,
        to_version: Optional[int] = None,
    ) -> "RolledBack":
        """Roll back the ``current`` pointer of a Specialty_Agent lineage.

        Thin wrapper over :func:`lab._canonical_ops.rollback_asset`
        with the specialty-agent category constant. Rollback NEVER
        mutates ``status`` (Requirement 8.3) and does not touch S3 —
        it toggles the ``current`` flag between the prior current row
        and the resolved target version in a single
        ``TransactWriteItems``.

        Requirements: 8.1, 8.2, 8.3, 8.4, 8.5
        Properties: 3, 8
        """
        from ._canonical_ops import rollback_asset

        return await rollback_asset(
            ctx=ctx,
            asset_index=self._index,
            category=_CATEGORY,
            published_id=published_id,
            to_version=to_version,
        )

    async def set_external_access(
        self,
        ctx: LabRequestContext,
        published_id: str,
        enabled: bool,
    ) -> "ExternalAccessSet":
        """Revoke or re-enable a Custom Agent's external access (Req 16).

        Thin wrapper over
        :func:`lab._canonical_ops.set_external_access_asset` with the
        specialty-agent category constant. This is the Owner's control for the
        request-time external-access gate that is SEPARATE from any version
        snapshot's ``integration_config``.

        Passing ``enabled=False`` revokes access immediately and independently
        of publishing (Req 16.1); the gate is written across every row of the
        lineage (working copy + all version snapshots) so a subsequent rollback
        cannot silently re-grant it (Req 16.2, 16.3 / Property 16). Passing
        ``enabled=True`` re-enables access.

        Requirements: 16.1, 16.2, 16.3
        Properties: 16
        """
        from ._canonical_ops import set_external_access_asset

        return await set_external_access_asset(
            ctx=ctx,
            asset_index=self._index,
            category=_CATEGORY,
            published_id=published_id,
            enabled=enabled,
        )
