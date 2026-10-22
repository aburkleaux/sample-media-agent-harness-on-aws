"""Capability API Improve routes (Step 3) + the store/DTO they build on.

What matters at this layer, and what these tests pin:

* the bundle read merges this actor's dismiss overlay on top of the global
  SNAPSHOT, strictly bundle→overlay — a stale overlay id never fabricates a
  suggestion (port of the Step-2 `ui/server` proxy merge),
* the caller's own evidence bundle is attached when it exists and its ABSENCE is
  served as global-only, never as a 404 or a blank surface — that is the ordinary
  state, because the producer is a daily cron over a bounded harvest window,
* identity is the verified token's subject and nowhere else — a body-supplied
  `actorId` is ignored, and the partition query is keyed on the token actor,
* `Decimal` from the boto3 resource layer decodes to JSON int/float,
* absent ≠ empty: no SNAPSHOT is a 404, an unconfigured store is a 503,
* an unauthenticated request is 401.

The `FakeTable` is deliberately **no looser than the real DynamoDB table**: it
asserts the single-table schema (PK `actorId` / SK `sk`) and the exact
`begins_with` query shape on every call, so a param-shape regression fails here
rather than silently in staging (AGENTS.md: a fake that accepts any params hid
three never-worked projector bugs while its tests passed).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Optional

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from capability.config import CapabilityConfig  # noqa: E402
from capability.improve_store import (  # noqa: E402
    ImproveStore,
    ImproveStoreUnavailable,
    SNAPSHOT_SK,
    SUGG_PREFIX,
    SYSTEM_ACTOR,
)
from capability_api.app import Deps, create_app  # noqa: E402
from capability_api.auth import AuthError  # noqa: E402
from capability_api.improve_dto import (  # noqa: E402
    decode_decimals, merge_overlay, with_actor_bundle,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeVerifier:
    """Fixed claim set; only ``good-token`` verifies (sub ``u-jane``)."""

    configured = True

    def __init__(self, claims: Optional[dict[str, Any]] = None) -> None:
        self._claims = claims or {
            "sub": "u-jane",
            "cognito:groups": ["g-1"],
            "custom:org_id": "o-acme",
            "token_use": "access",
        }

    def verify(self, token: Optional[str]) -> dict[str, Any]:
        if token != "good-token":
            raise AuthError("invalid_token", "nope")
        return dict(self._claims)


class FakeTable:
    """In-memory DynamoDB resource ``Table``, no looser than the real one."""

    def __init__(self, items: Optional[list[dict[str, Any]]] = None) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items or []:
            self._items[(item["actorId"], item["sk"])] = dict(item)
        self.put_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def get_item(self, Key: Optional[dict[str, Any]] = None, **kw: Any) -> dict[str, Any]:
        assert Key is not None and set(Key) == {"actorId", "sk"}, Key
        assert not kw, f"unexpected get_item kwargs: {kw}"
        found = self._items.get((Key["actorId"], Key["sk"]))
        return {"Item": dict(found)} if found is not None else {}

    def query(
        self,
        KeyConditionExpression: Optional[str] = None,
        ExpressionAttributeValues: Optional[dict[str, Any]] = None,
        ExclusiveStartKey: Optional[dict[str, Any]] = None,
        **kw: Any,
    ) -> dict[str, Any]:
        # The whole partition, no sort-key clause: the actor's own bundle and their overlay rows
        # come back in one query and the reader classifies them by `sk`. Asserted literally, so a
        # widened or narrowed key condition has to be a deliberate edit here.
        assert KeyConditionExpression == "actorId = :a", KeyConditionExpression
        assert ExpressionAttributeValues is not None
        assert set(ExpressionAttributeValues) == {":a"}, ExpressionAttributeValues
        assert not kw, f"unexpected query kwargs: {kw}"
        actor = ExpressionAttributeValues[":a"]
        items = [dict(v) for (a, _sk), v in self._items.items() if a == actor]
        return {"Items": items}

    def put_item(self, Item: Optional[dict[str, Any]] = None, **kw: Any) -> None:
        assert Item is not None and "actorId" in Item and "sk" in Item, Item
        assert not kw, f"unexpected put_item kwargs: {kw}"
        self.put_calls.append(dict(Item))
        self._items[(Item["actorId"], Item["sk"])] = dict(Item)

    def delete_item(self, Key: Optional[dict[str, Any]] = None, **kw: Any) -> None:
        assert Key is not None and set(Key) == {"actorId", "sk"}, Key
        assert not kw, f"unexpected delete_item kwargs: {kw}"
        self.delete_calls.append(dict(Key))
        self._items.pop((Key["actorId"], Key["sk"]), None)


class FakeDeps(Deps):
    """Deps whose only real behaviour is the (real) ``improve_store`` property
    over an injected ``improve_table``. Everything else is stubbed."""

    def __init__(self, config: CapabilityConfig, verifier: Any, table: Any) -> None:
        self.config = config
        self.verifier = verifier
        self._asset_table = object()
        self._gateway_table = object()
        self._improve_table = table

    @property
    def improve_table(self) -> Any:  # type: ignore[override]
        return self._improve_table


def _config(**overrides: Any) -> CapabilityConfig:
    base: dict[str, Any] = {
        "deploy_env": "sandbox",
        "region": "us-west-2",
        "asset_index_table": "assets",
        "improve_table": "mediaworks-harness-improve",
        "modes": {},
    }
    base.update(overrides)
    return CapabilityConfig(**base)


def _snapshot(*suggestions: dict[str, Any], **extra: Any) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "suggestions": list(suggestions),
        "components": {"yours": [], "admin": {}, "locked": {}},
        "patterns": {
            "admitted": [],
            "residue": [],
            "environment": [],
            "evidenceGaps": [],
        },
    }
    bundle.update(extra)
    return {
        "actorId": SYSTEM_ACTOR,
        "sk": SNAPSHOT_SK,
        "bundle": bundle,
        "createdAt": "2026-08-25T00:00:00Z",
    }


def _actor_snapshot(actor_id: str, **bundle: Any) -> dict[str, Any]:
    """One actor's own evidence item — same sort key as the global one, their own partition."""
    inner: dict[str, Any] = {
        "runs": [{"runId": "r1", "verdict": {"overall": "FAIL", "reason": "gating: x:FAIL"}}],
        "runsTotal": 1,
        "evidence": {},
        "waste": {"runsWithWaste": 0, "runs": 1, "findings": []},
        "patterns": {"admitted": [], "residue": [], "evidenceGaps": [], "environment": [],
                     "unattributed": []},
    }
    inner.update(bundle)
    return {
        "actorId": actor_id,
        "sk": SNAPSHOT_SK,
        "bundle": inner,
        "createdAt": "2026-08-25T00:00:00Z",
    }


