import { describe, it, expect } from 'vitest';
import { attachmentBubbleText, attachmentPromptBlock, buildAttachmentPrompt, deriveInitialSessionTitle, enqueueMessage, formatAttachmentSize, isValidChatInput, MAX_QUEUE_SIZE, mergeSessionMessages, type QueuedMessage } from './ChatCanvas';
import type { ChatMessage } from '../lab-api';
import { normalizeUserTurnContent } from '../promptNormalize';

const m = (id: string, role: 'user' | 'assistant', ts: number): ChatMessage =>
  ({ id, role, content: `${role}-${id}`, timestamp: ts } as ChatMessage);
const mk = (id: string, role: 'user' | 'assistant', content: string, ts: number): ChatMessage =>
  ({ id, role, content, timestamp: ts } as ChatMessage);

describe('mergeSessionMessages — non-destructive live-refresh', () => {
  it('retains an in-memory turn the (stale) cache is missing', () => {
    // The bug: a background job completion fired the cache-refresh with a cache that lagged the
    // just-added assistant turn; replacing state dropped it. Merge must keep it.
    const current = [m('u1', 'user', 1), m('a1', 'assistant', 2), m('u2', 'user', 3), m('a2', 'assistant', 4)];
    const staleCache = [m('u1', 'user', 1), m('a1', 'assistant', 2), m('u2', 'user', 3)]; // missing a2
    const merged = mergeSessionMessages(current, staleCache);
    expect(merged.map(x => x.id)).toEqual(['u1', 'a1', 'u2', 'a2']); // a2 retained
  });

  it('adds a background-only turn from the cache and orders by timestamp', () => {
    const current = [m('u1', 'user', 1), m('a1', 'assistant', 2)];
    const cache = [m('u1', 'user', 1), m('a1', 'assistant', 2), m('bg', 'assistant', 3)]; // background summary
    const merged = mergeSessionMessages(current, cache);
    expect(merged.map(x => x.id)).toEqual(['u1', 'a1', 'bg']);
  });

  it('never drops messages regardless of cache order', () => {
    const current = [m('a', 'user', 5), m('b', 'assistant', 6)];
    const cache = [m('c', 'user', 1), m('a', 'user', 5)];
    const merged = mergeSessionMessages(current, cache);
    expect(new Set(merged.map(x => x.id))).toEqual(new Set(['a', 'b', 'c']));
    expect(merged.map(x => x.id)).toEqual(['c', 'a', 'b']); // by timestamp
  });
});

describe('mergeSessionMessages — union by id (proxy-authoritative single source)', () => {
  // Since the proxy became the authoritative transcript writer, the transcript has a SINGLE
  // source shape (`msg-` turns). The former `mem-`/provisional dedup + content normalization are
  // gone — merge is now a pure non-destructive union by id, used only to fold a background
  // cache-refresh into on-screen state without dropping an in-flight turn.
  it('returns the SAME array reference when the cache adds nothing (no flicker)', () => {
    const current = [mk('msg-1', 'user', 'hi', 1), mk('msg-2', 'assistant', 'yo', 2)];
    const merged = mergeSessionMessages(current, [mk('msg-1', 'user', 'hi', 1)]); // subset by id
    expect(merged).toBe(current);
  });

  it('keeps the in-memory copy on an id collision (in-memory wins)', () => {
    const current = [mk('msg-1', 'user', 'edited locally', 2)];
    const cache = [mk('msg-1', 'user', 'stale copy', 1)];
    const merged = mergeSessionMessages(current, cache);
    expect(merged).toHaveLength(1);
    expect(merged[0].content).toBe('edited locally');
  });

  it('adds a durable/cache-only turn (restored on switch-back) ordered by timestamp', () => {
    const current = [mk('msg-2', 'assistant', 'reply', 2)];
    const durable = [mk('msg-1', 'user', 'the question', 1), mk('msg-2', 'assistant', 'reply', 2)];
    const merged = mergeSessionMessages(current, durable);
    expect(merged.map(x => x.id)).toEqual(['msg-1', 'msg-2']); // restored user turn placed by ts
  });

  it('no longer dedups by content — two distinct ids with equal text are both kept', () => {
    // A user genuinely repeating a message keeps both; there is no second source to collapse.
    const current = [mk('msg-1', 'user', 'yes', 1), mk('msg-2', 'assistant', 'ok', 2), mk('msg-3', 'user', 'yes', 3)];
    const merged = mergeSessionMessages(current, []);
    expect(merged).toBe(current);
    expect(merged.map(x => x.id)).toEqual(['msg-1', 'msg-2', 'msg-3']);
  });

  it('ignores the third argument (liveAssistantContent is no longer consulted)', () => {
    const current = [mk('msg-u', 'user', 'go', 1)];
    const cache = [mk('msg-a', 'assistant', 'a reply', 2)];
    const merged = mergeSessionMessages(current, cache, 'a reply');
    expect(merged.map(x => x.id)).toEqual(['msg-u', 'msg-a']); // added regardless of live content
  });
});

