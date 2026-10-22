"""The purge route — the one destructive endpoint on the Capability API.

Everything else here reads. So the tests concentrate on the three ways an HTTP
layer can turn a careful domain operation into an accident:

* **Defaulting.** ``dryRun`` must default to a preview, and only a literal JSON
  ``false`` may destroy anything. A client bug that sends the string ``"false"``
  must preview.
* **Identity.** The actor comes from the token; a body-supplied actor is ignored,
  exactly as on the read routes (R5).
* **Error mapping.** Without an explicit mapping a denial would surface as a bare
  500 and the SPA could not distinguish "you may not" from "the service broke" —
  which for an irreversible operation is the difference between stopping and
  retrying.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import lab._canonical_ops as canonical_ops  # noqa: E402
import lab.purge_scan as purge_scan  # noqa: E402
from capability.config import CapabilityConfig  # noqa: E402
from capability_api.app import CORRELATION_HEADER, Deps, create_app  # noqa: E402
from capability_api.auth import AuthError  # noqa: E402
from lab.errors import AuthorityDenied, ConflictError, NotFoundOrForbidden  # noqa: E402
from lab.operation_results import (  # noqa: E402
    AgentReferenceHit,
    Purged,
    PurgePreflight,
)
from lab.path_validator import PrefixMismatchError  # noqa: E402

PURGE_PATH = "/control-plane/assets/specialty-agent/p-agent-1/purge"
AUTH = {"Authorization": "Bearer good-token"}


class FakeVerifier:
    configured = True

    def verify(self, token: str | None) -> dict[str, Any]:
        if token != "good-token":
            raise AuthError("invalid_token", "nope")
        return {
            "sub": "u-jane",
            "cognito:groups": ["g-1"],
            "custom:org_id": "o-acme",
            "token_use": "access",
        }


class FakeS3:
    """Records that the route bound the tool to the token's actor, nothing more."""

    def __init__(self, actor_id: str) -> None:
        self.actor_id = actor_id


class FakeDeps(Deps):
    def __init__(self, config: CapabilityConfig, *, bucket: bool = True) -> None:
        self.config = config
        self.verifier = FakeVerifier()
        self._asset_table = object()
        self._gateway_table = object()
        self._bucket = bucket
        self.tools: list[FakeS3] = []

    @property
    def asset_index(self) -> Any:
        return object()

    def artifacts_tool(self, actor_id: str) -> Any:
        if not self._bucket:
            raise ValueError("ARTIFACTS_BUCKET is not configured")
        tool = FakeS3(actor_id)
        self.tools.append(tool)
        return tool


def _config() -> CapabilityConfig:
    return CapabilityConfig(
        deploy_env="sandbox",
        region="us-west-2",
        asset_index_table="assets",
        artifacts_bucket="artifacts-bucket",
        modes={"specialty-agent": "legacy"},
    )


def _preflight(**overrides: Any) -> PurgePreflight:
    base: dict[str, Any] = {
        "published_id": "p-agent-1",
        "category": "specialty-agent",
        "versions": (1, 2),
        "scopes": ("org", "private"),
        "working_copy_exists": True,
        "external_access_live": True,
        "prefixes": ("users/u-jane/published/specialty-agents/p-agent-1/v1/",),
        "hits": (
            AgentReferenceHit(
                kind="workflow-step", asset_id="wf-1", name="Ingest",
                status="published", detail="step 'transcribe'",
            ),
        ),
        "partial": ("org-shared workflows (no org id on this request)",),
    }
    base.update(overrides)
    return PurgePreflight(**base)


def _purged() -> Purged:
    return Purged(
        published_id="p-agent-1",
        category="specialty-agent",
        versions_deleted=(2, 1),
        working_copy_deleted=True,
        prefixes_deleted=("users/u-jane/published/specialty-agents/p-agent-1/v2/",),
        prefixes_orphaned=("users/u-jane/published/specialty-agents/p-agent-1/v1/",),
        outbox_events=1,
        preflight=_preflight(),
    )


@pytest.fixture
def harness(monkeypatch):
    """A client plus a record of what the domain layer was asked to do."""
    calls: dict[str, Any] = {"scan": [], "preflight": [], "purge": []}

    def fake_scan(which: str):
        """Records WHICH scan ran, not just that one did.

        The route dispatches on category, and running the wrong scan is silent:
        the workflow scan pointed at a skill simply finds nothing and the dialog
        reports an empty blast radius.
        """

        async def _fake(**kwargs):
            calls["scan"].append({"scan": which, **kwargs})
            return (_preflight().hits, _preflight().partial)

        return _fake

    async def fake_preflight(**kwargs):
        calls["preflight"].append(kwargs)
        return _preflight(hits=kwargs["hits"], partial=kwargs["partial"])

    async def fake_purge(**kwargs):
        calls["purge"].append(kwargs)
        return _purged()

    monkeypatch.setattr(purge_scan, "scan_agent_references", fake_scan("agent"))
    monkeypatch.setattr(purge_scan, "scan_skill_references", fake_scan("skill"))
    monkeypatch.setattr(canonical_ops, "preflight_asset_purge", fake_preflight)
    monkeypatch.setattr(canonical_ops, "purge_asset_lineage", fake_purge)

    deps = FakeDeps(_config())
    client = TestClient(create_app(_config(), deps))
    return client, calls, deps


