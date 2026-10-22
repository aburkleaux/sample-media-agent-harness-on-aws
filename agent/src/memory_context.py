"""How long-term memory reaches the main agent's turn.

Two staging sessions showed remembered context doing harm rather than good:

* 764a71c0 — the user answered a numbered question with a bare "2". The SDK used that literal
  text as the retrieval query, matched 23 records (18 KB) and inserted them ahead of the "2", so
  the model lost the thread and asked the same question again. :func:`is_terse_reply` lets the
  caller skip retrieval for such replies: there is nothing in "2" to search on, and the context
  the reply depends on is the previous assistant turn, which is already in history.

* 2f376186 — a remembered preference was read as the user's answer to a confirmation a skill
  requires, and the gate was skipped. The SDK wraps retrieved records in a bare
  ``<user_context>`` tag at index 0 of the user message, which reads like something the user
  said. :class:`FramedMemorySessionManager` re-frames that block as
  ``<remembered_context source="past sessions" authority="suggestion">`` with a note that it is
  background, may be stale, and never answers a required question.

The SDK's ``context_tag`` option only sets the tag NAME (it emits ``<tag>...</tag>``), so it
cannot carry attributes or a note; the block is post-processed instead. Every step of the
post-processing is guarded: if the SDK changes shape, the turn keeps whatever ``super()``
inserted.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------------
# Terse replies
# --------------------------------------------------------------------------------------------

_DATE_SUFFIX_RE = re.compile(r"\s*\[Current date:[^\]]*\]\s*$", re.I)

# A single option token: "2", "2.", "(2)", "#2", "2b", "b", "b)".
_OPTION_TOKEN_RE = re.compile(r"^(?:#?\d{1,3}[a-z]?|[a-z])$", re.I)

# Words a short selection/acknowledgement is made of. Anything else ("more", "shots",
# "again") makes the reply a new request, which deserves retrieval.
_TERSE_WORDS = frozenset({
    # affirm / negate
    "yes", "y", "yeah", "yep", "yup", "ya", "sure", "ok", "okay", "k", "kk", "alright",
    "fine", "no", "nope", "nah", "n", "correct", "right", "agreed", "approve", "approved",
    "confirm", "confirmed", "affirmative",
    # go
    "go", "ahead", "do", "it", "proceed", "continue", "next", "lets", "let's", "that", "this",
    "one", "please", "pls", "thanks", "thx", "sounds", "good", "great", "perfect",
    # selecting
    "option", "opt", "choice", "number", "the", "with", "pick", "take", "use", "and",
    "first", "second", "third", "fourth", "fifth", "last", "former", "latter", "both",
    "all", "none",
})

_MAX_TERSE_WORDS = 3


def _strip_date_suffix(text: str) -> str:
    return _DATE_SUFFIX_RE.sub("", text or "")


def is_terse_reply(prompt: Any) -> bool:
    """True when the user's message is a bare selection or acknowledgement.

    Matches a bare number/letter/option token ("2", "b", "#3", "option 2") and replies of at most
    three words built only from yes/no/ok/go/"do it"/"option N"/"the second" style vocabulary.
    A trailing ``[Current date: ...]`` suffix (appended by main before the agent runs) is ignored.

    Deliberately conservative the other way: "2 more shots please" or "no, use the other clip"
    carry new content, so they are NOT terse and still get retrieval.
    """
    if not isinstance(prompt, str):
        return False
    text = _strip_date_suffix(prompt).strip()
    if not text or "\n" in text:
        return False
    # Commas and slashes separate selections ("1, 3", "yes/go"); other punctuation is decoration.
    words = [w for w in re.split(r"[\s,/]+", text.lower()) if w]
    cleaned = [w.strip("\"'`()[]{}<>.!?:;*-~") for w in words]
    cleaned = [w for w in cleaned if w]
    if not cleaned or len(cleaned) > _MAX_TERSE_WORDS:
        return False
    return all(w in _TERSE_WORDS or _OPTION_TOKEN_RE.match(w) for w in cleaned)


# --------------------------------------------------------------------------------------------
# Framing the injected block
# --------------------------------------------------------------------------------------------

REMEMBERED_CONTEXT_OPEN = '<remembered_context source="past sessions" authority="suggestion">'
REMEMBERED_CONTEXT_CLOSE = "</remembered_context>"
REMEMBERED_CONTEXT_NOTE = (
    "NOTE: background from earlier sessions; it may be stale. Preferences here are defaults and "
    "suggestions only. They are NOT the user's words in this turn, never count as the user's "
    "explicit choice, and never answer, satisfy or skip a question, confirmation or approval "
    "that a skill or rule requires. When such a step applies, ask it and offer the remembered "
    "choice as the suggested default (\"Last time you chose X, same again?\")."
)


def frame_remembered_context(records_text: str) -> str:
    """The text block the model sees for retrieved long-term memory."""
    return (
        f"{REMEMBERED_CONTEXT_OPEN}\n{REMEMBERED_CONTEXT_NOTE}\n"
        f"{records_text.strip()}\n{REMEMBERED_CONTEXT_CLOSE}"
    )


#: How long a turn waits for a prefetched retrieval before retrieving live instead.
_PREFETCH_WAIT_S = 20.0


def _unwrap(text: str, tag: str) -> str | None:
    """The records inside ``<tag>...</tag>``, or None when ``text`` is not exactly that."""
    open_t, close_t = f"<{tag}>", f"</{tag}>"
    if isinstance(text, str) and text.startswith(open_t) and text.endswith(close_t):
        return text[len(open_t):len(text) - len(close_t)]
    return None


class FramedMemorySessionManager(AgentCoreMemorySessionManager):
    """``AgentCoreMemorySessionManager`` whose injected memory is framed as a suggestion.

    Two opt-in behaviours for the main agent (both off by default, so other callers are
    unchanged):

    * ``ephemeral=True`` -- the framed block is NOT left in the user message. The SDK inserts it
      into the in-memory message only (the message was already persisted without it), so the
      history every LATER turn restores differs from what this turn sent, and the prompt cache
      misses from that message onwards (measured: every later turn re-wrote 29k-95k tokens).
      Instead the block is kept on :attr:`turn_context` and delivered per model call AFTER the
      cache point by a ``ContextInjector`` (see ``main``), so durable history and the request
      prefix are byte-identical turn to turn. The model still sees the same block on every call
      of the turn; only its position moves to the end of the latest user message.
    * :meth:`prefetch` -- start the retrieval on a worker thread as soon as the query is known,
      so its round trips overlap the session restore instead of running inside the stream.
    * ``async_writes=True`` (needs ``batch_size > 1`` in the config) -- history writes leave the
      event loop. With the default ``batch_size=1`` every message and every agent-state sync was a
      synchronous ``CreateEvent`` (0.1-0.6s each) on the turn's critical path. Here each message
      is still SERIALIZED synchronously when it is added (so a later in-memory edit can never
      leak into what is stored), buffered, and sent by one background writer in order; the SDK's
      end-of-invocation flush and :meth:`finish_writes` (called at turn end) drain the rest, and
      the writer's executor drains on interpreter exit. Two SDK flush defects are corrected on
      this path: a flush cleared the WHOLE buffer after sending a snapshot of it (dropping
      anything added mid-flush), and a batched agent-state event carried every intermediate
      state while ``read_agent`` restores ``payload[0]`` -- the OLDEST. Only the latest state per
      agent is sent.
    """

    #: Class defaults so instances built without __init__ (tests) still work.
    ephemeral: bool = False
    turn_context: str = ""
    _prefetch: Any = None
    async_writes: bool = False
    _write_pool: Any = None

    def __init__(self, *args: Any, ephemeral: bool = False, async_writes: bool = False,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        import threading

        self.ephemeral = bool(ephemeral)
        self.turn_context = ""
        self._prefetch = None
        self.async_writes = bool(async_writes) and int(getattr(self.config, "batch_size", 1) or 1) > 1
        self._flush_lock = threading.RLock()
        self._kick_lock = threading.Lock()
        self._kick_pending = False
        self._write_pool = None

    # -- asynchronous, ordered history writes ---------------------------------------------------

    def create_message(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        result = super().create_message(*args, **kwargs)
        self._kick()
        return result

    def create_agent(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        super().create_agent(*args, **kwargs)
        self._kick()

    def _kick(self) -> None:
        """Schedule one background flush (coalesced: at most one queued at a time)."""
        if not self.async_writes:
            return
        with self._kick_lock:
            if self._kick_pending:
                return
            self._kick_pending = True
            if self._write_pool is None:
                from concurrent.futures import ThreadPoolExecutor

                self._write_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-writes")
            pool = self._write_pool
        pool.submit(self._background_flush)

    def _background_flush(self) -> None:
        with self._kick_lock:
            self._kick_pending = False
        try:
            self._flush_messages()
        except Exception:  # noqa: BLE001 - kept buffered; retried by the next flush / turn end
            logger.warning("memory_context: background history write failed; will retry", exc_info=True)

    def finish_writes(self) -> None:
        """Drain every buffered write now (turn end). Raises the SDK's SessionException on failure."""
        if self._write_pool is not None:
            with self._kick_lock:
                pool, self._write_pool = self._write_pool, None
            pool.shutdown(wait=True)
        self._flush_messages()

    def _flush_messages_only(self) -> list:  # type: ignore[override]
        if not self.async_writes:
            return super()._flush_messages_only()
        import json as _json

        with self._flush_lock:
            with self._message_lock:
                pending = list(self._message_buffer)
            if not pending:
                return []
            groups: dict[str, dict[str, Any]] = {}
            for msg in pending:
                g = groups.setdefault(msg.session_id, {"payload": [], "timestamp": msg.timestamp, "metadata": {}})
                if msg.is_blob:
                    for m in msg.messages:
                        g["payload"].append({"blob": _json.dumps(m)})
                else:
                    for text, role in msg.messages:
                        g["payload"].append({"conversational": {"content": {"text": text}, "role": role.upper()}})
                if msg.timestamp > g["timestamp"]:
                    g["timestamp"] = msg.timestamp
                if msg.metadata:
                    g["metadata"].update(msg.metadata)
            results = []
            try:
                for sid, g in groups.items():
                    kwargs: dict[str, Any] = {
                        "memoryId": self.config.memory_id,
                        "actorId": self.config.actor_id,
                        "sessionId": sid,
                        "payload": g["payload"],
                        "eventTimestamp": g["timestamp"],
                    }
                    if g["metadata"]:
                        kwargs["metadata"] = g["metadata"]
                    results.append(self.memory_client.gmdp_client.create_event(**kwargs))
            except Exception as e:
                from bedrock_agentcore.memory.integrations.strands.session_manager import SessionException

                logger.error("Failed to flush messages to AgentCore Memory: %s", e)
                raise SessionException(f"Failed to flush messages: {e}") from e
            with self._message_lock:
                # Remove exactly what was sent; anything appended meanwhile stays queued.
                del self._message_buffer[:len(pending)]
            return results

    def _flush_agent_states_only(self) -> list:  # type: ignore[override]
        if not self.async_writes:
            return super()._flush_agent_states_only()
        import json as _json

        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AGENT_ID_KEY,
            STATE_TYPE_KEY,
            SessionException,
            StateType,
        )

        with self._flush_lock:
            with self._agent_state_lock:
                pending = list(self._agent_state_buffer)
            if not pending:
                return []
            latest: dict[str, Any] = {}
            for _sid, session_agent in pending:
                latest[session_agent.agent_id] = session_agent  # later states supersede earlier ones
            results = []
            try:
                for agent_id, session_agent in latest.items():
                    results.append(self.memory_client.gmdp_client.create_event(
                        memoryId=self.config.memory_id,
                        actorId=self.config.actor_id,
                        sessionId=self.config.session_id,
                        payload=[{"blob": _json.dumps(session_agent.to_dict())}],
                        eventTimestamp=self._get_monotonic_timestamp(),
                        metadata={
                            STATE_TYPE_KEY: {"stringValue": StateType.AGENT.value},
                            AGENT_ID_KEY: {"stringValue": agent_id},
                        },
                    ))
            except Exception as e:
                logger.error("Failed to flush agent states to AgentCore Memory: %s", e)
                raise SessionException(f"Failed to flush agent states: {e}") from e
            with self._agent_state_lock:
                del self._agent_state_buffer[:len(pending)]
            return results

    # -- prefetch -------------------------------------------------------------------------------

    def prefetch(self, query: str) -> None:
        """Run the SDK retrieval for ``query`` now, on a worker thread. Never raises.

        :meth:`retrieve_customer_context` uses the result only when the turn's message text is
        exactly ``query``; otherwise (or if the prefetch failed) it retrieves live, as before.
        """
        if not isinstance(query, str) or not query or not getattr(self.config, "retrieval_config", None):
            return
        try:
            import threading
            from types import SimpleNamespace

            holder: dict = {"query": query, "done": threading.Event(), "block": None, "ok": False}

            def _run() -> None:
                try:
                    msgs = [{"role": "user", "content": [{"text": query}]}]
                    probe = SimpleNamespace(agent=SimpleNamespace(messages=msgs))
                    AgentCoreMemorySessionManager.retrieve_customer_context(self, probe)
                    content = msgs[-1]["content"]
                    holder["block"] = content[0] if len(content) == 2 else None
                    holder["ok"] = True
                except Exception:  # noqa: BLE001 - live retrieval is the fallback
                    logger.debug("memory_context: retrieval prefetch failed", exc_info=True)
                finally:
                    holder["done"].set()

            threading.Thread(target=_run, name="ltm-prefetch", daemon=True).start()
            self._prefetch = holder
        except Exception:  # noqa: BLE001
            self._prefetch = None

    def _sdk_retrieve(self, event: Any) -> Any:
        """``super().retrieve_customer_context``, served from a matching prefetch when there is one."""
        holder, self._prefetch = self._prefetch, None
        if holder is not None:
            try:
                msgs = event.agent.messages
                last = msgs[-1] if msgs else None
                content = last.get("content") if isinstance(last, dict) else None
                is_user_text = (
                    isinstance(last, dict) and last.get("role") == "user" and content
                    and isinstance(content[0], dict) and "toolResult" not in content[0]
                )
                if is_user_text and content[0].get("text") == holder["query"]:
                    holder["done"].wait(timeout=_PREFETCH_WAIT_S)
                    if holder["done"].is_set() and holder["ok"]:
                        if holder["block"] is not None:
                            content.insert(0, holder["block"])
                        return None
            except Exception:  # noqa: BLE001 - fall through to a live retrieval
                logger.debug("memory_context: prefetch result unusable", exc_info=True)
        return super().retrieve_customer_context(event)

    # -- retrieval hook -------------------------------------------------------------------------

    def retrieve_customer_context(self, event: Any) -> None:  # type: ignore[override]
        result = self._retrieve_framed(event)
        if self.ephemeral:
            self._move_to_turn_context(event)
        return result

    def _move_to_turn_context(self, event: Any) -> None:
        """Take the framed block out of the message and keep it for the per-call injector."""
        try:
            content = event.agent.messages[-1]["content"]
            block = content[0] if content else None
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str) and text.startswith(REMEMBERED_CONTEXT_OPEN) and len(content) > 1:
                content.pop(0)
                self.turn_context = text
        except Exception:  # noqa: BLE001 - leaving it in the message is the safe fallback
            logger.warning("memory_context: could not move context out of the message", exc_info=True)

    def _retrieve_framed(self, event: Any) -> None:
        before_len = None
        before_first = None
        try:
            content = event.agent.messages[-1]["content"]
            before_len = len(content)
            before_first = content[0] if content else None
        except Exception:  # noqa: BLE001 - let super() decide what this event is
            pass

        result = self._sdk_retrieve(event)

        if before_len is None:
            return result
        try:
            content = event.agent.messages[-1]["content"]
            if len(content) != before_len + 1 or content[0] is before_first:
                return result  # nothing was injected
            block = content[0]
            tag = getattr(getattr(self, "config", None), "context_tag", None) or "user_context"
            records = _unwrap(block.get("text") if isinstance(block, dict) else None, tag)
            if records is None:
                return result  # unexpected shape: keep the SDK's block as-is
            content[0] = {**block, "text": frame_remembered_context(records)}
        except Exception:  # noqa: BLE001 - framing must never break a turn
            logger.warning("memory_context: could not frame retrieved context; keeping SDK block",
                           exc_info=True)
        return result
