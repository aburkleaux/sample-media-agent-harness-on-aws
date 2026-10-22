/**
 * Render Manifest TypeScript interfaces and parser/formatter.
 *
 * Provides:
 * - Type definitions for the Render_Manifest schema
 * - `formatManifest` — serializes a manifest to pretty-printed JSON (2-space indent, 3dp time fields)
 * - `parseManifest` — validates and parses a JSON string into a RenderManifest or structured errors
 *
 * Unknown fields are preserved on round-trip without producing validation errors.
 */

// ---------------------------------------------------------------------------
// Interfaces
// ---------------------------------------------------------------------------

export interface ManifestTrack {
  id: string;
  type: 'video' | 'audio' | 'overlay' | 'subtitle';
  src: string;
  start: number;
  end: number;
  [key: string]: unknown;
}

export interface ManifestClip {
  id: string;
  name: string; // max 128 chars
  start: number;
  end: number;
  sourceTrack: string;
  [key: string]: unknown;
}

export interface CaptionCue {
  start: number;
  end: number;
  speaker: string; // 1–50 chars
  text: string; // 1–300 chars
  [key: string]: unknown;
}

/** Composable overlay sections (design-v2). All optional / data-driven. */
export interface ManifestMarker {
  id: string;
  time: number;
  endTime?: number;
  label: string;
  category?: string;
  color?: string;
  thumbnail?: string;
  [key: string]: unknown;
}

export interface ManifestSegment {
  id: string;
  name: string;
  start: number;
  end: number;
  sourceTrack?: string;
  group?: string; // Lane key — segments sharing a group render on one row
  fields?: Record<string, string | number | boolean>;
  [key: string]: unknown;
}

export interface ManifestDocument {
  previewUrl: string;
  pageCount?: number | null;
  outline?: { title: string; page: number }[];
  [key: string]: unknown;
}

export interface ManifestError {
  code: string;
  message: string;
  field?: string; // dot-notation path
  [key: string]: unknown;
}

export interface ManifestWarning {
  code: string;
  message: string;
  field?: string;
  [key: string]: unknown;
}

export interface RenderManifest {
  id: string; // UUID v4
  duration: number; // Total duration in seconds (0.001–86400)
  tracks: ManifestTrack[]; // Ordered track definitions (max 32)
  clips: ManifestClip[]; // Named segments (may be empty)
  captions?: CaptionCue[]; // Speaker-identified cues (optional)
  peaks?: Record<string, number[]>; // Track ID → peak data (optional)
  markers?: ManifestMarker[]; // Timeline markers (optional, design-v2)
  segments?: ManifestSegment[]; // Interactive segment rows (optional, design-v2)
  metadata?: Record<string, string | number | boolean>; // Key/value panel (optional)
  document?: ManifestDocument; // Document preview (PDF) (optional)
  /** Every document of a multi-document render (first === `document`); the viewer offers a selector. */
  documents?: (ManifestDocument & { title?: string })[];
  createdAt: string; // ISO 8601 timestamp
  errors?: ManifestError[]; // Validation errors (present in fallback manifests)
  warnings?: ManifestWarning[]; // Non-fatal issues
  /**
   * Set by `parseManifest` when a timeline-shaped manifest has no usable timeline (duration < 0.001, or a
   * track missing its start/end). The manifest still parses `ok` so the renderer can DEGRADE to a friendly
   * "no preview available" state instead of the hard "Could not display this render" error (staging session
   * 12f6f088: a conversation-analysis node that produced no timeline emitted `duration: 0` + an overlay
   * track with no start/end).
   */
  unrenderable?: { reason: string };
  [key: string]: unknown; // Unknown fields preserved on round-trip
}

export interface ParseError {
  path: string; // dot-notation field path
  code: 'MISSING_FIELD' | 'INVALID_TYPE' | 'INVALID_VALUE' | 'INVALID_JSON';
  message: string;
  expected?: string;
  actual?: string;
}

export type ParseResult =
  | { ok: true; manifest: RenderManifest }
  | { ok: false; errors: ParseError[] };

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const VALID_TRACK_TYPES = new Set(['video', 'audio', 'overlay', 'subtitle']);

/**
 * Templates with no timeline of their own: an image / document / data / generic / graph / asset-player
 * manifest has no duration or tracks, so it is NOT validated through the video-timeline validator (and is
 * never degraded as a degenerate timeline). Hoisted to module scope so `degradedManifestFromJson` and
 * `parseManifest` share one list.
 */
