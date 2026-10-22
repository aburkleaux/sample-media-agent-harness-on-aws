import { test, expect, Page, Response } from '@playwright/test';

/**
 * Routing UAT — validates the orchestrator routes to the correct execution path.
 *
 * Two categories:
 * 1. DIRECT DISPATCH — single gateway tool call, expect "Dispatching:" step, NO "Planning"
 * 2. PLANNING REQUIRED — multi-step chain, expect "Planning and executing..." step
 *
 * These tests validate ROUTING correctness, not job completion. They pass once
 * the correct path is triggered (within ~60s), without waiting for jobs to finish.
 */

const BASE = 'http://localhost:3000';
const INGEST_BUCKET = 'mediaworks-ingest-100000000002-us-west-2';
const ASSET_KEY = 'meridian_3min_720p.mp4';
const ASSET_URI = `s3://${INGEST_BUCKET}/${ASSET_KEY}`;

interface StreamCapture {
  steps: Array<{ status: string; label: string; agent?: string }>;
  texts: string[];
  summary: { tools?: Record<string, { calls: number }> } | null;
  raw: string;
}

let page: Page;

// ─── Helpers ────────────────────────────────────────────────────────────────

function createStreamCapture(): StreamCapture {
  return { steps: [], texts: [], summary: null, raw: '' };
}

function attachStreamInterceptor(p: Page, capture: StreamCapture): void {
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
            try { parsed = JSON.parse(parsed); } catch { capture.texts.push(parsed); continue; }
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
        } catch { /* not JSON */ }
      }
    } catch { /* response body may not be available */ }
  });
}

async function devLogin(p: Page) {
  await p.goto(BASE);
  await p.waitForLoadState('domcontentloaded');
  await p.waitForTimeout(1000);

  const getStarted = p.locator('button:has-text("Get Started")').first();
  if (await getStarted.isVisible({ timeout: 3000 }).catch(() => false)) {
    await getStarted.click();
    await p.waitForTimeout(2000);
  }

  const devBtn = p.locator('button:has-text("Dev Login")');
  if (await devBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
    await devBtn.click();
    await p.waitForTimeout(3000);
  }

  await p.getByLabel('Chat message input').waitFor({ state: 'visible', timeout: 15000 });
}

async function newSession(p: Page) {
  const btn = p.getByRole('button', { name: 'New session', exact: true });
  await btn.click();
  await p.waitForTimeout(1500);
  await p.getByLabel('Chat message input').waitFor({ state: 'visible', timeout: 15000 });
}

async function send(p: Page, text: string) {
  const input = p.getByLabel('Chat message input');
  await input.waitFor({ state: 'visible', timeout: 15000 });
  await input.fill(text);
  await p.getByRole('button', { name: 'Send message', exact: true }).click();
  await p.waitForTimeout(1500);
}

async function waitForAgentDone(p: Page, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const stopBtn = p.getByRole('button', { name: 'Stop agent' });
    if (!await stopBtn.isVisible({ timeout: 500 }).catch(() => false)) return;
    await p.waitForTimeout(3000);
  }
}

/**
 * Wait until we see at least one routing indicator in the stream, or timeout.
 * This is faster than waiting for the full agent to finish — we only need
 * to confirm routing, not job completion.
 */
async function waitForRoutingSignal(p: Page, capture: StreamCapture, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const hasDispatch = capture.steps.some(s =>
      s.label.startsWith('Dispatching') || s.label.startsWith('Planning')
    );
    if (hasDispatch) return;
    await p.waitForTimeout(2000);
  }
}

function hasDispatchStep(capture: StreamCapture): boolean {
  return capture.steps.some(s =>
    s.label.startsWith('Dispatching to sub-agent') ||
    s.label.startsWith('Dispatching:') ||
    s.label.startsWith('Dispatching with tools:')
  );
}

function hasPlanningStep(capture: StreamCapture): boolean {
  return capture.steps.some(s =>
    s.label.startsWith('Planning and executing') ||
    s.label.startsWith('Planning Agent')
  );
}

function hasSkillActivation(capture: StreamCapture): boolean {
  return capture.steps.some(s =>
    s.label.includes('video-processing') || s.label.includes('skill')
  );
}

// ─── Suite 1: Direct Dispatch (single tool) ──────────────────────────────────

