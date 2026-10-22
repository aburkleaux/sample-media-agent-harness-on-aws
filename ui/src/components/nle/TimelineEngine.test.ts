import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  TimelineEngine,
  calculateTickInterval,
  TICK_INTERVALS,
  MIN_ZOOM,
  MAX_ZOOM,
  type TimelineState,
} from './TimelineEngine';

// ---------------------------------------------------------------------------
// Helper: create a mock HTMLMediaElement
// ---------------------------------------------------------------------------

function createMockMediaElement(options: {
  currentTime?: number;
  duration?: number;
  paused?: boolean;
  playbackRate?: number;
} = {}): HTMLMediaElement & { _trigger: (event: string) => void } {
  const listeners: Record<string, Array<() => void>> = {};
  const state = {
    currentTime: options.currentTime ?? 0,
    duration: options.duration ?? 120,
    paused: options.paused ?? true,
    playbackRate: options.playbackRate ?? 1,
  };

  const el = {
    get currentTime() { return state.currentTime; },
    set currentTime(v: number) { state.currentTime = v; },
    get duration() { return state.duration; },
    get paused() { return state.paused; },
    get playbackRate() { return state.playbackRate; },
    set playbackRate(v: number) { state.playbackRate = v; },
    play: vi.fn(() => {
      state.paused = false;
      listeners['play']?.forEach((fn) => fn());
    }),
    pause: vi.fn(() => {
      state.paused = true;
      listeners['pause']?.forEach((fn) => fn());
    }),
    addEventListener: vi.fn((event: string, fn: () => void) => {
      if (!listeners[event]) listeners[event] = [];
      listeners[event].push(fn);
    }),
    removeEventListener: vi.fn((event: string, fn: () => void) => {
      if (listeners[event]) {
        listeners[event] = listeners[event].filter((f) => f !== fn);
      }
    }),
    _trigger: (event: string) => {
      listeners[event]?.forEach((fn) => fn());
    },
  } as unknown as HTMLMediaElement & { _trigger: (event: string) => void };
  return el;
}

// ---------------------------------------------------------------------------
// TimelineEngine — State Management
// ---------------------------------------------------------------------------

describe('TimelineEngine', () => {
  let engine: TimelineEngine;

  beforeEach(() => {
    engine = new TimelineEngine(120);
  });

  afterEach(() => {
    engine.destroy();
  });

  describe('initial state', () => {
    it('should initialize with correct default state', () => {
      const state = engine.getState();
      expect(state.currentTime).toBe(0);
      expect(state.duration).toBe(120);
      expect(state.isPlaying).toBe(false);
      expect(state.playbackRate).toBe(1);
      expect(state.zoomLevel).toBe(100);
      expect(state.scrollOffset).toBe(0);
      expect(state.markIn).toBeNull();
      expect(state.markOut).toBeNull();
      expect(state.selectedTrackId).toBeNull();
      expect(state.selectedClipId).toBeNull();
      expect(state.selectedCueIndex).toBeNull();
    });

    it('should clamp negative duration to 0', () => {
      const e = new TimelineEngine(-5);
      expect(e.getState().duration).toBe(0);
      e.destroy();
    });
  });

  describe('subscription', () => {
    it('should notify subscribers on state change', () => {
      const subscriber = vi.fn();
      engine.subscribe(subscriber);
      engine.seek(10);
      expect(subscriber).toHaveBeenCalledWith(expect.objectContaining({ currentTime: 10 }));
    });

    it('should allow unsubscription', () => {
      const subscriber = vi.fn();
      const unsub = engine.subscribe(subscriber);
      unsub();
      engine.seek(10);
      expect(subscriber).not.toHaveBeenCalled();
    });
  });

  describe('seek', () => {
    it('should clamp seek to [0, duration]', () => {
      engine.seek(-5);
      expect(engine.getState().currentTime).toBe(0);

      engine.seek(999);
      expect(engine.getState().currentTime).toBe(120);

      engine.seek(60);
      expect(engine.getState().currentTime).toBe(60);
    });
  });

  describe('zoom', () => {
    it('should clamp zoom level to [MIN_ZOOM, MAX_ZOOM]', () => {
      engine.setZoomLevel(5);
      expect(engine.getState().zoomLevel).toBe(MIN_ZOOM);

      engine.setZoomLevel(1000);
      expect(engine.getState().zoomLevel).toBe(MAX_ZOOM);

      engine.setZoomLevel(200);
      expect(engine.getState().zoomLevel).toBe(200);
    });
  });

  describe('scroll', () => {
    it('should clamp scroll offset to >= 0', () => {
      engine.setScrollOffset(-10);
      expect(engine.getState().scrollOffset).toBe(0);

      engine.setScrollOffset(50);
      expect(engine.getState().scrollOffset).toBe(50);
    });
  });
});

