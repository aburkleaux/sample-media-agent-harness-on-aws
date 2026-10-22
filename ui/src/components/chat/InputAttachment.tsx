import React, { useRef } from 'react';
import { theme } from '../../styles/tokens';
import { ArtifactLink } from './ArtifactLink';
import { useArtifactMediaResign, useResolvedArtifactUrl } from '../../artifacts/useArtifactMedia';
import { attachmentKind } from './attachmentKind';

/**
 * Minimalistic inline preview for an uploaded input artifact in the chat bubble.
 * Previews the file directly (image/video/audio) — no collapse, no chrome — and
 * falls back to a simple link for non-previewable types. Type is inferred from the
 * url's extension, then the name's (see attachmentKind: a `clip.mp4 (2)` name hides it).
 */
export function InputAttachment({ url, name }: { url: string; name: string }) {
  const kind = attachmentKind({ url, name });
  // The upload URL is an artifact IDENTIFIER (a CDN URL that 403s once the artifacts CDN is
  // private), not something to load directly: resolve it to a presigned URL, and re-sign on a
  // media error after the signature expires (session b42731df: raw <img src=cdn> broke).
  const hostRef = useRef<HTMLElement>(null);
  const inline = kind === 'image' || kind === 'audio';
  useArtifactMediaResign(hostRef, inline);
  const resolved = useResolvedArtifactUrl(inline ? url : undefined);

  if (kind === 'video') {
    // The uploaded video is rendered in the Renderer panel (the NLE timeline) the moment
    // it's uploaded, and the bubble already NAMES the file in its text ("📎 <name> (size)").
    // A chat chip on top of that is the redundant "render in the chat" — drop it entirely.
    return null;
  }

  if (kind === 'other') {
    return <ArtifactLink href={url}>{name}</ArtifactLink>;
  }

  // While resolving, reserve the box rather than loading the (403ing) identifier.
  const src = resolved.src;
  return (
    <figure ref={hostRef} style={figureStyle}>
      {kind === 'image' && (src
        ? <img src={src} alt={name} style={mediaStyle} />
        : <div aria-label={`Loading ${name}`} style={{ ...mediaStyle, height: 120 }} />)}
      {kind === 'audio' && src && <audio src={src} controls style={audioStyle} />}
      <figcaption style={captionStyle} title={name}>{name}</figcaption>
    </figure>
  );
}

const figureStyle: React.CSSProperties = {
  margin: 0,
  display: 'inline-block',
  maxWidth: 280,
};

const mediaStyle: React.CSSProperties = {
  display: 'block',
  width: '100%',
  maxWidth: 280,
  maxHeight: 200,
  objectFit: 'contain',
  borderRadius: theme.radius.panel,
  border: `1px solid ${theme.colors.border}`,
  background: theme.colors.surfaceSubtle,
};

const audioStyle: React.CSSProperties = {
  display: 'block',
  width: 280,
  height: 36,
};

const captionStyle: React.CSSProperties = {
  marginTop: 4,
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  maxWidth: 280,
};
