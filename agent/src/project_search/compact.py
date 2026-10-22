"""Compact_Hits shaping and the Asset_Kind_Filter (search-qa-rendering design Component 2, Req 1.18, 1.22, 1.23).

Pure, no I/O. Every Search_Tool call (arm A and inside a Search_Dispatch) shapes its result here: the
model receives Compact_Hits (reduced fields, truncated text), while the ledger keeps the same Hits in
full. `with_asset_kinds` adds the optional `asset_kinds` input property to the gateway tool spec; the
filter is applied by the harness after the search, so it may leave fewer than `k` hits.

The Search_Tool asks M2C for :data:`TAB_FETCH_K` hits whatever the model's `k` (2026-10-10): the model
still receives at most `k` Compact_Hits, and the hits ranked after them (``overflow``) feed only the
Moments tab — never citation verification, the Session_Hit_Store, or the frame-key allowlist.
"""
from __future__ import annotations

import copy
import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from projects.document_locator import page_of_span  # the page a document hit's span encodes

from .ledger import hits_from_result

# The kinds a search can return -- every indexed kind (`projects.schema.ASSET_KINDS` minus `data`, which is
# never indexed). ONE list for the agent's asset_kinds enum, turn-end asset resolution and the Moments
# card; pinned against the proxy and the SPA by `infra/lib/projects/fixtures/search-kinds.json`. A document
# was missing here (and in all its copies) after PDFs became indexed, so every document hit was dropped.
ASSET_KINDS = ("video", "image", "audio", "document")
# Kinds `show_asset` can open in the Renderer: every searchable kind (a document as an embedded PDF at its page).
# `data` files are not openable there.
OPENABLE_KINDS = ASSET_KINDS

DEFAULT_K = 10
# How many hits the Search_Tool requests from M2C for the Moments tab (the `k` ceiling call_checks enforces).
TAB_FETCH_K = 50
# Model-visible length caps (2026-10-16: the model text is what a search turn's second model call pays for).
# A media hit's `text` is a scene description / transcript line / topic summary: p90 564, max ~1,200 chars in
# staging. A document hit's `text` is a PDF page chunk (up to 4,000) and IS the answer, so it keeps 1,500.
TEXT_MAX = 800
DOCUMENT_TEXT_MAX = 1500
ENTITIES_MAX = 300
# A highlight passage the compact text does not already contain (e.g. a match past the text cap): a window
# of this many code points around its first match.
HIGHLIGHT_SNIPPET_MAX = 300
_SNIPPET_LEAD = 100
SCORE_DIGITS = 3
ELLIPSIS = "…"

COMPACT_FIELDS = ("asset_id", "start_ms", "end_ms", "source_index", "final_score")  # always copied
OPTIONAL_FIELDS = ("title", "text", "named_entities", "highlights", "frame_s3_key")  # read when present
# Derived, model-only fields: `matched_terms` (the <em> terms of `highlights`), `page` (a document hit) and
# `derived_from` (the title of the original a job output was made from, see :func:`_lineage_first`).
DERIVED_FIELDS = ("matched_terms", "page", "derived_from")
# A job output's Display_Name is `<source> — <Job label> (<raw stem>)<ext>` (`ingestion.generated.qualify_name`),
# so the text before the first separator names the original it came from.
_QUALIFIER_SEP = " — "
_ALLOCATED_SUFFIX_RE = re.compile(r"\s\(\d+\)$")


SEARCH_IMAGE_ASSET_ID = "search_image_asset_id"


def with_harness_inputs(spec: dict) -> dict:
    """The gateway spec plus the Harness-only inputs: ``asset_kinds`` and ``search_image_asset_id``.

    ``search_image_asset_id`` is how a search runs by an image: the harness resolves the asset to its
    Search_Rendition (the form the index embedded, and the only image form M2C's search may read) and sends
    that as ``search_image_s3uri``; M2C never receives the asset id.
    """
    out = with_asset_kinds(spec)
    out["inputSchema"]["json"]["properties"][SEARCH_IMAGE_ASSET_ID] = {
        "type": "string",
        "description": (
            "Search by an image: the asset_id (image:<uuid>) of an image in the active project — an attached "
            "image's `asset_id:` line, or one found with project_status. Use INSTEAD of search_term and "
            "search_image_s3uri. One image per call: for several images, call once per image. Only images can "
            "be a query; search by video or audio is not supported at the moment."
        ),
    }
    return out