const NON_TIMELINE_TEMPLATES = new Set([
  'image-annotated', 'document-viewer', 'data-table', 'generic-preview', 'clip-gallery',
  // A project file opened by `show_asset`: an Asset_Reference the player presigns at view time. It has no
  // timeline of its own (an image has no duration); rejecting it showed "Could not display this render".
  'asset-player',
  // The agent's "↳ Graph" result (render/graph_view.py): nodes/edges, no timeline.
  'graph-view',
  // The agent's browse_project tab (project_search/project_tools.py): a project id + filter, no timeline.
  'project-browser',
]);

// ---------------------------------------------------------------------------
// formatManifest
// ---------------------------------------------------------------------------

/**
 * Serialize a RenderManifest to a human-readable JSON string.
 * - 2-space indentation
 * - All numeric time fields (duration, start, end) in tracks, clips, and captions
 *   are rounded to 3 decimal places (millisecond precision).
 */
export function formatManifest(manifest: RenderManifest): string {
  const rounded = roundTimeFields(manifest);
  return JSON.stringify(rounded, null, 2);
}

/**
 * Deep-clone a manifest rounding all time fields to 3 decimal places.
 */
function roundTimeFields(manifest: RenderManifest): RenderManifest {
  const result: Record<string, unknown> = {};

  // Copy all fields from the manifest (preserving unknown ones)
  for (const key of Object.keys(manifest)) {
    if (key === 'duration') {
      result[key] = roundTo3(manifest.duration);
    } else if (key === 'tracks') {
      result[key] = manifest.tracks.map(roundTrackTimeFields);
    } else if (key === 'clips') {
      result[key] = manifest.clips.map(roundClipTimeFields);
    } else if (key === 'captions' && manifest.captions !== undefined) {
      result[key] = manifest.captions.map(roundCueTimeFields);
    } else {
      result[key] = manifest[key];
    }
  }

  return result as RenderManifest;
}

function roundTrackTimeFields(track: ManifestTrack): ManifestTrack {
  const result: Record<string, unknown> = {};
  for (const key of Object.keys(track)) {
    if (key === 'start' || key === 'end') {
      result[key] = roundTo3(track[key] as number);
    } else {
      result[key] = track[key];
    }
  }
  return result as ManifestTrack;
}

function roundClipTimeFields(clip: ManifestClip): ManifestClip {
  const result: Record<string, unknown> = {};
  for (const key of Object.keys(clip)) {
    if (key === 'start' || key === 'end') {
      result[key] = roundTo3(clip[key] as number);
    } else {
      result[key] = clip[key];
    }
  }
  return result as ManifestClip;
}

function roundCueTimeFields(cue: CaptionCue): CaptionCue {
  const result: Record<string, unknown> = {};
  for (const key of Object.keys(cue)) {
    if (key === 'start' || key === 'end') {
      result[key] = roundTo3(cue[key] as number);
    } else {
      result[key] = cue[key];
    }
  }
  return result as CaptionCue;
}

function roundTo3(n: number): number {
  return Math.round(n * 1000) / 1000;
}

// ---------------------------------------------------------------------------
// parseManifest
// ---------------------------------------------------------------------------

/**
 * Parse a JSON string into a validated RenderManifest or return structured errors.
 *
 * - Returns INVALID_JSON if the string isn't valid JSON (with line/column info)
 * - Returns MISSING_FIELD for each required field that is absent
 * - Returns INVALID_TYPE for fields present but of incorrect type
 * - Returns INVALID_VALUE for fields with out-of-range values
 * - Preserves unknown fields in the returned manifest
 */
