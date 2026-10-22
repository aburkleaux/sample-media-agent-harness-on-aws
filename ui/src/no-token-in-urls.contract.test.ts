/**
 * Finding #5: the SPA must never put the user's Cognito access token in a URL (an iframe src, an
 * "Open" link, a stream URL). A URL is readable by the agent-written page it loads, and lands in
 * history, access logs and Referer headers. Frames and links carry a VIEW TICKET instead
 * (`resolveArtifactUrls(keys, {frame: true})`), and `/jobs/stream` is read with fetch + header.
 * The proxy no longer accepts `?access_token=` at all, so a regression here would also simply 401.
 *
 * Source scan (comments stripped), so a new URL builder fails the build rather than a review.
 */
import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

const SRC = path.resolve(__dirname);

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...sources(p));
    else if (/\.(ts|tsx)$/.test(e.name) && !/\.test\.(ts|tsx)$/.test(e.name)) out.push(p);
  }
  return out;
}

/** Code only: block comments and line comments removed (strings kept — that is where URLs live). */
function code(text: string): string {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .map((l) => l.replace(/(^|[^:'"`])\/\/.*$/, '$1'))
    .join('\n');
}

describe('no access token in any SPA URL', () => {
  it('no source builds a `?access_token=` / `&access_token=` URL or sets it as a search param', () => {
    const offenders: string[] = [];
    for (const f of sources(SRC)) {
      const c = code(fs.readFileSync(f, 'utf8'));
      if (/access_token=/.test(c) || /searchParams\.(set|append)\(\s*['"`]access_token/.test(c)) {
        offenders.push(path.relative(SRC, f));
      }
    }
    expect(offenders).toEqual([]);
  });

  it('nothing opens a native EventSource (it cannot send a header, which is what forced the query token)', () => {
    const offenders = sources(SRC).filter((f) => /new\s+EventSource\s*\(/.test(code(fs.readFileSync(f, 'utf8'))));
    expect(offenders.map((f) => path.relative(SRC, f))).toEqual([]);
  });
});
