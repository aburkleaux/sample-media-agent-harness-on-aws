/**
 * renderTabs — build and lazily fill the Renderer panel's result tabs.
 *
 * A finished job (or each node of a finished DAG) becomes one tab. Tabs are
 * created immediately from job metadata, which is cheap; the manifest is
 * composed by the Render Agent only when a tab is first opened.
 *
 * That laziness is deliberate. Rendering every completed job up front fired the
 * Render Agent for results the user never asked to see (~70-95s of compute each)
 * and measurably slowed dispatch. Creating a tab costs nothing.
 */
import { authFetch } from '../auth/authFetch';
import type { Job } from '../types/jobs';
import type { RenderHistoryEntry } from '../components/renderer/types';

/** Node-level jobs are bookkeeping rows; the real work carries the artifacts. */
function isNodeRow(job: Job): boolean {
  return (job.trioId || '').startsWith('node|');
}

function isPlanParent(job: Job): boolean {
  return (job.trioId || '').startsWith('plan|') || (job.runId != null && job.runId === job.jobId);
}

/**
 * Human label for a tab.
 *
 * Uses the TASK name, not the plan's internal node id: a node id like `sg1_trim`
 * or `sg2_stems` is planner bookkeeping and means nothing to the person reading
 * the tab strip. The tool name reads as a task ("Separate Audio Stems",
 * "Recognize Faces"). The node id is only a last resort when there is no trioId.
 */
export function tabLabel(job: Job): string {
  const trio = job.trioId || '';
  const titleize = (raw: string) =>
    raw.replace(/[-_]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()).trim();
  const tool = trio.split('|')[1] || '';
  if (tool) return titleize(tool);
  const gateway = trio.split('|')[0] || '';
  if (gateway) return titleize(gateway);
  return job.nodeId || 'Result';
}

/**
 * Build the unrendered tabs for a completed job.
 *
 * For a plan parent this returns one tab per child that actually produced
 * artifacts (skipping `node|` bookkeeping rows and the parent itself), so a
 * 4-node DAG yields up to 4 tabs. For a standalone job it returns a single tab.
 */
export function buildResultTabs(job: Job, allJobs: readonly Job[]): RenderHistoryEntry[] {
  const now = Date.now();
  const make = (j: Job): RenderHistoryEntry | null => {
    const urls = (j.artifactUrls ?? []).filter(Boolean);
    if (urls.length === 0) return null;
    return {
      turnIndex: 0,
      timestamp: now,
      label: tabLabel(j),   // task name, e.g. "Separate Audio Stems"
      jobId: j.jobId,
      artifactUrls: urls,
      blocks: [],           // filled on first open
    };
  };

  if (!isPlanParent(job)) {
    const one = make(job);
    return one ? [one] : [];
  }
  const tabs = allJobs
    .filter((j) => j.runId === job.jobId && j.jobId !== job.jobId && !isNodeRow(j) && !isPlanParent(j))
    .map(make)
    .filter((e): e is RenderHistoryEntry => e !== null);

  // A plan can run the same tool twice (e.g. two process_media nodes), which
  // would otherwise produce two tabs with the identical task name and no way to
  // tell them apart. Number the repeats.
  const counts = new Map<string, number>();
  for (const t of tabs) counts.set(t.label!, (counts.get(t.label!) ?? 0) + 1);
  const seen = new Map<string, number>();
  for (const t of tabs) {
    if ((counts.get(t.label!) ?? 0) < 2) continue;
    const n = (seen.get(t.label!) ?? 0) + 1;
    seen.set(t.label!, n);
    t.label = `${t.label} ${n}`;
  }
  return tabs;
}

/**
 * Compose a manifest for one tab's artifacts. Returns the same-origin manifest
 * URL, or null on failure.
 *
 * All urls belong to a SINGLE node, so combining them is safe (e.g. 4 stems of
 * equal length on one timeline). Artifacts are never combined across nodes, and
 * no `sourceUrl` is sent — that forces the Render Agent's annotation branch,
 * which drops every non-source track.
 */
/**
 * What composing a tab produced. `no_view` (a deliberate decline, e.g. data-only JSON) and `failed` (the render
 * agent returned only an error record, HTTP 422) are DEFINITIVE: the tab has nothing to show and is removed.
 * `unavailable` is a transport failure (network, 5xx): the tab stays so the user can retry.
 */
export type RenderTabOutcome =
  | { kind: 'manifest'; manifestUrl: string }
  | { kind: 'no_view' }
  | { kind: 'failed' }
  | { kind: 'unavailable' };

export async function renderTabOutcome(urls: readonly string[], sessionId?: string): Promise<RenderTabOutcome> {
  const list = urls.filter(Boolean);
  if (list.length === 0) return { kind: 'no_view' };
  try {
    const body: Record<string, unknown> = list.length === 1 ? { url: list[0] } : { urls: list };
    if (sessionId && sessionId.length >= 33) body.session_id = sessionId;
    const res = await authFetch('/render', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (res.status === 422) return { kind: 'failed' };
    if (!res.ok) return { kind: 'unavailable' };
    const data = (await res.json()) as { manifestUrl?: string; noView?: boolean };
    if (data?.noView) return { kind: 'no_view' };
    return data?.manifestUrl ? { kind: 'manifest', manifestUrl: data.manifestUrl } : { kind: 'failed' };
  } catch {
    return { kind: 'unavailable' };
  }
}

export async function renderTab(urls: readonly string[], sessionId?: string): Promise<string | null> {
  const list = urls.filter(Boolean);
  if (list.length === 0) return null;
  try {
    const body: Record<string, unknown> = list.length === 1 ? { url: list[0] } : { urls: list };
    // Scope the render to the CURRENT session so its manifest is written under
    // artifacts/<sessionId>/render/ (what GET /sessions/:id/renders lists) instead of a random
    // render-<ts> dir. Without this the uploaded SOURCE video's manifest lands in an orphan
    // session, so analyses of the original (which ARE session-scoped, poller-written) have no
    // sibling to merge with and spawn a solo tab (session 6dffd3ec). The /render handler only
    // honours a session_id of >=33 chars, so pass it through untouched (session ids are 36).
    if (sessionId && sessionId.length >= 33) body.session_id = sessionId;
    const res = await authFetch('/render', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { manifestUrl?: string };
    return data?.manifestUrl ?? null;
  } catch {
    return null;
  }
}

/** Wrap a composed manifest URL into the block the renderer consumes. */
export function manifestBlock(manifestUrl: string, title: string) {
  return { template: 'manifest-router', title, data: { manifestUrl } };
}
