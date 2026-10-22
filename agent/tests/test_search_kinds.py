"""Documents in project search: one kind list, the page locator, and page citations.

Staging 2026-10-11: PDFs were indexed (page text, `ingestion/documents.py`) but no document hit ever reached
a user. `document` was absent from the agent's search kinds (resolution dropped the hit), the proxy's Moments
search kinds, and the SPA's chips and card filter — five copies, all out of date the same way. These tests
pin every copy to `infra/lib/projects/fixtures/search-kinds.json` (the TS twins assert the same file), and
cover what a document hit needs to be usable: a page the model can cite, and a citation that verifies only
against a document on that page.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ingestion import documents  # noqa: E402
from projects import schema  # noqa: E402
from project_search import assets, citations, compact  # noqa: E402
from project_search import document_recall as dr  # noqa: E402
from project_search.assets import ResolvedAsset  # noqa: E402
from project_search.citations import format_page_citation, page_citation_key, parse_citations  # noqa: E402
from project_search.session_hits import SessionHit  # noqa: E402
from project_search.verify import classify_citations  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[2] / "infra/lib/projects/fixtures/search-kinds.json"
PID = "p" + "a" * 32
DOC = "document:11111111-1111-4111-8111-111111111111"
VID = "video:22222222-2222-4222-8222-222222222222"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_every_agent_copy_of_the_search_kinds_matches_the_fixture():
    kinds = tuple(_fixture()["kinds"])
    assert compact.ASSET_KINDS == kinds
    assert assets._ASSET_KINDS == kinds  # turn-end resolution: a kind missing here drops the hit silently
    # Every indexed kind is searchable: the registry's kinds minus `data` (never indexed).
    assert kinds == tuple(k for k in schema.ASSET_KINDS if k != "data")
    enum = compact.with_asset_kinds({})["inputSchema"]["json"]["properties"]["asset_kinds"]["items"]["enum"]
    assert tuple(enum) == kinds


def test_the_page_stride_is_one_constant():
    # The search layer imports the ingestion constant (no copies); the SPA pins the same fixture value.
    assert documents.PAGE_STRIDE_MS == citations.PAGE_STRIDE_MS == _fixture()["pageStrideMs"]


def test_a_document_hit_carries_its_page_and_a_media_hit_does_not():
    page3 = {"asset_id": DOC, "start_ms": 2 * 10_000 + 4, "end_ms": 2 * 10_000 + 5, "final_score": 1.0}
    assert compact.compact_hit(page3)["page"] == 3
    assert "page" not in compact.compact_hit({"asset_id": VID, "start_ms": 20_004, "end_ms": 30_000,
                                              "final_score": 1.0})


def test_page_citations_parse_and_keep_their_own_key():
    [c] = parse_citations("It says so [report.pdf p.3].")
    assert (c.display_name, c.page, c.start_ms, c.end_ms) == ("report.pdf", 3, 20_000, 20_000)
    assert c.key == page_citation_key("report.pdf", 3) == "report.pdf|p3"
    # The same span written as a timecode is a DIFFERENT citation (it cannot dedupe into the page one).
    [t] = parse_citations("[report.pdf @ 00:20–00:20]")
    assert t.page is None and t.key != c.key
    assert format_page_citation("report.pdf", 3) == "[report.pdf p.3]"


def _verify(text: str, asset_id: str, kind: str, start_ms: int, end_ms: int):
    hit = {"asset_id": asset_id, "start_ms": start_ms, "end_ms": end_ms, "final_score": 1.0,
           "source_index": "scene", "text": "x"}
    sh = SessionHit(hit=hit, project_id=PID, turn_id="t1", tool_use_id="u1", index=0, recency=(1, 0, 0))
    resolved = {asset_id: ResolvedAsset(asset_id=asset_id, kind=kind, display_name="report.pdf", status="indexed")}
    return classify_citations(parse_citations(text), project_id=PID, session_hits=[sh], assets=resolved)


def test_a_page_citation_verifies_against_a_document_hit_on_that_page_only():
    on_page = (2 * 10_000 + 7, 2 * 10_000 + 8)  # page 3, chunk 7
    [ok] = _verify("[report.pdf p.3]", DOC, "document", *on_page)
    assert ok.verified and ok.kind == "document" and ok.page == 3
    [wrong_page] = _verify("[report.pdf p.4]", DOC, "document", *on_page)
    assert not wrong_page.verified


def test_citation_form_must_match_the_asset_kind():
    # A timecode citation never verifies against a document (its span is a page locator, not a time) ...
    [t] = _verify("[report.pdf @ 00:20–00:20]", DOC, "document", 20_000, 20_001)
    assert not t.verified
    # ... and a page citation never verifies against media.
    [p] = _verify("[report.pdf p.3]", VID, "video", 20_000, 30_000)
    assert not p.verified


def test_the_page_travels_on_the_wire():
    from project_search.turn_end import _record_wire

    [ok] = _verify("[report.pdf p.3]", DOC, "document", 20_000, 20_001)
    wire = _record_wire(ok)
    assert wire["page"] == 3 and wire["kind"] == "document" and wire["key"] == "report.pdf|p3"


# ---------------------------------------------------------------------------- document recall (document_recall.py)
# Staging session 853bdc11: indexed reports never surfaced for a plain query (video shots outrank PDF pages).
# The policy cases live in a fixture shared with the TS twin (ui/server/projects/documentRecall.ts).
RECALL = Path(__file__).resolve().parents[2] / "infra/lib/projects/fixtures/document-recall-cases.json"


def _h(aid, start, score, **kw):
    return {"asset_id": aid, "start_ms": start, "end_ms": start + 1, "final_score": score, **kw}


def _recall() -> dict:
    return json.loads(RECALL.read_text())


def test_recall_policy_matches_the_shared_fixture():
    from project_search import document_recall as dr

    fx = _recall()
    assert dr.SCOPED_QUERY_PATTERN == fx["scopedQueryPattern"]
    assert documents.DOCUMENT_SCOPE == fx["documentScope"]
    for case in fx["companionCases"]:
        assert dr.companion_term(case["args"], case["kinds"]) == case["expect"], case
    for case in fx["mergeCases"]:
        got = [[h["asset_id"], h["start_ms"]] for h in dr.merge_hits(case["primary"], case["companion"])]
        assert got == case["expect"], case["note"]


def test_documents_are_built_under_the_scope_recall_asks():
    built = documents.build_document(DOC, [{"page": 2, "chunk_index": 0, "heading": "H", "text": "t"}])
    assert {d["doc_type"] for d in built.documents} == {documents.DOCUMENT_SCOPE}


def test_merge_keeps_the_raw_result_shape():
    from project_search import document_recall as dr

    primary = json.dumps({"results": [_h("video:a", 0, 1.0)], "skipped_indices": ["x"]})
    merged = dr.merge(primary, {"results": [_h(DOC, 0, 0.5)]})
    assert merged["skipped_indices"] == ["x"] and [h["asset_id"] for h in merged["results"]] == ["video:a", DOC]
    assert dr.merge(primary, {"results": [_h("video:z", 0, 1.0)]}) == primary  # nothing new: untouched
    assert dr.merge(primary, "not json") == primary


class _ByTermClient:
    tool_name = "target___search_content"

    def __init__(self, by_term):
        self.by_term, self.calls = by_term, []

    async def call(self, token, args):
        from project_search.errors import SearchOutcome
        self.calls.append(dict(args))
        r = self.by_term.get(args["search_term"])
        if isinstance(r, BaseException):
            raise r
        return SearchOutcome("call", "ok", result=json.dumps(r)) if r is not None else SearchOutcome("call", "timeout")


def _search(client, args):
    import asyncio
    from project_search.tool import ProjectSearchTool
    from project_search.token_ref import ProjectTokenRef
    from test_compact_hits import _SPEC, _tok  # noqa: PLC0415 -- the shared spec/token fixtures

    tool = ProjectSearchTool(ProjectTokenRef(_tok()), _SPEC, project_id=PID, turn_id="t-doc", client=client)

    async def go():
        return [ev async for ev in tool.stream({"toolUseId": "u1", "name": _SPEC["name"], "input": args}, {})]
    return json.loads(asyncio.run(go())[-1].tool_result["content"][0]["text"])


def _videos(n: int, top: float = 1.0) -> list:
    return [_h(f"video:{i}", 0, round(top - i * 0.01, 2)) for i in range(n)]


def test_a_plain_search_returns_the_documents_its_scene_companion_finds():
    # Session 853bdc11's shape, in a project this process has not seen a document in yet: the plain query ranks
    # a report page under the video hits, past the model's k (10), so it shows documents exist but none reaches
    # the model; the deferred companion then finds the pages the ranking buried.
    from project_search import document_recall
    document_recall.reset_documents_seen()
    plain = _videos(2) + _videos(12, 0.5)[2:] + [_h(DOC, 20_004, 0.1)]
    client = _ByTermClient({
        "suggested rating": {"results": plain},
        "suggested rating #scene": {"results": [_h(DOC, 4, 1.0), _h("video:c", 0, 0.7)]},
    })
    out = _search(client, {"collection_id": PID, "search_term": "suggested rating"})
    assert [c["search_term"] for c in client.calls] == ["suggested rating", "suggested rating #scene"]
    assert [(h["asset_id"], h.get("page")) for h in out["results"]][:3] == [
        ("video:0", None), (DOC, 1), ("video:1", None)]
    # ...and the project is now known to hold documents: its next plain query runs the companion in parallel.
    assert dr.companion_plan({"search_term": "a car"}, None, PID) == dr.PARALLEL
    document_recall.reset_documents_seen()


def test_a_document_query_runs_its_companion_in_parallel_and_a_visual_one_runs_alone():
    from project_search import document_recall
    document_recall.reset_documents_seen()
    # Names a document: the companion runs alongside the plain query, as before 2026-10-16.
    client = _ByTermClient({
        "suggested rating in the compliance report": {"results": [_h("video:a", 0, 1.0)]},
        "suggested rating in the compliance report #scene": {"results": [_h(DOC, 20_004, 1.0)]},
    })
    out = _search(client, {"collection_id": PID, "search_term": "suggested rating in the compliance report"})
    assert len(client.calls) == 2
    assert [h["asset_id"] for h in out["results"]] == ["video:a", DOC]
    # asset_kinds naming documents: parallel too.
    assert dr.companion_plan({"search_term": "x"}, ["document", "video"]) == dr.PARALLEL
    assert dr.companion_plan({"search_term": "page 4 of the deck"}, None) == dr.PARALLEL
    # A visual query whose results hold no document: ONE M2C run, no companion.
    document_recall.reset_documents_seen()
    client = _ByTermClient({"Meridian title card": {"results": _videos(12)}})
    _search(client, {"collection_id": PID, "search_term": "Meridian title card"})
    assert [c["search_term"] for c in client.calls] == ["Meridian title card"]
    # "title page" and "white letters" are video words, not document intent.
    assert dr.companion_plan({"search_term": "Meridian title page"}, None) == dr.DEFERRED
    assert dr.companion_plan({"search_term": "large white letters"}, None) == dr.DEFERRED


def test_documents_inside_the_models_window_need_no_companion():
    from project_search import document_recall
    document_recall.reset_documents_seen()
    client = _ByTermClient({"budget": {"results": [_h(DOC, 20_004, 1.0)] + _videos(5)}})
    out = _search(client, {"collection_id": PID, "search_term": "budget"})
    assert len(client.calls) == 1 and out["results"][0]["asset_id"] == DOC


def test_a_project_with_documents_keeps_the_parallel_companion():
    # Recall is unchanged wherever documents exist: once a result of this project carried a document, every
    # plain query runs its companion alongside, as before -- including one whose plain result is empty.
    from project_search import document_recall
    document_recall.reset_documents_seen()
    by_term = {"the rating": {"results": []}, "the rating #scene": {"results": [_h(DOC, 20_004, 1.0)]}}
    client = _ByTermClient(by_term)
    assert _search(client, {"collection_id": PID, "search_term": "the rating"})["results"] == []
    assert len(client.calls) == 1  # no document ever seen in this project: no companion
    document_recall.note_documents(PID, {"results": [_h(DOC, 0, 1.0)]})
    client = _ByTermClient(by_term)
    out = _search(client, {"collection_id": PID, "search_term": "the rating"})
    assert len(client.calls) == 2 and [h["asset_id"] for h in out["results"]] == [DOC]
    # The memory is per project and expires.
    assert dr.companion_plan({"search_term": "the rating"}, None, "p" + "b" * 32) == dr.DEFERRED
    document_recall._documents_seen[PID] -= document_recall.DOCUMENTS_SEEN_TTL_S + 1
    assert dr.companion_plan({"search_term": "the rating"}, None, PID) == dr.DEFERRED
    document_recall.reset_documents_seen()


def test_a_failed_companion_leaves_the_plain_results():
    from project_search import document_recall
    document_recall.reset_documents_seen()
    plain = _videos(10) + [_h(DOC, 20_004, 0.1)]
    for companion in (None, RuntimeError("boom")):  # times out / raises
        client = _ByTermClient({"suggested rating": {"results": plain}, "suggested rating #scene": companion})
        out = _search(client, {"collection_id": PID, "search_term": "suggested rating"})
        assert len(client.calls) == 2
        assert [h["asset_id"] for h in out["results"]] == [f"video:{i}" for i in range(10)]
    document_recall.reset_documents_seen()


# Session 993b85b5: "[tidal_…_compliance_report.pdf p.1–2]" -- a page RANGE -- was plain text, so the report the
# answer was about had no link and no Moments card.
def test_a_page_range_citation_verifies_on_a_hit_on_any_of_its_pages():
    on_page_2 = (1 * 10_000 + 3, 1 * 10_000 + 4)
    [ok] = _verify("[report.pdf p.1–2]", DOC, "document", *on_page_2)
    assert ok.verified and (ok.page, ok.page_end) == (1, 2) and ok.key == "report.pdf|p1-2"
    [miss] = _verify("[report.pdf pp. 3-4]", DOC, "document", *on_page_2)
    assert not miss.verified


def test_a_page_range_travels_on_the_wire_and_round_trips():
    from project_search.turn_end import _record_wire

    [ok] = _verify("[report.pdf pp. 1-2]", DOC, "document", 10_003, 10_004)
    wire = _record_wire(ok)
    assert (wire["page"], wire["page_end"]) == (1, 2)
    assert format_page_citation("report.pdf", 1, 2) == "[report.pdf p.1–2]"
    [back] = parse_citations(format_page_citation("report.pdf", 1, 2))
    assert (back.page, back.page_end) == (1, 2)
