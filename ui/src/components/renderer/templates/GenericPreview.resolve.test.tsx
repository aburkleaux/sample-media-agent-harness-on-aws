/**
 * @vitest-environment jsdom
 *
 * ISSUE 2a: GenericPreview must presign its OWN artifact identifiers (through
 * useResolvedArtifactData), so the login-free deep-link path — where ManifestRouter mounts
 * templates with artifact resolution turned OFF — no longer hands a raw `artifacts/<key>` /
 * `/api/artifact?key=…` src to an <img>, which the auth-required proxy answered 401 `missing_token`.
 *
 * The resolve hook is mocked to a deterministic presigner so the test asserts the template renders
 * the RESOLVED url, independent of ManifestRouter.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { theme } from '../../../styles/tokens';

vi.mock('../../../artifacts/useArtifactMedia', () => ({
  useResolvedArtifactData: (value: unknown) => {
    const sign = (u: unknown) =>
      typeof u === 'string' && u.startsWith('artifacts/') ? `https://signed/${u}` : u;
    const walk = (n: unknown): unknown => {
      if (typeof n === 'string') return sign(n);
      if (Array.isArray(n)) return n.map(walk);
      if (n && typeof n === 'object') {
        return Object.fromEntries(
          Object.entries(n).map(([k, v]) => [k, k === 'url' || k === 'src' || k === 'sourceUrl' ? sign(v) : walk(v)]),
        );
      }
      return n;
    };
    return value == null ? value : walk(value);
  },
}));

import GenericPreview from './GenericPreview';

describe('GenericPreview — ISSUE 2a presign of raw artifact identifiers', () => {
  it('renders the presigned URL for a raw artifacts/<key> image, not the raw key', () => {
    const manifest = {
      template: 'generic-preview',
      artifacts: [{ url: 'artifacts/sess/output/a.png', label: 'a.png' }],
    };
    render(<GenericPreview data={{ manifest } as never} title="Preview" theme={theme as never} />);
    const img = screen.getByTestId('image-gallery').querySelector('img') as HTMLImageElement;
    expect(img.getAttribute('src')).toBe('https://signed/artifacts/sess/output/a.png');
  });

  it('passes an already-presigned CDN url through unchanged (no-op on the resolved path)', () => {
    const url = 'https://cf/artifacts/external/already.png';
    const manifest = { template: 'generic-preview', artifacts: [{ url, label: 'already.png' }] };
    render(<GenericPreview data={{ manifest } as never} title="Preview" theme={theme as never} />);
    const img = screen.getByTestId('image-gallery').querySelector('img') as HTMLImageElement;
    expect(img.getAttribute('src')).toBe(url);
  });
});
