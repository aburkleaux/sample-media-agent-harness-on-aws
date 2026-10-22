"""Tests for the `bearer` gateway auth variant.

Covers the user-configured remote MCP server path: a static long-lived
token (PAT / API key) pasted into the register form, stored in Secrets
Manager, and replayed as a request header at connect time.

The invariant under test throughout is R2: the registry row must never
hold the literal credential, only an ARN.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.registry import (  # noqa: E402
    _AUTH_CONFIG_CLASSES,
    BearerAuthConfig,
    GatewayDescriptor,
    JwtAuthConfig,
    _auth_config_from_dict,
)


# ---------------------------------------------------------------------------
# BearerAuthConfig serialisation
# ---------------------------------------------------------------------------


def test_defaults_are_authorization_bearer():
    cfg = BearerAuthConfig()
    assert cfg.auth_type == "bearer"
    assert cfg.header_name == "Authorization"
    assert cfg.value_prefix == "Bearer "


def test_to_dict_carries_arn_when_set():
    cfg = BearerAuthConfig(token_arn="arn:aws:secretsmanager:us-west-2:1:secret:t")
    d = cfg.to_dict()
    assert d["auth_type"] == "bearer"
    assert d["token_arn"] == "arn:aws:secretsmanager:us-west-2:1:secret:t"


def test_to_dict_omits_arn_when_unset():
    assert "token_arn" not in BearerAuthConfig().to_dict()


def test_public_dict_never_exposes_the_arn():
    """R8 — the public view must not leak the secret reference."""
    cfg = BearerAuthConfig(token_arn="arn:aws:secretsmanager:us-west-2:1:secret:t")
    assert "token_arn" not in cfg.public_dict()
    assert cfg.public_dict()["header_name"] == "Authorization"


def test_round_trip_through_dict():
    cfg = BearerAuthConfig(
        token_arn="arn:x", header_name="X-API-Key", value_prefix=""
    )
    again = BearerAuthConfig.from_dict(cfg.to_dict())
    assert again == cfg


def test_from_dict_preserves_deliberately_empty_prefix():
    """An API-key header sends the bare secret — `or` would clobber this."""
    cfg = BearerAuthConfig.from_dict(
        {"auth_type": "bearer", "header_name": "X-API-Key", "value_prefix": ""}
    )
    assert cfg.value_prefix == ""


def test_from_dict_defaults_prefix_when_key_absent():
    cfg = BearerAuthConfig.from_dict({"auth_type": "bearer"})
    assert cfg.value_prefix == "Bearer "
    assert cfg.header_name == "Authorization"


def test_dispatch_resolves_bearer():
    cfg = _auth_config_from_dict("bearer", {"header_name": "X-Token"})
    assert isinstance(cfg, BearerAuthConfig)
    assert cfg.header_name == "X-Token"


def test_bearer_is_in_the_runtime_class_tuple():
    """Guards the bug where an inline isinstance tuple omitted the variant."""
    assert BearerAuthConfig in _AUTH_CONFIG_CLASSES


# ---------------------------------------------------------------------------
# Descriptor integration
# ---------------------------------------------------------------------------


def _descriptor(**auth_kwargs) -> GatewayDescriptor:
    return GatewayDescriptor(
        id="my-mcp",
        name="My MCP",
        url="https://mcp.example.com/sse",
        auth_type="bearer",
        auth_config=BearerAuthConfig(**auth_kwargs),
    )


def test_descriptor_round_trips_bearer():
    d = _descriptor(token_arn="arn:x")
    again = GatewayDescriptor.from_dict(d.to_dict())
    assert isinstance(again.auth_config, BearerAuthConfig)
    assert again.auth_config.token_arn == "arn:x"
    assert again.auth_type == "bearer"


def test_connection_config_exposes_header_shape_and_arn():
    cfg = _descriptor(
        token_arn="arn:x", header_name="X-API-Key", value_prefix=""
    ).to_connection_config()
    assert cfg["auth_type"] == "bearer"
    assert cfg["header_name"] == "X-API-Key"
    assert cfg["value_prefix"] == ""
    assert cfg["token_arn"] == "arn:x"
    # The literal must never appear in the config the descriptor emits;
    # MCPManager merges it in after resolving the ARN.
    assert "bearer_token" not in cfg


def test_descriptor_public_dict_hides_token_arn():
    pub = _descriptor(token_arn="arn:x").public_dict()
    assert "token_arn" not in pub.get("auth_config", {})


def test_update_accepts_a_typed_bearer_config():
    """`update()` gated on an isinstance tuple that used to omit bearer."""
    assert isinstance(BearerAuthConfig(), _AUTH_CONFIG_CLASSES)


# ---------------------------------------------------------------------------
# Secret storage on register()
# ---------------------------------------------------------------------------


class _FakeSecrets:
    def __init__(self):
        self.created: list[dict] = []

    def create_secret(self, **kwargs):
        self.created.append(kwargs)
        return {"ARN": "arn:aws:secretsmanager:us-west-2:1:secret:made"}


class _FakeTable:
    def __init__(self):
        self.items: list[dict] = []

    def put_item(self, Item, **kwargs):  # noqa: N803 — boto3 casing
        self.items.append(Item)


@pytest.mark.asyncio
async def test_register_stores_bearer_token_and_persists_only_the_arn():
    from gateway.registry import GatewayRegistry

    secrets = _FakeSecrets()
    table = _FakeTable()
    reg = GatewayRegistry(
        ddb_table=table, project_name="harness", secrets_client=secrets
    )

    d = _descriptor()
    await reg.register(d, raw_secret="ghp_supersecret")

    # The token went to Secrets Manager...
    assert len(secrets.created) == 1
    assert secrets.created[0]["SecretString"] == "ghp_supersecret"
    assert "Bearer token" in secrets.created[0]["Description"]
    # ...the ARN landed on the descriptor...
    assert d.auth_config.token_arn.endswith(":made")
    # ...and the persisted row contains the ARN, never the literal (R2).
    row = table.items[0]
    assert "ghp_supersecret" not in str(row)
    assert row["auth_config"]["token_arn"].endswith(":made")


@pytest.mark.asyncio
async def test_register_still_rejects_a_secret_for_none_auth():
    from gateway.registry import GatewayRegistry, NoneAuthConfig

    reg = GatewayRegistry(
        ddb_table=_FakeTable(), project_name="harness",
        secrets_client=_FakeSecrets(),
    )
    d = GatewayDescriptor(
        id="pub", name="Public", url="https://x/sse",
        auth_type="none", auth_config=NoneAuthConfig(),
    )
    with pytest.raises(ValueError, match="JWT or bearer"):
        await reg.register(d, raw_secret="nope")


@pytest.mark.asyncio
async def test_register_jwt_path_unchanged():
    """The generalisation must not move the OAuth secret to the wrong field."""
    from gateway.registry import GatewayRegistry

    secrets = _FakeSecrets()
    reg = GatewayRegistry(
        ddb_table=_FakeTable(), project_name="harness", secrets_client=secrets
    )
    d = GatewayDescriptor(
        id="oauth-gw", name="OAuth GW", url="https://x/sse",
        auth_type="jwt",
        auth_config=JwtAuthConfig(token_endpoint="https://t", client_id="cid"),
    )
    await reg.register(d, raw_secret="shhh")
    assert d.auth_config.client_secret_arn.endswith(":made")
    assert "OAuth client secret" in secrets.created[0]["Description"]


# ---------------------------------------------------------------------------
# Connect-time header construction
# ---------------------------------------------------------------------------


def test_connection_builds_default_bearer_header():
    from mcp_manager import GatewayConnection

    conn = GatewayConnection(
        "my-mcp",
        {"url": "https://mcp.example.com/sse", "auth_type": "bearer",
         "bearer_token": "tok123"},
    )
    assert conn.header_name == "Authorization"
    assert f"{conn.value_prefix}{conn.bearer_token}" == "Bearer tok123"


def test_connection_builds_api_key_header_with_empty_prefix():
    from mcp_manager import GatewayConnection

    conn = GatewayConnection(
        "my-mcp",
        {"url": "https://x/sse", "auth_type": "bearer", "bearer_token": "k",
         "header_name": "X-API-Key", "value_prefix": ""},
    )
    assert conn.header_name == "X-API-Key"
    assert f"{conn.value_prefix}{conn.bearer_token}" == "k"


def test_connect_fails_loudly_when_token_missing():
    """Better a clear error than an unauthenticated connect and a far-end 401."""
    from mcp_manager import GatewayConnection

    conn = GatewayConnection(
        "my-mcp", {"url": "https://x/sse", "auth_type": "bearer"}
    )
    with pytest.raises(RuntimeError, match="no token was resolved"):
        conn._do_connect()


# ---------------------------------------------------------------------------
# Secret resolution boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_resolves_token_arn_into_bearer_token():
    from mcp_manager import MCPManager

    class _SM:
        def get_secret_value(self, SecretId):  # noqa: N803
            assert SecretId == "arn:aws:secretsmanager:us-west-2:1:secret:t"
            return {"SecretString": "resolved-token"}

    mgr = MCPManager()
    conn = await mgr._build_connection_for_descriptor(
        _descriptor(token_arn="arn:aws:secretsmanager:us-west-2:1:secret:t"),
        secrets_client=_SM(),
    )
    assert conn is not None
    assert conn.bearer_token == "resolved-token"
    # The ARN must not survive into the connection config.
    assert conn.auth_type == "bearer"


@pytest.mark.asyncio
async def test_manager_returns_none_when_token_cannot_be_resolved():
    from mcp_manager import MCPManager

    class _SM:
        def get_secret_value(self, SecretId):  # noqa: N803
            raise RuntimeError("AccessDenied")

    mgr = MCPManager()
    conn = await mgr._build_connection_for_descriptor(
        _descriptor(token_arn="arn:aws:secretsmanager:us-west-2:1:secret:t"),
        secrets_client=_SM(),
    )
    assert conn is None


def test_fetch_secret_string_unwraps_a_token_bundle():
    from mcp_manager import _fetch_secret_string

    class _SM:
        def get_secret_value(self, SecretId):  # noqa: N803
            return {"SecretString": '{"token": "from-bundle"}'}

    assert _fetch_secret_string(_SM(), "arn:x") == "from-bundle"


def test_fetch_secret_string_still_prefers_client_secret():
    from mcp_manager import _fetch_secret_string

    class _SM:
        def get_secret_value(self, SecretId):  # noqa: N803
            return {
                "SecretString":
                    '{"clientSecret": "oauth", "token": "other"}'
            }

    assert _fetch_secret_string(_SM(), "arn:x") == "oauth"


def test_fetch_secret_string_returns_bare_token_verbatim():
    from mcp_manager import _fetch_secret_string

    class _SM:
        def get_secret_value(self, SecretId):  # noqa: N803
            return {"SecretString": "ghp_rawtoken"}

    assert _fetch_secret_string(_SM(), "arn:x") == "ghp_rawtoken"
