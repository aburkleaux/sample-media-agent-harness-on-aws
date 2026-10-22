/**
 * annotations — pure helpers for the `video-annotations` template.
 *
 * The Render Agent emits normalized, time-bounded boxes (see
 * `_build_video_annotations_manifest`). Detections are *sampled* (often about
 * once per second), so playback needs two behaviours to look right:
 *
 *   - hold the current box until its interval ends, and
 *   - interpolate towards the next box of the same entity when the samples are
 *     close together, so a moving subject's box travels instead of jumping.
 *
 * Keeping this logic pure makes it directly testable and keeps the component
 * focused on presentation.
 */

export interface AnnotationBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Annotation {
  entityId: string;
  start: number;
  end: number;
  box: AnnotationBox;
  confidence?: number;
  /** Resolved person name / label when face recognition matches a collection (else absent). */
  name?: string;
  label?: string;
}

export interface AnnotationEntity {
  id: string;
  label: string;
  confidence?: number;
  thumbnail?: string;
  screenTimeSec?: number;
  detectionCount?: number;
  appearanceCount?: number;
  named?: boolean;
  nameSource?: string;
}

export interface Appearance {
  entityId: string;
  start: number;
  end: number;
}

export interface ActiveBox {
  entityId: string;
  box: AnnotationBox;
  confidence?: number;
}

/** Max sample gap (seconds) across which box motion is interpolated. */
const INTERPOLATION_WINDOW_SECONDS = 2.5;

const clamp01 = (value: number): number => Math.min(Math.max(value, 0), 1);

function lerpBox(from: AnnotationBox, to: AnnotationBox, ratio: number): AnnotationBox {
  const r = clamp01(ratio);
  return {
    x: from.x + (to.x - from.x) * r,
    y: from.y + (to.y - from.y) * r,
    width: from.width + (to.width - from.width) * r,
    height: from.height + (to.height - from.height) * r,
  };
}

/** How long a single-`time` detection sample is held when the manifest gives no end (samples are ~1 fps). */
const POINT_SAMPLE_HOLD_SECONDS = 1;

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

/**
 * Accept both annotation shapes a manifest may carry and drop anything unusable:
 *  - canonical `{entityId, start, end, box: {x, y, width, height}}` (the video-annotations builder);
 *  - flat detection samples `{x, y, w, h, label, time, frame?}` (shot-segmentation + people timelines), which
 *    became `{entityId: label, start: time, end: time + 1s, box}`.
 * A row without a finite start, or without a box, is dropped rather than passed on to crash a renderer.
 */
export function normalizeAnnotations(raw: unknown): Annotation[] {
  if (!Array.isArray(raw)) return [];
  const out: Annotation[] = [];
  for (const r of raw) {
    if (!r || typeof r !== 'object') continue;
    const a = r as Record<string, unknown>;
    const start = finite(a.start) ? a.start : finite(a.time) ? a.time : undefined;
    if (start === undefined) continue;
    const end = finite(a.end) && a.end >= start ? a.end
      : finite(a.endTime) && a.endTime >= start ? a.endTime
        : start + POINT_SAMPLE_HOLD_SECONDS;
    const b = (a.box && typeof a.box === 'object' ? a.box : a) as Record<string, unknown>;
    const width = finite(b.width) ? b.width : b.w;
    const height = finite(b.height) ? b.height : b.h;
    if (!finite(b.x) || !finite(b.y) || !finite(width) || !finite(height)) continue;
    const entityId = typeof a.entityId === 'string' && a.entityId
      ? a.entityId
      : typeof a.label === 'string' && a.label ? a.label : typeof a.name === 'string' && a.name ? a.name : 'unknown';
    out.push({
      entityId, start, end, box: { x: b.x, y: b.y, width, height },
      ...(finite(a.confidence) ? { confidence: a.confidence } : {}),
      ...(typeof a.name === 'string' ? { name: a.name } : {}),
      ...(typeof a.label === 'string' ? { label: a.label } : {}),
    });
  }
  return out;
}

/** Group annotations by entity, each list ordered by start time. */
export function groupAnnotationsByEntity(
  annotations: Annotation[],
): Map<string, Annotation[]> {
  const grouped = new Map<string, Annotation[]>();
  for (const annotation of annotations) {
    const list = grouped.get(annotation.entityId);
    if (list) list.push(annotation);
    else grouped.set(annotation.entityId, [annotation]);
  }
  for (const list of grouped.values()) list.sort((a, b) => a.start - b.start);
  return grouped;
}

/**
 * Boxes visible at `time`, one per entity, interpolated between nearby samples.
 *
 * `visibleEntityIds` filters the result; omit it to include every entity.
 */
export function activeBoxesAt(
  annotations: Annotation[],
  time: number,
  visibleEntityIds?: ReadonlySet<string>,
): ActiveBox[] {
  const out: ActiveBox[] = [];
  for (const [entityId, list] of groupAnnotationsByEntity(annotations)) {
    if (visibleEntityIds && !visibleEntityIds.has(entityId)) continue;
    const index = list.findIndex((a) => time >= a.start && time < a.end);
    if (index === -1) continue;
    const current = list[index];
    const next = list[index + 1];
    let box = current.box;
    if (next && next.start - current.start > 0 && next.start - current.start <= INTERPOLATION_WINDOW_SECONDS) {
      box = lerpBox(current.box, next.box, (time - current.start) / (next.start - current.start));
    }
    out.push(
      current.confidence === undefined
        ? { entityId, box }
        : { entityId, box, confidence: current.confidence },
    );
  }
  return out;
}

/** Merge overlapping/adjacent appearances per entity for a clean timeline. */
export function mergeAppearances(appearances: Appearance[], entityId: string): Appearance[] {
  const ordered = appearances
    .filter((a) => a.entityId === entityId && a.end > a.start)
    .sort((a, b) => a.start - b.start);
  const merged: Appearance[] = [];
  for (const appearance of ordered) {
    const last = merged[merged.length - 1];
    if (last && appearance.start <= last.end + 0.001) {
      last.end = Math.max(last.end, appearance.end);
    } else {
      merged.push({ ...appearance });
    }
  }
  return merged;
}

/** Format seconds as m:ss (or h:mm:ss past an hour). */
export function formatTimecode(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const mm = hours > 0 ? String(minutes).padStart(2, '0') : String(minutes);
  return `${hours > 0 ? `${hours}:` : ''}${mm}:${String(secs).padStart(2, '0')}`;
}

/** Deterministic, readable colour per entity index. */
export function entityColor(index: number): string {
  const palette = [
    '#5b8def', '#e0518f', '#f0a83a', '#3fbf9f', '#8b5cf6',
    '#ef4444', '#14b8a6', '#eab308', '#ec4899', '#22c55e',
  ];
  return palette[index % palette.length];
}

/**
 * Human label for a detected person: a resolved name (from face recognition against a collection)
 * wins; otherwise anonymous ids like "entity-0" become a friendly, 1-based "Person 1". Used by both
 * the timeline people-lane label and the on-video bbox label so they agree.
 */
export function personLabel(entityId: string, sample?: { name?: string; label?: string }): string {
  if (sample?.name && sample.name.trim()) return sample.name.trim();
  if (sample?.label && sample.label.trim()) return sample.label.trim();
  const m = /(?:entity|person|face)[-_ ]?(\d+)/i.exec(entityId || '');
  return m ? `Person ${Number(m[1]) + 1}` : (entityId || 'Person');
}