describe('normalizeUserTurnContent', () => {
  it('strips the trailing [Current date: …] block', () => {
    expect(normalizeUserTurnContent('do X\n\n[Current date: 2026-09-02]')).toBe('do X');
  });
  it('trims-only when there is no augmentation', () => {
    expect(normalizeUserTurnContent('  do X  ')).toBe('do X');
  });
  it('strips a trailing [source: …] block', () => {
    expect(normalizeUserTurnContent('trim this [source: https://cf/x.mp4]')).toBe('trim this');
  });
  it('strips a trailing [sources: …] batch block', () => {
    expect(normalizeUserTurnContent('extract frames [sources: a=https://cf/x.mp4 @0s]')).toBe('extract frames');
  });
  it('strips a leading [File attached: …] block, leaving the typed message', () => {
    expect(normalizeUserTurnContent(
      '[File attached: clip.mp4 (1 MB)\nS3 key: k\nFull S3 URI: s3://b/k]\n\ntranscribe this',
    )).toBe('transcribe this');
  });
  it('strips every leading [File attached: …] block and 📎 line of a multi-file turn', () => {
    expect(normalizeUserTurnContent(
      '[File attached: a.png (0.1 MB)\nS3 bucket: b\nS3 key: k1\nFull S3 URI: s3://b/k1]\n\n'
      + '[File attached: b.png (0.2 MB)\nS3 key: k2]\n\ncompare these',
    )).toBe('compare these');
    expect(normalizeUserTurnContent('📎 a.png (0.1 MB)\n📎 b.png (0.2 MB)\ncompare these')).toBe('compare these');
    expect(normalizeUserTurnContent(attachmentBubbleText(
      [{ key: 'k', url: 'u', name: 'a.png', size: 1 }, { key: 'k2', url: 'u2', name: 'b.png', size: 2 }], 'hi',
    ))).toBe('hi');
    expect(normalizeUserTurnContent(buildAttachmentPrompt(
      [{ key: 'k', url: 'https://cdn/k', name: 'a.png', size: 1, bucket: 'b' }, { key: 'k2', url: '', name: 'b.png', size: 2 }], 'hi',
    ))).toBe('hi');
  });
  it('strips a leading 📎 chip line, leaving the typed message', () => {
    expect(normalizeUserTurnContent('📎 clip.mp4 (12.3 MB)\ntranscribe this')).toBe('transcribe this');
  });
  it('(b42731df) sizes small files in KB/B instead of "0.0 MB", and still normalizes', () => {
    expect(formatAttachmentSize(11890)).toBe('11.6 KB');
    expect(formatAttachmentSize(512)).toBe('512 B');
    expect(formatAttachmentSize(12.3 * 1024 * 1024)).toBe('12.3 MB');
    expect(formatAttachmentSize(3 * 1024 * 1024 * 1024)).toBe('3.00 GB');
    expect(formatAttachmentSize(NaN)).toBe('0 B');
    const files = [{ key: 'k', url: 'u', name: 'sam.webp', size: 11890 }, { key: 'k2', url: 'u2', name: 'images.webp', size: 6000 }];
    expect(attachmentBubbleText(files, 'hi')).toBe('📎 sam.webp (11.6 KB)\n📎 images.webp (5.9 KB)\nhi');
    expect(normalizeUserTurnContent(attachmentBubbleText(files, 'hi'))).toBe('hi');
    expect(normalizeUserTurnContent(buildAttachmentPrompt(files, 'hi'))).toBe('hi');
  });
});

describe('enqueueMessage', () => {
  const q = (prompt: string, displayText?: string): QueuedMessage => ({ prompt, ...(displayText ? { displayText } : {}) });

  it('adds a message to an empty queue', () => {
    const result = enqueueMessage([], q('hello'));
    expect(result).toEqual([q('hello')]);
  });

  it('preserves displayText through the queue (so a queued completion keeps its clean bubble)', () => {
    const result = enqueueMessage([], q('The X job/flow completed (arn:...). Fetch its results.', 'Show the X results'));
    expect(result[0].prompt).toContain('arn:');
    expect(result[0].displayText).toBe('Show the X results');
  });

  it('adds a message to a non-full queue', () => {
    const queue = [q('msg1'), q('msg2')];
    const result = enqueueMessage(queue, q('msg3'));
    expect(result).toEqual([q('msg1'), q('msg2'), q('msg3')]);
  });

  it('drops the message when queue is at max capacity', () => {
    const queue = Array.from({ length: MAX_QUEUE_SIZE }, (_, i) => q(`msg${i}`));
    const result = enqueueMessage(queue, q('overflow'));
    expect(result).toEqual(queue);
    expect(result).toHaveLength(MAX_QUEUE_SIZE);
  });

  it('allows adding up to exactly MAX_QUEUE_SIZE messages', () => {
    let queue: QueuedMessage[] = [];
    for (let i = 0; i < MAX_QUEUE_SIZE; i++) {
      queue = enqueueMessage(queue, q(`msg${i}`));
    }
    expect(queue).toHaveLength(MAX_QUEUE_SIZE);
  });

  it('does not mutate the original queue', () => {
    const queue = [q('a'), q('b')];
    const result = enqueueMessage(queue, q('c'));
    expect(queue).toEqual([q('a'), q('b')]);
    expect(result).toEqual([q('a'), q('b'), q('c')]);
  });

  it('MAX_QUEUE_SIZE is 10', () => {
    expect(MAX_QUEUE_SIZE).toBe(10);
  });
});

