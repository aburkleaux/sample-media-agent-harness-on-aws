"""Capability API HTTP surface (migration Tasks 22-23).

What matters at this layer:

* identity comes from the token and nowhere else — a body-supplied actor is
  ignored (R5),
* an unauthenticated request gets 401 with a stable code and no detail,
* `/health` and `/control-plane/config` are reachable without a token but leak
  no identifiers,
* one bootstrap request covers a tab, and only the categories asked for,
* an id the caller cannot see is simply absent — not-found-or-forbidden (R5).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from capability.config import CapabilityConfig  # noqa: E402
from capability.dto import CapabilitySummary, CategoryPage  # noqa: E402
from capability_api.app import CORRELATION_HEADER, Deps, create_app  # noqa: E402
from capability_api.auth import (  # noqa: E402
    AuthError,
    bearer_token,
    normalize_groups,
    org_id_from_claims,
)


class FakeVerifier:
    """Stands in for Cognito verification with a fixed claim set."""

    configured = True

    def __init__(self, claims: dict[str, Any] | None = None) -> None:
        self._claims = claims or {
            "sub": "u-jane",
            "cognito:groups": ["g-1"],
            "custom:org_id": "o-acme",
            "token_use": "access",
        }

    def verify(self, token: str | None) -> dict[str, Any]:
        if token != "good-token":
            raise AuthError("invalid_token", "nope")
        return dict(self._claims)


class FakeDeps(Deps):
    def __init__(self, config: CapabilityConfig, verifier: Any) -> None:
        self.config = config
        self.verifier = verifier
        self._asset_table = object()
        self._gateway_table = object()

    @property
    def asset_index(self) -> Any:
        return object()

    @property
    def gateway_table(self) -> Any:
        return object()


def _config(**overrides: Any) -> CapabilityConfig:
    base = {
        "deploy_env": "sandbox",
        "region": "us-west-2",
        "asset_index_table": "assets",
        "registry_id": "reg-secret-1",
        "registry_name": "mediaworks-harness-capabilities-sandbox",
        "modes": {"skill": "registry", "tool": "legacy"},
    }
    base.update(overrides)
    return CapabilityConfig(**base)


def _summary(asset_id: str = "asset-1", **overrides: Any) -> CapabilitySummary:
    base = {
        "id": f"skill:{asset_id}:v1",
        "category": "skill",
        "capability_class": "asset",
        "display_name": f"Skill {asset_id}",
        "scope": "org",
        "status": "published",
        "source": "asset-index",
        "asset_id": asset_id,
        "version": 1,
    }
    base.update(overrides)
    return CapabilitySummary(**base)


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """A client whose aggregation layer is replaced by a recording stub."""
    calls: list[tuple[str, ...]] = []

    async def fake_bootstrap(categories, **_kwargs):
        calls.append(tuple(categories))
        return {
            category: CategoryPage(
                category=category,
                items=(_summary(), _summary("asset-2")) if category == "skill" else (),
                sources_queried=("asset-index",),
            )
            for category in categories
        }

    async def fake_load_category(category, **_kwargs):
        calls.append((category,))
        return CategoryPage(
            category=category, items=(_summary(),), sources_queried=("asset-index",)
        )

    monkeypatch.setattr("capability_api.app.bootstrap", fake_bootstrap)
    monkeypatch.setattr("capability_api.app.load_category", fake_load_category)

    app = create_app(_config(), FakeDeps(_config(), FakeVerifier()))
    test_client = TestClient(app)
    test_client.calls = calls  # type: ignore[attr-defined]
    return test_client


AUTH = {"Authorization": "Bearer good-token"}


# ---------------------------------------------------------------------------
# Unauthenticated surface
# ---------------------------------------------------------------------------


def test_health_needs_no_token_and_leaks_no_identifiers(client):
    """The ALB hits this; an unauthenticated probe must learn nothing useful."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["deployEnv"] == "sandbox"
    assert "reg-secret-1" not in response.text
    assert "assets" not in response.text


def test_public_config_exposes_flags_only(client):
    response = client.get("/control-plane/config")
    assert response.status_code == 200
    body = response.json()
    assert body["capabilitySources"]["skill"] == "registry"
    assert body["registryConfigured"] is True
    assert "reg-secret-1" not in response.text


