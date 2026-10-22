"""Asset resolution against the Project_Registry (design Component 4 "Asset resolution", Req 2.5).

Citation verification and the Moments_Rule judge each Hit by the Project_Asset it resolves to **at
turn end**: same Active_Project, status not ``removed``, Display_Name equal to the cited name
case-insensitively. The Main_Agent runtime never reads the registry table directly; it reaches it
only through the ingestion **Asset_Service** Lambda, exactly as the Project_Tools do.

This module is the agent-side client of that one read-only command. It defines:

- :class:`ResolvedAsset` — the ``(kind, display_name, status)`` a resolved id carries.
- :class:`RegistryUnavailable` — raised when the whole resolution could not be trusted.
- :class:`AssetResolver` — the ``Protocol`` the turn-end path and the Moments_Rule depend on.
- :class:`AssetServiceResolver` — the deployed implementation: a boto3 Lambda ``Invoke`` of
  ``ASSET_SERVICE_FN`` (hydrated into the env from the ``RuntimeConfigSecret``), paged at 100 ids.

Cross-spec: the Asset_Service Lambda and its ``resolve_assets`` command are owned by
**media-harness-project-ingestion** and are not built here. This module only encodes the
request/reply contract that this spec depends on, so the ingestion spec can match it:

    # event (one page; <= 100 ids)
    {"userToken": "<Cognito access token>", "projectId": "p…", "command": "resolve_assets",
     "args": {"assetIds": ["video:…", …]}, "caller": "main_agent",
     "sessionId": "…", "turnId": "…"}          # sessionId/turnId best-effort, omitted when unbound

    # reply (always a JSON object, never a Lambda error for a domain refusal)
    {"ok": true, "value": {"assets": {"<asset_id>": {"kind": "video"|"image"|"audio",
                                                     "displayName": "<Display_Name>",
                                                     "status": "<Ingestion_Status>"}}}}
    {"ok": false, "status": 400|401|404|409|503, "error": {"code": "…", "message": "…"}}

The Lambda omits malformed or absent ids and ids of another Project, so an id simply does not appear
in ``assets``. A non-ok reply (any ``status``, including a ``404``/denial meaning the Actor lost
access mid-turn), a Lambda ``FunctionError``, an unparseable payload, or an invoke exception all map
to :class:`RegistryUnavailable`; the turn-end path treats every one the same way.

The boto3 client is constructed lazily and the call runs synchronously, which is correct: the design
drives ``resolve`` from an ``asyncio.to_thread`` worker, so a blocking boto3 call does not stall the
event loop.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:  # typing only; the deployed path never imports the store
    from .session_hits import SessionHitStore

logger = logging.getLogger(__name__)

ASSET_SERVICE_FN_ENV = "ASSET_SERVICE_FN"
RESOLVE_COMMAND = "resolve_assets"
SEARCH_IMAGES_COMMAND = "search_images"
CALLER = "main_agent"
PAGE_SIZE = 100  # Asset_Service accepts at most 100 ids per resolve_assets call.

from .compact import ASSET_KINDS as _ASSET_KINDS  # noqa: E402 -- every searchable kind


@dataclass(frozen=True)
class ResolvedAsset:
    """One Project_Asset the registry resolved an ``asset_id`` to, at turn end."""

    asset_id: str
    kind: str  # "video" | "image" | "audio"
    display_name: str  # the Project_Asset's Display_Name
    status: str  # Ingestion_Status (e.g. "indexed", "removed", "failed", "queued")


class RegistryUnavailable(Exception):
    """The Project_Registry could not be reached or trusted for this resolution.

    Raised on any invoke error, a Lambda ``FunctionError``, an unparseable or non-ok reply, or a
    denial. A denial means the Actor lost access to the Project mid-turn, which the turn-end path
    handles identically: it marks the turn's Citations ``registry_unavailable`` rather than guessing.
    """


@runtime_checkable
class AssetResolver(Protocol):
    def resolve(
        self, project_id: str, asset_ids: Sequence[str]
    ) -> dict[str, "ResolvedAsset"]:
        """Resolve ``asset_ids`` under ``project_id`` to Project_Assets.

        Returns a map ``asset_id -> ResolvedAsset`` with absent, malformed, or other-Project ids
        omitted. Raises :class:`RegistryUnavailable` on any invoke error, a non-ok reply, or a denial.
        """
        ...


def _distinct(asset_ids: Sequence[str]) -> list[str]:
    """Non-empty string ids, de-duplicated, order preserved (avoids redundant registry reads)."""
    seen: set[str] = set()
    out: list[str] = []
    for aid in asset_ids:
        if isinstance(aid, str) and aid and aid not in seen:
            seen.add(aid)
            out.append(aid)
    return out


def _pages(ids: list[str], size: int = PAGE_SIZE):
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


class AssetServiceResolver:
    """Deployed :class:`AssetResolver`: a boto3 Lambda ``Invoke`` of the ingestion Asset_Service.

    ``function_name`` defaults to ``ASSET_SERVICE_FN`` from the ``RuntimeConfigSecret``-hydrated env.
    The caller is always ``"main_agent"`` (only the Main_Agent runtime role may invoke the writer),
    and the turn's User_Token is read from the per-invocation request context, like the Project_Tools.
    """

    def __init__(
        self,
        function_name: Optional[str] = None,
        *,
        token_source: Optional[Callable[[], str]] = None,
        client=None,
    ):
        self.function_name = (
            function_name
            if function_name is not None
            else os.environ.get(ASSET_SERVICE_FN_ENV, "")
        )
        self._token_source = token_source or _default_token_source
        self._client = client  # lazily built on first use when None

    def _lambda(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "lambda",
                region_name=os.environ.get("AWS_REGION", "us-west-2"),
                config=Config(
                    connect_timeout=2,
                    read_timeout=10,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )
        return self._client

    def _event(self, project_id: str, page: list[str], command: str = RESOLVE_COMMAND,
               args: Optional[dict] = None) -> dict:
        event = {
            "userToken": self._token_source() or "",
            "projectId": project_id,
            "command": command,
            "args": args if args is not None else {"assetIds": page},
            "caller": CALLER,
        }
        # sessionId / turnId are best-effort telemetry fields of the shared envelope; include them
        # only when the invocation bound them, never fail resolution over a missing binding.
        session_id, turn_id = _request_ids()
        if session_id:
            event["sessionId"] = session_id
        if turn_id:
            event["turnId"] = turn_id
        return event

    def _invoke_page(self, project_id: str, page: list[str]) -> dict:
        return _assets_of(self._invoke(self._event(project_id, page)))

    def _invoke(self, event: dict) -> object:
        """One Asset_Service invoke; the parsed reply body. :class:`RegistryUnavailable` on any failure."""
        if not self.function_name:
            raise RegistryUnavailable(f"{ASSET_SERVICE_FN_ENV} is not configured")
        payload = json.dumps(event).encode()
        try:
            resp = self._lambda().invoke(
                FunctionName=self.function_name, Payload=payload
            )
        except Exception as exc:  # noqa: BLE001 - every invoke failure degrades the same way
            raise RegistryUnavailable(
                f"asset_service invoke failed: {type(exc).__name__}"
            ) from exc
        if resp.get("FunctionError"):
            raise RegistryUnavailable("asset_service returned a FunctionError")
        try:
            body = json.loads(resp["Payload"].read())
        except Exception as exc:  # noqa: BLE001
            raise RegistryUnavailable("asset_service reply was not valid JSON") from exc
        return body

    def search_images(self, project_id: str, asset_ids: Sequence[str], session_id: str) -> dict[str, dict]:
        """``search_images``: per asset id its Search_Rendition ``{"uri", "displayName"}`` or ``{"reason"}``.

        Blocking (a Lambda invoke): an async caller runs it in a thread. Raises :class:`RegistryUnavailable`.
        """
        ids = _distinct(asset_ids)
        if not ids:
            return {}
        body = self._invoke(self._event(project_id, ids, SEARCH_IMAGES_COMMAND,
                                        {"assetIds": ids, "sessionId": session_id}))
        if not isinstance(body, dict) or body.get("ok") is not True:
            status = body.get("status") if isinstance(body, dict) else None
            raise RegistryUnavailable(f"asset_service search_images not ok (status={status})")
        images = (body.get("value") or {}).get("images") if isinstance(body.get("value"), dict) else None
        if not isinstance(images, dict):
            raise RegistryUnavailable("asset_service reply missing value.images")
        return {k: v for k, v in images.items() if isinstance(k, str) and isinstance(v, dict)}

    def resolve(
        self, project_id: str, asset_ids: Sequence[str]
    ) -> dict[str, ResolvedAsset]:
        ids = _distinct(asset_ids)
        if not ids:
            return {}
        resolved: dict[str, ResolvedAsset] = {}
        for page in _pages(ids):
            for asset_id, raw in self._invoke_page(project_id, page).items():
                asset = _resolved_asset(asset_id, raw)
                if asset is not None:
                    resolved[asset_id] = asset
        return resolved


def _assets_of(body: object) -> dict:
    """The ``value.assets`` map of an ok reply, or raise :class:`RegistryUnavailable`."""
    if not isinstance(body, dict) or body.get("ok") is not True:
        status = body.get("status") if isinstance(body, dict) else None
        raise RegistryUnavailable(f"asset_service reply not ok (status={status})")
    value = body.get("value")
    assets = value.get("assets") if isinstance(value, dict) else None
    if not isinstance(assets, dict):
        raise RegistryUnavailable("asset_service reply missing value.assets")
    return assets


def _resolved_asset(asset_id: str, raw: object) -> Optional[ResolvedAsset]:
    """A :class:`ResolvedAsset`, or ``None`` for a malformed entry (treated as absent)."""
    if not isinstance(asset_id, str) or not asset_id or not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    display_name = raw.get("displayName")
    status = raw.get("status")
    if not (isinstance(kind, str) and isinstance(display_name, str) and isinstance(status, str)):
        return None
    if kind not in _ASSET_KINDS:
        return None
    return ResolvedAsset(
        asset_id=asset_id, kind=kind, display_name=display_name, status=status
    )


def _default_token_source() -> str:
    """The turn's forwarded User_Token, or ``""`` when none is bound (e.g. outside a request)."""
    try:
        from jobs.context import current_user_token
    except Exception:  # noqa: BLE001 - jobs package absent in some test contexts
        return ""
    return current_user_token() or ""


