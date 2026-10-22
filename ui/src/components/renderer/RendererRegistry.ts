import React from 'react';
import type { RendererProps } from './types';
import AudioWaveform from './templates/AudioWaveform';
import ImageAnnotated from './templates/ImageAnnotated';
import ChartInteractive from './templates/ChartInteractive';
import CodeArtifact from './templates/CodeArtifact';
import DataTable from './templates/DataTable';
import MarkdownRich from './templates/MarkdownRich';
import NleTimeline from './templates/NleTimeline';
import DocumentViewer from './templates/DocumentViewer';
import ManifestRouter from './templates/ManifestRouter';
import GenericPreview from './templates/GenericPreview';
import ClipGallery from './templates/ClipGallery';
import AssetPlayer from './templates/AssetPlayer';
import GraphView from './templates/GraphView';
import ProjectBrowser from './templates/ProjectBrowser';

type RendererComponent = React.ComponentType<RendererProps<any>>;

const registry = new Map<string, RendererComponent>();

// `video-markers` is an ALIAS for the timeline container, not its own view. VideoMarkers
// rendered a <video> plus a clickable LIST of shots — a strict subset of the timeline's data
// (`src` + `markers`) with no playhead, no zoom and no shared gutter. Worse, the SAME analysis
// looked different depending on whether a sibling manifest happened to render: alone it was the
// list, composited it was a timeline (staging 9e977f30 is exactly that complaint). One container
// for one axis removes the divergence at the root. The NAME stays registered so stored manifests
// keep resolving (templateContract.test.ts pins that).
registry.set('video-markers', NleTimeline);
registry.set('audio-waveform', AudioWaveform);
registry.set('image-annotated', ImageAnnotated);
registry.set('chart-interactive', ChartInteractive);
registry.set('code-artifact', CodeArtifact);
registry.set('data-table', DataTable);
registry.set('markdown-rich', MarkdownRich);
registry.set('nle-timeline', NleTimeline);
registry.set('composite-timeline', NleTimeline); // v2 alias — same container, data-driven panels
registry.set('document-viewer', DocumentViewer);
// Render Agent manifests arrive as a key only, so the block declares the router
// and the real template is resolved from the fetched manifest.
registry.set('manifest-router', ManifestRouter);
// `video-annotations` is an ALIAS for the timeline container too. NleTimeline already draws the
// boxes on the frame (activeBoxesAt + per-box label + a Faces toggle) AND puts each appearance on
// the shared time gutter, which is what a face result needs: WHERE in the picture and WHEN. The
// dedicated component additionally offered per-person colours, face thumbnails and per-person
// show/hide; those were dropped deliberately — see the fold note in NleTimeline. One container
// per axis means an analysis can never look different depending on how it was reached.
registry.set('video-annotations', NleTimeline);
registry.set('generic-preview', GenericPreview);
// Independent-clip gallery: N separate audio/video takes, each with its own mark-in/out
// editor, plus a batch "trim/clip all marked" action. See ClipGallery.
registry.set('clip-gallery', ClipGallery);
// Single-asset citation/Moment player: a verified Citation or a Moment's Expand opens one of these
// (via WorkspaceView's lab:open-asset). Its block data is an Asset_Reference and no URL; the player
// mints a presigned URL on mount. See AssetPlayer (Component 5).
registry.set('asset-player', AssetPlayer);
registry.set('graph-view', GraphView);
// The agent's browse_project tab: the Active_Project's file grid, browse-only.
registry.set('project-browser', ProjectBrowser);

export function lookupRenderer(templateName: string): RendererComponent | null {
  return registry.get(templateName) || null;
}

export function getRegisteredTemplates(): string[] {
  return Array.from(registry.keys());
}
