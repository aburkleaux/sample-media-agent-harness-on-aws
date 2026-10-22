"""Property test for the Session_Hit_Store (Task 4.4).

Feature: media-harness-search-qa-rendering, Property 11: Session_Hit_Store append
idempotency and Project isolation.

Validates: Requirements 1.20, 2.5, 3.4

The one property is run against BOTH implementations in `project_search.session_hits`:
`MemorySessionHitStore` and `DynamoSessionHitStore` on an in-process moto DynamoDB table
shaped like the plan-state table (``session_id`` HASH, ``node_id`` RANGE).

Store internals read the wall clock (``time.time_ns``) once per first append of a
``(session, turn)`` to stamp a turn's ``turn_seq``. We inject a strictly-increasing clock
(``session_hits.time`` is only ever used for ``time_ns``) so recency ordering is a total
order and the oracle is deterministic. Hit payloads are kept small so the 350 KB size cap
never trims; the cap itself is exercised by the dedicated test at the bottom.
"""

import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import boto3  # noqa: E402
import pytest  # noqa: E402
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from moto import mock_aws  # noqa: E402

from project_search import session_hits  # noqa: E402
from project_search.ledger import SearchLedgerEntry  # noqa: E402
from project_search.session_hits import (  # noqa: E402
    MAX_CALL_ITEMS,
    DynamoSessionHitStore,
    MemorySessionHitStore,
    SessionHit,
)

TABLE_NAME = "mediaworks-harness-plan-state-local"
REGION = "us-east-1"

SESSIONS = ["s0", "s1", "s2"]
PROJECTS = ["p0", "p1", "p2"]
# Non-empty frame keys live in a tiny pool so values repeat across hits (frame_keys is a set).
FRAME_POOL = ["f0", "f1", "f2"]


# ---------------------------------------------------------------------------
# Injected clock
# ---------------------------------------------------------------------------
class _Clock:
    """Strictly-increasing ``time_ns`` surrogate.

    Returns ``n * 1_000_000`` so that ``value // 1_000_000`` (the ms ``turn_seq`` both stores
    derive) is distinct and increasing on every call — one call per first-append of a turn.
    """

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> int:
        self.n += 1
        return self.n * 1_000_000


