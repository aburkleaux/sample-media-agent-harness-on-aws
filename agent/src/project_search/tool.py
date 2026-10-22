"""ProjectSearchTool: the model-visible Search_Tool (design Component 7 `tool.py`, Req 4.13, 4.14, 5.2, 7.x).

Constructed per turn (arm A) or per Search_Dispatch (arm B) with ONE ProjectTokenRef. Each call reads
`await ref.current()` just before the request, so a refreshed token is used from the next request on.
Arguments pass through unchanged except the Harness-only `asset_kinds` (stripped) and `k` (raised to
`compact.TAB_FETCH_K` for the Moments tab; the model still receives at most its own `k`). No search retry.

Every call runs the search-qa-rendering Component 2 pipeline (Req 1.18–1.23): the argument and
image-path checks (`call_checks.precheck`) run BEFORE the token is read, so a rejected call sends
nothing to the Search_Gateway and triggers no token refresh; the result is then shaped into
Compact_Hits (`compact.shape_result`) so the model never sees a raw M2C result, while the ledger and
the arm-B frame sink keep the delivered Hits in full.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Any, Callable, Optional

from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

from . import call_checks, compact, document_recall, errors, ledger, telemetry
from .client import SearchGatewayClient
from .assets import RegistryUnavailable
from .token_ref import AccessEnded, NotEstablished, ProjectTokenRef
from projects import schema

logger = logging.getLogger(__name__)

_SPEC_TTL_S = 600
_spec_cache: dict[str, tuple[float, dict]] = {}


async def fetch_spec(client: SearchGatewayClient, ref: ProjectTokenRef) -> dict:
    """Model-visible spec from the Search_Gateway's `tools/list` (Cedar filters it to the one tool).

    The spec carries the Harness-only `asset_kinds` and `search_image_asset_id` inputs
    (`compact.with_harness_inputs`); the cached value already has them. M2C receives neither.
    """
    hit = _spec_cache.get(client.tool_name)
    if hit and time.time() - hit[0] < _SPEC_TTL_S:
        return hit[1]
    tools = await client.list_tools(await ref.current())
    for t in tools:
        if t.name == client.tool_name:
            from strands.tools.mcp._compat import input_schema  # mcp 1.x/2.x field-name shim
            spec = {"name": t.name, "description": t.description or "Search the active project's media.",
                    "inputSchema": {"json": input_schema(t) or {"type": "object", "properties": {}}}}
            spec = compact.with_harness_inputs(spec)
            _spec_cache[client.tool_name] = (time.time(), spec)
            return spec
    raise LookupError(f"{client.tool_name} not offered by the search gateway")


class ProjectSearchTool(AgentTool):
    def __init__(self, ref: Optional[ProjectTokenRef], spec: dict, *, project_id: str, turn_id: str,
                 client: SearchGatewayClient, unavailable: bool = False,
                 dispatch_nonce: Optional[str] = None, frame_sink: Optional[Callable[[dict], None]] = None,
                 scope: Optional[call_checks.CallScope] = None, indexing_off: bool = False,
                 images: Any = None):
        super().__init__()
        self._ref, self._spec, self._client = ref, spec, client
        self._project_id, self._turn_id = project_id, turn_id
        self._unavailable = unavailable or ref is None
        self._indexing_off = indexing_off
        self._nonce, self._sink = dispatch_nonce, frame_sink
        self._scope = scope
        self._images = images  # Asset_Service `search_images` (AssetServiceResolver); None: no search by asset

    @property
    def tool_name(self) -> str:
        return self._spec["name"]

    @property
    def tool_spec(self) -> ToolSpec:
        return self._spec  # type: ignore[return-value]

    @property
    def tool_type(self) -> str:
        return "python"

    def __repr__(self) -> str:
        return f"<ProjectSearchTool {self.tool_name} project={self._project_id}>"

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        tool_use_id = tool_use["toolUseId"]
        args = dict(tool_use.get("input") or {})
        _t0 = time.monotonic()
        category, message, hits, text, extra = await self._run(args)
        # Search telemetry (Component 9, Req 4.1): one sample per call, timed entry→return, including
        # calls the Component 2 checks reject. "success" or the errors.py failure category.
        telemetry.note_call(
            self._turn_id,
            telemetry.CallSample(
                latency_ms=int((time.monotonic() - _t0) * 1000),
                outcome="success" if category is None else category,
            ),
        )
        ok = category is None
        if ok:
            term = args.get("search_term")
            ledger.record(ledger.SearchLedgerEntry(self._turn_id, tool_use_id, self._project_id, True, hits,
                                                   "search_tool", extra_hits=extra,
                                                   search_term=term if isinstance(term, str) else "",
                                                   query_image=_query_image_of(args)))
        if self._nonce and self._sink:
            self._sink({"type": "search_result", "nonce": self._nonce, "tool_use_id": tool_use_id,
                        "project_id": self._project_id, "ok": ok, "category": category, "hits": hits if ok else []})
        logger.info("project_search call project=%s ok=%s category=%s hits=%d", self._project_id, ok, category, len(hits))
        if ok:
            yield ToolResultEvent({"toolUseId": tool_use_id, "status": "success", "content": [{"text": text}]})
        else:
            yield ToolResultEvent({"toolUseId": tool_use_id, "status": "error",
                                   "content": [{"text": errors.error_result(category, message)}]})

    async def _run(self, args: dict):
        # 1. availability (existing): a tool bound in unavailable mode never reaches the gateway.
        if self._unavailable:
            return errors.PROJECT_ACCESS_UNAVAILABLE, errors.ACCESS_NOT_ESTABLISHED_TEXT, [], "", []
        # 1b. indexing OFF: nothing was analyzed, so a search can only come back empty. Say why instead, with
        #     no gateway call and no token read (Project settings; filename lookup stays on project_status).
        if self._indexing_off:
            return errors.INDEXING_OFF, errors.INDEXING_OFF_TEXT, [], "", []
        # 1c. search by an image ASSET: resolved to its Search_Rendition, which then goes through the same
        #     checks as any query image. Unresolvable (not an image, not ready, not found) never reaches M2C.
        if compact.SEARCH_IMAGE_ASSET_ID in args:
            resolved = await self._query_image(args)
            if isinstance(resolved, tuple):
                return resolved
            args = resolved
        # 2. argument + image-path checks, BEFORE the token is read (Req 1.19, 1.20, 1.21). A rejected
        #    call sends nothing to the gateway and triggers no refresh.
        if self._scope is not None:
            rejected = call_checks.precheck(args, self._scope)
            if rejected:
                return rejected[0], rejected[1], [], "", []
        # 3. token (existing).
        try:
            token = await self._ref.current()  # type: ignore[union-attr]
        except AccessEnded:
            return errors.PROJECT_ACCESS_ENDED, errors.ACCESS_ENDED_TEXT, [], "", []
        except NotEstablished:
            return errors.PROJECT_ACCESS_UNAVAILABLE, errors.ACCESS_NOT_ESTABLISHED_TEXT, [], "", []
        # 4. strip the Harness-only asset_kinds (Req 1.22); M2C never receives it.
        asset_kinds = args.pop("asset_kinds", None)
        # 5. call. M2C is asked for TAB_FETCH_K hits whatever the model's `k`, so the Moments tab can show
        #    more than the model reads; the model's `k` still bounds what it receives (step 7).
        model_k = args.get("k")
        call_args = {**args, "k": max(model_k or compact.DEFAULT_K, compact.TAB_FETCH_K)}
        # 5b. document recall (document_recall.py): a plain query also asks the document scope, because a
        #     plain M2C query ranks every PDF page under the video shots. In parallel when the query targets
        #     documents; otherwise only after the plain result shows documents are in play (companion_plan).
        companion_term = document_recall.companion_term(args, asset_kinds)
        plan = document_recall.companion_plan(args, asset_kinds, self._project_id) if companion_term else None
        companion_args = {**call_args, "search_term": companion_term}
        if plan == document_recall.PARALLEL:
            outcome, companion = await asyncio.gather(
                self._client.call(token, call_args),
                self._client.call(token, companion_args),
                return_exceptions=True)
            if isinstance(outcome, BaseException):
                raise outcome
        else:
            outcome, companion = await self._client.call(token, call_args), None
            if (plan == document_recall.DEFERRED and not errors.classify(outcome)
                    and document_recall.deferred_needed(outcome.result, model_k or compact.DEFAULT_K, asset_kinds)):
                try:
                    companion = await self._client.call(token, companion_args)
                except Exception as exc:  # noqa: BLE001 - the companion never fails the search
                    companion = exc
        # 6. classify (existing).
        cls = errors.classify(outcome)
        if cls:
            return cls[0], cls[1], [], "", []
        document_recall.note_documents(
            self._project_id, outcome.result,
            companion.result if isinstance(companion, errors.SearchOutcome) else None)
        if companion is not None and not isinstance(companion, BaseException) and not errors.classify(companion):
            outcome = dataclasses.replace(outcome, result=document_recall.merge(outcome.result, companion.result))
        elif companion is not None:
            logger.info("project_search document companion failed; plain results only")
        # 7. shape into Compact_Hits (Req 1.18, 1.22, 1.23). The model gets model_text(); the ledger and
        #    the arm-B frame sink get the full delivered Hits; the overflow goes to the ledger for the tab.
        shaped = compact.shape_result(outcome.result, k=model_k, asset_kinds=asset_kinds)
        return None, "", shaped.delivered, shaped.model_text(), shaped.overflow

    async def _query_image(self, args: dict):
        """``args`` with ``search_image_asset_id`` replaced by that image's rendition URI, or an error 5-tuple."""
        args = dict(args)
        asset_id = args.pop(compact.SEARCH_IMAGE_ASSET_ID)

        def fail(text):
            return errors.OTHER, text, [], "", []

        if args.get("search_term") or args.get("search_image_s3uri"):
            return fail(QUERY_ONE_OF_TEXT)
        if not (isinstance(asset_id, str) and schema.is_asset_id(asset_id)):
            return fail(QUERY_ASSET_ID_TEXT)
        if schema.asset_kind(asset_id) != "image":
            return fail(QUERY_NOT_SUPPORTED_TEXT)
        session_id = _bound_session_id() or (self._scope.session_id if self._scope is not None else "")
        if self._images is None or not session_id:
            return fail(QUERY_LOOKUP_FAILED_TEXT)
        try:
            images = await asyncio.to_thread(self._images.search_images, self._project_id, [asset_id], session_id)
        except RegistryUnavailable:
            logger.info("project_search query image lookup failed", exc_info=True)
            return fail(QUERY_LOOKUP_FAILED_TEXT)
        entry = images.get(asset_id) or {"reason": "not_found"}
        uri = entry.get("uri")
        if isinstance(uri, str) and uri:
            args["search_image_s3uri"] = uri
            return args
        return fail(_QUERY_REASON_TEXT.get(str(entry.get("reason")), QUERY_NOT_FOUND_TEXT))


