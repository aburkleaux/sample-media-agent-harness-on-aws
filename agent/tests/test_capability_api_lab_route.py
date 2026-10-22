"""``POST /control-plane/lab`` — serving lab actions without waking the runtime.

The route is thin on purpose: it mounts the same ``LabService`` dispatcher the
runtime mounts, so there is no response-shaping logic here to test. What *is*
worth testing is everything around the dispatch, because each of these is a way an
HTTP layer can turn a correct domain call into a wrong answer:

* **Host gating.** A data-plane action must be refused *before* anything is
  constructed. This process has no model, no MCP session and no
  ``InvokeAgentRuntime`` permission, so attempting one cannot fail usefully.
* **Identity.** The actor comes from the verified token; a body-supplied actor is
  stripped, exactly as the runtime strips it.
* **Error parity.** The runtime returns failures as HTTP 200 with an ``error``
  field, and some *successes* are shaped that way too. If this route mapped
  exceptions onto status codes, the same request would behave differently
  depending on which host served it — the one thing sharing a dispatcher is meant
  to prevent.
* **Result passthrough.** ``List*`` handlers return bare arrays, so the route must
  not assume an object.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from capability.config import CapabilityConfig  # noqa: E402
from capability_api.app import (  # noqa: E402
    CORRELATION_HEADER,
    Deps,
    _refusing_gateway_browser,
    create_app,
)
from capability_api.auth import AuthError  # noqa: E402

LAB_PATH = "/control-plane/lab"
AUTH = {"Authorization": "Bearer good-token"}


class FakeVerifier:
    configured = True

    def verify(self, token: Optional[str]) -> dict[str, Any]:
        if token != "good-token":
            raise AuthError("invalid_token", "nope")
        return {
            "sub": "u-jane",
            "cognito:groups": ["g-1"],
            "custom:org_id": "o-acme",
            "token_use": "access",
        }


class FakeDeps(Deps):
    def __init__(self, config: CapabilityConfig, *, bucket: bool = True) -> None:
        self.config = config
        self.verifier = FakeVerifier()
        self._asset_table = object()
        self._gateway_table = object()
        self._bucket = bucket

    @property
    def asset_index(self) -> Any:
        return object()

    def artifacts_tool(self, actor_id: str) -> Any:
        if not self._bucket:
            raise ValueError("ARTIFACTS_BUCKET is not configured")
        return object()


def _config() -> CapabilityConfig:
    return CapabilityConfig(
        deploy_env="sandbox",
        region="us-west-2",
        asset_index_table="assets",
        artifacts_bucket="artifacts-bucket",
        modes={"specialty-agent": "canonical"},
    )


@pytest.fixture
def harness(monkeypatch):
    """A client, plus a record of how the dispatcher was constructed and called."""
    calls: dict[str, Any] = {"built": [], "handled": []}
    behaviour: dict[str, Any] = {"result": {"ok": True}, "raises": None}

    class FakeLabService:
        def __init__(self, *, ctx, s3, index, gateway_browser) -> None:
            calls["built"].append(
                {
                    "actor_id": ctx.actor_id,
                    "group_ids": list(ctx.group_ids),
                    "gateway_browser": gateway_browser,
                }
            )

        async def handle(self, payload: dict[str, Any]) -> Any:
            calls["handled"].append(payload)
            if behaviour["raises"] is not None:
                raise behaviour["raises"]
            return behaviour["result"]

    import lab.service as service_mod

    monkeypatch.setattr(service_mod, "LabService", FakeLabService)

    deps = FakeDeps(_config())
    client = TestClient(create_app(_config(), deps))
    return client, calls, behaviour, deps


# --- host gating -----------------------------------------------------------


def test_a_control_plane_action_dispatches(harness) -> None:
    client, calls, behaviour, _ = harness
    behaviour["result"] = {"assetId": "s-1", "name": "demo"}

    res = client.post(LAB_PATH, json={"action": "ReadSkillDraft", "assetId": "s-1"}, headers=AUTH)

    assert res.status_code == 200
    assert res.json() == {"assetId": "s-1", "name": "demo"}
    assert calls["handled"] == [{"action": "ReadSkillDraft", "assetId": "s-1"}]
    assert res.headers[CORRELATION_HEADER]


def test_a_data_plane_action_is_refused_before_anything_is_built(harness) -> None:
    """Refused early: constructing the service would be pointless work, and
    attempting the action would fail in a way the caller cannot act on."""
    client, calls, _, _ = harness

    res = client.post(LAB_PATH, json={"action": "ChatMessage", "text": "hi"}, headers=AUTH)

    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail["code"] == "action_not_available_here"
    assert detail["host"] == "agent-runtime"
    assert detail["action"] == "ChatMessage"
    assert "model" in detail["message"]
    assert calls["built"] == [] and calls["handled"] == []


def test_the_tool_collection_writes_are_refused_with_the_reason_why(harness) -> None:
    """The subtle pair — refusing them is the point of the gate, not an oversight."""
    client, calls, _, _ = harness

    for action in ("CreateToolCollectionDraft", "UpdateToolCollectionDraft"):
        res = client.post(LAB_PATH, json={"action": action, "name": "c"}, headers=AUTH)
        assert res.status_code == 400
        assert "validat" in res.json()["detail"]["message"].lower()
    assert calls["handled"] == []


def test_an_unknown_action_is_refused_rather_than_dispatched(harness) -> None:
    """Fail closed: not in the allowlist means not served, known or not."""
    client, calls, _, _ = harness

    res = client.post(LAB_PATH, json={"action": "NoSuchAction"}, headers=AUTH)

    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "action_not_available_here"
    assert calls["handled"] == []


def test_a_missing_action_is_a_bad_request(harness) -> None:
    client, calls, _, _ = harness

    res = client.post(LAB_PATH, json={"assetId": "s-1"}, headers=AUTH)

    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "invalid_request"
    assert calls["handled"] == []


# --- identity --------------------------------------------------------------


def test_the_actor_comes_from_the_token_not_the_body(harness) -> None:
    """A body-supplied actor must not reach the CRUD layer (R5 / Property 3)."""
    client, calls, _, _ = harness

    res = client.post(
        LAB_PATH,
        json={
            "action": "ListSkillDrafts",
            "actor_id": "u-attacker",
            "__actor_id": "u-attacker",
            "__actor_groups": ["g-admin"],
        },
        headers=AUTH,
    )

    assert res.status_code == 200
    assert calls["built"][0]["actor_id"] == "u-jane"
    assert calls["built"][0]["group_ids"] == ["g-1"]
    # The smuggled fields are gone before dispatch, not merely ignored downstream.
    assert calls["handled"] == [{"action": "ListSkillDrafts"}]


def test_a_bad_token_is_rejected(harness) -> None:
    client, calls, _, _ = harness

    res = client.post(
        LAB_PATH, json={"action": "ListSkillDrafts"}, headers={"Authorization": "Bearer nope"}
    )

    assert res.status_code == 401
    assert calls["handled"] == []


# --- error parity ----------------------------------------------------------


def test_a_handler_failure_is_a_200_with_an_error_field(harness) -> None:
    """Parity with the runtime, which wraps every exception this way.

    Deliberately not a 500: `EditAsset` reports an existing working copy as a 200
    with an `error` field, so a client cannot treat this envelope as failure-only.
    Mapping to a status code here would split behaviour by host.
    """
    client, _, behaviour, _ = harness
    behaviour["raises"] = ValueError("draft already exists")

    res = client.post(LAB_PATH, json={"action": "EditAsset", "assetId": "a-1"}, headers=AUTH)

    assert res.status_code == 200
    assert res.json() == {"error": "ValueError", "message": "draft already exists"}


def test_a_list_action_returns_a_bare_array(harness) -> None:
    """`List*` handlers return arrays; the route must not wrap them in an object."""
    client, _, behaviour, _ = harness
    behaviour["result"] = [{"assetId": "s-1"}, {"assetId": "s-2"}]

    res = client.post(LAB_PATH, json={"action": "ListSkillDrafts"}, headers=AUTH)

    assert res.status_code == 200
    assert res.json() == [{"assetId": "s-1"}, {"assetId": "s-2"}]


def test_an_unconfigured_bucket_is_a_503_not_a_500(harness, monkeypatch) -> None:
    """The deployment is incomplete, not the request wrong."""
    client, _, _, deps = harness
    monkeypatch.setattr(deps, "_bucket", False)

    res = client.post(LAB_PATH, json={"action": "ListSkillDrafts"}, headers=AUTH)

    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "artifacts_bucket_not_configured"


# --- the gateway browser ---------------------------------------------------


def test_the_injected_gateway_browser_raises_rather_than_reporting_no_tools() -> None:
    """An empty projection would be worse than an error.

    `ToolCollectionCRUD._validate_tool_refs` treats a missing tool as an invalid
    ref, so a browser that answered "no tools" would make an allowlist mistake
    look like the caller's fault — every `gateway___tool` ref rejected, no error
    anywhere. That is the failure that already happened on the runtime's skip path.
    """
    from lab.errors import GatewayUnavailableError

    class Ctx:
        actor_id = "u-jane"
        session_id = "s-1"

    browser = _refusing_gateway_browser()
    with pytest.raises(GatewayUnavailableError):
        browser.browse(Ctx())
