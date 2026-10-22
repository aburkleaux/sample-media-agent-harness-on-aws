/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import TimelineEditor, { buildActionPrompt, tickStep } from './TimelineEditor';
import { TimelineEngine, type TimelineState } from './TimelineEngine';
import { theme } from '../../styles/tokens';

function makeEngine(duration = 60): TimelineEngine { return new TimelineEngine(duration); }
function renderWith(engine: TimelineEngine, onInteraction = vi.fn()) {
  const { rerender } = render(
    <TimelineEditor engine={engine} state={{ ...engine.getState(), duration: 60 }} duration={60} src="https://cdn/x.mp4" theme={theme} onInteraction={onInteraction} />,
  );
  const sync = () => rerender(
    <TimelineEditor engine={engine} state={{ ...engine.getState(), duration: 60 }} duration={60} src="https://cdn/x.mp4" theme={theme} onInteraction={onInteraction} />,
  );
  return { onInteraction, sync };
}

describe('buildActionPrompt (multi-range / multi-point)', () => {
  it('clip: whole clip, single range, N segments', () => {
    expect(buildActionPrompt('clip', [], [])).toBe('Clip the video.');
    expect(buildActionPrompt('clip', [{ in: 12, out: 18.5 }], [])).toBe('Clip the video from 00:12.000 to 00:18.500.');
    expect(buildActionPrompt('clip', [{ in: 12, out: 18.5 }, { in: 30, out: 35 }], []))
      .toBe('Clip the video into 2 segments: 00:12.000–00:18.500, 00:30.000–00:35.000.');
  });
  it('analyze verbs scope to range(s) or whole clip; frame lists points', () => {
    expect(buildActionPrompt('stems', [], [])).toBe('Separate the audio stems (speech, music, SFX) from the video.');
    expect(buildActionPrompt('stems', [{ in: 12, out: 18.5 }], [])).toContain('for the marked range 00:12.000–00:18.500');
    expect(buildActionPrompt('frame', [], [5, 14.2, 40])).toBe('Extract 3 still frames at 00:05.000, 00:14.200, 00:40.000.');
  });
  it('de-dupes identical ranges/points and threads the source', () => {
    expect(buildActionPrompt('clip', [{ in: 1, out: 2 }, { in: 1, out: 2 }], [])).toBe('Clip the video from 00:01.000 to 00:02.000.');
    expect(buildActionPrompt('frame', [], [5, 5])).toBe('Extract a still frame at 00:05.000.');
    // Source is NOT baked into the prompt text anymore — it is threaded via
    // TimelineActionData.src and appended to the AGENT prompt at send time (WorkspaceView),
    // never shown in the chat bubble. So the composed prompt stays clean regardless of src.
    expect(buildActionPrompt('clip', [{ in: 1, out: 2 }], [], 'https://cdn/v.mp4')).toBe('Clip the video from 00:01.000 to 00:02.000.');
  });
});

describe('tickStep', () => {
  it('grows with duration and is zero-safe', () => {
    expect(tickStep(8)).toBeLessThanOrEqual(tickStep(60));
    expect(tickStep(60)).toBeLessThanOrEqual(tickStep(600));
    expect(tickStep(0)).toBe(1);
  });
});

describe('TimelineEditor — lean action bar (3 buttons, no mark buttons)', () => {
  it('renders exactly Clip / Frame / Analyze plus ruler; NO mark/range/marker/clear buttons', () => {
    renderWith(makeEngine());
    for (const id of ['timeline-editor', 'timeline-ruler', 'timeline-playhead', 'timeline-timecode', 'action-clip', 'action-frame', 'analyze-menu']) {
      expect(screen.getByTestId(id)).toBeInTheDocument();
    }
    for (const gone of ['mark-in', 'mark-out', 'add-range', 'add-marker', 'mark-clear']) {
      expect(screen.queryByTestId(gone)).not.toBeInTheDocument();
    }
    expect(screen.getByTestId('timeline-timecode')).toHaveTextContent('00:00.000 / 01:00.000');
  });

  it('wheel over the ruler zooms; the compact level readout appears only above 1×', () => {
    renderWith(makeEngine());
    expect(screen.queryByTestId('zoom-level')).not.toBeInTheDocument(); // 1× is clean — no +/- buttons, no readout
    fireEvent.wheel(screen.getByTestId('timeline-ruler-viewport'), { deltaY: -100 });
    expect(screen.getByTestId('zoom-level')).toBeInTheDocument();
  });

  it('shows the selection pill (with clear) instead of the hint once something is marked', () => {
    const engine = makeEngine();
    engine.addRange(12, 18.5); engine.addRange(30, 35); engine.addPoint(5);
    const { sync } = renderWith(engine);
    sync();
    const pill = screen.getByTestId('selection-summary');
    expect(pill).toHaveTextContent('2 ranges');
    expect(pill).toHaveTextContent('1 marker');
    expect(screen.queryByTestId('ruler-hint')).not.toBeInTheDocument();
    expect(screen.getByTestId(`range-band-${engine.getState().ranges[0].id}`)).toBeInTheDocument();
  });
});