def _overlay_row(actor_id: str, suggestion_id: str, **extra: Any) -> dict[str, Any]:
    row = {
        "actorId": actor_id,
        "sk": f"{SUGG_PREFIX}{suggestion_id}",
        "status": "dismissed",
        "dismissedAt": "2026-08-25T01:00:00Z",
    }
    row.update(extra)
    return row


def _client(table: Any, *, verifier: Optional[FakeVerifier] = None) -> TestClient:
    config = _config(improve_table=None if table is None else "mediaworks-harness-improve")
    deps = FakeDeps(config, verifier or FakeVerifier(), table)
    return TestClient(create_app(config, deps))


AUTH = {"Authorization": "Bearer good-token"}


def resp_text(body: Any) -> str:
    """The whole served payload as one string, for probing that a value is nowhere in it."""
    return json.dumps(body)


# ---------------------------------------------------------------------------
# GET /control-plane/improve/data
# ---------------------------------------------------------------------------


def test_data_merges_overlay_and_decodes_decimals():
    table = FakeTable(
        [
            _snapshot(
                {"suggestionId": "s1", "status": "open", "score": Decimal("0.400"),
                 "baseVersion": Decimal("4")},
                {"suggestionId": "s2", "status": "open"},
            ),
            _overlay_row("u-jane", "s1"),
        ]
    )
    resp = _client(table).get("/control-plane/improve/data", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    by_id = {s["suggestionId"]: s for s in body["suggestions"]}
    assert by_id["s1"]["status"] == "dismissed"   # overlay applied
    assert by_id["s2"]["status"] == "open"          # no overlay, untouched
    # Decimal decoded: an integral value stays int, a fractional one becomes float.
    assert by_id["s1"]["baseVersion"] == 4 and isinstance(by_id["s1"]["baseVersion"], int)
    assert by_id["s1"]["score"] == pytest.approx(0.4) and isinstance(by_id["s1"]["score"], float)
    # The rest of the bundle rides through unchanged.
    assert set(body) >= {"suggestions", "components", "patterns"}


def test_data_never_fabricates_a_suggestion_from_an_overlay_only_id():
    table = FakeTable(
        [
            _snapshot({"suggestionId": "s1", "status": "open"}),
            _overlay_row("u-jane", "ghost"),  # no matching suggestion in the bundle
        ]
    )
    body = _client(table).get("/control-plane/improve/data", headers=AUTH).json()
    ids = [s["suggestionId"] for s in body["suggestions"]]
    assert ids == ["s1"]  # the stale overlay id matched nothing and was ignored


def test_data_overlay_is_scoped_to_the_token_actor_not_a_stranger():
    table = FakeTable(
        [
            _snapshot({"suggestionId": "s1", "status": "open"}),
            _overlay_row("u-someone-else", "s1"),  # a different actor dismissed it
        ]
    )
    body = _client(table).get("/control-plane/improve/data", headers=AUTH).json()
    # u-jane has no overlay of their own, so the row stays open.
    assert body["suggestions"][0]["status"] == "open"


def test_data_404_when_no_snapshot():
    resp = _client(FakeTable([])).get("/control-plane/improve/data", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


def test_data_404_when_snapshot_row_has_no_bundle():
    table = FakeTable([{"actorId": SYSTEM_ACTOR, "sk": SNAPSHOT_SK, "createdAt": "x"}])
    resp = _client(table).get("/control-plane/improve/data", headers=AUTH)
    assert resp.status_code == 404


def test_data_serves_global_only_when_this_actor_has_no_bundle():
    """The NORMAL state, not an edge case — so it is pinned as one.

    The producer is a daily cron over a bounded harvest window, so between any deploy and the next
    run nobody has a bundle, and an actor with no conversational session never gets one at all. The
    surface must serve the shared answer and say nothing about the reader's own runs, rather than
    404 or blank. `actor` is ABSENT, not `{}` — an empty object would read as "measured, and you
    have nothing".
    """
    table = FakeTable([_snapshot({"suggestionId": "s1", "status": "open"})])
    resp = _client(table).get("/control-plane/improve/data", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"suggestions", "components", "patterns"}
    assert "actor" not in body


def test_data_attaches_this_actors_own_bundle_when_one_exists():
    table = FakeTable([
        _snapshot({"suggestionId": "s1", "status": "open"}),
        _actor_snapshot("u-jane", evidence={"s1": {"rootCauseHypo": "sg2 finished before sg1",
                                                   "memberRunIds": ["r1"],
                                                   "shapeFacets": ["fanout_parent_ran_early"]}}),
    ])
    body = _client(table).get("/control-plane/improve/data", headers=AUTH).json()
    assert body["actor"]["evidence"]["s1"]["rootCauseHypo"] == "sg2 finished before sg1"
    # The item's stamp is lifted onto the served object: an actor's evidence only refreshes when
    # they had a session in the window, so it can legitimately be older than the global bundle.
    assert body["actor"]["createdAt"] == "2026-08-25T00:00:00Z"
    # The global half is untouched by the attach.
    assert body["suggestions"][0]["suggestionId"] == "s1"


def test_data_never_serves_another_actors_bundle():
    """The key IS the isolation boundary: this is the one item carrying a tenant's own run ids and
    the judge's prose about their session, and it is reachable only from their partition."""
    table = FakeTable([
        _snapshot({"suggestionId": "s1", "status": "open"}),
        _actor_snapshot("u-someone-else", evidence={"s1": {"rootCauseHypo": "not yours"}}),
    ])
    body = _client(table).get("/control-plane/improve/data", headers=AUTH).json()
    assert "actor" not in body
    assert "not yours" not in resp_text(body)


def test_data_ignores_an_actor_row_that_carries_no_bundle():
    """A malformed or pre-bundle row degrades to global-only rather than attaching a shell the UI
    would render as an empty evidence set."""
    table = FakeTable([
        _snapshot({"suggestionId": "s1", "status": "open"}),
        {"actorId": "u-jane", "sk": SNAPSHOT_SK, "createdAt": "2026-08-25T00:00:00Z"},
    ])
    body = _client(table).get("/control-plane/improve/data", headers=AUTH).json()
    assert "actor" not in body


def test_data_401_without_a_token():
    table = FakeTable([_snapshot({"suggestionId": "s1", "status": "open"})])
    resp = _client(table).get("/control-plane/improve/data")
    assert resp.status_code == 401
    assert "Bearer" in resp.headers.get("www-authenticate", "")


def test_data_503_when_store_not_configured():
    resp = _client(None).get("/control-plane/improve/data", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "improve_store_not_configured"


def test_data_500_on_store_failure_is_not_a_silent_empty():
    class Boom(FakeTable):
        def get_item(self, **kw):  # type: ignore[override]
            raise RuntimeError("ddb down")

    client = TestClient(
        create_app(
            _config(),
            FakeDeps(_config(), FakeVerifier(), Boom([])),
        ),
        raise_server_exceptions=False,
    )
    resp = client.get("/control-plane/improve/data", headers=AUTH)
    assert resp.status_code == 500  # a read failure is an error, never {} served as data


# ---------------------------------------------------------------------------
# POST / DELETE /control-plane/improve/suggestions/{id}/dismiss
# ---------------------------------------------------------------------------


def test_dismiss_writes_overlay_keyed_on_the_token_actor():
    table = FakeTable([])
    resp = _client(table).post(
        "/control-plane/improve/suggestions/s1/dismiss",
        json={"reason": "risky", "note": "revisit later"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "suggestionId": "s1", "status": "dismissed"}
    assert len(table.put_calls) == 1
    row = table.put_calls[0]
    assert row["actorId"] == "u-jane"           # from the token
    assert row["sk"] == f"{SUGG_PREFIX}s1"
    assert row["status"] == "dismissed"
    assert row["reason"] == "risky" and row["note"] == "revisit later"
    assert row["dismissedAt"].endswith("Z")


def test_dismiss_omits_absent_or_blank_reason_and_note():
    table = FakeTable([])
    # A blank reason must not masquerade as a reasoned label (absent ≠ empty).
    _client(table).post(
        "/control-plane/improve/suggestions/s1/dismiss",
        json={"reason": ""},
        headers=AUTH,
    )
    row = table.put_calls[0]
    assert "reason" not in row and "note" not in row


def test_dismiss_ignores_a_body_supplied_actor():
    table = FakeTable([])
    _client(table).post(
        "/control-plane/improve/suggestions/s1/dismiss",
        json={"actorId": "u-evil", "orgId": "o-evil"},
        headers=AUTH,
    )
    assert table.put_calls[0]["actorId"] == "u-jane"  # never the body


def test_dismiss_401_without_a_token():
    table = FakeTable([])
    resp = _client(table).post(
        "/control-plane/improve/suggestions/s1/dismiss", json={}
    )
    assert resp.status_code == 401
    assert table.put_calls == []


def test_dismiss_503_when_store_not_configured():
    resp = _client(None).post(
        "/control-plane/improve/suggestions/s1/dismiss", json={}, headers=AUTH
    )
    assert resp.status_code == 503


def test_undo_deletes_the_overlay_row():
    table = FakeTable([_overlay_row("u-jane", "s1")])
    resp = _client(table).delete(
        "/control-plane/improve/suggestions/s1/dismiss", headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "suggestionId": "s1", "status": "open"}
    assert table.delete_calls == [{"actorId": "u-jane", "sk": f"{SUGG_PREFIX}s1"}]


def test_undo_401_without_a_token():
    table = FakeTable([_overlay_row("u-jane", "s1")])
    resp = _client(table).delete("/control-plane/improve/suggestions/s1/dismiss")
    assert resp.status_code == 401
    assert table.delete_calls == []


def test_undo_503_when_store_not_configured():
    resp = _client(None).delete(
        "/control-plane/improve/suggestions/s1/dismiss", headers=AUTH
    )
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# improve_dto — pure helpers
# ---------------------------------------------------------------------------


def test_merge_overlay_flips_only_matching_dismissed_ids():
    bundle = {"suggestions": [{"suggestionId": "a", "status": "open"},
                              {"suggestionId": "b", "status": "open"}]}
    merged = merge_overlay(bundle, {"a": {"status": "dismissed"}})
    statuses = {s["suggestionId"]: s["status"] for s in merged["suggestions"]}
    assert statuses == {"a": "dismissed", "b": "open"}
    # The input bundle is not mutated in place.
    assert bundle["suggestions"][0]["status"] == "open"


def test_merge_overlay_ignores_non_dismissed_overlay_status():
    bundle = {"suggestions": [{"suggestionId": "a", "status": "open"}]}
    merged = merge_overlay(bundle, {"a": {"status": "open"}})
    assert merged["suggestions"][0]["status"] == "open"


def test_merge_overlay_never_invents_from_overlay_only_id():
    bundle = {"suggestions": [{"suggestionId": "a", "status": "open"}]}
    merged = merge_overlay(bundle, {"ghost": {"status": "dismissed"}})
    assert [s["suggestionId"] for s in merged["suggestions"]] == ["a"]


def test_merge_overlay_returns_bundle_when_suggestions_missing_or_malformed():
    assert merge_overlay({"components": {}}, {"x": {"status": "dismissed"}}) == {"components": {}}
    assert merge_overlay({"suggestions": "nope"}, {}) == {"suggestions": "nope"}


def test_with_actor_bundle_omits_the_key_rather_than_attaching_an_empty_object():
    """Absent ≠ empty. `actor: {}` would say "measured, and you have nothing"; the key being
    missing says "no bundle exists for you", which is the true and usual state."""
    bundle = {"suggestions": []}
    for absent in (None, "nope", {}, {"createdAt": "x"}, {"bundle": "nope"}):
        assert with_actor_bundle(bundle, absent) == {"suggestions": []}


def test_with_actor_bundle_lifts_the_items_stamp_and_leaves_the_global_half_alone():
    merged = with_actor_bundle(
        {"suggestions": [{"suggestionId": "s1"}]},
        {"bundle": {"runs": [], "runsTotal": 0}, "createdAt": "2026-08-25T00:00:00Z"})
    assert merged["actor"] == {"runs": [], "runsTotal": 0,
                              "createdAt": "2026-08-25T00:00:00Z"}
    assert merged["suggestions"] == [{"suggestionId": "s1"}]


def test_with_actor_bundle_omits_a_blank_stamp_rather_than_carrying_an_empty_string():
    merged = with_actor_bundle({"suggestions": []}, {"bundle": {"runs": []}, "createdAt": ""})
    assert merged["actor"] == {"runs": []}


def test_decode_decimals_int_float_and_nesting():
    out = decode_decimals(
        {"v": Decimal("4"), "s": Decimal("0.400"), "l": [Decimal("1"), {"n": Decimal("2.5")}]}
    )
    assert out["v"] == 4 and isinstance(out["v"], int)
    assert out["s"] == pytest.approx(0.4) and isinstance(out["s"], float)
    assert out["l"][0] == 1 and isinstance(out["l"][0], int)
    assert out["l"][1]["n"] == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# ImproveStore — over the FakeTable directly
# ---------------------------------------------------------------------------


def test_store_get_snapshot_returns_none_when_absent():
    assert ImproveStore(FakeTable([])).get_snapshot() is None


def test_store_get_actor_partition_strips_prefix_and_keys_by_id():
    store = ImproveStore(
        FakeTable([_overlay_row("u-jane", "s1"), _overlay_row("u-jane", "s2")])
    )
    bundle, overlay = store.get_actor_partition("u-jane")
    assert bundle is None      # this actor has overlay rows but no evidence bundle
    assert set(overlay) == {"s1", "s2"}
    assert overlay["s1"]["status"] == "dismissed"


def test_store_get_actor_partition_separates_the_bundle_from_the_overlay():
    """Both kinds share the partition, so the sort key is what tells them apart — and a row whose
    `sk` matches neither must be ignored rather than read as one of them."""
    store = ImproveStore(FakeTable([
        _actor_snapshot("u-jane"),
        _overlay_row("u-jane", "s1"),
        {"actorId": "u-jane", "sk": "SOMETHING_ELSE", "runs": ["not a bundle"]},
    ]))
    bundle, overlay = store.get_actor_partition("u-jane")
    assert bundle is not None and bundle["sk"] == SNAPSHOT_SK
    assert set(overlay) == {"s1"}


def test_store_get_actor_partition_paginates():
    class Paging:
        """Two-page query, exercising the LastEvaluatedKey loop. The bundle is on the SECOND
        page, so a reader that stopped after one would report it absent."""

        def query(self, ExclusiveStartKey=None, **kw):
            if ExclusiveStartKey is None:
                return {"Items": [_overlay_row("u-jane", "s1")],
                        "LastEvaluatedKey": {"actorId": "u-jane", "sk": f"{SUGG_PREFIX}s1"}}
            return {"Items": [_overlay_row("u-jane", "s2"), _actor_snapshot("u-jane")]}

    bundle, overlay = ImproveStore(Paging()).get_actor_partition("u-jane")
    assert set(overlay) == {"s1", "s2"}
    assert bundle is not None


def test_store_wraps_failures_as_typed_unavailable():
    class Boom:
        def get_item(self, **kw):
            raise RuntimeError("down")

    with pytest.raises(ImproveStoreUnavailable) as exc:
        ImproveStore(Boom()).get_snapshot()
    assert exc.value.kind == "error"