def with_asset_kinds(spec: dict) -> dict:
    """Copy of the gateway tool spec with one optional property added to inputSchema.json.properties.

    `required` is left unchanged. Idempotent: the property is only added once.
    """
    out = copy.deepcopy(spec)
    schema = out.setdefault("inputSchema", {}).setdefault("json", {})
    properties = schema.setdefault("properties", {})
    properties["asset_kinds"] = {
        "type": "array",
        "items": {"type": "string", "enum": list(ASSET_KINDS)},
        "minItems": 1,
        "uniqueItems": True,
        "description": (
            "Optional. Keep only hits of these asset kinds (applied by the harness after the search; "
            "may leave fewer than k hits)."
        ),
    }
    return out


def truncate(s: str, limit: int = TEXT_MAX) -> str:
    """`s` when it is at most `limit` code points, else the first `limit` code points plus '…'."""
    return s if len(s) <= limit else s[:limit] + ELLIPSIS


def serialize_entities(v: Any) -> str:
    """`v` when it is already a str, else its JSON form (non-ASCII preserved)."""
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def compact_score(v: Any) -> Any:
    """A float `final_score` rounded to :data:`SCORE_DIGITS` places (it only orders hits); anything else as is."""
    return round(v, SCORE_DIGITS) if isinstance(v, float) else v


_EM_OPEN = re.compile(r"<em>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>")


def _plain(s: str) -> str:
    return html.unescape(_TAG.sub("", s))


def _str_leaves(node: Any, depth: int = 1) -> list[str]:
    if isinstance(node, str):
        return [node]
    if depth > 3:
        return []
    if isinstance(node, list):
        return [leaf for item in node for leaf in _str_leaves(item, depth + 1)]
    if isinstance(node, dict):
        return [leaf for value in node.values() for leaf in _str_leaves(value, depth + 1)]
    return []


def _snippet(leaf: str) -> str:
    """The tag-free passage of a highlight leaf around its first match, at most HIGHLIGHT_SNIPPET_MAX long."""
    plain = _plain(leaf)
    if len(plain) <= HIGHLIGHT_SNIPPET_MAX:
        return plain
    m = _EM_OPEN.search(leaf)
    at = len(_plain(leaf[: m.start()])) if m else 0
    start = max(0, min(at - _SNIPPET_LEAD, len(plain) - HIGHLIGHT_SNIPPET_MAX))
    out = plain[start:start + HIGHLIGHT_SNIPPET_MAX]
    return (ELLIPSIS if start > 0 else "") + out + (ELLIPSIS if start + HIGHLIGHT_SNIPPET_MAX < len(plain) else "")


def compact_highlights(highlights: Any, shown: list[str]) -> tuple[list[str], list[str]]:
    """(matched_terms, extra passages) of a hit's `highlights` for the model.

    M2C's `highlights` repeat the hit's `text` and `named_entities` verbatim with `<em>` around the matched
    words -- about 40% of a result's model text (staging, 2026-10-16). The model gets the matched words once,
    as `matched_terms`, plus only those highlight passages that ``shown`` (the compact text and entities it
    already receives) does not contain, as a window around the match. The ledger keeps `highlights` whole
    (the Moments tab reads them there).
    """
    from render.moments import highlight_terms  # lazy: render.moments imports this module

    terms = highlight_terms(highlights)
    extra: list[str] = []
    for leaf in _str_leaves(highlights):
        plain = _plain(leaf).strip()
        if not plain or any(plain in s for s in shown):
            continue
        snippet = _snippet(leaf)
        if snippet not in extra:
            extra.append(snippet)
    return terms, extra


