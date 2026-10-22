import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RenderFailure } from '../renderFailure';
import type { InteractionEvent, RendererProps } from '../types';
import { parseManifest, degradedManifestFromJson, type RenderManifest } from '../../nle/manifestParser';
import { authFetch } from '../../../auth/authFetch';
import {
  preserveDocumentPreview,
  resolveArtifactUrlsDeep,
  resolveArtifactUrlsDeepSync,
  unresolveArtifactUrls,
} from '../../../artifacts/artifactUrls';
import { useArtifactMediaResign } from '../../../artifacts/useArtifactMedia';
import NleTimeline from './NleTimeline';
import DocumentViewer from './DocumentViewer';
import GenericPreview from './GenericPreview';
import ImageAnnotated from './ImageAnnotated';
import DataTable from './DataTable';
import ClipGallery from './ClipGallery';
import AssetPlayer from './AssetPlayer';
import GraphView from './GraphView';
import ProjectBrowser from './ProjectBrowser';
import { manifestKeyOfUrl, sessionOfRenderKey } from '../renderTabKey';
import { restoredViewState, saveViewState } from '../viewStatePersist';
import type { RenderViewState } from '../../../shared/renderViewState';

/**
 * ManifestRouter — the single entry point for Render Agent manifests.
 *
 * The stream only carries a manifest key, so the template is unknown until the
 * manifest is fetched. Previously every manifest was declared as `nle-timeline`,
 * which meant non-timeline results (image galleries, annotation overlays) were
 * forced through the timeline renderer and appeared empty. This component
 * fetches once, reads `manifest.template`, and hands the parsed manifest to the
 * matching renderer — so adding a template is a UI registration, with no
 * changes to gateways, tools, or the streaming contract.
 */

const MANIFEST_LOAD_TIMEOUT_MS = 10_000;

// A just-produced manifest can be transiently unavailable: the proxy returns 410
// for a missing object (so CloudFront can't mask it as the SPA), and the render
// key reaches the UI across runtimes (render agent → main → SSE) around the same
// time the object lands. Rather than fail on the first miss, retry a few transient
// failures with backoff so an occasional race self-heals; a genuinely deleted
// manifest stays 410 and surfaces the error once the attempts are spent.
const MANIFEST_MAX_ATTEMPTS = 4;
const MANIFEST_RETRY_BASE_MS = 700;