def _request_ids() -> tuple[str, str]:
    """``(session_id, turn_id)`` from the per-invocation context, best-effort (never raises)."""
    session_id = ""
    turn_id = ""
    try:
        from jobs.context import current_session_id, current_turn_id

        try:
            session_id = current_session_id() or ""
        except Exception:  # noqa: BLE001 - unbound session id is normal in tests/startup
            session_id = ""
        turn_id = current_turn_id() or ""
    except Exception:  # noqa: BLE001
        return "", ""
    return session_id, turn_id


# ---------------------------------------------------------------------------
# Local-dev-only resolvers and the env factory (design "Local end-to-end run").
#
# These exist ONLY so the agent side can be driven end to end on the local
# harness before media-harness-project-ingestion ships the real Asset_Service.
# They are NEVER selected in a deployed environment: `resolver_from_env` refuses
# every value of ``LOCAL_DEV_ASSET_RESOLVER`` (logs it, returns ``None`` so the
# caller falls back to :class:`AssetServiceResolver`) when ``DEPLOY_ENV`` names a
# deployed env, and ``registry`` additionally requires a loopback
# ``PROJECTS_DDB_ENDPOINT`` so it can only ever read a local moto table.
# ---------------------------------------------------------------------------

LOCAL_DEV_ASSET_RESOLVER_ENV = "LOCAL_DEV_ASSET_RESOLVER"
PROJECTS_TABLE_ENV = "PROJECTS_TABLE"
PROJECTS_DDB_ENDPOINT_ENV = "PROJECTS_DDB_ENDPOINT"
DEPLOY_ENV_ENV = "DEPLOY_ENV"

