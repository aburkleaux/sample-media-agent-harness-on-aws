/**
 * A minimal Server-Sent Events reader over `fetch`, so a stream can authenticate with an
 * `Authorization` header.
 *
 * Native `EventSource` cannot set request headers, which is why `/jobs/stream` used to take the
 * user's Cognito access token as `?access_token=` — a credential in a URL, where it reaches access
 * logs, browser history and anything that can read the URL. Reading the stream with `fetch` lets the
 * token travel in the header like every other request, and the proxy no longer accepts a query token
 * at all.
 *
 * Only what the proxy emits is needed: `event:` and `data:` fields (multi-line `data` joined with
 * `\n`), `:` comments (the heartbeat) ignored, blank line dispatches. `id:`/`retry:` are ignored —
 * the caller owns reconnection.
 */

export interface SseEvent {
  /** The `event:` field; `message` when the frame names none. */
  event: string;
  data: string;
}

/**
 * Incremental parser: feed text chunks as they arrive, get back every COMPLETE event. A partial
 * trailing frame is buffered until its blank line arrives.
 */
export class SseParser {
  private buffer = '';

  push(chunk: string): SseEvent[] {
    this.buffer += chunk;
    const out: SseEvent[] = [];
    // Frames end at a blank line; normalise CRLF / CR line endings first.
    this.buffer = this.buffer.replace(/\r\n?/g, '\n');
    let idx: number;
    while ((idx = this.buffer.indexOf('\n\n')) >= 0) {
      const frame = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + 2);
      const parsed = parseFrame(frame);
      if (parsed) out.push(parsed);
    }
    return out;
  }
}

function parseFrame(frame: string): SseEvent | null {
  let event = 'message';
  const data: string[] = [];
  let sawData = false;
  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon >= 0 ? line.slice(0, colon) : line;
    let value = colon >= 0 ? line.slice(colon + 1) : '';
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') event = value || 'message';
    else if (field === 'data') { data.push(value); sawData = true; }
  }
  return sawData ? { event, data: data.join('\n') } : null;
}

/**
 * Read an SSE response body to its end, calling `onEvent` for each event. Resolves when the server
 * closes the stream (or `signal` aborts); rejects on a network error mid-stream.
 */
export async function readSseStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (e: SseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  try {
    for (;;) {
      if (signal?.aborted) return;
      const { value, done } = await reader.read();
      // An unterminated trailing frame is discarded, as EventSource does.
      if (done) return;
      for (const e of parser.push(decoder.decode(value, { stream: true }))) onEvent(e);
    }
  } finally {
    try { reader.releaseLock(); } catch { /* already released */ }
  }
}