# ---------------------------------------------------------------------------
# Store construction (fresh per Hypothesis example for isolation)
# ---------------------------------------------------------------------------
def _create_plan_state_table(dynamodb) -> None:
    dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "session_id", "KeyType": "HASH"},
            {"AttributeName": "node_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "session_id", "AttributeType": "S"},
            {"AttributeName": "node_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@contextmanager
def _store(backend: str):
    if backend == "memory":
        yield MemorySessionHitStore()
    else:
        with mock_aws():
            dynamodb = boto3.resource("dynamodb", region_name=REGION)
            _create_plan_state_table(dynamodb)
            yield DynamoSessionHitStore(TABLE_NAME, region=REGION)


# ---------------------------------------------------------------------------
# Hypothesis strategy: raw turns + an op order with interleaved repeats
# ---------------------------------------------------------------------------
_hit = st.fixed_dictionaries(
    {
        # None -> no frame_s3_key; "" -> present but empty; otherwise a pooled non-empty key.
        "frame": st.one_of(st.none(), st.just(""), st.sampled_from(FRAME_POOL)),
    }
)

_entry = st.fixed_dictionaries(
    {
        "project": st.sampled_from(PROJECTS),
        "ok": st.booleans(),
        "hits": st.lists(_hit, max_size=4),
    }
)

_turn = st.fixed_dictionaries(
    {
        "session": st.sampled_from(SESSIONS),
        "entries": st.lists(_entry, max_size=4),
    }
)


@st.composite
def _scenario(draw):
    turns = draw(st.lists(_turn, max_size=6))
    n = len(turns)
    if n == 0:
        return turns, []
    # An op order that interleaves repeats: a free draw of turn indices (giving repeats and
    # interleaving), then every missing index appended so each turn is appended at least once.
    ops = draw(st.lists(st.integers(min_value=0, max_value=n - 1), max_size=3 * n))
    present = set(ops)
    ops = ops + [i for i in range(n) if i not in present]
    return turns, ops


# ---------------------------------------------------------------------------
# Materialise generated turns into concrete ids (unique by position)
# ---------------------------------------------------------------------------
def _materialise(turns):
    """Return per-turn ``(turn_id, entries)`` with globally-unique ids derived from positions.

    Each entry's ``turn_id`` equals its turn's ``turn_id`` and every ``tool_use_id`` is unique
    within the append, so ``MemorySessionHitStore`` (dedup on ``(turn_id, tool_use_id)``) and
    ``DynamoSessionHitStore`` (dedup on the full node id) have identical observable behaviour.
    """
    out = []
    for i, turn in enumerate(turns):
        turn_id = f"t-{i}"
        entries = []
        for j, e in enumerate(turn["entries"]):
            hits = []
            for k, h in enumerate(e["hits"]):
                hit = {"id": f"h-{i}-{j}-{k}"}
                if h["frame"] is not None:
                    hit["frame_s3_key"] = h["frame"]
                hits.append(hit)
            entries.append(
                SearchLedgerEntry(
                    turn_id=turn_id,
                    tool_use_id=f"tu-{i}-{j}",
                    project_id=e["project"],
                    ok=e["ok"],
                    hits=hits,
                )
            )
        out.append((turn["session"], turn_id, entries))
    return out


def _apply(store, materialised, ops):
    for ti in ops:
        session_id, turn_id, entries = materialised[ti]
        store.append(session_id, turn_id, entries)


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------
def _first_positions(ops):
    first = {}
    for pos, ti in enumerate(ops):
        if ti not in first:
            first[ti] = pos
    return first


def _oracle(turns, materialised, ops, session, project):
    """Expected ``(ordered_hit_ids, frame_key_set)`` for ``hits``/``frame_keys``."""
    first = _first_positions(ops)
    # One call item per accepted ok entry under (session, project).
    call_items = []  # (turn_pos, call_index, [(recency, hit_id)], frame_keys_of_item)
    for ti, (sess, _turn_id, entries) in enumerate(materialised):
        if sess != session or ti not in first:
            continue
        tpos = first[ti]
        for call_index, entry in enumerate(entries):
            if not entry.ok or entry.project_id != project:
                continue
            hit_rows = []
            frames = set()
            for k, hit in enumerate(entry.hits):
                hit_rows.append(((tpos, call_index, -k), hit["id"]))
                fk = hit.get("frame_s3_key")
                if isinstance(fk, str) and fk:
                    frames.add(fk)
            call_items.append((tpos, call_index, hit_rows, frames))
    # Newest call items first, capped at MAX_CALL_ITEMS, per Project.
    call_items.sort(key=lambda c: (c[0], c[1]), reverse=True)
    call_items = call_items[:MAX_CALL_ITEMS]
    hit_rows = [row for c in call_items for row in c[2]]
    hit_rows.sort(key=lambda r: r[0], reverse=True)
    ordered_ids = [hid for _, hid in hit_rows]
    frame_keys = set()
    for c in call_items:
        frame_keys |= c[3]
    return ordered_ids, frame_keys


# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend", ["memory", "dynamo"])
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(scenario=_scenario())
def test_property_11_append_idempotency_and_project_isolation(backend, scenario):
    turns, ops = scenario
    materialised = _materialise(turns)

    with mock.patch.object(session_hits, "time", SimpleNamespace(time_ns=_Clock())):
        with _store(backend) as store:
            _apply(store, materialised, ops)

            # hits/frame_keys match the oracle for every (session, project) — including pairs
            # with no data (expected empty), which also proves isolation across sessions/Projects.
            snapshot = {}
            for session in SESSIONS:
                for project in PROJECTS:
                    want_ids, want_frames = _oracle(turns, materialised, ops, session, project)

                    got = store.hits(session, project)
                    assert all(isinstance(sh, SessionHit) for sh in got)
                    # Never a Hit returned under another Project.
                    assert all(sh.project_id == project for sh in got)
                    got_ids = [sh.hit["id"] for sh in got]
                    # Exactly-once, no foreign hits, recency order, 100-call-item cap.
                    assert got_ids == want_ids
                    # Each delivered hit appears exactly once.
                    assert len(got_ids) == len(set(got_ids))

                    got_frames = store.frame_keys(session, project)
                    assert isinstance(got_frames, frozenset)
                    assert set(got_frames) == want_frames

                    snapshot[(session, project)] = (got_ids, set(got_frames))

            # Repeating every append leaves hits and frame_keys unchanged (idempotence).
            _apply(store, materialised, ops)
            for session in SESSIONS:
                for project in PROJECTS:
                    want_ids, want_frames = snapshot[(session, project)]
                    assert [sh.hit["id"] for sh in store.hits(session, project)] == want_ids
                    assert set(store.frame_keys(session, project)) == want_frames


# ---------------------------------------------------------------------------
# The 100-call-item cap (exercised deterministically; one example, both backends)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend", ["memory", "dynamo"])
def test_hits_cover_only_newest_100_call_items(backend):
    session, project = "s0", "p0"
    total = MAX_CALL_ITEMS + 25  # > 100 call items under one Project
    materialised = []
    for i in range(total):
        turn_id = f"t-{i}"
        entry = SearchLedgerEntry(
            turn_id=turn_id,
            tool_use_id=f"tu-{i}",
            project_id=project,
            ok=True,
            hits=[{"id": f"h-{i}", "frame_s3_key": f"frame-{i}"}],
        )
        materialised.append((session, turn_id, [entry]))

    with mock.patch.object(session_hits, "time", SimpleNamespace(time_ns=_Clock())):
        with _store(backend) as store:
            for session_id, turn_id, entries in materialised:
                store.append(session_id, turn_id, entries)

            got = store.hits(session, project)
            # Only the newest 100 call items (one hit each), most-recent-first.
            assert [sh.hit["id"] for sh in got] == [f"h-{i}" for i in range(total - 1, total - 1 - MAX_CALL_ITEMS, -1)]
            # The oldest 25 are excluded.
            got_ids = {sh.hit["id"] for sh in got}
            assert all(f"h-{i}" not in got_ids for i in range(total - MAX_CALL_ITEMS))
            # frame_keys reflects only those newest 100 call items.
            assert set(store.frame_keys(session, project)) == {
                f"frame-{i}" for i in range(total - 1, total - 1 - MAX_CALL_ITEMS, -1)
            }
