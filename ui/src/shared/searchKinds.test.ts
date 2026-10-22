import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { SEARCH_KINDS } from './searchKinds';
import { SEARCH_KINDS as TAB_SEARCH_KINDS } from '../components/renderer/templates/momentsTabSearch';
import { PAGE_STRIDE_MS } from './pageLocator';

// One kind list across the agent, the proxy and the SPA (the agent and proxy tests assert the same file).
describe('search kinds', () => {
  const fixture = JSON.parse(readFileSync(join(__dirname, '../../../infra/lib/projects/fixtures/search-kinds.json'), 'utf8'));
  it('match the shared fixture everywhere the SPA lists them', () => {
    expect([...SEARCH_KINDS]).toEqual(fixture.kinds);
    expect(TAB_SEARCH_KINDS).toEqual(fixture.kinds); // the Moments tab's chips
  });
  it('share the document page stride', () => {
    expect(PAGE_STRIDE_MS).toBe(fixture.pageStrideMs);
  });
});
