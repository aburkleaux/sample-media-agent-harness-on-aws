import { describe, expect, it, vi, beforeEach } from 'vitest';

const m = vi.hoisted(() => ({ resolveLocations: vi.fn(), request: vi.fn() }));
vi.mock('./api', () => ({ resolveLocations: m.resolveLocations }));
vi.mock('./assetUrlBatcher', () => ({ request: m.request }));

import { isHtmlName, refreshEmbeddedUrls } from './htmlEmbeds';

const OLD = 'https://store.s3.amazonaws.com/artifacts/external/a.jpg?X-Amz-Date=20261015T020803Z&amp;X-Amz-Expires=21600';
const OTHER = 'https://elsewhere.s3.us-west-2.amazonaws.com/x/b.png?sig=1';

beforeEach(() => {
  vi.clearAllMocks();
  m.resolveLocations.mockResolvedValue({ ok: true, value: {
    'https://store.s3.amazonaws.com/artifacts/external/a.jpg?X-Amz-Date=20261015T020803Z&X-Amz-Expires=21600': 'image:a',
    [OTHER]: null,
  } });
  m.request.mockResolvedValue(new Map([['image:a', { ok: true, entry: { source: { url: 'https://fresh/a.jpg?s=1&t=2', expiresAt: 1 } } }]]));
});

describe('refreshEmbeddedUrls', () => {
  it('re-links an expired S3 image to a fresh view-time URL, leaving what it cannot resolve', async () => {
    const html = `<img src="${OLD}"><div style="background:url(${OTHER})"></div><img src="${OLD}">`;
    const out = await refreshEmbeddedUrls(html, 'p1');
    expect(m.resolveLocations).toHaveBeenCalledWith('p1', [
      'https://store.s3.amazonaws.com/artifacts/external/a.jpg?X-Amz-Date=20261015T020803Z&X-Amz-Expires=21600', OTHER]);
    expect(out).toBe(`<img src="https://fresh/a.jpg?s=1&amp;t=2"><div style="background:url(${OTHER})"></div><img src="https://fresh/a.jpg?s=1&amp;t=2">`);
  });

  it('a page with no S3 links is returned as it is, without a request', async () => {
    expect(await refreshEmbeddedUrls('<p>hi</p>', 'p1')).toBe('<p>hi</p>');
    expect(m.resolveLocations).not.toHaveBeenCalled();
  });

  it('only .html / .htm names are re-linked', () => {
    expect(isHtmlName('ai_summit_storyboard.html')).toBe(true);
    expect(isHtmlName('notes.HTM')).toBe(true);
    expect(isHtmlName('report.pdf')).toBe(false);
  });
});
