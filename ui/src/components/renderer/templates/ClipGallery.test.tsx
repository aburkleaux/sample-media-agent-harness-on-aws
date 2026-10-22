// @vitest-environment jsdom
import { describe, it, expect, vi, beforeAll } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import ClipGallery, { buildBatchTrimPrompt, buildFrameExtractPrompt, buildBatchCropPrompt } from './ClipGallery';
import { theme } from '../../../styles/tokens';
import type { ClipGalleryClip } from '../types';

// jsdom implements no media clock; make currentTime a settable data property so a
// test can position the playhead and the "⇤ In"/"Out ⇥" buttons capture it.
beforeAll(() => {
  Object.defineProperty(window.HTMLMediaElement.prototype, 'currentTime', {
    configurable: true, writable: true, value: 0,
  });
});

const CLIPS: ClipGalleryClip[] = [
  { id: 'seg1', label: 'Segment 1', text: 'Hi there', src: 'https://cdn.example/seg1.wav', kind: 'audio', durationHint: 16.9 },
  { id: 'seg2', label: 'Segment 2', text: 'and welcome', src: 'https://cdn.example/seg2.wav', kind: 'audio', durationHint: 7.9 },
];

function markClip(clipId: string, inSec: number, outSec: number) {
  const card = screen.getByTestId(`clip-card-${clipId}`);
  const audio = card.querySelector('audio') as HTMLAudioElement;
  (audio as unknown as { currentTime: number }).currentTime = inSec;
  fireEvent.click(screen.getByTestId(`clip-set-in-${clipId}`));
  (audio as unknown as { currentTime: number }).currentTime = outSec;
  fireEvent.click(screen.getByTestId(`clip-set-out-${clipId}`));
}

