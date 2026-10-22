"""Turn-end orchestration tests for media-harness-search-qa-rendering (design Component 4 / 6).

This file hosts two bodies of tests that are kept in clearly separated sections:

- **Property 10** (below): the end-to-end invariant that *only* delivered Hits can verify a Citation
  or become a Moment. Driven through the real pipeline ``ProjectSearchTool._run`` →
  ``turn_end.finish_turn`` → ``render.moments.select_moments``, with a fake search client and the real
  in-memory Search_Ledger (the fake-client / token / ledger pattern of ``test_compact_hits.py``
  Property 2).
- Turn-end orchestration **unit tests** (Task 6.7) are appended in their own section at the bottom.

Keeping the two apart lets Task 6.7 add unit tests without touching the Property 10 section.
"""
from __future__ import annotations

# ===========================================================================
# Property 10: Only delivered Hits verify Citations or become Moments
# (media-harness-search-qa-rendering design Component 4/6, Correctness Property 10).
#
# For any search_content result, any valid k and any valid asset_kinds, let
# `delivered` be the Hits the Search_Tool actually hands the model and records in
# the ledger — i.e. `[h for h in results if kinds is None or asset_id startswith
# "<kind>:"][: k or 10]`. A Hit dropped by the Asset_Kind_Filter or cut by `k`
# never entered the ledger. Build an answer that cites every result Hit (delivered
# AND dropped) plus a few assets that were never returned, resolve every asset's
# Display_Name so resolution is never the gate, then run finish_turn and
# select_moments. The invariant: every Verified_Citation and every Moment maps to
# a *delivered* Hit — never a dropped, filtered, or never-returned one. Because
# every delivered Hit here is cited, resolvable and not removed, the verified set
# and the Moment set are exactly the delivered Hits (a crisp two-way check).
#
# Validates: Requirements 1.18, 2.5, 3.19
#
# The delivered set is recomputed from the requirement text by an in-test oracle,
# independent of compact.py / verify.py / moments.py.
# ===========================================================================

import asyncio
import base64
import json
import os
import sys
import time
import uuid

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from project_search import ledger, turn_end  # noqa: E402
from project_search.assets import ResolvedAsset  # noqa: E402
from project_search.citations import format_citation, format_page_citation  # noqa: E402
from projects.document_locator import page_of_span  # noqa: E402
from project_search.compact import ASSET_KINDS  # noqa: E402
from project_search.context import (  # noqa: E402
    ActiveProject,
    TurnContext,
    current_turn_context,
)
from project_search.errors import SearchOutcome  # noqa: E402
from project_search.session_hits import MemorySessionHitStore  # noqa: E402
from project_search.token_ref import ProjectTokenRef  # noqa: E402
from project_search.tool import ProjectSearchTool  # noqa: E402
from project_search.verify import whole_second_range  # noqa: E402
from render.moments import select_moments  # noqa: E402

_PID = "p" + "a" * 32
_SPEC = {
    "name": "tgt___hybrid-search___search_content",
    "description": "d",
    "inputSchema": {"json": {"type": "object"}},
}


def _tok() -> str:
    """A JWT-shaped Project_Token that ProjectTokenRef accepts and returns verbatim (iat now, 1 h life)."""
    iat = int(time.time())
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    header = seg({"alg": "RS256", "typ": "JWT"})
    body = seg({"sub": "u1", "project_id": _PID, "iat": iat, "exp": iat + 3600, "jti": "j1"})
    return f"{header}.{body}.sig"


class _FakeClient:
    """Records every (token, args) and returns a fixed SearchOutcome (test_compact_hits pattern)."""

    tool_name = _SPEC["name"]

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def call(self, token, args):
        self.calls.append((token, dict(args)))
        return self.outcome


class _FakeResolver:
    """An AssetResolver that resolves exactly the asset_ids it is told about (never raises)."""

    def __init__(self, by_id: dict[str, ResolvedAsset]):
        self.by_id = by_id

    def resolve(self, project_id: str, asset_ids):
        return {aid: self.by_id[aid] for aid in asset_ids if aid in self.by_id}


