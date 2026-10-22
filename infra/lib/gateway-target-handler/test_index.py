"""Unit test for the gateway-target-handler MOVE path (Wave 4 / Option B).

Moving the AWS-doc target from the tenant gateway to the Platform gateway changes GatewayId.
The handler's identity-change path must delete the EXISTING target from the OLD gateway (where
it actually lives) before creating it on the NEW gateway — otherwise the stale target lingers on
the previous gateway and the tool surfaces on BOTH gateways (a fail-closed collision in the
runtime's find_gateway_for_tool).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import index  # noqa: E402


def test_gateway_move_deletes_from_old_gateway(monkeypatch):
    calls = {}

    def fake_delete(event, props):
        calls["delete_gateway"] = props.get("GatewayId")
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "")}

    def fake_create(props):
        calls["create_gateway"] = props.get("GatewayId")
        return {"PhysicalResourceId": "new-target-id"}

    monkeypatch.setattr(index, "handle_delete", fake_delete)
    monkeypatch.setattr(index, "handle_create", fake_create)
    monkeypatch.setattr(index, "get_region", lambda: "us-west-2")

    event = {
        "PhysicalResourceId": "old-target-id",
        "OldResourceProperties": {
            "GatewayId": "tenant-gw", "Endpoint": "e", "TargetName": "aws-mcp", "Revision": "2",
        },
    }
    props = {"GatewayId": "platform-gw", "Endpoint": "e", "TargetName": "aws-mcp", "Revision": "3"}

    result = index.handle_update(event, props)

    assert calls["delete_gateway"] == "tenant-gw"    # deleted from the OLD gateway (not the new)
    assert calls["create_gateway"] == "platform-gw"  # created on the NEW gateway
    assert result["PhysicalResourceId"] == "new-target-id"


def test_revision_bump_recreates_in_place_same_gateway(monkeypatch):
    # A Revision bump WITHOUT a GatewayId change still recreates, deleting from the SAME gateway.
    calls = {}
    monkeypatch.setattr(index, "handle_delete", lambda e, p: calls.__setitem__("del", p.get("GatewayId")) or {"PhysicalResourceId": ""})
    monkeypatch.setattr(index, "handle_create", lambda p: {"PhysicalResourceId": "n"})
    monkeypatch.setattr(index, "get_region", lambda: "us-west-2")
    event = {"PhysicalResourceId": "t", "OldResourceProperties": {"GatewayId": "gw", "Endpoint": "e", "TargetName": "n", "Revision": "2"}}
    props = {"GatewayId": "gw", "Endpoint": "e", "TargetName": "n", "Revision": "3"}
    index.handle_update(event, props)
    assert calls["del"] == "gw"
