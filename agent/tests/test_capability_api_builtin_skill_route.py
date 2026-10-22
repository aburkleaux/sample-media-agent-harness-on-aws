"""``GET /control-plane/builtin-skills/{slug}`` — a built-in skill's package.

Exists so opening a built-in skill does not invoke the agent runtime. It used to call
`get_skill` there, which meant every open paid an AgentCore cold start — and unlike the
skills list that call had no client cache, so the second open was as slow as the first.

The route is thin, so what these tests are really about is the three answers it has to
keep apart. Collapsing any two of them turns a diagnosable state into a mystery:

* **200** — the package, as the detail view renders it.
* **404** — no such skill. A typo, not an outage.
* **503** — the catalog itself is unreadable or was never deployed. The harness is
  incomplete, and that is not the caller's fault.

`SourceUnavailable` is the trap: it is not mapped by `_domain_errors` (which covers the
canonical *write* layer), and the read routes never surface it because `aggregate` folds
it into `partialErrors`. Unmapped it would leave here as a bare 500.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from capability.config import CapabilityConfig  # noqa: E402
from capability_api.app import Deps, create_app  # noqa: E402
from capability_api.auth import AuthError  # noqa: E402

AUTH = {"Authorization": "Bearer good-token"}


class FakeVerifier:
    configured = True

    def verify(self, token: Optional[str]) -> dict[str, Any]:
        if token != "good-token":
            raise AuthError("invalid_token", "nope")
        return {"sub": "u-jane", "token_use": "access"}


class FakeTable:
    def __init__(self, items: list[dict], *, raises: bool = False) -> None:
        self.items = items
        self.raises = raises

    def get_item(self, **kwargs):
        if self.raises:
            raise RuntimeError("dynamodb is unhappy")
        sk = kwargs["Key"]["SK"]
        for item in self.items:
            if item["SK"] == sk:
                return {"Item": item}
        return {}


class FakeDeps(Deps):
    def __init__(self, config: CapabilityConfig, table: Any) -> None:
        self.config = config
        self.verifier = FakeVerifier()
        self._asset_table = object()
        self._gateway_table = table

    @property
    def gateway_table(self) -> Any:
        return self._gateway_table


def _config() -> CapabilityConfig:
    return CapabilityConfig(
        deploy_env="sandbox",
        region="us-west-2",
        asset_index_table="assets",
        gateway_registry_table="gateways",
        artifacts_bucket="artifacts-bucket",
        modes={"skill": "canonical"},
    )


def _client(table: Any) -> TestClient:
    """`table` may be a list of items (wrapped), a FakeTable, or None (unconfigured)."""
    if isinstance(table, list):
        table = FakeTable(table)
    return TestClient(create_app(_config(), FakeDeps(_config(), table)))


def _item(slug: str) -> dict:
    return {
        "PK": "GATEWAY#CATALOG",
        "SK": f"__skill__{slug}",
        "name": slug,
        "description": "Does things.",
        "skillMd": f"---\nname: {slug}\n---\n# {slug}\n\nBody.\n",
        "supportingFiles": ["ref/notes.md"],
    }


def test_serves_the_package_the_detail_view_renders() -> None:
    res = _client([_item("pdf")]).get("/control-plane/builtin-skills/pdf", headers=AUTH)

    assert res.status_code == 200
    body = res.json()
    # Preview parses `skillMd`, Source shows it raw, and Package counts
    # `supportingFiles.length + 1`. Anything missing here renders as an empty tab.
    assert body["name"] == "pdf"
    assert body["skillMd"].startswith("---")
    assert body["supportingFiles"] == ["ref/notes.md"]


def test_an_unknown_skill_is_404_not_503() -> None:
    res = _client([_item("pdf")]).get("/control-plane/builtin-skills/ghost", headers=AUTH)

    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "unknown_skill"


def test_an_unreadable_catalog_is_503_not_500() -> None:
    """"The catalog is not deployed" must not read as "the service broke"."""
    res = _client(FakeTable([_item("pdf")], raises=True)).get(
        "/control-plane/builtin-skills/pdf", headers=AUTH
    )

    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "builtin_catalog_unavailable"


def test_an_unconfigured_table_is_503_with_a_distinct_reason() -> None:
    res = _client(None).get("/control-plane/builtin-skills/pdf", headers=AUTH)

    assert res.status_code == 503
    assert res.json()["detail"]["reason"] == "config"


def test_requires_a_token() -> None:
    res = _client([_item("pdf")]).get(
        "/control-plane/builtin-skills/pdf", headers={"Authorization": "Bearer nope"}
    )

    assert res.status_code == 401
