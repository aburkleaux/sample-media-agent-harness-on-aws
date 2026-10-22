import React, { useContext, useMemo, useState } from 'react';
import { ChatAssetContext } from '../../chat/InlineAssetImage';
import { FileLocation } from '../../projects/FileLocation';
import { useAssetFolders } from '../../../projects/assetFolders';
import { useLocationAssets } from '../../../projects/useLocationAssets';
import type { RendererProps } from '../types';
import type { RenderManifest } from '../../nle/manifestParser';
import ArtifactCards, { type ArtifactDescriptor } from './ArtifactCards';
import { useResolvedArtifactData } from '../../../artifacts/useArtifactMedia';

/**
 * GenericPreview — the `generic-preview` template.
 *
 * Shown when a result has no timeline to place its artifacts on. Instead of an
 * empty player it presents what actually exists: an image gallery with a
 * lightbox plus labelled cards for data files. Purely manifest-driven, so any
 * tool's artifacts render here without special-casing.
 */

const IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'];

interface GenericPreviewData {
  manifest?: RenderManifest;
  manifestUrl?: string;
}

function isImage(url: string, mime?: string): boolean {
  if (mime?.startsWith('image/')) return true;
  const path = url.split('?')[0].toLowerCase();
  return IMAGE_EXTENSIONS.some((ext) => path.endsWith(ext));
}

function labelFor(url: string): string {
  const file = url.split('?')[0].split('/').filter(Boolean).pop() || 'artifact';
  return file.replace(/\.[^.]+$/, '').replace(/[-_]/g, ' ');
}

function readArtifacts(manifest: RenderManifest): ArtifactDescriptor[] {
  const declared = (manifest as { artifacts?: unknown }).artifacts;
  if (Array.isArray(declared)) {
    const parsed = declared.filter(
      (item): item is ArtifactDescriptor =>
        !!item && typeof item === 'object' && typeof (item as ArtifactDescriptor).url === 'string',
    );
    if (parsed.length > 0) return parsed;
  }
  const sources = (manifest as { sources?: unknown }).sources;
  const sourceUrl = (manifest as { sourceUrl?: unknown }).sourceUrl;
  const urls = Array.isArray(sources)
    ? sources.filter((s): s is string => typeof s === 'string')
    : typeof sourceUrl === 'string' && sourceUrl
      ? [sourceUrl]
      : [];
  return urls.map((url) => ({ url, label: labelFor(url) }));
}

