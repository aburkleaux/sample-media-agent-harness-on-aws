"""Tests for ``list_platform_mcp_servers`` — platform targets in the MCP panel.

aws-mcp never appeared in the MCP servers panel for one narrow reason: it is a
PLATFORM target on the harness gateway, not a user registration, so it has no
ownership row and was never in ``list_user_mcp_servers``' output. This adds the
platform rows alongside the owned ones.

Scope is deliberately the HARNESS gateway alone. Other registered gateways hold
targets too, and a version of this listing walked the whole gateway registry to
find them — but their tools already reach agents over the gateway's own OAuth
connection, so enumerating their targets added nothing an operator could act on
while requiring ``ListGatewayTargets`` across the account. That was reverted; the
gateway registry (Settings → Gateways) is where those gateways are managed.

Two requirements outlive that revert and are tested here:

* **Say when the read failed.** An empty panel must state a reason rather than
  read as "the harness has no MCP server" — the same silent-shortfall class of bug
  as aws-mcp being missing in the first place.
* **Never offer to remove a platform target.** It belongs to the harness, and
  ``deregister_user_mcp_server`` re-checks ownership against a recorded row a
  platform target does not have, so the button could only ever fail.

The isolation requirement from the original fix still holds: listing platform
targets is a control-plane call, and it must never break the caller's own list.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gateway.user_mcp as user_mcp  # noqa: E402


class _Client:
    def __init__(self, pages, raises=False):
        self._pages = pages
        self._raises = raises
        self.calls = []

    def list_gateway_targets(self, **kw):
        if self._raises:
            raise RuntimeError("control plane down")
        self.calls.append(kw)
        return self._pages[len(self.calls) - 1]


@pytest.fixture(autouse=True)
def _gateway(monkeypatch):
    monkeypatch.setattr(user_mcp, "HARNESS_GATEWAY_ID", "gw-1")
    yield


def _wire(monkeypatch, client):
    monkeypatch.setattr(user_mcp.boto3, "client", lambda *a, **k: client)


def test_no_gateway_id_returns_empty(monkeypatch):
    monkeypatch.setattr(user_mcp, "HARNESS_GATEWAY_ID", "")
    assert user_mcp.list_platform_mcp_servers() == ([], [])


def test_returns_platform_target(monkeypatch):
    _wire(monkeypatch, _Client([
        {"items": [{"name": "mediaworks-harness-aws-mcp", "targetId": "P8", "status": "READY"}]}
    ]))
    rows, unreadable = user_mcp.list_platform_mcp_servers()
    assert unreadable == []
    assert len(rows) == 1
    assert rows[0]["target_name"] == "mediaworks-harness-aws-mcp"
    assert rows[0]["target_id"] == "P8"
    assert rows[0]["status"] == "ready"
    assert rows[0]["platform"] is True


def test_only_the_harness_gateway_is_queried(monkeypatch):
    """Scope check. Walking the registry was reverted; keep it reverted."""
    client = _Client([{"items": []}])
    _wire(monkeypatch, client)

    user_mcp.list_platform_mcp_servers()

    assert [c["gatewayIdentifier"] for c in client.calls] == ["gw-1"]


def test_platform_targets_are_not_removable(monkeypatch):
    """The button could only ever fail: a platform target has no ownership row."""
    _wire(monkeypatch, _Client([
        {"items": [{"name": "plat", "targetId": "P", "status": "READY"}]}
    ]))
    rows, _ = user_mcp.list_platform_mcp_servers()
    assert rows[0]["removable"] is False


def test_owned_registrations_are_removable(monkeypatch):
    """The counterpart: the caller's own rows are theirs to delete."""
    class _OwnTable:
        def query(self, **_kw):
            return {"Items": [{
                "SK": "usr-abc123", "ownerActorId": "actor-1", "targetId": "U1",
                "serverName": "my-server", "endpoint": "https://mine.example.com/mcp",
                "status": "ready",
            }]}

    monkeypatch.setattr(user_mcp, "_table", lambda: _OwnTable())

    rows = user_mcp.list_user_mcp_servers("actor-1")

    assert [r["server_name"] for r in rows] == ["my-server"]
    assert rows[0]["removable"] is True


def test_user_targets_are_excluded(monkeypatch):
    # A usr- target belongs to some actor and is already covered by the ownership
    # rows; including it here would list it twice for its owner and, worse, show
    # another actor's target to everyone.
    _wire(monkeypatch, _Client([
        {"items": [
            {"name": "usr-abc123", "targetId": "U1", "status": "READY"},
            {"name": "mediaworks-harness-aws-mcp", "targetId": "P8", "status": "READY"},
        ]}
    ]))
    rows, _ = user_mcp.list_platform_mcp_servers()
    assert [r["target_name"] for r in rows] == ["mediaworks-harness-aws-mcp"]


def test_non_ready_status_is_synchronizing(monkeypatch):
    _wire(monkeypatch, _Client([
        {"items": [{"name": "plat", "targetId": "T", "status": "CREATING"}]}
    ]))
    rows, _ = user_mcp.list_platform_mcp_servers()
    assert rows[0]["status"] == "synchronizing"


def test_pagination_is_followed(monkeypatch):
    c = _Client([
        {"items": [{"name": "a", "targetId": "1", "status": "READY"}], "nextToken": "t1"},
        {"items": [{"name": "b", "targetId": "2", "status": "READY"}]},
    ])
    _wire(monkeypatch, c)
    rows, _ = user_mcp.list_platform_mcp_servers()
    assert [r["target_name"] for r in rows] == ["a", "b"]
    assert c.calls[1].get("nextToken") == "t1"


def test_a_control_plane_failure_degrades_to_empty_and_says_so(monkeypatch):
    # Must NOT raise: the caller concatenates this with the user's own servers,
    # and that list already worked. A control-plane problem loses platform rows,
    # never the caller's own — but it must not look like "there is no platform
    # server", which is why the reason comes back rather than a bare [].
    _wire(monkeypatch, _Client([], raises=True))
    rows, unreadable = user_mcp.list_platform_mcp_servers()
    assert rows == []
    assert len(unreadable) == 1
    assert "Platform gateway" in unreadable[0]


def test_unnamed_items_are_skipped(monkeypatch):
    _wire(monkeypatch, _Client([{"items": [{"targetId": "x", "status": "READY"}, {}]}]))
    rows, unreadable = user_mcp.list_platform_mcp_servers()
    assert (rows, unreadable) == ([], [])