# ``DEPLOY_ENV`` values that are real deployed environments; a dev resolver is
# refused for any of them. Locally the design sets ``DEPLOY_ENV=local`` (any
# value outside this set, including unset, counts as local).
DEPLOYED_ENVS = frozenset({"sandbox", "staging", "prod", "source", "customer"})

ASSET_SK_PREFIX = "ASSET#"
PROJECT_PK_PREFIX = "PROJECT#"

# infra/lib/lambdas/token-issuer, which holds access.py (and the tokens.py it imports).
_TOKEN_ISSUER_DIR = (
    Path(__file__).resolve().parents[3] / "infra" / "lib" / "lambdas" / "token-issuer"
)


def _is_loopback_endpoint(url: str) -> bool:
    """True only for an ``http(s)://127.0.0.1|localhost|::1[:port]`` endpoint (a local moto server)."""
    if not isinstance(url, str) or not url:
        return False
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")


def _is_local_env(deploy_env: str) -> bool:
    """True when ``DEPLOY_ENV`` is not one of the deployed environments (so a dev seam may run)."""
    return (deploy_env or "").strip().lower() not in DEPLOYED_ENVS


def _load_access():
    """Import the token-issuer ``access`` module, adding its dir to ``sys.path`` on first use.

    This module (and the ``tokens`` it imports) lives under ``infra/lib/lambdas/token-issuer`` and is
    not on the agent runtime's path. Imported lazily, and only by :class:`LocalRegistryResolver`, so a
    deployed runtime that never builds that resolver never touches this path.
    """
    try:
        import access  # type: ignore

        return access
    except ImportError:
        import sys

        issuer_dir = str(_TOKEN_ISSUER_DIR)
        if issuer_dir not in sys.path:
            sys.path.insert(0, issuer_dir)
        import access  # type: ignore

        return access