export default function GenericPreview({ data, title, theme: t }: RendererProps<GenericPreviewData>) {
  // Presign the manifest's artifact identifiers here (ISSUE 2a). ManifestRouter normally resolves
  // them before this template mounts, but the login-free deep-link path mounts templates with
  // `resolveArtifacts=false`, so a raw `/api/artifact?key=…` or private-CDN src reached the `<img>`
  // token-less and the proxy answered 401 `missing_token`. This hook is a no-op on the resolved
  // path (strings are already presigned → `artifactKeyOf` returns null) and fails open to the raw
  // url, so it only ever ADDS a presign on the un-resolved path.
  // Fail open to the raw manifest while a cold-cache resolve is in flight (same pattern as
  // ImageAnnotated's `?? rawData`): render the raw src at once, then swap to the presigned copy
  // when resolution lands. Blocking on null here hid every image until resolution settled, which
  // emptied the panel on the synchronous render.
  const manifest = useResolvedArtifactData(data.manifest ?? null) ?? data.manifest ?? null;
  const [zoomed, setZoomed] = useState<string | null>(null);

  const artifacts = useMemo(() => (manifest ? readArtifacts(manifest) : []), [manifest]);
  const images = useMemo(() => artifacts.filter((a) => isImage(a.url, a.mime)), [artifacts]);
  const others = useMemo(() => artifacts.filter((a) => !isImage(a.url, a.mime)), [artifacts]);

  // Where each image is filed in the chat's project (looked up by the manifest's own, stable URLs). One line above
  // the grid when they all share a folder; otherwise each tile names its own. The lightbox always says.
  const projectId = useContext(ChatAssetContext)?.projectId ?? null;
  const rawImageUrls = useMemo(() => {
    const raw = data.manifest ? readArtifacts(data.manifest).filter((a) => isImage(a.url, a.mime)).map((a) => a.url) : [];
    return raw.length === images.length ? raw : images.map((a) => a.url);
  }, [data.manifest, images]);
  const located = useLocationAssets(projectId, rawImageUrls);
  const imageIds = useMemo(() => rawImageUrls.map((u) => located?.[u] ?? null), [rawImageUrls, located]);
  const folders = useAssetFolders(projectId, imageIds.filter((x): x is string => !!x));
  const folderOf = (i: number): string | undefined => { const id = imageIds[i]; return id ? folders[id] : undefined; };
  const known = imageIds.map((_, i) => folderOf(i));
  const sharedFolder = images.length > 1 && known.every((f) => f !== undefined) && new Set(known).size === 1 ? known[0] : undefined;
  const zoomedIndex = zoomed ? images.findIndex((im) => im.url === zoomed) : -1;

  if (!manifest) {
    return <div style={{ padding: 16, color: t.colors.textSecondary }}>Loading preview…</div>;
  }

  if (artifacts.length === 0) {
    return (
      <div style={{ padding: 24, textAlign: 'center', color: t.colors.textSecondary, fontSize: 12 }}>
        No previewable artifacts in this render.
      </div>
    );
  }

  return (
    <div
      data-testid="generic-preview"
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
        background: t.colors.surface,
        border: `1px solid ${t.colors.border}`,
        borderRadius: t.radius.panel,
        overflow: 'hidden',
        paddingBottom: 10,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8, padding: '12px 14px 0' }}>
        <span style={{ fontSize: 14, fontWeight: 600, color: t.colors.textPrimary }}>
          {title || 'Artifacts'}
        </span>
        <span style={{ fontSize: 11, color: t.colors.textSecondary }}>
          {images.length > 0 ? `${images.length} image${images.length === 1 ? '' : 's'}` : ''}
          {images.length > 0 && others.length > 0 ? ' · ' : ''}
          {others.length > 0 ? `${others.length} data file${others.length === 1 ? '' : 's'}` : ''}
        </span>
      </div>

      {images.length === 1 && (
        <div data-testid="image-gallery" style={{ padding: '0 14px' }}>
          {/* A single image is the RESULT, not an attachment. It was previously
              rendered in the same 132px grid tile as a gallery member and
              cropped to a fixed 108px height, so a generated image arrived as a
              thumbnail of itself. Sized by its own aspect ratio here, capped by
              viewport height so a tall image cannot push the files row out of
              reach. */}
          <button
            type="button"
            onClick={() => setZoomed(images[0].url)}
            aria-label={`Enlarge ${images[0].label}`}
            style={{
              padding: 0,
              border: `1px solid ${t.colors.border}`,
              borderRadius: 10,
              background: t.colors.background,
              cursor: 'zoom-in',
              overflow: 'hidden',
              display: 'block',
              width: '100%',
              boxShadow: '0 1px 3px rgba(0,0,0,0.10), 0 6px 16px rgba(0,0,0,0.06)',
            }}
          >
            <img
              src={images[0].url}
              alt={images[0].label}
              style={{
                display: 'block',
                width: '100%',
                height: 'auto',
                maxHeight: '62vh',
                // `contain` rather than `cover`: cropping a generated image to
                // fill a box is exactly the complaint, and the whole frame is
                // the point of it.
                objectFit: 'contain',
                background: t.colors.surface,
              }}
            />
          </button>
          <div
            style={{
              fontSize: 11,
              color: t.colors.textSecondary,
              padding: '6px 2px 0',
              textAlign: 'left',
            }}
          >
            {images[0].label}
            {projectId && folderOf(0) !== undefined && <FileLocation projectId={projectId} folder={folderOf(0)} />}
          </div>
        </div>
      )}

      {images.length > 1 && projectId && sharedFolder !== undefined && (
        <div style={{ padding: '0 14px', marginBottom: -4 }} data-testid="gallery-location">
          <FileLocation variant="crumb" projectId={projectId} folder={sharedFolder} lead="All in" />
        </div>
      )}
      {images.length > 1 && (
        <div
          data-testid="image-gallery"
          style={{
            display: 'grid',
            // Was 132px, which made even two images read as thumbnails. 200px
            // with auto-fill keeps a pair side by side at panel width while
            // still reflowing to more columns when there are many.
            gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
            gap: 10,
            padding: '0 14px',
          }}
        >
          {images.map((image, imageIndex) => (
            <figure key={image.url} style={{ margin: 0, minWidth: 0 }}>
              <button
                type="button"
                onClick={() => setZoomed(image.url)}
                aria-label={`Enlarge ${image.label}`}
                style={{
                  padding: 0,
                  border: `1px solid ${t.colors.border}`,
                  borderRadius: 8,
                  background: t.colors.background,
                  cursor: 'zoom-in',
                  overflow: 'hidden',
                  display: 'block',
                  width: '100%',
                  boxShadow: '0 1px 2px rgba(0,0,0,0.06)',
                }}
              >
                <img
                  src={image.url}
                  alt={image.label}
                  loading="lazy"
                  style={{
                    display: 'block',
                    width: '100%',
                    // A consistent 4:3 keeps the grid tidy without hard-coding a
                    // pixel height that crops portrait images to a letterbox.
                    aspectRatio: '4 / 3',
                    objectFit: 'cover',
                  }}
                />
              </button>
              <figcaption
                style={{
                  fontSize: 10,
                  color: t.colors.textSecondary,
                  padding: '4px 2px 0',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  textAlign: 'left',
                }}
              >
                {image.label}
                {projectId && sharedFolder === undefined && folderOf(imageIndex) !== undefined && (
                  <FileLocation projectId={projectId} folder={folderOf(imageIndex)} />
                )}
              </figcaption>
            </figure>
          ))}
        </div>
      )}

      <ArtifactCards artifacts={others.length > 0 ? others : artifacts} theme={t} title="Files" />

      {zoomed && (
        <div
          role="dialog"
          aria-label="Enlarged artifact"
          onClick={() => setZoomed(null)}
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.72)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1000,
            cursor: 'zoom-out',
            padding: 24,
          }}
        >
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, maxWidth: '92vw' }}>
            <img src={zoomed} alt="Enlarged artifact" style={{ maxWidth: '92vw', maxHeight: '84vh', borderRadius: 6 }} />
            {zoomedIndex >= 0 && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: '#e5e7eb', fontSize: 12.5, maxWidth: '100%', minWidth: 0 }}
                onClick={(e) => e.stopPropagation()} data-testid="lightbox-caption">
                <b style={{ fontWeight: 600, whiteSpace: 'nowrap' }}>{images[zoomedIndex].label}</b>
                {projectId && folderOf(zoomedIndex) !== undefined && (
                  <FileLocation variant="crumb" dark projectId={projectId} folder={folderOf(zoomedIndex)} />
                )}
                {images.length > 1 && <span style={{ color: '#9ca3af', whiteSpace: 'nowrap' }}>{zoomedIndex + 1} of {images.length}</span>}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
