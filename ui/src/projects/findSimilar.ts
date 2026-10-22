// Find_Similar — start a new chat turn that searches for Moments like a given card
// (media-harness-search-qa-rendering design Component 7 "Find_Similar", Req 3.23, 3.24).
//
// A Moment card's "Find similar" action dispatches a `chat:send` event carrying both the Actor-visible
// message AND a `findSimilar` payload. `ChatCanvas` forwards that payload into the turn body as
// `find_similar_ref`; the proxy turn gate (`ui/server/projects/turnGate.ts`), NOT the client, resolves the
// reference to the turn context's `find_similar` object and builds the image URI. No URL and no S3 URI ever
// appears in the Actor message (design "Find_Similar"): the text names only the Display_Name and timecode.
//
// Enablement (Req 3.24): an image card can always Find similar; a video card can only when it carries a
// `frame_s3_key` (a `shot` Hit); an audio card has no Find similar at all. The visual disabled/absent state
// is the caller's (`MomentCard`); `canFindSimilar` is the single predicate both share.
import type { PlayableKind } from '../shared/searchKinds';
import { formatCitation, formatCitationTimecode } from '../components/chat/citations';
import type { MomentItem } from '../components/renderer/types';

/** The en-dash (U+2013) the Citation_Formatter writes between timecodes; kept identical here. */
const DASH = '\u2013';

/** The `findSimilar` detail carried on the `chat:send` event and forwarded as the body's `find_similar_ref`. */
export interface FindSimilarPayload {
  project_id: string;
  asset_id: string;
  /** The `shot` Hit's frame key for a video; `null` for an image (the proxy uses the asset's rendition). */
  frame_s3_key: string | null;
  kind: 'video' | 'image' | 'audio';
  /** The card's Display_Name; the proxy overrides it from the registry row, so it is advisory only. */
  display_name: string;
  /** The Citation timecode span, e.g. `01:14`+en-dash+`01:36`; the proxy validates and may drop it. */
  timecode: string;
}

/** The `chat:send` event detail a Find_Similar action emits. */
export interface FindSimilarChatSendDetail {
  prompt: string;
  displayText: string;
  sessionId: string;
  findSimilar: FindSimilarPayload;
}

/** The fields of a Moment a Find_Similar needs (a `MomentItem`, or any shape carrying them). */
export type FindSimilarMoment = Pick<
  MomentItem,
  'ref' | 'display_name' | 'start_ms' | 'end_ms'
> & { kind: PlayableKind }; // a document page has no frame or span to search from

/**
 * Whether a card can start a Find_Similar search: an image always can; a video can only when it has a
 * `frame_s3_key`; audio never can (Req 3.24). The caller decides whether an unavailable action is rendered
 * disabled (video without a frame) or absent (audio).
 */
export function canFindSimilar(moment: Pick<FindSimilarMoment, 'kind' | 'ref'>): boolean {
  if (moment.kind === 'image') return true;
  if (moment.kind === 'video') return typeof moment.ref.frame_s3_key === 'string' && moment.ref.frame_s3_key !== '';
  return false; // audio
}

/**
 * The Citation timecode span for a Moment, e.g. `01:14`+en-dash+`01:36` (floor on the start second, ceil on
 * the end second — exactly the Citation_Formatter's rule). An image card has no timecode of its own, so its
 * `start_ms`/`end_ms` (0–1 ms) render as `00:00`+en-dash+`00:01`, sharing one format with video.
 */
export function findSimilarTimecode(moment: Pick<FindSimilarMoment, 'start_ms' | 'end_ms'>): string {
  const start = Math.floor(Math.max(0, moment.start_ms) / 1000);
  const end = Math.ceil(Math.max(moment.start_ms, moment.end_ms) / 1000);
  return `${formatCitationTimecode(start)}${DASH}${formatCitationTimecode(end)}`;
}

/**
 * The Actor-visible message for a Find_Similar turn, e.g.
 * `Find moments similar to [nba.mp4 @ 01:14`+en-dash+`01:36]`. No URL, no S3 URI (design "Find_Similar").
 */
export function findSimilarMessage(moment: FindSimilarMoment): string {
  return `Find moments similar to ${formatCitation(moment.display_name, moment.start_ms, moment.end_ms)}`;
}

/** The `findSimilar` payload forwarded as the turn body's `find_similar_ref`. */
export function findSimilarPayload(moment: FindSimilarMoment): FindSimilarPayload {
  return {
    project_id: moment.ref.project_id,
    asset_id: moment.ref.asset_id,
    frame_s3_key: moment.ref.frame_s3_key,
    kind: moment.kind,
    display_name: moment.display_name,
    timecode: findSimilarTimecode(moment),
  };
}

/**
 * Start a Find_Similar turn for `moment` in `sessionId` by dispatching a `chat:send` event. The event
 * carries the clean Actor message as both `prompt` and `displayText` and the `findSimilar` payload that
 * `ChatCanvas` forwards into the turn body as `find_similar_ref`. Does nothing when the card cannot Find
 * similar (Req 3.24), so an accidental call on an audio or frameless-video card is a no-op.
 */
export function findSimilar(moment: FindSimilarMoment, sessionId: string): void {
  if (!canFindSimilar(moment)) return;
  const text = findSimilarMessage(moment);
  const detail: FindSimilarChatSendDetail = {
    prompt: text,
    displayText: text,
    sessionId,
    findSimilar: findSimilarPayload(moment),
  };
  window.dispatchEvent(new CustomEvent<FindSimilarChatSendDetail>('chat:send', { detail }));
}
