"""Read the real conversation for a session from AgentCore Memory.

The response text the judge scores is not in spans; it is in Memory, retrievable
via ``bedrock-agentcore.list_events``. Event content is double-JSON-wrapped
(the outer ``conversational.content.text`` is itself a JSON message envelope),
and sessions are multi-turn, so goal/response extraction is a real parse.

Events are keyed by ``(memoryId, actorId, sessionId)``, so every read has to resolve the
OWNER first — and getting that wrong is expensive out of proportion to how it looks. A
session whose owner is not found returns an empty ``Conversation``, which costs the run
three things at once: the owner (so that user gets no per-actor bundle), the final
assistant response, and the turns. The judge scores "did the agent understand the request"
and "did a usable result arrive" off exactly that goal/response pair, so such a run is
unjudgeable while presenting as merely quiet. Owner resolution is therefore written to be
complete (every actor page) and cheap (one sweep per run), in that order.

Read-only.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

# Default staging memory id; override via config (loop.config.yaml observe.memory_id)
# or the IMPROVE_MEMORY_ID env var. Not hard-wired into call sites.
import os as _os

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_ID = _os.environ.get(
    "IMPROVE_MEMORY_ID", "mediaworks_harness_conversation_memory-hvhTmA58za"
)

# The API's own per-page maximum for list_actors / list_sessions / list_events (`MaxResults`
# is bounded at 100 by the service model). One constant rather than the literal repeated at
# three call sites, so the three cannot drift apart.
_PAGE_SIZE = 100

# OPERATIONAL, env-overridable. How many ``list_actors`` PAGES the owner lookup will follow.
#
# Same discipline as ``harvest_live``'s ``_ALARM_PAGE_CAP``: follow ``nextToken``, but bound the
# total so a runaway list cannot spend the improve-worker's 15-minute Lambda ceiling — and report
# it when the bound truncates. Reading page 1 only, which is what this did, is the failure that
# matters: every actor past the cap owns sessions nothing can attribute, and each of those
# sessions comes back as an EMPTY Conversation (no owner, no response, no turns) rather than as
# an error. That reads as thin data and is actually a correctness loss, so truncation is logged
# at ERROR both where the list is read and where a session fails to resolve.
#
# 20 pages x 100 = 2,000 actors. Measured against the staging conversation memory
# ``mediaworks_harness_conversation_memory-hvhTmA58za`` (us-west-2) on 2026-08-31: 14 actors,
# returned in ONE page with NO ``nextToken``. So the cap is nowhere near binding on today's data
# and following the token changes nothing about today's output — this is a latent fix.
_ACTOR_PAGE_CAP = int(_os.environ.get("IMPROVE_ACTOR_PAGE_CAP", "20") or "20")

# OPERATIONAL, env-overridable. How many ``list_sessions`` PAGES the owner MAP will follow per
# actor. Deliberately NOT the same constant as ``_ACTOR_PAGE_CAP`` even though both default to
# 20: they bound different things and an account can need one raised without the other.
#
# Truncating here is not a correctness loss the way truncating the actor list is, because the map
# is only ever consulted for HITS — a session it does not name falls back to the per-actor probe.
# So this one warns rather than errors: it costs calls, not answers.
_SESSION_PAGE_CAP = int(_os.environ.get("IMPROVE_SESSION_PAGE_CAP", "20") or "20")


@dataclass
class Conversation:
    session_id: str
    actor_id: Optional[str] = None
    # [{role, text, tool_uses, raw, ts}] — OLDEST-first. The reader sorts by
    # eventTimestamp because list_events returns NEWEST-first; relying on API order
    # made final_assistant_text return the session's FIRST assistant turn.
    turns: list[dict] = field(default_factory=list)

    @property
    def final_assistant_text(self) -> Optional[str]:
        for turn in reversed(self.turns):
            if turn["role"] == "ASSISTANT" and turn["text"]:
                return turn["text"]
        return None

    @property
    def user_texts(self) -> list[str]:
        # Exclude toolResult echoes; keep genuine user prose. Multi-turn: the
        # first entry is not assumed to be the goal.
        return [t["text"] for t in self.turns
                if t["role"] == "USER" and t["text"] and "toolResult" not in t["raw"]]


def _unwrap_event_content(conversational: dict) -> tuple[str, str, list[str]]:
    """Return (role, plain_text, tool_use_names) from one conversational block.

    The block's content.text is a JSON string:
    {"message": {"role", "content": [{"text"|"toolUse"|"toolResult"}...]}}.
    """
    role = conversational.get("role", "")
    content = conversational.get("content", {})
    raw = content.get("text", "") if isinstance(content, dict) else ""
    try:
        envelope = json.loads(raw)
        blocks = envelope.get("message", {}).get("content", [])
    except (json.JSONDecodeError, TypeError, AttributeError):
        return role, raw, []
    texts = [b["text"] for b in blocks if isinstance(b, dict) and "text" in b]
    tool_uses = [b["toolUse"]["name"] for b in blocks
                 if isinstance(b, dict) and isinstance(b.get("toolUse"), dict) and b["toolUse"].get("name")]
    return role, "\n".join(texts).strip(), tool_uses


class MemoryReader:
    """Reads a session's conversation from AgentCore Memory (read-only)."""

    def __init__(self, memory_id: str = DEFAULT_MEMORY_ID, region: str = "us-west-2",
                 profile: Optional[str] = None, max_events: int = 1000) -> None:
        self._memory_id = memory_id
        self._max_events = max_events
        self._region = region
        self._profile = profile
        self._client = None
        self._actors: Optional[list[str]] = None
        # Three-state, like ``evidence_complete`` on a Run: None until the sweep has actually run,
        # because False would assert a truncation nobody observed.
        self._actors_complete: Optional[bool] = None
        self._owners: Optional[dict[str, str]] = None
        # Owners resolved by `owner_of_session`, INCLUDING the ones that resolved to nobody, so a
        # session with no owner is asked about once per run instead of re-probing every actor each
        # time. Ownership is immutable, unlike the events, which are never cached.
        self._owner_probes: dict[str, Optional[str]] = {}

    def _bac(self):
        if self._client is None:
            import boto3
            session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
            self._client = session.client("bedrock-agentcore", region_name=self._region)
        return self._client

    def actor_list_complete(self) -> Optional[bool]:
        """Whether the actor sweep read EVERY page, or None before any sweep has run.

        The completeness flag the callers of this reader can check, in the same spirit as the
        harvest's own flags: a short actor list and a small account look identical from outside,
        and only the sweep knows which it saw.
        """
        return self._actors_complete

    def _actor_ids(self) -> list[str]:
        """Every actor in this memory, in API order, following ``nextToken``.

        Read ONCE per reader and reused, which is once per producer run — ``loop.run_once`` and
        ``mine_local`` each construct one ``MemoryReader`` and hand it to every ``build_run``.

        Paged rather than capped at a single ``maxResults=100`` call. Nothing about today's data
        changes (14 actors, one page, no token — measured 2026-08-31), but an account that grows
        past one page would silently hide every actor after the first, and their sessions would
        surface as runs with no owner and no response instead of as an error.
        """
        if self._actors is None:
            bac = self._bac()
            actors: list[str] = []
            token: Optional[str] = None
            pages = 0
            complete = True
            while True:
                kwargs = dict(memoryId=self._memory_id, maxResults=_PAGE_SIZE)
                if token:
                    kwargs["nextToken"] = token
                resp = bac.list_actors(**kwargs) or {}
                summaries = resp.get("actorSummaries", resp.get("actors", [])) or []
                actors.extend(a.get("actorId") for a in summaries if a.get("actorId"))
                token = resp.get("nextToken")
                pages += 1
                if not token:
                    break
                if pages >= _ACTOR_PAGE_CAP:
                    complete = False
                    logger.error(
                        "list_actors hit the %d-page cap after %d actors and MORE remain — every "
                        "actor past it is invisible, so their sessions resolve to no owner, no "
                        "response and no turns and cannot be judged. Raise "
                        "IMPROVE_ACTOR_PAGE_CAP.", _ACTOR_PAGE_CAP, len(actors))
                    break
            self._actors = actors
            self._actors_complete = complete
        return self._actors

    def _session_owner_map(self) -> dict[str, str]:
        """``{session_id: actor_id}`` for every actor's sessions, built ONCE per reader.

        This is the cost half. The original resolution path probed ``list_events(actor, session)``
        actor by actor for EVERY session, i.e. O(actors x sessions) calls: at the 14 actors and 25
        sessions measured on staging 2026-08-31, up to 350 calls per producer run, and paging the
        actor list past one page multiplies that inside a 15-minute Lambda. ``list_sessions``
        returns ``(sessionId, actorId)`` pairs, so one sweep of O(actors) calls answers every
        session at once — 14 calls plus one ``list_events`` per session instead of up to 350.

        An ACCELERATOR, never an authority, which is what makes it safe to hold for a whole run.
        Only HITS are used: ``conversation_for_session`` falls back to the original probe when this
        map has no entry, or names an actor whose events cannot be read, so a session created
        after the sweep, a partial sweep, or a refused call can only cost calls, never an answer.
        And it caches session OWNERSHIP, which is immutable, never the events, which are re-read
        every time; so nothing here can serve a stale conversation within a run.

        Degrades on the FIRST refusal rather than per actor: the deployed improve-worker's grant is
        ``ListActors`` + ``ListEvents`` only, so an AccessDenied here is expected in production and
        spending one denied call per actor would be pure waste.
        """
        if self._owners is not None:
            return self._owners
        owners: dict[str, str] = {}
        lister = getattr(self._bac(), "list_sessions", None)
        if not callable(lister):
            # A boto3 too old to know the operation, or a client that implements only the two calls
            # the probe needs. Nothing to report: the probe IS the original path.
            self._owners = owners
            return self._owners
        for actor_id in self._actor_ids():
            token: Optional[str] = None
            pages = 0
            try:
                while True:
                    kwargs = dict(memoryId=self._memory_id, actorId=actor_id,
                                  maxResults=_PAGE_SIZE)
                    if token:
                        kwargs["nextToken"] = token
                    resp = lister(**kwargs) or {}
                    for summary in resp.get("sessionSummaries", []) or []:
                        session_id = summary.get("sessionId")
                        # FIRST actor wins, matching the probe's own tie-break exactly. Session ids
                        # are namespaced per actor, so two actors CAN hold the same one, and the
                        # probe returns the first in ``list_actors`` order — the map must not pick
                        # a different winner than the path it accelerates.
                        if session_id and session_id not in owners:
                            owners[session_id] = summary.get("actorId") or actor_id
                    token = resp.get("nextToken")
                    pages += 1
                    if not token:
                        break
                    if pages >= _SESSION_PAGE_CAP:
                        logger.warning(
                            "list_sessions hit the %d-page cap for one actor at %d mapped "
                            "sessions — the rest fall back to the per-actor list_events probe, "
                            "which costs calls and loses nothing. Raise "
                            "IMPROVE_SESSION_PAGE_CAP.", _SESSION_PAGE_CAP, len(owners))
                        break
            except Exception as e:  # noqa: BLE001 — a failed accelerator costs calls, not answers
                logger.warning(
                    "list_sessions unusable (%s: %s) — the session owner map stops at %d "
                    "session(s) and the rest resolve by the per-actor list_events probe, as they "
                    "always did. Expected where only ListActors + ListEvents are granted; grant "
                    "bedrock-agentcore:ListSessions to get the cheaper path.",
                    type(e).__name__, e, len(owners))
                break
        self._owners = owners
        return self._owners

    def owner_of_session(self, session_id: str) -> Optional[str]:
        """WHO owns this session, and deliberately nothing else. ``None`` when no actor claims it.

        Exists for the one caller that needs an owner for a session whose CONVERSATION belongs to
        somebody else: a plan sub-session has no conversation of its own, so the actor can only come
        from the request that spawned it. That caller must not receive the parent's turns — they are
        a different session's prose and would be scored as if they were this one's — so this returns
        an id and never a `Conversation`. ``conversation_for_session`` is the right call whenever the
        turns are wanted; this is not a cheaper version of it.

        Three sources, cheapest first, and none of them can answer WRONG:

          the probe cache      already asked this run. Ownership is immutable, so this is safe to
                               hold for the whole run in a way the events never would be.
          the session map      one sweep per run, and free here because something else already paid
                               for it. Empty where ``ListSessions`` is not granted — which is the
                               deployed improve-worker's case (`ListActors` + `ListEvents` only), so
                               the fallback below is the PRODUCTION path and not an edge case.
          the per-actor probe  ``list_events`` per actor until one answers, exactly the tie-break
                               ``conversation_for_session`` uses (first in ``list_actors`` order).

        ``maxResults=1`` on the probe, unlike ``_all_events``' full paged read: the question is
        whether this actor has ANY event for the session, so one event settles it and a parent with
        55 turns costs the same as one with two.

        A miss is cached as ``None`` rather than left to be re-probed. Without that, a population
        whose sub-sessions all share one unresolvable parent would re-sweep every actor per child.
        """
        if session_id in self._owner_probes:
            return self._owner_probes[session_id]
        owner = self._session_owner_map().get(session_id)
        if not owner:
            bac = self._bac()
            for actor_id in self._actor_ids():
                try:
                    resp = bac.list_events(memoryId=self._memory_id, actorId=actor_id,
                                           sessionId=session_id, maxResults=1) or {}
                except Exception:  # noqa: BLE001
                    # Per-actor tolerance for the same reason `conversation_for_session` has it: one
                    # actor's AccessDenied or throttle must not decide that the session is unowned
                    # when the owner may be further down the list.
                    continue
                if resp.get("events"):
                    owner = actor_id
                    break
        self._owner_probes[session_id] = owner or None
        return self._owner_probes[session_id]

    def actor_ids(self) -> list[str]:
        """Every actor this memory knows, for a caller that needs the ROSTER rather than a lookup.

        The public face of `_actor_ids`. It is what makes "this actor spent nothing" distinguishable
        from "we have never heard of this actor": a producer can only write a measured zero for an
        actor it can name, and this memory is the only place the harness enumerates them.

        Read once per reader and shared with the owner resolution above, so asking for the roster
        after a run has resolved owners costs no call at all.
        """
        return list(self._actor_ids())

    def _all_events(self, bac, actor_id: str, session_id: str) -> list[dict]:
        """Every event for the session, oldest-first, following pagination.

        ``list_events`` returns events NEWEST-first and caps at ``maxResults``, so a
        single unsorted page silently yields a reversed, truncated conversation. Both
        halves matter: without the sort, ``final_assistant_text`` returns the session's
        FIRST assistant turn; without pagination, long sessions lose their oldest turns.
        """
        events: list[dict] = []
        token: Optional[str] = None
        while True:
            kwargs = dict(memoryId=self._memory_id, actorId=actor_id,
                          sessionId=session_id, maxResults=_PAGE_SIZE)
            if token:
                kwargs["nextToken"] = token
            resp = bac.list_events(**kwargs)
            events.extend(resp.get("events", []))
            token = resp.get("nextToken")
            if not token or len(events) >= self._max_events:
                break
        # Sort ascending so index order == chronological order. Events without a
        # timestamp sort last rather than being dropped.
        events.sort(key=lambda e: (e.get("eventTimestamp") is None, e.get("eventTimestamp")))
        return events

    @staticmethod
    def _conversation(session_id: str, actor_id: str, events: list[dict]) -> Conversation:
        """One owner's events -> a Conversation. Shared by both resolution paths.

        Factored out so the mapped path and the probe cannot come to build turns differently: a
        session's conversation must not depend on WHICH route found its owner.
        """
        turns = []
        for event in events:
            for payload in (event.get("payload") or []):
                conversational = payload.get("conversational", {}) if isinstance(payload, dict) else {}
                if not conversational:
                    continue
                role, text, tool_uses = _unwrap_event_content(conversational)
                if not text and not tool_uses:
                    continue
                raw = conversational.get("content", {}).get("text", "") if isinstance(conversational.get("content"), dict) else ""
                turns.append({"role": role, "text": text, "tool_uses": tool_uses,
                              "raw": raw, "ts": str(event.get("eventTimestamp") or "")})
        return Conversation(session_id=session_id, actor_id=actor_id, turns=turns)

    def conversation_for_session(self, session_id: str) -> Conversation:
        """Find the actor that owns this session and return its conversation.

        Turns are returned OLDEST-first; ``turns[-1]`` is the latest turn.

        Two routes to the same answer. The session owner map (``_session_owner_map``, one sweep per
        run) is tried first and costs one ``list_events``; on a miss — no entry, or an entry whose
        events cannot be read or come back empty — the original per-actor probe runs unchanged.
        Keeping the probe as the backstop is what makes the fast path unable to answer WRONG, and
        it means an empty Conversation still has exactly one meaning: no actor in the actor list
        holds this session.
        """
        bac = self._bac()
        owner = self._session_owner_map().get(session_id)
        if owner:
            try:
                events = self._all_events(bac, owner, session_id)
            except Exception:
                # Same tolerance as the probe below and for the same reason: one actor's
                # unreadable events must not decide that a session has no conversation.
                events = []
            if events:
                return self._conversation(session_id, owner, events)
        for actor_id in self._actor_ids():
            try:
                events = self._all_events(bac, actor_id, session_id)
            except Exception:
                # Per-actor tolerance, deliberately: `list_events` can fail for ONE actor —
                # AccessDenied on a resource-scoped grant, a throttle, an actor deleted between
                # the two calls — and the owner may still be further down the list. Aborting the
                # search on the first such error would turn one bad actor into every later
                # actor's sessions losing owner, response and turns.
                continue
            if not events:
                continue
            return self._conversation(session_id, actor_id, events)
        if self._actors_complete is False:
            logger.error(
                "session %s: no actor claims it AND the actor list was truncated at the %d-page "
                "cap, so its owner may simply be past the cap. This run loses its owner, its "
                "final response and its turns, which the judge needs to score whether the "
                "request was understood and a usable result arrived — treat it as a gap, not as "
                "thin data. Raise IMPROVE_ACTOR_PAGE_CAP.", session_id[:8], _ACTOR_PAGE_CAP)
        return Conversation(session_id=session_id)