def _query_image_of(args: dict):
    """The query image of a call's ORIGINAL arguments (before an asset id is resolved to its rendition):
    ``{"assetId": …}`` or ``{"s3uri": …}``, else ``None``."""
    asset_id = args.get(compact.SEARCH_IMAGE_ASSET_ID)
    if isinstance(asset_id, str) and schema.is_asset_id(asset_id):
        return {"assetId": asset_id}
    uri = args.get("search_image_s3uri")
    if isinstance(uri, str) and uri.startswith("s3://"):
        return {"s3uri": uri}
    return None


def _bound_session_id() -> str:
    """The session id bound for THIS call. Read at call time, not from the CallScope: ``main`` builds the scope
    (``prepare_turn``) before it binds the request context, so the scope's session id is empty in a real turn
    (staging 2026-10-15: every search by an image asset failed "could not be looked up", in 0 ms)."""
    try:
        from jobs.context import current_session_id
        return current_session_id() or ""
    except Exception:  # noqa: BLE001 - no bound session (tests, management calls)
        return ""


QUERY_ONE_OF_TEXT = "pass exactly one of search_term, search_image_s3uri and search_image_asset_id"
QUERY_ASSET_ID_TEXT = "search_image_asset_id must be an image asset_id (image:<uuid>) of the active project"
QUERY_NOT_SUPPORTED_TEXT = (
    "only an image can be a search query: search by video or audio is not supported at the moment. Tell the "
    "user so; a text search describing what they are looking for still works")
QUERY_NOT_READY_TEXT = "that image is still being prepared for search; try again in a minute"
QUERY_NOT_FOUND_TEXT = "that image is not in the active project (or is another chat's private upload)"
QUERY_LOOKUP_FAILED_TEXT = "the image could not be looked up; try the search again"
QUERY_UNREADABLE_TEXT = "that image cannot be read as a search query (it may be corrupt or too large)"
_QUERY_REASON_TEXT = {"not_image": QUERY_NOT_SUPPORTED_TEXT, "not_ready": QUERY_NOT_READY_TEXT,
                      "not_found": QUERY_NOT_FOUND_TEXT, "unreadable": QUERY_UNREADABLE_TEXT}

