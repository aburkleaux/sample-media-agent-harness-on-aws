"""The Project_Collection on hybrid-search (Req 4.1–4.3; design Component 11 "Collection").

Named ``collection`` rather than the design's ``collections`` so it can never shadow the standard library.
The collection id IS the project id, and the collection is created with exactly the four scopes the pipeline
indexes. ``create_collection`` is create-if-absent on hybrid-search's side (``created: false`` when it already
exists), and an "already exists" refusal also counts as success.
"""
from __future__ import annotations

from typing import Any, Optional

from .m2c import ToolError

CREATE_TOOL = "hybrid-search___create_collection"
DELETE_TOOL = "hybrid-search-admin___delete_collection"
CONFIGURE_TOOL = "hybrid-search-admin___configure_search_backend"
# Optional: read to MERGE the weights (toolkits from alpha.20261017a refuse a write that omits a stored sub-query).
LIST_TOOL = "hybrid-search___list_collections"
ACTIVE_SCOPES = ("shot", "scene", "utterance", "topic")

# Retrieval weights for every Project_Collection: hybrid-search's convention defaults (returned by
# create_collection) plus topic sub-queries. With the defaults a topic summary is reachable only through
# named-entity FTS, so "what is X about" — no entities, decomposed to the `visual` path alone — returns
# frames and `#topic` is skipped outright ("no runnable sub-queries"). The topic weights are LOW on purpose:
# they make `#topic` runnable without letting summaries crowd ordinary searches. Measured on Personal
# (staging, 8 assets): at visual.topic_ann 0.6 "a wolf" returned Meridian topic summaries at ranks 2-3; at
# 0.45 one at rank 5; at 0.3 none, while "#topic what is the tidal line trailer about" returns that
# trailer's summary at 1.00 vs 0.37 for the next. The Media_Search_Skill sends "about / summarize" questions
# with `#topic`. Sub-query keys are `<scope>_ann` / `<scope>_<field>_fts` (the tool does not validate
# them, so a typo silently does nothing — keep them in step with the convention defaults above).
#
# This is an OVERLAY, not the full set: the toolkit's own sub-queries stay (alpha.20261017a stores
# `dialogue.topic_ann` and refuses a write that would drop it — PARTIAL_REPLACEMENT_PAYLOAD). See `merged_paths`.
RETRIEVAL_PATHS: dict = {
    "visual": {"shot_ann": 0.7, "scene_ann": 0.5, "topic_ann": 0.3},
    "text_overlay": {"scene_text_fts": 0.7},
    "entity_fts": {"scene_named_entities_fts": 0.9, "topic_named_entities_fts": 0.8},
    "dialogue": {"utterance_text_fts": 0.8, "utterance_ann": 0.4, "topic_text_fts": 0.3},
}


def create_arguments(pid: str, project_name: str) -> dict:
    return {"collection_id": pid, "active_scopes": list(ACTIVE_SCOPES), "name": project_name or pid}


def configure_arguments(pid: str, retrieval_paths: Optional[dict] = None) -> dict:
    return {"collection_id": pid, "retrieval_paths": RETRIEVAL_PATHS if retrieval_paths is None else retrieval_paths}


def merged_paths(stored: Optional[dict]) -> dict:
    """The harness weights overlaid on the stored ones at sub-query depth: ours win where both name a sub-query,
    and a stored-only sub-query (the toolkit's ``dialogue.topic_ann``) is kept. Never ``replace_all`` — that
    would wipe the toolkit's own tuning."""
    out: dict = {}
    for path, subs in (stored or {}).items():
        if isinstance(subs, dict):
            out[path] = {k: v for k, v in subs.items() if isinstance(v, (int, float))}
    for path, subs in RETRIEVAL_PATHS.items():
        out[path] = {**out.get(path, {}), **subs}
    return out


def stored_paths(client: Any, pid: str) -> Optional[dict]:
    """The collection's stored ``retrieval_paths``, or ``None`` when they cannot be read (an older toolkit without
    the tool or the field, or any read error) — the caller then writes the harness set as before."""
    try:
        listed = client.call_tool(LIST_TOOL, {})
    except Exception:  # noqa: BLE001 — optional read: fall back to the full write
        return None
    for c in (listed or {}).get("collections") or []:
        if isinstance(c, dict) and c.get("collection_id") == pid:
            rp = c.get("retrieval_paths")
            return rp if isinstance(rp, dict) else None
    return None


def ensure(client: Any, pid: str, project_name: str) -> bool:
    """Create the collection if absent and set its weights. Returns ``True`` when this call created it.

    The weights are applied on every call, not only on creation: a call that created the collection but
    failed to configure it is retried, finds the collection already there, and must still configure it.
    They are MERGED with what the collection already stores, and not written at all when nothing would change.
    Raises on real failure; a failure of the configure step carries ``step = "configure_search_backend"`` so it
    is reported as that, not as a create_collection failure (its class, e.g. ``Transient``, is unchanged).
    """
    created = False
    try:
        res = client.call_tool(CREATE_TOOL, create_arguments(pid, project_name))
        created = bool(res.get("created"))
    except ToolError as exc:
        if "exist" not in exc.text.lower():
            raise
    stored = stored_paths(client, pid)
    paths = RETRIEVAL_PATHS if stored is None else merged_paths(stored)
    if stored is not None and _same(paths, stored):
        return created  # steady state: no write (and no weights_locked flip) on every tick
    try:
        client.call_tool(CONFIGURE_TOOL, configure_arguments(pid, paths))
    except Exception as exc:
        exc.step = "configure_search_backend"  # type: ignore[attr-defined]
        raise
    return created


def _same(a: dict, b: dict) -> bool:
    norm = lambda d: {p: {k: float(v) for k, v in (s or {}).items() if isinstance(v, (int, float))}  # noqa: E731
                      for p, s in (d or {}).items() if isinstance(s, dict)}
    return norm(a) == norm(b)


def delete(client: Any, pid: str) -> bool:
    """Delete the collection; ``False`` when it did not exist (that counts as success, Req 10.6c)."""
    try:
        client.call_tool(DELETE_TOOL, {"collection_id": pid})
    except ToolError as exc:
        if exc.code == "NOT_FOUND" or "does not exist" in exc.text.lower():
            return False
        raise
    return True
