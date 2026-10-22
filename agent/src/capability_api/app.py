"""FastAPI surface for the Capability API (R6, R22-R23).

Routes, all under ``/control-plane`` so one ALB rule and one CloudFront
behavior cover them:

======  ===============================================  ======================
POST    /control-plane/capabilities/bootstrap            several categories, one trip
GET     /control-plane/capabilities/{category}           one category, paginated
POST    /control-plane/capabilities/search               text search across sources
POST    /control-plane/capabilities/batch-get            detail for known ids
POST    /control-plane/assets/{category}/{id}/purge      destroy a published lineage
GET     /control-plane/config                            feature flags, no auth
GET     /health                                          ALB target-group check
======  ===============================================  ======================

Four behaviours worth calling out because they are requirements, not choices:

* **One bootstrap per tab.** ``bootstrap`` returns *only* the categories asked
  for. There is no "all categories" path, so a tab selection cannot trigger
  hidden preloads of the other four (R10/R13).
* **Not-found-or-forbidden.** A cross-owner miss is indistinguishable from a
  nonexistent asset. The visibility resolver simply never returns the row, so
  existence is not disclosed (R5).
* **No Runtime fallback.** A failing source yields ``partialErrors``; nothing
  here can reach ``InvokeAgentRuntime`` (Property 5).
* **Purge is a control-plane operation, not a tool.** It lives here and *only*
  here — no dispatch-table entry, no ``@tool`` wrapper, nothing an agent can
  call. That the model cannot purge is a property of which process the code runs
  in (this one has no Strands and no model client — see
  ``tests/test_capability_api_isolation.py``), not of a guard that could be
  talked around. It is also the first slice of migration Task 27.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import uuid
from contextlib import contextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app_version import app_version
from capability.aggregate import ActorContext, bootstrap, load_category
from capability.config import CapabilityConfig, ConfigError, load_config
from capability.dto import CATEGORIES, CapabilitySummary

from .auth import AuthError, TokenVerifier, actor_from_claims, bearer_token
from .lab_actions import WRONG_HOST_CODE, is_control_plane, runtime_only_reason
from .origin_verify import OriginVerifyMiddleware
from .improve_dto import decode_decimals, merge_overlay, with_actor_bundle

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

#: Header carrying a client-supplied correlation id. Every response echoes one
#: back (generated when absent) so a slow tab load can be traced through the
#: API, its sources, and the projector (R13).
CORRELATION_HEADER: str = "x-correlation-id"

#: Cap on ``limit`` so one caller cannot ask for an unbounded page and blow the
#: p95 budget for everyone on the task.
MAX_LIMIT: int = 200


class Deps:
    """Process-wide clients, built once at startup.

    Held on the app rather than module globals so tests can construct an
    isolated instance with fakes, and so a configuration error surfaces as a
    failed health check on one task instead of a partially initialized process.
    """

    def __init__(self, config: CapabilityConfig) -> None:
        self.config = config
        self.verifier = TokenVerifier.from_env()
        self._asset_table: Any = None
        self._gateway_table: Any = None
        self._improve_table: Any = None

    @property
    def asset_table(self) -> Any:
        if self._asset_table is None:
            import boto3

            resource = boto3.resource("dynamodb", region_name=self.config.region)
            self._asset_table = resource.Table(self.config.asset_index_table)
        return self._asset_table

    @property
    def asset_index(self) -> Any:
        from lab.asset_index import AssetIndex

        return AssetIndex(self.asset_table)

    @property
    def gateway_table(self) -> Any:
        if not self.config.gateway_registry_table:
            return None
        if self._gateway_table is None:
            import boto3

            resource = boto3.resource("dynamodb", region_name=self.config.region)
            self._gateway_table = resource.Table(self.config.gateway_registry_table)
        return self._gateway_table

    @property
    def improve_table(self) -> Any:
        """DynamoDB resource for the Improve store, or ``None`` when unset.

        Optional like ``gateway_table``: the Capability API runs before the
        Improve table exists (staging bootstrap), and the routes turn a ``None``
        here into a 503 rather than a 500 — the deployment is incomplete, not the
        request wrong.
        """
        if not self.config.improve_table:
            return None
        if self._improve_table is None:
            import boto3

            resource = boto3.resource("dynamodb", region_name=self.config.region)
            self._improve_table = resource.Table(self.config.improve_table)
        return self._improve_table

    @property
    def improve_store(self) -> Any:
        """Reader/writer over the Improve store, or ``None`` when unconfigured.

        Lazy import (like ``asset_index``) so a cold start does not pay for a
        module a request may never reach.
        """
        table = self.improve_table
        if table is None:
            return None
        from capability.improve_store import ImproveStore

        return ImproveStore(table)

    def artifacts_tool(self, actor_id: str) -> Any:
        """Artifact-bucket tool bound to one caller, built per request.

        Per request rather than cached because the tool carries the actor
        identity that gates its relative-path methods — a process-wide instance
        would be one actor's prefix serving every caller. Purge only uses the
        bucket-absolute methods, but the binding stays honest anyway.

        Raises ``ValueError`` when the bucket is unconfigured; the route turns
        that into a 503 rather than a 500, since the deployment is incomplete
        rather than the request wrong.
        """
        from lab.s3_user_files_tool import S3UserFilesTool

        if not self.config.artifacts_bucket:
            raise ValueError("ARTIFACTS_BUCKET is not configured")
        return S3UserFilesTool(self.config.artifacts_bucket, actor_id)


def create_app(
    config: Optional[CapabilityConfig] = None, deps: Optional[Deps] = None
) -> FastAPI:
    """Build the ASGI app.

    A configuration error is raised here, at import/startup, rather than
    per-request: a task holding another environment's registry id must fail its
    health check instead of serving one environment's data from another's
    catalog (Property 7).
    """
    resolved = config or load_config()
    resolved_deps = deps or Deps(resolved)

    app = FastAPI(
        title="MediaWorks Capability API",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.deps = resolved_deps
    # Origin lock: refuse anything that did not come through our CloudFront distribution (only
    # /health is exempt). Reads ORIGIN_VERIFY_SECRET; a no-op where it is unset.
    app.add_middleware(OriginVerifyMiddleware)

    # -- plumbing ------------------------------------------------------

    def correlation_id(request: Request) -> str:
        return request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4())

    async def current_actor(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> ActorContext:
        """Resolve the trusted actor from the bearer token.

        Raises 401 with a stable code; the token itself is never logged, and
        the failure reason is not echoed beyond the code.
        """
        deps: Deps = request.app.state.deps
        try:
            claims = deps.verifier.verify(bearer_token(authorization))
        except AuthError as err:
            raise HTTPException(
                status_code=401,
                detail={"code": err.code, "message": "authentication failed"},
                headers={"WWW-Authenticate": f'Bearer error="{err.code}"'},
            ) from err
        return actor_from_claims(claims, request_id=correlation_id(request))

    def _validate_categories(raw: Any) -> list[str]:
        """Accept only known categories; ignore anything else silently.

        Silently, not with a 400: an older SPA asking for a category this
        build does not know should get the categories it *can* have rather
        than a failed tab.
        """
        if not isinstance(raw, (list, tuple)):
            return []
        seen: list[str] = []
        for item in raw:
            name = str(item)
            if name in CATEGORIES and name not in seen:
                seen.append(name)
        return seen

    # -- routes --------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Unauthenticated liveness for the ALB target group.

        Reports configuration facts only — never counts or identifiers, since
        this endpoint is reachable without a token.
        """
        cfg = request_config(app)
        return {
            "ok": True,
            "deployEnv": cfg.deploy_env,
            "appVersion": app_version(),
            "registryConfigured": cfg.registry_configured,
            "projectionEnabled": cfg.projection_enabled,
            "authConfigured": app.state.deps.verifier.configured,
        }

    @app.get("/control-plane/config")
    def public_config() -> dict[str, Any]:
        """Category feature flags for the SPA, and the release this env runs. No identity, no auth."""
        return {**request_config(app).to_public_json(), "appVersion": app_version()}

    @app.post("/control-plane/capabilities/bootstrap")
    async def bootstrap_route(
        request: Request,
        body: dict[str, Any],
        actor: ActorContext = Depends(current_actor),
    ) -> JSONResponse:
        """Load the requested categories concurrently, one trip."""
        deps: Deps = request.app.state.deps
        categories = _validate_categories(body.get("categories"))
        limit = _clamp_limit(body.get("limit"))
        pages = await bootstrap(
            categories,
            config=deps.config,
            actor=actor,
            asset_index=deps.asset_index if categories else None,
            gateway_table=deps.gateway_table,
            limit=limit,
        )
        return _json(
            {"categories": {name: page.to_json() for name, page in pages.items()}},
            correlation_id(request),
        )

    @app.get("/control-plane/capabilities/{category}")
    async def category_route(
        request: Request,
        category: str,
        cursor: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
        actor: ActorContext = Depends(current_actor),
    ) -> JSONResponse:
        """One category, with an opaque cursor for the next page."""
        deps: Deps = request.app.state.deps
        if category not in CATEGORIES:
            raise HTTPException(status_code=404, detail={"code": "unknown_category"})
        page = await load_category(
            category,
            config=deps.config,
            actor=actor,
            asset_index=deps.asset_index,
            gateway_table=deps.gateway_table,
            cursor=cursor,
            limit=_clamp_limit(limit),
        )
        return _json(page.to_json(), correlation_id(request))

    @app.post("/control-plane/capabilities/search")
    async def search_route(
        request: Request,
        body: dict[str, Any],
        actor: ActorContext = Depends(current_actor),
    ) -> JSONResponse:
        """Substring search over the categories the caller names.

        Filtering happens after normalization rather than being pushed into
        each source: the Registry's own search and a DynamoDB partition query
        have different matching semantics, and a merged result whose membership
        depended on which source matched would make shadow-parity comparison
        meaningless.
        """
        deps: Deps = request.app.state.deps
        query = str(body.get("query") or "").strip().lower()
        categories = _validate_categories(body.get("categories")) or sorted(CATEGORIES)
        pages = await bootstrap(
            categories,
            config=deps.config,
            actor=actor,
            asset_index=deps.asset_index,
            gateway_table=deps.gateway_table,
            limit=_clamp_limit(body.get("limit")),
        )
        matches: list[CapabilitySummary] = []
        errors: list[dict[str, Any]] = []
        for page in pages.values():
            errors.extend(e.to_json() for e in page.partial_errors)
            for item in page.items:
                if not query or _matches(item, query):
                    matches.append(item)
        return _json(
            {
                "items": [item.to_json() for item in matches],
                **({"partialErrors": errors} if errors else {}),
            },
            correlation_id(request),
        )

    @app.post("/control-plane/capabilities/batch-get")
    async def batch_get_route(
        request: Request,
        body: dict[str, Any],
        actor: ActorContext = Depends(current_actor),
    ) -> JSONResponse:
        """Resolve known summary ids within the caller's visibility.

        An id the caller cannot see is simply absent from the response —
        not-found-or-forbidden, with no separate "denied" list that would
        disclose existence (R5).
        """
        deps: Deps = request.app.state.deps
        wanted = {str(i) for i in (body.get("ids") or []) if str(i)}
        categories = _validate_categories(body.get("categories")) or sorted(CATEGORIES)
        if not wanted:
            return _json({"items": []}, correlation_id(request))
        pages = await bootstrap(
            categories,
            config=deps.config,
            actor=actor,
            asset_index=deps.asset_index,
            gateway_table=deps.gateway_table,
            limit=MAX_LIMIT,
        )
        items = [
            item.to_json()
            for page in pages.values()
            for item in page.items
            if item.id in wanted
        ]
        return _json({"items": items}, correlation_id(request))

    @app.post("/control-plane/assets/{category}/{asset_id}/purge")
    async def purge_route(
        request: Request,
        category: str,
        asset_id: str,
        body: Optional[dict[str, Any]] = None,
        actor: ActorContext = Depends(current_actor),
    ) -> JSONResponse:
        """Preview or destroy every version of a published lineage.

        ``dryRun`` defaults to **true**, so a request that forgets the flag
        previews instead of destroying. Nothing else in this API defaults a
        parameter for safety; this one does because the mistake is unrecoverable.

        The reference scan runs on both paths: on the preview it is what the
        dialog shows, and on the real purge it is recorded on the result as an
        audit trail of what the caller was told they would break.

        Domain imports are lazy, matching ``Deps.asset_index`` — the purge
        modules are the only reason this process would touch the canonical write
        path, and a route that exists should not cost every cold start.
        """
        deps: Deps = request.app.state.deps
        from lab._canonical_ops import preflight_asset_purge, purge_asset_lineage
        from lab.purge_scan import scan_references

        dry_run = _dry_run(body)
        ctx = actor.as_lab_context()
        try:
            s3 = deps.artifacts_tool(actor.actor_id)
        except ValueError as err:
            logger.error("purge unavailable: %s", err)
            raise HTTPException(
                status_code=503, detail={"code": "artifacts_bucket_not_configured"}
            ) from err

        with _domain_errors():
            # Dispatched on category: an agent is referenced by workflow steps,
            # a skill by an agent's compatible-skill list. Running the wrong scan
            # would report an empty blast radius for an asset things depend on.
            hits, partial = await scan_references(
                category=category,
                ctx=ctx,
                asset_index=deps.asset_index,
                s3=s3,
                published_id=asset_id,
                org_id=actor.org_id,
            )
            if dry_run:
                preflight = await preflight_asset_purge(
                    ctx=ctx,
                    asset_index=deps.asset_index,
                    category=category,
                    published_id=asset_id,
                    hits=hits,
                    partial=partial,
                )
                payload: dict[str, Any] = {
                    "dryRun": True,
                    "preflight": _preflight_json(preflight),
                }
            else:
                purged = await purge_asset_lineage(
                    ctx=ctx,
                    asset_index=deps.asset_index,
                    s3=s3,
                    category=category,
                    published_id=asset_id,
                    hits=hits,
                    partial=partial,
                )
                logger.info(
                    "purge applied: actor=%s %s#%s versions=%s",
                    actor.actor_id,
                    category,
                    asset_id,
                    list(purged.versions_deleted),
                )
                payload = {"dryRun": False, **_purged_json(purged)}
        return _json(payload, correlation_id(request))

    @app.get("/control-plane/builtin-skills/{slug}")
    async def builtin_skill_route(
        request: Request,
        slug: str,
        actor: ActorContext = Depends(current_actor),
    ) -> JSONResponse:
        """One built-in skill's package, for the detail view.

        Exists so opening a built-in skill does not invoke the agent runtime. It used to
        call `get_skill` there, which meant every open paid a cold start — and unlike the
        skills list, that call had no client cache, so the second open was as slow as the
        first.

        Deliberately NOT folded into the category page: the body is ~10-25 KB per skill
        and the list renders none of it, so carrying it there would tax every tab open to
        serve a view the user may never open.

        Authenticated like every other route here, but the content is not actor-specific —
        these ship with the harness. The token gates access to the harness, not to a
        particular skill.
        """
        deps: Deps = request.app.state.deps
        from capability.sources import SourceUnavailable, read_builtin_skill

        try:
            package = await read_builtin_skill(slug, deps.gateway_table)
        except SourceUnavailable as err:
            # NOT `_domain_errors`: that maps the canonical WRITE layer's exceptions, and
            # the read routes never surface this one — `aggregate` folds it into
            # `partialErrors` instead. Unmapped it would be a bare 500, which reads as "the
            # service broke" when the truth is "the catalog is not deployed". 503, matching
            # how an unconfigured artifacts bucket is reported.
            logger.error("builtin skill catalog unavailable: %s", err.kind)
            raise HTTPException(
                status_code=503,
                detail={"code": "builtin_catalog_unavailable", "reason": err.kind},
            ) from err
        if package is None:
            raise HTTPException(status_code=404, detail={"code": "unknown_skill"})
        return _json(package, correlation_id(request))

    @app.post("/control-plane/lab")
    async def lab_route(
        request: Request,
        body: Optional[dict[str, Any]] = None,
        actor: ActorContext = Depends(current_actor),
    ) -> JSONResponse:
        """Serve a control-plane ``LabService`` action without waking the runtime.

        This mounts the **same dispatcher** the agent runtime mounts, so a caller
        cannot tell which host answered — the handlers, and therefore the response
        shapes, are literally the same code. That is the point: the alternative
        (per-resource REST routes over the ``*_crud`` classes) would re-implement
        the normalization the SPA depends on, and the two would drift.

        Only the actions in ``lab_actions.CONTROL_PLANE_ACTIONS`` are served. The
        rest are refused rather than attempted, because this process has no model,
        no live MCP session, and deliberately no ``InvokeAgentRuntime`` permission
        — so there is nothing to forward to, only a wrong answer to give.

        **Failures come back as HTTP 200 with an ``error`` field, on purpose.**
        The runtime's own bypass wraps every exception as
        ``{"error": <ExceptionType>, "message": ...}`` and yields it with a success
        status (``main.py``, the ``LAB_BYPASS_ACTIONS`` branch), and some results
        are *contractually* shaped that way rather than being failures at all —
        ``EditAsset`` reports an existing working copy as a 200 with an ``error``
        field. Mapping exceptions onto status codes here, as ``_domain_errors``
        does for purge, would make the same request behave differently depending on
        which host served it, which is exactly what sharing one dispatcher is meant
        to rule out. Do not "fix" this into a 500.
        """
        deps: Deps = request.app.state.deps
        payload = dict(body or {})

        # Identity comes from the verified token, never the body (R5 / Property 3).
        # The runtime strips these before dispatching too; stripping here as well
        # means a payload that arrives by any route cannot smuggle an actor.
        for injected in ("actor_id", "__actor_id", "__actor_groups"):
            payload.pop(injected, None)

        action = str(payload.get("action") or "")
        if not action:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_request", "message": "action is required"},
            )

        if not is_control_plane(action):
            # 400 rather than 404 or 501: the route exists, the caller is
            # authenticated and authorized, and the payload is well-formed — it
            # simply arrived at the host that cannot serve it. ``host`` is
            # machine-readable so a client can correct its routing instead of
            # parsing the message.
            raise HTTPException(
                status_code=400,
                detail={
                    "code": WRONG_HOST_CODE,
                    "message": runtime_only_reason(action),
                    "action": action,
                    "host": "agent-runtime",
                },
            )

        try:
            s3 = deps.artifacts_tool(actor.actor_id)
        except ValueError as err:
            logger.error("lab route unavailable: %s", err)
            raise HTTPException(
                status_code=503, detail={"code": "artifacts_bucket_not_configured"}
            ) from err

        # Lazy, matching ``Deps.asset_index`` and the purge route: importing the
        # dispatcher pulls the whole lab package, which a health check or a
        # ``/control-plane/config`` request has no use for.
        from lab.service import LabService

        service = LabService(
            ctx=actor.as_lab_context(),
            s3=s3,
            index=deps.asset_index,
            gateway_browser=_refusing_gateway_browser(),
        )

        try:
            result = await service.handle(payload)
        except Exception as exc:  # noqa: BLE001 — parity with the runtime, above
            logger.exception("control-plane lab action failed: %s", action)
            result = {"error": type(exc).__name__, "message": str(exc)}

        # Results are not always objects: the ``List*`` actions return bare arrays.
        return _json(result, correlation_id(request))

    # -- improve -------------------------------------------------------
    #
    # The Improve surface's read + dismiss transport, ported from the Step-2
    # `ui/server` proxy (`/improve/*`) onto this always-on service. Pure DDB —
    # no model client, ever — so it stays inside the isolation boundary. Storage
    # is the global SNAPSHOT (the shared harness's claim) plus this actor's own
    # partition, which holds their evidence bundle and their `SUGG#` overlay
    # rows; the merge is bundle→overlay only, so a stale overlay row is inert.

    def _improve_store_or_503(request: Request) -> Any:
        store = request.app.state.deps.improve_store
        if store is None:
            raise HTTPException(
                status_code=503, detail={"code": "improve_store_not_configured"}
            )
        return store

    @app.get("/control-plane/improve/data")
    async def improve_data_route(
        request: Request,
        actor: ActorContext = Depends(current_actor),
    ) -> JSONResponse:
        """The global bundle, this caller's dismiss overlay merged on top, and
        their own evidence bundle attached when the producer has written one.

        The two reads run concurrently and off the event loop so a boto3 round
        trip does not stall the always-on task's other requests. Absent ≠ empty:
        no global SNAPSHOT at all is a 404 ("no data yet"), distinct from a
        populated bundle whose lists happen to be empty.

        The caller's own bundle is OPTIONAL and its absence is NOT a 404 — the
        producer runs daily over a bounded harvest window, so a fresh deploy and
        any actor without a conversational session in that window both land here.
        Missing it degrades to the global answer; the global bundle missing does
        not degrade at all, which is why only one of them gates the response.

        Both come from the actor's partition, which is read in one query. The
        actor is the verified token's subject and never a request parameter — the
        same rule the dismiss routes below follow, and here it is the whole
        isolation boundary for one tenant's runs, prompts and judge prose.
        """
        store = _improve_store_or_503(request)
        snapshot, (actor_bundle, overlay) = await asyncio.gather(
            asyncio.to_thread(store.get_snapshot),
            asyncio.to_thread(store.get_actor_partition, actor.actor_id),
        )
        if not snapshot or not snapshot.get("bundle"):
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        merged = with_actor_bundle(merge_overlay(snapshot["bundle"], overlay), actor_bundle)
        return _json(decode_decimals(merged), correlation_id(request))

    @app.post("/control-plane/improve/suggestions/{suggestion_id}/dismiss")
    async def improve_dismiss_route(
        request: Request,
        suggestion_id: str,
        body: Optional[dict[str, Any]] = None,
        actor: ActorContext = Depends(current_actor),
    ) -> JSONResponse:
        """Record a dismissal for the signed-in actor (a ``SUGG#`` overlay row).

        The row is keyed on the verified token's actor, never the body.
        ``reason``/``note`` are carried only when present — absent ≠ empty, so a
        blank dismissal is not stored as a reasoned label.
        """
        store = _improve_store_or_503(request)
        payload = body if isinstance(body, dict) else {}
        reason = payload.get("reason")
        note = payload.get("note")
        await asyncio.to_thread(
            store.put_dismissal,
            actor.actor_id,
            suggestion_id,
            _now_iso(),
            reason if isinstance(reason, str) and reason else None,
            note if isinstance(note, str) and note else None,
        )
        return _json(
            {"ok": True, "suggestionId": suggestion_id, "status": "dismissed"},
            correlation_id(request),
        )

    @app.delete("/control-plane/improve/suggestions/{suggestion_id}/dismiss")
    async def improve_undo_route(
        request: Request,
        suggestion_id: str,
        actor: ActorContext = Depends(current_actor),
    ) -> JSONResponse:
        """Undo a dismissal (delete this actor's ``SUGG#`` overlay row)."""
        store = _improve_store_or_503(request)
        await asyncio.to_thread(store.delete_dismissal, actor.actor_id, suggestion_id)
        return _json(
            {"ok": True, "suggestionId": suggestion_id, "status": "open"},
            correlation_id(request),
        )

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def request_config(app: FastAPI) -> CapabilityConfig:
    deps: Deps = app.state.deps
    return deps.config


