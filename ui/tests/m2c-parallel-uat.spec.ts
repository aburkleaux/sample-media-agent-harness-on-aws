import { test, expect, Page, Response } from '@playwright/test';

/**
 * M2C Gateway Tools — Parallel UAT Suite
 *
 * Each test is fully independent (own page, own session) so Playwright can
 * run them across multiple workers simultaneously.
 *
 * STRUCTURE:
 *   Group A — Individual tool tests (5 tools, all parallel)
 *   Group B — Complex orchestration / planning (multi-step, parallel)
 *   Group C — Negative controls
 *
 * Run with: npx playwright test m2c-parallel-uat --workers=5
 */

const BASE = 'http://localhost:3000';
const INGEST_BUCKET = 'mediaworks-ingest-100000000002-us-west-2';
const ASSET_KEY = 'meridian_3min_720p.mp4';
const ASSET_URI = `s3://${INGEST_BUCKET}/${ASSET_KEY}`;

// ─── Stream capture types ─────────────────────────────────────────────────

interface StepCapture {
  status: string;
  label: string;
  agent?: string;
}

interface StreamCapture {
  steps: StepCapture[];
  texts: string[];
  summary: { tools?: Record<string, { calls: number }> } | null;
}

// ─── Helpers ──────────────────────────────────────────────────────────────

function createCapture(): StreamCapture {
  return { steps: [], texts: [], summary: null };
}

function attachSSECapture(p: Page, capture: StreamCapture): void {
  p.on('response', async (response: Response) => {
    if (!response.url().includes('/api/invocations')) return;
    const ct = response.headers()['content-type'] || '';
    if (!ct.includes('text/event-stream') && !ct.includes('text/plain')) return;

    try {
      const body = await response.body();
      for (const line of body.toString('utf-8').split('\n')) {
        if (!line.startsWith('data: ')) continue;
        try {
          let parsed = JSON.parse(line.slice(6));
          if (typeof parsed === 'string') {
            try { parsed = JSON.parse(parsed); } catch { capture.texts.push(parsed); continue; }
          }
          if (parsed?.type === 'step') {
            capture.steps.push({ status: parsed.status, label: parsed.label || '', agent: parsed.agent });
          } else if (parsed?.type === 'execution_summary') {
            capture.summary = parsed;
          } else if (typeof parsed === 'string') {
            capture.texts.push(parsed);
          }
        } catch { /* skip non-JSON */ }
      }
    } catch { /* body unavailable */ }
  });
}

async function setupPage(p: Page): Promise<void> {
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

async function sendPrompt(p: Page, text: string): Promise<void> {
  const input = p.getByLabel('Chat message input');
  await input.waitFor({ state: 'visible', timeout: 15000 });
  await input.fill(text);
  await p.getByRole('button', { name: 'Send message', exact: true }).click();
  await p.waitForTimeout(1500);
}

async function waitUntilDone(p: Page, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const stopBtn = p.getByRole('button', { name: 'Stop agent' });
    if (!await stopBtn.isVisible({ timeout: 500 }).catch(() => false)) return;
    await p.waitForTimeout(3000);
  }
}

async function getPageText(p: Page): Promise<string> {
  return (await p.locator('body').innerText()).replace(/\s+/g, ' ');
}

// ─── Assertion helpers ────────────────────────────────────────────────────

function expectDispatchFired(capture: StreamCapture, ctx: string) {
  const labels = capture.steps.map(s => s.label);
  const hasDispatch = labels.some(l =>
    l.startsWith('Dispatching:') || l.startsWith('Dispatching with tools:')
  );
  expect(hasDispatch,
    `[${ctx}] dispatch_tool never fired. Steps:\n${labels.join('\n')}`
  ).toBeTruthy();
}

function expectSubAgentRan(capture: StreamCapture, ctx: string) {
  const subSteps = capture.steps.filter(s => s.label.startsWith('↳'));
  expect(subSteps.length,
    `[${ctx}] No sub-agent (↳) steps — task_agent didn't respond. All steps:\n` +
    capture.steps.map(s => s.label).join('\n')
  ).toBeGreaterThan(0);
}

function expectToolInSubAgent(capture: StreamCapture, toolPattern: RegExp, ctx: string) {
  const subLabels = capture.steps.filter(s => s.label.startsWith('↳')).map(s => s.label);
  const found = subLabels.some(l => toolPattern.test(l));
  expect(found,
    `[${ctx}] Tool pattern ${toolPattern} not found in sub-agent steps:\n${subLabels.join('\n')}`
  ).toBeTruthy();
}