/** Transient HTTP statuses worth retrying (timeout, missing-yet, too-early, throttle, 5xx). */
function isTransientManifestStatus(status: number): boolean {
  return status === 408 || status === 410 || status === 425 || status === 429 || status >= 500;
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

type TemplateRenderer = React.ComponentType<RendererProps<{ manifest: RenderManifest }>>;

// This is the routing table for MANIFESTS, and it is separate from
// RendererRegistry (which serves the direct-artifact path). A template registered
// there but missing here silently resolves to GenericPreview — which is exactly how
// specialised results ended up as generic previews.
const TEMPLATES: Record<string, TemplateRenderer> = {
  'nle-timeline': NleTimeline as TemplateRenderer,
  'composite-timeline': NleTimeline as TemplateRenderer,
  'document-viewer': DocumentViewer as TemplateRenderer,
  // Alias -> the timeline container (see RendererRegistry).
  'video-annotations': NleTimeline as unknown as TemplateRenderer,
  // Alias -> the timeline container (see RendererRegistry). Stored manifests keep working.
  'video-markers': NleTimeline as unknown as TemplateRenderer,
  'image-annotated': ImageAnnotated as unknown as TemplateRenderer,
  'data-table': DataTable as unknown as TemplateRenderer,
  'clip-gallery': ClipGallery as unknown as TemplateRenderer,
  // Single-asset citation/Moment player (Component 5). Reads its Asset_Reference off `data`, not
  // `data.manifest`; registered here so a stored manifest naming it still resolves.
  'asset-player': AssetPlayer as unknown as TemplateRenderer,
  // The agent's "↳ Graph" result (knowledge-graph tools, render-graph rule). Reads nodes/edges off `data`.
  'graph-view': GraphView as unknown as TemplateRenderer,
  // The agent's browse_project tab. Reads project_id/project_name/filter off `data`.
  'project-browser': ProjectBrowser as unknown as TemplateRenderer,
  'generic-preview': GenericPreview as TemplateRenderer,
};

/** Exported for testing: template → renderer resolution with a safe default. */
export function resolveTemplate(template: unknown): TemplateRenderer {
  if (typeof template === 'string' && TEMPLATES[template]) return TEMPLATES[template];
  // A manifest with real media tracks is still best served by the timeline;
  // anything else degrades to the artifact preview rather than an empty player.
  return GenericPreview as TemplateRenderer;
}

/** Exported for testing: pick the renderer for a fully parsed manifest. */
export function rendererForManifest(manifest: RenderManifest): TemplateRenderer {
  const template = (manifest as { template?: unknown }).template;
  if (typeof template === 'string' && TEMPLATES[template]) return TEMPLATES[template];
  if (Array.isArray(manifest.tracks) && manifest.tracks.length > 0) {
    return NleTimeline as TemplateRenderer;
  }
  return GenericPreview as TemplateRenderer;
}

interface ManifestRouterData {
  manifestUrl?: string;
  manifest?: RenderManifest;
  /**
   * A server composite's identity, kept on its tab when the inline manifest is stripped
   * (leanRenderHistory persists refs only). With no manifest to show yet, such a tab waits for the
   * server list to supply it — it is a known render, not a broken one.
   */
  compositeKey?: string;
  sourceKey?: string;
  memberKeys?: string[];
}

/** A tab that names a server composite (its manifest arrives with the next render-list merge). */
function isKnownComposite(data: ManifestRouterData): boolean {
  return !!(data.compositeKey || data.sourceKey || (Array.isArray(data.memberKeys) && data.memberKeys.length));
}

/**
 * The persisted view behind a tab: a manifest under a session's render prefix, addressed by its
 * `/api/artifact?key=` URL. Only such a tab saves its timeline state (R8.2).
 */
export function persistedViewOf(manifestUrl: unknown): { sessionId: string; key: string } | null {
  const key = manifestKeyOfUrl(manifestUrl);
  const sessionId = sessionOfRenderKey(key);
  return sessionId ? { sessionId, key } : null;
}

// Artifact identifiers in a manifest are resolved to short-lived presigned URLs HERE, once, before
// any template sees the manifest — so no template needs its own rewrite. `document.previewUrl` is
// kept: DocumentViewer frames it through `/api/artifact?…&view=<ticket>` (content-type guessing).
const RESOLVE_OPTS = { skip: preserveDocumentPreview };

/** Never rejects (resolveArtifactUrlsDeep keeps unresolvable strings as they were). */
async function resolveManifest(m: RenderManifest): Promise<RenderManifest> {
  try {
    return await resolveArtifactUrlsDeep(m, RESOLVE_OPTS);
  } catch {
    return m;
  }
}

type LoadState =
  | { status: 'loading' }
  | { status: 'loaded'; manifest: RenderManifest }
  | { status: 'error'; message: string };

interface ManifestRouterProps extends RendererProps<ManifestRouterData> {
  /**
   * Resolve artifact identifiers to presigned URLs (default). Only the login-free deep-link page
   * (RenderDeepLink) turns this off: resolution needs the authenticated `/api/artifact-urls`.
   */
  resolveArtifacts?: boolean;
}

export default function ManifestRouter({ data, title, theme: t, onInteraction, resolveArtifacts = true }: ManifestRouterProps) {
  const [state, setState] = useState<LoadState>(() => {
    // An inline manifest renders at once when it has no artifact identifiers or the cache is warm.
    const sync = data.manifest
      ? (resolveArtifacts ? resolveArtifactUrlsDeepSync(data.manifest, RESOLVE_OPTS) : data.manifest)
      : null;
    return sync ? { status: 'loaded', manifest: sync } : { status: 'loading' };
  });
  const hostRef = useRef<HTMLDivElement>(null);
  useArtifactMediaResign(hostRef, resolveArtifacts);

  // Keyed on the inline manifest's CONTENT: a poll re-delivers an equal manifest as a new object,
  // which must not re-resolve or hand the template a new object.
  const inlineSig = useMemo(() => (data.manifest ? JSON.stringify(data.manifest) : ''), [data.manifest]);
  const inlineRef = useRef(data.manifest);
  inlineRef.current = data.manifest;

  const load = useCallback(async (token?: { cancelled: boolean }) => {
    const inline = inlineRef.current;
    if (inline) {
      if (!resolveArtifacts) { setState({ status: 'loaded', manifest: inline }); return; }
      const sync = resolveArtifactUrlsDeepSync(inline, RESOLVE_OPTS);
      const resolved = sync ?? (await resolveManifest(inline));
      if (token?.cancelled) return;
      setState({ status: 'loaded', manifest: resolved });
      return;
    }
    if (!data.manifestUrl) {
      // A persisted composite tab carries refs only until the server list re-supplies its manifest:
      // show it loading, never as an error.
      setState(isKnownComposite(data) ? { status: 'loading' } : { status: 'error', message: 'No manifest URL provided' });
      return;
    }
    setState({ status: 'loading' });

    for (let attempt = 1; attempt <= MANIFEST_MAX_ATTEMPTS; attempt++) {
      if (token?.cancelled) return;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), MANIFEST_LOAD_TIMEOUT_MS);
      try {
        // authFetch: manifests are served by the auth-protected /api/artifact proxy.
        const res = await authFetch(data.manifestUrl, { signal: controller.signal });
        clearTimeout(timeout);
        if (token?.cancelled) return;
        if (!res.ok) {
          if (isTransientManifestStatus(res.status) && attempt < MANIFEST_MAX_ATTEMPTS) {
            await sleep(MANIFEST_RETRY_BASE_MS * attempt); // 700ms, 1.4s, 2.1s
            continue;
          }
          setState({ status: 'error', message: `Failed to load manifest (${res.status})` });
          return;
        }
        const text = await res.text();
        const parsed = parseManifest(text);
        if (!parsed.ok) {
          // A parse failure is deterministic — retrying can't help. But a timeline that is merely
          // DEGENERATE (duration 0, or a track with no time range — a dead analysis node) degrades to a
          // calm "no preview available" state via the renderer, instead of the hard "Could not display
          // this render" error (staging 12f6f088).
          const degraded = degradedManifestFromJson(text);
          if (degraded) {
            const resolvedDegraded = resolveArtifacts ? await resolveManifest(degraded) : degraded;
            if (token?.cancelled) return;
            setState({ status: 'loaded', manifest: resolvedDegraded });
            return;
          }
          setState({ status: 'error', message: parsed.errors.map((e) => e.message).join('; ') });
          return;
        }
        const resolved = resolveArtifacts ? await resolveManifest(parsed.manifest) : parsed.manifest;
        if (token?.cancelled) return;
        setState({ status: 'loaded', manifest: resolved });
        return;
      } catch (err) {
        clearTimeout(timeout);
        if (token?.cancelled) return;
        // Timeouts (AbortError) and network errors are transient too — retry, then report.
        if (attempt < MANIFEST_MAX_ATTEMPTS) {
          await sleep(MANIFEST_RETRY_BASE_MS * attempt);
          continue;
        }
        const message =
          err instanceof Error && err.name === 'AbortError'
            ? 'Manifest loading timed out (10s)'
            : `Failed to load manifest: ${err instanceof Error ? err.message : String(err)}`;
        setState({ status: 'error', message });
        return;
      }
    }
  }, [inlineSig, data.manifestUrl, data.compositeKey, data.sourceKey, resolveArtifacts]); // eslint-disable-line react-hooks/exhaustive-deps

  // Timeline state of a persisted view (selected range, frame marker, crop box): the template
  // restores it on mount and reports changes, which are PATCHed (debounced) into the manifest. Not
  // on the login-free deep-link page (`resolveArtifacts` off), which cannot write.
  const persisted = useMemo(
    () => (resolveArtifacts ? persistedViewOf(data.manifestUrl) : null),
    [data.manifestUrl, resolveArtifacts],
  );
  const loadedViewState = state.status === 'loaded' ? (state.manifest as { viewState?: unknown }).viewState : undefined;
  const viewState = useMemo<RenderViewState | undefined>(
    () => (persisted ? restoredViewState(persisted.key, loadedViewState) : undefined),
    // Re-read only when the view or its manifest's stored state changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [persisted, JSON.stringify(loadedViewState ?? null)],
  );
  const onViewStateChange = useCallback(
    (vs: RenderViewState) => { if (persisted) saveViewState(persisted.sessionId, persisted.key, vs); },
    [persisted],
  );

  // Interaction payloads (crop/trim actions, open-tab, gallery select) quote the src a template was
  // given — now a presigned URL. Map those back to the stable identifier before they reach chat
  // history, a render tab, or an agent prompt.
  const forwardInteraction = useCallback(
    (e: InteractionEvent) => onInteraction?.(unresolveArtifactUrls(e)),
    [onInteraction],
  );

  useEffect(() => {
    const token = { cancelled: false };
    void load(token);
    return () => { token.cancelled = true; };
  }, [load]);

  if (state.status === 'loading') {
    return (
      <div
        data-testid="manifest-router-loading"
        style={{ padding: 24, color: t.colors.textSecondary, fontSize: 12, textAlign: 'center' }}
      >
        Loading render…
      </div>
    );
  }

  if (state.status === 'error') {
    // Terminal (transient failures were already retried): inside a renderer panel the tab is dropped
    // instead of showing this card; standalone, the card still explains itself.
    return (
      <RenderFailure
        reason={`manifest: ${state.message}`}
        fallback={(
        <div
          data-testid="manifest-router-error"
          style={{
            padding: 24,
            background: t.colors.surface,
            border: `1px solid ${t.colors.border}`,
            borderRadius: t.radius.panel,
            textAlign: 'center',
          }}
        >
          <p style={{ color: t.colors.textPrimary, marginBottom: 8 }}>Could not display this render</p>
          <p style={{ color: t.colors.textSecondary, fontSize: 12, marginBottom: 12 }}>{state.message}</p>
          {data.manifestUrl && (
            <a
              href={data.manifestUrl}
              target="_blank"
              rel="noopener noreferrer"
              style={{ color: t.colors.primary, fontSize: 12, display: 'block', marginBottom: 12 }}
            >
              Open raw manifest
            </a>
          )}
          <button
            type="button"
            onClick={() => void load()}
            style={{
              padding: '6px 16px',
              background: t.colors.primary,
              color: '#fff',
              border: 'none',
              borderRadius: 4,
              cursor: 'pointer',
              fontSize: 12,
            }}
          >
            Retry
          </button>
        </div>
        )}
      />
    );
  }

  const Renderer = rendererForManifest(state.manifest);
  const templateData = { ...state.manifest, manifest: state.manifest };
  // A persisted view's timeline state + its change reporter (NleTimeline / AssetPlayer read them).
  const persistProps = { viewState, onViewStateChange };
  // Two kinds of template share this registry: manifest-shaped ones read
  // `data.manifest` (NleTimeline, VideoAnnotations), while those originally written
  // for the direct-artifact path read their props off `data` itself (DataTable
  // wants `rows`/`columns`, ImageAnnotated `src`/`annotations`, VideoMarkers
  // `src`/`markers`). Passing only `{ manifest }` left that second group with every
  // prop undefined — they resolve to a real component and then render nothing,
  // which is invisible in testing. Supplying both shapes lets the agent select any
  // registered template.
  // The host div (display: contents, so layout is unchanged) carries the ONE re-sign listener for
  // every media element the template renders.
  return (
    <div ref={hostRef} data-testid="manifest-router-host" style={{ display: 'contents' }}>
      <Renderer
        data={persisted ? { ...templateData, ...persistProps } : templateData}
        title={title}
        theme={t}
        onInteraction={onInteraction ? forwardInteraction : undefined}
      />
    </div>
  );
}
