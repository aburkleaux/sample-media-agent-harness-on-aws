"""Typed descriptors and DDB wrapper for the gateway registry (R1, R5, R6).

`GatewayDescriptor` is the in-memory shape backing one row of the
`GATEWAY#REGISTERED` DDB table. The Main Agent loads it on startup in place
of the legacy `GATEWAYS` env var (R5), and admin runtime actions
(R3, R4) read and write through it.

Auth-specific configuration is split into a discriminated `auth_config`
union (`jwt` | `sigv4` | `none` | `bearer`) so callers can branch on the
descriptor without falling back to raw dict access. The discriminator
(`auth_type`) lives both on the descriptor and on the serialized
`auth_config` sub-dict so round-trips through DDB and the admin UI stay
symmetric.

Secrets never live on the descriptor. JWT gateways carry only the
Secrets Manager ARN (`client_secret_arn`) and bearer gateways only
`token_arn`; the literal is fetched at gateway-connection time by
`MCPManager` (R2). `public_dict()` strips the ARN for the consent-panel
response (R8) so non-admin readers cannot enumerate secret references.

`GatewayRegistry` is the thin async wrapper around the DDB table. Task
2.3 implements the read surface (`list_enabled`, `get`); tasks 2.4 and
2.5 add the mutation methods (`register`, `update`, `disable`).
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Union

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError


AuthType = Literal["jwt", "sigv4", "none", "bearer", "user_jwt"]


# ---------------------------------------------------------------------------
# auth_config variants — discriminated by `auth_type`
# ---------------------------------------------------------------------------


@dataclass
class JwtAuthConfig:
    """OAuth2 client-credentials config for a JWT-authenticated gateway.

    The actual `client_secret` is stored in Secrets Manager; only the ARN
    is persisted on the descriptor (R2).
    """

    token_endpoint: str = ""
    client_id: str = ""
    client_secret_arn: Optional[str] = None
    scope: str = ""

    auth_type: Literal["jwt"] = field(default="jwt", init=False)

    def to_dict(self) -> dict:
        d: dict = {
            "auth_type": "jwt",
            "token_endpoint": self.token_endpoint,
            "client_id": self.client_id,
            "scope": self.scope,
        }
        if self.client_secret_arn is not None:
            d["client_secret_arn"] = self.client_secret_arn
        return d

    @classmethod
    def from_dict(cls, data: dict) -> JwtAuthConfig:
        return cls(
            token_endpoint=data.get("token_endpoint", ""),
            client_id=data.get("client_id", ""),
            client_secret_arn=data.get("client_secret_arn"),
            scope=data.get("scope", ""),
        )

    def public_dict(self) -> dict:
        """Same as `to_dict()` but omits the secret ARN (R8)."""
        return {
            "auth_type": "jwt",
            "token_endpoint": self.token_endpoint,
            "client_id": self.client_id,
            "scope": self.scope,
        }


@dataclass
class SigV4AuthConfig:
    """SigV4 config for an AWS-signed gateway.

    No secret material is involved — credentials come from the runtime's
    IAM role at call time. `to_dict` and `public_dict` are equivalent.
    """

    region: str = ""
    service: str = ""

    auth_type: Literal["sigv4"] = field(default="sigv4", init=False)

    def to_dict(self) -> dict:
        return {"auth_type": "sigv4", "region": self.region, "service": self.service}

    @classmethod
    def from_dict(cls, data: dict) -> SigV4AuthConfig:
        return cls(
            region=data.get("region", ""),
            service=data.get("service", ""),
        )

    def public_dict(self) -> dict:
        return self.to_dict()


@dataclass
class NoneAuthConfig:
    """Empty auth config for a public / unauthenticated gateway."""

    auth_type: Literal["none"] = field(default="none", init=False)

    def to_dict(self) -> dict:
        return {"auth_type": "none"}

    @classmethod
    def from_dict(cls, data: dict) -> NoneAuthConfig:  # noqa: ARG003 — empty case
        return cls()

    def public_dict(self) -> dict:
        return self.to_dict()


@dataclass
class BearerAuthConfig:
    """Static-token config for a user-registered remote MCP server.

    The user pastes a long-lived credential (a PAT, an API key) rather
    than OAuth2 client-credentials. The token is stored in Secrets
    Manager and only its ARN is persisted (R2), exactly as for
    :class:`JwtAuthConfig`; the difference is purely how the token is
    *acquired* — there is no token endpoint to exchange against, so the
    value goes straight onto the request header.

    `header_name` and `value_prefix` are configurable so one variant
    covers both conventions without a fifth auth type:

    - `Authorization` + `"Bearer "` — the default, an OAuth-style bearer.
    - `X-API-Key` + `""` — a raw API-key header.

    The prefix is stored verbatim including its trailing space; an empty
    prefix sends the secret as the entire header value.
    """

    token_arn: Optional[str] = None
    header_name: str = "Authorization"
    value_prefix: str = "Bearer "

    auth_type: Literal["bearer"] = field(default="bearer", init=False)

    def to_dict(self) -> dict:
        d: dict = {
            "auth_type": "bearer",
            "header_name": self.header_name,
            "value_prefix": self.value_prefix,
        }
        if self.token_arn is not None:
            d["token_arn"] = self.token_arn
        return d

    @classmethod
    def from_dict(cls, data: dict) -> BearerAuthConfig:
        return cls(
            token_arn=data.get("token_arn"),
            header_name=data.get("header_name") or "Authorization",
            # `or` would clobber a deliberately empty prefix (API-key
            # style), so only default when the key is absent entirely.
            value_prefix=(
                data["value_prefix"]
                if "value_prefix" in data and data["value_prefix"] is not None
                else "Bearer "
            ),
        )

    def public_dict(self) -> dict:
        """Same as `to_dict()` but omits the token ARN (R8)."""
        return {
            "auth_type": "bearer",
            "header_name": self.header_name,
            "value_prefix": self.value_prefix,
        }


@dataclass
class UserJwtAuthConfig:
    """Pass the CALLING USER's token through as inbound auth.

    For a gateway whose authorizer is the end-user pool. Unlike every other
    variant there is no stored credential at all -- not even an ARN. The token
    is the caller's own, forwarded per invocation from the proxy through the
    runtime, so the gateway validates a real user identity and its interceptor
    can scope tools to a verified claim rather than an asserted header.

    Because the credential is per-caller and short lived, a connection built
    with it is only valid for that user and only until the token expires. That
    is compatible with how connections already work: AgentCore gives each
    session its own microVM and a session belongs to one user, so a container
    serves one actor for its lifetime.
    """

    auth_type: Literal["user_jwt"] = field(default="user_jwt", init=False)

    def to_dict(self) -> dict:
        return {"auth_type": "user_jwt"}

    @classmethod
    def from_dict(cls, data: dict) -> UserJwtAuthConfig:  # noqa: ARG003 — empty case
        return cls()

    def public_dict(self) -> dict:
        return self.to_dict()


AuthConfig = Union[
    JwtAuthConfig, SigV4AuthConfig, NoneAuthConfig, BearerAuthConfig,
    UserJwtAuthConfig,
]

# Runtime counterpart of the `AuthConfig` union, for `isinstance` checks.
# Kept adjacent to the union so adding a variant is a single-site change —
# an inline isinstance tuple silently excluded new variants and made
# `update()` reject them as "not an auth config".
_AUTH_CONFIG_CLASSES = (
    JwtAuthConfig, SigV4AuthConfig, NoneAuthConfig, BearerAuthConfig,
    UserJwtAuthConfig,
)


def _auth_config_from_dict(auth_type: str, data: dict) -> AuthConfig:
    """Dispatch to the right `*AuthConfig.from_dict` based on the discriminator."""
    if auth_type == "jwt":
        return JwtAuthConfig.from_dict(data)
    if auth_type == "sigv4":
        return SigV4AuthConfig.from_dict(data)
    if auth_type == "none":
        return NoneAuthConfig.from_dict(data)
    if auth_type == "bearer":
        return BearerAuthConfig.from_dict(data)
    if auth_type == "user_jwt":
        return UserJwtAuthConfig.from_dict(data)
    raise ValueError(f"Unknown auth_type: {auth_type!r}")


# ---------------------------------------------------------------------------
# GatewayDescriptor — one row of the gateway registry (R1)
# ---------------------------------------------------------------------------


@dataclass
class GatewayDescriptor:
    """Typed descriptor for one row of the gateway registry (R1).

    Fields mirror the DDB schema documented in the design's "Components"
    section:

    - `id`            — slug; uniquely identifies the row (`SK`)
    - `name`          — human-facing display name
    - `description`   — human-facing free-text description
    - `url`           — MCP endpoint URL
    - `auth_type`     — discriminator for `auth_config`
    - `auth_config`   — typed auth payload (`JwtAuthConfig` | `SigV4AuthConfig` | `NoneAuthConfig` | `BearerAuthConfig`)
    - `enabled`       — soft-delete flag (R4)
    - `created_at`    — ISO-8601 stamp set on `register`
    - `updated_at`    — ISO-8601 stamp bumped on every mutation
    - `created_by`    — Cognito sub of the admin who registered it
    """

    id: str
    name: str
    url: str
    auth_type: AuthType
    auth_config: AuthConfig
    description: str = ""
    enabled: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: Optional[str] = None
    # Optional fail-closed tool allowlist. Only these tool names are exposed.
    # A registry-level concern rather than an auth one: it is about which tools a
    # gateway may surface, independent of how the connection authenticates.
    tool_allowlist: tuple[str, ...] = ()
    # Per-tool job ROLE declarations for servers that follow neither the
    # start_*/*_status/*_results convention nor MCP's _meta job_role annotation.
    # Registry-level like tool_allowlist: a statement about what the server's
    # tools MEAN, not about how the connection authenticates. Shape:
    #   {"submit_job": {"job_role": "start", "job_group": "fal|job"}, ...}
    job_role_overrides: dict = field(default_factory=dict)
    # Explicit trust plane (Wave 4 / Req 13.8). Trust is NEVER inferred from
    # auth_type, target prefix, display name, or registry order — it is stated
    # here. "tenant" (default) = user-owned targets behind CUSTOM_JWT + the
    # ownership interceptor; "platform" = shared IAM/policy-governed targets;
    # "federated" = an external gateway registered as-is.
    plane: Literal["platform", "tenant", "federated"] = "tenant"

    def __post_init__(self) -> None:
        # Defensive: if a caller hand-built a descriptor with a mismatched
        # discriminator, surface it loudly rather than silently letting the
        # DDB row drift away from the auth_config payload it points at.
        expected = self.auth_config.auth_type
        if self.auth_type != expected:
            raise ValueError(
                f"auth_type {self.auth_type!r} does not match auth_config "
                f"variant {expected!r}"
            )

    def to_dict(self) -> dict:
        """Full serialization, suitable for writing to DDB."""
        d: dict = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "auth_type": self.auth_type,
            "auth_config": self.auth_config.to_dict(),
            "enabled": self.enabled,
        }
        if self.tool_allowlist:
            d["tool_allowlist"] = list(self.tool_allowlist)
        if self.job_role_overrides:
            d["job_role_overrides"] = dict(self.job_role_overrides)
        if self.created_at is not None:
            d["created_at"] = self.created_at
        if self.updated_at is not None:
            d["updated_at"] = self.updated_at
        if self.created_by is not None:
            d["created_by"] = self.created_by
        return d

    @classmethod
    def from_dict(cls, data: dict) -> GatewayDescriptor:
        """Reverse of `to_dict()`. Accepts a DDB row (or compatible JSON dict)."""
        auth_type = data.get("auth_type", "none")
        auth_config = _auth_config_from_dict(auth_type, data.get("auth_config", {}))
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            url=data["url"],
            auth_type=auth_type,
            auth_config=auth_config,
            enabled=bool(data.get("enabled", True)),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            created_by=data.get("created_by"),
            tool_allowlist=tuple(data.get("tool_allowlist") or ()),
            job_role_overrides=dict(data.get("job_role_overrides") or {}),
        )

    def public_dict(self) -> dict:
        """Dict safe for non-admin readers (R8); strips `client_secret_arn`.

        Used by the `gateway_list` action and by the consent-panel UI,
        both of which are authenticated but not necessarily admin.
        Admins read the full descriptor via `to_dict()` when they need
        to round-trip it through the edit modal.
        """
        d = self.to_dict()
        d["auth_config"] = self.auth_config.public_dict()
        return d

    def to_connection_config(self) -> dict:
        """Build the kwargs dict that `GatewayConnection` consumes.

        Mirrors the legacy `GATEWAYS` env-var entry shape so `MCPManager`
        can keep instantiating `GatewayConnection` unchanged. For JWT
        gateways the dict carries `client_secret_arn` rather than the
        raw secret — `MCPManager.load_gateways_from_registry` (task 3.1)
        is responsible for resolving the ARN against Secrets Manager and
        merging the literal `client_secret` in before construction.
        """
        cfg: dict = {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "auth_type": self.auth_type,
        }
        if self.tool_allowlist:
            cfg["tool_allowlist"] = list(self.tool_allowlist)
        if self.job_role_overrides:
            cfg["job_role_overrides"] = dict(self.job_role_overrides)
        if isinstance(self.auth_config, JwtAuthConfig):
            cfg["client_id"] = self.auth_config.client_id
            cfg["token_endpoint"] = self.auth_config.token_endpoint
            cfg["scope"] = self.auth_config.scope
            if self.auth_config.client_secret_arn is not None:
                cfg["client_secret_arn"] = self.auth_config.client_secret_arn
        elif isinstance(self.auth_config, SigV4AuthConfig):
            cfg["region"] = self.auth_config.region
            cfg["service"] = self.auth_config.service
        elif isinstance(self.auth_config, BearerAuthConfig):
            cfg["header_name"] = self.auth_config.header_name
            cfg["value_prefix"] = self.auth_config.value_prefix
            if self.auth_config.token_arn is not None:
                cfg["token_arn"] = self.auth_config.token_arn
        # NoneAuthConfig contributes nothing beyond the base fields.
        return cfg

    @classmethod
    def from_legacy_env(cls, cfg: dict) -> GatewayDescriptor:
        """Build a descriptor from a single legacy `GATEWAYS` env-var entry (R5).

        The legacy shape is the dict the CDK stack emits today (see
        `infra/lib/agent-infra-stack.ts`)::

            {
              "id":             "...",
              "name":           "...",
              "url":            "...",
              "client_id":      "...",
              "client_secret":  "...",   # never persisted to the descriptor
              "token_endpoint": "...",
              "scope":          "..."
            }

        which is purely OAuth/JWT — there is no `auth_type` discriminator.
        The migration writes one row per entry using `auth_type="jwt"`.
        The raw `client_secret` is intentionally **not** read here: the
        migration script (task 6.1) pops it before calling this method,
        writes it to Secrets Manager, and patches the resulting ARN onto
        `auth_config.client_secret_arn` afterwards.
        """
        gw_id = cfg.get("id") or cfg.get("name") or "unknown"
        return cls(
            id=str(gw_id),
            name=str(cfg.get("name", gw_id)),
            description=str(cfg.get("description", "")),
            url=str(cfg.get("url", "")),
            auth_type="jwt",
            auth_config=JwtAuthConfig(
                token_endpoint=str(cfg.get("token_endpoint", "")),
                client_id=str(cfg.get("client_id", "")),
                client_secret_arn=cfg.get("client_secret_arn"),
                scope=str(cfg.get("scope", "")),
            ),
            enabled=bool(cfg.get("enabled", True)),
        )


__all__ = [
    "AuthType",
    "AuthConfig",
    "JwtAuthConfig",
    "SigV4AuthConfig",
    "NoneAuthConfig",
    "GatewayDescriptor",
    "GatewayRegistry",
    "DuplicateGatewayError",
    "GATEWAY_PK",
]


# ---------------------------------------------------------------------------
# GatewayRegistry — DDB wrapper for the gateway registry table (R1, R6)
# ---------------------------------------------------------------------------


#: Single fixed partition key for every registered gateway. Per the design's
#: "Architecture" section, all gateway rows live under one PK so
#: ``list_enabled`` is a single ``Query`` (not a ``Scan``) and the SK is the
#: gateway id.
GATEWAY_PK: str = "GATEWAY#REGISTERED"


class DuplicateGatewayError(ValueError):
    """Raised by :meth:`GatewayRegistry.register` when ``id`` already exists.

    The conditional ``PutItem`` in ``register`` uses
    ``attribute_not_exists(SK)`` to refuse silent overwrites (R1). Boto3
    surfaces that as a ``ClientError`` with code
    ``ConditionalCheckFailedException``; we re-raise it as this typed
    error so the runtime action (`gateway_register`, task 5.3) can map
    it to a structured ``{"error": "duplicate_id"}`` response without
    string-matching botocore exceptions.
    """

    def __init__(self, gateway_id: str) -> None:
        super().__init__(f"gateway id already registered: {gateway_id!r}")
        self.gateway_id = gateway_id


class GatewayRegistry:
    """Thin async wrapper around the gateway-registry DDB table (R1, R6).

    Mirrors the boto3 usage in :class:`AssetIndex`: the ``Table`` resource
    is supplied by the caller (so the runtime, tests, and the migration
    script can each inject their own resource), boto3 calls are
    synchronous and run via :func:`asyncio.to_thread` so the API stays
    async-friendly for the AgentCore runtime, and the optional
    ``secrets_client`` is held here for the mutation methods (tasks 2.4 /
    2.5) without forcing reads to require it.

    Args:
        ddb_table: A boto3 DynamoDB ``Table`` resource (or compatible
            mock) bound to the ``${projectName}-gateway-registry`` table.
        secrets_client: Optional boto3 ``secretsmanager`` client. Only
            required by the mutation methods; reads (``list_enabled`` /
            ``get``) never touch Secrets Manager.
        project_name: Optional deployment slug used to build the Secrets
            Manager secret name (``/<project_name>/gateway/<id>/secret``)
            in :meth:`register`. Falls back to the ``PROJECT_NAME`` env
            var when not supplied; ``register`` raises ``ValueError`` if
            neither is set and a ``raw_secret`` is provided.
    """

    def __init__(
        self,
        ddb_table: Any,
        secrets_client: Any = None,
        project_name: Optional[str] = None,
    ) -> None:
        self._table = ddb_table
        self._secrets = secrets_client
        self._project_name = project_name or os.environ.get("PROJECT_NAME") or None

    async def list_enabled(self) -> list[GatewayDescriptor]:
        """Return every enabled gateway descriptor in the registry (R1, R6).

        Issues a DDB ``Query`` against ``PK = "GATEWAY#REGISTERED"`` with
        a ``FilterExpression`` of ``enabled = true`` so disabled rows are
        skipped server-side. Pages through ``LastEvaluatedKey`` until
        the partition is exhausted; the registry is small (one row per
        gateway) so paging is defensive rather than load-bearing.

        Rows that fail to decode are skipped silently — keeping a single
        malformed row from breaking startup matches the resilience
        pattern in :meth:`AssetIndex.query_pk`.
        """
        descriptors: list[GatewayDescriptor] = []
        last_key: Optional[dict[str, Any]] = None

        while True:
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key("PK").eq(GATEWAY_PK),
                "FilterExpression": Attr("enabled").eq(True),
            }
            if last_key is not None:
                kwargs["ExclusiveStartKey"] = last_key

            def _do_query(_kwargs: dict[str, Any] = kwargs) -> dict[str, Any]:
                return self._table.query(**_kwargs)

            resp = await asyncio.to_thread(_do_query)
            for raw in resp.get("Items", []):
                try:
                    descriptors.append(GatewayDescriptor.from_dict(raw))
                except (KeyError, ValueError):
                    # Defensive: skip malformed rows rather than failing
                    # the whole query. Mirrors AssetIndex.query_pk.
                    continue

            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break

        return descriptors

    async def get(self, id: str) -> Optional[GatewayDescriptor]:
        """Fetch a single gateway descriptor by id, or None if missing (R1).

        Issues a direct ``GetItem`` on ``(PK = "GATEWAY#REGISTERED",
        SK = id)``. Unlike :meth:`list_enabled`, this returns disabled
        rows too — admin actions need to read the soft-deleted row to
        re-enable or fully delete it (R4).
        """
        if not id:
            return None

        def _do_get() -> Optional[dict[str, Any]]:
            resp = self._table.get_item(Key={"PK": GATEWAY_PK, "SK": id})
            return resp.get("Item")

        item = await asyncio.to_thread(_do_get)
        if item is None:
            return None
        try:
            return GatewayDescriptor.from_dict(item)
        except (KeyError, ValueError):
            return None

    async def register(
        self,
        descriptor: GatewayDescriptor,
        raw_secret: Optional[str] = None,
    ) -> str:
        """Persist a new gateway descriptor and (optionally) its secret (R1, R2).

        Flow:

        1. If ``raw_secret`` is provided, write it to Secrets Manager at
           ``/<project_name>/gateway/<id>/secret``, capture the returned
           ARN, and patch it onto ``descriptor.auth_config.client_secret_arn``
           **before** the DDB write so the row never lands without its
           secret reference (R2). Raises ``ValueError`` if the descriptor
           is not JWT-authenticated or if no project name is configured.
        2. Stamp ``created_at`` / ``updated_at`` (ISO-8601 UTC) and
           ``created_by`` from the caller-supplied descriptor metadata.
           The descriptor passed in is mutated in place so the caller can
           round-trip the stamped descriptor into the live ``MCPManager``
           (task 5.3) without re-reading from DDB.
        3. Conditional ``PutItem`` with ``attribute_not_exists(SK)`` so a
           duplicate ``id`` raises :class:`DuplicateGatewayError` rather
           than silently overwriting.

        Returns:
            The gateway id on success.

        Raises:
            DuplicateGatewayError: When a row already exists at
                ``(PK = "GATEWAY#REGISTERED", SK = descriptor.id)``.
            ValueError: When ``raw_secret`` is provided but the
                descriptor is not JWT-authenticated, or when no project
                name is configured for the secret path.
        """
        # 1. Secrets Manager handoff. Only valid for JWT auth_config — the
        # other variants either don't use a secret (sigv4 uses runtime IAM)
        # or don't have one at all (none). Surface the misuse loudly rather
        # than silently dropping the secret on the floor.
        if raw_secret is not None:
            # Which descriptor field receives the resulting ARN. Both
            # variants funnel through one Secrets Manager write so the R2
            # boundary (row never holds the literal) has a single
            # implementation; only the target attribute differs.
            if isinstance(descriptor.auth_config, JwtAuthConfig):
                arn_attr = "client_secret_arn"
                secret_desc = f"OAuth client secret for gateway {descriptor.id!r}"
            elif isinstance(descriptor.auth_config, BearerAuthConfig):
                arn_attr = "token_arn"
                secret_desc = f"Bearer token for gateway {descriptor.id!r}"
            else:
                raise ValueError(
                    "raw_secret is only valid for JWT or bearer auth_config; "
                    f"got auth_type={descriptor.auth_type!r}"
                )
            if not self._project_name:
                raise ValueError(
                    "project_name is required to store a gateway secret; "
                    "pass project_name to GatewayRegistry(...) or set "
                    "PROJECT_NAME in the environment"
                )
            if self._secrets is None:
                raise ValueError(
                    "secrets_client is required to store a gateway secret"
                )

            secret_name = f"/{self._project_name}/gateway/{descriptor.id}/secret"

            def _do_create_secret() -> str:
                resp = self._secrets.create_secret(
                    Name=secret_name,
                    SecretString=raw_secret,
                    Description=secret_desc,
                )
                return resp["ARN"]

            secret_arn = await asyncio.to_thread(_do_create_secret)
            setattr(descriptor.auth_config, arn_attr, secret_arn)

        # 2. Stamp lifecycle metadata. `created_by` comes from the caller
        # (the `gateway_register` action resolves it from the JWT `sub`
        # claim, R1); `created_at` / `updated_at` are stamped here so the
        # registry owns the timeline and tests don't have to thread a
        # clock through every assertion.
        now = datetime.now(timezone.utc).isoformat()
        if descriptor.created_at is None:
            descriptor.created_at = now
        descriptor.updated_at = now

        # 3. Conditional PutItem. The SK is the gateway id; guarding on
        # `attribute_not_exists(SK)` (rather than PK, which is the fixed
        # partition key shared by every row) is the correct duplicate
        # check for this single-partition layout.
        item: dict[str, Any] = {
            "PK": GATEWAY_PK,
            "SK": descriptor.id,
            **descriptor.to_dict(),
        }

        def _do_put() -> None:
            try:
                self._table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(SK)",
                )
            except ClientError as err:
                code = err.response.get("Error", {}).get("Code", "")
                if code == "ConditionalCheckFailedException":
                    raise DuplicateGatewayError(descriptor.id) from err
                raise

        await asyncio.to_thread(_do_put)
        return descriptor.id

    async def update(
        self,
        id: str,
        patch: dict,
    ) -> Optional[GatewayDescriptor]:
        """Patch the mutable fields of an existing gateway row (R1, R4).

        Only the fields enumerated in :data:`_MUTABLE_FIELDS`
        (``name``, ``description``, ``url``, ``auth_config``,
        ``enabled``) are read out of ``patch``; everything else
        (``id``, ``created_at``, ``created_by``, the DDB key columns)
        is ignored so a caller can round-trip a full descriptor dict
        through here without accidentally rewriting immutable fields.

        Secret preservation: when the patch swaps in a new
        :class:`JwtAuthConfig` and the **patch** does not carry an
        explicit ``client_secret_arn`` key, the existing row's
        ``client_secret_arn`` (if any) is copied forward. Callers that
        want to rotate the secret must put the new ARN in the patch
        explicitly — admin "rotate secret" is the only path that should
        ever overwrite it (R2, design "Risks" §2). Switching
        ``auth_type`` away from JWT drops the ARN naturally because the
        new variant has no slot for it.

        ``updated_at`` is bumped to ``now`` on every successful patch;
        ``created_at`` and ``created_by`` are preserved.

        Args:
            id: Gateway id (the DDB ``SK``) to patch.
            patch: Partial dict of mutable fields. ``auth_config`` may
                be either an already-typed ``AuthConfig`` instance or a
                serialized dict (the shape :meth:`GatewayDescriptor.to_dict`
                emits, with the ``auth_type`` discriminator embedded).

        Returns:
            The updated descriptor, or ``None`` if no row exists at
            ``(PK = "GATEWAY#REGISTERED", SK = id)``.
        """
        if not id:
            return None

        existing = await self.get(id)
        if existing is None:
            return None

        # Build the patched descriptor by copying `existing` and applying
        # only the mutable fields. We work on the typed descriptor so
        # `__post_init__` validates any auth_type / auth_config swap.
        new_name = existing.name
        new_description = existing.description
        new_url = existing.url
        new_enabled = existing.enabled
        new_auth_type: AuthType = existing.auth_type
        new_auth_config: AuthConfig = existing.auth_config

        if "name" in patch:
            new_name = str(patch["name"])
        if "description" in patch:
            new_description = str(patch["description"])
        if "url" in patch:
            new_url = str(patch["url"])
        if "enabled" in patch:
            new_enabled = bool(patch["enabled"])

        if "auth_config" in patch:
            raw = patch["auth_config"]
            if isinstance(raw, _AUTH_CONFIG_CLASSES):
                # Caller already handed us a typed variant. Honor it as-is;
                # the discriminator on the variant is the source of truth.
                new_auth_config = raw
                new_auth_type = raw.auth_type
            elif isinstance(raw, dict):
                # Allow the discriminator to live on either the patch dict
                # (for full-dict round-trips) or the inner auth_config dict
                # (matching `GatewayDescriptor.to_dict` output).
                inner_type = raw.get("auth_type") or patch.get("auth_type")
                if not inner_type:
                    raise ValueError(
                        "auth_config patch dict must carry an 'auth_type' "
                        "discriminator"
                    )
                new_auth_config = _auth_config_from_dict(inner_type, raw)
                new_auth_type = new_auth_config.auth_type  # type: ignore[assignment]
            else:
                raise ValueError(
                    f"auth_config patch must be a dict or AuthConfig "
                    f"instance; got {type(raw).__name__}"
                )

            # Secret preservation. Only meaningful when both sides are
            # JWT — switching variants intentionally drops the ARN
            # because the new variant has no slot for it.
            if (
                isinstance(new_auth_config, JwtAuthConfig)
                and isinstance(existing.auth_config, JwtAuthConfig)
            ):
                # The patch explicitly rotates iff the inner dict carried
                # a `client_secret_arn` key (even if the value is None,
                # which we treat as "clear it"). Anything else preserves.
                patch_inner = raw if isinstance(raw, dict) else {}
                if "client_secret_arn" not in patch_inner:
                    new_auth_config.client_secret_arn = (
                        existing.auth_config.client_secret_arn
                    )

        now = datetime.now(timezone.utc).isoformat()
        updated = GatewayDescriptor(
            id=existing.id,
            name=new_name,
            description=new_description,
            url=new_url,
            auth_type=new_auth_type,
            auth_config=new_auth_config,
            enabled=new_enabled,
            created_at=existing.created_at,
            updated_at=now,
            created_by=existing.created_by,
        )

        item: dict[str, Any] = {
            "PK": GATEWAY_PK,
            "SK": updated.id,
            **updated.to_dict(),
        }

        def _do_put() -> bool:
            try:
                self._table.put_item(
                    Item=item,
                    # Defends against a race where the row gets deleted
                    # between our `get` and this `put_item`. The pre-read
                    # is what powers the "return None for missing" contract;
                    # the conditional just keeps us from creating a ghost
                    # row in that narrow window.
                    ConditionExpression="attribute_exists(SK)",
                )
                return True
            except ClientError as err:
                code = err.response.get("Error", {}).get("Code", "")
                if code == "ConditionalCheckFailedException":
                    # Lost the race: row was deleted under us. Surface as
                    # "not found" so the caller can retry or report cleanly.
                    return False
                raise

        ok = await asyncio.to_thread(_do_put)
        if not ok:
            return None
        return updated

    async def disable(self, id: str) -> Optional[GatewayDescriptor]:
        """Soft-delete a gateway by flipping ``enabled`` to ``False`` (R4).

        Thin wrapper over :meth:`update` so the soft-delete contract has
        a single owner of ``updated_at`` bumping and secret-ARN
        preservation. Returns the updated descriptor (with
        ``enabled = False``) or ``None`` if no row exists.

        The DDB row is intentionally **not** deleted; the design's
        "Disable a flaky gateway" flow keeps the row around so an admin
        can re-enable later by toggling, and so the audit trail
        (``created_by`` / ``created_at``) survives.
        """
        return await self.update(id, {"enabled": False})


# Tuple of the field names :meth:`GatewayRegistry.update` treats as
# patchable. Kept module-level (rather than per-call) so tests and the
# admin-action layer can introspect the contract without instantiating
# a registry.
_MUTABLE_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "url",
    "auth_config",
    "enabled",
)
