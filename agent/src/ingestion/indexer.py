"""Index submitter (Requirement 6, Req 7.5–7.9; design Component 11).

The sequencing is one pure state machine, :class:`IndexRun`, so the step-wise ``IngestTick`` Lambda and the
blocking local driver (:func:`run_blocking`) share every rule:

- chunks of 100 in build order, each submitted only after the previous one's job reported ``SUCCEEDED``;
- every chunk carries ``asset_metadata.title`` = the asset's current Display_Name and one asset's documents;
- a chunk whose job FAILED with a cold-start/throttle error and no ``prepared``/``indexed`` is resubmitted
  unchanged at most 2 more times, ≥ 30 s after the failure; it does not restart at chunk 1;
- 3 consecutive failed submit or status calls, ≥ 30 s apart, fail the run; a success in between resets the count;
- any other failure ends the run; nothing is resubmitted automatically.

Collection creation and the rebuild hold are the caller's (``collections.py``, the reducer).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, Sequence

from . import chunking, index_errors, records
from .m2c import M2CError, ToolError, Transient

SUBMIT_TOOL = "hybrid-search___index_content"
STATUS_TOOL = "hybrid-search___get_job_status"
CALL_FAILURES_TO_FAIL = 3
CALL_RETRY_GAP_S = 30


@dataclass(frozen=True)
class Prepared:
    record: str
    digest: str
    refresh: bool
    chunks: tuple[tuple[dict, ...], ...]
    scopes: frozenset


def prepare(docs: Sequence[dict], history: chunking.History) -> Prepared:
    record = records.serialize(docs)
    dg = records.digest(record)
    refresh = chunking.refresh_mode(history, dg)
    parts = tuple(tuple(c) for c in chunking.chunks(list(docs), refresh))
    return Prepared(record, dg, refresh, parts, frozenset(d["doc_type"] for d in docs))


def chunk_arguments(collection_id: str, chunk: Sequence[dict], title: str) -> dict:
    return {"collection_id": collection_id, "documents": list(chunk), "asset_metadata": {"title": title}}


# ------------------------------------------------------------------------------------------ actions
@dataclass(frozen=True)
class Submit:
    chunk: int  # 0-based


@dataclass(frozen=True)
class Poll:
    job_id: str


@dataclass(frozen=True)
class Wait:
    until: float


@dataclass(frozen=True)
class Done:
    prepared: int
    skipped: int
    indexed: int


@dataclass(frozen=True)
class Failed:
    message: str  # the message half of the reason; the step is ``indexing``
    kind: str


@dataclass(frozen=True)
class IndexRun:
    n_chunks: int
    chunk: int = 0
    job_id: Optional[str] = None
    accepted: bool = False          # some index_content call returned a job id (chunking.History.accepted)
    resubmits: int = 0              # cold-start resubmits of the CURRENT chunk
    call_failures: int = 0          # consecutive failed submit/status calls
    not_before: float = 0.0
    totals: tuple[int, int, int] = (0, 0, 0)
    outcome: Optional[Any] = None   # Done | Failed

    # ------------------------------------------------------------------- what next
    def next(self, now: float) -> Any:
        if self.outcome is not None:
            return self.outcome
        if now < self.not_before:
            return Wait(self.not_before)
        return Poll(self.job_id) if self.job_id else Submit(self.chunk)

    # ------------------------------------------------------------- observations
    def submitted(self, job_id: str) -> "IndexRun":
        return replace(self, job_id=job_id, accepted=True, call_failures=0, not_before=0.0)

    def call_failed(self, err: Exception, now: float) -> "IndexRun":
        """A submit or status CALL failed (no answer). Transient → count; anything else ends the run."""
        if isinstance(err, ToolError):
            c = index_errors.classify(err.text, code=err.code)
            return replace(self, outcome=Failed(c.reason_message, c.kind))
        n = self.call_failures + 1
        if not isinstance(err, Transient) or n >= CALL_FAILURES_TO_FAIL:
            return replace(self, call_failures=n, outcome=Failed(str(err), index_errors.OTHER))
        return replace(self, call_failures=n, not_before=now + CALL_RETRY_GAP_S)

    def status(self, st: dict, now: float) -> "IndexRun":
        state = st.get("status")
        ok = replace(self, call_failures=0)
        if state == "SUCCEEDED":
            p, s, i = index_errors.job_counts(st)
            totals = (self.totals[0] + p, self.totals[1] + s, self.totals[2] + i)
            nxt = self.chunk + 1
            if nxt >= self.n_chunks:
                return replace(ok, job_id=None, totals=totals, outcome=Done(*totals))
            return replace(ok, chunk=nxt, job_id=None, resubmits=0, totals=totals)
        failure = index_errors.failure_of(st)
        if failure is None:  # still running
            return ok
        if index_errors.may_resubmit_chunk(failure, self.resubmits):
            return replace(ok, job_id=None, resubmits=self.resubmits + 1,
                           not_before=now + index_errors.RESUBMIT_GAP_S)
        return replace(ok, outcome=Failed(failure.reason_message, failure.kind))


# ---------------------------------------------------------------------------------- blocking driver
def run_blocking(client: Any, collection_id: str, prepared: Prepared, title: str, *,
                 poll_s: float = 10.0, deadline_s: float = 3600.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda _m: None) -> IndexRun:
    """Drive :class:`IndexRun` to an outcome with real calls (local driver, live tests)."""
    run = IndexRun(n_chunks=len(prepared.chunks))
    end = clock() + deadline_s
    while True:
        now = clock()
        act = run.next(now)
        if isinstance(act, (Done, Failed)):
            return run
        if now > end:
            return replace(run, outcome=Failed("timed out after 1 h", index_errors.OTHER))
        if isinstance(act, Wait):
            sleep(max(0.0, act.until - now))
            continue
        try:
            if isinstance(act, Submit):
                res = client.call_tool(SUBMIT_TOOL, chunk_arguments(collection_id, prepared.chunks[act.chunk], title))
                job = res.get("job_id")
                if not job:
                    raise ToolError(f"index_content returned no job id: {str(res)[:300]}")
                log(f"chunk {act.chunk + 1}/{run.n_chunks} submitted ({len(prepared.chunks[act.chunk])} docs)")
                run = run.submitted(job)
            else:
                sleep(poll_s)
                st = client.call_tool(STATUS_TOOL, {"job_id": act.job_id})
                before = run
                run = run.status(st, clock())
                if st.get("status") in ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
                    log(f"chunk {before.chunk + 1}/{run.n_chunks}: {st.get('status')} "
                        f"prepared={st.get('prepared')} skipped={st.get('skipped')} indexed={st.get('indexed')} "
                        f"{st.get('error') or ''}".rstrip())
        except M2CError as exc:
            run = run.call_failed(exc, clock())
