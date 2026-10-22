/**
 * The model badge must reflect the model the agent actually ran.
 *
 * Two bugs sat behind this. The badge fell back to the FIRST entry of the model
 * list when nothing was explicitly selected, so it advertised Claude 3 Haiku
 * while the agent ran Sonnet 5. And the fix initially published the model from
 * `finalizeStreamWithSummary`, which the live turn never calls — the summary is
 * captured in the streaming callback — so the publish silently never ran.
 *
 * This asserts the publish lives at the capture site, next to `receivedSummary`.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const src = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), './ChatCanvas.tsx'),
  'utf8',
);

describe('orchestrator model publish', () => {
  it('publishes from the stream callback that captures the summary', () => {
    const i = src.indexOf('receivedSummary = event.summary;');
    expect(i, 'summary capture site not found').toBeGreaterThan(-1);
    // The publish must be within a few lines of the capture, not in a helper the
    // live turn never invokes.
    const window_ = src.slice(i, i + 600);
    expect(window_).toContain('lab-orchestrator-model-active');
    expect(window_).toContain('orchestrator:model');
  });

  it('reads model_id off the summary rather than guessing', () => {
    expect(src).toMatch(/event\.summary\?\.model_id/);
  });

  it('does not publish from finalizeStreamWithSummary (never called live)', () => {
    const j = src.indexOf('const finalizeStreamWithSummary');
    expect(j).toBeGreaterThan(-1);
    // End at that useCallback's dependency array, whatever it currently lists.
    // Pinning the exact list ('}, [liveText, liveSteps]);') made this test slice
    // to end-of-file the moment a dependency was added — and the whole file
    // DOES contain 'orchestrator:model', so it failed for the wrong reason.
    const rest = src.slice(j);
    const end = rest.search(/\n\s*\}, \[[^\]]*\]\);/);
    expect(end).toBeGreaterThan(-1);
    expect(rest.slice(0, end)).not.toContain('orchestrator:model');
  });
});
