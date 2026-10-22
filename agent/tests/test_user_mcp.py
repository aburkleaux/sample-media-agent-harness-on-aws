"""Tests for user MCP server registration.

The load-bearing behaviours are the ordering (ownership row before target) and
the unwind on failure: a target without its ownership row is invisible to
everyone including its owner, who then cannot delete it.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import gateway.user_mcp as user_mcp  # noqa: E402
from gateway.user_mcp import RegistrationError, validate_endpoint  # noqa: E402

ALICE = "USER#alice"
BOB = "bob-sub"
GW = "gw-test"


@pytest.fixture(autouse=True)
def _public_dns():
    """No network in unit tests: every name resolves to a public address via the shared
    SSRF guard's resolver hook, unless a test installs its own."""
    from security import url_guard
    previous = url_guard.set_resolver(lambda host, port: ["93.184.215.14"])
    yield
    url_guard.set_resolver(previous)


# ---------------------------------------------------------------------------
# Endpoint validation
# ---------------------------------------------------------------------------


def test_https_required():
    """The user's credential travels on this connection."""
    with pytest.raises(RegistrationError, match="https"):
        validate_endpoint("http://mcp.example.com/sse")


def test_accepts_public_https():
    assert validate_endpoint(" https://mcp.example.com/sse ") == "https://mcp.example.com/sse"


@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1",
    "169.254.169.254", "svc.internal", "box.local", "0.0.0.0",
])
def test_rejects_non_public_hosts(host):
    """169.254.169.254 is the instance metadata endpoint."""
    with pytest.raises(RegistrationError, match="publicly reachable"):
        validate_endpoint(f"https://{host}/mcp")


@pytest.mark.parametrize("host", [
    "100.64.0.1", "[::1]", "[fd00:ec2::254]", "[::ffff:127.0.0.1]",
    "2130706433", "0177.0.0.1", "0x7f000001", "127.1",
])
def test_rejects_cgnat_ipv6_and_non_canonical_literals(host):
    with pytest.raises(RegistrationError, match="publicly reachable"):
        validate_endpoint(f"https://{host}/mcp")


def test_rejects_a_public_name_that_resolves_to_metadata():
    """The old name patterns let any DNS name pointing at 169.254.169.254 through."""
    from security import url_guard
    url_guard.set_resolver(lambda host, port: ["169.254.169.254"])
    with pytest.raises(RegistrationError, match="publicly reachable"):
        validate_endpoint("https://rebind.attacker.example/mcp")


def test_url_required():
    with pytest.raises(RegistrationError):
        validate_endpoint("")


def test_header_name_charset_enforced():
    with pytest.raises(RegistrationError, match="letters, numbers and hyphens"):
        user_mcp._validate_header_name("X Bad Header")


def test_header_name_defaults():
    assert user_mcp._validate_header_name("") == "Authorization"


# ---------------------------------------------------------------------------
# Target naming
# ---------------------------------------------------------------------------


def test_target_name_is_opaque_unique_and_legal():
    a, b = user_mcp.new_target_name(), user_mcp.new_target_name()
    assert a != b
    for name in (a, b):
        assert name.startswith("usr-")
        assert len(name) <= 100


def test_target_name_satisfies_the_service_pattern():
    """Pins a constraint learned from a live rejection, not from the docs.

    The service requires ([0-9a-zA-Z][-]?){1,100} -- no underscores. The CDK L2
    documents gatewayTargetName as permitting underscores, and an earlier
    usr_<uuid> scheme was rejected with "Value at 'name' failed to satisfy
    constraint".
    """
    import re
    service = re.compile(r"^([0-9a-zA-Z][-]?){1,100}$")
    for _ in range(50):
        name = user_mcp.new_target_name()
        assert service.match(name), name
        assert "_" not in name


def test_underscore_prefix_would_be_rejected():
    """Guards the specific regression: a prefix with an underscore must fail the
    generator's own check rather than reaching the gateway."""
    with pytest.raises(RegistrationError, match="not valid"):
        user_mcp.new_target_name(prefix="usr_")


# ---------------------------------------------------------------------------
# Registration flow
# ---------------------------------------------------------------------------


