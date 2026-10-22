/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { classifyUrl, filenameOf, deriveArtifactKey, downloadFile } from './artifactUrl';

describe('classifyUrl — extension-driven, host-agnostic', () => {
  it('classifies images regardless of host (fal + cloudfront)', () => {
    expect(classifyUrl('https://v3b.fal.media/files/b/x/ZCxZs.png').kind).toBe('image');
    expect(classifyUrl('https://dexamplecdn002.cloudfront.net/artifacts/panel.jpg').kind).toBe('image');
  });
  it('classifies video/audio/document by extension', () => {
    expect(classifyUrl('https://cdn.cloudfront.net/a/clip.mp4').kind).toBe('video');
    expect(classifyUrl('https://cdn.cloudfront.net/a/stem.wav').kind).toBe('audio');
    expect(classifyUrl('https://cdn.cloudfront.net/artifacts/report.pdf').kind).toBe('document');
  });
  it('treats raw JSON as a plain link, never a viewable document (no generic-viewer tab)', () => {
    // JSON is plumbing (manifests, analysis sidecars, data) — never displayed by itself. It is a
    // download link, not a document, so it never opens a generic file-viewer tab.
    expect(classifyUrl('https://cdn.cloudfront.net/artifacts/shots.json').kind).toBe('link');
    expect(classifyUrl('https://cdn.cloudfront.net/a/analysis_metadata.json').kind).toBe('link');
    // csv stays a document (DataTable is a genuine view).
    expect(classifyUrl('https://cdn.cloudfront.net/a/results.csv').kind).toBe('document');
  });
  it('treats a non-media URL as a plain link', () => {
    expect(classifyUrl('https://example.com/some/page').kind).toBe('link');
  });
  it('ignores query strings for extension + filename', () => {
    const i = classifyUrl('https://x.cloudfront.net/artifacts/report.pdf?X-Amz-Signature=abc');
    expect(i.kind).toBe('document');
    expect(i.filename).toBe('report.pdf');
  });
});

describe('classifyUrl — trustedHost (auto-embed security gate)', () => {
  it('trusts cloudfront, fal.media, relative, and the given artifacts domain', () => {
    expect(classifyUrl('https://x.cloudfront.net/a.png').trustedHost).toBe(true);
    expect(classifyUrl('https://v3b.fal.media/a.png').trustedHost).toBe(true);
    expect(classifyUrl('/api/artifact?key=artifacts/a.png').trustedHost).toBe(true);
    expect(classifyUrl('https://d1.example.com/a.png', { artifactsDomain: 'd1.example.com' }).trustedHost).toBe(true);
  });
  it('does NOT trust an arbitrary host (tracking-pixel guard)', () => {
    const i = classifyUrl('https://tracker.evil.com/pixel.png');
    expect(i.kind).toBe('image');
    expect(i.trustedHost).toBe(false);
  });
});

describe('classifyUrl — ourArtifact (download strategy)', () => {
  it('marks proxy, artifacts/ key, and domain URLs as ours', () => {
    expect(classifyUrl('/api/artifact?key=artifacts/x.pdf').ourArtifact).toBe(true);
    expect(classifyUrl('https://x.cloudfront.net/artifacts/x.pdf').ourArtifact).toBe(true);
  });
  it('a fal media file is not our artifact', () => {
    expect(classifyUrl('https://v3b.fal.media/files/x.png').ourArtifact).toBe(false);
  });
});

describe('filenameOf', () => {
  it('extracts and decodes the last path segment', () => {
    expect(filenameOf('https://x/y/My%20File.pdf?z=1')).toBe('My File.pdf');
    expect(filenameOf('https://x/y/z/report.json')).toBe('report.json');
  });
});

describe('deriveArtifactKey', () => {
  it('derives the key from a cloudfront artifacts URL', () => {
    expect(deriveArtifactKey('https://d12.cloudfront.net/artifacts/x/report.pdf'))
      .toBe('artifacts/x/report.pdf');
  });
  it('reads the key param from a proxy URL', () => {
    expect(deriveArtifactKey('/api/artifact?key=artifacts/x.pdf')).toBe('artifacts/x.pdf');
  });
  it('strips a query string from the derived key', () => {
    expect(deriveArtifactKey('https://d12.cloudfront.net/artifacts/x.pdf?sig=1')).toBe('artifacts/x.pdf');
  });
  it('returns null for a non-artifact URL', () => {
    expect(deriveArtifactKey('https://v3b.fal.media/files/x.png')).toBeNull();
  });
});

describe('downloadFile', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('downloads via a blob on success', async () => {
    const blob = new Blob(['x']);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, blob: async () => blob }));
    const createObj = vi.fn().mockReturnValue('blob:obj');
    (URL as unknown as { createObjectURL: unknown }).createObjectURL = createObj;
    (URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn();
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    await downloadFile('https://x.cloudfront.net/artifacts/f.png', 'f.png');
    expect(createObj).toHaveBeenCalledWith(blob);
    expect(clickSpy).toHaveBeenCalled();
    clickSpy.mockRestore();
  });

  it('opens in a new tab when the fetch is blocked (non-CORS host)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('blocked')));
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    await downloadFile('https://evil.example.com/f.png', 'f.png');
    expect(openSpy).toHaveBeenCalledWith('https://evil.example.com/f.png', '_blank', 'noopener,noreferrer');
    openSpy.mockRestore();
  });
});

describe('a document on another site is a web page link (staging 2026-10-20)', () => {
  it('docs pages and public PDFs are plain links; our files keep the document card', () => {
    expect(classifyUrl('https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks.html').kind).toBe('link');
    expect(classifyUrl('https://example.com/whitepaper.pdf').kind).toBe('link');
    expect(classifyUrl('https://d12.cloudfront.net/projects/p0123456789abcdef0123456789abcdef/video:x/report.pdf').kind).toBe('document');
    expect(classifyUrl('https://d12.cloudfront.net/artifacts/s/output/report.html').kind).toBe('document');
    expect(classifyUrl('https://bucket.s3.amazonaws.com/artifacts/s/output/r.pdf?X-Amz-Signature=1').kind).toBe('document');
  });
});

