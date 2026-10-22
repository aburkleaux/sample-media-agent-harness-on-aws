"""Unit tests for the asset resolvers (Task 5.3; media-harness-search-qa-rendering).

Covers the three behaviours Task 5.3 asserts for `project_search.assets`:

1. **Factory refusal outside local** — `resolver_from_env` returns `None` (so the caller falls
   back to the deployed `AssetServiceResolver`) and logs a warning whenever `DEPLOY_ENV` is one of
   the deployed environments (`sandbox`/`staging`/`prod`/`source`), regardless of the requested dev
   resolver, and for an unknown value or an unmet precondition.
2. **`AssetServiceResolver` id omission and paging** — ids are de-duplicated and sent in pages of at
   most 100; ids the Lambda omits (or returns malformed) do not appear in the resolved map.
3. **`RegistryUnavailable`** — raised on a denial / non-ok reply, a `FunctionError`, an unparseable
   payload, a missing `value.assets`, an invoke exception, and an unconfigured function name.

A fake boto3 Lambda client stands in for the ingestion Asset_Service; no network, no AWS.

_Requirements: 2.5_
"""
from __future__ import annotations

import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from project_search import assets  # noqa: E402
from project_search.assets import (  # noqa: E402
    AssetServiceResolver,
    HitTitleResolver,
    LocalRegistryResolver,
    RegistryUnavailable,
    ResolvedAsset,
    resolver_from_env,
)


