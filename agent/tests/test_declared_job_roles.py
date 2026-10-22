"""Tests for registry-declared job roles — bringing fal into the async job pipeline.

fal's async tools are ``submit_job`` / ``check_job`` / ``get_job_result``. That is a
complete trio, but it matches neither the ``start_*`` / ``*_status`` / ``*_results``
naming convention nor MCP's ``_meta.job_role`` annotation (fal's server sends none).
So no job was ever registered, polled, or notified — every other async job in the
system does all three, and fal silently did not.

The Job_Detector already treats ``_meta.job_role`` as authoritative (R2.3). Declaring
roles on the gateway's registry row feeds that SAME mechanism, so nothing downstream —
detector, registry, poller, changefeed, SSE, UI — learns that fal exists.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mcp_manager  # noqa: E402
from gateway.registry import GatewayDescriptor  # noqa: E402
from jobs.detector import ToolDescriptor, classify_and_group  # noqa: E402

FAL_ROLES = {
    "submit_job": {"job_role": "start", "job_group": "fal|job"},
    "check_job": {"job_role": "status", "job_group": "fal|job"},
    "get_job_result": {"job_role": "results", "job_group": "fal|job"},
}


def _row(**extra) -> dict:
    return {
        "id": "harness-user-mcp",
        "name": "Your MCP servers",
        "url": "https://gw/mcp",
        "auth_type": "user_jwt",
        "auth_config": {"auth_type": "user_jwt"},
        "enabled": True,
        **extra,
    }


def _conn(overrides: dict):
    c = mcp_manager.GatewayConnection.__new__(mcp_manager.GatewayConnection)
    c.job_role_overrides = overrides
    return c


def test_roles_survive_the_registry_round_trip():
    d = GatewayDescriptor.from_dict(_row(job_role_overrides=FAL_ROLES))
    assert d.job_role_overrides == FAL_ROLES
    assert d.to_dict()["job_role_overrides"] == FAL_ROLES
    assert d.to_connection_config()["job_role_overrides"] == FAL_ROLES


def test_absent_overrides_add_nothing_to_the_config():
    cfg = GatewayDescriptor.from_dict(_row()).to_connection_config()
    assert "job_role_overrides" not in cfg


def test_declared_role_is_merged_into_meta():
    c = _conn(FAL_ROLES)
    assert c._meta_with_declared_roles("usr-x___submit_job", None) == FAL_ROLES["submit_job"]


def test_a_servers_own_annotation_wins():
    # Config fills a GAP; it must not override what a server says about itself.
    c = _conn(FAL_ROLES)
    server_said = {"job_role": "results", "job_group": "theirs"}
    assert c._meta_with_declared_roles("usr-x___submit_job", server_said) == server_said


def test_existing_meta_keys_are_preserved():
    c = _conn(FAL_ROLES)
    merged = c._meta_with_declared_roles("usr-x___check_job", {"other": "keep me"})
    assert merged["other"] == "keep me"
    assert merged["job_role"] == "status"


def test_undeclared_tools_are_untouched():
    c = _conn(FAL_ROLES)
    assert c._meta_with_declared_roles("usr-x___run_model", None) is None


def test_no_overrides_configured_is_a_no_op():
    c = _conn({})
    assert c._meta_with_declared_roles("usr-x___submit_job", None) is None


def test_two_separator_names_do_not_false_match():
    # Tool names can carry TWO ___ separators (target___aws___call_aws). Splitting
    # with maxsplit=1 means the remainder is "aws___call_aws", which must not match
    # a bare declaration. This bug class has appeared three times in this codebase.
    c = _conn(FAL_ROLES)
    assert c._meta_with_declared_roles("t___aws___call_aws", None) is None


def test_malformed_declaration_is_ignored():
    for bad in ({"submit_job": "start"}, {"submit_job": {}}, {"submit_job": {"job_group": "g"}}):
        assert _conn(bad)._meta_with_declared_roles("usr-x___submit_job", None) is None


def test_group_is_optional():
    c = _conn({"submit_job": {"job_role": "start"}})
    merged = c._meta_with_declared_roles("usr-x___submit_job", None)
    assert merged == {"job_role": "start"}


def test_detector_forms_a_complete_trio_from_declared_roles():
    """The point of the whole change: fal becomes a real job trio."""
    c = _conn(FAL_ROLES)
    names = [
        "usr-x___submit_job",
        "usr-x___check_job",
        "usr-x___get_job_result",
        "usr-x___run_model",
    ]
    descriptors = [
        ToolDescriptor(name=n, meta=c._meta_with_declared_roles(n, None)) for n in names
    ]
    result = classify_and_group(descriptors, gateway_id="harness-user-mcp")
    trios = getattr(result, "trios", result)
    assert len(trios) == 1
    trio = trios[0]
    assert trio.start_tool == "usr-x___submit_job"
    assert trio.status_tool == "usr-x___check_job"
    assert trio.results_tool == "usr-x___get_job_result"
    assert trio.gateway_id == "harness-user-mcp"


def test_without_declared_roles_there_is_no_trio():
    """Proves the change is load-bearing rather than incidental."""
    names = ["usr-x___submit_job", "usr-x___check_job", "usr-x___get_job_result"]
    descriptors = [ToolDescriptor(name=n, meta=None) for n in names]
    result = classify_and_group(descriptors, gateway_id="harness-user-mcp")
    assert len(getattr(result, "trios", result)) == 0
