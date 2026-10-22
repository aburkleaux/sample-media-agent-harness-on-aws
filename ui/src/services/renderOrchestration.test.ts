import { describe, it, expect } from 'vitest';
import {
  computeInspectorSelection,
  shouldRenderManifest,
  isRenderPendingStep,
  shouldRevertSpuriousOpen,
  shouldShowRenderer,
  shouldEnterRenderPending,
  shouldAutoFollow,
  shouldDropComposingEntry,
} from './renderOrchestration';
import type { ManifestClip, CaptionCue } from '../components/nle/manifestParser';

const clip = (id: string): ManifestClip => ({ id, name: id, start: 0, end: 5, sourceTrack: 'v1' });
const cue = (text: string): CaptionCue => ({ start: 0, end: 2, speaker: 'SPEAKER_00', text });

describe('computeInspectorSelection (T4: no leftover placeholder)', () => {
  it('returns the explicitly selected clip when present', () => {
    const sel = computeInspectorSelection([clip('a'), clip('b')], undefined, clip('b'), null);
    expect(sel).toEqual({ type: 'clip', clip: clip('b') });
  });

  it('returns the explicitly selected cue when present', () => {
    const c = cue('hi');
    expect(computeInspectorSelection([], [c], null, c)).toEqual({ type: 'cue', cue: c });
  });

  it('defaults to the first clip when nothing is selected', () => {
    const sel = computeInspectorSelection([clip('first'), clip('second')], undefined, null, null);
    expect(sel).toEqual({ type: 'clip', clip: clip('first') });
  });

  it('defaults to the first cue when there are no clips', () => {
    const c = cue('opening line');
    const sel = computeInspectorSelection([], [c, cue('second')], null, null);
    expect(sel).toEqual({ type: 'cue', cue: c });
  });

  it('returns null (placeholder) only for a genuinely empty manifest', () => {
    expect(computeInspectorSelection([], undefined, null, null)).toBeNull();
    expect(computeInspectorSelection([], [], null, null)).toBeNull();
  });
});

describe('shouldRenderManifest (T5: no duplicate renders)', () => {
  it('renders a key once, then dedupes repeats', () => {
    const seen = new Set<string>();
    expect(shouldRenderManifest(seen, 'artifacts/x/render/abc.json')).toBe(true);
    expect(shouldRenderManifest(seen, 'artifacts/x/render/abc.json')).toBe(false);
  });

  it('renders a different (e.g. re-rendered, versioned) key', () => {
    const seen = new Set<string>();
    expect(shouldRenderManifest(seen, 'artifacts/x/render/v1.json')).toBe(true);
    expect(shouldRenderManifest(seen, 'artifacts/x/render/v2.json')).toBe(true);
  });

  it('never renders an empty key', () => {
    expect(shouldRenderManifest(new Set(), '')).toBe(false);
  });
});

describe('isRenderPendingStep (T6: consistent animation)', () => {
  it('is true for any step marked agent=render (hook path, any label)', () => {
    expect(isRenderPendingStep({ agent: 'render', label: '↳ Probing media' })).toBe(true);
    expect(isRenderPendingStep({ agent: 'render', label: '↳ Artifact download: x.wav' })).toBe(true);
  });

  it('is true when the label mentions render (dispatch path fallback)', () => {
    expect(isRenderPendingStep({ label: 'Dispatching: Render these artifacts…' })).toBe(true);
  });

  it('is false for unrelated steps', () => {
    expect(isRenderPendingStep({ agent: 'task', label: 'Running code…' })).toBe(false);
    expect(isRenderPendingStep(null)).toBe(false);
    expect(isRenderPendingStep({})).toBe(false);
  });
});

