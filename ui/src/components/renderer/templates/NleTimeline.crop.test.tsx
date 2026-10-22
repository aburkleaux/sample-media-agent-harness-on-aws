import { describe, it, expect } from 'vitest';
import { buildCropPrompt } from './NleTimeline';
import type { CropBox } from '../types';

// The single-video crop handoff. The agent only ever sees this prompt string (the structured box
// stays client-side), so it must carry the exact box. It is now concise: the box value routes to
// the crop_video tool (the SKILL owns the 'pass verbatim, never recompute' guard, not the prompt).
describe('buildCropPrompt (single-video crop handoff)', () => {
  const box: CropBox = { x: 0.23, y: 0.0, w: 0.316, h: 1.0 }; // the emitted 9:16 box

  it('carries the exact box as a copy-ready box="x,y,w,h" arg', () => {
    expect(buildCropPrompt(box)).toContain('box="0.230,0.000,0.316,1.000"');
  });

  it('labels the values as fractions of the frame', () => {
    expect(buildCropPrompt(box)).toContain('fractions of the frame');
  });

  it('never emits the ratio (0.5625) as the width — the reversal bug', () => {
    expect(buildCropPrompt(box)).not.toContain('0.563'); // 9/16 as a width fraction
  });

  it('is concise — no leftover "do NOT recompute" clause (the tool + SKILL own that now)', () => {
    expect(buildCropPrompt(box)).not.toContain('do NOT recompute');
  });

  it('clamps out-of-range values into 0..1', () => {
    expect(buildCropPrompt({ x: -0.2, y: 0.5, w: 2.0, h: 0.4 })).toContain('box="0.000,0.500,1.000,0.400"');
  });
});