// ---------------------------------------------------------------------------
// Mark In/Out Logic
// ---------------------------------------------------------------------------

describe('TimelineEngine — Mark In/Out', () => {
  let engine: TimelineEngine;

  beforeEach(() => {
    engine = new TimelineEngine(100);
  });

  afterEach(() => {
    engine.destroy();
  });

  it('should set markIn clamped to [0, duration]', () => {
    engine.setMarkIn(50);
    expect(engine.getState().markIn).toBe(50);

    engine.setMarkIn(-10);
    expect(engine.getState().markIn).toBe(0);

    engine.setMarkIn(200);
    expect(engine.getState().markIn).toBe(100);
  });

  it('should set markOut clamped to [0, duration]', () => {
    engine.setMarkOut(80);
    expect(engine.getState().markOut).toBe(80);

    engine.setMarkOut(-5);
    expect(engine.getState().markOut).toBe(0);

    engine.setMarkOut(150);
    expect(engine.getState().markOut).toBe(100);
  });

  it('should clear markOut if markIn >= markOut', () => {
    engine.setMarkOut(50);
    expect(engine.getState().markOut).toBe(50);

    engine.setMarkIn(50); // markIn == markOut → clear markOut
    expect(engine.getState().markIn).toBe(50);
    expect(engine.getState().markOut).toBeNull();
  });

  it('should clear markOut if markIn > markOut', () => {
    engine.setMarkOut(30);
    engine.setMarkIn(60); // markIn > markOut → clear markOut
    expect(engine.getState().markIn).toBe(60);
    expect(engine.getState().markOut).toBeNull();
  });

  it('should clear markIn if markOut <= markIn', () => {
    engine.setMarkIn(50);
    expect(engine.getState().markIn).toBe(50);

    engine.setMarkOut(50); // markOut == markIn → clear markIn
    expect(engine.getState().markIn).toBeNull();
    expect(engine.getState().markOut).toBe(50);
  });

  it('should clear markIn if markOut < markIn', () => {
    engine.setMarkIn(60);
    engine.setMarkOut(30); // markOut < markIn → clear markIn
    expect(engine.getState().markIn).toBeNull();
    expect(engine.getState().markOut).toBe(30);
  });

  it('should allow markOut without markIn set', () => {
    engine.setMarkOut(75);
    expect(engine.getState().markIn).toBeNull();
    expect(engine.getState().markOut).toBe(75);
  });

  it('should correctly set both marks when markIn < markOut', () => {
    engine.setMarkIn(20);
    engine.setMarkOut(80);
    expect(engine.getState().markIn).toBe(20);
    expect(engine.getState().markOut).toBe(80);
  });

  it('should clear both marks', () => {
    engine.setMarkIn(10);
    engine.setMarkOut(90);
    engine.clearMarks();
    expect(engine.getState().markIn).toBeNull();
    expect(engine.getState().markOut).toBeNull();
  });

  it('should emit InteractionEvent on mark change', () => {
    const handler = vi.fn();
    engine.onInteraction(handler);

    engine.setMarkIn(25);
    expect(handler).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'select',
        target: 'mark-range',
        data: { markIn: 25, markOut: null },
      }),
    );
  });

  it('should emit InteractionEvent on clearMarks', () => {
    const handler = vi.fn();
    engine.setMarkIn(10);
    engine.setMarkOut(50);
    engine.onInteraction(handler);

    engine.clearMarks();
    expect(handler).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'select',
        target: 'mark-range',
        data: { markIn: null, markOut: null },
      }),
    );
  });

  it('should use current playhead position when no argument provided', () => {
    engine.seek(42);
    engine.setMarkIn();
    expect(engine.getState().markIn).toBe(42);

    engine.seek(85);
    engine.setMarkOut();
    expect(engine.getState().markOut).toBe(85);
  });
});