export function parseManifest(json: string): ParseResult {
  // Step 1: parse JSON
  let parsed: unknown;
  try {
    parsed = JSON.parse(json);
  } catch (e: unknown) {
    const { line, column } = extractJsonErrorPosition(json, e);
    return {
      ok: false,
      errors: [
        {
          path: '',
          code: 'INVALID_JSON',
          message: `Invalid JSON at line ${line}, column ${column}`,
        },
      ],
    };
  }

  // Step 2: must be an object
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return {
      ok: false,
      errors: [
        {
          path: '',
          code: 'INVALID_TYPE',
          message: 'Manifest must be a JSON object',
          expected: 'object',
          actual: Array.isArray(parsed) ? 'array' : typeof parsed,
        },
      ],
    };
  }

  const obj = parsed as Record<string, unknown>;
  const errors: ParseError[] = [];

  // Step 3: validate required fields. Timeline fields (duration/tracks/clips) are required ONLY
  // for timeline-shaped templates. An image / document / data / generic manifest has no timeline,
  // so forcing it through the video-timeline validator was wrong -- it made a still image fail
  // with "duration must be >= 0.001" purely because a picture has no duration. Non-timeline
  // templates are validated for what they actually need; timeline (and unknown) stay strict.
  const template = typeof obj.template === 'string' ? obj.template : '';
  const isNonTimeline = NON_TIMELINE_TEMPLATES.has(template);

  validateRequiredString(obj, 'id', errors);
  validateRequiredString(obj, 'createdAt', errors);
  if (!isNonTimeline) {
    validateRequiredNumber(obj, 'duration', errors, 0.001, 86400);
    validateRequiredArray(obj, 'tracks', errors, validateTrack);
    validateRequiredArray(obj, 'clips', errors, validateClip);
  } else {
    // Non-timeline: these are optional; validate only if present, and never on `duration`
    // (a still image legitimately has none).
    if (obj.tracks !== undefined) validateRequiredArray(obj, 'tracks', errors, validateTrack);
    if (obj.clips !== undefined) validateRequiredArray(obj, 'clips', errors, validateClip);
  }

  // Step 4: validate optional arrays
  if (obj.captions !== undefined) {
    if (!Array.isArray(obj.captions)) {
      errors.push({
        path: 'captions',
        code: 'INVALID_TYPE',
        message: 'Field "captions" must be an array',
        expected: 'array',
        actual: typeof obj.captions,
      });
    } else {
      (obj.captions as unknown[]).forEach((item, i) => {
        validateCaptionCue(item, `captions[${i}]`, errors);
      });
    }
  }

  if (obj.peaks !== undefined) {
    if (typeof obj.peaks !== 'object' || obj.peaks === null || Array.isArray(obj.peaks)) {
      errors.push({
        path: 'peaks',
        code: 'INVALID_TYPE',
        message: 'Field "peaks" must be an object (Record<string, number[]>)',
        expected: 'object',
        actual: Array.isArray(obj.peaks) ? 'array' : typeof obj.peaks,
      });
    }
  }

  if (errors.length > 0) {
    return { ok: false, errors };
  }

  // All valid — return the manifest (preserving all fields including unknown ones)
  return { ok: true, manifest: obj as unknown as RenderManifest };
}

// ---------------------------------------------------------------------------
// Validation helpers
// ---------------------------------------------------------------------------

/**
 * The reason a timeline manifest cannot be rendered, or `null` when it is renderable (or not a timeline).
 * Only fires for a structurally sound manifest (string id/createdAt, numeric duration, array tracks) whose
 * timeline is degenerate — duration below the schema floor, or an object track missing start/end. Anything
 * else returns `null` so the strict validator can report a proper parse error.
 */
export function timelineDegradeReason(obj: Record<string, unknown>, isNonTimeline: boolean): string | null {
  if (isNonTimeline) return null;
  if (typeof obj.id !== 'string' || typeof obj.createdAt !== 'string') return null;
  if (!Array.isArray(obj.tracks)) return null;
  if (typeof obj.duration !== 'number' || isNaN(obj.duration)) return null;
  if (obj.duration < 0.001) return 'This render has no usable timeline (its duration is 0).';
  for (const track of obj.tracks as unknown[]) {
    if (track && typeof track === 'object' && !Array.isArray(track)) {
      const tr = track as Record<string, unknown>;
      if (typeof tr.start !== 'number' || typeof tr.end !== 'number') {
        return 'This render has no usable timeline (a track is missing its time range).';
      }
    }
  }
  return null;
}

/**
 * A degraded, STILL-RENDERABLE manifest for a timeline JSON that `parseManifest` would reject only because
 * its timeline is degenerate (duration < 0.001, or a track missing start/end). Returns the original object
 * with an `unrenderable: {reason}` marker so a renderer can show a calm "no preview available" state, or
 * `null` when the JSON is not that case (genuinely broken, or a valid manifest). The UI uses this to DEGRADE
 * a parse failure instead of throwing the hard "Could not display this render" error (staging 12f6f088).
 */