describe('TimelineEditor — ruler marking', () => {
  it('double-click drops a point marker; the pill clear resets everything', () => {
    const engine = makeEngine();
    const { sync } = renderWith(engine);
    fireEvent.doubleClick(screen.getByTestId('timeline-ruler'));
    expect(engine.getState().points).toHaveLength(1);
    sync();
    fireEvent.click(screen.getByTestId('selection-clear'));
    expect(engine.getState().points).toHaveLength(0);
    expect(engine.getState().ranges).toHaveLength(0);
  });

  it('a pointer-down on the ruler seeks and does not throw (jsdom 0-width)', () => {
    const engine = makeEngine();
    renderWith(engine);
    expect(() => fireEvent.pointerDown(screen.getByTestId('timeline-ruler'), { clientX: 40, pointerId: 1 })).not.toThrow();
    expect(engine.getState().currentTime).toBeGreaterThanOrEqual(0);
  });

  it('a committed range exposes remove ✕ that deletes it', () => {
    const engine = makeEngine();
    const id = engine.addRange(10, 20)!;
    const { sync } = renderWith(engine);
    sync();
    fireEvent.click(screen.getByTestId(`range-remove-${id}`));
    expect(engine.getState().ranges).toHaveLength(0);
  });
});

describe('TimelineEditor — actions push a live turn (onInteraction=action)', () => {
  it('Clip batches over committed ranges', () => {
    const engine = makeEngine();
    engine.addRange(12, 18.5); engine.addRange(30, 35);
    const { onInteraction, sync } = renderWith(engine);
    sync();
    fireEvent.click(screen.getByTestId('action-clip'));
    const evt = onInteraction.mock.calls[0][0];
    expect(evt.type).toBe('action');
    expect(evt.target).toBe('clip');
    expect(evt.data.prompt).toContain('2 segments');
    expect(evt.data.ranges).toHaveLength(2);
  });

  it('Frame button extracts stills at all committed points (bulk); no crop/thumbnail sub-menu', () => {
    const engine = makeEngine();
    engine.addPoint(5); engine.addPoint(14.2);
    const { onInteraction, sync } = renderWith(engine);
    sync();
    fireEvent.click(screen.getByTestId('action-frame')); // direct action, no menu
    expect(onInteraction.mock.calls[0][0].target).toBe('frame');
    expect(onInteraction.mock.calls[0][0].data.prompt).toContain('Extract 2 still frames at 00:05.000, 00:14.200.');
    // Frame is no longer a dropdown — crop/thumbnail variants are gone.
    expect(screen.queryByTestId('frame-menu-list')).not.toBeInTheDocument();
  });

  it('Analyze menu is portaled, opens, and an item emits its verb', () => {
    const { onInteraction } = renderWith(makeEngine());
    expect(screen.queryByTestId('analyze-menu-list')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('analyze-menu'));
    // portaled to document.body — still queryable via screen
    expect(screen.getByTestId('analyze-menu-list')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('analyze-item-stems'));
    const evt = onInteraction.mock.calls[0][0];
    expect(evt.target).toBe('stems');
    expect(evt.data.prompt).toContain('Separate the audio stems');
    expect(screen.queryByTestId('analyze-menu-list')).not.toBeInTheDocument(); // closes after select
  });
});

// Glyph characters (✂ ⧉ ✕ ▾) render differently per platform and ignore the theme colour; every
// control draws an inline SVG icon instead, and the icon-only ones carry an accessible name.
const GLYPHS = /[✂⧉✕×▾▢✓☑☐⇤⇥]/;
describe('TimelineEditor icons are SVG, never glyphs', () => {
  it('action buttons and remove controls carry an svg and no glyph text', () => {
    const engine = makeEngine();
    const rid = engine.addRange(10, 20)!;
    const pid = engine.addPoint(30)!;
    const { sync } = renderWith(engine);
    sync();
    for (const id of ['action-clip', 'action-frame', 'analyze-menu', `range-remove-${rid}`, `point-remove-${pid}`, 'selection-clear']) {
      const el = screen.getByTestId(id);
      expect(el.querySelector('svg'), id).not.toBeNull();
      expect(el.textContent ?? '', id).not.toMatch(GLYPHS);
    }
    expect(screen.getByTestId(`range-remove-${rid}`)).toHaveAccessibleName('Remove range');
    expect(screen.getByTestId(`point-remove-${pid}`)).toHaveAccessibleName('Remove marker');
  });
});
