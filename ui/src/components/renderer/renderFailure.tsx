/**
 * "If it can't render, it shouldn't show" — the one rule every render tab follows.
 *
 * A tab used to stay in the strip with an error card ("An error occurred while rendering the timeline",
 * "Could not display this render", "Unknown template") whenever its renderer threw or its manifest could not be
 * loaded for good. Each template had its own fallback, so fixing them one at a time never ended. Instead the
 * renderer panel PROVIDES a reporter for the tab it is showing, and every terminal failure path reports to it:
 *
 *  - `TabRenderBoundary` catches anything a template throws while rendering;
 *  - `RenderFailure` replaces a template's own error card (ManifestRouter after its retries, NleTimeline's
 *    boundary, an unknown template) — it reports and renders nothing.
 *
 * The panel then drops the tab from the strip and moves to a neighbour. Outside a panel (no provider) the
 * templates keep their old, visible fallbacks, so standalone uses still explain themselves.
 *
 * Only TERMINAL failures report: ManifestRouter has already retried transient statuses and network errors, and
 * a still-loading tab never reports.
 */
import React, { createContext, useContext, useEffect } from 'react';

export type ReportRenderFailure = (reason: string) => void;

export const RenderFailureContext = createContext<ReportRenderFailure | null>(null);

/** The panel's reporter for the current tab, or `null` when rendered outside a panel. */
export function useRenderFailureReporter(): ReportRenderFailure | null {
  return useContext(RenderFailureContext);
}

/**
 * Render this where a template would show its terminal error card. Inside a panel it reports `reason` and renders
 * nothing (the tab disappears); outside one it renders `fallback`.
 */
export function RenderFailure({ reason, fallback }: { reason: string; fallback?: React.ReactNode }) {
  const report = useRenderFailureReporter();
  useEffect(() => { report?.(reason); }, [report, reason]);
  return report ? null : <>{fallback ?? null}</>;
}

interface BoundaryProps { onError: (error: Error) => void; children: React.ReactNode }
interface BoundaryState { failed: boolean }

/** Catches a render-time throw anywhere in one tab's content and reports it; renders nothing once failed. */
export class TabRenderBoundary extends React.Component<BoundaryProps, BoundaryState> {
  state: BoundaryState = { failed: false };

  static getDerivedStateFromError(): BoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: Error): void {
    this.props.onError(error);
  }

  render(): React.ReactNode {
    return this.state.failed ? null : this.props.children;
  }
}