def _raise_from_domain(monkeypatch, error: BaseException, *, dry: bool = False) -> None:
    async def _boom(**_kwargs):
        raise error

    monkeypatch.setattr(
        canonical_ops, "preflight_asset_purge" if dry else "purge_asset_lineage", _boom
    )


# ---------------------------------------------------------------------------
# dryRun defaulting — the safety property
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"dryRun": True},
        {"dryRun": "false"},   # a string, not a boolean — must NOT destroy
        {"dryRun": 0},
        {"dryRun": None},
        {"dry_run": False},    # snake_case, not the documented key
    ],
    ids=["no-body", "empty", "true", "string-false", "zero", "null", "snake-case"],
)
def test_anything_but_a_literal_false_previews(harness, body):
    client, calls, _ = harness
    response = client.post(PURGE_PATH, json=body, headers=AUTH)

    assert response.status_code == 200
    assert response.json()["dryRun"] is True
    assert calls["purge"] == []
    assert len(calls["preflight"]) == 1


def test_explicit_false_purges(harness):
    client, calls, _ = harness
    response = client.post(PURGE_PATH, json={"dryRun": False}, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["dryRun"] is False
    assert body["versionsDeleted"] == [2, 1]
    assert calls["preflight"] == []
    assert len(calls["purge"]) == 1


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_unauthenticated_purge_is_401_and_reaches_no_domain_code(harness):
    client, calls, _ = harness
    response = client.post(PURGE_PATH, json={"dryRun": False})
    assert response.status_code == 401
    assert calls["purge"] == [] and calls["scan"] == []


def test_actor_comes_from_the_token_not_the_body(harness):
    client, calls, deps = harness
    response = client.post(
        PURGE_PATH,
        json={"dryRun": False, "actorId": "u-someone-else", "orgId": "o-other"},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert calls["purge"][0]["ctx"].actor_id == "u-jane"
    assert calls["scan"][0]["org_id"] == "o-acme"
    # The artifact tool is bound to the token's actor, not the body's.
    assert [t.actor_id for t in deps.tools] == ["u-jane"]


def test_the_path_category_and_id_are_what_the_domain_receives(harness):
    client, calls, _ = harness
    client.post(
        "/control-plane/assets/specialty-agent/p-other/purge",
        json={"dryRun": False},
        headers=AUTH,
    )
    call = calls["purge"][0]
    assert call["category"] == "specialty-agent"
    assert call["published_id"] == "p-other"


# ---------------------------------------------------------------------------
# The scan is dispatched on category
# ---------------------------------------------------------------------------
#
# Getting this wrong is silent and dangerous. An agent is referenced by workflow
# steps, a skill by an agent's compatible-skill list. Running the workflow scan
# for a skill finds nothing and the dialog renders "nothing references this
# skill" over agents that depend on it — immediately before an irreversible
# delete.


@pytest.mark.parametrize(
    ("category", "expected_scan"),
    [("specialty-agent", "agent"), ("skill", "skill")],
)
@pytest.mark.parametrize("dry_run", [True, False])
def test_the_scan_matches_the_category(harness, category, expected_scan, dry_run):
    client, calls, _ = harness

    response = client.post(
        f"/control-plane/assets/{category}/p-thing/purge",
        json={"dryRun": dry_run},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert [c["scan"] for c in calls["scan"]] == [expected_scan]
    assert calls["scan"][0]["published_id"] == "p-thing"


def test_a_category_with_no_scan_is_refused_before_anything_is_read(harness):
    """`scan_references` raises rather than returning no hits.

    Surfaces as a 400. The alternative — an empty tuple — would be presented to
    the user as a verified-empty blast radius that nothing actually checked.
    """
    client, calls, _ = harness

    response = client.post(
        "/control-plane/assets/workflow/p-thing/purge",
        json={"dryRun": True},
        headers=AUTH,
    )

    assert response.status_code == 400
    assert calls["scan"] == []
    assert calls["preflight"] == [] and calls["purge"] == []


# ---------------------------------------------------------------------------
# The reference scan runs on both paths
# ---------------------------------------------------------------------------


def test_the_destructive_path_also_scans_and_records_references(harness):
    """The result carries an audit trail of what the caller was told."""
    client, calls, _ = harness
    response = client.post(PURGE_PATH, json={"dryRun": False}, headers=AUTH)

    assert len(calls["scan"]) == 1
    assert calls["purge"][0]["hits"] == _preflight().hits
    refs = response.json()["preflight"]["references"]
    assert refs == [
        {
            "kind": "workflow-step", "assetId": "wf-1", "name": "Ingest",
            "status": "published", "detail": "step 'transcribe'",
        }
    ]


def test_preflight_response_shape(harness):
    client, _, _ = harness
    body = client.post(PURGE_PATH, json={}, headers=AUTH).json()["preflight"]

    assert body["assetId"] == "p-agent-1"
    assert body["versions"] == [1, 2]
    assert body["scopes"] == ["org", "private"]
    assert body["workingCopyExists"] is True
    assert body["externalAccessLive"] is True
    # Always present, so the SPA cannot forget it and render "no references"
    # over an org tier that was never scanned.
    assert "partial" in body and body["partial"]


def test_orphaned_prefixes_are_reported_to_the_caller(harness):
    """An S3 sweep failure is not an error, but it is not invisible either."""
    client, _, _ = harness
    body = client.post(PURGE_PATH, json={"dryRun": False}, headers=AUTH).json()
    assert body["prefixesOrphaned"] == [
        "users/u-jane/published/specialty-agents/p-agent-1/v1/"
    ]
    assert body["outboxEvents"] == 1


def test_correlation_id_is_echoed(harness):
    client, _, _ = harness
    response = client.post(
        PURGE_PATH, json={}, headers={**AUTH, CORRELATION_HEADER: "corr-1"}
    )
    assert response.headers[CORRELATION_HEADER] == "corr-1"


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def test_not_found_is_404_and_says_nothing_else(harness, monkeypatch):
    """Not-found-or-forbidden: existence must not be disclosed (R5)."""
    client, _, _ = harness
    _raise_from_domain(monkeypatch, NotFoundOrForbidden("no p-agent-1 for u-jane"))
    response = client.post(PURGE_PATH, json={"dryRun": False}, headers=AUTH)

    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "not_found"}
    assert "u-jane" not in response.text


def test_authority_denied_is_403_with_the_reason(harness, monkeypatch):
    client, _, _ = harness
    _raise_from_domain(
        monkeypatch, AuthorityDenied(published_id="p-agent-1", version=1)
    )
    response = client.post(PURGE_PATH, json={"dryRun": False}, headers=AUTH)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "authority_denied"


def test_conflict_is_409_and_names_what_was_already_removed(harness, monkeypatch):
    """A partially applied purge must tell the user a retry can finish it."""
    client, _, _ = harness
    _raise_from_domain(
        monkeypatch, ConflictError("cancelled after removing [3]; retry to finish")
    )
    response = client.post(PURGE_PATH, json={"dryRun": False}, headers=AUTH)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "conflict"
    assert "[3]" in detail["message"]


def test_prefix_mismatch_is_409(harness, monkeypatch):
    client, _, _ = harness
    _raise_from_domain(
        monkeypatch, PrefixMismatchError("stored", "derived")
    )
    response = client.post(PURGE_PATH, json={"dryRun": False}, headers=AUTH)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "prefix_mismatch"


def test_a_non_purgeable_category_is_400(harness, monkeypatch):
    client, _, _ = harness
    _raise_from_domain(monkeypatch, ValueError("purge only supports ['specialty-agent']"))
    response = client.post(
        "/control-plane/assets/skill/p-skill-1/purge",
        json={"dryRun": False},
        headers=AUTH,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_request"


def test_errors_map_on_the_preview_path_too(harness, monkeypatch):
    """The dialog must fail before the user types the confirmation, not after."""
    client, _, _ = harness
    _raise_from_domain(
        monkeypatch, AuthorityDenied(published_id="p-agent-1"), dry=True
    )
    response = client.post(PURGE_PATH, json={}, headers=AUTH)
    assert response.status_code == 403


def test_missing_artifacts_bucket_is_503_not_500(monkeypatch):
    """A deployment gap, not a bad request — and nothing is deleted."""
    called: list[str] = []

    async def _should_not_run(**_kwargs):
        called.append("purge")

    monkeypatch.setattr(canonical_ops, "purge_asset_lineage", _should_not_run)
    client = TestClient(create_app(_config(), FakeDeps(_config(), bucket=False)))

    response = client.post(PURGE_PATH, json={"dryRun": False}, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "artifacts_bucket_not_configured"
    assert called == []
