// What kind of media a chat attachment is, from its REAL file path first.
//
// The display name is not reliable: a project file added twice is named `clip.mp4 (2)` (the registry's
// ` (n)` suffix goes after the extension), so a name test misses it. In session fd4ce1bd that made the
// second of two attached videos skip the Renderer panel AND show as a play-video link in the chat.
// The URL / key always keep the stored object's extension.

const IMAGE = /\.(png|jpe?g|gif|webp|bmp|svg|avif)(\?|$)/i;
const VIDEO = /\.(mp4|mov|m4v|webm|mkv|avi)(\?|$)/i;
const AUDIO = /\.(mp3|wav|m4a|aac|ogg|flac)(\?|$)/i;
const COPY_SUFFIX = / \(\d+\)$/u;

export type AttachmentKind = 'image' | 'video' | 'audio' | 'other';

function kindOf(s: string): AttachmentKind | null {
  if (IMAGE.test(s)) return 'image';
  if (VIDEO.test(s)) return 'video';
  if (AUDIO.test(s)) return 'audio';
  return null;
}

function pathOf(url: string): string {
  try {
    return new URL(url).pathname;
  } catch {
    return url;
  }
}

/** The attachment's kind: from the URL's path, else the key, else the name without a ` (n)` suffix. */
export function attachmentKind(a: { url?: string; key?: string; name?: string }): AttachmentKind {
  return (a.url ? kindOf(pathOf(a.url)) : null)
    ?? (a.key ? kindOf(a.key) : null)
    ?? (a.name ? kindOf(a.name.trim().replace(COPY_SUFFIX, '')) : null)
    ?? 'other';
}
