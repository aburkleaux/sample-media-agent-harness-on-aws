import { describe, expect, it } from 'vitest';
import { shouldAutoRender } from './autoRenderOnComplete';

describe('shouldAutoRender (auto-render job gate)', () => {
  it('fires when the harness discovered renderable artifacts', () => {
    expect(shouldAutoRender({ hasRenderableArtifacts: true })).toBe(true);
  });

  it('does not depend on a gateway or tool name', () => {
    const futureToolCapability = { hasRenderableArtifacts: true };
    expect(shouldAutoRender(futureToolCapability)).toBe(true);
  });

  it('does not fire when no renderable artifacts were discovered', () => {
    expect(shouldAutoRender({ hasRenderableArtifacts: false })).toBe(false);
  });

  it('fails closed when the capability is missing at runtime', () => {
    // @ts-expect-error — defensive compatibility with legacy SSE/job rows.
    expect(shouldAutoRender({})).toBe(false);
  });
});