def compact_hit(hit: dict) -> dict:
    """The model's view of one Hit: COMPACT_FIELDS (missing → absent) plus the present, non-None
    OPTIONAL_FIELDS, reduced.

    `final_score` is rounded; `text` is truncated (:data:`TEXT_MAX`, or :data:`DOCUMENT_TEXT_MAX` for a
    document hit); `named_entities` is serialized then truncated (:data:`ENTITIES_MAX`); `highlights`
    becomes `matched_terms` plus any passage not already shown (:func:`compact_highlights`); `title` and
    `frame_s3_key` are copied unchanged (a citation names the `title`; a frame query is built from the key).
    A document hit gains its `page`. No other key (doc_id, score, rrf_score, match_sources, *_parent_id,
    genres) survives. The ledger keeps the full Hit: citations and Moments never read this view.
    """
    out: dict = {}
    for key in COMPACT_FIELDS:
        if key in hit:
            out[key] = compact_score(hit[key]) if key == "final_score" else hit[key]
    document = _is_document_hit(hit)
    for key in OPTIONAL_FIELDS:
        value = hit.get(key)
        if value is None or key == "highlights":
            continue
        if key == "text":
            out[key] = truncate(str(value), DOCUMENT_TEXT_MAX if document else TEXT_MAX)
        elif key == "named_entities":
            out[key] = truncate(serialize_entities(value), ENTITIES_MAX)
        else:
            out[key] = value
    if hit.get("highlights") is not None:
        shown = [s for s in (out.get("text"), out.get("named_entities")) if isinstance(s, str)]
        terms, extra = compact_highlights(hit["highlights"], shown)
        if terms:
            out["matched_terms"] = terms
        if extra:
            out["highlights"] = extra
    if document:
        # A document hit's span is a page locator, not a time: give the model the page it can cite.
        out["page"] = page_of_span(hit["start_ms"])
    return out


def _is_document_hit(hit: dict) -> bool:
    start = hit.get("start_ms")
    return (isinstance(hit.get("asset_id"), str) and hit["asset_id"].startswith("document:")
            and isinstance(start, int) and not isinstance(start, bool) and start >= 0)


def _kind_ok(hit: dict, prefixes: tuple[str, ...]) -> bool:
    return isinstance(hit.get("asset_id"), str) and hit["asset_id"].startswith(prefixes)


def kind_filter(hits: list[dict], kinds: Optional[list[str]]) -> tuple[list[dict], int]:
    """(kept, removed). `kinds` None → (hits, 0). Otherwise keep, in order, hits whose `asset_id` is a
    str starting with f"{kind}:" for some kind in `kinds`; removed = len(hits) - len(kept)."""
    if kinds is None:
        return hits, 0
    prefixes = tuple(f"{kind}:" for kind in kinds)
    kept = [h for h in hits if _kind_ok(h, prefixes)]
    return kept, len(hits) - len(kept)


@dataclass(frozen=True)
class ShapedResult:
    delivered: list[dict]  # full, untruncated Hits delivered to the model, in result order
    compact: list[dict]  # [compact_hit(h) for h in delivered]
    removed: Optional[int]  # None when the call had no asset_kinds
    kinds: Optional[list[str]]
    skipped_indices: Optional[list]  # present only when the M2C result carried it
    # Kept Hits ranked after `delivered` (up to TAB_FETCH_K): the Moments tab's extra cards. Never shown to
    # the model, so never a citation target.
    overflow: list[dict] = field(default_factory=list)

    def model_text(self) -> str:
        payload: dict = {"results": self.compact}
        if self.skipped_indices is not None:
            payload["skipped_indices"] = self.skipped_indices
        if self.kinds is not None:
            payload["asset_kinds"] = {"kinds": self.kinds, "removed": self.removed}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def shape_result(result: Any, *, k: Optional[int], asset_kinds: Optional[list[str]]) -> ShapedResult:
    """Filter by asset kind, cut to the first `k`, and compact what the model sees.

    The ledger holds `delivered` (full Hits); the model gets `model_text()` built from `compact`. `removed`
    counts the hits the filter dropped while filling those `k` — with no more raw hits than `k` (the shape
    before TAB_FETCH_K) that is every dropped hit, as before. `overflow` is the kept tail after `delivered`.
    """
    hits = hits_from_result(result)
    limit = k or DEFAULT_K
    delivered: list[dict] = []
    overflow: list[dict] = []
    removed = 0
    prefixes = tuple(f"{kind}:" for kind in asset_kinds) if asset_kinds is not None else None
    for hit in hits:
        ok = prefixes is None or (isinstance(hit, dict) and _kind_ok(hit, prefixes))
        if len(delivered) < limit:
            if ok:
                delivered.append(hit)
            else:
                removed += 1
        elif ok and len(delivered) + len(overflow) < TAB_FETCH_K:
            overflow.append(hit)
    delivered, overflow, sources = _lineage_first(delivered, overflow)
    compact = [compact_hit(h) for h in delivered]
    for hit, view in zip(delivered, compact):
        source = sources.get(_source_stem(hit))
        if source:
            view["derived_from"] = source
    skipped_indices = _skipped_indices(result)
    return ShapedResult(
        delivered=delivered,
        compact=compact,
        removed=removed if asset_kinds is not None else None,
        kinds=asset_kinds,
        skipped_indices=skipped_indices,
        overflow=overflow,
    )


