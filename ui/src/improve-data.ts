/**
 * Fetches the Improve payload. The only place the surface gets its data.
 *
 * Served by the always-on Capability API at `GET /control-plane/improve/data`, scoped to
 * the signed-in actor (`authFetch` attaches the token; the API verifies it and reads the
 * shared snapshot plus that actor's own partition — their evidence bundle and their dismiss
 * overlay). Same {suggestions, components, patterns} shape the loop emits — the transport has
 * moved from the old public static `improve-data.json` → the `ui/server` proxy → here, and each
 * move changed only these URLs, not the contract.
 *
 * `/control-plane/*` bypasses `ui/server` entirely (an ALB rule forwards it straight to the
 * Capability API), so unlike the proxy step there is nothing to register in `authPaths`;
 * `authFetch` already protects the `/control-plane` prefix.
 *
 * There is deliberately NO fallback to fabricated data. An earlier version shipped a mock
 * module and badged invented rows "illustrative", and the labels drifted anyway: an advisory
 * suggestion was marked MEASURED while the pipeline emitted one proposal, and a fabricated
 * platform-degraded failure rendered unbadged while the real bucket was empty. A screen that
 * silently substitutes invented data when the real data is missing is worse than one that says
 * it has none, because the failure is invisible. A missing payload is an error with a message.
 *
 * Cached as a PROMISE via `createAsyncCache`, matching `lab-api`: the surface and the sidebar
 * badge both want this and must not race or double-fetch.
 */
import { authFetch } from './auth/authFetch';
import { createAsyncCache } from './utils/asyncCache';
import type {
  ActorBundle, ComponentsResponse, PatternsResponse, PopulationDay, RolledUpDays, Scorecard,
  Suggestion, WasteTotals,
} from './improve-api';

/**
 * Capability API namespace for the Improve surface. Relative + same-origin (no CORS),
 * and `authFetch` attaches the token because `/control-plane` is a protected prefix.
 * Mirrors the `/control-plane/*` convention used by `capability-api.ts`.
 */
const IMPROVE_BASE = '/control-plane/improve';

/** The authenticated, actor-scoped route that serves the bundle. */
export const IMPROVE_DATA_URL = `${IMPROVE_BASE}/data`;

/** Per-suggestion dismiss overlay route. POST records a dismissal, DELETE undoes it. */
function dismissUrl(suggestionId: string): string {
  return `${IMPROVE_BASE}/suggestions/${encodeURIComponent(suggestionId)}/dismiss`;
}

export interface ImproveData {
  suggestions: Suggestion[];
  /**
   * How many suggestions the producer published, before the list above was cut to fit the item's
   * size budget. So `suggestions.length < suggestionsTotal` IS the trim, and it is the only trace
   * of one: the global list has no per-row equivalent of `ActorBundle.runsTotal`.
   *
   * OPTIONAL for the same deploy-order reason as `scorecard` below, and absent means the bundle
   * predates the count — never that nothing was dropped. Equal values mean nothing was, which is
   * why a reader is told only when the two differ.
   */
  suggestionsTotal?: number;
  components: ComponentsResponse;
  patterns: PatternsResponse;
  /**
   * Population-level aggregates. OPTIONAL, and it must stay optional: the producer is a
   * scheduled Lambda and this SPA is a separate artifact, so the two deploy independently and in
   * either order. A UI build that shipped first would otherwise be typed to a key the served
   * SNAPSHOT does not have yet, and every reader would be written as though it were there.
   *
   * So branch on `undefined` rather than reaching through it — absent ≠ empty (`AGENTS.md`).
   * `undefined` means "this bundle predates the scorecard"; a scorecard whose `population.total`
   * is 0 means "the producer ran and scored nothing", which is a different fact worth showing.
   */
  scorecard?: Scorecard;
  /**
   * The efficiency path's population totals. OPTIONAL for the same deploy-order reason as
   * `scorecard`, and with the same rule: branch on `undefined` rather than reaching through it.
   *
   * `undefined` means "this bundle predates the efficiency path". `{runsWithWaste: 0, runs: 174}`
   * means the producer looked at 174 runs and found nothing, which is a different fact and worth
   * showing.
   *
   * Read by the outcome band and the impact table on the default view — the GLOBAL key, which is why
   * they have a figure for every reader. `ActorBundle.waste` is the same shape over one actor's runs
   * and is a different number, not a slice of this one; it is what the per-actor block behind
   * Advanced renders, and it is absent for most readers most days.
   *
   * What this still does NOT carry is a recommendation. Turning "this ran twice" into "change X" needs
   * an attribution the efficiency path deliberately does not make, so the surface reports the
   * measurement and attributes it to nobody.
   */
  waste?: WasteTotals;
  /**
   * The signed-in actor's OWN evidence: their run rows with the judge's reason, the facet detail
   * behind the suggestions their runs support, their waste findings with the tool sequence named,
   * their session ids. A second DynamoDB item on their own partition, attached by the API from the
   * verified token and never from a request parameter.
   *
   * OPTIONAL, and absent is the NORMAL state rather than an edge case. The producer is a daily cron
   * over a bounded harvest window, so for up to a day after any deploy nobody has one, and an actor
   * with no conversational session never has one at all. Everything above is a complete answer
   * without it — branch on `undefined` and degrade visibly, never blank a surface on it.
   */
  actor?: ActorBundle;
  /**
   * One row per day for the WHOLE population, oldest first — the shared counterpart to
   * `ActorBundle.history`.
   *
   * A top-level bundle key rather than part of `scorecard`, because that is where the producer puts it:
   * the read route serves `snapshot["bundle"]` and nothing else, so the history has to live inside the
   * bundle to reach a consumer at all, and it predates the reconciliation with the per-actor rows.
   *
   * OPTIONAL, with the same rule as everything else here: branch on `undefined` rather than reaching
   * through it. Absent means this bundle predates the day rows; `[]` would mean the producer wrote one
   * and had no day to record, which is a different fact.
   */
  history?: PopulationDay[];
  /** The population's evicted days as one bucket, so a shared all-time total does not quietly shrink
   *  to whatever retention still reaches. Spelled as on the per-actor bundle, and absent until the
   *  producer has evicted anything. */
  rolledUp?: RolledUpDays;
  /** `YYYY-MM-DD` of the earliest day the population history covers, the bucket included. What the
   *  basis label prints for a shared all-time total. */
  historyFirstDate?: string;
}