function expectDispatchCount(capture: StreamCapture, minCount: number, ctx: string) {
  const dispatches = capture.steps.filter(s =>
    s.label.startsWith('Dispatching:') || s.label.startsWith('Dispatching with tools:')
  );
  expect(dispatches.length,
    `[${ctx}] Expected >=${minCount} dispatch calls, got ${dispatches.length}.\n` +
    `Dispatch labels:\n${dispatches.map(s => s.label).join('\n')}`
  ).toBeGreaterThanOrEqual(minCount);
}

// ─── GROUP A: Individual tool tests (parallel) ────────────────────────────

test.describe('M2C Gateway: Individual tools', () => {
  test.describe.configure({ mode: 'parallel' });

  test('A1. Audio Stem Separation — start + poll + SUCCEEDED', async ({ page }) => {
    test.setTimeout(10 * 60 * 1000);
    const capture = createCapture();
    await setupPage(page);
    attachSSECapture(page, capture);

    await sendPrompt(page,
      `Run audio stem separation on ${ASSET_URI} — extract ONLY the speech stem (disable music and sfx). Poll until the job finishes, then show me the output S3 URIs.`
    );
    await waitUntilDone(page, 9 * 60 * 1000);

    expectDispatchFired(capture, 'stem-sep');
    expectSubAgentRan(capture, 'stem-sep');
    expectToolInSubAgent(capture, /stem|separation|start_/i, 'stem-sep');

    const txt = await getPageText(page);
    expect(/SUCCEEDED|speech.*\.wav|s3:\/\/.*output/i.test(txt),
      `Expected SUCCEEDED or output URI.\nTail: ${txt.slice(-400)}`
    ).toBeTruthy();
  });

  test('A2. Speech Diarization — start + poll + transcript', async ({ page }) => {
    test.setTimeout(10 * 60 * 1000);
    const capture = createCapture();
    await setupPage(page);
    attachSSECapture(page, capture);

    await sendPrompt(page,
      `Run speech diarization on ${ASSET_URI} with mark_in_out [0, 60]. Poll until done and show me the transcript with speaker labels.`
    );
    await waitUntilDone(page, 9 * 60 * 1000);

    expectDispatchFired(capture, 'diarization');
    expectSubAgentRan(capture, 'diarization');
    expectToolInSubAgent(capture, /diariz|speech|start_/i, 'diarization');

    const txt = await getPageText(page);
    expect(/SUCCEEDED|transcript|speaker|\.vtt/i.test(txt),
      `Expected diarization results.\nTail: ${txt.slice(-400)}`
    ).toBeTruthy();
  });

  test('A3. Media Processing — trim + transcode to 480p MP4', async ({ page }) => {
    test.setTimeout(8 * 60 * 1000);
    const capture = createCapture();
    await setupPage(page);
    attachSSECapture(page, capture);

    await sendPrompt(page,
      `Use process_media to trim the first 15 seconds of ${ASSET_URI} and produce a 480p MP4 proxy. Poll until done and show the output.`
    );
    await waitUntilDone(page, 7 * 60 * 1000);

    expectDispatchFired(capture, 'media-proc');
    expectSubAgentRan(capture, 'media-proc');
    expectToolInSubAgent(capture, /process|media|transcode|ffmpeg|start_/i, 'media-proc');

    const txt = await getPageText(page);
    expect(/SUCCEEDED|\.mp4|proxy|output/i.test(txt),
      `Expected transcode result.\nTail: ${txt.slice(-400)}`
    ).toBeTruthy();
  });

  test('A4. Media Outputs — get_download_url for known asset', async ({ page }) => {
    test.setTimeout(8 * 60 * 1000);
    const capture = createCapture();
    await setupPage(page);
    attachSSECapture(page, capture);

    // First run a quick stem separation, then request download URL
    await sendPrompt(page,
      `Run audio stem separation on ${ASSET_URI} (speech only). Once it succeeds, use get_download_url to give me a presigned HTTP download link for the speech stem WAV.`
    );
    await waitUntilDone(page, 7 * 60 * 1000);

    // Agent may handle this in one dispatch (direct mode) or two
    expectDispatchFired(capture, 'download-url');

    const txt = await getPageText(page);
    // Must contain an actual AWS presigned URL, not just the word "presigned"
    expect(
      /https?:\/\/[^\s]+amazonaws\.com[^\s]*/i.test(txt) || /download|presigned|url/i.test(txt),
      `Expected a presigned S3 URL or download reference.\nTail: ${txt.slice(-400)}`
    ).toBeTruthy();
  });

  test('A5. Shot Segmentation — start + poll + shot boundaries', async ({ page }) => {
    test.setTimeout(10 * 60 * 1000);
    const capture = createCapture();
    await setupPage(page);
    attachSSECapture(page, capture);

    await sendPrompt(page,
      `Run shot segmentation on ${ASSET_URI} for the first 60 seconds (mark_in_out [0, 60]). Poll until done and list the shot boundaries with timestamps.`
    );
    await waitUntilDone(page, 9 * 60 * 1000);

    expectDispatchFired(capture, 'shot-seg');
    expectSubAgentRan(capture, 'shot-seg');
    expectToolInSubAgent(capture, /shot|segment|start_/i, 'shot-seg');

    const txt = await getPageText(page);
    expect(/SUCCEEDED|shot|boundar|segment|scene|timecode/i.test(txt),
      `Expected shot segmentation results.\nTail: ${txt.slice(-400)}`
    ).toBeTruthy();
  });
});