def _default_actor_source() -> str:
    """The current turn's Actor sub, or ``""`` when none is bound (outside a request)."""
    try:
        from jobs.context import current_actor_id

        return current_actor_id() or ""
    except Exception:  # noqa: BLE001 - unbound actor is normal in tests/startup
        return ""


class LocalRegistryResolver:
    """Local ``registry`` :class:`AssetResolver`: the seeded moto Projects table read directly.

    Mirrors the Asset_Service ``resolve_assets`` semantics over the local stack
    (``local_projects_stack.py --seed-demo``): it runs the token-issuer ``access.check_access`` for the
    turn's Actor against the Project (a denial raises :class:`RegistryUnavailable`, meaning the Actor
    lost access), then a single consistent ``BatchGetItem`` of the ``ASSET#<id>`` rows, mapping each to
    a :class:`ResolvedAsset`. Absent, malformed, or other-Project ids are simply omitted. ``removed`` is
    honored — the status is returned as stored, never rewritten — so eligibility stays a downstream
    decision.

    Only ever constructed by :func:`resolver_from_env` after it confirms a loopback
    ``PROJECTS_DDB_ENDPOINT``, so it can read nothing but a local moto table.
    """

    def __init__(
        self,
        *,
        table_name: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        region_name: Optional[str] = None,
        table=None,
        actor_source: Optional[Callable[[], str]] = None,
    ):
        self._table_name = (
            table_name if table_name is not None else os.environ.get(PROJECTS_TABLE_ENV, "")
        )
        self._endpoint_url = (
            endpoint_url
            if endpoint_url is not None
            else os.environ.get(PROJECTS_DDB_ENDPOINT_ENV, "")
        )
        self._region_name = region_name or os.environ.get("AWS_REGION", "us-west-2")
        self._table = table  # injectable for tests; built lazily when None
        self._actor_source = actor_source or _default_actor_source

    def _open_table(self):
        if self._table is None:
            access = _load_access()
            if not self._table_name:
                raise RegistryUnavailable(f"{PROJECTS_TABLE_ENV} is not configured")
            self._table = access.open_table(
                self._table_name,
                endpoint_url=self._endpoint_url or None,
                region_name=self._region_name,
            )
        return self._table

    def _batch_get_assets(self, table, project_id: str, page: list[str]) -> dict[str, dict]:
        """One consistent ``BatchGetItem`` of ``ASSET#<id>`` rows; map ``asset_id -> row``."""
        pk = f"{PROJECT_PK_PREFIX}{project_id}"
        keys = [{"PK": pk, "SK": f"{ASSET_SK_PREFIX}{aid}"} for aid in page]
        client = table.meta.client
        name = table.name
        request = {name: {"Keys": keys, "ConsistentRead": True}}
        resp = client.batch_get_item(RequestItems=request)
        items = list(resp.get("Responses", {}).get(name, []))
        unprocessed = resp.get("UnprocessedKeys") or {}
        if unprocessed.get(name, {}).get("Keys"):
            resp = client.batch_get_item(RequestItems=unprocessed)
            items.extend(resp.get("Responses", {}).get(name, []))
        out: dict[str, dict] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            aid = item.get("assetId")
            if not isinstance(aid, str) or not aid:
                sk = item.get("SK")
                aid = sk[len(ASSET_SK_PREFIX) :] if isinstance(sk, str) and sk.startswith(ASSET_SK_PREFIX) else None
            if aid:
                out[aid] = item
        return out

    def resolve(self, project_id: str, asset_ids: Sequence[str]) -> dict[str, ResolvedAsset]:
        access = _load_access()
        table = self._open_table()
        try:
            result = access.check_access(table, self._actor_source() or "", project_id)
        except RegistryUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - a throttle/client error is not a denial
            raise RegistryUnavailable(
                f"local registry access check failed: {type(exc).__name__}"
            ) from exc
        if not getattr(result, "ok", False):
            raise RegistryUnavailable("local registry access denied")

        ids = _distinct(asset_ids)
        if not ids:
            return {}
        resolved: dict[str, ResolvedAsset] = {}
        try:
            for page in _pages(ids):
                for asset_id, raw in self._batch_get_assets(table, project_id, page).items():
                    asset = _resolved_asset(asset_id, raw)
                    if asset is not None:
                        resolved[asset_id] = asset
        except Exception as exc:  # noqa: BLE001 - any read failure degrades uniformly
            raise RegistryUnavailable(
                f"local registry read failed: {type(exc).__name__}"
            ) from exc
        return resolved