def _title_stem(hit: Any) -> Optional[str]:
    """`hit`'s title without extension or allocator ` (n)`, lower-cased; None without a title."""
    title = hit.get("title") if isinstance(hit, dict) else None
    if not isinstance(title, str) or not title.strip():
        return None
    stem = _ALLOCATED_SUFFIX_RE.sub("", title.strip())
    dot = stem.rfind(".")
    if dot > 0:
        stem = stem[:dot]
    return _ALLOCATED_SUFFIX_RE.sub("", stem).strip().lower() or None


def _source_stem(hit: Any) -> Optional[str]:
    """The stem of the original a job output names in its title, or None for a hit that is not one."""
    title = hit.get("title") if isinstance(hit, dict) else None
    if not isinstance(title, str) or _QUALIFIER_SEP not in title:
        return None
    return title.split(_QUALIFIER_SEP, 1)[0].strip().lower() or None


def _lineage_first(delivered: list[dict], overflow: list[dict]) -> tuple[list[dict], list[dict], dict[str, str]]:
    """Put each original ahead of the job outputs made from it; returns (delivered, overflow, sources).

    Trims, segments and clips of one video repeat its moments with near-equal scores, so they can fill
    every delivered slot and push the original into `overflow`, where the model never sees it and cannot
    cite it (golden case 2026-10-17: the Meridian title card's source ranked 10th or absent while 9 copies
    of it filled the list). For each original whose outputs were delivered, its best-ranked hit moves to
    just before the first of them -- from `overflow` if need be, the last delivered hit taking its place
    there -- so the delivered count and the set of hits are unchanged. `sources` maps an original's stem
    to its title, for the `derived_from` field. Only titles say what is derived (no lookup, no latency).
    """
    pool = delivered + overflow
    originals: dict[str, dict] = {}
    for hit in pool:
        stem = _title_stem(hit)
        if stem and _source_stem(hit) is None and stem not in originals:
            originals[stem] = hit
    sources: dict[str, str] = {}
    out = list(delivered)
    tail = list(overflow)
    for hit in list(out):
        stem = _source_stem(hit)
        original = originals.get(stem) if stem else None
        if original is None or stem in sources or not any(h is hit for h in out):
            continue
        sources[stem] = original["title"]
        at = next(i for i, h in enumerate(out) if h is hit)
        if any(h is original for h in out[:at]):
            continue
        if any(h is original for h in out):
            out.remove(original)
        else:
            tail.remove(original)
            tail.insert(0, out.pop())
            if at == len(out):  # the output itself was last: the original takes its slot
                out.append(original)
                continue
        out.insert(at, original)
    return out, tail, sources


def _skipped_indices(result: Any) -> Optional[list]:
    """The M2C result's `skipped_indices` when present as a list, else None."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return None
    if isinstance(result, dict):
        value = result.get("skipped_indices")
        if isinstance(value, list):
            return value
    return None
