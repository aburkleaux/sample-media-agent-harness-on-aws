// @vitest-environment jsdom
import '@testing-library/jest-dom';
import React from 'react';
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { theme } from '../../styles/tokens';
import CropOverlay, { contentRect, clampToContent, toNormalized, fitAspect, outputPixels } from './CropOverlay';

afterEach(cleanup);

// --- pure geometry (no DOM) ---------------------------------------------

describe('contentRect (letterbox mapping)', () => {
  it('fills exactly when element matches video aspect (no bars)', () => {
    const c = contentRect(640, 360, 1280, 720);
    expect(c).toMatchObject({ x: 0, y: 0, w: 640, h: 360 });
  });
  it('letterboxes (bars top/bottom) when element is taller than the video', () => {
    const c = contentRect(640, 480, 1280, 720); // 4:3 element, 16:9 video
    expect(c.w).toBe(640);
    expect(Math.round(c.h)).toBe(360);
    expect(c.y).toBeGreaterThan(0);          // top bar
    expect(c.x).toBe(0);
  });
  it('pillarboxes (bars left/right) when element is wider than the video', () => {
    const c = contentRect(800, 360, 1280, 720);
    expect(Math.round(c.w)).toBe(640);
    expect(c.h).toBe(360);
    expect(c.x).toBeGreaterThan(0);          // side bar
  });
  it('guards zero dimensions', () => {
    expect(contentRect(0, 0, 1280, 720)).toMatchObject({ w: 0, h: 0 });
  });
});

describe('clampToContent', () => {
  const content = { x: 0, y: 0, w: 640, h: 360 };
  it('keeps a box inside the content rect', () => {
    const b = clampToContent({ x: 600, y: 340, w: 200, h: 200 }, content);
    expect(b.x + b.w).toBeLessThanOrEqual(640);
    expect(b.y + b.h).toBeLessThanOrEqual(360);
  });
  it('enforces a minimum size', () => {
    const b = clampToContent({ x: 10, y: 10, w: 2, h: 2 }, content, 24);
    expect(b.w).toBeGreaterThanOrEqual(24);
    expect(b.h).toBeGreaterThanOrEqual(24);
  });
});

describe('toNormalized', () => {
  it('maps a content-relative box to 0..1', () => {
    const content = { x: 0, y: 0, w: 640, h: 360 };
    const n = toNormalized({ x: 128, y: 54, w: 352, h: 216 }, content);
    expect(n.x).toBeCloseTo(0.2, 5);
    expect(n.y).toBeCloseTo(0.15, 5);
    expect(n.w).toBeCloseTo(0.55, 5);
    expect(n.h).toBeCloseTo(0.6, 5);
  });
  it('subtracts the letterbox offset', () => {
    const content = { x: 0, y: 60, w: 640, h: 360 }; // 60px top bar
    const n = toNormalized({ x: 0, y: 60, w: 640, h: 360 }, content);
    expect(n).toMatchObject({ x: 0, y: 0, w: 1, h: 1 });
  });
});

describe('fitAspect', () => {
  const content = { x: 0, y: 0, w: 640, h: 360 };
  it('is a no-op for Free (ar=0)', () => {
    const b = { x: 10, y: 10, w: 100, h: 200 };
    expect(fitAspect(b, 0, content)).toBe(b);
  });
  it('fits a 9:16 box within a 16:9 frame, centered, preserving ratio', () => {
    const b = fitAspect({ x: 100, y: 100, w: 300, h: 100 }, 9 / 16, content);
    expect(b.w / b.h).toBeCloseTo(9 / 16, 3);
    expect(b.h).toBeLessThanOrEqual(360);
    expect(b.w).toBeLessThanOrEqual(640);
  });
});

describe('outputPixels', () => {
  it('reports even-snapped source pixels', () => {
    const content = { x: 0, y: 0, w: 640, h: 360 };
    const out = outputPixels({ x: 0, y: 0, w: 352, h: 216 }, content, 1280, 720);
    expect(out.w % 2).toBe(0);
    expect(out.h % 2).toBe(0);
    expect(out.w).toBe(704);   // 0.55 * 1280
    expect(out.h).toBe(432);   // 0.60 * 720
  });
});

// --- component smoke (jsdom, mocked geometry) ---------------------------

