"""LabService — the single backend module that fronts every Lab operation.

Binds LabRequestContext at construction, instantiates all storage and CRUD
components, and routes incoming payloads to the appropriate handler based on
the `action` field.

The `actor_id` field is NEVER read from the payload. If the client includes
one, it is dropped before the typed payload is passed to the operation.
This enforces Property 3 / Requirement 1.6 at the API layer.

Configuration is loaded from environment variables:
- ASSET_INDEX_TABLE: DynamoDB table name for the Asset_Index
- ARTIFACTS_BUCKET: S3 bucket name for draft content and lab run outputs
- GATEWAY_ENDPOINTS: Comma-separated list of Gateway endpoint URLs

Requirements: 1.1, 1.6
Properties: 3
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
from typing import Any, Literal, Optional

import boto3
from botocore.exceptions import ClientError

from .asset_index import (
    AssetIndex,
    make_pk_group_shared,
    make_pk_org_shared,
    make_pk_user_owned,
    make_pk_user_versions,
    make_sk_owned,
)
from .chat_agent import LabChatAgent
from .context import LabRequestContext
from .gateway_browser import GatewayBrowser, GatewayClient, GatewayEndpointConfig, ToolInvocationResult
from .models import (
    GatewayToolDescriptor,
    SampleRef,
    ToolRef,
    WorkflowStep,
    AgentRef,
    SkillRef,
    ToolCollectionRef,
)
from .readiness import PromotionReadiness
from .s3_user_files_tool import S3UserFilesTool
from .skill_crud import SkillCRUD
from .skill_package_validator import (
    MAX_FILE_BYTES,
    MAX_PACKAGE_BYTES,
    MAX_PACKAGE_FILES,
)
from .hook_crud import HookCRUD
from .specialty_crud import SpecialtyCRUD
from .test_harness import TestHarness
from .tool_collection_crud import ToolCollectionCRUD
from .workflow_crud import WorkflowCRUD

logger = logging.getLogger(__name__)

# Environment variable names
_ENV_ASSET_INDEX_TABLE = "ASSET_INDEX_TABLE"
_ENV_ARTIFACTS_BUCKET = "ARTIFACTS_BUCKET"
_ENV_GATEWAY_ENDPOINTS = "GATEWAY_ENDPOINTS"


class _DefaultGatewayClient:
    """MCP-based Gateway client that connects to the AgentCore Gateway.

    Uses OAuth2 client_credentials flow for authentication, then speaks
    MCP JSON-RPC to list tools and invoke them.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at: float = 0

    def _get_token(self) -> str:
        """Get a valid OAuth token, refreshing if expired."""
        import time as _time
        if self._token and _time.time() < self._token_expires_at:
            return self._token

        import urllib.request
        import urllib.parse
        import json as _json

        token_endpoint = os.environ.get("M2M_TOKEN_ENDPOINT", "")
        client_id = os.environ.get("M2M_CLIENT_ID", "")
        client_secret = os.environ.get("M2M_CLIENT_SECRET", "")
        scope = os.environ.get("M2M_SCOPE", "")

        if not all([token_endpoint, client_id, client_secret, scope]):
            from .errors import GatewayUnavailableError
            raise GatewayUnavailableError("OAuth credentials not configured for Gateway")

        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
        }).encode()

        req = urllib.request.Request(
            token_endpoint,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())

        self._token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        self._token_expires_at = _time.time() + expires_in - 300  # refresh 5 min early
        return self._token

    def _mcp_call(self, gateway_url: str, method: str, params: dict | None, session_id: str | None, timeout: float) -> dict:
        """Make a JSON-RPC call to the Gateway MCP endpoint."""
        import urllib.request
        import json as _json

        token = self._get_token()
        body_dict: dict = {
            "jsonrpc": "2.0",
            "id": f"{method}-{id(self)}",
            "method": method,
        }
        if params:
            body_dict["params"] = params

        body = _json.dumps(body_dict).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if session_id:
            headers["mcp-session-id"] = session_id

        req = urllib.request.Request(
            gateway_url,
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_headers = dict(resp.headers)
            resp_body = _json.loads(resp.read())
            return {"body": resp_body, "headers": resp_headers}

    def list_tools(self, actor_id: str, timeout: float) -> list[GatewayToolDescriptor]:
        """Query the Gateway for available tools via MCP protocol."""
        from .errors import GatewayUnavailableError

        gateway_url = os.environ.get("GATEWAY_ENDPOINTS", "")
        if not gateway_url:
            raise GatewayUnavailableError("GATEWAY_ENDPOINTS not configured")

        try:
            # Step 1: Initialize MCP session
            init_resp = self._mcp_call(
                gateway_url, "initialize",
                {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "lab-service", "version": "1.0.0"}},
                None, timeout
            )
            session_id = init_resp["headers"].get("mcp-session-id", "")

            # Step 2: Send initialized notification
            import urllib.request
            import json as _json
            token = self._get_token()
            notif_body = _json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
            notif_headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
            if session_id:
                notif_headers["mcp-session-id"] = session_id
            notif_req = urllib.request.Request(gateway_url, data=notif_body, headers=notif_headers, method="POST")
            urllib.request.urlopen(notif_req, timeout=timeout)

            # Step 3: List tools (with pagination)
            all_tools: list[GatewayToolDescriptor] = []
            cursor: str | None = None
            while True:
                params: dict = {}
                if cursor:
                    params["cursor"] = cursor
                list_resp = self._mcp_call(gateway_url, "tools/list", params or None, session_id, timeout)
                result = list_resp["body"].get("result", {})
                tools = result.get("tools", [])
                for t in tools:
                    all_tools.append(GatewayToolDescriptor(
                        name=t.get("name", ""),
                        description=t.get("description", ""),
                        version="1.0",
                        input_schema=t.get("inputSchema"),
                    ))
                cursor = result.get("nextCursor")
                if not cursor:
                    break

            return all_tools

        except Exception as exc:
            raise GatewayUnavailableError(f"Gateway MCP call failed: {exc}") from exc

    def call_tool(self, tool_name: str, tool_version: str, input_data: Any, timeout: float) -> "ToolInvocationResult":
        """Invoke a single Gateway tool via MCP."""
        from .errors import GatewayUnavailableError
        from .gateway_browser import ToolInvocationResult
        import time as _time

        gateway_url = os.environ.get("GATEWAY_ENDPOINTS", "")
        if not gateway_url:
            raise GatewayUnavailableError("GATEWAY_ENDPOINTS not configured")

        try:
            start = _time.time()
            # Initialize session first
            init_resp = self._mcp_call(
                gateway_url, "initialize",
                {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "lab-service", "version": "1.0.0"}},
                None, timeout
            )
            session_id = init_resp["headers"].get("mcp-session-id", "")

            # Call the tool
            call_resp = self._mcp_call(
                gateway_url, "tools/call",
                {"name": tool_name, "arguments": input_data if isinstance(input_data, dict) else {}},
                session_id, timeout
            )
            duration_ms = int((_time.time() - start) * 1000)
            result = call_resp["body"].get("result", {})
            return ToolInvocationResult(
                response_body=result,
                status=200,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            raise GatewayUnavailableError(f"Tool invocation failed: {exc}") from exc


class LabService:
    """The Lab_Service entrypoint that routes all Lab operations.

    Constructor binds LabRequestContext, instantiates S3UserFilesTool,
    AssetIndex, all CRUD classes, and GatewayBrowser. The handle() method
    dispatches to the appropriate handler based on the `action` field in
    the payload.

    The actor_id is bound once at construction from the LabRequestContext.
    Downstream methods never re-read it from any payload (Property 3 /
    Requirement 1.6 enforced structurally).

    Args:
        ctx: The LabRequestContext bound from the validated JWT.
        s3: The S3UserFilesTool instance (bound to ctx.actor_id).
        index: The AssetIndex instance (bound to ctx.actor_id).
        gateway_browser: The GatewayBrowser instance for tool browsing.
    """

    def __init__(
        self,
        ctx: LabRequestContext,
        s3: S3UserFilesTool,
        index: AssetIndex,
        gateway_browser: GatewayBrowser,
    ) -> None:
        self.ctx = ctx
        self.s3 = s3
        self.index = index
        self.gateway_browser = gateway_browser

        # Instantiate CRUD classes
        self.specialty_crud = SpecialtyCRUD(s3=s3, index=index)
        self.skill_crud = SkillCRUD(
            s3=s3, index=index, specialty_crud=self.specialty_crud
        )
        self.tool_collection_crud = ToolCollectionCRUD(
            s3=s3, index=index, gateway_browser=gateway_browser
        )
        self.workflow_crud = WorkflowCRUD(s3=s3, index=index)
        self.hook_crud = HookCRUD(index=index)

        # Instantiate chat agent
        self.chat_agent = LabChatAgent(self)

        # Instantiate test harness
        self.test_harness = TestHarness(
            s3=s3, index=index, gateway_browser=gateway_browser
        )

        # Instantiate promotion readiness checker
        self.readiness = PromotionReadiness(
            s3=s3, index=index, gateway_browser=gateway_browser
        )

        # Build the action dispatch table
        self._action_handlers: dict[str, Any] = {
            # Skills
            "CreateSkillDraft": self._handle_create_skill_draft,
            "ImportSkillPackage": self._handle_import_skill_package,
            "ReadSkillDraft": self._handle_read_skill_draft,
            "ReadPublishedSkill": self._handle_read_published_skill,
            "UpdateSkillMd": self._handle_update_skill_md,
            "PutSupportingFile": self._handle_put_supporting_file,
            "DeleteSupportingFile": self._handle_delete_supporting_file,
            "DeleteSkillDraft": self._handle_delete_skill_draft,
            "ListSkillDrafts": self._handle_list_skill_drafts,
            # Specialty Agents
            "CreateSpecialtyAgentDraft": self._handle_create_specialty_agent_draft,
            "ReadSpecialtyAgentDraft": self._handle_read_specialty_agent_draft,
            "UpdateSpecialtyAgentDraft": self._handle_update_specialty_agent_draft,
            "DeleteSpecialtyAgentDraft": self._handle_delete_specialty_agent_draft,
            "ListSpecialtyAgentDrafts": self._handle_list_specialty_agent_drafts,
            "ResolveAgentConfig": self._handle_resolve_agent_config,
            "ResolvePublishedAgentRef": self._handle_resolve_published_agent_ref,
            # Tool Collections
            "CreateToolCollectionDraft": self._handle_create_tool_collection_draft,
            "ReadToolCollectionDraft": self._handle_read_tool_collection_draft,
            "UpdateToolCollectionDraft": self._handle_update_tool_collection_draft,
            "DeleteToolCollectionDraft": self._handle_delete_tool_collection_draft,
            "ListToolCollectionDrafts": self._handle_list_tool_collection_drafts,
            # Workflows
            "CreateWorkflowDraft": self._handle_create_workflow_draft,
            "ReadWorkflowDraft": self._handle_read_workflow_draft,
            "UpdateWorkflowDraft": self._handle_update_workflow_draft,
            "DeleteWorkflowDraft": self._handle_delete_workflow_draft,
            "ListWorkflowDrafts": self._handle_list_workflow_drafts,
            # Hooks (declarative harness triggers)
            "CreateHook": self._handle_create_hook,
            "ReadHook": self._handle_read_hook,
            "UpdateHook": self._handle_update_hook,
            "DeleteHook": self._handle_delete_hook,
            "ListHooks": self._handle_list_hooks,
            "ValidateHookCondition": self._handle_validate_hook_condition,
            # Gateway
            "BrowseGatewayTools": self._handle_browse_gateway_tools,
            "RefreshGatewayTools": self._handle_refresh_gateway_tools,
            "TestGatewayTool": self._handle_test_gateway_tool,
            # Chat
            "ChatMessage": self._handle_chat_message,
            # Harness
            "RunHarness": self._handle_run_harness,
            "ListHarnessRuns": self._handle_list_harness_runs,
            "ReadHarnessRun": self._handle_read_harness_run,
            # Readiness
            "CheckReadiness": self._handle_check_readiness,
            # Generic
            "RenameAsset": self._handle_rename_asset,
            # Lifecycle (asset-lifecycle-redesign): publish / edit / discard
            # operate on any category via a single dispatch (category passed
            # in payload). This keeps the surface compact on both sides
            # while reusing the per-category CRUDs / canonical-ops layer.
            "PublishAsset": self._handle_publish_asset,
            "EditAsset": self._handle_edit_asset,
            "DiscardChanges": self._handle_discard_changes,
            "ListAssetVersions": self._handle_list_asset_versions,
            "ListMyPublishedAssets": self._handle_list_my_published_assets,
        }

    async def handle(self, payload: dict) -> Any:
        """Route an incoming payload to the appropriate handler.

        Drops any `actor_id` field from the payload before dispatching
        (Property 3 / Requirement 1.6). Routes based on the `action` field.

        Args:
            payload: The incoming request payload dict. Must contain an
                `action` field identifying the operation.

        Returns:
            The result from the dispatched handler.

        Raises:
            ValueError: If `action` is missing or unrecognized.
        """
        # Drop actor_id from payload — Property 3 / Requirement 1.6
        # The actor_id is ONLY sourced from LabRequestContext (bound from JWT).
        sanitized = {k: v for k, v in payload.items() if k != "actor_id"}

        action = sanitized.pop("action", None)
        if not action:
            raise ValueError("Payload must contain an 'action' field")

        handler = self._action_handlers.get(action)
        if handler is None:
            raise ValueError(f"Unrecognized action: '{action}'")

        logger.info(
            "Handling action=%s for actor=%s request=%s",
            action,
            self.ctx.actor_id,
            self.ctx.request_id,
        )

        result = await handler(sanitized)

        # Defensive: a handler that returns an un-awaited coroutine would
        # otherwise propagate to the entrypoint's ``json.dumps(result)`` and
        # crash the whole turn with "Object of type coroutine is not JSON
        # serializable" (observed in prod). Await it here so one mis-wired
        # handler can't take down the request.
        import inspect
        while inspect.iscoroutine(result):
            result = await result

        # Convert Decimal values from DynamoDB to JSON-safe types
        return self._make_json_safe(result)

    @staticmethod
    def _make_json_safe(obj):
        """Recursively convert Decimal and other non-JSON types to JSON-safe equivalents."""
        from decimal import Decimal
        if isinstance(obj, Decimal):
            if obj == int(obj):
                return int(obj)
            return float(obj)
        elif isinstance(obj, dict):
            return {k: LabService._make_json_safe(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [LabService._make_json_safe(item) for item in obj]
        return obj

    # -----------------------------------------------------------------------
    # Skill handlers
    # -----------------------------------------------------------------------

    async def _handle_create_skill_draft(self, params: dict) -> dict:
        result = await self.skill_crud.create(
            ctx=self.ctx,
            name=params["name"],
            skill_md=params["skillMd"],
        )
        return {"assetId": result.asset_id, "s3Prefix": result.s3_prefix}

    async def _handle_import_skill_package(self, params: dict) -> dict:
        name = params.get("name")
        skill_md = params.get("skillMd")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string")
        if not isinstance(skill_md, str) or not skill_md.strip():
            raise ValueError("skillMd must be a non-empty string")

        skill_md_bytes = skill_md.encode("utf-8")
        if len(skill_md_bytes) > MAX_FILE_BYTES:
            raise ValueError(
                f"SKILL.md exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit"
            )

        raw_files = params.get("files", [])
        if not isinstance(raw_files, list):
            raise ValueError("files must be a list")
        max_supporting_files = MAX_PACKAGE_FILES - 1
        if len(raw_files) > max_supporting_files:
            raise ValueError(
                f"Skill package may contain at most {MAX_PACKAGE_FILES} files"
            )

        encoded_files: list[tuple[str, str]] = []
        estimated_package_bytes = len(skill_md_bytes)
        max_encoded_file_bytes = ((MAX_FILE_BYTES + 2) // 3) * 4
        for entry in raw_files:
            if not isinstance(entry, dict):
                raise ValueError("Each package file must be an object")
            path = entry.get("path")
            content_base64 = entry.get("contentBase64")
            if not isinstance(path, str) or not isinstance(content_base64, str):
                raise ValueError("Each package file requires path and contentBase64 strings")
            if len(content_base64) > max_encoded_file_bytes:
                raise ValueError(
                    f"Package file '{path}' exceeds the "
                    f"{MAX_FILE_BYTES // (1024 * 1024)} MB limit"
                )

            padding = len(content_base64) - len(content_base64.rstrip("="))
            estimated_decoded_bytes = (
                (len(content_base64) // 4) * 3 - min(padding, 2)
                if len(content_base64) % 4 == 0
                else ((len(content_base64) + 3) // 4) * 3
            )
            estimated_package_bytes += estimated_decoded_bytes
            if estimated_package_bytes > MAX_PACKAGE_BYTES:
                raise ValueError(
                    f"Skill package exceeds the "
                    f"{MAX_PACKAGE_BYTES // (1024 * 1024)} MB limit"
                )
            encoded_files.append((path, content_base64))

        supporting_files: list[tuple[str, bytes]] = []
        for path, content_base64 in encoded_files:
            try:
                content = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError, TypeError) as exc:
                raise ValueError(f"Package file '{path}' has invalid base64 content") from exc
            supporting_files.append((path, content))

        result = await self.skill_crud.import_package(
            ctx=self.ctx,
            name=name,
            skill_md=skill_md,
            supporting_files=supporting_files,
        )
        return {
            "assetId": result.asset_id,
            "s3Prefix": result.s3_prefix,
            "supportingFiles": [path for path, _content in supporting_files],
        }

    async def _handle_read_skill_draft(self, params: dict) -> Any:
        result = await self.skill_crud.read(
            ctx=self.ctx,
            asset_id=params["assetId"],
        )
        if result is None:
            return {"error": "not_found"}
        return {
            "assetId": result.asset_id,
            "name": result.name,
            "skillMd": result.skill_md,
            "supportingFiles": result.supporting_files,
            "metadata": result.metadata,
        }

    async def _handle_read_published_skill(self, params: dict) -> Any:
        """Read an immutable published Skill version.

        Separate action rather than a flag on ``ReadSkillDraft`` because the
        CRUD methods are separate for a contractual reason: ``read`` is
        draft-only and keys the OWNED partition, which publishing empties. An
        absent ``version`` resolves the current version, matching the rule the
        invoke path uses.
        """
        version = params.get("version")
        result = await self.skill_crud.read_published(
            ctx=self.ctx,
            asset_id=params["assetId"],
            version=int(version) if version is not None else None,
        )
        if result is None:
            return {"error": "not_found"}
        return {
            "assetId": result.asset_id,
            "name": result.name,
            "skillMd": result.skill_md,
            "supportingFiles": result.supporting_files,
            "metadata": result.metadata,
        }

    async def _handle_update_skill_md(self, params: dict) -> Any:
        result = await self.skill_crud.update_skill_md(
            ctx=self.ctx,
            asset_id=params["assetId"],
            skill_md=params["skillMd"],
        )
        if result is None:
            return {"error": "not_found"}
        return result

    async def _handle_put_supporting_file(self, params: dict) -> Any:
        content = params.get("content", b"")
        if isinstance(content, str):
            content = content.encode("utf-8")
        result = await self.skill_crud.put_supporting_file(
            ctx=self.ctx,
            asset_id=params["assetId"],
            path=params["path"],
            content=content,
        )
        if result is None:
            return {"error": "not_found"}
        return result

    async def _handle_delete_supporting_file(self, params: dict) -> Any:
        result = await self.skill_crud.delete_supporting_file(
            ctx=self.ctx,
            asset_id=params["assetId"],
            path=params["path"],
        )
        if result is None:
            return {"error": "not_found"}
        return {"status": "ok"}

    async def _handle_delete_skill_draft(self, params: dict) -> Any:
        result = await self.skill_crud.delete(
            ctx=self.ctx,
            asset_id=params["assetId"],
        )
        if result is None:
            return {"error": "not_found"}
        return {"status": "ok"}

    async def _handle_list_skill_drafts(self, params: dict) -> list[dict]:
        # SkillCRUD.list returns typed Skill records under the new
        # AssetIndex API; the wire shape is a list of dicts.
        skills = await self.skill_crud.list(ctx=self.ctx)
        package_files = await self.skill_crud.list_supporting_files(
            [skill.asset_id for skill in skills]
        )
        return [
            {
                "assetId": s.asset_id,
                "category": s.category,
                "name": s.name,
                # Kept in parity with the Capability API's summary, which now
                # carries it too: the inventory row shows what a skill is FOR,
                # and a category read through either source must answer the
                # same question. Blank collapses to None so "the author left it
                # empty" and "this source cannot say" reach the client as one
                # absent field rather than as `""`.
                "description": (s.description or "").strip() or None,
                "status": s.status,
                "scope": s.scope,
                "s3Prefix": s.s3_prefix,
                "createdAt": s.created_at,
                "updatedAt": s.updated_at,
                "skillMd": "",
                "supportingFiles": package_files.get(s.asset_id, []),
            }
            for s in skills
        ]

    # -----------------------------------------------------------------------
    # Specialty Agent handlers
    # -----------------------------------------------------------------------

    async def _handle_create_specialty_agent_draft(self, params: dict) -> dict:
        from .models import _integration_config_from_item

        integration_config = _integration_config_from_item(
            params.get("integrationConfig")
        )
        result = await self.specialty_crud.create(
            ctx=self.ctx,
            name=params["name"],
            persona=params["persona"],
            compatible_skills=params.get("compatibleSkills"),
            model=params.get("model"),
            tool_collections=params.get("toolCollections"),
            integration_config=integration_config,
            description=params.get("description", ""),
        )
        return {"assetId": result.asset_id, "s3Prefix": result.s3_prefix}

    async def _handle_read_specialty_agent_draft(self, params: dict) -> Any:
        result = await self.specialty_crud.read(
            ctx=self.ctx,
            asset_id=params["assetId"],
        )
        if result is None:
            return {"error": "not_found"}
        from .models import _integration_config_to_item

        return {
            "assetId": result.asset_id,
            "name": result.name,
            "description": result.description,
            "persona": result.persona,
            "compatibleSkills": result.compatible_skills,
            "model": result.model,
            "toolCollections": result.tool_collections,
            "integrationConfig": _integration_config_to_item(
                result.integration_config
            ),
            "metadata": result.metadata,
        }

    async def _handle_resolve_agent_config(self, params: dict) -> Any:
        """Resolve what a Custom Agent would actually bind at invoke time.

        Powers the dev-portal test console's "resolved config" block: the
        owner's question is *which tools and skills actually load*, which no
        invocation can answer (``execution_summary.tools`` reports tools
        **called**, not bound — a bound-but-unused tool is invisible there).

        Runs the SAME pure resolver the invoke paths use
        (:func:`custom_agent_resolver.resolve_config`) against the SAME
        gateway projection the catalog and the tool-collection validator use
        (``gateway_browser``, registry-backed via ``mcp_manager``), so the
        answer matches a real run rather than approximating it.

        Costs no model tokens — it is pure resolution, no runtime invoke.

        Params:
            assetId: The agent's draft asset id, or published id when
                ``source == "published"``.
            source: ``"draft"`` (default) or ``"published"``.
            version: Optional explicit published version.

        Returns a config summary. Deliberately does **not** include the persona
        text: the published read-only view intentionally withholds it (an owner
        must fork to see it), and this action must not become a way around that.
        """
        from custom_agent_resolver import resolve_config

        asset_id = params.get("assetId") or ""
        source = params.get("source") or "draft"
        version = params.get("version")
        if not asset_id:
            return {"error": "invalid_params", "detail": "assetId is required"}
        if source not in ("draft", "published"):
            return {
                "error": "invalid_params",
                "detail": "source must be 'draft' or 'published'",
            }

        if source == "published":
            agent = await self.specialty_crud.read_published(
                ctx=self.ctx, asset_id=asset_id, version=version
            )
        else:
            agent = await self.specialty_crud.read(
                ctx=self.ctx, asset_id=asset_id
            )
        if agent is None:
            return {"error": "not_found"}

        # ---- Tool collections: the version's pin, else read each live ----
        # A published version carries its own frozen copy of the tool refs, and
        # that is what an invoke binds — so the console has to use it too, or it
        # would confidently report a tool set no invocation would ever use (and
        # would report a since-deleted collection as dropped when the real
        # version still runs it). ``is None`` distinguishes an unpinned row from
        # one pinned to no collections; see
        # ``lab.models.SpecialtyAgent.tool_collection_snapshots``.
        #
        # Drafts fall through to the live read, which is correct: a working copy
        # is meant to track the collections the owner is editing right now.
        # A collection the owner selected but which no longer reads (deleted,
        # cross-actor) is passed through as unavailable so the resolver reports
        # it in ``dropped_collections`` rather than silently yielding no tools.
        collections: list[dict] = []
        if agent.tool_collection_snapshots is not None:
            collections = [
                {
                    "id": snap.asset_id,
                    "name": snap.name,
                    "tool_refs": [
                        {"name": r.name, "version": r.version}
                        for r in snap.tool_refs
                    ],
                }
                for snap in agent.tool_collection_snapshots
            ]
        else:
            for cid in agent.tool_collections or []:
                try:
                    tc = await self.tool_collection_crud.read(
                        ctx=self.ctx, asset_id=cid
                    )
                except Exception:  # noqa: BLE001 — unreadable == unavailable here
                    logger.warning(
                        "ResolveAgentConfig: tool collection %s read failed", cid,
                        exc_info=True,
                    )
                    tc = None
                if tc is None:
                    collections.append({"id": cid, "tool_refs": None})
                    continue
                collections.append(
                    {
                        "id": cid,
                        "name": tc.name,
                        "tool_refs": [
                            {"name": r.name, "version": r.version}
                            for r in tc.tool_refs
                        ],
                    }
                )

        # ---- Available tool names from the live gateway projection ----
        # ``None`` disables availability filtering in the resolver, which would
        # over-report bindable tools. Distinguish "projection unavailable" from
        # "projection empty" so the UI can say which it is instead of showing a
        # misleading empty tool list.
        available: Optional[set[str]] = None
        projection_ok = True
        try:
            projection = self.gateway_browser.get_projection(self.ctx)
            if projection is None:
                projection = self.gateway_browser.browse(self.ctx).projection
            if projection is not None:
                available = {t.name for t in projection.tools}
            else:
                projection_ok = False
        except Exception:  # noqa: BLE001 — degrade to "unknown", never 500
            logger.warning(
                "ResolveAgentConfig: gateway projection unavailable", exc_info=True
            )
            projection_ok = False

        resolved = resolve_config(
            persona=agent.persona or None,
            model=agent.model,
            skills=None,  # skill bodies aren't needed to report which bind
            tool_collections=collections,
            available_tool_names=available,
            known_model_ids=None,  # catalog check belongs to the runtime
            compatible_skills=None,
        )

        return {
            "assetId": agent.asset_id,
            "name": agent.name,
            "source": source,
            "version": agent.metadata.get("version"),
            "modelId": resolved.model_id or None,
            "modelSource": "pinned" if resolved.model_id else "runtime-default",
            "tools": resolved.tools,
            "skills": list(agent.compatible_skills or []),
            "droppedCollections": resolved.dropped_collections,
            "toolCollections": [
                {"id": c["id"], "name": c.get("name")} for c in collections
            ],
            "personaChars": len(agent.persona or ""),
            "gatewayProjectionAvailable": projection_ok,
        }

    async def _handle_resolve_published_agent_ref(self, params: dict) -> Any:
        """Resolve a published Custom Agent reference for delegation.

        Answers "does this agent exist, is it mine, and which version would
        run" — and nothing else. Tool/skill resolution is deliberately absent:
        ``resolve_config`` filters against the tool names bindable on the
        runtime it runs on, and this process is the orchestrator, whose
        builtin set and gateway connections differ from the Task Agent's.

        Version selection comes from ``read_published``, whose rule is
        deliberately in lockstep with ``task_agent._pick_lineage_row``. The
        resolved integer is put on the wire so the runtime's own selection
        becomes a no-op and the two can never disagree.
        """
        asset_id = params.get("assetId") or ""
        version = params.get("version")
        if not asset_id:
            return {"error": "agent_not_found", "message": "assetId is required"}

        agent = await self.specialty_crud.read_published(
            ctx=self.ctx, asset_id=asset_id, version=version
        )
        if agent is None:
            if version is not None:
                return {
                    "error": "version_not_found",
                    "message": (
                        f"No published version {version} of agent {asset_id}."
                    ),
                    "assetId": asset_id,
                    "version": version,
                }
            # Distinguish "never published" from "does not exist": the remedies
            # differ (publish it vs. check the id), and naming the right one is
            # what lets the model self-correct in a single turn. `read` is
            # draft-only and actor-keyed, so a cross-actor draft is invisible
            # here too — no existence disclosure.
            draft = await self.specialty_crud.read(
                ctx=self.ctx, asset_id=asset_id
            )
            if draft is not None:
                return {
                    "error": "agent_not_published",
                    "message": (
                        f"Custom Agent {asset_id} ('{draft.name}') exists as a "
                        "draft but has never been published. Publish it first, "
                        "or test it from the agent editor's Test panel."
                    ),
                    "assetId": asset_id,
                }
            return {
                "error": "agent_not_found",
                "message": (
                    f"No published Custom Agent {asset_id} owned by you. "
                    'Check the id with lab_asset(action="list_custom_agents").'
                ),
                "assetId": asset_id,
            }

        resolved_version = agent.metadata.get("version")
        # _pick_published_row falls back to current when an explicit version
        # doesn't exist (matching task_agent behavior for leniency). For
        # validation, we want strict: if the caller asked for v99 and we resolved
        # v2, that's version_not_found, not "close enough".
        if version is not None and resolved_version != version:
            return {
                "error": "version_not_found",
                "message": (
                    f"No published version {version} of agent {asset_id}."
                ),
                "assetId": asset_id,
                "version": version,
            }
        return {
            "assetId": agent.asset_id,
            "name": agent.name,
            "description": agent.description,
            "version": resolved_version,
        }

    async def _handle_update_specialty_agent_draft(self, params: dict) -> Any:
        from .specialty_crud import _UNSET
        from .models import _integration_config_from_item

        # ``model`` and ``integrationConfig`` use a sentinel so an omitted key
        # leaves the field unchanged (``None`` is a meaningful stored value for
        # both). ``compatibleSkills``/``toolCollections`` follow the CRUD
        # contract where ``None`` means "unchanged" and ``[]`` clears.
        update_kwargs: dict[str, Any] = {
            "persona": params.get("persona"),
            "compatible_skills": params.get("compatibleSkills"),
            "tool_collections": params.get("toolCollections"),
        }
        if "model" in params:
            update_kwargs["model"] = params.get("model")
        if "integrationConfig" in params:
            update_kwargs["integration_config"] = _integration_config_from_item(
                params.get("integrationConfig")
            )
        if "description" in params:
            update_kwargs["description"] = params.get("description") or ""

        result = await self.specialty_crud.update(
            ctx=self.ctx,
            asset_id=params["assetId"],
            **update_kwargs,
        )
        if result is None:
            return {"error": "not_found"}
        return result

    async def _handle_delete_specialty_agent_draft(self, params: dict) -> Any:
        result = await self.specialty_crud.delete(
            ctx=self.ctx,
            asset_id=params["assetId"],
        )
        if result is None:
            return {"error": "not_found"}
        return {"status": "ok"}

    async def _handle_list_specialty_agent_drafts(self, params: dict) -> list[dict]:
        # SpecialtyCRUD.list returns typed SpecialtyAgent records under the new
        # AssetIndex API; the wire shape is a list of dicts.
        #
        # This listing is AUTHORED ASSETS ONLY. It used to append a virtual
        # `core-task-agent` row so the UI could show the platform's built-in Task
        # Agent alongside them (Requirement 3.7), from a time when the Agents tab
        # rendered nothing else — AUDIT-FINDINGS.md records that all four
        # built-in sub-agent runtimes were invisible in the UI.
        #
        # That gap is now filled properly by the `mcp_list_agents` action and the
        # Built-in agents browser, which surfaces task/render/planning/design with
        # their real purpose and runtime id. Keeping the injected row on top of
        # that showed "Task Agent" twice, and because it had no AssetIndex row
        # behind it every mutation addressed to it failed — which is what produced
        # a Delete button and an editor that could only error.
        from .models import _integration_config_to_item

        agents = await self.specialty_crud.list(ctx=self.ctx)
        items: list[dict] = [
            {
                "assetId": a.asset_id,
                "category": a.category,
                "name": a.name,
                "description": a.description,
                "status": a.status,
                "scope": a.scope,
                "s3Prefix": a.s3_prefix,
                "createdAt": a.created_at,
                "updatedAt": a.updated_at,
                "compatibleSkills": list(a.compatible_skills),
                "model": a.model,
                "toolCollections": list(a.tool_collections),
                "integrationConfig": _integration_config_to_item(
                    a.integration_config
                ),
            }
            for a in agents
        ]
        return items

    # -----------------------------------------------------------------------
    # Tool Collection handlers
    # -----------------------------------------------------------------------

    async def _handle_create_tool_collection_draft(self, params: dict) -> dict:
        tool_refs = [
            ToolRef(name=ref["name"], version=ref["version"])
            for ref in params.get("toolRefs", [])
        ]
        result = await self.tool_collection_crud.create(
            ctx=self.ctx,
            name=params["name"],
            tool_refs=tool_refs,
        )
        return {"assetId": result.asset_id, "s3Prefix": result.s3_prefix}

    async def _handle_read_tool_collection_draft(self, params: dict) -> Any:
        result = await self.tool_collection_crud.read(
            ctx=self.ctx,
            asset_id=params["assetId"],
        )
        if result is None:
            return {"error": "not_found"}
        return {
            "assetId": result.asset_id,
            "name": result.name,
            "toolRefs": [
                {"name": ref.name, "version": ref.version}
                for ref in result.tool_refs
            ],
            "metadata": result.metadata,
        }

    async def _handle_update_tool_collection_draft(self, params: dict) -> Any:
        tool_refs = None
        if "toolRefs" in params:
            tool_refs = [
                ToolRef(name=ref["name"], version=ref["version"])
                for ref in params["toolRefs"]
            ]
        result = await self.tool_collection_crud.update(
            ctx=self.ctx,
            asset_id=params["assetId"],
            name=params.get("name"),
            tool_refs=tool_refs,
        )
        if result is None:
            return {"error": "not_found"}
        return result

    async def _handle_delete_tool_collection_draft(self, params: dict) -> Any:
        # Imported here to match how this module reaches the other lab errors.
        from .errors import StillReferencedError

        try:
            result = await self.tool_collection_crud.delete(
                ctx=self.ctx,
                asset_id=params["assetId"],
                force=bool(params.get("force", False)),
            )
        except StillReferencedError as err:
            # Returned as data, not raised, so the caller can render WHICH agents
            # block the delete. `partial` travels with it: the scan sees only what
            # this actor can see, so the list is "references visible to you".
            return {
                "error": "still_referenced",
                "message": str(err),
                "references": [
                    {
                        "kind": hit.kind,
                        "assetId": hit.asset_id,
                        "name": hit.name,
                        "status": hit.status,
                        "detail": hit.detail,
                    }
                    for hit in err.hits
                ],
                "partial": list(err.partial),
            }
        if result is None:
            return {"error": "not_found"}
        return {"status": "ok"}

    async def _handle_list_tool_collection_drafts(self, params: dict) -> list[dict]:
        # ToolCollectionCRUD.list returns typed ToolCollection records
        # under the new AssetIndex API; the wire shape is a list of dicts.
        collections = await self.tool_collection_crud.list(ctx=self.ctx)
        return [
            {
                "assetId": c.asset_id,
                "category": c.category,
                "name": c.name,
                "status": c.status,
                "scope": c.scope,
                "s3Prefix": c.s3_prefix,
                "createdAt": c.created_at,
                "updatedAt": c.updated_at,
                "toolRefs": [
                    {"name": ref.name, "version": ref.version}
                    for ref in c.tool_refs
                ],
            }
            for c in collections
        ]

    # -----------------------------------------------------------------------
    # Workflow handlers
    # -----------------------------------------------------------------------

    async def _handle_create_workflow_draft(self, params: dict) -> dict:
        steps = _parse_workflow_steps(params.get("steps", []))
        result = await self.workflow_crud.create(
            ctx=self.ctx,
            name=params["name"],
            steps=steps,
        )
        return {"assetId": result.asset_id, "s3Prefix": result.s3_prefix}

    async def _handle_read_workflow_draft(self, params: dict) -> Any:
        result = await self.workflow_crud.read(
            ctx=self.ctx,
            asset_id=params["assetId"],
        )
        if result is None:
            return {"error": "not_found"}
        return {
            "assetId": result.asset_id,
            "name": result.name,
            "steps": [
                {
                    "stepId": step.step_id,
                    "agent": {
                        "kind": step.agent.kind,
                        **({"ref": step.agent.ref} if step.agent.ref else {}),
                        **({"assetId": step.agent.asset_id} if step.agent.asset_id else {}),
                    },
                    "skill": {"assetId": step.skill.asset_id},
                    "toolCollection": {"assetId": step.tool_collection.asset_id},
                    "prompt": step.prompt,
                    "inputs": step.inputs,
                }
                for step in result.steps
            ],
            "resolvedView": [
                {
                    "stepId": rv.step_id,
                    "agentKind": rv.agent_kind,
                    "agentName": rv.agent_name,
                    "agentAssetId": rv.agent_asset_id,
                    "agentResolved": rv.agent_resolved,
                    "skillName": rv.skill_name,
                    "skillAssetId": rv.skill_asset_id,
                    "skillResolved": rv.skill_resolved,
                    "toolCollectionName": rv.tool_collection_name,
                    "toolCollectionAssetId": rv.tool_collection_asset_id,
                    "toolCollectionResolved": rv.tool_collection_resolved,
                    "prompt": rv.prompt,
                    "inputs": rv.inputs,
                }
                for rv in result.resolved_view
            ],
            "metadata": result.metadata,
        }

    async def _handle_update_workflow_draft(self, params: dict) -> Any:
        steps = None
        if "steps" in params:
            steps = _parse_workflow_steps(params["steps"])
        result = await self.workflow_crud.update(
            ctx=self.ctx,
            asset_id=params["assetId"],
            name=params.get("name"),
            steps=steps,
        )
        if result is None:
            return {"error": "not_found"}
        return result

    async def _handle_delete_workflow_draft(self, params: dict) -> Any:
        result = await self.workflow_crud.delete(
            ctx=self.ctx,
            asset_id=params["assetId"],
        )
        if result is None:
            return {"error": "not_found"}
        return {"status": "ok"}

    async def _handle_list_workflow_drafts(self, params: dict) -> list[dict]:
        return self.workflow_crud.list(ctx=self.ctx)

    # -----------------------------------------------------------------------
    # Hook handlers (declarative harness triggers)
    # -----------------------------------------------------------------------

    @staticmethod
    def _hook_to_dict(h: Any) -> dict:
        return {
            "assetId": h.asset_id,
            "name": h.name,
            "event": h.event,
            "condition": h.condition,
            "action": {"type": h.action_type, "params": h.action_params},
            "priority": h.priority,
            "enabled": bool(h.enabled),
            "builtin": bool(getattr(h, "builtin", False)),
        }

    async def _handle_create_hook(self, params: dict) -> dict:
        action = params.get("action") or {}
        return await self.hook_crud.create(
            ctx=self.ctx,
            name=params["name"],
            event=params["event"],
            condition=params.get("condition", ""),
            action_type=action.get("type") or params.get("actionType", "notify"),
            action_params=action.get("params") if action else params.get("actionParams"),
            priority=int(params.get("priority", 100)),
            enabled=bool(params.get("enabled", True)),
        )

    async def _handle_read_hook(self, params: dict) -> Any:
        h = await self.hook_crud.read(ctx=self.ctx, asset_id=params["assetId"])
        return self._hook_to_dict(h) if h else None

    async def _handle_list_hooks(self, params: dict) -> dict:
        from hooks.defaults import LOCKED_BUILTINS, default_hook_specs

        user = await self.hook_crud.list(ctx=self.ctx)
        builtins = [
            {
                "assetId": s.name,
                "name": s.name,
                "event": s.event,
                "condition": s.condition,
                "action": {"type": s.action_type, "params": s.action_params},
                "priority": s.priority,
                "enabled": s.enabled,
                "builtin": True,
                # A locked built-in (e.g. render-moments) cannot be cloned, overridden or
                # disabled by a user hook (hooks.loader drops any user spec of this name), so the
                # Hooks UI must offer no Clone/override for it.
                "locked": s.name in LOCKED_BUILTINS,
            }
            for s in default_hook_specs()
        ]
        return {
            "userHooks": [self._hook_to_dict(h) for h in user],
            "builtinHooks": builtins,
        }

    async def _handle_update_hook(self, params: dict) -> dict:
        action = params.get("action") or {}
        ok = await self.hook_crud.update(
            ctx=self.ctx,
            asset_id=params["assetId"],
            name=params.get("name"),
            event=params.get("event"),
            condition=params.get("condition"),
            action_type=action.get("type") if action else params.get("actionType"),
            action_params=action.get("params") if action else params.get("actionParams"),
            priority=params.get("priority"),
            enabled=params.get("enabled"),
        )
        return {"updated": bool(ok)}

    async def _handle_delete_hook(self, params: dict) -> dict:
        ok = await self.hook_crud.delete(ctx=self.ctx, asset_id=params["assetId"])
        return {"deleted": bool(ok)}

    async def _handle_validate_hook_condition(self, params: dict) -> dict:
        from hooks.expr import ExpressionError, evaluate, validate

        condition = params.get("condition", "")
        sample = params.get("sampleEvent") or {}
        try:
            validate(condition)
        except ExpressionError as exc:
            return {"valid": False, "error": str(exc)}
        out: dict = {"valid": True}
        if sample:
            try:
                out["matched"] = evaluate(condition, sample)
            except ExpressionError as exc:
                out = {"valid": False, "error": str(exc)}
        return out

    # -----------------------------------------------------------------------
    # Gateway handlers
    # -----------------------------------------------------------------------

    async def _handle_browse_gateway_tools(self, params: dict) -> dict:
        result = self.gateway_browser.browse(ctx=self.ctx)
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "version": t.version,
                    "inputSchema": t.input_schema,
                }
                for t in result.projection.tools
            ],
            "retrievedAt": result.projection.retrieved_at,
            "stale": result.stale,
        }

    async def _handle_refresh_gateway_tools(self, params: dict) -> dict:
        result = self.gateway_browser.refresh(ctx=self.ctx)
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "version": t.version,
                    "inputSchema": t.input_schema,
                }
                for t in result.projection.tools
            ],
            "retrievedAt": result.projection.retrieved_at,
            "stale": result.stale,
        }

    async def _handle_test_gateway_tool(self, params: dict) -> dict:
        """Handle a single Gateway tool test invocation.

        Invokes the Gateway's call_tool method directly and returns the
        raw response body, status, and duration. Records the invocation
        as a Test_Harness_Run_Record with subject="tool".

        Requirements: 4.6
        """
        tool_name = params["name"]
        tool_version = params["version"]
        sample_input = params.get("sample", params.get("input", {}))

        result = self.gateway_browser.call_tool(
            tool_name=tool_name,
            tool_version=tool_version,
            input_data=sample_input,
        )

        return {
            "responseBody": result.response_body,
            "status": result.status,
            "durationMs": result.duration_ms,
        }

    # -----------------------------------------------------------------------
    # Chat handlers
    # -----------------------------------------------------------------------

    async def _handle_chat_message(self, params: dict) -> Any:
        """Handle a chat message through the Lab_Chat_Agent.

        Routes the message through the inventory-aware chat agent which
        builds a fresh snapshot per turn and routes mutations through
        validated CRUD (Requirements 6.1, 6.2, 6.3; Properties 13, 14).
        """
        message = params.get("message", "")
        tool_calls = params.get("toolCalls")
        history = params.get("history")  # Conversation history for context

        result = await self.chat_agent.handle_message(
            message=message,
            tool_calls=tool_calls,
            history=history,
        )

        return {
            "response": result.response,
            "affectedAssetIds": result.affected_asset_ids,
            "toolCallsMade": result.tool_calls_made,
        }

    # -----------------------------------------------------------------------
    # Harness handlers
    # -----------------------------------------------------------------------

    async def _handle_run_harness(self, params: dict) -> Any:
        """Handle a Test_Harness_Run request.

        Routes to the TestHarness.run() method which handles execution
        routing, timeout enforcement, and run record persistence.

        Requirements: 7.1, 7.5, 7.6, 7.8, 7.9
        """
        target_asset_id = params["assetId"]

        # Build SampleRef from payload
        sample_kind = params.get("sampleKind", "inline")
        sample_value = params.get("sampleInput", params.get("sample", ""))
        sample_input = SampleRef(kind=sample_kind, value=sample_value)

        bindings = params.get("bindings")
        timeout_seconds = params.get("timeoutSeconds")

        record = await self.test_harness.run(
            ctx=self.ctx,
            target_asset_id=target_asset_id,
            sample_input=sample_input,
            bindings=bindings,
            timeout_seconds=timeout_seconds,
        )

        # Serialize the run record for the response
        result = {
            "runId": record.run_id,
            "subject": record.subject,
            "status": record.status,
            "startedAt": record.started_at,
            "endedAt": record.ended_at,
            "outputs": record.outputs,
            "logs": record.logs,
        }
        if record.error:
            result["error"] = {
                "cause": record.error.cause,
            }
            if record.error.step_id:
                result["error"]["stepId"] = record.error.step_id

        return result

    async def _handle_list_harness_runs(self, params: dict) -> Any:
        """Handle listing prior Test_Harness_Run_Records.

        Returns only records whose storage prefix begins with
        {actor_id}/lab-runs/.

        Requirements: 7.7
        """
        return await self.test_harness.list_runs(ctx=self.ctx)

    async def _handle_read_harness_run(self, params: dict) -> Any:
        """Handle reading a single Test_Harness_Run_Record by runId.

        Requirements: 7.7
        """
        run_id = params["runId"]
        record = await self.test_harness.read_run_record(
            ctx=self.ctx, run_id=run_id
        )
        if record is None:
            return {"error": "not_found"}

        result = {
            "runId": record.run_id,
            "subject": record.subject,
            "status": record.status,
            "startedAt": record.started_at,
            "endedAt": record.ended_at,
            "outputs": record.outputs,
            "logs": record.logs,
        }
        if record.error:
            result["error"] = {
                "cause": record.error.cause,
            }
            if record.error.step_id:
                result["error"]["stepId"] = record.error.step_id

        return result

    # -----------------------------------------------------------------------
    # Readiness handlers
    # -----------------------------------------------------------------------

    async def _handle_check_readiness(self, params: dict) -> Any:
        """Handle a Promotion_Readiness_Check request.

        Routes to the PromotionReadiness.check() method which performs
        a read-only validation of the workflow and all transitively
        referenced assets.

        Requirements: 8.1–8.10
        Properties: 16, 17, 18, 19, 24
        """
        workflow_asset_id = params["workflowAssetId"]

        report = await self.readiness.check(
            ctx=self.ctx,
            workflow_asset_id=workflow_asset_id,
        )

        if report is None:
            return {"error": "not_found"}

        return {
            "workflowAssetId": report.workflow_asset_id,
            "overallStatus": report.overall_status,
            "entries": [
                {
                    "category": entry.category,
                    "assetId": entry.asset_id,
                    "status": entry.status,
                    "failures": entry.failures,
                }
                for entry in report.entries
            ],
        }

    # -----------------------------------------------------------------------
    # Generic handlers
    # -----------------------------------------------------------------------

    async def _handle_rename_asset(self, params: dict) -> Any:
        """Rename any asset (skill, specialty-agent, tool-collection, workflow).

        Updates the name field in the DynamoDB index.
        """
        asset_id = params.get("assetId", "")
        new_name = params.get("name", "").strip()
        category = params.get("category", "")

        if not asset_id or not new_name or not category:
            raise ValueError("assetId, name, and category are required")

        if len(new_name) > 128:
            raise ValueError("Name must be 128 characters or fewer")

        import time as _time
        updated_at = int(_time.time() * 1000)

        # Resolve the right PK based on category — tool-collections live in a
        # different partition than skill / specialty-agent / workflow drafts.
        if category == "tool-collection":
            pk = make_pk_user_owned(self.ctx.actor_id)
            sk = make_sk_owned(category, asset_id)
        else:
            pk = make_pk_user_owned(self.ctx.actor_id)
            sk = make_sk_owned(category, asset_id)

        try:
            await self.index.update(
                pk,
                sk,
                update_expression="SET #name = :name, updatedAt = :updatedAt",
                expression_attribute_names={"#name": "name"},  # name is reserved
                expression_attribute_values={
                    ":name": new_name,
                    ":updatedAt": updated_at,
                },
                condition_expression="attribute_exists(PK)",
            )
        except ClientError as err:
            if (
                err.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                raise FileNotFoundError(f"Asset not found: {asset_id}")
            raise

        return {"assetId": asset_id, "name": new_name, "updatedAt": updated_at}

    # -----------------------------------------------------------------------
    # Lifecycle handlers — publish / edit / discard / list versions
    # -----------------------------------------------------------------------

    async def _handle_publish_asset(self, params: dict) -> Any:
        """Publish the working copy of any asset as the next immutable version.

        Required params: ``category``, ``assetId``.
        Optional params: ``scope`` (default ``"private"``),
        ``groupIds``, ``orgId``.
        """
        from ._lifecycle_ops import publish_asset

        return await publish_asset(
            ctx=self.ctx,
            asset_index=self.index,
            s3=self.s3,
            category=params["category"],
            asset_id=params["assetId"],
            scope=params.get("scope", "private"),
            group_ids=params.get("groupIds"),
            org_id=params.get("orgId"),
        )

    async def _handle_edit_asset(self, params: dict) -> Any:
        """Fork a published version into a working copy under the actor's OWNED partition.

        Required params: ``category``, ``assetId``.
        Optional: ``fromVersion`` (defaults to the current pointer).
        """
        from ._lifecycle_ops import edit_asset, WorkingCopyExists

        try:
            wc = await edit_asset(
                ctx=self.ctx,
                asset_index=self.index,
                s3=self.s3,
                category=params["category"],
                asset_id=params["assetId"],
                from_version=params.get("fromVersion"),
            )
        except WorkingCopyExists as exc:
            return {"error": "working_copy_exists", "message": str(exc)}

        return {
            "assetId": wc.asset_id,
            "category": wc.category,
            "name": wc.name,
            "s3Prefix": wc.s3_prefix,
            "forkedFrom": (
                {"assetId": wc.cloned_from[0], "version": wc.cloned_from[1]}
                if wc.cloned_from else None
            ),
        }

    async def _handle_discard_changes(self, params: dict) -> Any:
        """Drop an unpublished working copy. Latest version is unaffected."""
        from ._lifecycle_ops import discard_changes

        return await discard_changes(
            ctx=self.ctx,
            asset_index=self.index,
            s3=self.s3,
            category=params["category"],
            asset_id=params["assetId"],
        )

    async def _handle_list_asset_versions(self, params: dict) -> Any:
        """Enumerate every version of an asset via the byPublishedId GSI.

        Required: ``assetId``.
        Returns a list of {version, deprecated, current, scope, createdAt}.
        """
        asset_id = params["assetId"]
        lineage = await self.index.query_lineage(asset_id)
        return [
            {
                "version": r.version,
                "deprecated": r.deprecated,
                "current": r.current,
                "scope": r.scope,
                "createdAt": r.created_at,
                "updatedAt": r.updated_at,
                "name": r.name,
            }
            for r in sorted(lineage, key=lambda r: r.version or 0)
        ]

    async def _handle_list_my_published_assets(self, params: dict) -> Any:
        """List the actor's own published assets in ``category``.

        Returns the latest non-deprecated version per asset_id, so an
        asset published twice shows up once.
        """
        from .asset_index import make_pk_user_versions

        category = params["category"]
        pk = make_pk_user_versions(self.ctx.actor_id)
        items, _ = await self.index.query_pk(pk, sk_prefix=f"{category}#")

        # Latest non-deprecated per asset_id.
        latest: dict[str, Any] = {}
        for r in items:
            if r.deprecated or r.version is None:
                continue
            existing = latest.get(r.asset_id)
            if existing is None or r.version > existing.version:
                latest[r.asset_id] = r

        return [
            {
                "assetId": r.asset_id,
                "name": r.name,
                "description": r.description,
                "version": r.version,
                "current": r.current,
                "scope": r.scope,
                "createdAt": r.created_at,
                "updatedAt": r.updated_at,
            }
            for r in sorted(latest.values(), key=lambda r: r.name.lower())
        ]


# ---------------------------------------------------------------------------
# Visibility resolver — list_visible_assets
# ---------------------------------------------------------------------------


# Bound on how many group-tier queries are issued concurrently. Cognito
# group membership is bounded in practice (low double digits), but the
# semaphore protects the connection pool when an actor happens to belong
# to many groups.
_GROUP_QUERY_CONCURRENCY: int = 10

# Sentinel returned by ``_decode_cursor`` when the input is None / empty
# — represents "every tier starts at its first page".
_EMPTY_CURSOR_STATE: dict[str, Any] = {
    "user_drafts": None,
    "user_pub": None,
    "group_pub": {},
    "org_pub": None,
}


def _encode_cursor(state: dict[str, Any]) -> Optional[str]:
    """Encode the per-tier ``LastEvaluatedKey`` dict to an opaque token.

    Returns ``None`` when every tier is exhausted (i.e., ``state`` is
    structurally equivalent to :data:`_EMPTY_CURSOR_STATE`). Otherwise
    returns a base64url-encoded JSON string. The caller should pass the
    returned token back as ``cursor`` on the next call to
    :func:`list_visible_assets`; the token's payload is opaque.

    The encoding is deliberately JSON-over-base64 rather than a tighter
    binary format because DynamoDB ``LastEvaluatedKey`` values are small
    dicts of attribute-value scalars that JSON-encode cleanly, and
    debugging is much easier when a cursor can be inspected with a
    one-line decode.
    """
    if not _state_has_open_tier(state):
        return None
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: Optional[str]) -> dict[str, Any]:
    """Decode an opaque cursor token back to a per-tier state dict.

    Returns a fresh copy of :data:`_EMPTY_CURSOR_STATE` when ``cursor``
    is ``None`` or empty. Raises :class:`ValueError` for malformed
    input — the resolver translates that to a 400-class response at the
    handler boundary.
    """
    if not cursor:
        return {
            "user_drafts": None,
            "user_pub": None,
            "group_pub": {},
            "org_pub": None,
        }
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        state = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError(f"invalid cursor: {err}") from err
    if not isinstance(state, dict):
        raise ValueError("invalid cursor: not a JSON object")
    # Normalize missing keys defensively so a partial cursor still works.
    return {
        "user_drafts": state.get("user_drafts"),
        "user_pub": state.get("user_pub"),
        "group_pub": dict(state.get("group_pub") or {}),
        "org_pub": state.get("org_pub"),
    }