def test_public_config_and_health_say_which_release_runs(client, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "0.9.0-rc.870+abc1234")
    assert client.get("/control-plane/config").json()["appVersion"] == "0.9.0-rc.870+abc1234"
    assert client.get("/health").json()["appVersion"] == "0.9.0-rc.870+abc1234"
    monkeypatch.delenv("APP_VERSION")
    assert client.get("/control-plane/config").json()["appVersion"] == "dev"


# ---------------------------------------------------------------------------
# Authentication (R5)
# ---------------------------------------------------------------------------


def test_missing_token_is_401_with_a_challenge(client):
    response = client.post("/control-plane/capabilities/bootstrap", json={"categories": ["skill"]})
    assert response.status_code == 401
    assert "Bearer" in response.headers.get("www-authenticate", "")


def test_bad_token_is_401_and_does_not_echo_the_reason(client):
    response = client.post(
        "/control-plane/capabilities/bootstrap",
        json={"categories": ["skill"]},
        headers={"Authorization": "Bearer nope"},
    )
    assert response.status_code == 401
    # The stable code is fine to return; the verifier's internal detail is not.
    assert "nope" not in response.text.replace("invalid_token", "")


def test_body_supplied_actor_is_ignored(client, monkeypatch):
    """A caller must not be able to widen their visibility by asserting identity."""
    seen: dict[str, Any] = {}

    async def capture(categories, **kwargs):
        seen["actor"] = kwargs["actor"]
        return {c: CategoryPage(category=c) for c in categories}

    monkeypatch.setattr("capability_api.app.bootstrap", capture)
    response = client.post(
        "/control-plane/capabilities/bootstrap",
        json={
            "categories": ["skill"],
            "actorId": "u-someone-else",
            "orgId": "o-other",
            "groupIds": ["g-admin"],
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    assert seen["actor"].actor_id == "u-jane"
    assert seen["actor"].org_id == "o-acme"
    assert seen["actor"].group_ids == ["g-1"]


# ---------------------------------------------------------------------------
# Bootstrap (R10 / R13)
# ---------------------------------------------------------------------------


def test_bootstrap_loads_only_the_requested_categories(client):
    response = client.post(
        "/control-plane/capabilities/bootstrap",
        json={"categories": ["skill"]},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert list(response.json()["categories"]) == ["skill"]
    assert client.calls == [("skill",)]


def test_bootstrap_ignores_unknown_categories_rather_than_failing(client):
    """An older SPA asking for a category this build lacks should still work."""
    response = client.post(
        "/control-plane/capabilities/bootstrap",
        json={"categories": ["skill", "not-a-category"]},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert list(response.json()["categories"]) == ["skill"]


def test_response_is_camel_case_with_empty_fields_dropped(client):
    response = client.post(
        "/control-plane/capabilities/bootstrap",
        json={"categories": ["skill"]},
        headers=AUTH,
    )
    item = response.json()["categories"]["skill"]["items"][0]
    assert item["displayName"] == "Skill asset-1"
    assert item["capabilityClass"] == "asset"
    assert "gatewayId" not in item
    assert "targetName" not in item


def test_every_response_carries_a_correlation_id(client):
    supplied = client.get(
        "/control-plane/capabilities/skill",
        headers={**AUTH, CORRELATION_HEADER: "trace-42"},
    )
    assert supplied.headers[CORRELATION_HEADER] == "trace-42"
    generated = client.get("/control-plane/capabilities/skill", headers=AUTH)
    assert generated.headers[CORRELATION_HEADER]


# ---------------------------------------------------------------------------
# Category / search / batch-get
# ---------------------------------------------------------------------------


def test_unknown_category_is_404(client):
    response = client.get("/control-plane/capabilities/nonsense", headers=AUTH)
    assert response.status_code == 404


def test_limit_is_clamped(client, monkeypatch):
    """One caller must not be able to blow the p95 budget for the task."""
    seen: dict[str, Any] = {}

    async def capture(category, **kwargs):
        seen["limit"] = kwargs["limit"]
        return CategoryPage(category=category)

    monkeypatch.setattr("capability_api.app.load_category", capture)
    client.get("/control-plane/capabilities/skill?limit=99999", headers=AUTH)
    assert seen["limit"] == 200
    client.get("/control-plane/capabilities/skill?limit=-4", headers=AUTH)
    assert seen["limit"] == 1


def test_search_filters_by_substring(client):
    response = client.post(
        "/control-plane/capabilities/search",
        json={"query": "asset-2", "categories": ["skill"]},
        headers=AUTH,
    )
    assert [item["assetId"] for item in response.json()["items"]] == ["asset-2"]


def test_search_with_an_empty_query_returns_everything_in_scope(client):
    response = client.post(
        "/control-plane/capabilities/search",
        json={"query": "", "categories": ["skill"]},
        headers=AUTH,
    )
    assert len(response.json()["items"]) == 2


def test_batch_get_omits_ids_the_caller_cannot_see(client):
    """Not-found-or-forbidden: no separate 'denied' list to disclose existence."""
    response = client.post(
        "/control-plane/capabilities/batch-get",
        json={"ids": ["skill:asset-1:v1", "skill:someone-elses:v1"], "categories": ["skill"]},
        headers=AUTH,
    )
    body = response.json()
    assert [item["id"] for item in body["items"]] == ["skill:asset-1:v1"]
    assert "errors" not in body
    assert "denied" not in body


def test_batch_get_with_no_ids_short_circuits(client):
    response = client.post(
        "/control-plane/capabilities/batch-get", json={"ids": []}, headers=AUTH
    )
    assert response.json() == {"items": []}
    assert client.calls == []


def test_partial_errors_are_surfaced_to_the_caller(client, monkeypatch):
    from capability.dto import SourceError

    async def degraded(categories, **_kwargs):
        return {
            "skill": CategoryPage(
                category="skill",
                items=(_summary(),),
                partial_errors=(SourceError(source="agent-registry", error="timeout"),),
            )
        }

    monkeypatch.setattr("capability_api.app.bootstrap", degraded)
    response = client.post(
        "/control-plane/capabilities/bootstrap",
        json={"categories": ["skill"]},
        headers=AUTH,
    )
    page = response.json()["categories"]["skill"]
    assert page["partialErrors"] == [{"source": "agent-registry", "error": "timeout"}]


# ---------------------------------------------------------------------------
# Token parsing helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("  Bearer   abc  ", "abc"),
        ("Basic abc", None),
        ("abc", None),
        ("Bearer", None),
        ("", None),
        (None, None),
    ],
)
def test_bearer_token_parsing(header, expected):
    assert bearer_token(header) == expected


def test_groups_claim_normalizes_both_shapes():
    """Cognito emits an array; some upstreams send a comma-separated string."""
    assert normalize_groups(["a", "b"]) == ["a", "b"]
    assert normalize_groups("a, b") == ["a", "b"]
    assert normalize_groups("[a, b]") == ["a", "b"]
    assert normalize_groups(None) == []
    assert normalize_groups("") == []


def test_org_claim_is_read_from_the_token_and_fails_closed():
    """Guessing an org id would grant access to another org's shared assets."""
    assert org_id_from_claims({"custom:org_id": "o-1"}) == "o-1"
    assert org_id_from_claims({"custom:orgId": "o-2"}) == "o-2"
    assert org_id_from_claims({"orgId": "o-3"}) is None
    assert org_id_from_claims({}) is None


def test_verifier_ignores_dev_bypass_when_a_real_pool_is_configured():
    """A deployed task must not be talked into bypass mode by a stray env var."""
    from capability_api.auth import TokenVerifier

    verifier = TokenVerifier.from_env(
        {
            "DEV_BYPASS_AUTH": "true",
            "USER_POOL_ISSUER": "https://cognito-idp.us-west-2.amazonaws.com/pool",
            "USER_POOL_CLIENT_ID": "client",
        }
    )
    with pytest.raises(AuthError):
        verifier.verify(None)


def test_verifier_without_a_pool_or_bypass_reports_misconfiguration():
    from capability_api.auth import TokenVerifier

    verifier = TokenVerifier.from_env({})
    assert verifier.configured is False
    with pytest.raises(AuthError) as excinfo:
        verifier.verify("anything")
    assert excinfo.value.code == "auth_misconfigured"
