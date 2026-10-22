/**
 * The client's PROTECTED_PREFIXES must cover every path the proxy protects.
 *
 * These are two independent lists in two files. When they drift, the client sends
 * a request with no Authorization header to a route that requires one and the
 * feature 401s — silently, because nothing type-checks the pair. This happened
 * twice: `/agent-test` (caught in review) and `/render`, which I dropped during a
 * revert for "baseline fidelity" without noticing the proxy still protected it,
 * breaking every render with a 401.
 *
 * Reads the proxy source so the assertion tracks the real server list rather than
 * a copy that can rot the same way.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

function serverAuthPaths(): string[] {
  const src = readFileSync(resolve(here, '../../server/app.ts'), 'utf8');
  const m = src.match(/const authPaths\s*=\s*\[([^\]]*)\]/);
  if (!m) throw new Error('could not locate authPaths in ui/server/app.ts');
  return [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
}

function clientProtectedPrefixes(): string[] {
  const src = readFileSync(resolve(here, './authFetch.ts'), 'utf8');
  const m = src.match(/const PROTECTED_PREFIXES:[^=]*=\s*\[([\s\S]*?)\];/);
  if (!m) throw new Error('could not locate PROTECTED_PREFIXES in authFetch.ts');
  return [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
}

describe('authFetch protected prefixes vs proxy authPaths', () => {
  it('covers every server-protected path', () => {
    const server = serverAuthPaths();
    const client = clientProtectedPrefixes();
    // "/api" on the server is "/api/" on the client (it already ends in a slash),
    // so compare on a normalized form.
    const norm = (p: string) => p.replace(/\/$/, '');
    const clientNorm = new Set(client.map(norm));
    const missing = server.map(norm).filter((p) => !clientNorm.has(p));
    expect(missing, `client is missing token attachment for: ${missing.join(', ')}`).toEqual([]);
  });

  it('actually lists /render (the route the renderer tabs invoke)', () => {
    expect(clientProtectedPrefixes().map(p => p.replace(/\/$/, ''))).toContain('/render');
  });

  it('found non-empty lists on both sides', () => {
    // Guards against a regex that silently matches nothing and passes vacuously.
    expect(serverAuthPaths().length).toBeGreaterThan(3);
    expect(clientProtectedPrefixes().length).toBeGreaterThan(3);
  });
});
