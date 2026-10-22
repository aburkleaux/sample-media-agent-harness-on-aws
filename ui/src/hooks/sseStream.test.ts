// @vitest-environment jsdom
/**
 * The fetch-based SSE reader that replaced EventSource for `/jobs/stream` (finding #5: EventSource
 * cannot send a header, so the stream URL carried `?access_token=`), and the hook's transport: the
 * token goes in the Authorization header and NEVER in the URL.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { SseParser, readSseStream } from './sseStream';

vi.mock('../auth/authManager', () => ({ getAccessToken: vi.fn().mockResolvedValue('jwt-abc') }));

describe('SseParser', () => {
  it('parses named events, joins multi-line data, ignores comments and the heartbeat', () => {
    const p = new SseParser();
    const out = p.push(': ok\n\nevent: job.update\ndata: {"jobId":"j1"}\n\n:\n\nevent: job.terminal\ndata: a\ndata: b\n\n');
    expect(out).toEqual([
      { event: 'job.update', data: '{"jobId":"j1"}' },
      { event: 'job.terminal', data: 'a\nb' },
    ]);
  });

  it('buffers a frame split across chunks, and handles CRLF line endings', () => {
    const p = new SseParser();
    expect(p.push('event: job.up')).toEqual([]);
    expect(p.push('date\r\ndata: {"x":1}\r')).toEqual([]);
    expect(p.push('\n\r\n')).toEqual([{ event: 'job.update', data: '{"x":1}' }]);
  });

  it('defaults the event name to "message" and drops a frame with no data', () => {
    const p = new SseParser();
    expect(p.push('data: hi\n\nevent: only-name\n\n')).toEqual([{ event: 'message', data: 'hi' }]);
  });
});

function bodyOf(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(c) { for (const ch of chunks) c.enqueue(enc.encode(ch)); c.close(); },
  });
}

describe('readSseStream', () => {
  it('delivers every complete event and discards an unterminated trailing frame', async () => {
    const seen: string[] = [];
    await readSseStream(bodyOf(['event: job.update\nda', 'ta: 1\n\nevent: job.update\ndata: 2']), (e) => seen.push(e.data));
    expect(seen).toEqual(['1']);
  });
});

describe('useJobsSubscription — Authorization header, never ?access_token=', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.resetModules(); });

  it('opens /jobs/stream with the bearer header and a URL carrying no token', async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url: String(url), init });
      if (String(url).startsWith('/jobs/stream')) {
        return new Response(bodyOf([': ok\n\n']), { status: 200, headers: { 'content-type': 'text/event-stream' } });
      }
      return new Response('{"jobs":[]}', { status: 200, headers: { 'content-type': 'application/json' } });
    });
    vi.stubGlobal('fetch', fetchMock);
    const { useJobsSubscription } = await import('./useJobsSubscription');
    const { unmount } = renderHook(() => useJobsSubscription());
    await vi.waitFor(() => expect(calls.some((c) => c.url.startsWith('/jobs/stream'))).toBe(true));
    unmount();
    const stream = calls.find((c) => c.url.startsWith('/jobs/stream'))!;
    expect(stream.url).toBe('/jobs/stream');
    expect(stream.url).not.toContain('access_token');
    expect((stream.init.headers as Record<string, string>).authorization).toBe('Bearer jwt-abc');
  });
});