describe('isValidChatInput', () => {
  it('returns false for empty string', () => {
    expect(isValidChatInput('')).toBe(false);
  });

  it('returns false for whitespace-only string', () => {
    expect(isValidChatInput('   ')).toBe(false);
    expect(isValidChatInput('\t\n')).toBe(false);
    expect(isValidChatInput('  \n  \t  ')).toBe(false);
  });

  it('returns true for a valid message', () => {
    expect(isValidChatInput('hello')).toBe(true);
  });

  it('returns true for a message with leading/trailing whitespace', () => {
    expect(isValidChatInput('  hello  ')).toBe(true);
  });

  it('returns false for a string exceeding 10,000 characters', () => {
    const longString = 'a'.repeat(10_001);
    expect(isValidChatInput(longString)).toBe(false);
  });

  it('returns true for a string of exactly 10,000 characters', () => {
    const maxString = 'a'.repeat(10_000);
    expect(isValidChatInput(maxString)).toBe(true);
  });

  it('returns true for a single non-whitespace character', () => {
    expect(isValidChatInput('x')).toBe(true);
  });
});

describe('deriveInitialSessionTitle', () => {
  it('derives the first persisted title from valid text input', () => {
    expect(deriveInitialSessionTitle('hello staging')).toBe('hello staging');
  });

  it('uses a file name when the first submission has no text', () => {
    expect(deriveInitialSessionTitle('', 'sample-video.mp4')).toBe('sample-video.mp4');
  });

  it('preserves a locally renamed ephemeral session', () => {
    expect(deriveInitialSessionTitle('first prompt', undefined, 'My review')).toBe('My review');
  });

  it('truncates generated titles to the sidebar limit', () => {
    const title = deriveInitialSessionTitle('x'.repeat(50));
    expect(title).toBe(`${'x'.repeat(40)}…`);
  });
});

describe('attachmentPromptBlock — the attachment reference the model reads', () => {
  const ref = {
    key: 'projects/p1/image:7/poster.png',
    url: 'https://s3.example/projects/p1/image:7/poster.png?X-Amz-Signature=abc',
    name: 'poster.png',
    size: 1024,
    bucket: 'ingest',
    contentType: 'image/png',
    assetId: 'image:7',
    projectId: 'p1',
    sourceUri: 's3://ingest/projects/p1/image:7/poster.png',
  };

  it('names the project asset (asset-lineage R1.1), its s3:// URI, and the https URL for fetching', () => {
    expect(attachmentPromptBlock(ref)).toBe(
      '[File attached: poster.png (1.0 KB)\n'
      + 'project_id: p1\n'
      + 'asset_id: image:7\n'
      + 'S3 bucket: ingest\n'
      + 'S3 key: projects/p1/image:7/poster.png\n'
      + 'Full S3 URI: s3://ingest/projects/p1/image:7/poster.png\n'
      + 'Public HTTPS URL (use this as the image_url/video_url for generation models like fal.ai'
      + ' — do NOT try to read the s3:// URI): https://s3.example/projects/p1/image:7/poster.png?X-Amz-Signature=abc]',
    );
    expect(normalizeUserTurnContent(buildAttachmentPrompt([ref], 'describe this'))).toBe('describe this');
  });

  it('a plain chat upload (no asset) keeps the block it always had', () => {
    const plain = { key: 'artifacts/S1/input/1-a.png', url: 'https://cdn/x', name: 'a.png', size: 1024, bucket: 'art' };
    expect(attachmentPromptBlock(plain)).toContain('S3 bucket: art\nS3 key: artifacts/S1/input/1-a.png\n'
      + 'Full S3 URI: s3://art/artifacts/S1/input/1-a.png');
    expect(attachmentPromptBlock(plain)).not.toContain('asset_id');
  });
});