def _now_iso() -> str:
    """UTC dismissal timestamp, matching the seeder's ``...Z`` form."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clamp_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 100
    return max(1, min(limit, MAX_LIMIT))


def _matches(item: CapabilitySummary, query: str) -> bool:
    haystacks = (
        item.display_name,
        item.asset_id or "",
        item.bare_tool_name or "",
        item.qualified_tool_name or "",
        item.target_name or "",
    )
    return any(query in field.lower() for field in haystacks)


def _json(payload: Any, correlation: str) -> JSONResponse:
    """Wrap a result with the correlation header.

    ``Any``, not ``dict``: the lab route forwards whatever a ``LabService`` handler
    returned, and the ``List*`` handlers return bare arrays.
    """
    return JSONResponse(
        content=payload, headers={CORRELATION_HEADER: correlation}
    )


def _refusing_gateway_browser() -> Any:
    """A ``GatewayBrowser`` that cannot succeed, for the control-plane host.

    ``LabService`` requires a browser to construct, but every action this process
    serves is gateway-free, so the correct browser here is one that raises.

    It matters that it *raises* rather than returning an empty projection. An empty
    one is indistinguishable from "this gateway genuinely has no tools", and
    ``ToolCollectionCRUD._validate_tool_refs`` treats absence as invalidity — so an
    allowlist mistake would not crash, it would quietly reject every valid
    ``gateway___tool`` ref. That failure already happened once on the runtime's own
    skip path (see ``main.LAB_GATEWAY_FREE_ACTIONS``); loud is better.
    """
    from lab.errors import GatewayUnavailableError
    from lab.gateway_browser import GatewayBrowser

    class _RefusingGatewayClient:
        """Implements ``GatewayClient`` by refusing both of its methods."""

        def list_tools(self, actor_id: str, timeout: float) -> Any:
            raise GatewayUnavailableError(
                "the control plane has no gateway session; this action belongs on "
                "the agent runtime"
            )

        def call_tool(
            self, tool_name: str, tool_version: str, input_data: Any, timeout: float
        ) -> Any:
            raise GatewayUnavailableError(
                "the control plane cannot invoke gateway tools; this action belongs "
                "on the agent runtime"
            )

    return GatewayBrowser(
        gateway_client=_RefusingGatewayClient(), configured_endpoints=[]
    )


def _dry_run(body: Optional[dict[str, Any]]) -> bool:
    """Resolve ``dryRun``, defaulting to a preview.

    Only an explicit JSON ``false`` destroys anything. A missing body, a missing
    key, the string ``"false"``, ``0``, ``null`` — every one of them previews.
    Truthiness would be wrong here in the one direction that matters: it would
    make a client bug that sends ``"false"`` as a string delete five versions.
    """
    if not isinstance(body, dict):
        return True
    return body.get("dryRun") is not False


@contextmanager
def _domain_errors():
    """Map canonical-layer exceptions onto status codes.

    The read routes never needed this — ``aggregate`` folds source failures into
    ``partialErrors`` — so without it a purge denial would surface as a bare 500
    and the SPA could not tell "you may not" from "the service broke".

    ``NotFoundOrForbidden`` becomes 404 rather than 403 on purpose: the same
    not-found-or-forbidden discipline the read paths follow (R5). The messages of
    the authorization and conflict errors are surfaced because the caller needs
    them — a partially applied purge must say which versions are already gone —
    while 404 stays deliberately mute.
    """
    from lab.errors import AuthorityDenied, ConflictError, NotFoundOrForbidden
    from lab.path_validator import PathTraversalError, PrefixMismatchError

    try:
        yield
    except NotFoundOrForbidden as err:
        raise HTTPException(status_code=404, detail={"code": "not_found"}) from err
    except AuthorityDenied as err:
        raise HTTPException(
            status_code=403,
            detail={"code": "authority_denied", "message": str(err)},
        ) from err
    except ConflictError as err:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": str(err)},
        ) from err
    except (PrefixMismatchError, PathTraversalError) as err:
        # The stored prefix does not match the canonical grammar. Refusing is
        # correct; the operator needs the detail to repair the row.
        logger.error("purge refused on a non-canonical prefix: %r", err)
        raise HTTPException(
            status_code=409,
            detail={"code": "prefix_mismatch", "message": str(err)},
        ) from err
    except ValueError as err:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_request", "message": str(err)},
        ) from err


def _preflight_json(preflight: Any) -> dict[str, Any]:
    """Serialize the blast-radius report for the confirmation dialog.

    ``partial`` is always present, even when empty, so the SPA cannot forget to
    handle it and quietly render "no references" over an unscanned org tier.
    """
    return {
        "assetId": preflight.published_id,
        "category": preflight.category,
        "versions": list(preflight.versions),
        "scopes": list(preflight.scopes),
        "workingCopyExists": preflight.working_copy_exists,
        "externalAccessLive": preflight.external_access_live,
        "prefixes": list(preflight.prefixes),
        "references": [
            {
                "kind": hit.kind,
                "assetId": hit.asset_id,
                "name": hit.name,
                "status": hit.status,
                "detail": hit.detail,
            }
            for hit in preflight.hits
        ],
        "partial": list(preflight.partial),
    }


def _purged_json(purged: Any) -> dict[str, Any]:
    return {
        "assetId": purged.published_id,
        "category": purged.category,
        "versionsDeleted": list(purged.versions_deleted),
        "workingCopyDeleted": purged.working_copy_deleted,
        "prefixesDeleted": list(purged.prefixes_deleted),
        "prefixesOrphaned": list(purged.prefixes_orphaned),
        "outboxEvents": purged.outbox_events,
        "preflight": _preflight_json(purged.preflight),
    }


def build_app() -> FastAPI:
    """ASGI factory — ``uvicorn capability_api.app:build_app --factory``.

    A factory rather than a module-level ``app`` for two reasons: importing
    this module stays free of side effects (so tests can import it without a
    fully configured environment), and a configuration failure surfaces as the
    container exiting at startup rather than a process that stays up serving
    500s which look like an application bug. Cross-environment configuration
    must fail closed (design "Error Handling").
    """
    try:
        return create_app()
    except ConfigError:
        logger.exception("Capability API configuration is invalid; refusing to start")
        raise


__all__ = ["Deps", "build_app", "create_app"]
