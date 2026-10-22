"""Regression guard for the 2026-09-08 prod outage (all mediaworks_harness_* runtimes).

`mcp` was absent from the agent's DEPLOYMENT requirements and `strands-agents` was a loose
floor, so the CDK zip-packager (which builds the runtime package with `pip download` over
``agent/src/requirements.txt`` — NOT ``uv.lock``, and NOT ``agent/requirements.txt``, which is
only the CI pytest deps) resolved a transitive mcp < 1.8.0. That version no longer exports
``mcp.client.streamable_http.streamablehttp_client``; both entrypoints import it at module scope
(``mcp_manager.py`` -> ``main.py`` / ``task_agent.py``), so every container crashed at import:

    ImportError: cannot import name 'streamablehttp_client' from 'mcp.client.streamable_http'

These tests pin the contract on the file the deploy actually reads, keep the two requirements
files in lockstep, and assert the symbol is importable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_AGENT = Path(__file__).parents[1]
# The DEPLOYMENT package requirements — what the zip-packager pip-downloads (agentSrcDir).
DEPLOY_REQUIREMENTS = _AGENT / "src" / "requirements.txt"
# The CI pytest requirements — kept in lockstep so tests reflect the deployed runtime.
TEST_REQUIREMENTS = _AGENT / "requirements.txt"


def _specs(path: Path) -> dict:
    from packaging.requirements import Requirement

    specs: dict = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            req = Requirement(line)
        except Exception:  # noqa: BLE001 — a non-requirement line is simply skipped
            continue
        specs[req.name.lower()] = req
    return specs


def _min_lower_bound(spec) -> "object | None":
    from packaging.version import Version

    lowers = [Version(s.version) for s in spec.specifier if s.operator in (">=", "==", "~=")]
    return min(lowers) if lowers else None


def test_deploy_requirements_pin_mcp_explicit_floor() -> None:
    from packaging.version import Version

    specs = _specs(DEPLOY_REQUIREMENTS)
    assert "mcp" in specs, (
        "mcp must be EXPLICITLY pinned in agent/src/requirements.txt (the DEPLOYMENT package). "
        "Leaving it transitive let the deploy build resolve a bad version (2026-09-08 outage)."
    )
    floor = _min_lower_bound(specs["mcp"])
    assert floor is not None, f"mcp needs a lower-bound pin; got {specs['mcp'].specifier!r}"
    # Reviewed floor for the MCP 2.x / Strands 1.55.x pair (Wave 1).
    assert floor >= Version("2.1.1"), (
        f"mcp floor must be >= 2.1.1 (reviewed MCP 2.x pair); got {specs['mcp'].specifier!r}"
    )


def test_deploy_requirements_pin_the_agent_sdks() -> None:
    # Loose strands-agents (>=1.13.0) was the transitive lever that dragged mcp to a bad version;
    # an unpinned bedrock-agentcore compounds it. Both must be version-pinned in the deploy file.
    specs = _specs(DEPLOY_REQUIREMENTS)
    for pkg in ("strands-agents", "bedrock-agentcore"):
        assert pkg in specs, f"{pkg} must be present in agent/src/requirements.txt"
        assert str(specs[pkg].specifier), f"{pkg} must be version-pinned (was a bare/loose floor)"


def test_requirements_files_are_in_lockstep_on_mcp() -> None:
    # The deploy file and the CI-test file must both pin mcp, so tests reflect what deploys.
    for path in (DEPLOY_REQUIREMENTS, TEST_REQUIREMENTS):
        specs = _specs(path)
        assert "mcp" in specs, f"mcp must be pinned in {path.relative_to(_AGENT.parent)}"


def test_mcpclient_public_url_headers_interface() -> None:
    # Wave 1: mcp_manager constructs MCPClient(url=, headers=) instead of the
    # MCP-1.x-only streamablehttp_client factory (removed in MCP 2.x). Guard the
    # public interface the harness now depends on.
    import inspect

    from strands.tools.mcp import MCPClient

    params = inspect.signature(MCPClient.__init__).parameters
    assert "url" in params and "headers" in params, list(params)


def test_mcp_manager_imports_cleanly() -> None:
    import importlib

    pytest.importorskip("strands")
    importlib.import_module("mcp_manager")


# --- Wave 0 (Req 12.4) lockstep guards ------------------------------------
# The lower-floor guards above prevented the 2026-09-08 mcp<1.8.0 crash. These
# add the UPPER bound + full-manifest agreement so mcp cannot silently float to
# 2.x before the controlled Strands/MCP 2.x upgrade (Wave 1), and so pyproject,
# both requirements files, and uv.lock stay in lockstep.

PYPROJECT = _AGENT / "pyproject.toml"
UV_LOCK = _AGENT / "uv.lock"


def _pyproject_specs() -> dict:
    import tomllib

    from packaging.requirements import Requirement

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    specs: dict = {}
    for line in data.get("project", {}).get("dependencies", []):
        try:
            req = Requirement(line)
        except Exception:  # noqa: BLE001
            continue
        specs[req.name.lower()] = req
    return specs


def _lock_versions() -> dict:
    import tomllib

    data = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    return {p["name"].lower(): p["version"] for p in data.get("package", [])}


def test_mcp_pinned_to_2_1_series_in_every_manifest() -> None:
    """Wave 1: mcp is pinned to the reviewed 2.1.x series across all manifests.

    Admits 2.1.1, excludes 2.2.0 (outside Strands 1.55.1's declared range) and
    any 2.0.x / 1.x regression.
    """
    from packaging.version import Version

    sources = {
        "pyproject.toml": _pyproject_specs(),
        "agent/requirements.txt": _specs(TEST_REQUIREMENTS),
        "agent/src/requirements.txt": _specs(DEPLOY_REQUIREMENTS),
    }
    for label, specs in sources.items():
        assert "mcp" in specs, f"mcp must be pinned in {label}"
        spec = specs["mcp"].specifier
        assert Version("2.1.1") in spec, f"{label} must admit mcp 2.1.1 ({spec!r})"
        assert Version("2.2.0") not in spec, f"{label} must exclude mcp 2.2.0 ({spec!r})"
        assert Version("2.0.5") not in spec and Version("1.26.0") not in spec, (
            f"{label} must not allow a pre-2.1 regression ({spec!r})"
        )


def test_mcp_specifier_identical_across_manifests() -> None:
    from packaging.specifiers import SpecifierSet

    seen = {
        str(SpecifierSet(str(_pyproject_specs()["mcp"].specifier))),
        str(SpecifierSet(str(_specs(TEST_REQUIREMENTS)["mcp"].specifier))),
        str(SpecifierSet(str(_specs(DEPLOY_REQUIREMENTS)["mcp"].specifier))),
    }
    assert len(seen) == 1, f"mcp specifier drifted across manifests: {seen}"


def test_uv_lock_resolves_within_declared_bounds() -> None:
    from packaging.version import Version

    versions = _lock_versions()
    assert "mcp" in versions, "uv.lock has no mcp package"
    mcp_v = Version(versions["mcp"])
    assert mcp_v in _specs(DEPLOY_REQUIREMENTS)["mcp"].specifier, (
        f"uv.lock mcp {mcp_v} is outside the deploy specifier"
    )
    assert Version("2.1.1") <= mcp_v < Version("2.2.0"), f"uv.lock mcp {mcp_v} not in the 2.1.x series"
    strands_pin = _pyproject_specs()["strands-agents"].specifier
    assert Version(versions["strands-agents"]) in strands_pin, (
        f"uv.lock strands-agents {versions['strands-agents']} != pyproject pin {strands_pin}"
    )
