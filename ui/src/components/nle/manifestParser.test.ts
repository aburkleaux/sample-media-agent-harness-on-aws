import { describe, it, expect } from 'vitest';
import {
  formatManifest,
  parseManifest,
  RenderManifest,
} from './manifestParser';

// ---------------------------------------------------------------------------
// Test fixture helper
// ---------------------------------------------------------------------------

function validManifest(overrides: Partial<RenderManifest> = {}): RenderManifest {
  return {
    id: '550e8400-e29b-41d4-a716-446655440000',
    duration: 120.456,
    tracks: [
      { id: 'v1', type: 'video', src: 'https://cdn.example.com/video.mp4', start: 0, end: 120.456 },
      { id: 'a1', type: 'audio', src: 'https://cdn.example.com/audio.mp3', start: 0, end: 120.456 },
    ],
    clips: [
      { id: 'c1', name: 'Intro', start: 0, end: 15.5, sourceTrack: 'v1' },
    ],
    createdAt: '2024-01-15T10:30:00.000Z',
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// formatManifest
// ---------------------------------------------------------------------------

describe('formatManifest', () => {
  it('produces 2-space-indented JSON', () => {
    const manifest = validManifest();
    const json = formatManifest(manifest);
    // Check indentation: second line should start with 2 spaces
    const lines = json.split('\n');
    expect(lines[1]).toMatch(/^ {2}"/);
  });

  it('rounds duration to 3 decimal places', () => {
    const manifest = validManifest({ duration: 120.45678 });
    const json = formatManifest(manifest);
    const parsed = JSON.parse(json);
    expect(parsed.duration).toBe(120.457);
  });

  it('rounds track start/end to 3 decimal places', () => {
    const manifest = validManifest({
      tracks: [{ id: 't1', type: 'video', src: 'http://x.com/v.mp4', start: 1.23456, end: 99.99999 }],
    });
    const json = formatManifest(manifest);
    const parsed = JSON.parse(json);
    expect(parsed.tracks[0].start).toBe(1.235);
    expect(parsed.tracks[0].end).toBe(100);
  });

  it('rounds clip start/end to 3 decimal places', () => {
    const manifest = validManifest({
      clips: [{ id: 'c1', name: 'Test', start: 5.1234, end: 10.9999, sourceTrack: 'v1' }],
    });
    const json = formatManifest(manifest);
    const parsed = JSON.parse(json);
    expect(parsed.clips[0].start).toBe(5.123);
    expect(parsed.clips[0].end).toBe(11);
  });

  it('rounds caption start/end to 3 decimal places', () => {
    const manifest = validManifest({
      captions: [{ start: 1.5555, end: 3.9999, speaker: 'Alice', text: 'Hello' }],
    });
    const json = formatManifest(manifest);
    const parsed = JSON.parse(json);
    expect(parsed.captions[0].start).toBe(1.556);
    expect(parsed.captions[0].end).toBe(4);
  });

  it('preserves unknown fields', () => {
    const manifest = validManifest({ customMeta: { foo: 'bar' } } as any);
    const json = formatManifest(manifest);
    const parsed = JSON.parse(json);
    expect(parsed.customMeta).toEqual({ foo: 'bar' });
  });

  it('preserves unknown fields in tracks', () => {
    const manifest = validManifest({
      tracks: [{ id: 't1', type: 'audio', src: 'http://x.com/a.mp3', start: 0, end: 10, codec: 'aac' } as any],
    });
    const json = formatManifest(manifest);
    const parsed = JSON.parse(json);
    expect(parsed.tracks[0].codec).toBe('aac');
  });
});

// ---------------------------------------------------------------------------
// parseManifest — success cases
// ---------------------------------------------------------------------------

describe('parseManifest — success', () => {
  it('parses a valid manifest', () => {
    const manifest = validManifest();
    const json = formatManifest(manifest);
    const result = parseManifest(json);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.manifest.id).toBe(manifest.id);
      expect(result.manifest.duration).toBe(120.456);
      expect(result.manifest.tracks).toHaveLength(2);
      expect(result.manifest.clips).toHaveLength(1);
    }
  });

  it('preserves unknown top-level fields', () => {
    const manifest = validManifest({ extraData: 42 } as any);
    const json = formatManifest(manifest);
    const result = parseManifest(json);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect((result.manifest as any).extraData).toBe(42);
    }
  });

  it('preserves unknown fields inside tracks', () => {
    const manifest = validManifest({
      tracks: [{ id: 't1', type: 'video', src: 'http://x.com/v.mp4', start: 0, end: 60, customField: true } as any],
    });
    const json = formatManifest(manifest);
    const result = parseManifest(json);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect((result.manifest.tracks[0] as any).customField).toBe(true);
    }
  });

  it('accepts empty clips array', () => {
    const manifest = validManifest({ clips: [] });
    const json = formatManifest(manifest);
    const result = parseManifest(json);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.manifest.clips).toEqual([]);
    }
  });

  it('accepts manifest without optional captions', () => {
    const manifest = validManifest();
    delete (manifest as any).captions;
    const json = formatManifest(manifest);
    const result = parseManifest(json);
    expect(result.ok).toBe(true);
  });

  it('accepts a graph-view manifest, which has nodes/edges and no timeline', () => {
    // The shape render/graph_view.py::build_graph_manifest writes. Fetched by manifestUrl, it goes through
    // this parser before ManifestRouter routes it; without the exemption the "↳ Graph" tab read
    // 'Required field "duration" is missing; ... "tracks" ...; ... "clips" ...'.
    const json = JSON.stringify({
      id: 'graph-s1-t1', createdAt: '2026-10-08T00:00:00Z', template: 'graph-view', title: 'Graph · Personal',
      project_id: 'p1', turn_id: 't1', origin: 'agent', truncated: false,
      nodes: [{ id: 'n1', label: 'Person', name: 'Ada' }], edges: [],
      calls: [{ tool: 'graph_query', nodes: 1, edges: 0 }],
    });
    const result = parseManifest(json);
    expect(result.ok ? [] : result.errors.map((e) => e.message)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// parseManifest — error cases
// ---------------------------------------------------------------------------

describe('parseManifest — errors', () => {
  it('returns INVALID_JSON for non-JSON input', () => {
    const result = parseManifest('not json at all');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.errors).toHaveLength(1);
      expect(result.errors[0].code).toBe('INVALID_JSON');
      expect(result.errors[0].message).toMatch(/line.*column/i);
    }
  });

  it('returns INVALID_JSON for incomplete JSON', () => {
    const result = parseManifest('{"id": "test"');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.errors[0].code).toBe('INVALID_JSON');
    }
  });

  it('returns INVALID_TYPE when root is not an object', () => {
    const result = parseManifest('[1, 2, 3]');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.errors[0].code).toBe('INVALID_TYPE');
      expect(result.errors[0].expected).toBe('object');
    }
  });

  it('returns MISSING_FIELD for missing id', () => {
    const manifest = validManifest();
    delete (manifest as any).id;
    const json = JSON.stringify(manifest);
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      const idError = result.errors.find(e => e.path === 'id');
      expect(idError).toBeDefined();
      expect(idError!.code).toBe('MISSING_FIELD');
    }
  });

  it('returns MISSING_FIELD for missing duration', () => {
    const manifest = validManifest();
    delete (manifest as any).duration;
    const json = JSON.stringify(manifest);
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      const err = result.errors.find(e => e.path === 'duration');
      expect(err).toBeDefined();
      expect(err!.code).toBe('MISSING_FIELD');
    }
  });

  it('returns MISSING_FIELD for missing tracks', () => {
    const manifest = validManifest();
    delete (manifest as any).tracks;
    const json = JSON.stringify(manifest);
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      const err = result.errors.find(e => e.path === 'tracks');
      expect(err).toBeDefined();
      expect(err!.code).toBe('MISSING_FIELD');
    }
  });

  it('returns MISSING_FIELD for missing clips', () => {
    const manifest = validManifest();
    delete (manifest as any).clips;
    const json = JSON.stringify(manifest);
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      const err = result.errors.find(e => e.path === 'clips');
      expect(err).toBeDefined();
      expect(err!.code).toBe('MISSING_FIELD');
    }
  });

  it('returns MISSING_FIELD for missing createdAt', () => {
    const manifest = validManifest();
    delete (manifest as any).createdAt;
    const json = JSON.stringify(manifest);
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      const err = result.errors.find(e => e.path === 'createdAt');
      expect(err).toBeDefined();
      expect(err!.code).toBe('MISSING_FIELD');
    }
  });

  it('returns INVALID_TYPE when id is not a string', () => {
    const json = JSON.stringify({ ...validManifest(), id: 123 });
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      const err = result.errors.find(e => e.path === 'id');
      expect(err).toBeDefined();
      expect(err!.code).toBe('INVALID_TYPE');
      expect(err!.expected).toBe('string');
      expect(err!.actual).toBe('number');
    }
  });

  it('returns INVALID_TYPE when duration is not a number', () => {
    const json = JSON.stringify({ ...validManifest(), duration: 'abc' });
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      const err = result.errors.find(e => e.path === 'duration');
      expect(err).toBeDefined();
      expect(err!.code).toBe('INVALID_TYPE');
    }
  });

  it('returns INVALID_VALUE when duration is below minimum', () => {
    const json = JSON.stringify({ ...validManifest(), duration: 0.0001 });
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      const err = result.errors.find(e => e.path === 'duration');
      expect(err).toBeDefined();
      expect(err!.code).toBe('INVALID_VALUE');
    }
  });

  it('returns INVALID_VALUE when duration exceeds maximum', () => {
    const json = JSON.stringify({ ...validManifest(), duration: 100000 });
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      const err = result.errors.find(e => e.path === 'duration');
      expect(err).toBeDefined();
      expect(err!.code).toBe('INVALID_VALUE');
    }
  });

  it('returns INVALID_TYPE when tracks is not an array', () => {
    const json = JSON.stringify({ ...validManifest(), tracks: 'not an array' });
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      const err = result.errors.find(e => e.path === 'tracks');
      expect(err).toBeDefined();
      expect(err!.code).toBe('INVALID_TYPE');
    }
  });

  it('returns errors for invalid track entries with dot-notation paths', () => {
    const json = JSON.stringify({
      ...validManifest(),
      tracks: [{ id: 123, type: 'invalid_type', src: null, start: 'x', end: 'y' }],
    });
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      const idErr = result.errors.find(e => e.path === 'tracks[0].id');
      expect(idErr).toBeDefined();
      expect(idErr!.code).toBe('INVALID_TYPE');

      const typeErr = result.errors.find(e => e.path === 'tracks[0].type');
      expect(typeErr).toBeDefined();
      expect(typeErr!.code).toBe('INVALID_VALUE');

      const srcErr = result.errors.find(e => e.path === 'tracks[0].src');
      expect(srcErr).toBeDefined();

      const startErr = result.errors.find(e => e.path === 'tracks[0].start');
      expect(startErr).toBeDefined();
      expect(startErr!.code).toBe('INVALID_TYPE');
    }
  });

  it('returns errors for missing clip fields', () => {
    const json = JSON.stringify({
      ...validManifest(),
      clips: [{ id: 'c1' }], // missing name, start, end, sourceTrack
    });
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.errors.find(e => e.path === 'clips[0].name')).toBeDefined();
      expect(result.errors.find(e => e.path === 'clips[0].start')).toBeDefined();
      expect(result.errors.find(e => e.path === 'clips[0].end')).toBeDefined();
      expect(result.errors.find(e => e.path === 'clips[0].sourceTrack')).toBeDefined();
    }
  });

  it('returns INVALID_VALUE for clip name exceeding 128 chars', () => {
    const json = JSON.stringify({
      ...validManifest(),
      clips: [{ id: 'c1', name: 'a'.repeat(129), start: 0, end: 10, sourceTrack: 'v1' }],
    });
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      const err = result.errors.find(e => e.path === 'clips[0].name');
      expect(err).toBeDefined();
      expect(err!.code).toBe('INVALID_VALUE');
    }
  });

  it('returns errors for invalid caption cues', () => {
    const json = JSON.stringify({
      ...validManifest(),
      captions: [{ start: 'x', end: 'y', speaker: '', text: '' }],
    });
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.errors.find(e => e.path === 'captions[0].start')).toBeDefined();
      expect(result.errors.find(e => e.path === 'captions[0].end')).toBeDefined();
      expect(result.errors.find(e => e.path === 'captions[0].speaker')).toBeDefined();
      expect(result.errors.find(e => e.path === 'captions[0].text')).toBeDefined();
    }
  });

  it('returns multiple errors at once', () => {
    const json = JSON.stringify({ tracks: 'invalid', clips: 42 });
    const result = parseManifest(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      // Should report errors for id, duration, tracks, clips, createdAt
      expect(result.errors.length).toBeGreaterThanOrEqual(4);
    }
  });
});

