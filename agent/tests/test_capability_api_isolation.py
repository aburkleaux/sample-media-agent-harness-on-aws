"""The control plane must be importable without the Runtime stack (R1).

The whole point of the migration is that listing skills does not initialize
Strands, a model client, MCP sessions, or skill packages. That guarantee is
worth nothing if a convenience import quietly reintroduces one, and the failure
would be invisible locally (everything is installed here) — it would surface as
the Capability API container or the projection Lambda crash-looping on a
``ModuleNotFoundError`` at cold start.

So these tests import the control plane in a subprocess with the Runtime's
dependencies **blocked at the meta-path level**, which is the same condition
those two deployment targets actually run under:

* the Capability API container installs only fastapi/uvicorn/boto3/PyJWT
  (``agent/capability.Dockerfile``) — no strands, no bedrock-agentcore,
* the projection Lambda runs on the managed ``python3.13`` runtime, which
  bundles boto3 but not PyYAML and not strands.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

#: Runtime-only dependencies absent from the Capability API container and the
#: projection Lambda. `yaml` is included because the managed Lambda runtime does
#: not bundle PyYAML — an import of it at `lab` module scope broke the projector
#: cold start once already.
BLOCKED = ("strands", "strands_tools", "bedrock_agentcore", "yaml")

_HARNESS = textwrap.dedent(
    """
    import importlib.abc, importlib.machinery, sys
    BLOCKED = {blocked!r}

    class Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError(f"BLOCKED at module scope: {{name}}")
            return None

    sys.meta_path.insert(0, Blocker())
    sys.path.insert(0, {src!r})
    {body}
    print("OK")
    """
)


def _run_isolated(body: str) -> subprocess.CompletedProcess:
    script = _HARNESS.format(blocked=BLOCKED, src=str(SRC), body=textwrap.dedent(body))
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )


def _assert_imports(body: str) -> None:
    result = _run_isolated(body)
    assert result.returncode == 0, (
        f"import failed with the Runtime stack blocked:\n{result.stderr[-3000:]}"
    )
    assert "OK" in result.stdout


def test_capability_package_imports_without_the_runtime_stack():
    _assert_imports(
        """
        import capability.dto
        import capability.naming
        import capability.config
        import capability.descriptors
        import capability.registry
        import capability.projection_state
        import capability.projector
        import capability.sources
        import capability.aggregate
        import capability.improve_store
        import capability_api.improve_dto
        """
    )


def test_projection_lambda_entrypoint_imports_without_the_runtime_stack():
    """The exact handler path the Lambda is configured with.

    `capability.projector` imports `lab.asset_index` and `lab.outbox` on
    purpose — reusing the schema constants rather than re-declaring them — so
    this also proves the whole `lab` package stays Runtime-free.
    """
    _assert_imports(
        """
        from capability.projector import handler
        assert callable(handler)
        """
    )


def test_lab_domain_layer_imports_without_the_runtime_stack():
    _assert_imports(
        """
        import lab
        import lab.asset_index, lab.models, lab.outbox
        import lab.skill_crud, lab.specialty_crud
        import lab.tool_collection_crud, lab.workflow_crud, lab.hook_crud
        import lab.service, lab._canonical_ops, lab._lifecycle_ops
        import lab.skill_package_validator
        import lab.purge_scan
        """
    )


def test_the_purge_path_imports_without_the_runtime_stack():
    """The purge route's imports are lazy, so `create_app` cannot prove them.

    Every other Capability API route resolves its dependencies at startup, which
    the test above covers. Purge does not — a `ModuleNotFoundError` here would
    surface only on the one request that matters, and only in the deployed
    container. So the import chain is exercised directly, including the
    `lab.service` visibility resolver the reference scan pulls in on demand.
    """
    _assert_imports(
        """
        from lab._canonical_ops import preflight_asset_purge, purge_asset_lineage
        from lab.purge_scan import (
            scan_agent_references,
            scan_references,
            scan_skill_references,
        )
        from lab.service import list_visible_assets
        from lab.s3_user_files_tool import S3UserFilesTool
        from lab.errors import AuthorityDenied, ConflictError, NotFoundOrForbidden
        from lab.path_validator import PathTraversalError, PrefixMismatchError
        assert callable(purge_asset_lineage) and callable(scan_agent_references)
        assert callable(scan_references) and callable(scan_skill_references)
        """
    )


def test_capability_api_app_imports_without_the_runtime_stack():
    pytest.importorskip("fastapi")
    _assert_imports(
        """
        import capability_api.app
        import capability_api.auth
        import capability_api.lab_actions
        assert hasattr(capability_api.app, "build_app")
        """
    )


def test_the_lab_route_can_be_served_without_the_runtime_stack():
    """The whole chain `POST /control-plane/lab` needs, not just the app.

    The route imports the dispatcher lazily, so neither the import test above nor
    `test_building_the_app_does_not_load_strands` proves a *request* would work —
    both pass while the first call raises ImportError. This exercises what the
    handler actually reaches: the dispatcher, the browser it is given, and the
    error type that browser raises.
    """
    pytest.importorskip("fastapi")
    _assert_imports(
        """
        from capability_api.lab_actions import (
            CONTROL_PLANE_ACTIONS, RUNTIME_ONLY_ACTIONS, is_control_plane,
        )
        from capability_api.app import _refusing_gateway_browser
        from lab.service import LabService
        from lab.errors import GatewayUnavailableError
        from lab.gateway_browser import GatewayBrowser

        # Constructing the browser is part of serving a request, so prove it here
        # rather than only that its module imports.
        assert isinstance(_refusing_gateway_browser(), GatewayBrowser)
        assert is_control_plane("ReadSkillDraft")
        assert not is_control_plane("ChatMessage")
        """
    )


def test_building_the_app_does_not_load_strands():
    """Constructing the app — not just importing it — must stay clean.

    A route registered with a lazily-imported Runtime dependency would pass the
    import test above and still break the first request.
    """
    pytest.importorskip("fastapi")
    _assert_imports(
        """
        import os, sys
        os.environ.update({
            "DEPLOY_ENV": "sandbox",
            "ASSET_INDEX_TABLE": "table",
            "DEV_BYPASS_AUTH": "true",
        })
        from capability_api.app import create_app
        create_app()
        loaded = [m for m in sys.modules if m.split(".")[0] in
                  {"strands", "strands_tools", "bedrock_agentcore"}]
        assert not loaded, loaded
        """
    )


def test_skill_package_validator_still_parses_frontmatter_when_yaml_is_present():
    """The lazy PyYAML import must not have broken validation itself.

    Moving an import to call scope is the kind of change that silently converts
    a working function into a NameError on the one path nobody runs locally.
    """
    from lab.skill_package_validator import validate_skill_document

    parsed = validate_skill_document(
        "---\nname: video-processing\ndescription: Do the thing\n---\nBody text.\n",
        expected_name="video-processing",
    )
    assert parsed["name"] == "video-processing"