def _stream_result(tool, args, tuid):
    async def go():
        return [ev async for ev in tool.stream({"toolUseId": tuid, "name": _SPEC["name"], "input": args}, {})]

    return asyncio.run(go())[-1].tool_result


# ---------------------------------------------------------------------------
# Oracle: the delivered set re-stated from the requirement text.
# ---------------------------------------------------------------------------
def _oracle_kept(hits: list[dict], kinds) -> list[dict]:
    if kinds is None:
        return list(hits)
    prefixes = tuple(f"{x}:" for x in kinds)
    return [h for h in hits if isinstance(h.get("asset_id"), str) and h["asset_id"].startswith(prefixes)]


def _oracle_delivered(hits: list[dict], k, kinds) -> list[dict]:
    return _oracle_kept(hits, kinds)[: (k or 10)]


def _hit_key(hit: dict) -> tuple[str, int, int]:
    return (hit["asset_id"], hit["start_ms"], hit["end_ms"])


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
_SOURCE_INDEX = st.sampled_from(["shot", "scene", "utterance", "topic"])


@st.composite
def _scenario(draw):
    """A list of well-formed Hits (one unique asset each, disjoint whole-second spans), plus k / kinds.

    Each Hit i is its own asset ``<kind>:i`` with Display_Name ``Clip i`` and span
    ``[i*10+1 .. i*10+3]`` seconds, so a citation built from its whole-second range verifies against
    that Hit alone and against no other.
    """
    n = draw(st.integers(min_value=0, max_value=12))
    hits: list[dict] = []
    for i in range(n):
        kind = draw(st.sampled_from(ASSET_KINDS))
        source_index = draw(_SOURCE_INDEX)
        hit: dict = {
            "asset_id": f"{kind}:{i}",
            "start_ms": i * 10_000 + 1_000,
            "end_ms": i * 10_000 + 3_000,
            "final_score": draw(st.integers(min_value=0, max_value=100)),
            "source_index": source_index,
            "title": f"Clip {i}",
        }
        if source_index == "shot":
            hit["frame_s3_key"] = f"projects/{_PID}/frames/{i}.jpg"
        else:
            hit["text"] = draw(st.text(max_size=30))
        hits.append(hit)
    k = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=50)))
    kinds = draw(
        st.one_of(
            st.none(),
            st.lists(st.sampled_from(ASSET_KINDS), min_size=1, max_size=3, unique=True),
        )
    )
    return hits, k, kinds


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.data_too_large])
@given(_scenario())
# No hits: nothing verifies, no Moments tab.
@example(([], None, None))
# A kinds filter that drops every hit (all a different kind than requested).
@example(
    (
        [
            {"asset_id": "image:0", "start_ms": 1_000, "end_ms": 3_000, "final_score": 1,
             "source_index": "scene", "title": "Clip 0", "text": "a"},
            {"asset_id": "audio:1", "start_ms": 11_000, "end_ms": 13_000, "final_score": 2,
             "source_index": "utterance", "title": "Clip 1", "text": "b"},
        ],
        None,
        ["video"],
    )
)
# k cuts the delivered list below the number of returned hits.
@example(
    (
        [
            {"asset_id": "video:0", "start_ms": 1_000, "end_ms": 3_000, "final_score": 1,
             "source_index": "scene", "title": "Clip 0", "text": "x"},
            {"asset_id": "video:1", "start_ms": 11_000, "end_ms": 13_000, "final_score": 2,
             "source_index": "scene", "title": "Clip 1", "text": "y"},
            {"asset_id": "video:2", "start_ms": 21_000, "end_ms": 23_000, "final_score": 3,
             "source_index": "scene", "title": "Clip 2", "text": "z"},
        ],
        1,
        None,
    )
)
def test_property10_only_delivered_hits_verify_or_become_moments(scenario):
    hits, k, kinds = scenario

    delivered = _oracle_delivered(hits, k, kinds)
    delivered_keys = {_hit_key(h) for h in delivered}
    delivered_asset_ids = {h["asset_id"] for h in delivered}

    # Resolve EVERY returned asset (delivered or dropped) with a matching Display_Name and a
    # non-removed status, so resolution is never what gates verification — only ledger membership is.
    by_id = {
        h["asset_id"]: ResolvedAsset(
            asset_id=h["asset_id"],
            kind=h["asset_id"].split(":", 1)[0],
            display_name=h["title"],
            status="indexed",
        )
        for h in hits
    }

    # An answer that cites every returned Hit (delivered AND dropped) plus two never-returned assets.
    # A document is cited by the page its span encodes; media by timecode (verify refuses the cross).
    citation_texts = [
        format_page_citation(h["title"], page_of_span(h["start_ms"]))
        if h["asset_id"].startswith("document:") else format_citation(h["title"], *whole_second_range(h))
        for h in hits
    ]
    citation_texts.append(format_citation("Ghost One", 500_000, 502_000))
    citation_texts.append(format_citation("Ghost Two", 600_000, 601_000))
    answer = "The project shows " + " and then ".join(citation_texts) + "."

    # Build the M2C result exactly as the gateway returns it: a JSON string.
    result_json = json.dumps({"results": hits}, ensure_ascii=False)
    client = _FakeClient(SearchOutcome("call", "ok", result=result_json))

    turn_id = "p10-" + uuid.uuid4().hex
    tool = ProjectSearchTool(
        ProjectTokenRef(_tok()),
        _SPEC,
        project_id=_PID,
        turn_id=turn_id,
        client=client,
        dispatch_nonce="n1",
        frame_sink=[].append,
    )

    args: dict = {"collection_id": _PID, "search_term": "q"}
    if k is not None:
        args["k"] = k
    if kinds is not None:
        args["asset_kinds"] = list(kinds)

    tc = TurnContext(
        turn_id=turn_id,
        active_project=ActiveProject(id=_PID, name="", collection_id=_PID, personal=False),
        web_search=False,
        project_access="ok",
    )
    ctx_token = current_turn_context.set(tc)
    try:
        # 1. Run the search: the ledger now holds exactly the delivered Hits.
        result = _stream_result(tool, args, "tu-p10")
        assert result["status"] == "success"
        entries = ledger.results_for(turn_id)
        assert len(entries) == 1 and entries[0].hits == delivered  # sanity: delivered == ledger

        # 2. Feed the answer and run turn-end classification.
        turn_end.note_answer_text(turn_id, answer)
        outcome = turn_end.finish_turn(store=MemorySessionHitStore(), resolver=_FakeResolver(by_id))
        assert outcome is not None

        # 3. Every Verified_Citation maps to a delivered Hit (located by the chosen Hit's recency).
        verified = [r for r in outcome.citations if r.verified]
        by_recency = {sh.recency: sh for sh in outcome.session_hits}
        verified_keys: set[tuple[str, int, int]] = set()
        for record in verified:
            assert record.ref is not None
            assert record.ref.asset_id in delivered_asset_ids
            assert record.hit_recency in by_recency, "verified record points at a Hit not in the session"
            chosen = by_recency[record.hit_recency]
            key = _hit_key(chosen.hit)
            assert key in delivered_keys, "a Citation verified against a non-delivered Hit"
            verified_keys.add(key)

        # Two-way: every delivered Hit (all cited, resolvable, not removed) verifies, and nothing else.
        assert verified_keys == delivered_keys

        # 4. Moments (as amended 2026-10-10): every Hit the search returned that passed the kind filter
        #    (delivered to the model, or ranked past its k — up to 50) is a Moment, and nothing else; only
        #    a delivered Hit is ever a CITED card.
        returned = _oracle_kept(hits, kinds)[:50]
        tab_keys = {_hit_key(h) for h in returned}
        selection = select_moments(outcome)
        if selection is not None:
            assert {m.key for m in selection.items} == tab_keys
            for moment in selection.items:
                if moment.citation is not None:
                    assert moment.key in delivered_keys, "a cited card for a Hit the model never saw"
        else:
            # No tab only when the search returned nothing to show.
            assert not tab_keys
    finally:
        current_turn_context.reset(ctx_token)
        turn_end.release(turn_id)