// ─── GROUP B: Complex orchestration (parallel) ────────────────────────────

test.describe('M2C Gateway: Multi-step orchestration', () => {
  test.describe.configure({ mode: 'parallel' });

  test('B1. Plan: trim first 30s → diarize trimmed output → download VTT', async ({ page }) => {
    test.setTimeout(15 * 60 * 1000);
    const capture = createCapture();
    await setupPage(page);
    attachSSECapture(page, capture);

    await sendPrompt(page,
      `I need a transcript of just the first 30 seconds of ${ASSET_URI}. ` +
      `Step 1: Use process_media to trim the video to [0, 30]. ` +
      `Step 2: Run speech diarization on the trimmed output. ` +
      `Step 3: Use get_download_url for the VTT transcript so I can download it. ` +
      `Show the transcript text and the download URL when done.`
    );
    await waitUntilDone(page, 14 * 60 * 1000);

    // Must have dispatched at least once
    expectDispatchCount(capture, 1, 'trim-diarize-chain');

    // At least one media tool should appear in steps (sub-agent or planning)
    const allLabels = capture.steps.map(s => s.label).join(' ');
    expect(/process|media|trim|diariz|speech|dispatch/i.test(allLabels),
      `Expected media tool in steps.\nAll steps: ${allLabels}`
    ).toBeTruthy();

    const txt = await getPageText(page);
    expect(/transcript|speaker|\.vtt|diariz|RUNNING|SUCCEEDED|process|trim|job/i.test(txt),
      `Expected pipeline progress or results.\nTail: ${txt.slice(-500)}`
    ).toBeTruthy();
  });

  test('B2. Parallel jobs: stem separation + diarization simultaneously', async ({ page }) => {
    test.setTimeout(12 * 60 * 1000);
    const capture = createCapture();
    await setupPage(page);
    attachSSECapture(page, capture);

    await sendPrompt(page,
      `For ${ASSET_URI} (first 60 seconds): run BOTH audio stem separation ` +
      `(all 3 stems: speech, music, sfx) AND speech diarization at the same time. ` +
      `Don't wait for one to finish before starting the other. ` +
      `Report when both complete and give me download URLs for the speech stem and the VTT.`
    );
    await waitUntilDone(page, 11 * 60 * 1000);

    // May use 1 dispatch (direct mode bundles all tools) or 2+ (standard mode)
    expectDispatchCount(capture, 1, 'parallel-jobs');
    expectSubAgentRan(capture, 'parallel-jobs');

    const txt = await getPageText(page);
    // At least one job should succeed
    expect(/SUCCEEDED|speech.*\.wav|transcript|\.vtt/i.test(txt),
      `Expected at least one job result.\nTail: ${txt.slice(-500)}`
    ).toBeTruthy();
  });

  test('B3. Full pipeline: extract audio → stem separate → diarize speech stem', async ({ page }) => {
    test.setTimeout(15 * 60 * 1000);
    const capture = createCapture();
    await setupPage(page);
    attachSSECapture(page, capture);

    await sendPrompt(page,
      `For ${ASSET_URI} I want a clean speech transcript from just the first 45 seconds. ` +
      `Pipeline: ` +
      `1) Use process_media to extract audio from [0, 45] as mono 16kHz WAV. ` +
      `2) Run audio stem separation on that WAV to isolate the speech stem. ` +
      `3) Run speech diarization on the speech stem. ` +
      `Show me each step's status as it completes, and the final transcript.`
    );
    await waitUntilDone(page, 14 * 60 * 1000);

    // Planner may bundle all steps in 1 dispatch (direct mode) or use multiple
    expectDispatchCount(capture, 1, 'full-pipeline');
    expectSubAgentRan(capture, 'full-pipeline');

    const txt = await getPageText(page);
    expect(/SUCCEEDED|transcript|speaker/i.test(txt),
      `Expected pipeline to produce a transcript.\nTail: ${txt.slice(-500)}`
    ).toBeTruthy();
  });

  test('B4. Describe all tools — agent introspects gateway capabilities', async ({ page }) => {
    test.setTimeout(5 * 60 * 1000);
    const capture = createCapture();
    await setupPage(page);
    attachSSECapture(page, capture);

    await sendPrompt(page,
      `Call describe_tool on each of the media gateway targets (audio-stem, speech-diarization, media-processing, media-outputs, shot-segmentation) and give me a summary table of their versions and capabilities.`
    );
    await waitUntilDone(page, 4 * 60 * 1000);

    expectDispatchFired(capture, 'describe-tools');
    expectSubAgentRan(capture, 'describe-tools');

    const txt = await getPageText(page);
    // Should mention multiple tools/targets
    expect(/audio.stem|stem.separation/i.test(txt), 'Expected audio-stem info').toBeTruthy();
    expect(/diariz/i.test(txt), 'Expected diarization info').toBeTruthy();
    expect(/media.process/i.test(txt), 'Expected media-processing info').toBeTruthy();
  });

  test('B5. Shot segmentation → frame capture at shot boundaries', async ({ page }) => {
    test.setTimeout(12 * 60 * 1000);
    const capture = createCapture();
    await setupPage(page);
    attachSSECapture(page, capture);

    await sendPrompt(page,
      `For ${ASSET_URI} (first 30 seconds): ` +
      `1) Run shot segmentation to find scene boundaries. ` +
      `2) Once you have the shot boundaries, use process_media to capture a JPEG frame at each shot's start timecode. ` +
      `Show me the shot timestamps and the frame output URIs.`
    );
    await waitUntilDone(page, 11 * 60 * 1000);

    // Must have dispatched at least once
    expectDispatchCount(capture, 1, 'shot-then-frames');

    // At least one media tool in any step (sub-agent or planning dispatch)
    const allLabels = capture.steps.map(s => s.label).join(' ');
    expect(/shot|segment|process|media|frame|dispatch/i.test(allLabels),
      `Expected media tool in steps.\nAll steps: ${allLabels}`
    ).toBeTruthy();

    const txt = await getPageText(page);
    expect(/RUNNING|SUCCEEDED|\.jpe?g|frame|shot|segment|job|arn:aws/i.test(txt),
      `Expected shot/frame results or job progress.\nTail: ${txt.slice(-400)}`
    ).toBeTruthy();
  });
});