// ---------------------------------------------------------------------------
// Round-trip property
// ---------------------------------------------------------------------------

describe('round-trip', () => {
  it('parseManifest(formatManifest(m)) produces a deeply equal object', () => {
    const manifest = validManifest({
      captions: [{ start: 10.123, end: 15.456, speaker: 'Bob', text: 'Hi there' }],
      peaks: { a1: [0.1, 0.5, 0.9] },
    });
    const json = formatManifest(manifest);
    const result = parseManifest(json);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.manifest).toEqual(JSON.parse(json));
    }
  });

  it('preserves array ordering', () => {
    const manifest = validManifest({
      tracks: [
        { id: 'a', type: 'video', src: 'http://x/a.mp4', start: 0, end: 10 },
        { id: 'b', type: 'audio', src: 'http://x/b.mp3', start: 0, end: 10 },
        { id: 'c', type: 'subtitle', src: 'http://x/c.vtt', start: 0, end: 10 },
      ],
      clips: [
        { id: 'c1', name: 'First', start: 0, end: 5, sourceTrack: 'a' },
        { id: 'c2', name: 'Second', start: 5, end: 10, sourceTrack: 'a' },
      ],
    });
    const json = formatManifest(manifest);
    const result = parseManifest(json);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.manifest.tracks.map(t => t.id)).toEqual(['a', 'b', 'c']);
      expect(result.manifest.clips.map(c => c.id)).toEqual(['c1', 'c2']);
    }
  });

  it('preserves unknown fields through round-trip', () => {
    const manifest = validManifest({
      myCustomField: { nested: [1, 2, 3] },
    } as any);
    const json = formatManifest(manifest);
    const result = parseManifest(json);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect((result.manifest as any).myCustomField).toEqual({ nested: [1, 2, 3] });
    }
  });

  it('maintains 3dp numeric precision on round-trip', () => {
    const manifest = validManifest({
      duration: 60.1234567,
      tracks: [{ id: 'v1', type: 'video', src: 'http://x/v.mp4', start: 0.0001, end: 59.9999 }],
    });
    const json = formatManifest(manifest);
    const parsed = JSON.parse(json);
    expect(parsed.duration).toBe(60.123);
    expect(parsed.tracks[0].start).toBe(0);
    expect(parsed.tracks[0].end).toBe(60);
  });
});