# ===========================================================================
# Turn-end orchestration unit tests (Task 6.7) — appended below in their own
# section. Intentionally left for Task 6.7 so it can add tests without touching
# the Property 10 section above.
# ===========================================================================
import contextlib  # noqa: E402

from jobs.context import reset_request_context, set_request_context  # noqa: E402

from project_search.ledger import SearchLedgerEntry  # noqa: E402
from project_search.session_hits import SessionHitStoreError  # noqa: E402

# ---------------------------------------------------------------------------
# Unit-test fixtures: build the pieces finish_turn reads (ledger, context,
# bound session id, resolver) directly, so each case exercises one branch of
# the seven-step orchestration without driving the whole search tool.
# ---------------------------------------------------------------------------
def _unit_hit(asset_id, start_ms, end_ms, *, title, source_index="scene", final_score=10, frame_s3_key=None):
    """A well-formed Hit (Req 3.1) whose whole-second span is [start_ms, end_ms]."""
    hit = {
        "asset_id": asset_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "final_score": final_score,
        "source_index": source_index,
        "title": title,
    }
    if frame_s3_key is not None:
        hit["frame_s3_key"] = frame_s3_key
    return hit


def _record_entry(turn_id, tool_use_id, hits, *, project_id=_PID, ok=True):
    """Record one ok ledger entry under the Active_Project for ``turn_id``."""
    ledger.record(
        SearchLedgerEntry(
            turn_id=turn_id, tool_use_id=tool_use_id, project_id=project_id, ok=ok, hits=list(hits)
        )
    )