function mountActive(compact = false, withOnChange = true) {
  // 16:9 element matching a 1280x720 video → no letterbox, default box = 20/15/55/60%.
  const rectSpy = vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({
    width: 640, height: 360, left: 0, top: 0, right: 640, bottom: 360, x: 0, y: 0, toJSON() {},
  } as DOMRect);
  const video = document.createElement('video');
  Object.defineProperty(video, 'videoWidth', { value: 1280, configurable: true });
  Object.defineProperty(video, 'videoHeight', { value: 720, configurable: true });
  const videoRef = { current: video } as React.RefObject<HTMLMediaElement>;
  const onCommit = vi.fn();
  const onCancel = vi.fn();
  const onChange = vi.fn();
  render(<CropOverlay videoRef={videoRef} theme={theme} onCommit={onCommit} onCancel={onCancel}
    onChange={withOnChange ? onChange : undefined} compact={compact} />);
  return { onCommit, onCancel, onChange, rectSpy };
}

describe('CropOverlay component', () => {
  it('renders the selection, presets and confirm once laid out', async () => {
    // Single-video (confirm-to-commit) mode: no onChange → the ✓ Crop confirm is shown.
    mountActive(false, false);
    await waitFor(() => expect(screen.getByTestId('crop-selection')).toBeInTheDocument());
    expect(screen.getByTestId('crop-preset-9:16')).toBeInTheDocument();
    expect(screen.getByTestId('crop-confirm')).toBeInTheDocument();
    expect(screen.getByTestId('crop-cancel')).toBeInTheDocument();
  });

  it('hides the per-overlay ✓ Crop in multi-view (onChange live-commit) mode', async () => {
    // Gallery multi-view: onChange live-commits the box and "Crop all" applies them, so the
    // redundant per-card confirm is not rendered (presets + cancel remain).
    mountActive(true, true);
    await waitFor(() => expect(screen.getByTestId('crop-selection')).toBeInTheDocument());
    expect(screen.getByTestId('crop-preset-9:16')).toBeInTheDocument();
    expect(screen.queryByTestId('crop-confirm')).toBeNull();
    expect(screen.getByTestId('crop-cancel')).toBeInTheDocument();
  });

  it('commits a normalized 0..1 box (≈ the default 20/15/55/60%)', async () => {
    const { onCommit } = mountActive(false, false);
    await waitFor(() => expect(screen.getByTestId('crop-selection')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('crop-confirm'));
    expect(onCommit).toHaveBeenCalledTimes(1);
    const box = onCommit.mock.calls[0][0];
    for (const k of ['x', 'y', 'w', 'h']) {
      expect(box[k]).toBeGreaterThanOrEqual(0);
      expect(box[k]).toBeLessThanOrEqual(1);
    }
    expect(box.w).toBeCloseTo(0.55, 2);
    expect(box.h).toBeCloseTo(0.6, 2);
  });

  it('cancels on Escape', async () => {
    const { onCancel } = mountActive();
    await waitFor(() => expect(screen.getByTestId('crop-selection')).toBeInTheDocument());
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('selecting a preset marks it active', async () => {
    mountActive(false, false);
    await waitFor(() => expect(screen.getByTestId('crop-selection')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('crop-preset-1:1'));
    // the chip reflects the active aspect via its border/background (primary) — assert it stays mounted
    expect(screen.getByTestId('crop-preset-1:1')).toBeInTheDocument();
    // and a fresh commit still yields an in-range box
    fireEvent.click(screen.getByTestId('crop-confirm'));
  });
});

describe('CropOverlay compact (gallery variant)', () => {
  it('keeps all presets + only the SE corner (no edges / other corners)', async () => {
    mountActive(true);
    await waitFor(() => expect(screen.getByTestId('crop-selection')).toBeInTheDocument());
    for (const p of ['Free', '16:9', '1:1', '9:16']) {
      expect(screen.getByTestId(`crop-preset-${p}`)).toBeInTheDocument();
    }
    expect(screen.getByTestId('crop-handle-se')).toBeInTheDocument();      // aspect-preserving corner
    expect(screen.queryByTestId('crop-handle-nw')).toBeNull();             // other corners omitted
    expect(screen.queryByTestId('crop-edge-e')).toBeNull();                // edges omitted (too small)
  });

  it('a preset changes the box aspect', async () => {
    const { onCommit } = mountActive(true, false);
    await waitFor(() => expect(screen.getByTestId('crop-selection')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('crop-preset-1:1'));                // pixel-square in a 16:9 frame
    fireEvent.click(screen.getByTestId('crop-confirm'));
    const box = onCommit.mock.calls[0][0];
    // 1:1 pixels => w*1280 ≈ h*720 => w/h ≈ 720/1280 = 0.5625 (normalized)
    expect(box.w / box.h).toBeCloseTo(0.5625, 2);
  });

  it('a 9:16 preset commits a FULL-HEIGHT 9:16 box (w≈0.316, h≈1.0) — the ratio is NOT the width', async () => {
    // The efa3569d regression: a 9:16 selection on a 16:9 clip must emit a tall, narrow box
    // (w≈0.316, full height), NOT width=9/16=0.5625 (which crops a square). Locks the UI end.
    const { onCommit } = mountActive(true, false);
    await waitFor(() => expect(screen.getByTestId('crop-selection')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('crop-preset-9:16'));
    fireEvent.click(screen.getByTestId('crop-confirm'));
    const box = onCommit.mock.calls[0][0];
    expect(box.h).toBeCloseTo(1.0, 2);                       // spans the full frame height
    expect(box.w).toBeCloseTo(0.316, 2);                     // 405px of 1280 → 0.316
    expect(box.w).not.toBeCloseTo(0.5625, 2);                // NOT the ratio-as-width reversal
    // Output aspect in real pixels is a true 9:16 portrait.
    expect((box.w * 1280) / (box.h * 720)).toBeCloseTo(9 / 16, 2);
  });

  it('drag-moves the box', async () => {
    mountActive(true);
    await waitFor(() => expect(screen.getByTestId('crop-selection')).toBeInTheDocument());
    const sel = screen.getByTestId('crop-selection');
    const before = sel.style.left;
    act(() => {
      fireEvent.pointerDown(sel, { clientX: 200, clientY: 150 });
      // The move/up listeners are on window; MouseEvent carries clientX in jsdom.
      window.dispatchEvent(new MouseEvent('pointermove', { clientX: 260, clientY: 150 }));
      window.dispatchEvent(new MouseEvent('pointerup', {}));
    });
    expect(screen.getByTestId('crop-selection').style.left).not.toBe(before); // box moved right
  });

  it('commits the box LIVE on a preset pick — no confirm click needed (enables "Crop all")', async () => {
    const { onChange } = mountActive(true);
    await waitFor(() => expect(screen.getByTestId('crop-selection')).toBeInTheDocument());
    onChange.mockClear();                                   // ignore anything before the gesture
    fireEvent.click(screen.getByTestId('crop-preset-9:16'));
    expect(onChange).toHaveBeenCalled();                   // committed without ✓ Crop
    const calls = onChange.mock.calls;
    const box = calls[calls.length - 1][0];
    expect(box.w).toBeCloseTo(0.316, 2);                   // a real 9:16 box, live
    expect(box.h).toBeCloseTo(1.0, 2);
  });

  it('commits the box LIVE on a drag-settle (pointerup), not only on confirm', async () => {
    const { onChange } = mountActive(true);
    await waitFor(() => expect(screen.getByTestId('crop-selection')).toBeInTheDocument());
    onChange.mockClear();
    const sel = screen.getByTestId('crop-selection');
    act(() => {
      fireEvent.pointerDown(sel, { clientX: 200, clientY: 150 });
      window.dispatchEvent(new MouseEvent('pointermove', { clientX: 260, clientY: 150 }));
      window.dispatchEvent(new MouseEvent('pointerup', {}));
    });
    expect(onChange).toHaveBeenCalled();                   // drawing alone committed the box
  });
});

describe('CropOverlay icons', () => {
  it('confirm and cancel draw SVG icons, not ✓ / ✕ glyphs; cancel has an accessible name', async () => {
    mountActive(false, false);
    await waitFor(() => expect(screen.getByTestId('crop-confirm')).toBeInTheDocument());
    for (const id of ['crop-confirm', 'crop-cancel']) {
      const el = screen.getByTestId(id);
      expect(el.querySelector('svg'), id).not.toBeNull();
      expect(el.textContent ?? '', id).not.toMatch(/[✓✕×✂▢]/);
    }
    expect(screen.getByTestId('crop-confirm')).toHaveTextContent(/^Crop$/);
    expect(screen.getByTestId('crop-cancel')).toHaveAccessibleName('Cancel crop');
    // The size readout is plain text: no leading → arrow glyph.
    expect(screen.getByTestId('crop-readout').textContent ?? '').toMatch(/^\d+ × \d+$/);
  });
});

describe('CropOverlay confirm', () => {
  it('the single-video timeline shows Crop even when it saves the box as it changes (onChange)', async () => {
    const { render, screen } = await import('@testing-library/react');
    const React = await import('react');
    const { default: CropOverlay } = await import('./CropOverlay');
    const { theme } = await import('../../styles/tokens');
    const video = document.createElement('video');
    render(React.createElement(CropOverlay, { videoRef: { current: video }, theme: theme as never,
      onCommit: () => {}, onCancel: () => {}, onChange: () => {}, initialBox: { x: 0.1, y: 0.1, w: 0.5, h: 0.5 } }));
    expect(await screen.findByTestId('crop-confirm')).toBeInTheDocument();
  });
});
