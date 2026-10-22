import { expect, Page, Response } from '@playwright/test';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/**
 * Shared helpers for the Capability Audit UAT specs (Phase 1 of 4).
 *
 * Consolidates the login / SSE-stream-capture / settings-navigation
 * patterns that were previously copy-pasted across `m2c-gateway-tools-uat`,
 * `routing-uat`, `hooks-ui`, and `render-pipeline-uat` so every audit spec
 * (`audit-model`, `audit-skills`, `audit-tools-gateway`, `audit-agents`,
 * `audit-hooks`) drives the live stack the same way.
 *
 * These specs run directly against the deployed staging CloudFront URL and
 * restore a real Cognito PKCE session created by staging-auth.setup.ts.
 * Credentials and the generated session file remain external to source control.
 */

export const BASE = process.env.STAGING_URL ?? 'https://dexamplecdn004.cloudfront.net';
const SESSION_PATH = fileURLToPath(new URL('../test-results/staging-session-storage.json', import.meta.url));

// Newline-delimited log of the sessionIds THIS run creates. The teardown deletes ONLY these,
// so it can never sweep up real user sessions that merely happen to be created during the run.
// The setup truncates it at suite start; `attachSessionRecorder` appends to it.
export const CREATED_SESSIONS_PATH = fileURLToPath(
  new URL('../test-results/staging-created-sessions.log', import.meta.url),
);

/**
 * Record every session this suite creates by intercepting `POST /sessions` (the client posts
 * `{ sessionId, title }` on the first message). Attached in `stagingLogin`, so every audit spec
 * records automatically. This is what lets the teardown delete ONLY our own sessions instead of
 * falling back to a `createdAt >= suiteStartedAt` time window that also deleted concurrent real
 * user sessions. Best-effort: recording must never fail a test.
 */
export function attachSessionRecorder(p: Page): void {
  p.on('request', (req) => {
    try {
      if (req.method() !== 'POST' || !req.url().endsWith('/sessions')) return;
      const data = req.postData();
      if (!data) return;
      const sid = (JSON.parse(data) as { sessionId?: string }).sessionId;
      if (sid) appendFileSync(CREATED_SESSIONS_PATH, `${sid}\n`);
    } catch {
      /* best-effort; never fail a test on recording */
    }
  });
}

// ─── SSE stream capture ───────────────────────────────────────────────────
//
// The agent streams `type: step | execution_summary | <string text>` events
// over `/api/invocations` as `text/event-stream`. We intercept the response
// body and parse each `data:` line so assertions can inspect the step trail
// (dispatch / sub-agent `↳` steps), the execution summary tool-call map, and
// the raw text chunks.

export interface StreamStep {
  status: string;
  label: string;
  agent?: string;
}

export interface StreamCapture {
  steps: StreamStep[];
  texts: string[];
  summary: { tools?: Record<string, { calls: number }> } | null;
  raw: string;
}

export function createStreamCapture(): StreamCapture {
  return { steps: [], texts: [], summary: null, raw: '' };
}

export function attachStreamInterceptor(p: Page, capture: StreamCapture): void {
  p.on('response', async (response: Response) => {
    const url = response.url();
    if (!url.includes('/api/invocations')) return;
    if (!response.headers()['content-type']?.includes('text/event-stream')) return;

    try {
      const body = await response.body();
      const text = body.toString('utf-8');
      capture.raw += text;

      for (const line of text.split('\n')) {
        if (!line.startsWith('data: ')) continue;
        const payload = line.slice(6);
        try {
          let parsed = JSON.parse(payload);
          if (typeof parsed === 'string') {
            try {
              parsed = JSON.parse(parsed);
            } catch {
              capture.texts.push(parsed);
              continue;
            }
          }
          if (parsed?.type === 'step') {
            capture.steps.push({
              status: parsed.status,
              label: parsed.label || '',
              agent: parsed.agent,
            });
          } else if (parsed?.type === 'execution_summary') {
            capture.summary = parsed;
          } else if (typeof parsed === 'string') {
            capture.texts.push(parsed);
          }
        } catch {
          /* not JSON */
        }
      }
    } catch {
      /* response body may not be available */
    }
  });
}