def _resolver_for(hits):
    """A resolver that resolves every hit's asset_id to a non-removed asset named after its title."""
    return _FakeResolver(
        {
            h["asset_id"]: ResolvedAsset(
                asset_id=h["asset_id"],
                kind=(h["asset_id"].split(":", 1)[0] if ":" in h["asset_id"] else "video"),
                display_name=h["title"],
                status="indexed",
            )
            for h in hits
        }
    )


@contextlib.contextmanager
def _bound_turn(turn_id, *, session_id, project_access="ok"):
    """Bind the turn's Projects context and (optionally) a session id, cleaning up afterward."""
    tc = TurnContext(
        turn_id=turn_id,
        active_project=ActiveProject(id=_PID, name="", collection_id=_PID, personal=False),
        web_search=False,
        project_access=project_access,
    )
    ctx_token = current_turn_context.set(tc)
    sess_tokens = (
        set_request_context(actor_id="u1", session_id=session_id) if session_id else None
    )
    try:
        yield
    finally:
        if sess_tokens is not None:
            reset_request_context(sess_tokens)
        current_turn_context.reset(ctx_token)
        turn_end.release(turn_id)


class _AppendFailsStore:
    """A SessionHitStore whose append always raises; reads return nothing."""

    def __init__(self):
        self.append_calls = 0

    def append(self, session_id, turn_id, entries):
        self.append_calls += 1
        raise SessionHitStoreError("append boom")

    def hits(self, session_id, project_id):
        return []

    def frame_keys(self, session_id, project_id):
        return frozenset()


class _HitsFailStore:
    """A SessionHitStore whose append succeeds but whose reads raise."""

    def __init__(self):
        self.append_calls = 0
        self.hits_calls = 0

    def append(self, session_id, turn_id, entries):
        self.append_calls += 1

    def hits(self, session_id, project_id):
        self.hits_calls += 1
        raise SessionHitStoreError("hits boom")

    def frame_keys(self, session_id, project_id):
        raise SessionHitStoreError("frame_keys boom")


