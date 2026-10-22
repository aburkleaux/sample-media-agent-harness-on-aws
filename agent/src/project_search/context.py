"""Per-invocation search context (design Component 7 `context.py`, Req 3.8, 4.15, 7.1).

Set by the Main_Agent entrypoint from the proxy-authored `__turn_context` and the ProjectToken
header. Contextvars, so concurrent invocations on one container never see each other's token.
"""
from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass
from typing import Any, Optional

from projects.ids import is_project_id

from .token_ref import ProjectTokenRef

PROJECT_TOKEN_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-ProjectToken"


@dataclass(frozen=True)
class ActiveProject:
    id: str
    name: str
    collection_id: str
    personal: bool
    # Project settings (proxy-stamped). `indexing` False: files are stored and findable by name, but
    # nothing is analyzed, so semantic search has nothing to search. Absent (an older proxy) → True.
    indexing: bool = True
    description: str = ""
    # Knowledge graph (proxy-stamped `graph: true`). Absent → False: agents get no graph tools.
    graph: bool = False


_DESCRIPTION_MAX = 500
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _clean_description(raw: Any) -> str:
    """User-authored text bound for the system prompt: one line, no control characters, bounded."""
    if not isinstance(raw, str):
        return ""
    return " ".join(_CTRL_RE.sub(" ", raw).split())[:_DESCRIPTION_MAX]


@dataclass(frozen=True)
class FindSimilar:
    """A card's Find similar action, stamped by the proxy (design Component 1, Req 3.23).

    The `image_uri` is not trusted for being here: the Search_Tool's image-path check (Component 2)
    applies to it like any other `search_image_s3uri`.
    """
    image_uri: str
    display_name: str
    timecode: str

    @classmethod
    def parse(cls, raw: Any) -> Optional["FindSimilar"]:
        """Parse one `find_similar` value; `None` when malformed (dropped, never fatal to the turn)."""
        if not isinstance(raw, dict):
            return None
        image_uri = raw.get("image_uri")
        if not isinstance(image_uri, str) or not image_uri.strip():
            return None
        return cls(image_uri=image_uri,
                   display_name=str(raw.get("display_name") or ""),
                   timecode=str(raw.get("timecode") or ""))


@dataclass(frozen=True)
class TurnContext:
    turn_id: str
    active_project: ActiveProject
    web_search: bool
    project_access: str  # "ok" | "unavailable"
    find_similar: Optional[FindSimilar] = None

    @classmethod
    def parse(cls, raw: Any) -> Optional["TurnContext"]:
        """Strictly parse `__turn_context`; anything malformed means "no turn context" (no search).

        The context is valid when `active_project` is valid and `project_access` is `ok`/`unavailable`.
        Any `search_arm`/`search_execution_mode` the proxy may still send is ignored: search is arm-A
        only (the Main_Agent runs the Search_Tool itself). A malformed `find_similar` is the one tolerant
        exception — it is dropped and the turn still runs.
        """
        if not isinstance(raw, dict):
            return None
        ap = raw.get("active_project")
        if not isinstance(ap, dict) or not is_project_id(ap.get("id")) or ap.get("collection_id") != ap.get("id"):
            return None
        access = raw.get("project_access")
        if access not in ("ok", "unavailable"):
            return None
        return cls(turn_id=str(raw.get("turn_id") or ""),
                   active_project=ActiveProject(id=ap["id"], name=str(ap.get("name") or ""),
                                                collection_id=ap["collection_id"], personal=bool(ap.get("personal")),
                                                indexing=ap.get("indexing") is not False,
                                                description=_clean_description(ap.get("description")),
                                                graph=ap.get("graph") is True),
                   web_search=bool(raw.get("web_search")), project_access=access,
                   find_similar=FindSimilar.parse(raw.get("find_similar")))


current_project_token: contextvars.ContextVar[Optional[ProjectTokenRef]] = contextvars.ContextVar(
    "current_project_token", default=None)
current_turn_context: contextvars.ContextVar[Optional[TurnContext]] = contextvars.ContextVar(
    "current_turn_context", default=None)


def header_value(request_headers: Any, name: str = PROJECT_TOKEN_HEADER) -> str:
    """Case-insensitive header read. Never log the value."""
    if not isinstance(request_headers, dict):
        return ""
    wanted = name.lower()
    for k, v in request_headers.items():
        if isinstance(k, str) and k.lower() == wanted and isinstance(v, str):
            v = v.strip()
            return v[7:].strip() if v.lower().startswith("bearer ") else v
    return ""


def graph_scope_project_id() -> Optional[str]:
    """The id a dispatch/plan should carry as `project_scope` (knowledge-graph tools for sub-agents), or None.

    Only the turn's ACTIVE Project, only when it has a graph and project access is `ok`. An id — never a token:
    the sub-agent mints its own Project_Token from its delegation grant (`delegated_token.py`).
    """
    tc = current_turn_context.get()
    if tc is None or tc.project_access != "ok" or not tc.active_project.graph:
        return None
    return tc.active_project.id