// ---------------------------------------------------------------------------
// Duration Clamping
// ---------------------------------------------------------------------------

describe('TimelineEngine — Duration Clamping', () => {
  let engine: TimelineEngine;

  beforeEach(() => {
    engine = new TimelineEngine(0);
  });

  afterEach(() => {
    engine.destroy();
  });

  it('should use manifest duration when actual is not provided', () => {
    engine.setDuration(120);
    expect(engine.getState().duration).toBe(120);
  });

  it('should use manifest duration when difference <= 1s', () => {
    engine.setDuration(120, 119.5);
    expect(engine.getState().duration).toBe(120);
  });

  it('should clamp to actual duration when manifest exceeds by > 1s', () => {
    engine.setDuration(125, 120);
    expect(engine.getState().duration).toBe(120);
  });

  it('should clamp to actual when manifest exceeds by exactly > 1s', () => {
    engine.setDuration(121.5, 120);
    expect(engine.getState().duration).toBe(120);
  });

  it('should NOT clamp when manifest exceeds by exactly 1s', () => {
    engine.setDuration(121, 120);
    expect(engine.getState().duration).toBe(121);
  });

  it('should handle manifest < actual (no clamping needed)', () => {
    engine.setDuration(100, 120);
    expect(engine.getState().duration).toBe(100);
  });

  it('should clamp negative manifest duration to 0', () => {
    engine.setDuration(-5);
    expect(engine.getState().duration).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Tick Interval Calculation
// ---------------------------------------------------------------------------

describe('calculateTickInterval', () => {
  it('should return 1s at maximum zoom (500 px/s)', () => {
    expect(calculateTickInterval(500)).toBe(1);
  });

  it('should return 60s at minimum zoom (10 px/s)', () => {
    expect(calculateTickInterval(10)).toBe(60);
  });

  it('should always return a value from the valid set', () => {
    const validSet = new Set(TICK_INTERVALS);
    for (let zoom = MIN_ZOOM; zoom <= MAX_ZOOM; zoom += 5) {
      const interval = calculateTickInterval(zoom);
      expect(validSet.has(interval as typeof TICK_INTERVALS[number])).toBe(true);
    }
  });

  it('should produce monotonically non-increasing intervals as zoom increases', () => {
    let prev = Infinity;
    for (let zoom = MIN_ZOOM; zoom <= MAX_ZOOM; zoom += 1) {
      const interval = calculateTickInterval(zoom);
      expect(interval).toBeLessThanOrEqual(prev);
      prev = interval;
    }
  });

  it('should produce reasonable pixel spacing at mid zoom (100 px/s)', () => {
    const interval = calculateTickInterval(100);
    const pxSpacing = interval * 100;
    // At mid zoom, ticks should have at least 500px spacing (the target threshold)
    expect(pxSpacing).toBeGreaterThanOrEqual(500);
    // But not unreasonably large — at most the largest interval * zoom
    expect(pxSpacing).toBeLessThanOrEqual(60 * 100);
  });

  it('should handle zoom levels outside valid range by clamping', () => {
    // Below MIN_ZOOM — treated as MIN_ZOOM
    expect(calculateTickInterval(1)).toBe(calculateTickInterval(MIN_ZOOM));
    // Above MAX_ZOOM — treated as MAX_ZOOM
    expect(calculateTickInterval(999)).toBe(calculateTickInterval(MAX_ZOOM));
  });
});

// ---------------------------------------------------------------------------
// Media Element Integration
// ---------------------------------------------------------------------------

describe('TimelineEngine — Media Element', () => {
  let engine: TimelineEngine;

  beforeEach(() => {
    engine = new TimelineEngine(120);
    // Mock requestAnimationFrame for test environment
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      return setTimeout(() => cb(performance.now()), 16) as unknown as number;
    });
    vi.stubGlobal('cancelAnimationFrame', (id: number) => {
      clearTimeout(id);
    });
  });

  afterEach(() => {
    engine.destroy();
    vi.unstubAllGlobals();
  });

  it('should attach media element and listen for events', () => {
    const el = createMockMediaElement({ currentTime: 5, paused: true });
    engine.attachMedia(el);
    expect(el.addEventListener).toHaveBeenCalledWith('play', expect.any(Function));
    expect(el.addEventListener).toHaveBeenCalledWith('pause', expect.any(Function));
    expect(el.addEventListener).toHaveBeenCalledWith('ratechange', expect.any(Function));
    expect(el.addEventListener).toHaveBeenCalledWith('loadedmetadata', expect.any(Function));
  });

  it('should detach media element and remove listeners', () => {
    const el = createMockMediaElement();
    engine.attachMedia(el);
    engine.detachMedia();
    expect(el.removeEventListener).toHaveBeenCalledWith('play', expect.any(Function));
    expect(el.removeEventListener).toHaveBeenCalledWith('pause', expect.any(Function));
  });

  it('should update isPlaying on play/pause events', () => {
    const el = createMockMediaElement({ paused: true });
    engine.attachMedia(el);

    el.play();
    expect(engine.getState().isPlaying).toBe(true);

    el.pause();
    expect(engine.getState().isPlaying).toBe(false);
  });

  it('should seek by setting media element currentTime', () => {
    const el = createMockMediaElement({ currentTime: 0 });
    engine.attachMedia(el);
    engine.seek(30);
    expect(el.currentTime).toBe(30);
    expect(engine.getState().currentTime).toBe(30);
  });

  it('should set playback rate on media element', () => {
    const el = createMockMediaElement({ playbackRate: 1 });
    engine.attachMedia(el);
    engine.setPlaybackRate(2);
    expect(el.playbackRate).toBe(2);
    expect(engine.getState().playbackRate).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// Selection
// ---------------------------------------------------------------------------

describe('TimelineEngine — Selection', () => {
  let engine: TimelineEngine;

  beforeEach(() => {
    engine = new TimelineEngine(60);
  });

  afterEach(() => {
    engine.destroy();
  });

  it('should select and deselect tracks', () => {
    engine.selectTrack('track-1');
    expect(engine.getState().selectedTrackId).toBe('track-1');
    engine.selectTrack(null);
    expect(engine.getState().selectedTrackId).toBeNull();
  });

  it('should select and deselect clips', () => {
    engine.selectClip('clip-1');
    expect(engine.getState().selectedClipId).toBe('clip-1');
    engine.selectClip(null);
    expect(engine.getState().selectedClipId).toBeNull();
  });

  it('should select and deselect cues', () => {
    engine.selectCue(3);
    expect(engine.getState().selectedCueIndex).toBe(3);
    engine.selectCue(null);
    expect(engine.getState().selectedCueIndex).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Interaction events
// ---------------------------------------------------------------------------

describe('TimelineEngine — Interaction events', () => {
  let engine: TimelineEngine;

  beforeEach(() => {
    engine = new TimelineEngine(100);
  });

  afterEach(() => {
    engine.destroy();
  });

  it('should emit interaction events and allow unsubscription', () => {
    const handler = vi.fn();
    const unsub = engine.onInteraction(handler);
    engine.setMarkIn(10);
    expect(handler).toHaveBeenCalledTimes(1);

    unsub();
    engine.setMarkOut(50);
    expect(handler).toHaveBeenCalledTimes(1); // not called again
  });

  it('should include timestamp in interaction events', () => {
    const handler = vi.fn();
    engine.onInteraction(handler);
    const before = Date.now();
    engine.setMarkIn(10);
    const after = Date.now();

    const event = handler.mock.calls[0][0];
    expect(event.timestamp).toBeGreaterThanOrEqual(before);
    expect(event.timestamp).toBeLessThanOrEqual(after);
  });
});