class _Recorder:
    """Captures the order of side effects so sequencing can be asserted."""

    def __init__(self):
        self.calls: list[str] = []
        self.items: dict = {}

    # DynamoDB surface
    def put_item(self, Item):  # noqa: N803
        self.calls.append("record_ownership")
        self.items[Item["SK"]] = Item

    def delete_item(self, Key):  # noqa: N803
        self.calls.append("forget_ownership")
        self.items.pop(Key["SK"], None)

    def get_item(self, Key):  # noqa: N803
        item = self.items.get(Key["SK"])
        return {"Item": item} if item else {}

    def update_item(self, **kwargs):
        self.calls.append("record_target_id")


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(user_mcp, "_table", lambda: r)
    monkeypatch.setattr(user_mcp, "HARNESS_GATEWAY_ID", GW)
    monkeypatch.setattr(
        user_mcp, "_create_api_key_provider",
        lambda name, key: (r.calls.append("create_provider"), "arn:provider")[1],
    )
    monkeypatch.setattr(
        user_mcp, "_delete_api_key_provider",
        lambda name: r.calls.append("delete_provider"),
    )
    monkeypatch.setattr(
        user_mcp, "_create_target",
        lambda *a, **k: (r.calls.append("create_target"), "tgt-1")[1],
    )
    monkeypatch.setattr(
        user_mcp, "delete_target",
        lambda gw, tid: r.calls.append("delete_target"),
    )
    monkeypatch.setattr(user_mcp, "_target_status", lambda gw, tid: "READY")
    monkeypatch.setattr(user_mcp, "_synchronize", lambda gw, tid: None)
    return r


def test_happy_path_returns_ready(rec):
    out = user_mcp.register_user_mcp_server(
        ALICE, "My Notes", "https://mcp.example.com/sse", token="tok",
    )
    assert out["status"] == "ready"
    assert out["target_id"] == "tgt-1"
    assert out["target_name"].startswith("usr-")


def test_ownership_is_recorded_before_the_target_exists(rec):
    """A target without its row is invisible to everyone, owner included."""
    user_mcp.register_user_mcp_server(
        ALICE, "My Notes", "https://mcp.example.com/sse", token="tok",
    )
    assert rec.calls.index("record_ownership") < rec.calls.index("create_target")


def test_ownership_row_carries_the_owner(rec):
    out = user_mcp.register_user_mcp_server(
        ALICE, "My Notes", "https://mcp.example.com/sse", token="tok",
    )
    item = rec.items[out["target_name"]]
    assert item["PK"] == user_mcp.OWNER_PK
    assert item["ownerActorId"] == ALICE
    assert item["serverName"] == "My Notes"


def test_owner_pk_matches_the_readers():
    """Interceptor ownership.py and toolCatalog.ts read this exact partition."""
    assert user_mcp.OWNER_PK == "TARGET#OWNER"


def test_token_never_lands_in_the_ownership_row(rec):
    out = user_mcp.register_user_mcp_server(
        ALICE, "My Notes", "https://mcp.example.com/sse", token="super-secret",
    )
    assert "super-secret" not in str(rec.items[out["target_name"]])


def test_target_failure_unwinds_everything(rec, monkeypatch):
    def _boom(*a, **k):
        rec.calls.append("create_target")
        raise RegistrationError("Gateway rejected the target: bad endpoint")

    monkeypatch.setattr(user_mcp, "_create_target", _boom)
    with pytest.raises(RegistrationError, match="bad endpoint"):
        user_mcp.register_user_mcp_server(
            ALICE, "My Notes", "https://mcp.example.com/sse", token="tok",
        )
    assert "delete_provider" in rec.calls
    assert "forget_ownership" in rec.calls
    assert rec.items == {}, "no orphaned ownership row"


def test_unexpected_error_also_unwinds(rec, monkeypatch):
    monkeypatch.setattr(
        user_mcp, "_create_target",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("socket blew up")),
    )
    with pytest.raises(RegistrationError, match="socket blew up"):
        user_mcp.register_user_mcp_server(
            ALICE, "My Notes", "https://mcp.example.com/sse", token="tok",
        )
    assert rec.items == {}


def test_validation_runs_before_any_side_effect(rec):
    with pytest.raises(RegistrationError):
        user_mcp.register_user_mcp_server(
            ALICE, "My Notes", "http://mcp.example.com/sse", token="tok",
        )
    assert rec.calls == [], "nothing should be created on invalid input"


def test_anonymous_registration_refused(rec):
    with pytest.raises(RegistrationError, match="who is registering"):
        user_mcp.register_user_mcp_server(
            "", "My Notes", "https://mcp.example.com/sse", token="tok",
        )


