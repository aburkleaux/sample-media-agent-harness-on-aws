"""Fail-closed tool allowlist on a gateway connection.

Kept from the short-lived direct AWS MCP connection, which was removed: routing
one server straight to the runtime bypassed the harness gateway, and with it the
interceptor, per-actor scoping, and any future policy engine. Everything now goes
through the gateway, so write-capable tools are dropped by the interceptor rather
than client side.

The allowlist survives because it is a general registry capability -- a gateway
can declare which of its tools may surface at all -- and because it fails closed,
which is the property worth pinning.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.registry import GatewayDescriptor
from mcp_manager import GatewayConnection

DOCS_ONLY = ["aws___search_documentation", "aws___read_documentation"]


class FakeTool:
    def __init__(self, name): self.tool_name = name
    def __repr__(self): return self.tool_name


def _tools():
    p = "mediaworks-harness-aws-mcp___"
    return [FakeTool(p + n) for n in
            ("aws___search_documentation", "aws___read_documentation",
             "aws___call_aws", "aws___retrieve_skill")]


def _conn(**cfg):
    base = {"name": "g", "url": "https://gw.example/mcp"}
    base.update(cfg)
    return GatewayConnection("g", base)


def test_allowlist_excludes_write_capable_tools():
    kept = [str(t) for t in _conn(auth_type="jwt", tool_allowlist=DOCS_ONLY)
            ._apply_tool_allowlist(_tools())]
    assert len(kept) == 2
    assert not any("call_aws" in n for n in kept)


def test_allowlist_matches_through_the_target_prefix():
    """The tool name itself contains `___`.

    A gateway-prefixed AWS MCP tool is `<target>___aws___search_documentation`.
    Splitting on every separator yields `search_documentation`, matching no entry
    and -- fail-closed -- dropping every tool. Split once only.
    """
    conn = _conn(auth_type="jwt", tool_allowlist=DOCS_ONLY)
    assert len(conn._apply_tool_allowlist(_tools())) == 2
    assert len(conn._apply_tool_allowlist([FakeTool("aws___search_documentation")])) == 1


def test_no_allowlist_means_no_filtering():
    assert len(_conn(auth_type="jwt")._apply_tool_allowlist(_tools())) == 4


def test_unknown_tool_is_dropped_not_kept():
    conn = _conn(auth_type="jwt", tool_allowlist=DOCS_ONLY)
    assert conn._apply_tool_allowlist([FakeTool("something___entirely___odd")]) == []


def test_allowlist_survives_from_descriptor_into_connection():
    """A descriptor that parses but drops the allowlist fails OPEN."""
    row = {
        "id": "g", "name": "G", "url": "https://gw.example/mcp",
        "auth_type": "none", "auth_config": {"auth_type": "none"},
        "tool_allowlist": DOCS_ONLY, "enabled": True,
    }
    d = GatewayDescriptor.from_dict(row)
    assert d.tool_allowlist == tuple(DOCS_ONLY)
    cfg = d.to_connection_config()
    assert cfg["tool_allowlist"] == DOCS_ONLY
    assert GatewayConnection("g", cfg).tool_allowlist
    again = GatewayDescriptor.from_dict(d.to_dict())
    assert again.tool_allowlist == d.tool_allowlist


def test_removed_bypass_auth_type_is_gone():
    """`aws_signin_oauth` routed a server directly to the runtime, past the
    gateway. Its absence is the invariant: one enforcement point, no special cases."""
    from gateway.registry import AuthType
    import typing
    assert "aws_signin_oauth" not in typing.get_args(AuthType)