// Template-aware validation: non-timeline templates must not be validated as video timelines.
describe('parseManifest — template-aware (non-timeline)', () => {
  it('image-annotated parses without a valid timeline duration (a still has none)', () => {
    const m = { id: '550e8400-e29b-41d4-a716-446655440000', template: 'image-annotated',
      duration: 0, tracks: [], clips: [], createdAt: '2026-08-11T00:00:00.000Z',
      images: [{ src: 'https://cdn/a.png' }, { src: 'https://cdn/b.jpg' }] };
    const r = parseManifest(JSON.stringify(m));
    expect(r.ok, r.ok ? '' : r.errors.map((e) => e.message).join('; ')).toBe(true);
  });

  it('document-viewer parses with no duration/tracks/clips at all', () => {
    const m = { id: '550e8400-e29b-41d4-a716-446655440000', template: 'document-viewer',
      createdAt: '2026-08-11T00:00:00.000Z', document: { previewUrl: 'https://cdn/x.pdf' } };
    expect(parseManifest(JSON.stringify(m)).ok).toBe(true);
  });

  it('nle-timeline STILL rejects duration 0 (timeline stays strict — regression guard)', () => {
    const m = { id: '550e8400-e29b-41d4-a716-446655440000', template: 'nle-timeline',
      duration: 0, tracks: [], clips: [], createdAt: '2026-08-11T00:00:00.000Z' };
    expect(parseManifest(JSON.stringify(m)).ok).toBe(false);
  });

  it('asset-player (show_asset) parses as written: an Asset_Reference, no timeline, no URL', () => {
    // The exact shape agent/src/project_search/project_tools.py writes. Staging 2026-10-07: rejected with
    // "Required field duration/tracks/clips is missing", so the opened file showed as a broken tab.
    const m = { id: 'asset-0123456789abcdef01234567', createdAt: '2026-10-07T23:37:00.000+00:00',
      template: 'asset-player', title: 'sam.webp',
      ref: { project_id: 'p3965bf1d75bf4852bc9d58358e001bd9', asset_id: 'image:aeaa3dc3', frame_s3_key: null },
      kind: 'image', displayName: 'sam.webp', startMs: 0, origin: 'agent' };
    const r = parseManifest(JSON.stringify(m));
    expect(r.ok, r.ok ? '' : r.errors.map((e) => e.message).join('; ')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Degenerate timeline degradation (staging session 12f6f088): a conversation-analysis node that
// produced no usable timeline emitted an invalid timeline manifest. `degradedManifestFromJson` must
// recognise it and return a renderable `unrenderable` manifest (so the UI shows "No preview
// available") instead of the hard "Could not display this render" error, while `parseManifest` stays
// strict and a valid timeline still parses clean.
// ---------------------------------------------------------------------------

import { degradedManifestFromJson, timelineDegradeReason } from './manifestParser';

describe('degradedManifestFromJson — degenerate timeline degradation', () => {
  // The EXACT malformed sg4 shape read from S3: duration 0, a single overlay track with
  // id/type/src/label/metadata but NO start/end, plus a top-level errors field.
  const malformed = {
    id: '736211b1-9fbe-4b42-85b4-82fe6536db4e',
    duration: 0,
    tracks: [
      {
        id: 'conversation-analysis-track',
        type: 'overlay',
        src: 'https://cdn.example.com/artifacts/conversation_analysis.json',
        label: 'Conversation Analysis - Content Moderation',
        metadata: { analysis_type: 'content_moderation' },
      },
    ],
    clips: [],
    createdAt: '2026-10-09T00:00:00.000Z',
    errors: [{ code: 'SOURCE_VIDEO_UNREACHABLE', message: 'The source video URL returned HTTP 403.' }],
  };

  it('parseManifest still REJECTS the malformed sg4 shape (strict contract unchanged)', () => {
    expect(parseManifest(JSON.stringify(malformed)).ok).toBe(false);
  });

  it('degrades the exact malformed sg4 shape to a renderable unrenderable manifest', () => {
    const m = degradedManifestFromJson(JSON.stringify(malformed));
    expect(m).not.toBeNull();
    expect(m!.unrenderable?.reason).toContain('duration');
    // Identity + errors are preserved so the renderer can still show the message.
    expect(m!.id).toBe('736211b1-9fbe-4b42-85b4-82fe6536db4e');
  });

  it('degrades a timeline whose track is missing start/end (duration otherwise valid)', () => {
    const noRange = {
      id: 'abc', duration: 300.167, clips: [], createdAt: '2026-10-09T00:00:00.000Z',
      tracks: [{ id: 't', type: 'video', src: 'https://cdn/x.mp4', label: 'x' }],
    };
    const m = degradedManifestFromJson(JSON.stringify(noRange));
    expect(m).not.toBeNull();
    expect(m!.unrenderable?.reason).toContain('track');
  });

  it('does NOT degrade the valid sg3 shape (duration 300.167, track with start/end) — it still renders', () => {
    const good = {
      id: '736211b1-sg3', duration: 300.167, clips: [], createdAt: '2026-10-09T00:00:00.000Z',
      template: 'nle-timeline', renderSkill: 'render-generic',
      tracks: [{ id: 'video-nba', type: 'video', src: 'https://cdn/nba.mp4', label: 'nba', start: 0.0, end: 300.167 }],
    };
    expect(degradedManifestFromJson(JSON.stringify(good))).toBeNull();
    const parsed = parseManifest(JSON.stringify(good));
    expect(parsed.ok).toBe(true);
    if (parsed.ok) expect(parsed.manifest.duration).toBe(300.167);
  });

  it('does NOT degrade a non-timeline template (image/generic have no timeline to be degenerate)', () => {
    const image = {
      id: 'img', template: 'image-annotated', clips: [], createdAt: '2026-10-09T00:00:00.000Z',
      images: [{ src: 'https://cdn/a.png' }],
    };
    expect(degradedManifestFromJson(JSON.stringify(image))).toBeNull();
  });

  it('does NOT degrade a genuinely broken manifest (missing id) — that stays a hard error', () => {
    const broken = { duration: 0, tracks: [], clips: [], createdAt: '2026-10-09T00:00:00.000Z' };
    expect(degradedManifestFromJson(JSON.stringify(broken))).toBeNull();
    expect(timelineDegradeReason(broken as any, false)).toBeNull();
    expect(parseManifest(JSON.stringify(broken)).ok).toBe(false);
  });
});