// ─── Auth + session + chat ──────────────────────────────────────────────────

/**
 * Restore the oidc-client-ts session created by the Cognito setup project.
 * Playwright storageState does not preserve sessionStorage, so the state must
 * be injected before the staging origin's first application script runs.
 */
export async function stagingLogin(p: Page): Promise<void> {
  if (!existsSync(SESSION_PATH)) {
    throw new Error(
      `Missing ${SESSION_PATH}. Run the capabilities-staging project with ` +
      'STAGING_EMAIL and STAGING_PASSWORD so its auth dependency can create it.',
    );
  }
  const saved = JSON.parse(readFileSync(SESSION_PATH, 'utf8')) as {
    origin: string;
    entries: [string, string][];
  };
  attachSessionRecorder(p);
  await p.addInitScript(({ origin, entries }) => {
    if (window.location.origin !== origin) return;
    for (const [key, value] of entries) sessionStorage.setItem(key, value);
  }, saved);
  await p.goto(BASE);
  await p.waitForLoadState('domcontentloaded');
  await p.getByLabel('Chat message input').waitFor({ state: 'visible', timeout: 30_000 });
}

export async function newSession(p: Page): Promise<void> {
  const btn = p.getByRole('button', { name: 'New session', exact: true });
  await btn.click();
  await p.waitForTimeout(1500);
  await p.getByLabel('Chat message input').waitFor({ state: 'visible', timeout: 15000 });
}

export async function send(p: Page, text: string): Promise<void> {
  const input = p.getByLabel('Chat message input');
  await input.waitFor({ state: 'visible', timeout: 15000 });
  await input.fill(text);
  await p.getByRole('button', { name: 'Send message', exact: true }).click();
  await p.waitForTimeout(1500);
}

export async function bodyText(p: Page): Promise<string> {
  return (await p.locator('body').innerText()).replace(/\s+/g, ' ');
}

/**
 * Poll until the "Stop agent" button disappears (agent turn finished) or the
 * timeout elapses. Non-fatal on timeout — callers assert on captured output.
 */
export async function waitForAgentDone(p: Page, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const stopBtn = p.getByRole('button', { name: 'Stop agent' });
    if (!(await stopBtn.isVisible({ timeout: 500 }).catch(() => false))) return;
    await p.waitForTimeout(3000);
  }
}

/**
 * Invoke a non-streaming Lab action from the authenticated browser context.
 * Used for deterministic fixture cleanup: relying on an LLM turn to call a
 * delete tool can pass the behavioral assertion while silently leaving debris.
 */
export async function labActionInPage<T>(
  p: Page,
  action: string,
  params: Record<string, unknown> = {},
): Promise<T> {
  return p.evaluate(async ({ action, params }) => {
    let token: string | undefined;
    for (let i = 0; i < sessionStorage.length; i += 1) {
      const key = sessionStorage.key(i);
      if (!key?.startsWith('oidc.user:')) continue;
      const raw = sessionStorage.getItem(key);
      if (!raw) continue;
      try {
        token = JSON.parse(raw).access_token;
      } catch { /* continue searching */ }
      if (token) break;
    }
    if (!token) throw new Error('No Cognito access token found in sessionStorage');
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    };
    const response = await fetch('/lab', {
      method: 'POST',
      headers,
      body: JSON.stringify({ action, ...params }),
    });
    const text = await response.text();
    if (!response.ok) throw new Error(`Lab action ${action} failed: ${response.status} ${text}`);

    const parse = (raw: string): unknown => {
      if (!raw.includes('data: ')) return JSON.parse(raw);
      for (const line of raw.split('\n')) {
        if (!line.startsWith('data: ')) continue;
        try {
          let value: unknown = JSON.parse(line.slice(6));
          if (typeof value === 'string') {
            try { value = JSON.parse(value); } catch { /* keep scanning */ }
          }
          if (value !== null && typeof value === 'object') return value;
        } catch { /* try the next event */ }
      }
      throw new Error(`Lab action ${action} returned no structured event`);
    };
    return parse(text) as T;
  }, { action, params });
}