# ---------------------------------------------------------------------------
# Ordering: the outcome exists when a session_end rule runs.
#
# The hook awaits finish_turn before firing session_end rules, so the stored
# outcome must be readable through outcome_for the instant finish_turn returns
# (that is what the Moments_Rule reads). Validates: Requirements 2.6
# ---------------------------------------------------------------------------
def test_outcome_is_readable_as_soon_as_finish_turn_returns():
    turn_id = "unit-order-" + uuid.uuid4().hex
    hit = _unit_hit("video:order", 1_000, 3_000, title="Clip Order")
    with _bound_turn(turn_id, session_id="sess-" + uuid.uuid4().hex):
        _record_entry(turn_id, "tu-order", [hit])
        turn_end.note_answer_text(turn_id, f"See {format_citation('Clip Order', 1_000, 3_000)}.")

        outcome = turn_end.finish_turn(
            store=MemorySessionHitStore(), resolver=_resolver_for([hit])
        )

        # The returned outcome is the same object a session_end rule reads via outcome_for.
        assert outcome is not None
        assert turn_end.outcome_for(turn_id) is outcome
        assert outcome.turn_id == turn_id and outcome.project_id == _PID
        verified = [r for r in outcome.citations if r.verified]
        assert len(verified) == 1 and verified[0].ref.asset_id == "video:order"


# ---------------------------------------------------------------------------
# Store append failure: this turn's Hits still verify from memory.
#
# A failed append only costs later turns; this turn classifies against the
# in-memory ledger Hits. finish_turn must not raise. Validates: Requirements 2.6
# ---------------------------------------------------------------------------
def test_store_append_failure_still_verifies_this_turn():
    turn_id = "unit-appendfail-" + uuid.uuid4().hex
    hit = _unit_hit("video:af", 1_000, 3_000, title="Clip AF")
    store = _AppendFailsStore()
    with _bound_turn(turn_id, session_id="sess-" + uuid.uuid4().hex):
        _record_entry(turn_id, "tu-af", [hit])
        turn_end.note_answer_text(turn_id, f"Here {format_citation('Clip AF', 1_000, 3_000)}.")

        outcome = turn_end.finish_turn(store=store, resolver=_resolver_for([hit]))

        assert store.append_calls == 1  # the failing append was attempted
        assert outcome is not None
        verified = [r for r in outcome.citations if r.verified]
        assert len(verified) == 1 and verified[0].ref.asset_id == "video:af"
        # This turn's Hits are present even though nothing was persisted.
        assert {sh.hit["asset_id"] for sh in outcome.turn_hits} == {"video:af"}


# ---------------------------------------------------------------------------
# Store read failure: fall back to this turn's Hits only.
#
# When hits() raises, only this turn's Hits remain, so this turn's Citation
# still verifies (an earlier-turn Citation would become no_matching_hit).
# Validates: Requirements 2.6
# ---------------------------------------------------------------------------
def test_store_read_failure_falls_back_to_turn_hits():
    turn_id = "unit-readfail-" + uuid.uuid4().hex
    hit = _unit_hit("video:rf", 1_000, 3_000, title="Clip RF")
    store = _HitsFailStore()
    with _bound_turn(turn_id, session_id="sess-" + uuid.uuid4().hex):
        _record_entry(turn_id, "tu-rf", [hit])
        turn_end.note_answer_text(turn_id, f"Look {format_citation('Clip RF', 1_000, 3_000)}.")

        outcome = turn_end.finish_turn(store=store, resolver=_resolver_for([hit]))

        assert store.append_calls == 1 and store.hits_calls == 1  # both were attempted
        assert outcome is not None
        verified = [r for r in outcome.citations if r.verified]
        assert len(verified) == 1 and verified[0].ref.asset_id == "video:rf"
        # The read failure left only this turn's Hits as candidates.
        assert {sh.hit["asset_id"] for sh in outcome.session_hits} == {"video:rf"}


# ---------------------------------------------------------------------------
# No Projects context: nothing is verified and no outcome is stored.
#
# finish_turn returns None both when the contextvar is unset and when the turn
# id is empty; its text then renders as before. Validates: Requirements 2.6
# ---------------------------------------------------------------------------
def test_none_context_returns_none():
    token = current_turn_context.set(None)
    try:
        assert (
            turn_end.finish_turn(store=MemorySessionHitStore(), resolver=_FakeResolver({}))
            is None
        )
    finally:
        current_turn_context.reset(token)


def test_empty_turn_id_returns_none_and_stores_nothing():
    tc = TurnContext(
        turn_id="",
        active_project=ActiveProject(id=_PID, name="", collection_id=_PID, personal=False),
        web_search=False,
        project_access="ok",
    )
    token = current_turn_context.set(tc)
    try:
        assert (
            turn_end.finish_turn(store=MemorySessionHitStore(), resolver=_FakeResolver({}))
            is None
        )
        assert turn_end.outcome_for("") is None
    finally:
        current_turn_context.reset(token)


