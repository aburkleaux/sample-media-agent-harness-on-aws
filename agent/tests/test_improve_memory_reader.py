"""Owner resolution in `improve/observe/memory_reader.py`, offline. No AWS, no credentials.

Memory events are keyed `(memoryId, actorId, sessionId)`, so nothing can be read until the
OWNING actor is known — and the failure mode of getting that wrong is quiet out of all
proportion to its cost. `conversation_for_session` returns `Conversation(session_id=...)`
when no actor claims the session, and that single empty object costs the run THREE things at
once (asserted directly in `test_an_unresolved_owner_loses_owner_response_and_turns_together`):

  * `actor_id is None`   -> `build_run` leaves `Run.actor_id` UNKNOWN, so the person who ran
                            the session gets no per-actor Improve bundle;
  * `final_assistant_text is None` -> `Run.response` stays NO_DATA;
  * `turns == []`        -> `Run.turns` is empty.

The judge scores "did the agent understand the request" and "did a usable result arrive" off
exactly that goal/response pair, so a session whose owner cannot be found is unjudgeable while
presenting as a thin-but-fine run. Two defects produced exactly that, and these tests pin both:

  1. COMPLETENESS — `list_actors` was called once with `maxResults=100` and its `nextToken`
     was never followed, so every actor past page 1 was invisible and their sessions could
     never resolve. Measured against the staging conversation memory
     `mediaworks_harness_conversation_memory-hvhTmA58za` (us-west-2) on 2026-08-31: 14 actors
     in ONE page with no `nextToken`, so the cap was not biting and this is a latent fix —
     which is why `test_the_measured_fourteen_actor_page_resolves_the_same_owner_either_way`
     exists to prove today's answer does not move.
  2. COST — the search then probed `list_events(actor, session)` actor by actor for every
     session: O(actors x sessions), up to 14 x 25 = 350 calls per producer run inside a
     Lambda with a 15-minute ceiling.

The fakes here are deliberately plain dict-returning clients with manual `nextToken`
pagination, matching `compliance._iter_events`' note about not requiring a boto3 paginator.
`_ModelCheckedMemory` additionally runs botocore's own `ParamValidator` for
`bedrock-agentcore` over every call, because a fake looser than the real API hides total
breakage.
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import botocore.session  # noqa: E402
from botocore.validate import ParamValidator  # noqa: E402

from improve.observe import memory_reader as mr  # noqa: E402
from improve.observe.memory_reader import MemoryReader  # noqa: E402

# A real staging memory id. Not decoration: the service model gives `MemoryId` a min length of
# 12, so a placeholder like "mem" is rejected by `_ModelCheckedMemory` below.
MEMORY_ID = "mediaworks_harness_conversation_memory-hvhTmA58za"


def _event(role: str, text: str, ts: int):
    """One `list_events` event in the shape the reader actually parses.

    Content is double-JSON-wrapped exactly as Memory stores it — the outer
    `conversational.content.text` is itself a message envelope — so these fixtures exercise
    `_unwrap_event_content` rather than sidestepping it.
    """
    envelope = '{"message": {"role": "%s", "content": [{"text": "%s"}]}}' % (role, text)
    return {"eventTimestamp": ts,
            "payload": [{"conversational": {"role": role, "content": {"text": envelope}}}]}


def _turns(*specs):
    """`[(role, text, ts), ...]` -> a list of events."""
    return [_event(role, text, ts) for role, text, ts in specs]


class _FakeMemory:
    """A plain dict-returning `bedrock-agentcore` stand-in that COUNTS its calls.

    `actor_pages` is the literal `list_actors` response sequence, so a test can hand it one
    page (today's staging shape) or several. `events` is `{(actor_id, session_id): [event]}`.
    `sessions` is `{actor_id: [session_id]}` and drives `list_sessions`; passing
    `supports_list_sessions=False` removes the method entirely, which is the only honest way
    to model a client whose boto3 predates the operation.
    """

    def __init__(self, actor_pages, events=None, sessions=None,
                 supports_list_sessions=True, raise_on_events=(), raise_on_sessions=()):
        self.actor_pages = list(actor_pages)
        self.events = dict(events or {})
        self.sessions = dict(sessions or {})
        self.raise_on_events = set(raise_on_events)
        self.raise_on_sessions = set(raise_on_sessions)
        self.calls = {"list_actors": 0, "list_sessions": 0, "list_events": 0}
        self.events_probed: list[tuple] = []
        # The full `list_events` parameters, not just the key, so a test can assert HOW a call was
        # made and not only that it happened — the owner-only lookup pages one event and the
        # conversation read pages the maximum, and a fake that hid that could not tell them apart.
        self.event_params: list[dict] = []
        if not supports_list_sessions:
            # Deleting the bound method makes `getattr(client, "list_sessions", None)` fall to
            # None, which is what the reader branches on.
            self.list_sessions = None

    def _record(self, name, params):
        self.calls[name] += 1

    def list_actors(self, **params):
        self._record("list_actors", params)
        token = params.get("nextToken")
        index = int(token) if token else 0
        return self.actor_pages[index]

    def list_sessions(self, **params):
        self._record("list_sessions", params)
        actor_id = params["actorId"]
        if actor_id in self.raise_on_sessions:
            raise RuntimeError(f"AccessDeniedException: no ListSessions grant ({actor_id})")
        return {"sessionSummaries": [{"sessionId": sid, "actorId": actor_id}
                                     for sid in self.sessions.get(actor_id, [])]}

    def list_events(self, **params):
        self._record("list_events", params)
        key = (params["actorId"], params["sessionId"])
        self.events_probed.append(key)
        self.event_params.append(dict(params))
        if params["actorId"] in self.raise_on_events:
            raise RuntimeError(f"ThrottlingException on {params['actorId']}")
        return {"events": list(self.events.get(key, []))}


class _ModelCheckedMemory(_FakeMemory):
    """`_FakeMemory` plus botocore's own `ParamValidator` on every call.

    The same validator the deployed worker's client runs, against botocore's bundled
    `bedrock-agentcore` model. Offline and creds-free — it catches a misspelled parameter or a
    missing required one, which a hand-written fake accepts silently.
    """

    _MODEL = botocore.session.get_session().get_service_model("bedrock-agentcore")
    _OPERATION = {"list_actors": "ListActors", "list_sessions": "ListSessions",
                  "list_events": "ListEvents"}

    def _record(self, name, params):
        shape = self._MODEL.operation_model(self._OPERATION[name]).input_shape
        report = ParamValidator().validate(dict(params), shape)
        assert not report.has_errors(), f"{name}: {report.generate_report()}"
        super()._record(name, params)


def _reader(client, **kwargs):
    reader = MemoryReader(memory_id=MEMORY_ID, **kwargs)
    # The one seam that would otherwise construct a boto3 session. Set directly rather than
    # patched, so no test in this file can reach AWS even by accident.
    reader._client = client
    return reader


def _actor_page(actor_ids, next_token=None):
    page = {"actorSummaries": [{"actorId": a} for a in actor_ids]}
    if next_token is not None:
        page["nextToken"] = next_token
    return page


# The measured staging shape: 14 actors, 8 of them 36-char UUIDs and 6 longer non-UUID ids,
# returned in ONE page with no `nextToken` (2026-08-31). Kept literal because the whole
# identical-behaviour claim is about THIS list.
FOURTEEN_ACTORS = [
    "6f1c2f6e-1111-4c2a-9a55-aaaaaaaaaaaa",
    "a2b4c6d8-2222-4c2a-9a55-bbbbbbbbbbbb",
    "b3c5d7e9-3333-4c2a-9a55-cccccccccccc",
    "c4d6e8fa-4444-4c2a-9a55-dddddddddddd",
    "d5e7f9ab-5555-4c2a-9a55-eeeeeeeeeeee",
    "e6f8aabc-6666-4c2a-9a55-ffffffffffff",
    "f709bbcd-7777-4c2a-9a55-000000000000",
    "081accde-8888-4c2a-9a55-111111111111",
    "us-west-2:9c1d2e3f-4a5b-6c7d-8e9f-0a1b2c3d4e5f",
    "us-west-2:8b0c1d2e-3f4a-5b6c-7d8e-9f0a1b2c3d4e",
    "us-west-2:7a9b0c1d-2e3f-4a5b-6c7d-8e9f0a1b2c3d",
    "us-west-2:6980abbc-1d2e-3f4a-5b6c-7d8e9f0a1b2c",
    "us-west-2:587faaab-0c1d-2e3f-4a5b-6c7d8e9f0a1b",
    "us-west-2:476e99aa-fb0c-1d2e-3f4a-5b6c7d8e9f0a",
]
OWNER = FOURTEEN_ACTORS[8]
SESSION = "abc839ca-1234-4c2a-9a55-abcdefabcdef"
CONVERSATION = _turns(("USER", "transcode the promo master", 1),
                      ("ASSISTANT", "started the transcode", 2),
                      ("USER", "and the thumbnails", 3),
                      ("ASSISTANT", "here is the finished bundle", 4))


# ---- problem 1: completeness ---------------------------------------------------------------

def test_the_measured_fourteen_actor_page_resolves_the_same_owner_either_way():
    """Today's data must answer identically, by BOTH routes.

    14 actors in one page with no `nextToken` is what staging returned on 2026-08-31, so this
    is the case the fix must not move. The owner is the 9th actor — past the point where a
    first-match shortcut would look right — and the two clients differ only in whether
    `list_sessions` exists, i.e. whether the owner map or the per-actor probe resolves it.
    Both must produce the same actor, the same turns and the same final response.
    """
    resolved = []
    for supports in (False, True):
        client = _ModelCheckedMemory(
            [_actor_page(FOURTEEN_ACTORS)], events={(OWNER, SESSION): CONVERSATION},
            sessions={OWNER: [SESSION]}, supports_list_sessions=supports)
        conversation = _reader(client).conversation_for_session(SESSION)
        resolved.append(conversation)
        assert conversation.actor_id == OWNER
        assert conversation.final_assistant_text == "here is the finished bundle"
        assert conversation.user_texts == ["transcode the promo master", "and the thumbnails"]
        assert [t["role"] for t in conversation.turns] == [
            "USER", "ASSISTANT", "USER", "ASSISTANT"]
        assert client.calls["list_actors"] == 1

    probe, mapped = resolved
    assert probe.turns == mapped.turns
    assert probe.actor_id == mapped.actor_id


def test_an_actor_on_the_second_page_is_found():
    """The completeness fix. 103 actors over two pages, owner LAST on page 2.

    Reading page 1 only — one `list_actors(maxResults=100)` with the `nextToken` dropped —
    made this session unresolvable, and it surfaced as an empty Conversation rather than an
    error. Asserted through the probe path (no `list_sessions`) because that is the path whose
    coverage depends on the actor list being whole.
    """
    page_one = [f"actor-{i:03d}" for i in range(100)]
    page_two = ["actor-100", "actor-101", "actor-102"]
    owner = "actor-102"
    client = _ModelCheckedMemory(
        [_actor_page(page_one, next_token="1"), _actor_page(page_two)],
        events={(owner, SESSION): CONVERSATION}, supports_list_sessions=False)
    reader = _reader(client)

    conversation = reader.conversation_for_session(SESSION)

    assert conversation.actor_id == owner
    assert conversation.final_assistant_text == "here is the finished bundle"
    assert client.calls["list_actors"] == 2
    assert reader.actor_list_complete() is True


def test_the_actor_sweep_is_read_once_per_reader_not_once_per_session():
    """One `MemoryReader` serves a whole producer run (`loop.run_once`, `mine_local`), so the
    two-page sweep must cost two calls for the run, not two per session."""
    client = _FakeMemory([_actor_page(FOURTEEN_ACTORS[:7], next_token="1"),
                          _actor_page(FOURTEEN_ACTORS[7:])],
                         events={(OWNER, SESSION): CONVERSATION},
                         sessions={OWNER: [SESSION]})
    reader = _reader(client)
    for _ in range(5):
        assert reader.conversation_for_session(SESSION).actor_id == OWNER
    assert client.calls["list_actors"] == 2


def test_a_truncated_actor_list_is_loud_and_flagged_not_silent(monkeypatch, caplog):
    """The bound is explicit, and hitting it must not read as thin data.

    A truncated actor list produces unowned sessions and unjudgeable runs, so both the sweep
    that truncated AND the session that consequently failed to resolve report at ERROR, and
    `actor_list_complete()` records it for a caller that would rather branch than grep logs.
    """
    monkeypatch.setattr(mr, "_ACTOR_PAGE_CAP", 1)
    client = _FakeMemory([_actor_page(["actor-000"], next_token="1"),
                          _actor_page(["actor-001"])],
                         events={("actor-001", SESSION): CONVERSATION},
                         supports_list_sessions=False)
    reader = _reader(client)
    assert reader.actor_list_complete() is None

    with caplog.at_level(logging.ERROR, logger=mr.__name__):
        conversation = reader.conversation_for_session(SESSION)

    assert reader.actor_list_complete() is False
    assert client.calls["list_actors"] == 1
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("list_actors hit the 1-page cap" in m for m in errors)
    assert any("truncated at the 1-page cap" in m for m in errors)
    # And the loss itself is still the loss — the fix makes it audible, not absent.
    assert conversation.actor_id is None


def test_an_unresolved_owner_loses_owner_response_and_turns_together():
    """Why this defect matters more than attribution: ONE empty Conversation, THREE losses.

    `build_run` reads `actor_id`, `final_assistant_text` and `turns` off this single object
    (and falls back to `user_texts[0]` for the goal), so an unresolved owner does not merely
    mislabel the run — it leaves `Run.response` NO_DATA and `Run.turns` empty, which is
    precisely the goal/response pair the judge grades.
    """
    client = _FakeMemory([_actor_page(FOURTEEN_ACTORS)], events={}, supports_list_sessions=False)

    conversation = _reader(client).conversation_for_session(SESSION)

    assert conversation.actor_id is None
    assert conversation.final_assistant_text is None
    assert conversation.turns == []
    assert conversation.user_texts == []


# ---- problem 2: cost of the scan ----------------------------------------------------------

def test_the_owner_map_replaces_the_quadratic_probe():
    """14 actors x 25 sessions: 350 `list_events` calls become 25, resolved by one sweep.

    The map is built ONCE for the reader — one `list_sessions` per actor — and every session
    then costs exactly one `list_events` against its own owner. The probe count is asserted
    against the same population so the two costs are compared rather than asserted in
    isolation.
    """
    sessions = [f"session-{i:02d}" for i in range(25)]
    owners = {sid: FOURTEEN_ACTORS[i % 14] for i, sid in enumerate(sessions)}
    events = {(actor, sid): CONVERSATION for sid, actor in owners.items()}
    by_actor: dict = {}
    for sid, actor in owners.items():
        by_actor.setdefault(actor, []).append(sid)

    mapped = _ModelCheckedMemory([_actor_page(FOURTEEN_ACTORS)], events=events,
                                 sessions=by_actor)
    reader = _reader(mapped)
    for sid in sessions:
        assert reader.conversation_for_session(sid).actor_id == owners[sid]
    assert mapped.calls == {"list_actors": 1, "list_sessions": 14, "list_events": 25}

    probing = _ModelCheckedMemory([_actor_page(FOURTEEN_ACTORS)], events=events,
                                  supports_list_sessions=False)
    probe_reader = _reader(probing)
    for sid in sessions:
        assert probe_reader.conversation_for_session(sid).actor_id == owners[sid]
    # The old cost, still the fallback's cost: every session walks the actor list until it
    # hits its owner, so a session owned by the nth actor costs n probes. Round-robining 25
    # sessions over 14 actors is sum((i % 14) + 1 for i in range(25)) = 171 here; the worst
    # case — every session owned by the LAST actor, or by none — is the full 25 x 14 = 350.
    assert probing.calls["list_events"] == 171
    assert probing.calls["list_events"] > 6 * mapped.calls["list_events"]


def test_the_owner_map_never_serves_a_stale_conversation():
    """The map caches session OWNERSHIP, which is immutable — never the events.

    A session read twice in one run must see the second read's events. Caching the
    Conversation would be the stale-answer-within-a-run failure the map is written to avoid.
    """
    client = _FakeMemory([_actor_page(FOURTEEN_ACTORS)],
                         events={(OWNER, SESSION): _turns(("ASSISTANT", "first", 1))},
                         sessions={OWNER: [SESSION]})
    reader = _reader(client)
    assert reader.conversation_for_session(SESSION).final_assistant_text == "first"

    client.events[(OWNER, SESSION)] = _turns(("ASSISTANT", "first", 1),
                                             ("ASSISTANT", "second", 2))

    assert reader.conversation_for_session(SESSION).final_assistant_text == "second"
    assert client.calls["list_events"] == 2


def test_a_session_the_map_does_not_know_still_resolves_by_probe():
    """A session created after the sweep is a MISS, and a miss must not be an answer.

    This is what stops the once-per-run map from going stale within the run: it is consulted
    for hits only, so the worst a miss can cost is the probe it was there to avoid.
    """
    late = "session-created-after-the-sweep"
    client = _FakeMemory([_actor_page(FOURTEEN_ACTORS)],
                         events={(OWNER, late): CONVERSATION},
                         sessions={OWNER: [SESSION]})

    conversation = _reader(client).conversation_for_session(late)

    assert conversation.actor_id == OWNER
    assert conversation.final_assistant_text == "here is the finished bundle"


def test_a_mapped_owner_with_no_events_falls_back_to_the_probe():
    """Session ids are namespaced per actor, so two actors can hold the same one.

    `list_sessions` names the first, whose event list is empty; the probe's rule — first actor
    with NON-EMPTY events wins — names the second. The map must not overrule that, or the
    answer would depend on which route found it.
    """
    claimant, real_owner = FOURTEEN_ACTORS[2], FOURTEEN_ACTORS[9]
    client = _FakeMemory([_actor_page(FOURTEEN_ACTORS)],
                         events={(real_owner, SESSION): CONVERSATION},
                         sessions={claimant: [SESSION], real_owner: [SESSION]})

    conversation = _reader(client).conversation_for_session(SESSION)

    assert conversation.actor_id == real_owner
    assert conversation.final_assistant_text == "here is the finished bundle"


def test_the_map_prefers_the_first_actor_exactly_as_the_probe_does():
    """When two actors both have events under one session id, both routes pick the same one:
    the first in `list_actors` order. A different tie-break would make the accelerator change
    the answer."""
    first, second = FOURTEEN_ACTORS[3], FOURTEEN_ACTORS[11]
    events = {(first, SESSION): _turns(("ASSISTANT", "first actor", 1)),
              (second, SESSION): _turns(("ASSISTANT", "second actor", 1))}
    mapped = _FakeMemory([_actor_page(FOURTEEN_ACTORS)], events=events,
                         sessions={second: [SESSION], first: [SESSION]})
    probing = _FakeMemory([_actor_page(FOURTEEN_ACTORS)], events=events,
                          supports_list_sessions=False)

    assert _reader(mapped).conversation_for_session(SESSION).actor_id == first
    assert _reader(probing).conversation_for_session(SESSION).actor_id == first


def test_an_ungranted_list_sessions_costs_exactly_one_denied_call(caplog):
    """The deployed improve-worker is granted ListActors + ListEvents only.

    So the map must degrade on the FIRST refusal, not spend one AccessDenied per actor, and
    resolution must continue by the probe. The warning names the grant, because the cheaper
    path is one IAM statement away and nothing else would say so.
    """
    client = _FakeMemory([_actor_page(FOURTEEN_ACTORS)],
                         events={(OWNER, SESSION): CONVERSATION},
                         sessions={OWNER: [SESSION]},
                         raise_on_sessions=FOURTEEN_ACTORS)
    with caplog.at_level(logging.WARNING, logger=mr.__name__):
        conversation = _reader(client).conversation_for_session(SESSION)

    assert conversation.actor_id == OWNER
    assert client.calls["list_sessions"] == 1
    assert any("bedrock-agentcore:ListSessions" in r.getMessage() for r in caplog.records)


def test_the_session_page_cap_warns_and_leaves_the_probe_to_finish(monkeypatch, caplog):
    """Truncating the MAP is not the same failure as truncating the actor list.

    A session the sweep did not reach is a miss, and a miss probes — so this bound costs calls
    and never an answer, which is why it warns where the actor cap errors.
    """
    monkeypatch.setattr(mr, "_SESSION_PAGE_CAP", 1)
    unmapped = "session-past-the-session-page-cap"

    class _PagingSessions(_FakeMemory):
        def list_sessions(self, **params):
            self._record("list_sessions", params)
            if params.get("nextToken"):
                return {"sessionSummaries": [{"sessionId": unmapped,
                                              "actorId": params["actorId"]}]}
            return {"sessionSummaries": [{"sessionId": SESSION, "actorId": params["actorId"]}],
                    "nextToken": "1"}

    client = _PagingSessions([_actor_page([OWNER])],
                             events={(OWNER, unmapped): CONVERSATION})
    with caplog.at_level(logging.WARNING, logger=mr.__name__):
        conversation = _reader(client).conversation_for_session(unmapped)

    assert conversation.actor_id == OWNER
    assert any("list_sessions hit the 1-page cap" in r.getMessage() for r in caplog.records)


# ---- the guarantee the swallowed exception provides ---------------------------------------

def test_one_unreadable_actor_does_not_hide_an_owner_further_down_the_list():
    """Why the probe swallows exceptions PER ACTOR, kept intact.

    `list_events` can fail for one actor — AccessDenied on a resource-scoped grant, a
    throttle, an actor deleted between the two calls — while the owner is further down. A
    single `except` around the whole loop would turn one bad actor into every later actor's
    sessions losing owner, response and turns.
    """
    broken = FOURTEEN_ACTORS[:8]
    client = _FakeMemory([_actor_page(FOURTEEN_ACTORS)],
                         events={(OWNER, SESSION): CONVERSATION},
                         supports_list_sessions=False, raise_on_events=broken)

    conversation = _reader(client).conversation_for_session(SESSION)

    assert conversation.actor_id == OWNER
    assert client.events_probed[:8] == [(a, SESSION) for a in broken]


def test_a_mapped_owner_whose_events_raise_still_reaches_the_probe():
    """The fast path inherits the same tolerance. An owner the map named but whose events
    could not be read must not decide the session has no conversation — the probe runs, and
    if it too finds nothing the empty Conversation keeps its single meaning."""
    client = _FakeMemory([_actor_page(FOURTEEN_ACTORS)],
                         events={(OWNER, SESSION): CONVERSATION},
                         sessions={FOURTEEN_ACTORS[0]: [SESSION]},
                         raise_on_events={FOURTEEN_ACTORS[0]})

    conversation = _reader(client).conversation_for_session(SESSION)

    assert conversation.actor_id == OWNER
    assert conversation.final_assistant_text == "here is the finished bundle"


# ---- the owner, without the conversation ---------------------------------------------------
#
# A plan sub-session has NO conversation of its own: its turns live under the request that spawned
# it, so `conversation_for_session` on the child's own id returns nothing. The owner can therefore
# only come from the parent, which means one lookup keyed on the parent's session id — and it must
# return an ID and never the parent's turns, because those are a different session's prose and the
# judge would score this run against them.

def test_the_owner_of_a_session_is_returned_without_its_conversation():
    """An id, never a `Conversation`. The turns belong to the parent and must not travel."""
    client = _ModelCheckedMemory([_actor_page(FOURTEEN_ACTORS)],
                                 events={(OWNER, SESSION): CONVERSATION},
                                 sessions={OWNER: [SESSION]})
    reader = _reader(client)

    assert reader.owner_of_session(SESSION) == OWNER
    assert reader.owner_of_session("a-session-no-actor-claims") is None


def test_the_owner_map_answers_without_a_single_extra_events_call():
    """Free where `ListSessions` is granted: the sweep the run already paid for names every session
    of every actor, so resolving a parent's owner costs no call at all."""
    client = _ModelCheckedMemory([_actor_page(FOURTEEN_ACTORS)],
                                 events={(OWNER, SESSION): CONVERSATION},
                                 sessions={OWNER: [SESSION]})
    reader = _reader(client)
    reader.conversation_for_session(SESSION)      # the run's normal work builds the map
    before = dict(client.calls)

    assert reader.owner_of_session(SESSION) == OWNER
    assert client.calls == before, "a mapped owner must cost nothing"


def test_without_list_sessions_the_probe_answers_and_stops_at_the_owner():
    """THE DEPLOYED CASE, not an edge case: the improve-worker is granted `ListActors` +
    `ListEvents` only, so the owner map is empty in production and the probe is the real path.

    ``maxResults=1`` is what keeps it cheap — the question is whether this actor has ANY event for
    the session, so a parent with 55 turns costs the same as one with two. And it stops at the first
    hit rather than sweeping the whole roster.
    """
    client = _ModelCheckedMemory([_actor_page(FOURTEEN_ACTORS)],
                                 events={(OWNER, SESSION): CONVERSATION},
                                 raise_on_sessions=set(FOURTEEN_ACTORS))
    reader = _reader(client)

    assert reader.owner_of_session(SESSION) == OWNER
    # The owner is the 9th actor, so nine probes and none after it.
    assert client.calls["list_events"] == FOURTEEN_ACTORS.index(OWNER) + 1
    assert [call["maxResults"] for call in client.event_params] == [1] * 9, \
        "one event settles ownership; paging the whole conversation would be waste"


def test_a_session_nobody_owns_is_asked_about_once_per_run():
    """A miss is cached as a miss. Without that, a population whose sub-sessions all share one
    unresolvable parent would re-sweep every actor once per child — the quadratic probe this
    reader already paid to remove, reintroduced by the back door."""
    client = _ModelCheckedMemory([_actor_page(FOURTEEN_ACTORS)],
                                 raise_on_sessions=set(FOURTEEN_ACTORS))
    reader = _reader(client)

    assert reader.owner_of_session("nobodys-session") is None
    after_first = client.calls["list_events"]
    for _ in range(5):
        assert reader.owner_of_session("nobodys-session") is None
    assert client.calls["list_events"] == after_first


def test_one_unreadable_actor_does_not_decide_that_a_session_is_unowned():
    """Per-actor tolerance, the same as the conversation probe's: `list_events` can fail for ONE
    actor — a resource-scoped denial, a throttle, an actor deleted between the two calls — and the
    owner may still be further down the list."""
    client = _ModelCheckedMemory([_actor_page(FOURTEEN_ACTORS)],
                                 events={(OWNER, SESSION): CONVERSATION},
                                 raise_on_events=set(FOURTEEN_ACTORS[:8]),
                                 raise_on_sessions=set(FOURTEEN_ACTORS))
    assert _reader(client).owner_of_session(SESSION) == OWNER


def test_the_actor_roster_is_the_same_sweep_and_costs_no_second_call():
    """`aws_lambda` needs the roster to write a measured zero for an actor with nothing in the
    window. It is the SAME list the owner resolution swept, so asking for it after a run is a read
    of memory rather than a second `list_actors`."""
    client = _ModelCheckedMemory([_actor_page(FOURTEEN_ACTORS)])
    reader = _reader(client)
    reader.owner_of_session("anything")           # forces the sweep
    before = client.calls["list_actors"]

    assert reader.actor_ids() == FOURTEEN_ACTORS
    assert client.calls["list_actors"] == before
    assert reader.actor_ids() is not reader.actor_ids(), "a copy, so a caller cannot mutate it"


# ---- the fakes must not be looser than the API --------------------------------------------

def test_the_page_size_is_within_the_service_models_own_bounds():
    """`_PAGE_SIZE` is the API's documented maximum, not a guess.

    botocore's `ParamValidator` does NOT range-check integers, so `_ModelCheckedMemory` would
    accept `maxResults=500` happily while the service rejected it. Read the bound off the
    model instead.
    """
    shape = _ModelCheckedMemory._MODEL.shape_for("MaxResults")
    assert shape.metadata["min"] <= mr._PAGE_SIZE <= shape.metadata["max"]
    assert mr._PAGE_SIZE == shape.metadata["max"]


@pytest.mark.parametrize("cap_name, env_name", [
    ("_ACTOR_PAGE_CAP", "IMPROVE_ACTOR_PAGE_CAP"),
    ("_SESSION_PAGE_CAP", "IMPROVE_SESSION_PAGE_CAP"),
])
def test_both_page_caps_are_explicit_and_overridable(cap_name, env_name):
    """An operational bound that cannot be raised without a deploy is not a bound, it is a
    ceiling. Both are named, positive, and read from the environment like `harvest_live`'s."""
    assert getattr(mr, cap_name) > 0
    assert env_name in open(mr.__file__).read()
