/**
 * Client-side asset kind and size helpers for the Upload_Client.
 *
 * The server decides the kind of every upload (from the declared content type, falling back to the extension,
 * anything else is `document`). The client applies the same rule only to pick the size limit it validates
 * against before any request, so a file the server would refuse as too large never starts transferring.
 */
import type { IngestionCapabilities, ProjectAssetKind } from '../api';

const VIDEO_EXT = new Set(['mp4', 'mov', 'm4v', 'webm', 'mkv', 'avi', 'mpg', 'mpeg', 'mxf', 'ts', 'mts', 'wmv', 'flv', '3gp']);
const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'tif', 'tiff', 'heic', 'heif', 'avif']);
const AUDIO_EXT = new Set(['mp3', 'wav', 'm4a', 'aac', 'ogg', 'oga', 'flac', 'aif', 'aiff', 'opus', 'wma']);

/** The extension of `name`, lower-cased and without the dot, or `''`. */
export function extensionOf(name: string): string {
  const m = /\.([A-Za-z0-9]{1,10})$/.exec(name || '');
  return m ? m[1].toLowerCase() : '';
}

/** The kind of a file: from its content type, else its extension, else `document`. */
export function kindOf(contentType: string | undefined | null, name: string): ProjectAssetKind {
  const ct = (contentType || '').toLowerCase();
  if (ct.startsWith('video/')) return 'video';
  if (ct.startsWith('image/')) return 'image';
  if (ct.startsWith('audio/')) return 'audio';
  const ext = extensionOf(name);
  if (VIDEO_EXT.has(ext)) return 'video';
  if (IMAGE_EXT.has(ext)) return 'image';
  if (AUDIO_EXT.has(ext)) return 'audio';
  return 'document';
}

/** The values `GET …/ingestion/capabilities` returns today; used until (or if) the call cannot be read. */
export const DEFAULT_CAPABILITIES: IngestionCapabilities = {
  maxFilesPerBatch: 20,
  limits: { video: 5368709120, audio: 5368709120, image: 52428800, document: 524288000 },
  multipartThresholdBytes: 104857600,
  partSizeBytes: 67108864,
};

/** Compact human-readable byte size ("1.4 GB", "512 MB", "12 KB"). */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  const GB = 1024 ** 3, MB = 1024 ** 2, KB = 1024;
  if (bytes >= GB) return `${(bytes / GB).toFixed(bytes >= 10 * GB ? 0 : 1)} GB`;
  if (bytes >= MB) return `${Math.round(bytes / MB)} MB`;
  if (bytes >= KB) return `${Math.round(bytes / KB)} KB`;
  return `${bytes} B`;
}

/** The content type to declare for a file: the browser's, else a generic binary type. */
export function contentTypeOf(file: { type?: string }): string {
  return file.type && file.type.trim() ? file.type : 'application/octet-stream';
}
