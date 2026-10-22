/**
 * JobReference — Inline chat-thread chip for an Async_Job (task 13.2).
 *
 * Rendered by the chat message renderer when a tool call response
 * carries a non-empty string `job_id` AND the call belongs to a
 * `Job_Trio` surfaced by `GET /catalog`. The chip is the chat-side
 * counterpart to the Sidebar Jobs section (task 13.1) — both surfaces
 * read the same `useJobs` store, so a single registry mutation flows
 * through DDB Streams → SSE → store → both surfaces in one push.
 *
 * Visibility predicate (R7.1)
 * ---------------------------
 * The chip is intentionally session-scoped — it only renders when the
 * message's session id matches the job's `sessionId`. The sidebar is
 * Actor-scoped; the chat chip is session-scoped. This is the
 * documented divergence in design.md: the registration message is
 * part of the spawning session's transcript and is never replicated,
 * so other sessions for the same Actor see the job in the sidebar
 * but not as an inline chip in their threads.
 *
 * Render shape (R7.1, R7.3)
 * -------------------------
 * Compact chip: `⏳ stem_separation · …abc12345 · RUNNING`
 *
 *   - Status emoji (terminal jobs render the completion indicator
 *     per R7.3 — same indicator set as the sidebar in task 13.1).
 *   - `trioId` (the human-friendly group key from the Job_Detector).
 *   - Truncated `job_id` so the chip stays narrow on long ARNs;
 *     the full id lives in `title` / `aria-label` for accessibility.
 *   - Current `status` value.
 *
 * Click handling (task 13.3 wires this through)
 * ---------------------------------------------
 * The chip is clickable. Task 13.2 stops at exposing an
 * `onActivate?` prop and forwarding `onClick` to it; task 13.3 will
 * plumb that into `SessionSidebar.scrollToJob(jobId)` so the matching
 * sidebar row scrolls into view and receives the highlight styling
 * (R7.2). Until 13.3 lands, omitting the prop is a no-op click — the
 * chip still renders correctly.
 *
 * Integration point (deferred; documented for task 13.3)
 * -------------------------------------------------------
 * The chat message renderer in `ui/src/components/ChatCanvas.tsx` is
 * NOT wired up by this task — the renderer doesn't currently surface
 * tool-call responses or the `/catalog` trio set. When task 13.3
 * (or a follow-up) integrates the chip, the renderer should:
 *
 *   1. Inspect each assistant tool-call response for a top-level
 *      non-empty string `job_id`.
 *   2. Look up the call's tool name in the `GET /catalog` trio set
 *      to confirm the call belongs to a `start_*` trio member.
 *   3. Render `<JobReference jobId={...} trioId={trio.trio_id}
 *      messageSessionId={message.sessionId}
 *      onActivate={() => sidebarRef.current?.scrollToJob(jobId)} />`.
 *
 * Subscribing to the `useJobs` store
 * ----------------------------------
 * The chip subscribes to `jobsStore` directly via `useSyncExternalStore`
 * and slices a single `Job` by `jobId` from `state.jobsById`. Going
 * through the lower-level store (rather than `useJobs()`) avoids the
 * cost of running the visible-list filter just to look up one record.
 * Both code paths read the same underlying data, so the chip stays in
 * sync with the sidebar through the same SSE-driven mutation.
 *
 * Failure modes (return `null`)
 * -----------------------------
 * The chip is purely additive — if any precondition fails, it renders
 * nothing and the chat message is otherwise unchanged. Specifically:
 *
 *   - `jobId` empty / missing → return `null` (no record to show).
 *   - Job not in the store → return `null` (the registry write may
 *     not have surfaced yet, or `GET /jobs` hasn't seeded the job;
 *     the chip will appear on the next store update).
 *   - `job.sessionId !== messageSessionId` → return `null` (out of
 *     visibility scope per R7.1).
 *
 * R7.4 ("IF the Inline_UI fails to render the reference for an
 * Async_Job, THEN the start tool invocation SHALL still complete") is
 * satisfied trivially: the chip is rendered downstream of the start
 * tool response, and a `null` return doesn't propagate any error.
 */

import { useMemo, useSyncExternalStore } from "react";
import type { CSSProperties } from "react";
import { jobsStore } from "../../hooks/useJobs";
import { theme } from '../../styles/tokens';
import type { Job, JobStatus } from "../../types/jobs";

/**
 * Status indicator emoji set. Coordinated with task 13.1's sidebar
 * rendering — both surfaces should display the same indicator for a
 * given status so a job's visual identity is stable across the
 * sidebar and chat chip (R7.3 / Property 14).
 *
 * Exported so task 13.1's sidebar implementation can import this map
 * directly when it lands; that keeps the indicator set in one place
 * rather than duplicating it across two components.
 */
export const STATUS_EMOJI: Record<JobStatus, string> = {
  PENDING: "⏳",
  RUNNING: "⏳",
  SUCCEEDED: "✅",
  FAILED: "❌",
  TIMED_OUT: "⌛",
  ABORTED: "🛑",
};

/**
 * How many trailing characters of the `job_id` to show on the chip.
 * SFN execution ARNs end with the user-supplied execution name (the
 * recognizable part); showing the last 8 characters keeps the chip
 * narrow while still making distinct jobs visually distinguishable.
 */
const JOB_ID_TAIL_CHARS = 8;

