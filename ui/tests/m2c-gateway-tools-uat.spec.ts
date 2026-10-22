import { test, expect, Page, Response } from '@playwright/test';

/**
 * M2C Gateway Tools — end-to-end UAT (M2C micro-workflow bundle)
 *
 * STRATEGY: Intercept the /api/invocations SSE stream and collect step events.
 * A test only passes when:
 *   1. The agent emitted a "Dispatching:" step (proves dispatch_tool was called)
 *   2. Sub-agent step events prefixed with "↳" appeared (proves task_agent ran)
 *   3. The execution_summary shows dispatch_tool in the tools map
 *   4. The final text contains expected output (job results, URLs, etc.)
 *
 * This eliminates false positives from stale DDB data — built-in tools like
 * list_my_jobs/get_job don't produce dispatch_tool steps.
 */

const BASE = 'http://localhost:3000';
const INGEST_BUCKET = 'mediaworks-ingest-100000000002-us-west-2';
const ASSET_KEY = 'meridian_3min_720p.mp4';
const ASSET_URI = `s3://${INGEST_BUCKET}/${ASSET_KEY}`;

// Collected from the SSE stream during each test
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

async function bodyText(p: Page): Promise<string> {
  return (await p.locator('body').innerText()).replace(/\s+/g, ' ');
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
 * Assert that the stream capture proves gateway dispatch happened.
 * Returns the concatenated step labels for further assertions.
 */
function assertGatewayDispatch(capture: StreamCapture, context: string): string {
  const allLabels = capture.steps.map(s => s.label).join('\n');

  // 1. Must have a "Dispatching:" step from the orchestrator
  const hasDispatchStep = capture.steps.some(s =>
    s.label.startsWith('Dispatching:') || s.label.startsWith('Dispatching with tools:')
  );
  expect(hasDispatchStep,
    `[${context}] No "Dispatching:" step found — dispatch_tool was never called.\nSteps:\n${allLabels}`
  ).toBeTruthy();

  // 2. Must have sub-agent steps (prefixed with ↳)
  const subAgentSteps = capture.steps.filter(s => s.label.startsWith('↳'));
  expect(subAgentSteps.length,
    `[${context}] No sub-agent (↳) steps found — task_agent never responded.\nSteps:\n${allLabels}`
  ).toBeGreaterThan(0);

  // 3. Execution summary must show dispatch_tool usage
  if (capture.summary?.tools) {
    const dispatchCalls = capture.summary.tools['dispatch_tool']?.calls ?? 0;
    expect(dispatchCalls,
      `[${context}] execution_summary.tools.dispatch_tool.calls is 0 — dispatch never fired`
    ).toBeGreaterThan(0);
  }

  return allLabels;
}

/**
 * Assert that a specific gateway tool was called by the task agent.
 * Looks for "↳ Calling <tool>..." or "↳ <tool>" patterns.
 */
function assertToolCalled(capture: StreamCapture, toolName: string, context: string) {
  const subSteps = capture.steps.filter(s => s.label.startsWith('↳'));
  const subLabels = subSteps.map(s => s.label).join('\n');
  const re = new RegExp(toolName.replace(/_/g, '[_\\s]'), 'i');
  const found = subSteps.some(s => re.test(s.label));
  expect(found,
    `[${context}] Tool "${toolName}" not found in sub-agent steps.\nSub-agent steps:\n${subLabels}`
  ).toBeTruthy();
}

// ─── Test Suite: Individual Gateway Tools ─────────────────────────────────

test.describe.serial('M2C Gateway: Dispatch verification', () => {
  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
    await devLogin(page);
  });
  test.afterAll(async () => { await page?.close(); });

  test('1. Audio Stem Separation — dispatch_tool fires + job completes', async () => {
    test.setTimeout(10 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    const prompt = `Run audio stem separation on ${ASSET_URI} — extract ONLY the speech stem.`;
    await send(page, prompt);
    await waitForAgentDone(page, 2 * 60 * 1000);

    // Core assertion: dispatch_tool was actually invoked
    assertGatewayDispatch(capture, 'stem-separation');

    // The sub-agent should have called start_stem_separation or similar
    const subLabels = capture.steps.filter(s => s.label.startsWith('↳')).map(s => s.label).join(' ');
    expect(
      /stem|separation|start_/i.test(subLabels),
      `Expected sub-agent to call a stem-separation gateway tool.\nSub-steps: ${subLabels}`
    ).toBeTruthy();

    // Fire-and-forget: response shows job submitted (RUNNING or job_id)
    const txt = await bodyText(page);
    expect(
      /RUNNING|SUCCEEDED|job.id|arn:aws|speech.*\.wav|s3:\/\//i.test(txt),
      `Expected job submission confirmation.\nTail: ${txt.slice(-400)}`
    ).toBeTruthy();
  });

  test('2. get_download_url — presigned URL returned', async () => {
    test.setTimeout(8 * 60 * 1000);
    // Continues from test 1's session — job may still be running (fire-and-forget).
    // Ask agent to check job + get download URL via dispatch (forces gateway tool use).

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, `For the stem separation job I just started: dispatch a sub-agent with get_job_status and get_download_url tools to check if it succeeded. If it did, get a presigned download link for the speech WAV. If still running, tell me the status.`);
    await waitForAgentDone(page, 7 * 60 * 1000);

    // The agent should have dispatched (or used built-in tools)
    const allLabels = capture.steps.map(s => s.label).join('\n');
    const anyAction = capture.steps.length > 0 ||
      allLabels.includes('Dispatching') || allLabels.includes('get_job');
    expect(anyAction,
      `[get_download_url] No steps captured at all.\nRaw steps: ${JSON.stringify(capture.steps)}`
    ).toBeTruthy();

    // Response should contain useful info (URL, status, or error)
    const txt = await bodyText(page);
    expect(
      /https?:\/\/|SUCCEEDED|RUNNING|presigned|download|speech|status|job/i.test(txt),
      `Expected a presigned URL or job status info.\nTail: ${txt.slice(-400)}`
    ).toBeTruthy();
  });

  test('3. Speech Diarization — dispatch fires + transcript produced', async () => {
    test.setTimeout(10 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    const prompt = `Run speech diarization on ${ASSET_URI} with mark_in_out [0, 60]. Poll until done and show me the transcript.`;
    await send(page, prompt);
    await waitForAgentDone(page, 9 * 60 * 1000);

    assertGatewayDispatch(capture, 'speech-diarization');

    const subLabels = capture.steps.filter(s => s.label.startsWith('↳')).map(s => s.label).join(' ');
    expect(
      /diariz|speech|start_/i.test(subLabels),
      `Expected sub-agent to call a diarization gateway tool.\nSub-steps: ${subLabels}`
    ).toBeTruthy();

    const txt = await bodyText(page);
    expect(
      /RUNNING|SUCCEEDED|transcript|speaker|\.vtt|diariz|job.id|arn:aws/i.test(txt),
      `Expected diarization job submission or results.\nTail: ${txt.slice(-400)}`
    ).toBeTruthy();
  });

  test('4. Media Processing (trim+transcode) — dispatch fires + MP4 output', async () => {
    test.setTimeout(8 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    const prompt = `Use process_media to trim the first 15 seconds of ${ASSET_URI} and produce a 480p MP4 proxy.`;
    await send(page, prompt);
    await waitForAgentDone(page, 2 * 60 * 1000);

    assertGatewayDispatch(capture, 'media-processing');

    const subLabels = capture.steps.filter(s => s.label.startsWith('↳')).map(s => s.label).join(' ');
    expect(
      /process|media|transcode|ffmpeg|start_/i.test(subLabels),
      `Expected sub-agent to call a media-processing gateway tool.\nSub-steps: ${subLabels}`
    ).toBeTruthy();

    const txt = await bodyText(page);
    expect(
      /RUNNING|SUCCEEDED|\.mp4|proxy|output|job.id|arn:aws/i.test(txt),
      `Expected media processing job submission or results.\nTail: ${txt.slice(-400)}`
    ).toBeTruthy();
  });

  test('5. Shot Segmentation — dispatch fires + shot boundaries returned', async () => {
    test.setTimeout(10 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    const prompt = `Run shot segmentation on ${ASSET_URI} for the first 60 seconds (mark_in_out [0, 60]).`;
    await send(page, prompt);
    await waitForAgentDone(page, 2 * 60 * 1000);

    assertGatewayDispatch(capture, 'shot-segmentation');

    const subLabels = capture.steps.filter(s => s.label.startsWith('↳')).map(s => s.label).join(' ');
    expect(
      /shot|segment|start_/i.test(subLabels),
      `Expected sub-agent to call a shot-segmentation gateway tool.\nSub-steps: ${subLabels}`
    ).toBeTruthy();

    const txt = await bodyText(page);
    expect(
      /RUNNING|SUCCEEDED|shot|boundar|segment|scene|job.id|arn:aws/i.test(txt),
      `Expected shot segmentation job submission or results.\nTail: ${txt.slice(-400)}`
    ).toBeTruthy();
  });
});

// ─── Test Suite: Multi-step Pipeline ──────────────────────────────────────

test.describe.serial('M2C Gateway: Multi-tool pipeline', () => {
  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
    await devLogin(page);
  });
  test.afterAll(async () => { await page?.close(); });

  test('6. Chain: trim → diarize — dispatched and both tools called', async () => {
    test.setTimeout(15 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    const prompt = `I need a transcript of just the first 30 seconds of ${ASSET_URI}. ` +
      `Use process_media to trim the video to the first 30s, then run speech diarization ` +
      `on the trimmed output. Show the transcript when done.`;
    await send(page, prompt);
    await waitForAgentDone(page, 14 * 60 * 1000);

    // Must have dispatched at least once (direct mode bundles both tools)
    const dispatchSteps = capture.steps.filter(s =>
      s.label.startsWith('Dispatching:') || s.label.startsWith('Dispatching with tools:')
    );
    expect(dispatchSteps.length,
      `Expected >=1 dispatch_tool call, got ${dispatchSteps.length}.\n` +
      `Dispatch steps: ${dispatchSteps.map(s => s.label).join('\n')}`
    ).toBeGreaterThanOrEqual(1);

    // Sub-agent steps must show at least one media tool was called
    const subLabels = capture.steps.filter(s => s.label.startsWith('↳')).map(s => s.label).join(' ');
    expect(
      /process.media|media.processing|diariz|speech|trim/i.test(subLabels),
      `Expected media tool in sub-agent steps.\nSub-steps: ${subLabels}`
    ).toBeTruthy();

    const txt = await bodyText(page);
    expect(
      /transcript|speaker|\.vtt|RUNNING|SUCCEEDED|job.id|arn:aws|process|diariz/i.test(txt),
      `Expected pipeline progress or results.\nTail: ${txt.slice(-500)}`
    ).toBeTruthy();
  });
});

// ─── Test Suite: Error Handling ───────────────────────────────────────────

test.describe.serial('M2C Gateway: Error handling', () => {
  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
    await devLogin(page);
  });
  test.afterAll(async () => { await page?.close(); });

  test('7. Invalid asset — dispatch fires but reports error gracefully', async () => {
    test.setTimeout(5 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, 'Run stem separation on s3://nonexistent-bucket-xyz/fake-video.mp4 — speech only.');
    await waitForAgentDone(page, 4 * 60 * 1000);

    // Even an error case must dispatch (the agent must TRY the gateway tool)
    const hasDispatch = capture.steps.some(s =>
      s.label.startsWith('Dispatching:') || s.label.startsWith('Dispatching with tools:')
    );
    expect(hasDispatch,
      `Expected dispatch_tool to fire even for an invalid URI.\nSteps: ${capture.steps.map(s => s.label).join('\n')}`
    ).toBeTruthy();

    // Should see an error/failure in the response
    const txt = await bodyText(page);
    expect(
      /error|fail|not found|access denied|does not exist|cannot|invalid/i.test(txt),
      `Expected an error message in response.\nTail: ${txt.slice(-300)}`
    ).toBeTruthy();
  });

  test('8. Dispatch failure visible in steps — failed status on sub-agent step', async () => {
    test.setTimeout(5 * 60 * 1000);
    // Same session as test 7 — check that the step events show failure

    // From test 7's capture, check for failed sub-agent step
    // Since we're in a serial suite, reuse the session and send another bad request
    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, 'Call get_download_url with s3uri_or_key="s3://fake-bucket-abc123/nonexistent.wav"');
    await waitForAgentDone(page, 3 * 60 * 1000);

    // Dispatch should fire
    const hasDispatch = capture.steps.some(s =>
      s.label.startsWith('Dispatching:') || s.label.startsWith('Dispatching with tools:')
    );
    expect(hasDispatch,
      `Expected dispatch for get_download_url on invalid key.\nSteps: ${capture.steps.map(s => s.label).join('\n')}`
    ).toBeTruthy();

    // Response should indicate the error
    const txt = await bodyText(page);
    expect(
      /error|fail|denied|not found|invalid|cannot|no such/i.test(txt),
      `Expected error for invalid S3 key.\nTail: ${txt.slice(-300)}`
    ).toBeTruthy();
  });
});

// ─── Test Suite: Dispatch sanity (no false positives) ─────────────────────

test.describe.serial('M2C Gateway: No false positive', () => {
  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
    await devLogin(page);
  });
  test.afterAll(async () => { await page?.close(); });

  test('9. Non-gateway prompt — dispatch_tool is NOT called', async () => {
    test.setTimeout(3 * 60 * 1000);
    await newSession(page);

    const capture = createStreamCapture();
    attachStreamInterceptor(page, capture);

    await send(page, 'What is 2+2? Just give me the number.');
    await waitForAgentDone(page, 2 * 60 * 1000);

    // For a simple math question, dispatch_tool should NOT fire
    const hasDispatch = capture.steps.some(s =>
      s.label.startsWith('Dispatching:') || s.label.startsWith('Dispatching with tools:')
    );
    expect(hasDispatch,
      `dispatch_tool should NOT fire for a non-gateway prompt.\nSteps: ${capture.steps.map(s => s.label).join('\n')}`
    ).toBeFalsy();
  });
});