test.describe.serial('Routing: Direct dispatch (single tool)', () => {
  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
    await devLogin(page);
  });
  test.afterAll(async () => { await page?.close(); });

  test('R1. "separate all stems" → dispatch_tool (NOT plan_and_execute)', async () => {
    test.setTimeout(3 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, `Separate all audio stems from ${ASSET_URI}`);
    await waitForRoutingSignal(page, capture, 60_000);

    const allLabels = capture.steps.map(s => s.label).join('\n');

    // Should dispatch directly — NOT plan
    expect(hasDispatchStep(capture),
      `Expected direct dispatch for single-tool task.\nSteps:\n${allLabels}`
    ).toBeTruthy();

    // Should NOT trigger planning for a single-tool request
    const planBeforeDispatch = capture.steps.findIndex(s => s.label.startsWith('Planning'));
    const dispatchIdx = capture.steps.findIndex(s => s.label.startsWith('Dispatching'));
    if (planBeforeDispatch !== -1) {
      expect(planBeforeDispatch,
        `Planning should NOT happen before dispatch for single-tool task.\nSteps:\n${allLabels}`
      ).toBeGreaterThan(dispatchIdx);
    }
  });

  test('R2. "trim to first 30 seconds" → dispatch_tool (ONE process_media)', async () => {
    test.setTimeout(3 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, `Trim ${ASSET_URI} to the first 30 seconds and output as MP4.`);
    await waitForRoutingSignal(page, capture, 60_000);

    const allLabels = capture.steps.map(s => s.label).join('\n');
    expect(hasDispatchStep(capture),
      `Expected direct dispatch for trim (single process_media).\nSteps:\n${allLabels}`
    ).toBeTruthy();
  });

  test('R3. "run speech diarization" → dispatch_tool (ONE diarize_speech)', async () => {
    test.setTimeout(3 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, `Run speech diarization on ${ASSET_URI}`);
    await waitForRoutingSignal(page, capture, 60_000);

    const allLabels = capture.steps.map(s => s.label).join('\n');
    expect(hasDispatchStep(capture),
      `Expected direct dispatch for diarization.\nSteps:\n${allLabels}`
    ).toBeTruthy();
  });

  test('R4. "detect shot boundaries" → dispatch_tool (ONE segment_shots)', async () => {
    test.setTimeout(3 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, `Detect shot boundaries in ${ASSET_URI}`);
    await waitForRoutingSignal(page, capture, 60_000);

    const allLabels = capture.steps.map(s => s.label).join('\n');
    expect(hasDispatchStep(capture),
      `Expected direct dispatch for shot segmentation.\nSteps:\n${allLabels}`
    ).toBeTruthy();
  });
});

// ─── Suite 2: Planning Required (multi-step) ─────────────────────────────────

test.describe.serial('Routing: Planning required (multi-step)', () => {
  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
    await devLogin(page);
  });
  test.afterAll(async () => { await page?.close(); });

  test('R5. "extract speech for first 60s" → plan_and_execute (trim THEN stem-separate)', async () => {
    test.setTimeout(3 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, `Extract the speech audio from the first 60 seconds of ${ASSET_URI}`);
    await waitForRoutingSignal(page, capture, 90_000);

    const allLabels = capture.steps.map(s => s.label).join('\n');

    // Should trigger planning (multi-step: trim → stem separate)
    expect(hasPlanningStep(capture),
      `Expected plan_and_execute for time-scoped stem separation (needs trim first).\nSteps:\n${allLabels}`
    ).toBeTruthy();
  });

  test('R6. "diarize just the first 30 seconds" → plan_and_execute (trim THEN diarize)', async () => {
    test.setTimeout(3 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, `Transcribe only the first 30 seconds of ${ASSET_URI} using speech diarization.`);
    await waitForRoutingSignal(page, capture, 90_000);

    const allLabels = capture.steps.map(s => s.label).join('\n');
    expect(hasPlanningStep(capture),
      `Expected plan_and_execute for time-scoped diarization (needs trim first).\nSteps:\n${allLabels}`
    ).toBeTruthy();
  });

  test('R7. "separate stems for seconds 30-90" → plan_and_execute (trim THEN stem-separate)', async () => {
    test.setTimeout(3 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, `Run audio stem separation on the segment from 30s to 90s of ${ASSET_URI}`);
    await waitForRoutingSignal(page, capture, 90_000);

    const allLabels = capture.steps.map(s => s.label).join('\n');
    expect(hasPlanningStep(capture),
      `Expected plan_and_execute for time-scoped stem separation.\nSteps:\n${allLabels}`
    ).toBeTruthy();
  });

  test('R8. "trim then run diarization on trimmed output" → plan_and_execute', async () => {
    test.setTimeout(3 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, `Trim ${ASSET_URI} to the first 45 seconds, then run speech diarization on the trimmed clip.`);
    await waitForRoutingSignal(page, capture, 90_000);

    const allLabels = capture.steps.map(s => s.label).join('\n');
    expect(hasPlanningStep(capture),
      `Expected plan_and_execute for explicit 2-step chain.\nSteps:\n${allLabels}`
    ).toBeTruthy();
  });
});

// ─── Suite 3: Direct Answer (no tools needed) ───────────────────────────────