export interface JobReferenceProps {
  /**
   * The `job_id` returned by the start tool. Looked up against the
   * `useJobs` store to fetch the full `Job` record.
   */
  jobId: string;

  /**
   * `trio_id` from the `Job_Detector` catalog entry that the start
   * tool belongs to. Displayed verbatim on the chip. Always passed in
   * by the renderer (rather than read from `job.trioId`) so the chip
   * can render its label even on the brief window between the start
   * tool returning and the SSE delivering the registry record.
   */
  trioId: string;

  /**
   * The chat message's session id. The chip renders only when this
   * matches the underlying `Job.sessionId` (R7.1 — visibility scope).
   */
  messageSessionId: string;

  /**
   * Optional click handler — task 13.3 wires this to the sidebar's
   * `scrollToJob(jobId)` imperative handle. When omitted (e.g.
   * during task 13.2's standalone unit-test path), clicks are no-ops.
   */
  onActivate?: () => void;
}

/**
 * Truncate a job id for display, prepending an ellipsis when the id
 * is longer than the configured tail. Pure helper, exported for tests.
 */
export function truncateJobId(
  jobId: string,
  tail: number = JOB_ID_TAIL_CHARS,
): string {
  if (jobId.length <= tail) return jobId;
  return `…${jobId.slice(-tail)}`;
}

/**
 * Pure visibility predicate. Exported so component tests (task 13.7)
 * can assert the predicate directly without reaching into the store.
 *
 * Returns `true` iff the chip should render: the `Job` exists, its
 * `sessionId` matches the message's session, and the `jobId` lookup
 * key is a non-empty string.
 */
export function isJobReferenceVisible(
  jobId: string,
  job: Job | undefined,
  messageSessionId: string,
): boolean {
  if (!jobId) return false;
  if (!job) return false;
  if (job.sessionId !== messageSessionId) return false;
  return true;
}

export default function JobReference({
  jobId,
  trioId,
  messageSessionId,
  onActivate,
}: JobReferenceProps): React.ReactElement | null {
  // Subscribe to the same module-level store the sidebar reads from.
  // Going through `jobsStore.subscribe` / `getSnapshot` directly (rather
  // than `useJobs()`) skips the visible-list filter — we only need a
  // single record by id.
  const snapshot = useSyncExternalStore(
    jobsStore.subscribe,
    jobsStore.getSnapshot,
    jobsStore.getSnapshot,
  );

  // Memoize the lookup so a snapshot change unrelated to this jobId
  // (e.g. a different actor's job arriving via SSE) doesn't re-run
  // any render-side computation downstream.
  const job = useMemo<Job | undefined>(
    () => (jobId ? snapshot.jobsById.get(jobId) : undefined),
    [snapshot.jobsById, jobId],
  );

  if (!isJobReferenceVisible(jobId, job, messageSessionId)) return null;

  // Type-narrow: `isJobReferenceVisible` already verified `job` is
  // defined when it returned true, but TS can't infer that across the
  // helper boundary, so the early return below the predicate is here.
  if (!job) return null;

  const emoji = STATUS_EMOJI[job.status];
  const shortId = truncateJobId(jobId);
  const accessibleLabel = `Job ${jobId} (${trioId}) — ${job.status}`;

  // Style notes
  // -----------
  // Inline-flex chip: small, single-line, doesn't wrap mid-text. The
  // background uses `accentSubtle` from the theme (the same family the
  // sidebar's active row borrows from) so the chip reads as
  // "interactive but not loud" inside a chat bubble.
  return (
    <button
      type="button"
      onClick={onActivate}
      title={accessibleLabel}
      aria-label={accessibleLabel}
      data-testid="job-reference"
      data-job-id={jobId}
      data-trio-id={trioId}
      data-status={job.status}
      style={chipStyle}
    >
      <span style={emojiStyle} aria-hidden="true">
        {emoji}
      </span>
      <span style={trioStyle}>{trioId}</span>
      <span style={separatorStyle} aria-hidden="true">
        ·
      </span>
      <span style={jobIdStyle}>{shortId}</span>
      <span style={separatorStyle} aria-hidden="true">
        ·
      </span>
      <span style={statusStyle}>{job.status}</span>
    </button>
  );
}

// --- Styles ---------------------------------------------------------------

const chipStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  padding: "2px 8px",
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.border}`,
  background: theme.colors.accentSubtle,
  color: theme.colors.textPrimary,
  fontSize: 'var(--fs-xs)',
  fontFamily: theme.typography.fontFamily,
  lineHeight: 1.5,
  cursor: "pointer",
  // Inherit the message's vertical rhythm without forcing block-level
  // layout — the chip is meant to flow inline with surrounding text.
  verticalAlign: "baseline",
};

const emojiStyle: CSSProperties = {
  // Emojis render slightly larger than 12px text in most fonts; pin
  // line-height to 1 so they don't push the chip taller than the
  // surrounding inline text.
  lineHeight: 1,
};

const trioStyle: CSSProperties = {
  fontWeight: theme.typography.weights.medium,
};

const jobIdStyle: CSSProperties = {
  fontFamily: theme.typography.fontMono,
  color: theme.colors.textSecondary,
};

const statusStyle: CSSProperties = {
  fontWeight: theme.typography.weights.medium,
  color: theme.colors.textSecondary,
};

const separatorStyle: CSSProperties = {
  color: theme.colors.textSecondary,
  opacity: 0.6,
};
