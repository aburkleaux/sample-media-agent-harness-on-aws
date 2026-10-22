"""Document recall: the search-policy layer that lets a plain query find document (PDF) pages.

Why it exists (staging 2026-10-12, session 853bdc11). A project's compliance-report PDFs were indexed, yet
no plain query returned one, not even an exact sentence from a report. M2C's default query is dominated by
video SHOT frames (`ann:vector` over images); a document page is indexed only as text under
:data:`projects.document_locator.DOCUMENT_SCOPE`, so it ranked 38th and 46th for "suggested rating". Restricted
to that scope, the same query ranks the reports 1-4.

The policy, in one place (its TypeScript twin is ``ui/server/projects/documentRecall.ts``; both are pinned
by ``infra/lib/projects/fixtures/document-recall-cases.json``):

* :func:`companion_term` -- a PLAIN text query that could want documents also runs as
  ``<term> #<DOCUMENT_SCOPE>``. A query that already names a scope or a field operator is the user's
  choice and is left alone, as is an image query or one whose ``asset_kinds`` exclude documents.
* :func:`merge` -- only the companion's DOCUMENT hits are taken (never its video scenes), minus any the
  plain result already holds, merged in by ``final_score`` with the plain result first on ties. M2C
  normalizes ``final_score`` against each call's best hit -- the companion's video scenes included -- so a
  weak page stays low rather than being promoted.

Why not default every query to the document scope: that returns ONLY scene hits, dropping the shot frames
visual queries rank on (and the Moments cards and Find similar need), utterances and topics.

WHEN the companion runs (:func:`companion_plan`, 2026-10-16). Every eligible query used to run it in parallel:
three M2C workflow runs (~6-7 s each) for a turn whose model searched twice, though most queries are visual
and most projects hold no document. Now:

* ``"parallel"`` (as before) when the query plausibly targets documents -- ``asset_kinds`` names
  ``document``, or the term names a document word (:data:`DOCUMENT_INTENT_PATTERN`: pdf, report, page 3, …)
  -- or the project is known to hold documents (:func:`note_documents`: any earlier result of this process
  carried a document hit), which keeps recall exactly as it was wherever documents exist;
* ``"deferred"`` otherwise (a project with no document seen yet): the plain query runs alone, and the
  companion follows only when :func:`deferred_needed` sees the project does hold documents after all -- a
  document hit in the plain result (it fetches 50) but none inside the model's ``k``. That first call
  also teaches the process, so the project's next queries run in parallel.

Every query of a project without documents saves a whole M2C run; :func:`companion_term` and :func:`merge` are
unchanged and still pinned by the fixture.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Optional

from projects.document_locator import DOCUMENT_SCOPE

from .ledger import hits_from_result

# A query that already restricts scope (`#shot`, `#scene`, `#utterance`, `#topic`) or uses a field
# operator (`says:"…"`, `shows:"…"`). Byte-identical to the fixture's `scopedQueryPattern`.
SCOPED_QUERY_PATTERN = r"#(?:shot|scene|utterance|topic)\b|\b(?:says|shows):"
_SCOPED_QUERY = re.compile(SCOPED_QUERY_PATTERN, re.IGNORECASE)

DOCUMENT_ID_PREFIX = "document:"


def companion_term(args: dict, asset_kinds: Optional[list[str]]) -> Optional[str]:
    """The document-scope companion query for ``args``, or None when the call should run alone."""
    term = args.get("search_term")
    if not isinstance(term, str) or not term.strip() or args.get("search_image_s3uri"):
        return None
    if asset_kinds is not None and "document" not in asset_kinds:
        return None
    if _SCOPED_QUERY.search(term):
        return None
    return f"{term} #{DOCUMENT_SCOPE}"


# A term that plausibly asks about a document (PDF): its kind, a document genre, or a page locator. Kept
# narrow on purpose: "title page", "letters" and "script" (a typeface) are video words too, and a miss only
# defers the companion, never drops it (deferred_needed still runs it when a document ranks low).
DOCUMENT_INTENT_PATTERN = (
    r"\b(?:pdfs?|documents?|docs?|reports?|slides?|slide ?decks?|decks?|papers?|whitepapers?|contracts?|"
    r"memos?|manuals?|handbooks?|brochures?|spreadsheets?|invoices?|policy|policies|guidelines?|"
    r"compliance|screenplays?|call ?sheets?|pages?\s*\d+|pp?\.\s*\d+)\b"
)
_DOCUMENT_INTENT = re.compile(DOCUMENT_INTENT_PATTERN, re.IGNORECASE)

PARALLEL = "parallel"
DEFERRED = "deferred"

# Projects whose search results have held a document hit, with when (monotonic). Process-local and
# best-effort: an unknown project only means an empty plain result does not trigger the companion.
DOCUMENTS_SEEN_TTL_S = 3600.0
_documents_seen: dict[str, float] = {}
_documents_lock = threading.Lock()
_DOCUMENTS_SEEN_MAX = 1024


def companion_plan(args: dict, asset_kinds: Optional[list[str]], project_id: str = "") -> Optional[str]:
    """How the document companion of ``args`` runs: :data:`PARALLEL`, :data:`DEFERRED`, or None (never)."""
    term = companion_term(args, asset_kinds)
    if term is None:
        return None
    if asset_kinds is not None and "document" in asset_kinds:
        return PARALLEL
    if _DOCUMENT_INTENT.search(args.get("search_term") or ""):
        return PARALLEL
    if project_id and project_has_documents(project_id):
        return PARALLEL
    return DEFERRED


def _is_document(h: Any) -> bool:
    return isinstance(h, dict) and isinstance(h.get("asset_id"), str) and h["asset_id"].startswith(DOCUMENT_ID_PREFIX)


def note_documents(project_id: str, *results: Any) -> None:
    """Remember that ``project_id`` holds documents when any of ``results`` carries a document hit."""
    if not project_id or not any(_is_document(h) for r in results for h in hits_from_result(r)):
        return
    with _documents_lock:
        if project_id not in _documents_seen and len(_documents_seen) >= _DOCUMENTS_SEEN_MAX:
            _documents_seen.pop(next(iter(_documents_seen)))
        _documents_seen[project_id] = time.monotonic()


def project_has_documents(project_id: str) -> bool:
    with _documents_lock:
        seen = _documents_seen.get(project_id)
    return seen is not None and time.monotonic() - seen < DOCUMENTS_SEEN_TTL_S


def reset_documents_seen() -> None:
    with _documents_lock:
        _documents_seen.clear()


def deferred_needed(primary_result: Any, k: int, asset_kinds: Optional[list[str]] = None) -> bool:
    """Whether a DEFERRED companion must run after the plain result ``primary_result``: the result shows the
    project holds documents (a document hit anywhere in it), yet none ranks inside the model's first ``k``
    of the hits the kind filter keeps. The companion then finds the pages the plain ranking buried."""
    hits = hits_from_result(primary_result)
    if asset_kinds is not None:
        prefixes = tuple(f"{kind}:" for kind in asset_kinds)
        hits = [h for h in hits if isinstance(h, dict) and isinstance(h.get("asset_id"), str)
                and h["asset_id"].startswith(prefixes)]
    return any(_is_document(h) for h in hits[k:]) and not any(_is_document(h) for h in hits[:k])


def _key(h: dict) -> tuple:
    return (h.get("asset_id"), h.get("start_ms"), h.get("end_ms"))


def _score(h: dict) -> float:
    v = h.get("final_score")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def merge_hits(primary: list, companion: list) -> list:
    """``primary`` with ``companion``'s new document hits merged in by score (primary first on ties)."""
    have = {_key(h) for h in primary if isinstance(h, dict)}
    docs = [h for h in companion
            if isinstance(h, dict) and isinstance(h.get("asset_id"), str)
            and h["asset_id"].startswith(DOCUMENT_ID_PREFIX) and _key(h) not in have]
    if not docs:
        return list(primary)
    out: list = []
    i = j = 0
    while i < len(primary) or j < len(docs):
        if j >= len(docs) or (i < len(primary) and _score(primary[i]) >= _score(docs[j])):
            out.append(primary[i])
            i += 1
        else:
            out.append(docs[j])
            j += 1
    return out


def merge(primary_result: Any, companion_result: Any) -> Any:
    """A raw M2C result (``{"results": [...]}``, as a dict or JSON text) with the companion's documents in.

    Every other key of the primary result (``skipped_indices``, …) is kept; an unreadable primary, or a
    companion with no new document hit, returns the primary unchanged.
    """
    base = primary_result
    if isinstance(base, str):
        try:
            base = json.loads(base)
        except ValueError:
            return primary_result
    if not isinstance(base, dict):
        return primary_result
    hits = hits_from_result(base)
    merged = merge_hits(hits, hits_from_result(companion_result))
    return primary_result if merged == hits else {**base, "results": merged}