/** 10 minutes: the pipeline is regenerated by hand, so a shorter TTL only adds requests. */
const cache = createAsyncCache<ImproveData>(10 * 60_000);

async function fetchImproveData(): Promise<ImproveData> {
  const response = await authFetch(IMPROVE_DATA_URL, { headers: { accept: 'application/json' } });

  if (!response.ok) {
    throw new Error(
      `No agent report (${response.status}). The pipeline has not written a bundle for `
      + 'this account yet, or the report store is not configured.');
  }

  // An unlisted path falls through to the SPA bucket and returns index.html with a 200, so a
  // response that parses as HTML means the `/control-plane/*` ALB rule / CloudFront behavior
  // is not routing to the Capability API yet (e.g. the service is not up in this env, or a
  // change is still propagating). Without this check the JSON parse fails on '<' and the error
  // reads as corruption instead of absence.
  const contentType = response.headers.get('content-type') ?? '';
  if (!contentType.includes('json')) {
    throw new Error(
      'No agent report: the request returned HTML, which means it fell through to the SPA '
      + '— the Capability API is not serving /control-plane/improve yet.');
  }

  const payload = await response.json() as Partial<ImproveData>;
  // The ORIGINAL three keys only. Neither `scorecard` nor `patterns.unattributed` is checked:
  // both are optional on the type for the deploy-order reason above, and rejecting a bundle that
  // lacks one would take the whole surface down for a missing panel. A newer key belongs in the
  // panel that renders it, which can say "this bundle predates it" — not here, where the only
  // available verdict is to fail the whole load.
  if (!payload.suggestions || !payload.components || !payload.patterns) {
    throw new Error('The agent report is missing one of suggestions/components/patterns');
  }
  return payload as ImproveData;
}

export function loadImproveData(force = false): Promise<ImproveData> {
  return cache.get(fetchImproveData, force);
}

/**
 * Record a dismissal (POST) or undo it (DELETE) against the per-actor overlay.
 *
 * The overlay is stored separately from the shared suggestion bundle: the suggestions are the
 * same for everyone, but a dismissal is one person's decision, so it is keyed to the signed-in
 * actor by the Capability API (from the token, never the body). On success the read cache is
 * invalidated, so the next `loadImproveData` re-merges the overlay and the status the server
 * holds becomes the one the surface shows — the optimistic move in the inbox is only a bridge
 * until then.
 */
async function writeDismiss(
  method: 'POST' | 'DELETE', suggestionId: string, body?: { reason?: string; note?: string },
): Promise<void> {
  const init: RequestInit = { method, headers: { accept: 'application/json' } };
  if (method === 'POST') {
    // Absent ≠ empty: send only the keys the caller actually provided, so a blank dismissal is
    // not recorded as a reasoned one. A JSON string body is reusable, which authFetch's 401
    // retry requires.
    const payload: Record<string, string> = {};
    if (body?.reason) payload.reason = body.reason;
    if (body?.note) payload.note = body.note;
    init.headers = { ...init.headers, 'content-type': 'application/json' };
    init.body = JSON.stringify(payload);
  }

  const response = await authFetch(dismissUrl(suggestionId), init);
  if (!response.ok) {
    throw new Error(
      `Could not ${method === 'POST' ? 'dismiss' : 'restore'} this suggestion `
      + `(${response.status}).`);
  }
  // The cached bundle now carries a stale status for this row; drop it so the next load re-reads
  // the overlay rather than serving the pre-write view for up to the TTL.
  cache.invalidate();
}

/** Dismiss a suggestion for the signed-in actor. `reason`/`note` are optional labels. */
export function dismissSuggestion(
  suggestionId: string, reason?: string, note?: string,
): Promise<void> {
  return writeDismiss('POST', suggestionId, { reason, note });
}

/** Undo a dismissal for the signed-in actor. */
export function undoDismiss(suggestionId: string): Promise<void> {
  return writeDismiss('DELETE', suggestionId);
}

/** Synchronous peek for instant paint, `undefined` before the first load resolves. */
export function peekImproveData(): ImproveData | undefined {
  return cache.peek();
}

/**
 * Whether the sidebar should show a dot: is there anything the user can APPLY?
 *
 * Not "is there anything open". Escalation is not improvement, so `canSendToTeam` does not
 * count; including it makes the dot permanent, since it is true on every non-publishable row.
 */
export function hasActionableSuggestions(data: ImproveData | undefined): boolean {
  return (data?.suggestions ?? []).some(
    (s) => s.status === 'open' && (s.affordances.canPublish || s.affordances.canTryBoth));
}