def test_missing_gateway_config_refused(rec, monkeypatch):
    monkeypatch.setattr(user_mcp, "HARNESS_GATEWAY_ID", "")
    with pytest.raises(RegistrationError, match="gateway is not configured"):
        user_mcp.register_user_mcp_server(
            ALICE, "My Notes", "https://mcp.example.com/sse", token="tok",
        )


def test_failed_status_reports_synchronizing_and_resyncs(rec, monkeypatch):
    seen: list = []
    monkeypatch.setattr(user_mcp, "_target_status", lambda gw, tid: "FAILED")
    monkeypatch.setattr(user_mcp, "_synchronize", lambda gw, tid: seen.append(tid))
    out = user_mcp.register_user_mcp_server(
        ALICE, "My Notes", "https://mcp.example.com/sse", token="tok",
    )
    assert out["status"] == "synchronizing"
    assert seen == ["tgt-1"]


def test_pending_status_does_not_block_the_caller(rec, monkeypatch):
    """Sync can take minutes; a form submit must not wait for it."""
    monkeypatch.setattr(user_mcp, "_target_status", lambda gw, tid: "CREATING")
    monkeypatch.setattr(user_mcp, "_STATUS_CHECK_ATTEMPTS", 1)
    out = user_mcp.register_user_mcp_server(
        ALICE, "My Notes", "https://mcp.example.com/sse", token="tok",
    )
    assert out["status"] == "synchronizing"


# ---------------------------------------------------------------------------
# Deregistration
# ---------------------------------------------------------------------------


def test_owner_can_deregister(rec):
    out = user_mcp.register_user_mcp_server(
        ALICE, "My Notes", "https://mcp.example.com/sse", token="tok",
    )
    res = user_mcp.deregister_user_mcp_server(ALICE, out["target_name"], "tgt-1")
    assert res["status"] == "deleted"
    assert rec.items == {}


def test_non_owner_cannot_deregister(rec):
    out = user_mcp.register_user_mcp_server(
        ALICE, "My Notes", "https://mcp.example.com/sse", token="tok",
    )
    before = dict(rec.items)
    with pytest.raises(RegistrationError, match="No such MCP server"):
        user_mcp.deregister_user_mcp_server(BOB, out["target_name"], "tgt-1")
    assert rec.items == before, "another actor's target must survive"


def test_unknown_and_unowned_are_indistinguishable(rec):
    """Otherwise deletion attempts enumerate other users' targets."""
    out = user_mcp.register_user_mcp_server(
        ALICE, "My Notes", "https://mcp.example.com/sse", token="tok",
    )
    with pytest.raises(RegistrationError) as unowned:
        user_mcp.deregister_user_mcp_server(BOB, out["target_name"])
    with pytest.raises(RegistrationError) as unknown:
        user_mcp.deregister_user_mcp_server(BOB, "usr-does-not-exist")
    assert str(unowned.value) == str(unknown.value)


def test_status_poll_maps_states(monkeypatch):
    monkeypatch.setattr(user_mcp, "HARNESS_GATEWAY_ID", GW)
    for raw, expected in (
        ("READY", "ready"), ("ACTIVE", "ready"),
        ("FAILED", "failed"), ("SYNCHRONIZE_UNSUCCESSFUL", "failed"),
        ("CREATING", "synchronizing"), ("", "synchronizing"),
    ):
        monkeypatch.setattr(user_mcp, "_target_status", lambda gw, tid, r=raw: r)
        assert user_mcp.registration_status("tgt-1") == expected, raw


def test_status_poll_without_config_fails():
    assert user_mcp.registration_status("") == "failed"


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class _QueryTable(_Recorder):
    """Adds Query, which the ownership partition is read with."""

    def query(self, **kwargs):
        return {"Items": list(self.items.values())}


@pytest.fixture
def qrec(monkeypatch):
    r = _QueryTable()
    monkeypatch.setattr(user_mcp, "_table", lambda: r)
    monkeypatch.setattr(user_mcp, "HARNESS_GATEWAY_ID", GW)
    return r


def test_list_returns_only_the_callers_servers(qrec):
    qrec.items["usr-a"] = {
        "PK": user_mcp.OWNER_PK, "SK": "usr-a", "ownerActorId": ALICE,
        "serverName": "Alice One", "endpoint": "https://a/mcp",
        "targetId": "t1", "status": "ready",
    }
    qrec.items["usr-b"] = {
        "PK": user_mcp.OWNER_PK, "SK": "usr-b", "ownerActorId": BOB,
        "serverName": "Bob One", "endpoint": "https://b/mcp",
        "targetId": "t2", "status": "ready",
    }
    names = [r["server_name"] for r in user_mcp.list_user_mcp_servers(ALICE)]
    assert names == ["Alice One"]