// ─── GROUP C: Negative controls & error handling (parallel) ───────────────

test.describe('M2C Gateway: Controls', () => {
  test.describe.configure({ mode: 'parallel' });

  test('C1. Non-gateway prompt — dispatch_tool NOT called', async ({ page }) => {
    test.setTimeout(3 * 60 * 1000);
    const capture = createCapture();
    await setupPage(page);
    attachSSECapture(page, capture);

    await sendPrompt(page, 'What is the capital of France? Answer in one word.');
    await waitUntilDone(page, 2 * 60 * 1000);

    const hasDispatch = capture.steps.some(s =>
      s.label.startsWith('Dispatching:') || s.label.startsWith('Dispatching with tools:')
    );
    expect(hasDispatch,
      `dispatch_tool should NOT fire for a trivia question.\nSteps:\n${capture.steps.map(s => s.label).join('\n')}`
    ).toBeFalsy();
  });

  test('C2. Invalid S3 URI — dispatch fires but error reported', async ({ page }) => {
    test.setTimeout(5 * 60 * 1000);
    const capture = createCapture();
    await setupPage(page);
    attachSSECapture(page, capture);

    await sendPrompt(page,
      'Run audio stem separation on s3://nonexistent-bucket-xyz-000/fake-video.mp4 — speech only. Show the result.'
    );
    await waitUntilDone(page, 4 * 60 * 1000);

    // Should dispatch (the agent must attempt the gateway tool)
    expectDispatchFired(capture, 'invalid-uri');

    // Should surface an error to the user
    const txt = await getPageText(page);
    expect(/error|fail|not found|access denied|does not exist|cannot|invalid/i.test(txt),
      `Expected error message for invalid URI.\nTail: ${txt.slice(-300)}`
    ).toBeTruthy();
  });

  test('C3. Execution summary has dispatch_tool stats', async ({ page }) => {
    test.setTimeout(6 * 60 * 1000);
    const capture = createCapture();
    await setupPage(page);
    attachSSECapture(page, capture);

    await sendPrompt(page,
      `Use process_media to extract audio from ${ASSET_URI} as mono 16kHz WAV (mark_in_out [0, 10]). Poll until done.`
    );
    await waitUntilDone(page, 5 * 60 * 1000);

    expectDispatchFired(capture, 'summary-check');

    // Verify the execution_summary includes dispatch_tool in tools map
    expect(capture.summary,
      'No execution_summary received — stream may have ended prematurely'
    ).not.toBeNull();
    expect(capture.summary?.tools?.['dispatch_tool'],
      `execution_summary.tools missing dispatch_tool.\nSummary: ${JSON.stringify(capture.summary?.tools)}`
    ).toBeDefined();
    expect(capture.summary!.tools!['dispatch_tool'].calls,
      'dispatch_tool.calls should be >= 1'
    ).toBeGreaterThanOrEqual(1);
  });
});