# ---------------------------------------------------------------------------
# Multi-cycle answer buffer: every data chunk of the turn accumulates.
#
# note_answer_text is called for every text event of a multi-cycle stream; the
# buffer is their concatenation, so a Citation split across two chunks (and with
# empty / non-string chunks interleaved) parses and verifies as one.
# Validates: Requirements 2.6
# ---------------------------------------------------------------------------
def test_multi_cycle_answer_buffer_concatenates_every_chunk():
    turn_id = "unit-multi-" + uuid.uuid4().hex
    hit = _unit_hit("video:mc", 1_000, 3_000, title="Clip MC")
    answer = f"Before the tool {format_citation('Clip MC', 1_000, 3_000)} after the tool."
    mid = len(answer) // 2
    with _bound_turn(turn_id, session_id="sess-" + uuid.uuid4().hex):
        _record_entry(turn_id, "tu-mc", [hit])
        # A multi-cycle stream: text before a tool cycle, then text after it, with
        # ignorable chunks interleaved (empty str and a non-str are no-ops).
        turn_end.note_answer_text(turn_id, answer[:mid])
        turn_end.note_answer_text(turn_id, "")
        turn_end.note_answer_text(turn_id, None)  # type: ignore[arg-type]
        turn_end.note_answer_text(turn_id, answer[mid:])

        assert turn_end._answer_text(turn_id) == answer  # exact concatenation, nothing dropped/added

        outcome = turn_end.finish_turn(store=MemorySessionHitStore(), resolver=_resolver_for([hit]))
        assert outcome is not None
        verified = [r for r in outcome.citations if r.verified]
        assert len(verified) == 1 and verified[0].ref.asset_id == "video:mc"


# ---------------------------------------------------------------------------
# release clears the ledger, the answer buffer, and the stored outcome.
# Validates: Requirements 2.6
# ---------------------------------------------------------------------------
def test_release_clears_ledger_buffer_and_outcome():
    turn_id = "unit-release-" + uuid.uuid4().hex
    hit = _unit_hit("video:rel", 1_000, 3_000, title="Clip Rel")
    tc = TurnContext(
        turn_id=turn_id,
        active_project=ActiveProject(id=_PID, name="", collection_id=_PID, personal=False),
        web_search=False,
        project_access="ok",
    )
    ctx_token = current_turn_context.set(tc)
    sess_tokens = set_request_context(actor_id="u1", session_id="sess-" + uuid.uuid4().hex)
    try:
        _record_entry(turn_id, "tu-rel", [hit])
        turn_end.note_answer_text(turn_id, f"Clip {format_citation('Clip Rel', 1_000, 3_000)}.")
        outcome = turn_end.finish_turn(store=MemorySessionHitStore(), resolver=_resolver_for([hit]))

        # Before release: everything is present.
        assert outcome is not None
        assert ledger.results_for(turn_id) != []
        assert turn_end.outcome_for(turn_id) is not None
        assert turn_end._answer_text(turn_id) != ""

        turn_end.release(turn_id)

        # After release: ledger, outcome, and answer buffer are all gone.
        assert ledger.results_for(turn_id) == []
        assert turn_end.outcome_for(turn_id) is None
        assert turn_end._answer_text(turn_id) == ""
    finally:
        reset_request_context(sess_tokens)
        current_turn_context.reset(ctx_token)
        turn_end.release(turn_id)


# ===========================================================================
# Moments show every search result (Req 3.4/3.5 as amended 2026-10-10).
# M2C is asked for 50 hits; the model still reads only its `k`; the overflow feeds
# the Moments tab alone and can never verify a Citation.
# ===========================================================================
def _twelve_hits():
    return [
        {"asset_id": f"video:{i}", "start_ms": i * 10_000 + 1_000, "end_ms": i * 10_000 + 3_000,
         "final_score": 100 - i, "source_index": "scene", "title": f"Clip {i}", "text": f"t{i}"}
        for i in range(12)
    ]