// ─── Settings / Capabilities navigation ─────────────────────────────────────

export type CapabilityTab = 'Model' | 'Skills' | 'Agents' | 'Tools' | 'Hooks' | 'General';

/**
 * Open the Capabilities settings modal via the left-rail "Capabilities"
 * button. On staging the button's accessible name is its text content, so we
 * use getByRole (the older `getByLabel('Capabilities')` matched an aria-label
 * that no longer exists and timed out).
 */
export async function openCapabilities(p: Page): Promise<void> {
  // Make sure the app shell is interactive first — on a slow cold load the rail
  // button can be absent for several seconds.
  await p.getByLabel('Chat message input').waitFor({ state: 'visible', timeout: 30000 }).catch(() => {});
  const btn = p.getByRole('button', { name: 'Capabilities', exact: true }).first();
  await btn.waitFor({ state: 'visible', timeout: 30000 });
  await btn.click({ timeout: 15000 });
  await p.waitForTimeout(1000);
}

/**
 * Open a specific tab inside the Capabilities settings modal
 * (Model / Skills / Agents / Tools / Hooks / General). Assumes
 * `openCapabilities` was already called.
 */
export async function openCapabilityTab(p: Page, tab: CapabilityTab): Promise<void> {
  const tabBtn = p.getByRole('button', { name: tab, exact: true }).last();
  await tabBtn.click({ timeout: 15000 });
  await expect(p.getByRole('heading', { name: tab, exact: true }).first()).toBeVisible({ timeout: 2_000 });
}

// ─── Step-trail assertions (dispatch / sub-agent) ────────────────────────────

/**
 * Assert the capture proves an orchestrator dispatch to a sub-agent occurred:
 *   1. a "Dispatching:" step from the orchestrator
 *   2. at least one sub-agent step (prefixed with ↳)
 *   3. (when a summary is present) dispatch_tool shows >0 calls
 * Returns the joined step labels for further assertions.
 */
export function assertDispatch(capture: StreamCapture, context: string): string {
  const allLabels = capture.steps.map((s) => s.label).join('\n');

  const hasDispatchStep = capture.steps.some(
    (s) => s.label.startsWith('Dispatching:') || s.label.startsWith('Dispatching with tools:'),
  );
  expect(
    hasDispatchStep,
    `[${context}] No "Dispatching:" step found — dispatch_tool was never called.\nSteps:\n${allLabels}`,
  ).toBeTruthy();

  const subAgentSteps = capture.steps.filter((s) => s.label.startsWith('↳'));
  expect(
    subAgentSteps.length,
    `[${context}] No sub-agent (↳) steps found.\nSteps:\n${allLabels}`,
  ).toBeGreaterThan(0);

  if (capture.summary?.tools) {
    const dispatchCalls = capture.summary.tools['dispatch_tool']?.calls ?? 0;
    expect(
      dispatchCalls,
      `[${context}] execution_summary.tools.dispatch_tool.calls is 0 — dispatch never fired`,
    ).toBeGreaterThan(0);
  }

  return allLabels;
}

/**
 * Assert a named tool appears in the sub-agent (↳) step trail. Matches on a
 * loose regex so `start_stem_separation` matches "↳ Calling start stem
 * separation...".
 */
export function assertToolCalled(capture: StreamCapture, toolName: string, context: string): void {
  const subSteps = capture.steps.filter((s) => s.label.startsWith('↳'));
  const subLabels = subSteps.map((s) => s.label).join('\n');
  const re = new RegExp(toolName.replace(/_/g, '[_\\s]'), 'i');
  const found = subSteps.some((s) => re.test(s.label));
  expect(
    found,
    `[${context}] Tool "${toolName}" not found in sub-agent steps.\nSub-agent steps:\n${subLabels}`,
  ).toBeTruthy();
}

/**
 * Count how many times a step label matching `re` reached `status: complete`.
 * Used by the hooks audit to prove the render hook fires EXACTLY once.
 */
export function countCompletedSteps(capture: StreamCapture, re: RegExp): number {
  return capture.steps.filter((s) => re.test(s.label) && /complete|done/i.test(s.status)).length;
}