export function degradedManifestFromJson(json: string): RenderManifest | null {
  let obj: unknown;
  try {
    obj = JSON.parse(json);
  } catch {
    return null;
  }
  if (typeof obj !== 'object' || obj === null || Array.isArray(obj)) return null;
  const o = obj as Record<string, unknown>;
  const template = typeof o.template === 'string' ? o.template : '';
  const reason = timelineDegradeReason(o, NON_TIMELINE_TEMPLATES.has(template));
  if (!reason) return null;
  return { ...o, unrenderable: { reason } } as unknown as RenderManifest;
}

function validateRequiredString(
  obj: Record<string, unknown>,
  field: string,
  errors: ParseError[],
): void {
  if (!(field in obj) || obj[field] === undefined) {
    errors.push({
      path: field,
      code: 'MISSING_FIELD',
      message: `Required field "${field}" is missing`,
    });
  } else if (typeof obj[field] !== 'string') {
    errors.push({
      path: field,
      code: 'INVALID_TYPE',
      message: `Field "${field}" must be a string`,
      expected: 'string',
      actual: typeof obj[field],
    });
  }
}

function validateRequiredNumber(
  obj: Record<string, unknown>,
  field: string,
  errors: ParseError[],
  min?: number,
  max?: number,
): void {
  if (!(field in obj) || obj[field] === undefined) {
    errors.push({
      path: field,
      code: 'MISSING_FIELD',
      message: `Required field "${field}" is missing`,
    });
  } else if (typeof obj[field] !== 'number' || isNaN(obj[field] as number)) {
    errors.push({
      path: field,
      code: 'INVALID_TYPE',
      message: `Field "${field}" must be a number`,
      expected: 'number',
      actual: typeof obj[field],
    });
  } else {
    const val = obj[field] as number;
    if (min !== undefined && val < min) {
      errors.push({
        path: field,
        code: 'INVALID_VALUE',
        message: `Field "${field}" must be >= ${min}, got ${val}`,
        expected: `>= ${min}`,
        actual: String(val),
      });
    }
    if (max !== undefined && val > max) {
      errors.push({
        path: field,
        code: 'INVALID_VALUE',
        message: `Field "${field}" must be <= ${max}, got ${val}`,
        expected: `<= ${max}`,
        actual: String(val),
      });
    }
  }
}

function validateRequiredArray(
  obj: Record<string, unknown>,
  field: string,
  errors: ParseError[],
  itemValidator: (item: unknown, path: string, errors: ParseError[]) => void,
): void {
  if (!(field in obj) || obj[field] === undefined) {
    errors.push({
      path: field,
      code: 'MISSING_FIELD',
      message: `Required field "${field}" is missing`,
    });
  } else if (!Array.isArray(obj[field])) {
    errors.push({
      path: field,
      code: 'INVALID_TYPE',
      message: `Field "${field}" must be an array`,
      expected: 'array',
      actual: typeof obj[field],
    });
  } else {
    (obj[field] as unknown[]).forEach((item, i) => {
      itemValidator(item, `${field}[${i}]`, errors);
    });
  }
}