def test_model_still_reads_top_k_and_overflow_lands_in_ledger_extra_hits():
    hits = _twelve_hits()
    client = _FakeClient(SearchOutcome("call", "ok", result=json.dumps({"results": hits})))
    turn_id = "all-k-" + uuid.uuid4().hex
    tool = ProjectSearchTool(ProjectTokenRef(_tok()), _SPEC, project_id=_PID, turn_id=turn_id, client=client)
    try:
        result = _stream_result(tool, {"collection_id": _PID, "search_term": "q"}, "tu-k")
        payload = json.loads(result["content"][0]["text"])
        assert [h["asset_id"] for h in payload["results"]] == [f"video:{i}" for i in range(10)]
        entry = ledger.results_for(turn_id)[0]
        assert entry.hits == hits[:10]
        assert entry.extra_hits == hits[10:]
        assert client.calls[0][1]["k"] == 50  # asked M2C for the tab's depth
        # The image-search allowlist only admits frames the model was shown.
        assert ledger.frame_keys_for(turn_id, _PID) == frozenset()
    finally:
        ledger.clear_turn(turn_id)


def test_citation_of_an_overflow_hit_never_verifies_but_its_moment_renders():
    hits = _twelve_hits()
    by_id = {h["asset_id"]: ResolvedAsset(h["asset_id"], "video", h["title"], "indexed") for h in hits}
    client = _FakeClient(SearchOutcome("call", "ok", result=json.dumps({"results": hits})))
    turn_id = "all-cite-" + uuid.uuid4().hex
    tool = ProjectSearchTool(ProjectTokenRef(_tok()), _SPEC, project_id=_PID, turn_id=turn_id, client=client)
    tc = TurnContext(turn_id=turn_id,
                     active_project=ActiveProject(id=_PID, name="", collection_id=_PID, personal=False),
                     web_search=False, project_access="ok")
    token = current_turn_context.set(tc)
    try:
        _stream_result(tool, {"collection_id": _PID, "search_term": "q"}, "tu-c")
        # Cite a delivered Hit (#3) and an overflow Hit (#11) the model never saw.
        answer = (f"See {format_citation('Clip 3', *whole_second_range(hits[3]))} and "
                  f"{format_citation('Clip 11', *whole_second_range(hits[11]))}.")
        turn_end.note_answer_text(turn_id, answer)
        outcome = turn_end.finish_turn(store=MemorySessionHitStore(), resolver=_FakeResolver(by_id))
        verified = [r for r in outcome.citations if r.verified]
        assert [r.ref.asset_id for r in verified] == ["video:3"]
        assert all(sh.hit in hits[:10] for sh in outcome.session_hits)  # overflow never a verify candidate

        sel = select_moments(outcome)
        keys = [m.key[0] for m in sel.items]
        assert keys[0] == "video:3" and sel.items[0].citation is not None
        assert sorted(keys) == sorted(h["asset_id"] for h in hits)  # all 12 render
        assert keys[-2:] == ["video:10", "video:11"]  # overflow after every delivered Hit
    finally:
        current_turn_context.reset(token)
        turn_end.release(turn_id)


# The Moments tab's search box starts from the turn's first search term: finish_turn carries the turn's
# distinct terms, in call order, on the outcome.
def test_outcome_carries_the_turns_distinct_search_terms_in_call_order():
    turn_id = "unit-queries-" + uuid.uuid4().hex
    hit = _unit_hit("video:q", 1_000, 3_000, title="Clip Q")
    with _bound_turn(turn_id, session_id="sess-" + uuid.uuid4().hex):
        for tu, term in (("tu1", "night drive"), ("tu2", "night drive"), ("tu3", "rain #topic"), ("tu4", "  ")):
            ledger.record(SearchLedgerEntry(turn_id=turn_id, tool_use_id=tu, project_id=_PID, ok=True,
                                            hits=[hit], search_term=term))
        outcome = turn_end.finish_turn(store=MemorySessionHitStore(), resolver=_resolver_for([hit]))
        assert outcome is not None and outcome.queries == ["night drive", "rain #topic"]
