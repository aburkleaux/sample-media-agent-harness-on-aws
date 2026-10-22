/**
 * autoRenderOnComplete — implicit "render on job completion" trigger.
 *
 * Design (agreed model): rendering has two paths.
 *   - EXPLICIT: the user asks ("show my results") → the main agent fetches +
 *     the deterministic `render-artifacts` hook renders. Already works.
 *   - IMPLICIT (this module): while the app is open, when a job finishes
 *     (`job.terminal` / SUCCEEDED over `/jobs/stream`), the browser fires the
 *     same turn *for us*, aimed at the session the job originated from.
 *
 * Because a job carries its originating `sessionId`, we can run the turn and
 * persist both the conversation turn and the render into THAT session's
 * stores — even if the user is currently viewing a different session. The
 * conversation itself is also persisted server-side in AgentCore Memory by the
 * agent runtime; we mirror it into the local caches (`lab-session-<sid>` and
 * `lab-render-<sid>` + the DynamoDB session row) so it shows immediately when
 * the user opens that session, and dispatch events so an already-open view can
 * refresh.
 *
 * Guardrails:
 *   - Fires only from live `job.terminal` SSE frames (not the mount-time
 *     backlog seed, which goes through `jobsStore.refresh`, not this path).
 *   - Once-per-job: a persisted guard set prevents re-firing on SSE redelivery
 *     or reconnect resync.
 */

import AgentService from './agent-service';
import { updateSessionRenderHistory } from '../lab-api';
import type { RenderHistoryEntry } from '../components/renderer/types';
import type { Job } from '../types/jobs';

const GUARD_KEY = 'lab-autorendered-jobs';
const MAX_GUARD = 200;

// The polling worker inspects each completed status payload and persists this
// capability on the job row. The UI therefore needs no knowledge of gateway
// ids or tool names; new artifact-producing jobs participate automatically.
/** Exported for unit testing — pure, no side effects. */
export function shouldAutoRender(job: Pick<Job, 'hasRenderableArtifacts'>): boolean {
  return job.hasRenderableArtifacts === true;
}

function readGuard(): Set<string> {
  try {
    const raw = localStorage.getItem(GUARD_KEY);
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set();
  }
}

function writeGuard(set: Set<string>): void {
  try {
    const arr = Array.from(set).slice(-MAX_GUARD);
    localStorage.setItem(GUARD_KEY, JSON.stringify(arr));
  } catch {
    /* quota — ignore */
  }
}

function persistRenders(sid: string, entries: RenderHistoryEntry[]): void {
  if (entries.length === 0) return;
  let merged: RenderHistoryEntry[] = entries;
  try {
    const raw = localStorage.getItem(`lab-render-${sid}`);
    const existing: RenderHistoryEntry[] = raw ? JSON.parse(raw) : [];
    merged = [...existing, ...entries].slice(-50);
    localStorage.setItem(`lab-render-${sid}`, JSON.stringify(merged));
  } catch {
    /* quota — still push to DDB below with what we have */
  }
  // Server-side persistence so it survives reloads / other devices.
  updateSessionRenderHistory(sid, merged).catch(() => {});
}

/**
 * Render a completed job's results into its session WITHOUT injecting a
 * synthetic chat turn — the click-to-view path behind the completion banner /
 * notification bell.
 *
 * Unlike `maybeAutoRenderJob` (the legacy auto-inject path), this never writes
 * user/assistant messages into the transcript. It runs the deterministic
 * results-fetch (Phase 1: a silent agent turn; Phase 2 will replace this with a
 * no-LLM server fetch) purely to drive the `render-artifacts` hook, then
 * persists only the render block into the session's render history so the
 * Renderer panel shows it. Fire-and-forget; never throws.
 */
export async function renderJobResults(job: Pick<Job, 'jobId' | 'sessionId'>): Promise<void> {
  const sid = job?.sessionId;
  if (!job?.jobId || !sid) return;

  const agentPrompt = `The job with id ${job.jobId} just completed. Fetch its results and feature the artifacts for me.`;
  const renders: RenderHistoryEntry[] = [];
  try {
    await AgentService.chat(agentPrompt, {
      sessionId: sid,
      onRenderBlock: (entry) => { renders.push(entry); },
    });
  } catch (err) {
    console.warn('[renderJobResults] fetch/render failed for job', job.jobId, err);
    return;
  }
  // Render-only: persist the render into the session (localStorage + DDB) and
  // nudge any open view to refresh. No synthetic chat messages are written.
  persistRenders(sid, renders);
  window.dispatchEvent(new CustomEvent('session-render-updated', { detail: { sessionId: sid } }));
}

/**
 * On a fresh SUCCEEDED completion, render the job's results into its session's
 * Renderer panel — RENDER-ONLY, no synthetic chat turn. Fire-and-forget.
 *
 * Rationale (agreed UX): auto-appending a request+summary to the conversation —
 * especially a live turn while the user is mid-thought after a long job — is
 * surprising. Results belong in the output panel (which populates on its own),
 * with a passive completion notification; a chat summary is now an explicit ask.
 * This mirrors how CLI agents surface async/background work: notify, don't
 * interrupt the transcript. (Phase 2 will make the fetch a no-LLM server call so
 * it leaves no memory turn either; today it still runs one silent fetch turn but
 * writes nothing to the visible transcript.)
 */
export async function maybeAutoRenderJob(job: Job): Promise<void> {
  if (!job || job.status !== 'SUCCEEDED') return;
  // The poller flags user-facing artifacts on the job row; skip when there are none.
  if (!shouldAutoRender(job)) return;
  // Plan-run nodes render per-node via the render hook AS each node succeeds and
  // are tracked in the Activity panel — don't auto-render them here.
  if (job.runId) return;
  const sid = job.sessionId;
  if (!sid) return;

  const guard = readGuard();
  if (guard.has(job.jobId)) return;
  // Mark BEFORE firing so SSE redelivery / reconnect resync can't double-fire.
  guard.add(job.jobId);
  writeGuard(guard);

  try {
    await renderJobResults(job);
  } catch (err) {
    // Un-guard so a fresh SUCCEEDED frame (or an explicit ask) can retry later.
    console.warn('[autoRenderOnComplete] render-only auto-render failed for job', job.jobId, err);
    const g = readGuard();
    g.delete(job.jobId);
    writeGuard(g);
  }
}
