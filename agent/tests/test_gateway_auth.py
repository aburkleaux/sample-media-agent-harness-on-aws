"""Unit tests for the extracted gateway auth-header resolver (Wave 2)."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.auth import GatewayAuthConfig, build_auth_headers  # noqa: E402

_GRANT_HEADER = "X-Delegation-Grant"


def _cfg(**kw) -> GatewayAuthConfig:
    base = dict(auth_type="none", url="https://gw.example/mcp", gateway_name="gw")
    base.update(kw)
    return GatewayAuthConfig(**base)


def _oauth(token="m2m-tok"):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return token

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


def test_none_auth_has_no_headers():
    assert build_auth_headers(_cfg(auth_type="none"), oauth_token_fn=_oauth(), delegation_grant_header=_GRANT_HEADER) == {}


def test_bearer_uses_prefix_and_header_name():
    h = build_auth_headers(
        _cfg(auth_type="bearer", bearer_token="pat", header_name="X-API-Key", value_prefix=""),
        oauth_token_fn=_oauth(), delegation_grant_header=_GRANT_HEADER,
    )
    assert h == {"X-API-Key": "pat"}


def test_bearer_without_token_fails_loudly():
    with pytest.raises(RuntimeError, match="no token was resolved"):
        build_auth_headers(_cfg(auth_type="bearer"), oauth_token_fn=_oauth(), delegation_grant_header=_GRANT_HEADER)


def test_user_jwt_interactive_prefers_user_token():
    fn = _oauth()
    h = build_auth_headers(_cfg(auth_type="user_jwt", user_token="utok"), oauth_token_fn=fn, delegation_grant_header=_GRANT_HEADER)
    assert h == {"Authorization": "Bearer utok"}
    assert fn.calls["n"] == 0, "interactive path must not mint a machine token"


def test_user_jwt_delegated_mints_machine_token_and_grant():
    fn = _oauth("m2m")
    h = build_auth_headers(
        _cfg(auth_type="user_jwt", delegation_grant="GRANT", client_id="c", client_secret="s", token_endpoint="https://t"),
        oauth_token_fn=fn, delegation_grant_header=_GRANT_HEADER,
    )
    assert h == {"Authorization": "Bearer m2m", _GRANT_HEADER: "GRANT"}
    assert fn.calls["n"] == 1


def test_user_jwt_without_token_or_grant_fails_loudly():
    with pytest.raises(RuntimeError, match="no user token was forwarded"):
        build_auth_headers(_cfg(auth_type="user_jwt"), oauth_token_fn=_oauth(), delegation_grant_header=_GRANT_HEADER)


def test_default_oauth_path_mints_bearer():
    fn = _oauth("jwt-tok")
    h = build_auth_headers(_cfg(auth_type="jwt"), oauth_token_fn=fn, delegation_grant_header=_GRANT_HEADER)
    assert h == {"Authorization": "Bearer jwt-tok"}
    assert fn.calls["n"] == 1
