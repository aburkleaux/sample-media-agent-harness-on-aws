import { describe, expect, it } from 'vitest';
import { attachmentKind } from './attachmentKind';

const CDN = 'https://dexamplecdn002.cloudfront.net/artifacts/fd4ce1bd/input';

describe('attachmentKind', () => {
  it('reads the real extension when the display name carries a " (n)" copy suffix (session fd4ce1bd)', () => {
    expect(attachmentKind({ url: `${CDN}/1791375015168-Netflix_Open_Content_Meridian.mp4`,
      name: 'Netflix_Open_Content_Meridian.mp4 (2)' })).toBe('video');
    expect(attachmentKind({ key: 'artifacts/s/input/pic.png', name: 'pic.png (3)' })).toBe('image');
    expect(attachmentKind({ name: 'take.wav (2)' })).toBe('audio');
  });

  it('prefers the URL path over the name and ignores a presign query', () => {
    expect(attachmentKind({ url: `${CDN}/a.mp4?X-Amz-Signature=1`, name: 'notes.txt' })).toBe('video');
    expect(attachmentKind({ url: `${CDN}/doc.pdf`, name: 'doc.pdf' })).toBe('other');
  });

  it('is "other" when nothing names a media type', () => {
    expect(attachmentKind({})).toBe('other');
    expect(attachmentKind({ name: 'clip (2)' })).toBe('other');
  });
});