function validateTrack(item: unknown, path: string, errors: ParseError[]): void {
  if (typeof item !== 'object' || item === null || Array.isArray(item)) {
    errors.push({
      path,
      code: 'INVALID_TYPE',
      message: `Track at "${path}" must be an object`,
      expected: 'object',
      actual: Array.isArray(item) ? 'array' : typeof item,
    });
    return;
  }

  const track = item as Record<string, unknown>;

  // id
  if (!('id' in track) || track.id === undefined) {
    errors.push({ path: `${path}.id`, code: 'MISSING_FIELD', message: `Required field "${path}.id" is missing` });
  } else if (typeof track.id !== 'string') {
    errors.push({ path: `${path}.id`, code: 'INVALID_TYPE', message: `Field "${path}.id" must be a string`, expected: 'string', actual: typeof track.id });
  }

  // type
  if (!('type' in track) || track.type === undefined) {
    errors.push({ path: `${path}.type`, code: 'MISSING_FIELD', message: `Required field "${path}.type" is missing` });
  } else if (typeof track.type !== 'string') {
    errors.push({ path: `${path}.type`, code: 'INVALID_TYPE', message: `Field "${path}.type" must be a string`, expected: 'string', actual: typeof track.type });
  } else if (!VALID_TRACK_TYPES.has(track.type)) {
    errors.push({ path: `${path}.type`, code: 'INVALID_VALUE', message: `Field "${path}.type" must be one of video, audio, overlay, subtitle`, expected: 'video | audio | overlay | subtitle', actual: track.type });
  }

  // src
  if (!('src' in track) || track.src === undefined) {
    errors.push({ path: `${path}.src`, code: 'MISSING_FIELD', message: `Required field "${path}.src" is missing` });
  } else if (typeof track.src !== 'string') {
    errors.push({ path: `${path}.src`, code: 'INVALID_TYPE', message: `Field "${path}.src" must be a string`, expected: 'string', actual: typeof track.src });
  }

  // start
  if (!('start' in track) || track.start === undefined) {
    errors.push({ path: `${path}.start`, code: 'MISSING_FIELD', message: `Required field "${path}.start" is missing` });
  } else if (typeof track.start !== 'number' || isNaN(track.start as number)) {
    errors.push({ path: `${path}.start`, code: 'INVALID_TYPE', message: `Field "${path}.start" must be a number`, expected: 'number', actual: typeof track.start });
  }

  // end
  if (!('end' in track) || track.end === undefined) {
    errors.push({ path: `${path}.end`, code: 'MISSING_FIELD', message: `Required field "${path}.end" is missing` });
  } else if (typeof track.end !== 'number' || isNaN(track.end as number)) {
    errors.push({ path: `${path}.end`, code: 'INVALID_TYPE', message: `Field "${path}.end" must be a number`, expected: 'number', actual: typeof track.end });
  }
}

function validateClip(item: unknown, path: string, errors: ParseError[]): void {
  if (typeof item !== 'object' || item === null || Array.isArray(item)) {
    errors.push({
      path,
      code: 'INVALID_TYPE',
      message: `Clip at "${path}" must be an object`,
      expected: 'object',
      actual: Array.isArray(item) ? 'array' : typeof item,
    });
    return;
  }

  const clip = item as Record<string, unknown>;

  // id
  if (!('id' in clip) || clip.id === undefined) {
    errors.push({ path: `${path}.id`, code: 'MISSING_FIELD', message: `Required field "${path}.id" is missing` });
  } else if (typeof clip.id !== 'string') {
    errors.push({ path: `${path}.id`, code: 'INVALID_TYPE', message: `Field "${path}.id" must be a string`, expected: 'string', actual: typeof clip.id });
  }

  // name
  if (!('name' in clip) || clip.name === undefined) {
    errors.push({ path: `${path}.name`, code: 'MISSING_FIELD', message: `Required field "${path}.name" is missing` });
  } else if (typeof clip.name !== 'string') {
    errors.push({ path: `${path}.name`, code: 'INVALID_TYPE', message: `Field "${path}.name" must be a string`, expected: 'string', actual: typeof clip.name });
  } else if ((clip.name as string).length > 128) {
    errors.push({ path: `${path}.name`, code: 'INVALID_VALUE', message: `Field "${path}.name" must be at most 128 characters`, expected: '<= 128 chars', actual: `${(clip.name as string).length} chars` });
  }

  // start
  if (!('start' in clip) || clip.start === undefined) {
    errors.push({ path: `${path}.start`, code: 'MISSING_FIELD', message: `Required field "${path}.start" is missing` });
  } else if (typeof clip.start !== 'number' || isNaN(clip.start as number)) {
    errors.push({ path: `${path}.start`, code: 'INVALID_TYPE', message: `Field "${path}.start" must be a number`, expected: 'number', actual: typeof clip.start });
  }

  // end
  if (!('end' in clip) || clip.end === undefined) {
    errors.push({ path: `${path}.end`, code: 'MISSING_FIELD', message: `Required field "${path}.end" is missing` });
  } else if (typeof clip.end !== 'number' || isNaN(clip.end as number)) {
    errors.push({ path: `${path}.end`, code: 'INVALID_TYPE', message: `Field "${path}.end" must be a number`, expected: 'number', actual: typeof clip.end });
  }

  // sourceTrack
  if (!('sourceTrack' in clip) || clip.sourceTrack === undefined) {
    errors.push({ path: `${path}.sourceTrack`, code: 'MISSING_FIELD', message: `Required field "${path}.sourceTrack" is missing` });
  } else if (typeof clip.sourceTrack !== 'string') {
    errors.push({ path: `${path}.sourceTrack`, code: 'INVALID_TYPE', message: `Field "${path}.sourceTrack" must be a string`, expected: 'string', actual: typeof clip.sourceTrack });
  }
}

