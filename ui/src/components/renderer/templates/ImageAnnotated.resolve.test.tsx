/**
 * @vitest-environment jsdom
 *
 * ISSUE 2a: ImageAnnotated must presign its OWN still URLs (`src`, `images[].src`) through
 * useResolvedArtifactData, so the login-free deep-link path (ManifestRouter resolution OFF) no
 * longer hands a raw `artifacts/<key>` src to an <img> and gets 401 `missing_token`.
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
          Object.entries(n).map(([k, v]) => [k, k === 'src' ? sign(v) : walk(v)]),
        );
      }
      return n;
    };
    return value == null ? value : walk(value);
  },
}));

import ImageAnnotated from './ImageAnnotated';

describe('ImageAnnotated — ISSUE 2a presign of raw still URLs', () => {
  it('renders the presigned URL for a raw artifacts/<key> primary still', () => {
    render(
      <ImageAnnotated
        data={{ src: 'artifacts/sess/output/frame.jpg', annotations: [] } as never}
        title="Faces"
        theme={theme as never}
      />,
    );
    const img = screen.getByTestId('image-annotated-frame').querySelector('img') as HTMLImageElement;
    expect(img.getAttribute('src')).toBe('https://signed/artifacts/sess/output/frame.jpg');
  });

  it('passes an already-presigned url through unchanged (no-op on the resolved path)', () => {
    const url = 'https://x/f.jpg';
    render(<ImageAnnotated data={{ src: url, annotations: [] } as never} title="Faces" theme={theme as never} />);
    const img = screen.getByTestId('image-annotated-frame').querySelector('img') as HTMLImageElement;
    expect(img.getAttribute('src')).toBe(url);
  });
});