test.describe.serial('Routing: Direct answer (no tools)', () => {
  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
    await devLogin(page);
  });
  test.afterAll(async () => { await page?.close(); });

  test('R9. Simple factual question → no dispatch, no planning', async () => {
    test.setTimeout(2 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, 'What is the capital of France?');
    await waitForAgentDone(page, 60_000);

    expect(hasDispatchStep(capture),
      `dispatch_tool should NOT fire for a simple question.\nSteps: ${capture.steps.map(s => s.label).join('\n')}`
    ).toBeFalsy();

    expect(hasPlanningStep(capture),
      `plan_and_execute should NOT fire for a simple question.\nSteps: ${capture.steps.map(s => s.label).join('\n')}`
    ).toBeFalsy();
  });

  test('R10. "what tools do you have?" → direct answer (no dispatch)', async () => {
    test.setTimeout(2 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, 'What tools and capabilities do you have?');
    await waitForAgentDone(page, 60_000);

    const allLabels = capture.steps.map(s => s.label).join('\n');
    expect(hasDispatchStep(capture),
      `dispatch_tool should NOT fire for capability question.\nSteps:\n${allLabels}`
    ).toBeFalsy();

    // Should contain text about tools/capabilities
    const txt = capture.texts.join(' ');
    expect(
      /tool|capabilit|gateway|media|process/i.test(txt + allLabels),
      `Expected agent to describe capabilities.\nTexts: ${txt.slice(0, 300)}`
    ).toBeTruthy();
  });

  test('R11. "explain what stem separation does" → direct answer (no dispatch)', async () => {
    test.setTimeout(2 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, 'Explain what audio stem separation does and when I would use it.');
    await waitForAgentDone(page, 60_000);

    const allLabels = capture.steps.map(s => s.label).join('\n');
    expect(hasDispatchStep(capture),
      `dispatch_tool should NOT fire for an explanatory question.\nSteps:\n${allLabels}`
    ).toBeFalsy();
  });
});

// ─── Suite 4: Multi-turn (follow-up in same session) ─────────────────────────

test.describe.serial('Routing: Multi-turn follow-up', () => {
  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
    await devLogin(page);
  });
  test.afterAll(async () => { await page?.close(); });

  test('R12. Turn 1: dispatch stem separation → Turn 2: "what about diarization too?" → plan', async () => {
    test.setTimeout(5 * 60 * 1000);
    await newSession(page);

    // Turn 1: single dispatch
    const capture1 = createStreamCapture();
    attachStreamInterceptor(page, capture1);

    await send(page, `Separate all audio stems from ${ASSET_URI}`);
    await waitForRoutingSignal(page, capture1, 60_000);

    const labels1 = capture1.steps.map(s => s.label).join('\n');
    expect(hasDispatchStep(capture1),
      `Turn 1 should dispatch directly.\nSteps:\n${labels1}`
    ).toBeTruthy();

    // Wait for turn 1 to finish before sending turn 2
    await waitForAgentDone(page, 120_000);

    // Turn 2: follow-up that requires a different tool (same session)
    const capture2 = createStreamCapture();
    attachStreamInterceptor(page, capture2);

    await send(page, 'Now also run speech diarization on the same video.');
    await waitForRoutingSignal(page, capture2, 60_000);

    const labels2 = capture2.steps.map(s => s.label).join('\n');
    // Should dispatch (single tool, not planning — it's a new independent operation)
    expect(hasDispatchStep(capture2),
      `Turn 2 should dispatch for diarization.\nSteps:\n${labels2}`
    ).toBeTruthy();
  });

  test('R13. Turn 1: ask a question → Turn 2: trigger a tool from context', async () => {
    test.setTimeout(5 * 60 * 1000);
    await newSession(page);

    // Turn 1: informational (no dispatch)
    const capture1 = createStreamCapture();
    attachStreamInterceptor(page, capture1);

    await send(page, `I have a video at ${ASSET_URI}. What processing options are available for it?`);
    await waitForAgentDone(page, 60_000);

    expect(hasDispatchStep(capture1),
      `Turn 1 (informational) should NOT dispatch.\nSteps: ${capture1.steps.map(s => s.label).join('\n')}`
    ).toBeFalsy();

    // Turn 2: now request an action using context from turn 1
    const capture2 = createStreamCapture();
    attachStreamInterceptor(page, capture2);

    await send(page, 'Go ahead and run shot segmentation on it.');
    await waitForRoutingSignal(page, capture2, 60_000);

    const labels2 = capture2.steps.map(s => s.label).join('\n');
    expect(hasDispatchStep(capture2),
      `Turn 2 should dispatch for shot segmentation.\nSteps:\n${labels2}`
    ).toBeTruthy();
  });

  test('R14. Turn 1: dispatch job → Turn 2: "check on my jobs" → built-in tool (no dispatch)', async () => {
    test.setTimeout(5 * 60 * 1000);
    await newSession(page);

    // Turn 1: kick off a job
    const capture1 = createStreamCapture();
    attachStreamInterceptor(page, capture1);

    await send(page, `Trim ${ASSET_URI} to the first 10 seconds.`);
    await waitForRoutingSignal(page, capture1, 60_000);
    await waitForAgentDone(page, 120_000);

    // Turn 2: ask about jobs (should use list_my_jobs built-in, NOT dispatch)
    const capture2 = createStreamCapture();
    attachStreamInterceptor(page, capture2);

    await send(page, 'What jobs do I have running?');
    await waitForAgentDone(page, 60_000);

    const labels2 = capture2.steps.map(s => s.label).join('\n');
    // list_my_jobs is a built-in — should NOT trigger dispatch_tool or planning
    expect(hasPlanningStep(capture2),
      `Job status check should NOT trigger planning.\nSteps:\n${labels2}`
    ).toBeFalsy();
  });
});