function validateCaptionCue(item: unknown, path: string, errors: ParseError[]): void {
  if (typeof item !== 'object' || item === null || Array.isArray(item)) {
    errors.push({
      path,
      code: 'INVALID_TYPE',
      message: `CaptionCue at "${path}" must be an object`,
      expected: 'object',
      actual: Array.isArray(item) ? 'array' : typeof item,
    });
    return;
  }

  const cue = item as Record<string, unknown>;

  // start
  if (!('start' in cue) || cue.start === undefined) {
    errors.push({ path: `${path}.start`, code: 'MISSING_FIELD', message: `Required field "${path}.start" is missing` });
  } else if (typeof cue.start !== 'number' || isNaN(cue.start as number)) {
    errors.push({ path: `${path}.start`, code: 'INVALID_TYPE', message: `Field "${path}.start" must be a number`, expected: 'number', actual: typeof cue.start });
  }

  // end
  if (!('end' in cue) || cue.end === undefined) {
    errors.push({ path: `${path}.end`, code: 'MISSING_FIELD', message: `Required field "${path}.end" is missing` });
  } else if (typeof cue.end !== 'number' || isNaN(cue.end as number)) {
    errors.push({ path: `${path}.end`, code: 'INVALID_TYPE', message: `Field "${path}.end" must be a number`, expected: 'number', actual: typeof cue.end });
  }

  // speaker
  if (!('speaker' in cue) || cue.speaker === undefined) {
    errors.push({ path: `${path}.speaker`, code: 'MISSING_FIELD', message: `Required field "${path}.speaker" is missing` });
  } else if (typeof cue.speaker !== 'string') {
    errors.push({ path: `${path}.speaker`, code: 'INVALID_TYPE', message: `Field "${path}.speaker" must be a string`, expected: 'string', actual: typeof cue.speaker });
  } else {
    const len = (cue.speaker as string).length;
    if (len < 1 || len > 50) {
      errors.push({ path: `${path}.speaker`, code: 'INVALID_VALUE', message: `Field "${path}.speaker" must be 1–50 characters`, expected: '1–50 chars', actual: `${len} chars` });
    }
  }

  // text
  if (!('text' in cue) || cue.text === undefined) {
    errors.push({ path: `${path}.text`, code: 'MISSING_FIELD', message: `Required field "${path}.text" is missing` });
  } else if (typeof cue.text !== 'string') {
    errors.push({ path: `${path}.text`, code: 'INVALID_TYPE', message: `Field "${path}.text" must be a string`, expected: 'string', actual: typeof cue.text });
  } else {
    const len = (cue.text as string).length;
    if (len < 1 || len > 300) {
      errors.push({ path: `${path}.text`, code: 'INVALID_VALUE', message: `Field "${path}.text" must be 1–300 characters`, expected: '1–300 chars', actual: `${len} chars` });
    }
  }
}

// ---------------------------------------------------------------------------
// JSON error position extraction
// ---------------------------------------------------------------------------

function extractJsonErrorPosition(json: string, error: unknown): { line: number; column: number } {
  const msg = error instanceof Error ? error.message : String(error);

  // Most JS engines include "at position N" or "at line L column C"
  const posMatch = msg.match(/position\s+(\d+)/i);
  if (posMatch) {
    const pos = parseInt(posMatch[1], 10);
    return offsetToLineCol(json, pos);
  }

  const lineColMatch = msg.match(/line\s+(\d+)\s+column\s+(\d+)/i);
  if (lineColMatch) {
    return { line: parseInt(lineColMatch[1], 10), column: parseInt(lineColMatch[2], 10) };
  }

  // Fallback: line 1, column 1
  return { line: 1, column: 1 };
}

function offsetToLineCol(str: string, offset: number): { line: number; column: number } {
  let line = 1;
  let col = 1;
  for (let i = 0; i < offset && i < str.length; i++) {
    if (str[i] === '\n') {
      line++;
      col = 1;
    } else {
      col++;
    }
  }
  return { line, column: col };
}
