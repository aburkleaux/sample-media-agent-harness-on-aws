// @vitest-environment jsdom
/**
 * ClipEditor `embedded` (a Moment card) after the Moments simplification (ISSUE 4d). The staging
 * report was "the card looks busy" — so the embedded card is now a PLAIN, playable 16:9 media box
 * with NO per-card edit affordances: no crop / in / out / clear toolbar, no keep-range chip, no
 * range bar. A fixed-height 14px spacer keeps every card the same height. The media still seeds its
 * mark from a Moment's span (so Expand can preselect that range) and reports media events up.
 *
 * The FULL (non-embedded) ClipEditor keeps all of those controls — see ClipGallery.test.tsx.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeAll } from 'vitest';
import { render, screen } from '@testing-library/react';

import ClipEditor from './ClipEditor';
import { CARD_TRANSPORT_HEIGHT } from './CardTransport';
import { theme } from '../../styles/tokens';

beforeAll(() => {
  Object.defineProperty(window.HTMLMediaElement.prototype, 'currentTime', {
    configurable: true, writable: true, value: 0,
  });
});

const clip = { id: 'm1', label: 'nba.mp4', kind: 'video' as const, src: 'https://x/v.mp4' };

function editor(over: Partial<React.ComponentProps<typeof ClipEditor>> = {}) {
  const onMarkChange = vi.fn();
  const onEditedChange = vi.fn();
  const utils = render(
    <ClipEditor clip={clip} theme={theme} embedded onMarkChange={onMarkChange} onEditedChange={onEditedChange} {...over} />,
  );
  return { onMarkChange, onEditedChange, ...utils };
}

describe('ClipEditor embedded (simplified Moment card)', () => {
  it('is a plain 16:9 media box with no edit toolbar, keep chip or range bar', () => {
    editor({ initialMark: { in: 74, out: 96 } });
    expect(screen.getByTestId('clip-media-box-m1').style.aspectRatio).toBe('16 / 9');
    // None of the per-card edit affordances exist any more.
    for (const id of ['clip-toolbar-m1', 'clip-timecode-m1', 'clip-ruler-m1',
      'clip-crop-m1', 'clip-set-in-m1', 'clip-set-out-m1', 'clip-clear-m1']) {
      expect(screen.queryByTestId(id)).toBeNull();
    }
    expect(document.querySelector('[data-card-ruler]')).toBeNull();
    expect(document.querySelector('[data-card-edit]')).toBeNull();
  });

  it('keeps the card height uniform: a fixed-height transport row below the media box', () => {
    editor();
    // The transport (play / range / seek / mute, 2026-10-11) replaced the old 14px spacer; image and status
    // Moment cards reserve the same CARD_TRANSPORT_HEIGHT slot.
    expect(screen.getByTestId('card-transport-m1').style.height).toBe(`${CARD_TRANSPORT_HEIGHT}px`);
  });

  it('still reports the seeded Moment span up through onMarkChange (so Expand can preselect it)', () => {
    const { onMarkChange } = editor({ initialMark: { in: 74, out: 96 } });
    expect(onMarkChange).toHaveBeenLastCalledWith('m1', { in: 74, out: 96 });
  });

  it('renders a playable media element and stays un-edited (no controls to edit with)', () => {
    editor({ initialMark: { in: 1, out: 5 } });
    const root = screen.getByTestId('clip-card-m1');
    expect(root.querySelector('video')).not.toBeNull();
    expect(root).toHaveAttribute('data-edited', 'false');
  });

  it('has no glyph characters', () => {
    const { container } = editor({ initialMark: { in: 1, out: 5 } });
    expect(container.textContent).not.toMatch(/[\u21E4\u21E5\u2702\u25A2\u2715\u2922]/u);
  });
});

describe('ClipEditor playback asks for no CORS (staging 20fd99a7)', () => {
  it('an audio clip from a host with no CORS headers is a plain <audio>, so it loads and plays', () => {
    const audioClip = { id: 'v4', label: 'agent_olive_voice_v4', kind: 'audio' as const,
      src: 'https://m2c-proxy.s3.us-west-2.amazonaws.com/voices/x/preview.wav?X-Amz-Signature=a' };
    for (const embedded of [false, true]) {
      const { container, unmount } = render(<ClipEditor clip={audioClip} theme={theme} embedded={embedded} onMarkChange={vi.fn()} />);
      const el = container.querySelector('audio');
      expect(el).not.toBeNull();
      expect(el!.hasAttribute('crossorigin')).toBe(false);
      unmount();
    }
  });
});