# ---------------------------------------------------------------------------
# Fake Lambda client
# ---------------------------------------------------------------------------
class _FakePayload:
    """A boto `invoke` response Payload: a file-like object whose ``read()`` returns bytes."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeLambda:
    """Records each `invoke` event and returns scripted responses via a handler.

    ``handler(event) -> resp`` where ``resp`` is the dict boto's ``invoke`` returns (``Payload`` and
    optional ``FunctionError``). The handler may raise to simulate an invoke exception.
    """

    def __init__(self, handler):
        self._handler = handler
        self.events: list[dict] = []

    def invoke(self, FunctionName, Payload):  # noqa: N803 - boto kwarg names
        event = json.loads(Payload)
        self.events.append(event)
        return self._handler(event)


def _ok_resp(assets_map: dict) -> dict:
    body = {"ok": True, "value": {"assets": assets_map}}
    return {"Payload": _FakePayload(json.dumps(body).encode())}


def _echo_handler(*, omit=frozenset(), malformed=frozenset()):
    """Resolve every requested id, deriving kind from its ``<kind>:`` prefix.

    Ids in ``omit`` are left out of the reply entirely (as the real Lambda drops absent / other-Project
    ids). Ids in ``malformed`` are returned with an invalid entry (bad ``kind``) so ``_resolved_asset``
    drops them as absent.
    """

    def handler(event: dict) -> dict:
        ids = event["args"]["assetIds"]
        out: dict[str, dict] = {}
        for aid in ids:
            if aid in omit:
                continue
            if aid in malformed:
                out[aid] = {"kind": "nope", "displayName": "x", "status": "indexed"}
                continue
            kind = aid.split(":", 1)[0] if ":" in aid else "video"
            out[aid] = {"kind": kind, "displayName": f"name-{aid}", "status": "indexed"}
        return _ok_resp(out)

    return handler


def _resolver(handler, *, function_name="asset-svc", token="tok.en"):
    return AssetServiceResolver(
        function_name=function_name,
        token_source=lambda: token,
        client=_FakeLambda(handler),
    )


# ---------------------------------------------------------------------------
# AssetServiceResolver — paging and id omission
# ---------------------------------------------------------------------------
def test_paging_splits_ids_into_pages_of_100():
    ids = [f"video:{i}" for i in range(250)]
    fake = _FakeLambda(_echo_handler())
    resolver = AssetServiceResolver(
        function_name="asset-svc", token_source=lambda: "tok.en", client=fake
    )

    resolved = resolver.resolve("p1", ids)

    # Three pages: 100, 100, 50.
    assert [len(e["args"]["assetIds"]) for e in fake.events] == [100, 100, 50]
    assert len(resolved) == 250
    assert set(resolved) == set(ids)
    # Every page carries the full request envelope.
    for e in fake.events:
        assert e["command"] == "resolve_assets"
        assert e["caller"] == "main_agent"
        assert e["projectId"] == "p1"
        assert e["userToken"] == "tok.en"


def test_exactly_100_ids_is_a_single_page():
    ids = [f"video:{i}" for i in range(100)]
    fake = _FakeLambda(_echo_handler())
    AssetServiceResolver(
        function_name="asset-svc", token_source=lambda: "t", client=fake
    ).resolve("p1", ids)
    assert len(fake.events) == 1


def test_ids_are_deduplicated_before_paging():
    fake = _FakeLambda(_echo_handler())
    resolver = AssetServiceResolver(
        function_name="asset-svc", token_source=lambda: "t", client=fake
    )
    resolved = resolver.resolve("p1", ["video:a", "video:a", "video:b", "video:a"])
    assert fake.events[0]["args"]["assetIds"] == ["video:a", "video:b"]
    assert set(resolved) == {"video:a", "video:b"}


def test_omitted_and_malformed_ids_do_not_appear():
    ids = ["video:a", "video:b", "image:c", "audio:d"]
    handler = _echo_handler(omit={"video:b"}, malformed={"image:c"})
    resolved = _resolver(handler).resolve("p1", ids)
    # video:b omitted by the Lambda; image:c dropped as malformed (bad kind).
    assert set(resolved) == {"video:a", "audio:d"}
    assert isinstance(resolved["video:a"], ResolvedAsset)
    assert resolved["audio:d"].kind == "audio"
    assert resolved["video:a"].display_name == "name-video:a"
    assert resolved["video:a"].status == "indexed"


def test_empty_or_blank_ids_make_no_call():
    fake = _FakeLambda(_echo_handler())
    resolver = AssetServiceResolver(
        function_name="asset-svc", token_source=lambda: "t", client=fake
    )
    assert resolver.resolve("p1", []) == {}
    assert resolver.resolve("p1", ["", None, 123]) == {}  # non-str / empty dropped by _distinct
    assert fake.events == []


# ---------------------------------------------------------------------------
# AssetServiceResolver — RegistryUnavailable
# ---------------------------------------------------------------------------
def test_non_ok_reply_raises():
    def handler(event):
        body = {"ok": False, "status": 404, "error": {"code": "x", "message": "gone"}}
        return {"Payload": _FakePayload(json.dumps(body).encode())}

    with pytest.raises(RegistryUnavailable):
        _resolver(handler).resolve("p1", ["video:a"])


def test_function_error_raises():
    def handler(event):
        return {"FunctionError": "Unhandled", "Payload": _FakePayload(b"{}")}

    with pytest.raises(RegistryUnavailable):
        _resolver(handler).resolve("p1", ["video:a"])


def test_unparseable_payload_raises():
    def handler(event):
        return {"Payload": _FakePayload(b"<not json>")}

    with pytest.raises(RegistryUnavailable):
        _resolver(handler).resolve("p1", ["video:a"])


def test_missing_value_assets_raises():
    def handler(event):
        return {"Payload": _FakePayload(json.dumps({"ok": True, "value": {}}).encode())}

    with pytest.raises(RegistryUnavailable):
        _resolver(handler).resolve("p1", ["video:a"])


def test_invoke_exception_raises():
    def handler(event):
        raise RuntimeError("boom")

    with pytest.raises(RegistryUnavailable):
        _resolver(handler).resolve("p1", ["video:a"])


def test_unconfigured_function_name_raises():
    resolver = AssetServiceResolver(
        function_name="", token_source=lambda: "t", client=_FakeLambda(_echo_handler())
    )
    with pytest.raises(RegistryUnavailable):
        resolver.resolve("p1", ["video:a"])


# ---------------------------------------------------------------------------
# resolver_from_env — factory refusal outside local
# ---------------------------------------------------------------------------
LOOPBACK = "http://127.0.0.1:8000"


@pytest.mark.parametrize("deploy_env", ["sandbox", "staging", "prod", "source"])
@pytest.mark.parametrize("mode", ["registry", "hits"])
def test_factory_refused_in_deployed_env(deploy_env, mode, caplog):
    env = {
        "LOCAL_DEV_ASSET_RESOLVER": mode,
        "DEPLOY_ENV": deploy_env,
        "PROJECTS_DDB_ENDPOINT": LOOPBACK,
        "PROJECTS_TABLE": "t",
    }
    with caplog.at_level(logging.WARNING, logger="project_search.assets"):
        got = resolver_from_env(hit_store=object(), env=env)
    assert got is None
    assert any(deploy_env in rec.getMessage() for rec in caplog.records)


@pytest.mark.parametrize("deploy_env", ["SANDBOX", "Staging", " prod "])
def test_deployed_env_match_is_case_and_space_insensitive(deploy_env):
    env = {"LOCAL_DEV_ASSET_RESOLVER": "registry", "DEPLOY_ENV": deploy_env,
           "PROJECTS_DDB_ENDPOINT": LOOPBACK, "PROJECTS_TABLE": "t"}
    assert resolver_from_env(hit_store=object(), env=env) is None


def test_factory_none_when_unset():
    assert resolver_from_env(env={"DEPLOY_ENV": "local"}) is None


def test_factory_refuses_unknown_mode(caplog):
    env = {"LOCAL_DEV_ASSET_RESOLVER": "bogus", "DEPLOY_ENV": "local"}
    with caplog.at_level(logging.WARNING, logger="project_search.assets"):
        assert resolver_from_env(env=env) is None
    assert any("unknown resolver" in rec.getMessage() for rec in caplog.records)


def test_factory_registry_requires_loopback_endpoint(caplog):
    env = {"LOCAL_DEV_ASSET_RESOLVER": "registry", "DEPLOY_ENV": "local",
           "PROJECTS_DDB_ENDPOINT": "https://dynamodb.us-west-2.amazonaws.com",
           "PROJECTS_TABLE": "t"}
    with caplog.at_level(logging.WARNING, logger="project_search.assets"):
        assert resolver_from_env(env=env) is None
    assert any("loopback" in rec.getMessage() for rec in caplog.records)


def test_factory_hits_requires_hit_store(caplog):
    env = {"LOCAL_DEV_ASSET_RESOLVER": "hits", "DEPLOY_ENV": "local"}
    with caplog.at_level(logging.WARNING, logger="project_search.assets"):
        assert resolver_from_env(hit_store=None, env=env) is None
    assert any("SessionHitStore" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# resolver_from_env — local selections
# ---------------------------------------------------------------------------
def test_factory_builds_registry_resolver_locally():
    env = {"LOCAL_DEV_ASSET_RESOLVER": "registry", "DEPLOY_ENV": "local",
           "PROJECTS_DDB_ENDPOINT": LOOPBACK, "PROJECTS_TABLE": "t"}
    got = resolver_from_env(env=env)
    assert isinstance(got, LocalRegistryResolver)


def test_factory_builds_hits_resolver_locally():
    env = {"LOCAL_DEV_ASSET_RESOLVER": "hits", "DEPLOY_ENV": "local"}
    got = resolver_from_env(hit_store=object(), env=env)
    assert isinstance(got, HitTitleResolver)


def test_factory_builds_registry_resolver_when_deploy_env_unset():
    # Unset DEPLOY_ENV counts as local.
    env = {"LOCAL_DEV_ASSET_RESOLVER": "registry",
           "PROJECTS_DDB_ENDPOINT": LOOPBACK, "PROJECTS_TABLE": "t"}
    assert isinstance(resolver_from_env(env=env), LocalRegistryResolver)