describe('ClipGallery', () => {
  it('renders one editable card per independent clip', () => {
    render(<ClipGallery data={{ galleryClips: CLIPS }} title="" theme={theme} />);
    expect(screen.getByTestId('clip-card-seg1')).toBeInTheDocument();
    expect(screen.getByTestId('clip-card-seg2')).toBeInTheDocument();
    // audio gallery → the batch verb is "Trim", and nothing is marked yet.
    const btn = screen.getByTestId('trim-all-marked');
    expect(btn).toHaveTextContent('Trim all marked (0)');
    expect(btn).toBeDisabled();
  });

  it('marks in/out per clip and trims ALL marked in one action that names each clip + range', () => {
    const onInteraction = vi.fn();
    render(<ClipGallery data={{ galleryClips: CLIPS }} title="" theme={theme} onInteraction={onInteraction} />);

    markClip('seg1', 0.6, 16.9);
    markClip('seg2', 0.3, 7.9);

    const btn = screen.getByTestId('trim-all-marked');
    expect(btn).toHaveTextContent('Trim all marked (2)');
    expect(btn).not.toBeDisabled();

    fireEvent.click(btn);

    expect(onInteraction).toHaveBeenCalledTimes(1);
    const evt = onInteraction.mock.calls[0][0];
    expect(evt.type).toBe('action');
    expect(evt.target).toBe('trim-batch');
    const d = evt.data as { verb: string; prompt: string; clips: Array<Record<string, unknown>> };
    expect(d.verb).toBe('trim');
    expect(d.clips).toHaveLength(2);
    // Each entry carries its OWN clip id + src + range — no "which one?" ambiguity.
    expect(d.clips[0]).toMatchObject({ clipId: 'seg1', label: 'Segment 1', src: 'https://cdn.example/seg1.wav', in: 0.6, out: 16.9 });
    expect(d.clips[1]).toMatchObject({ clipId: 'seg2', label: 'Segment 2', src: 'https://cdn.example/seg2.wav', in: 0.3, out: 7.9 });
    // The composed prompt names both clips and never leaks a URL into the transcript.
    expect(d.prompt).toContain('Segment 1');
    expect(d.prompt).toContain('Segment 2');
    expect(d.prompt).not.toContain('https://');
  });

  it('clearing a clip removes it from the batch count', () => {
    render(<ClipGallery data={{ galleryClips: CLIPS }} title="" theme={theme} />);
    markClip('seg1', 0.6, 16.9);
    expect(screen.getByTestId('trim-all-marked')).toHaveTextContent('(1)');
    fireEvent.click(screen.getByTestId('clip-clear-seg1'));
    expect(screen.getByTestId('trim-all-marked')).toHaveTextContent('(0)');
  });

  it('a video gallery clips (not trims), and drops clips with a non-https src', () => {
    const mixed: ClipGalleryClip[] = [
      { id: 'v1', label: 'Take 1', src: 'https://cdn.example/v1.mp4', kind: 'video', durationHint: 6 },
      { id: 'bad', label: 'Bad', src: 's3://bucket/x.mp4', kind: 'video' },
    ];
    render(<ClipGallery data={{ galleryClips: mixed }} title="" theme={theme} />);
    expect(screen.getByTestId('clip-card-v1')).toBeInTheDocument();
    expect(screen.queryByTestId('clip-card-bad')).toBeNull(); // s3:// filtered out
    expect(screen.getByTestId('trim-all-marked')).toHaveTextContent('Clip all marked (0)');
  });

  it('a video gallery offers per-clip crop boxing + a single "Crop all" (disabled until a region is set); audio galleries have none', () => {
    const vids: ClipGalleryClip[] = [
      { id: 'v1', label: 'Take 1', src: 'https://cdn.example/v1.mp4', kind: 'video' },
      { id: 'v2', label: 'Take 2', src: 'https://cdn.example/v2.mp4', kind: 'video' },
    ];
    const { unmount } = render(<ClipGallery data={{ galleryClips: vids }} title="" theme={theme} />);
    expect(screen.getByTestId('clip-crop-v1')).toBeInTheDocument();        // per-card crop toggle (sets a box)
    const cropAll = screen.getByTestId('crop-all');
    expect(cropAll).toHaveTextContent('Crop all (0)');
    expect(cropAll).toBeDisabled();                                        // nothing boxed yet
    // "Apply box to all" is gone — there is exactly one crop action.
    expect(screen.queryByTestId('apply-crop-all')).toBeNull();
    expect(screen.queryByTestId('crop-marked')).toBeNull();
    unmount();
    // Audio-only gallery: crop is video-only, so the controls are absent.
    render(<ClipGallery data={{ galleryClips: CLIPS }} title="" theme={theme} />);
    expect(screen.queryByTestId('crop-all')).toBeNull();
    expect(screen.queryByTestId('clip-crop-seg1')).toBeNull();
  });

  it('buildBatchTrimPrompt wording is verb- and count-aware', () => {
    expect(buildBatchTrimPrompt('trim', [{ label: 'S1', in: 0.6, out: 7 }])).toBe('Trim "S1" — keep 00:00.600 to 00:07.000.');
    expect(buildBatchTrimPrompt('clip', [{ label: 'T1', in: 1, out: 5 }])).toContain('Clip "T1"');
    const multi = buildBatchTrimPrompt('trim', [{ label: 'A', in: 0, out: 1 }, { label: 'B', in: 0, out: 2 }]);
    expect(multi).toContain('these 2 clips');
    expect(multi).toContain('"A"');
    expect(multi).toContain('"B"');
  });

  it('(27c33c57) says ONE pass when every marked range shares one source file', () => {
    // 7 ranges of the same video read as 7 independent targets, so the planner fanned them into
    // 7 nodes that each re-read the whole source (7m39s). The prompt now names the shared source.
    const src = 'https://cdn.example/tidal_line_trailer.mp4';
    const sameSource = buildBatchTrimPrompt('clip', [
      { label: 'Shot 3', in: 1, out: 4, src },
      { label: 'Shot 4', in: 5, out: 9, src },
      { label: 'Shot 5', in: 10, out: 12, src },
    ]);
    expect(sameSource).toContain('SAME source file');
    expect(sameSource).toContain('ONE pass');
    expect(sameSource).toContain('"Shot 3"');

    // Distinct sources ARE a genuine fan-out: the hint must not appear.
    const multiSource = buildBatchTrimPrompt('clip', [
      { label: 'A', in: 1, out: 4, src: 'https://cdn.example/a.mp4' },
      { label: 'B', in: 5, out: 9, src: 'https://cdn.example/b.mp4' },
    ]);
    expect(multiSource).not.toContain('SAME source file');

    // A missing src is unknown, not "same" — stay silent rather than assert one pass.
    const unknown = buildBatchTrimPrompt('clip', [
      { label: 'A', in: 1, out: 4, src },
      { label: 'B', in: 5, out: 9 },
    ]);
    expect(unknown).not.toContain('SAME source file');

    // A single clip is already one step; no hint needed.
    expect(buildBatchTrimPrompt('clip', [{ label: 'A', in: 1, out: 4, src }])).not.toContain('ONE pass');
  });

  // --- Video-only capabilities: Expand → own tab, and multi-video frame extraction ---
  const VIDEOS: ClipGalleryClip[] = [
    { id: 'v1', label: 'Segment 1', text: 'line one', src: 'https://cdn.example/v1.mp4', kind: 'video', durationHint: 6 },
    { id: 'v2', label: 'Segment 2', text: 'line two', src: 'https://cdn.example/v2.mp4', kind: 'video', durationHint: 8 },
  ];

  function markVideoClip(clipId: string, inSec: number, outSec: number) {
    const card = screen.getByTestId(`clip-card-${clipId}`);
    const video = card.querySelector('video') as HTMLVideoElement;
    (video as unknown as { currentTime: number }).currentTime = inSec;
    fireEvent.click(screen.getByTestId(`clip-set-in-${clipId}`));
    (video as unknown as { currentTime: number }).currentTime = outSec;
    fireEvent.click(screen.getByTestId(`clip-set-out-${clipId}`));
  }

  it('a video card Expands to its own timeline tab via an open-tab interaction (no agent turn)', () => {
    const onInteraction = vi.fn();
    render(<ClipGallery data={{ galleryClips: VIDEOS }} title="" theme={theme} onInteraction={onInteraction} />);
    fireEvent.click(screen.getByTestId('clip-expand-v1'));
    expect(onInteraction).toHaveBeenCalledTimes(1);
    const evt = onInteraction.mock.calls[0][0];
    expect(evt.type).toBe('open-tab');
    expect(evt.target).toBe('expand-timeline');
    expect(evt.data).toMatchObject({ url: 'https://cdn.example/v1.mp4', label: 'Segment 1' });
  });

  it('audio clips have no Expand button (video-only affordance)', () => {
    render(<ClipGallery data={{ galleryClips: CLIPS }} title="" theme={theme} />);
    expect(screen.queryByTestId('clip-expand-seg1')).toBeNull();
  });

  it('extracts a frame from each marked video at its mark-in, in ONE action', () => {
    const onInteraction = vi.fn();
    render(<ClipGallery data={{ galleryClips: VIDEOS }} title="" theme={theme} onInteraction={onInteraction} />);
    const extract = screen.getByTestId('extract-frames');
    expect(extract).toHaveTextContent('Extract frame (0)');
    expect(extract).toBeDisabled();

    markVideoClip('v1', 2.5, 6);
    markVideoClip('v2', 1.0, 8);
    expect(extract).toHaveTextContent('Extract frame (2)');
    expect(extract).not.toBeDisabled();

    fireEvent.click(extract);
    expect(onInteraction).toHaveBeenCalledTimes(1);
    const evt = onInteraction.mock.calls[0][0];
    expect(evt.type).toBe('action');
    expect(evt.target).toBe('extract-frames');
    const d = evt.data as { verb: string; prompt: string; clips: Array<Record<string, unknown>> };
    expect(d.verb).toBe('extract');
    expect(d.clips).toHaveLength(2);
    // capture point is each clip's mark-in
    expect(d.clips[0]).toMatchObject({ clipId: 'v1', src: 'https://cdn.example/v1.mp4', at: 2.5 });
    expect(d.clips[1]).toMatchObject({ clipId: 'v2', at: 1.0 });
    expect(d.prompt).toContain('Segment 1');
    expect(d.prompt).not.toContain('https://');
  });

  it('an audio-only gallery has no Extract frame button', () => {
    render(<ClipGallery data={{ galleryClips: CLIPS }} title="" theme={theme} />);
    expect(screen.queryByTestId('extract-frames')).toBeNull();
  });

  it('buildFrameExtractPrompt wording is count-aware and names the capture point', () => {
    expect(buildFrameExtractPrompt([{ label: 'S1', at: 2.5 }])).toContain('Extract a still frame from "S1" at 00:02.500');
    const multi = buildFrameExtractPrompt([{ label: 'A', at: 1 }, { label: 'B', at: 2 }]);
    expect(multi).toContain('these 2 clips');
    expect(multi).toContain('"A"');
    expect(multi).toContain('"B"');
  });

  it('buildBatchCropPrompt names each clip and carries a copy-ready box arg', () => {
    const one = buildBatchCropPrompt([{ label: 'S1', box: { x: 0.2, y: 0.15, w: 0.55, h: 0.6 } }]);
    expect(one).toContain('Crop "S1"');
    expect(one).toContain('box="0.200,0.150,0.550,0.600"');            // copy-ready, exact values
    expect(one).toContain('fractions of the frame');
    expect(one).not.toContain('do NOT recompute');                    // trimmed leftover text
    const multi = buildBatchCropPrompt([
      { label: 'A', box: { x: 0, y: 0, w: 0.5, h: 0.5 } },
      { label: 'B', box: { x: 0.5, y: 0.5, w: 0.5, h: 0.5 } },
    ]);
    expect(multi).toContain('these 2 clips');
    expect(multi).toContain('"A" box="0.000,0.000,0.500,0.500"');
    expect(multi).toContain('"B" box="0.500,0.500,0.500,0.500"');
  });
});