class HitTitleResolver:
    """Local ``hits`` :class:`AssetResolver`: resolve ids from the turn's own Hits, no registry at all.

    The aliased demo collection returns M2C ``asset_id``s that are not registered in the local Projects
    table, so ``resolve_assets`` would omit them all and the Moments_Rule would render nothing. This
    fallback instead resolves each requested id from the session's stored Hits (:class:`SessionHitStore`
    for the current ``session_id``): the ``kind`` from the id's ``"<kind>:"`` prefix, the ``display_name``
    from the most recent Hit whose ``asset_id`` matches, and the ``status`` always ``"indexed"`` (a
    non-``removed`` value, so every resolved asset stays eligible). An id with no matching Hit, no title,
    or an unknown kind prefix is omitted, matching the registry's "absent id" behavior.
    """

    _NON_REMOVED_STATUS = "indexed"

    def __init__(
        self,
        hit_store: "SessionHitStore",
        *,
        session_source: Optional[Callable[[], str]] = None,
    ):
        self._hit_store = hit_store
        self._session_source = session_source or _default_session_source

    def resolve(self, project_id: str, asset_ids: Sequence[str]) -> dict[str, ResolvedAsset]:
        ids = _distinct(asset_ids)
        if not ids:
            return {}
        session_id = self._session_source() or ""
        if not session_id:
            return {}
        try:
            hits = self._hit_store.hits(session_id, project_id)
        except Exception as exc:  # noqa: BLE001 - a store read failure degrades uniformly
            raise RegistryUnavailable(
                f"session hit store read failed: {type(exc).__name__}"
            ) from exc

        # Most recent title per asset_id (hits() returns newest first, so the first wins).
        titles: dict[str, str] = {}
        for sh in hits:
            hit = getattr(sh, "hit", None)
            if not isinstance(hit, dict):
                continue
            aid = hit.get("asset_id")
            title = hit.get("title")
            if isinstance(aid, str) and aid and aid not in titles and isinstance(title, str) and title:
                titles[aid] = title

        resolved: dict[str, ResolvedAsset] = {}
        for asset_id in ids:
            title = titles.get(asset_id)
            if title is None:
                continue
            kind = asset_id.split(":", 1)[0] if ":" in asset_id else ""
            if kind not in _ASSET_KINDS:
                continue
            resolved[asset_id] = ResolvedAsset(
                asset_id=asset_id,
                kind=kind,
                display_name=title,
                status=self._NON_REMOVED_STATUS,
            )
        return resolved


