/**
 * The workspace view's Graph tab: fetches the whole-workspace graph and renders one of
 *
 *   loading   → "Loading the graph…"
 *   off       → owner: "Enable knowledge graph" (one way, confirm); member: an explanation
 *   seeding   → "Building the starter graph…", polling stats every 3 s (up to 60 s), then reloads
 *   error     → the message + Retry (503 graph_service_unavailable lands here)
 *   ready     → the lazily loaded Sigma canvas (+ a banner when the server sampled a huge graph)
 *
 * The canvas is `React.lazy`, so sigma/graphology download only when someone opens this tab.
 */
import React, { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';
import { theme } from '../../../styles/tokens';
import Button from '../../ui/Button';
import { enableProjectGraph, getProjectGraph, getProjectGraphStats } from '../../../projects/api';
import type { GraphOverview, ProjectRole } from '../../../projects/api';

const GraphCanvas = lazy(() => import('./GraphCanvas'));

export interface GraphTabProps {
  project: { id: string; name: string; personal: boolean };
  role: ProjectRole;
  onNotFound(): void;
  /** Nodes to highlight and zoom to (from a chat graph result's "Open in project graph"). */
  focusIds?: string[];
}

type View =
  | { kind: 'loading' }
  | { kind: 'off' }
  | { kind: 'seeding' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; overview: GraphOverview };

const POLL_MS = 3000;
const POLL_LIMIT_MS = 60_000;

export default function GraphTab({ project, role, onNotFound, focusIds }: GraphTabProps) {
  const [view, setView] = useState<View>({ kind: 'loading' });
  const [confirming, setConfirming] = useState(false);
  const [enabling, setEnabling] = useState(false);
  const pollStarted = useRef<number | null>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const load = useCallback(async () => {
    const r = await getProjectGraph(project.id);
    if (!alive.current) return;
    if (r.ok) {
      const s = r.value.state;
      setView(s === 'ready' ? { kind: 'ready', overview: r.value } : s === 'off' ? { kind: 'off' } : { kind: 'seeding' });
      return;
    }
    if (r.kind === 'not_found') { onNotFound(); return; }
    setView({ kind: 'error', message: r.kind === 'error' && r.status === 503
      ? 'The knowledge graph is unavailable right now.' : 'Could not load the knowledge graph.' });
  }, [project.id, onNotFound]);

  useEffect(() => {
    setView({ kind: 'loading' });
    pollStarted.current = null;
    void load();
  }, [load]);

  // While seeding, poll the (cheap) stats route and reload the overview once the first build lands.
  useEffect(() => {
    if (view.kind !== 'seeding') return undefined;
    pollStarted.current ??= Date.now();
    const t = window.setTimeout(async () => {
      if (Date.now() - (pollStarted.current ?? 0) > POLL_LIMIT_MS) {
        setView({ kind: 'error', message: 'The starter graph is taking longer than expected.' });
        return;
      }
      const s = await getProjectGraphStats(project.id);
      if (!alive.current) return;
      if (s.ok && s.value.state === 'ready') void load();
      else setView({ kind: 'seeding' });
    }, POLL_MS);
    return () => window.clearTimeout(t);
  }, [view, project.id, load]);

  const enable = async () => {
    setEnabling(true);
    const r = await enableProjectGraph(project.id);
    setEnabling(false);
    setConfirming(false);
    if (r.ok) { pollStarted.current = null; setView({ kind: 'seeding' }); return; }
    if (r.kind === 'not_found') { onNotFound(); return; }
    setView({ kind: 'error', message: r.kind === 'error' ? r.message : 'Could not enable the knowledge graph.' });
  };

  if (view.kind === 'loading') return <p style={msgStyle} data-testid="graph-loading">Loading the graph…</p>;

  if (view.kind === 'off') {
    return (
      <div style={centerStyle} data-testid="graph-off">
        <h3 style={headingStyle}>No knowledge graph yet</h3>
        {role === 'owner' ? (
          <>
            <p style={msgStyle}>
              Build a knowledge graph of this project — its files, people, places, and topics — starting with a
              sample graph to explore. It cannot be turned off later.
            </p>
            {confirming ? (
              <div style={rowStyle}>
                <Button variant="primary" size="sm" onClick={() => void enable()} busy={enabling} busyLabel="Enabling…">
                  Turn on knowledge graph
                </Button>
                <Button variant="secondary" size="sm" onClick={() => setConfirming(false)} disabled={enabling}>Cancel</Button>
              </div>
            ) : (
              <Button variant="primary" size="sm" onClick={() => setConfirming(true)}>Enable knowledge graph</Button>
            )}
          </>
        ) : (
          <p style={msgStyle}>Only the project owner can turn on the knowledge graph.</p>
        )}
      </div>
    );
  }

  if (view.kind === 'seeding') {
    return <p style={msgStyle} data-testid="graph-seeding" role="status">Building the starter graph…</p>;
  }

  if (view.kind === 'error') {
    return (
      <div style={centerStyle} data-testid="graph-error">
        <p style={errorStyle} role="alert">{view.message}</p>
        <Button variant="secondary" size="sm" onClick={() => { setView({ kind: 'loading' }); void load(); }}>Retry</Button>
      </div>
    );
  }

  const ov = view.overview;
  return (
    <div style={readyStyle} data-testid="graph-ready">
      <div style={summaryStyle}>
        {(ov.totals?.nodes ?? 0).toLocaleString()} nodes · {(ov.totals?.edges ?? 0).toLocaleString()} relationships
        {ov.truncated && ' · showing the most connected part of a large graph'}
      </div>
      <Suspense fallback={<p style={msgStyle}>Loading the graph viewer…</p>}>
        <GraphCanvas projectId={project.id} overview={ov} focusIds={focusIds} dimOthers />
      </Suspense>
    </div>
  );
}

const readyStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, height: '100%' };
const summaryStyle: React.CSSProperties = {
  padding: '6px 24px', fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary,
  borderBottom: `1px solid ${theme.colors.border}`,
};
const centerStyle: React.CSSProperties = { padding: 24, display: 'flex', flexDirection: 'column', gap: 10, maxWidth: 560 };
const rowStyle: React.CSSProperties = { display: 'flex', gap: 8 };
const headingStyle: React.CSSProperties = { margin: 0, fontSize: 'var(--fs-md)', color: theme.colors.textPrimary };
const msgStyle: React.CSSProperties = { margin: 0, padding: '0 24px', color: theme.colors.textSecondary, fontSize: 'var(--fs-sm)' };
const errorStyle: React.CSSProperties = { margin: 0, color: theme.colors.error, fontSize: 'var(--fs-sm)' };