def test_list_is_sorted_and_carries_ids(qrec):
    for sk, name in (("usr-z", "Zulu"), ("usr-a", "alpha")):
        qrec.items[sk] = {
            "PK": user_mcp.OWNER_PK, "SK": sk, "ownerActorId": ALICE,
            "serverName": name, "endpoint": "https://x/mcp",
            "targetId": f"id-{sk}", "status": "synchronizing",
        }
    rows = user_mcp.list_user_mcp_servers(ALICE)
    assert [r["server_name"] for r in rows] == ["alpha", "Zulu"]
    assert rows[0]["target_id"] == "id-usr-a"


def test_list_without_actor_is_empty(qrec):
    assert user_mcp.list_user_mcp_servers("") == []


def test_list_survives_a_read_failure(monkeypatch):
    class _Boom:
        def query(self, **kw):
            raise RuntimeError("throttled")

    monkeypatch.setattr(user_mcp, "_table", lambda: _Boom())
    # An empty list beats propagating a 500 into the panel.
    assert user_mcp.list_user_mcp_servers(ALICE) == []


def test_registration_persists_the_target_id(rec):
    out = user_mcp.register_user_mcp_server(
        ALICE, "My Notes", "https://mcp.example.com/sse", token="tok",
    )
    assert "record_ownership" in rec.calls
    assert out["target_id"] == "tgt-1"


def test_resolved_status_is_written_back(rec, monkeypatch):
    """Otherwise a listed server shows as settling forever."""
    written = {}
    monkeypatch.setattr(user_mcp, "record_target_id",
                        lambda name, tid, status: written.update({name: status}))
    monkeypatch.setattr(user_mcp, "_target_status", lambda gw, tid: "READY")
    assert user_mcp.registration_status("t1", target_name="usr_x") == "ready"
    assert written == {"usr_x": "ready"}


def test_unresolved_status_is_not_written_back(rec, monkeypatch):
    written = {}
    monkeypatch.setattr(user_mcp, "record_target_id",
                        lambda name, tid, status: written.update({name: status}))
    monkeypatch.setattr(user_mcp, "_target_status", lambda gw, tid: "CREATING")
    assert user_mcp.registration_status("t1", target_name="usr_x") == "synchronizing"
    assert written == {}


# ---------------------------------------------------------------------------
# Header value splitting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("Key abc:def", ("Key ", "abc:def")),      # fal.ai
    ("Bearer tok123", ("Bearer ", "tok123")),  # the common OAuth shape
    ("rawapikey", ("", "rawapikey")),          # no prefix at all
    ("  Key   abc  ", ("Key   ", "abc")),      # surrounding and inner whitespace
    ("", ("", "")),
])
def test_split_header_value(value, expected):
    assert user_mcp.split_header_value(value) == expected


def test_split_keeps_the_separator_on_the_prefix():
    """AgentCore concatenates prefix + key with nothing between them, so the
    space has to live on the prefix or the header arrives as 'Keyabc'."""
    prefix, token = user_mcp.split_header_value("Key abc")
    assert prefix + token == "Key abc"


def test_a_pasted_header_value_wins_over_split_fields(rec):
    out = user_mcp.register_user_mcp_server(
        ALICE, "fal", "https://mcp.fal.ai/mcp",
        header_value="Key abc:def",
        # Deliberately wrong split fields: header_value must override them.
        token="ignored", value_prefix="Bearer ",
    )
    assert out["status"] == "ready"


def test_prefix_with_no_credential_is_refused(rec):
    """A truncated paste is a mistake, not a credential-free server."""
    with pytest.raises(RegistrationError, match="looks incomplete"):
        user_mcp.register_user_mcp_server(
            ALICE, "fal", "https://mcp.fal.ai/mcp", header_value="Bearer",
        )


def test_registration_errors_are_logged(caplog):
    """Two live failures produced no log line, leaving the only diagnosis in the
    user's browser."""
    import logging
    with caplog.at_level(logging.WARNING):
        RegistrationError("something specific went wrong")
    assert "something specific went wrong" in caplog.text