def _default_session_source() -> str:
    """The current turn's chat session id, or ``""`` when none is bound."""
    try:
        from jobs.context import current_session_id

        return current_session_id() or ""
    except Exception:  # noqa: BLE001 - unbound session id is normal in tests/startup
        return ""


def resolver_from_env(
    *,
    hit_store: "Optional[SessionHitStore]" = None,
    env: "Optional[dict]" = None,
) -> Optional[AssetResolver]:
    """Select a local dev :class:`AssetResolver` from ``LOCAL_DEV_ASSET_RESOLVER``, or ``None``.

    Returns ``None`` (so the caller falls back to :class:`AssetServiceResolver`) when no dev resolver is
    requested, when it is refused, or when its preconditions are not met. Every refusal is logged. This
    is the one gate that keeps a dev seam out of a deployed environment:

    - unset / empty ``LOCAL_DEV_ASSET_RESOLVER`` -> ``None`` (nothing requested; not logged as a refusal);
    - ``DEPLOY_ENV`` in :data:`DEPLOYED_ENVS` (``sandbox``/``staging``/``prod``/``source``) -> refused;
    - ``registry`` additionally requires a loopback ``PROJECTS_DDB_ENDPOINT`` -> else refused;
    - ``hits`` requires a ``hit_store`` -> else refused;
    - any other value -> refused.
    """
    env = os.environ if env is None else env
    mode = (env.get(LOCAL_DEV_ASSET_RESOLVER_ENV, "") or "").strip().lower()
    if not mode:
        return None  # no dev resolver requested; deployed path is used

    deploy_env = (env.get(DEPLOY_ENV_ENV, "") or "").strip().lower()
    if not _is_local_env(deploy_env):
        logger.warning(
            "%s=%r refused: DEPLOY_ENV=%r is a deployed environment; using AssetServiceResolver",
            LOCAL_DEV_ASSET_RESOLVER_ENV,
            mode,
            deploy_env,
        )
        return None

    if mode == "registry":
        endpoint = env.get(PROJECTS_DDB_ENDPOINT_ENV, "") or ""
        if not _is_loopback_endpoint(endpoint):
            logger.warning(
                "%s=registry refused: %s=%r is not a loopback endpoint; using AssetServiceResolver",
                LOCAL_DEV_ASSET_RESOLVER_ENV,
                PROJECTS_DDB_ENDPOINT_ENV,
                endpoint,
            )
            return None
        logger.info(
            "%s=registry: using LocalRegistryResolver over %s (%s)",
            LOCAL_DEV_ASSET_RESOLVER_ENV,
            env.get(PROJECTS_TABLE_ENV, ""),
            endpoint,
        )
        return LocalRegistryResolver(
            table_name=env.get(PROJECTS_TABLE_ENV, ""),
            endpoint_url=endpoint,
            region_name=env.get("AWS_REGION", "us-west-2"),
        )

    if mode == "hits":
        if hit_store is None:
            logger.warning(
                "%s=hits refused: no SessionHitStore available; using AssetServiceResolver",
                LOCAL_DEV_ASSET_RESOLVER_ENV,
            )
            return None
        logger.info("%s=hits: using HitTitleResolver", LOCAL_DEV_ASSET_RESOLVER_ENV)
        return HitTitleResolver(hit_store)

    logger.warning(
        "%s=%r refused: unknown resolver; using AssetServiceResolver",
        LOCAL_DEV_ASSET_RESOLVER_ENV,
        mode,
    )
    return None