describe('shouldRevertSpuriousOpen (T3: no empty open renderer)', () => {
  it('reverts when auto-opened but no block arrived', () => {
    expect(shouldRevertSpuriousOpen(true, false)).toBe(true);
  });

  it('does not revert when a block arrived', () => {
    expect(shouldRevertSpuriousOpen(true, true)).toBe(false);
  });

  it('does not revert when it was not auto-opened (user had it open)', () => {
    expect(shouldRevertSpuriousOpen(false, false)).toBe(false);
    expect(shouldRevertSpuriousOpen(false, true)).toBe(false);
  });
});

describe('shouldShowRenderer (renderer-visibility invariant)', () => {
  it('hidden on a no-artifact session even if not collapsed', () => {
    expect(shouldShowRenderer(false, 0, false)).toBe(false);
  });

  it('shown when a render exists in history', () => {
    expect(shouldShowRenderer(false, 1, false)).toBe(true);
  });

  it('shown while a first render is composing (pending, empty history)', () => {
    expect(shouldShowRenderer(false, 0, true)).toBe(true);
  });

  it('hidden when the user collapsed it, regardless of content', () => {
    expect(shouldShowRenderer(true, 3, true)).toBe(false);
  });
});

describe('shouldEnterRenderPending (double-render / flicker guard)', () => {
  it('enters pending on the FIRST render-agent step of a turn', () => {
    expect(shouldEnterRenderPending(true, false)).toBe(true);
  });

  it('does NOT re-enter pending once a manifest has rendered this turn', () => {
    // The reported flicker: render arrives, then a trailing render-agent step.
    expect(shouldEnterRenderPending(true, true)).toBe(false);
  });

  it('never enters pending for a non-render step', () => {
    expect(shouldEnterRenderPending(false, false)).toBe(false);
    expect(shouldEnterRenderPending(false, true)).toBe(false);
  });

  it('models the full turn sequence: pending → render → NO re-pending', () => {
    let renderArrived = false;
    const seq: boolean[] = [];
    // step: render-agent composing
    seq.push(shouldEnterRenderPending(isRenderPendingStep({ agent: 'render' }), renderArrived));
    // render manifest arrives
    renderArrived = true;
    // step: another render-agent event (would previously flip back to loading)
    seq.push(shouldEnterRenderPending(isRenderPendingStep({ agent: 'render' }), renderArrived));
    expect(seq).toEqual([true, false]);
  });
});


describe('shouldAutoFollow (tab auto-selection rule)', () => {
  const base = { firstEntry: false, turnActive: true, autoFollowConsumed: false, userPinned: false };

  it('situation 1: first entry in an empty panel always selects', () => {
    expect(shouldAutoFollow({ ...base, firstEntry: true, turnActive: false })).toBe(true);
  });

  it('situation 2: single render of the turn the user initiated → jump', () => {
    expect(shouldAutoFollow(base)).toBe(true);
  });

  it('situation 3: later tabs of a multi-render turn badge, do not hop', () => {
    expect(shouldAutoFollow({ ...base, autoFollowConsumed: true })).toBe(false);
  });

  it('situation 4: a manual click pins → no yank for the rest of the turn', () => {
    expect(shouldAutoFollow({ ...base, userPinned: true })).toBe(false);
  });

  it('situation 5: out-of-turn async completion badges only', () => {
    expect(shouldAutoFollow({ ...base, turnActive: false })).toBe(false);
  });

  it('situation 5 does not override an empty panel', () => {
    // an async completion that is ALSO the first thing to show still selects
    expect(shouldAutoFollow({ ...base, firstEntry: true, turnActive: false })).toBe(true);
  });
});

describe('shouldDropComposingEntry (turn-end cleanup)', () => {
  it('drops a composing placeholder that never filled', () => {
    expect(shouldDropComposingEntry({ status: 'composing', blocks: [] })).toBe(true);
  });
  it('keeps a composing tab that received blocks', () => {
    expect(shouldDropComposingEntry({ status: 'composing', blocks: [{}] })).toBe(false);
  });
  it('keeps a ready tab', () => {
    expect(shouldDropComposingEntry({ status: 'ready', blocks: [] })).toBe(false);
  });
});
