/**
 * @vitest-environment jsdom
 *
 * Image presentation in GenericPreview.
 *
 * A generated image arrived as a 132px grid tile cropped to a fixed 108px
 * height — a thumbnail of the very thing the user asked for. One image is the
 * RESULT; several are a gallery. Those are different layouts, not one layout
 * with a different item count.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import GenericPreview from './GenericPreview';
import { theme } from '../../../styles/tokens';

const manifest = (urls: string[]) => ({
  template: 'generic-preview',
  artifacts: urls.map((url) => ({ url, label: url.split('/').pop() ?? url })),
});

const renderPreview = (urls: string[]) =>
  render(
    <GenericPreview
      data={{ manifest: manifest(urls) } as never}
      title="Preview"
      theme={theme as never}
    />
  );

const IMG = 'https://cf/artifacts/external/abc.jpg';
const IMG2 = 'https://cf/artifacts/external/def.png';

describe('single image', () => {
  it('is not constrained to a thumbnail height', () => {
    renderPreview([IMG]);
    const img = screen.getByRole('img') as HTMLImageElement;
    expect(img.style.height).toBe('auto');
    expect(img.style.width).toBe('100%');
  });

  it('is not cropped', () => {
    /** `cover` crops to fill; the whole frame of a generated image is the point. */
    renderPreview([IMG]);
    expect((screen.getByRole('img') as HTMLImageElement).style.objectFit).toBe('contain');
  });

  it('is capped so a tall image cannot push the files row away', () => {
    renderPreview([IMG]);
    expect((screen.getByRole('img') as HTMLImageElement).style.maxHeight).toBe('62vh');
  });

  it('is still click-to-enlarge', () => {
    renderPreview([IMG]);
    expect(screen.getByLabelText(/Enlarge/)).toBeTruthy();
  });
});

describe('multiple images', () => {
  it('lays out as a gallery with room to see them', () => {
    renderPreview([IMG, IMG2]);
    const gallery = screen.getByTestId('image-gallery');
    // 132px made even a pair read as thumbnails.
    expect(gallery.style.gridTemplateColumns).toContain('200px');
  });

  it('renders every image', () => {
    renderPreview([IMG, IMG2]);
    expect(screen.getAllByRole('img')).toHaveLength(2);
  });

  it('uses an aspect ratio rather than a fixed pixel height', () => {
    /** A hard height letterboxes portrait images. */
    renderPreview([IMG, IMG2]);
    const img = screen.getAllByRole('img')[0] as HTMLImageElement;
    expect(img.style.aspectRatio).toBe('4 / 3');
    expect(img.style.height).toBe('');
  });
});

describe('non-images', () => {
  it('shows no gallery when there is nothing to show', () => {
    renderPreview(['https://cf/artifacts/data.csv']);
    expect(screen.queryByTestId('image-gallery')).toBeNull();
  });
});