def _state_has_open_tier(state: dict[str, Any]) -> bool:
    """Return True iff at least one tier still has a non-None LEK."""
    if state.get("user_drafts") is not None:
        return True
    if state.get("user_pub") is not None:
        return True
    if state.get("org_pub") is not None:
        return True
    group_pub = state.get("group_pub") or {}
    return any(v is not None for v in group_pub.values())


async def list_visible_assets(
    ctx: LabRequestContext,
    category: str,
    mode: Literal["lab", "chat", "project"],
    *,
    asset_index: AssetIndex,
    org_id: Optional[str] = None,
    include_deprecated: bool = False,
    cursor: Optional[str] = None,
    limit: int = 100,
) -> "VisibleAssetsPage":
    """Resolve the four-tier visibility union for ``ctx`` and ``category``.

    Issues one query each against the actor's drafts partition, the
    actor's private-published partition, every ``GROUP#<g>#PUBLISHED``
    partition the actor belongs to (fanned out across
    ``ctx.group_ids``), and the org-published partition. Results are
    unioned, mode-aware post-filtered, deprecation-filtered, and stable-
    sorted by ``updated_at desc`` before truncating to ``limit``.

    The four tier queries run concurrently via ``asyncio.gather``;
    group-tier fan-out is bounded by :data:`_GROUP_QUERY_CONCURRENCY` so
    that an actor with many groups does not exhaust the connection pool.
    Each tier advances independently — the returned ``next_cursor``
    encodes per-tier ``LastEvaluatedKey`` state and a future call with
    that cursor resumes only the tiers that still have pages.

    Args:
        ctx: Bound request context. ``ctx.actor_id`` selects the drafts
            and private-published partitions; ``ctx.group_ids`` drives
            the group-tier fan-out.
        category: One of the AssetIndex categories (``"skill"``,
            ``"specialty-agent"``, ``"workflow"``, ``"tool-collection"``).
            Each tier query restricts to ``begins_with(SK, "<cat>#")``.
        mode: The agent mode the resolver is serving. ``"lab"`` returns
            the full union; ``"chat"`` strips ``status=="draft"``;
            ``"project"`` strips ``status=="draft"`` and
            ``scope=="private"``.
        asset_index: The :class:`AssetIndex` to query. Passed
            explicitly so this resolver is callable without bringing
            up the full :class:`LabService` (its constructor is in
            mid-migration). All four tier queries use
            ``asset_index.query_pk``.
        org_id: The actor's org identifier. Required for the org tier;
            when ``None`` the resolver consults
            ``getattr(ctx, "org_id", None)`` for forward-compatibility
            with the LabRequestContext follow-on, and skips the org
            tier when both are absent.
        include_deprecated: When ``False`` (the default), records with
            ``status=="deprecated"`` are stripped from the result
            (Requirement 9.5). When ``True``, deprecated rows are
            preserved.
        cursor: Opaque token from a prior call's
            :attr:`VisibleAssetsPage.next_cursor`. ``None`` starts a
            fresh query.
        limit: Maximum number of records to return on this page.
            Default 100. The DynamoDB queries also use this limit per
            tier so the union before post-filter is bounded by
            ``limit * (3 + len(group_ids))``.

    Returns:
        A :class:`VisibleAssetsPage` whose ``items`` are the merged,
        filtered, sorted, and truncated assets and whose
        ``next_cursor`` is opaque (or ``None`` when every tier is
        exhausted).

    Requirements: 9.1, 9.2, 9.3, 9.4, 9.5
    Properties: 7
    """
    # Lazy import to avoid a top-of-file circular when models / results
    # import service-level types in the future.
    from .operation_results import VisibleAssetsPage

    if mode not in ("lab", "chat", "project"):
        raise ValueError(f"unknown mode: {mode!r}")
    if limit <= 0:
        raise ValueError(f"limit must be positive, got {limit!r}")

    actor_id = ctx.actor_id
    group_ids = list(getattr(ctx, "group_ids", []) or [])
    # Accept org_id either via the keyword or, forward-compatibly, via
    # the request context once Task 5.2's follow-on attaches it.
    effective_org_id = org_id if org_id is not None else getattr(ctx, "org_id", None)

    state = _decode_cursor(cursor)
    sk_prefix = f"{category}#"

    # ---- Tier 1: drafts owned by actor ---------------------------------
    drafts_pk = make_pk_user_owned(actor_id)

    async def _query_drafts() -> tuple[list, Optional[dict[str, Any]]]:
        return await asset_index.query_pk(
            drafts_pk,
            sk_prefix=sk_prefix,
            last_evaluated_key=state["user_drafts"],
            limit=limit,
        )

    # ---- Tier 2: actor's own immutable versions -----------------------
    user_pub_pk = make_pk_user_versions(actor_id)

    async def _query_user_pub() -> tuple[list, Optional[dict[str, Any]]]:
        return await asset_index.query_pk(
            user_pub_pk,
            sk_prefix=sk_prefix,
            last_evaluated_key=state["user_pub"],
            limit=limit,
        )

    # ---- Tier 3: group-published, fan-out across actor.group_ids -------
    group_semaphore = asyncio.Semaphore(_GROUP_QUERY_CONCURRENCY)

    async def _query_one_group(
        gid: str,
    ) -> tuple[str, list, Optional[dict[str, Any]]]:
        async with group_semaphore:
            items, next_lek = await asset_index.query_pk(
                make_pk_group_shared(gid),
                sk_prefix=sk_prefix,
                last_evaluated_key=state["group_pub"].get(gid),
                limit=limit,
            )
        return gid, items, next_lek

    # ---- Tier 4: org-published ----------------------------------------
    async def _query_org() -> tuple[list, Optional[dict[str, Any]]]:
        if not effective_org_id:
            return [], None
        return await asset_index.query_pk(
            make_pk_org_shared(effective_org_id),
            sk_prefix=sk_prefix,
            last_evaluated_key=state["org_pub"],
            limit=limit,
        )

    # Issue the four tiers concurrently. The group fan-out is itself a
    # gather bounded by the semaphore.
    drafts_task = _query_drafts()
    user_pub_task = _query_user_pub()
    org_task = _query_org()
    group_tasks = [_query_one_group(g) for g in group_ids]

    results = await asyncio.gather(
        drafts_task,
        user_pub_task,
        org_task,
        *group_tasks,
    )
    drafts_items, drafts_next = results[0]
    user_pub_items, user_pub_next = results[1]
    org_items, org_next = results[2]
    group_results = results[3:]  # list of (gid, items, next_lek)

    # ---- Union ---------------------------------------------------------
    merged: list = []
    merged.extend(drafts_items)
    merged.extend(user_pub_items)
    for _gid, gitems, _glek in group_results:
        merged.extend(gitems)
    merged.extend(org_items)

    # ---- Mode-aware post-filter (Requirement 9.2-9.4) ------------------
    if mode == "chat":
        merged = [a for a in merged if a.status != "draft"]
    elif mode == "project":
        merged = [
            a for a in merged
            if a.status != "draft" and a.scope != "private"
        ]
    # mode == "lab": full union, no filtering

    # ---- Deprecation filter (Requirement 9.5) --------------------------
    if not include_deprecated:
        merged = [a for a in merged if a.status != "deprecated"]

    # ---- Stable sort by updated_at desc + truncate ---------------------
    # Python's sort is stable; using a single key keeps ties in their
    # union order, which is deterministic across runs because the tier
    # results were appended in a fixed sequence.
    merged.sort(key=lambda a: a.updated_at, reverse=True)
    truncated = merged[:limit]

    # ---- Build next-cursor state --------------------------------------
    next_state: dict[str, Any] = {
        "user_drafts": drafts_next,
        "user_pub": user_pub_next,
        "group_pub": {
            gid: glek for gid, _gitems, glek in group_results if glek is not None
        },
        "org_pub": org_next,
    }
    next_cursor = _encode_cursor(next_state)

    return VisibleAssetsPage(items=truncated, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _parse_workflow_steps(raw_steps: list[dict]) -> list[WorkflowStep]:
    """Parse raw step dicts from a payload into WorkflowStep objects.

    Args:
        raw_steps: List of step dicts from the request payload.

    Returns:
        List of WorkflowStep dataclass instances.
    """
    steps: list[WorkflowStep] = []
    for raw_step in raw_steps:
        agent_data = raw_step.get("agent", {})
        skill_data = raw_step.get("skill", {})
        tc_data = raw_step.get("toolCollection", {})

        agent_ref = AgentRef(
            kind=agent_data.get("kind", "core"),
            ref=agent_data.get("ref"),
            asset_id=agent_data.get("assetId"),
        )
        skill_ref = SkillRef(asset_id=skill_data.get("assetId"))
        tc_ref = ToolCollectionRef(asset_id=tc_data.get("assetId"))

        step = WorkflowStep(
            step_id=raw_step.get("stepId", ""),
            agent=agent_ref,
            skill=skill_ref,
            tool_collection=tc_ref,
            prompt=raw_step.get("prompt", ""),
            inputs=raw_step.get("inputs", []),
        )
        steps.append(step)

    return steps


def _load_gateway_endpoints() -> list[GatewayEndpointConfig]:
    """Load Gateway endpoint configuration from environment.

    Reads GATEWAY_ENDPOINTS as a comma-separated list of URLs.

    Returns:
        List of GatewayEndpointConfig objects.
    """
    raw = os.environ.get(_ENV_GATEWAY_ENDPOINTS, "")
    if not raw.strip():
        return []

    endpoints: list[GatewayEndpointConfig] = []
    for i, url in enumerate(raw.split(",")):
        url = url.strip()
        if url:
            endpoints.append(
                GatewayEndpointConfig(
                    endpoint_id=f"gw-{i}",
                    url=url,
                    name=f"Gateway {i}",
                )
            )
    return endpoints


def create_lab_service(agentcore_context) -> LabService:
    """Factory function to create a fully-wired LabService instance.

    Binds the LabRequestContext from the AgentCore Runtime invocation context,
    reads configuration from environment variables, and instantiates all
    dependencies.

    Args:
        agentcore_context: The AgentCore Runtime context object containing
            actor_id (from JWT sub claim) and session_id.

    Returns:
        A fully-initialized LabService instance.

    Raises:
        AuthenticationError: If actor_id is missing from the context.
        ValueError: If required environment variables are not set.
    """
    # Bind identity from JWT context (Requirement 1.1)
    ctx = LabRequestContext.from_agentcore_context(agentcore_context)

    # Load configuration from environment
    table_name = os.environ.get(_ENV_ASSET_INDEX_TABLE)
    if not table_name:
        raise ValueError(
            f"Environment variable {_ENV_ASSET_INDEX_TABLE} is required"
        )

    bucket_name = os.environ.get(_ENV_ARTIFACTS_BUCKET)
    if not bucket_name:
        raise ValueError(
            f"Environment variable {_ENV_ARTIFACTS_BUCKET} is required"
        )

    # Instantiate storage primitives bound to actor_id
    s3 = S3UserFilesTool(bucket=bucket_name, actor_id=ctx.actor_id)

    ddb = boto3.resource("dynamodb")
    table = ddb.Table(table_name)
    index = AssetIndex(ddb_table=table, actor_id=ctx.actor_id)

    # Instantiate Gateway browser
    gateway_endpoints = _load_gateway_endpoints()
    gateway_client = _DefaultGatewayClient()
    gateway_browser = GatewayBrowser(
        gateway_client=gateway_client,
        configured_endpoints=gateway_endpoints,
    )

    return LabService(
        ctx=ctx,
        s3=s3,
        index=index,
        gateway_browser=gateway_browser,
    )
