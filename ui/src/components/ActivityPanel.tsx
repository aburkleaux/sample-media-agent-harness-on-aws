import React, { useState, useMemo, useEffect, useRef } from 'react';
import { theme, tint } from '../styles/tokens';
import { useJobs } from '../hooks/useJobs';
import type { Job, JobStatus } from '../types/jobs';
import PlanDagView, { computeDagLayout } from './PlanDagView';
import { isActive, isTerminalSuccess, isTerminalFailure, statusColor, statusLabel, formatElapsed, extractToolName } from '../utils/jobStatus';
import ConfirmDialog from './ConfirmDialog';
import TrashIcon from './icons/TrashIcon';
import type { JobDeleteError } from '../hooks/useJobs';
import WarningIcon from './icons/WarningIcon';

/**
 * ActivityPanel — Live activity monitor for async workflow jobs.
 *
 * Shows async jobs spawned from chat sessions, grouped by session and
 * sorted by recency. Provides real-time status updates via the shared
 * useJobs store (fed by SSE /jobs/stream).
 *
 * Phase 1: Read-only observability surface. Jobs are grouped by session,
 * displayed with status indicators, elapsed time, and expandable details.
 * No backend changes required — reads the existing job registry data.
 */

// --- Types ---

type FilterMode = 'all' | 'active' | 'completed' | 'failed';

interface SessionGroup {
  /**
   * Stable, unique identity for this group — the Map `groupKey`
   * (`run:<runId>` or `session:<sessionId>`). This, NOT `sessionId`, is the
   * React key and the expand-set key: a run group copies its first job's
   * `sessionId`, so a plan run that executed in session X collides with the
   * standalone `Session X` group on `sessionId` alone, and expanding one
   * would expand both (and React would warn on the duplicate key). The
   * groupKey is unique by construction (it IS the grouping Map's key).
   */
  id: string;
  sessionId: string;
  runId: string | null;
  label: string;
  jobs: Job[];
  startTime: string;
  latestUpdate: string;
  activeCount: number;
  completedCount: number;
  failedCount: number;
}

// --- Helpers ---
// isActive / isTerminalSuccess / isTerminalFailure / statusColor /
// statusLabel / formatElapsed / extractToolName now live in
// `utils/jobStatus.ts` (shared with PlanDagView so the graph and the
// flat list agree on status semantics).

function formatTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  if (diffMs < 60_000) return 'just now';
  if (diffMs < 3600_000) return `${Math.floor(diffMs / 60_000)}m ago`;
  if (diffMs < 86400_000) return `${Math.floor(diffMs / 3600_000)}h ago`;
  return d.toLocaleDateString();
}

/**
 * Within a plan run, each async DAG node produces up to TWO Job
 * Registry rows sharing the same `runId`:
 *
 *   1. The canonical `node|<id>` row — written by `plan_and_execute_
 *      tool.py` / `task_agent.py`, carries `nodeId`/`dependsOn`, and is
 *      the row the Plan Chainer's state machine actually drives
 *      (pending -> running -> processing -> results_ready -> completed).
 *   2. A raw underlying-gateway-job row (e.g. the real SFN execution,
 *      trioId like "audio-stem-separation|stem_separation") — registered
 *      independently by `JobTrackingMCPAgentTool` (or the `plan-node|`
 *      fallback in `task_agent.py`) the moment the node's tool call
 *      actually fires.
 *
 * Both represent the same unit of work, but their statuses/timestamps
 * disagree (the raw job finishes before the chainer marks the node
 * `completed`), so showing both doubles every async step and shows two
 * different "Completed Xm ago" times for what the user experiences as
 * one action. When a run has any `node|` rows, they are the single
 * source of truth for that run's DAG — drop the raw duplicates. Runs
 * with no `node|` rows at all (non-plan jobs, or older records
 * pre-dating this registration) are returned unchanged.
 */
export function dedupeRunNodeJobs(jobs: Job[]): Job[] {
  const hasNodeEntries = jobs.some(j => j.trioId.startsWith('node|'));
  if (!hasNodeEntries) return jobs;
  return jobs.filter(j => j.trioId.startsWith('node|'));
}

/**
 * Apply {@link dedupeRunNodeJobs} per-run across the whole job list, so
 * both the filter-tab counts and the grouped view agree on how many
 * "real" jobs exist (a raw duplicate shouldn't count toward "Active"
 * or "Completed" any more than it should get its own row). Jobs with
 * no `runId` (standalone, non-plan jobs) pass through untouched.
 */
export function dedupeJobsForDisplay(jobs: Job[]): Job[] {
  const byRun = new Map<string, Job[]>();
  const standalone: Job[] = [];
  for (const job of jobs) {
    if (!job.runId) {
      standalone.push(job);
      continue;
    }
    mapKeyPush(byRun, job.runId, job);
  }
  const deduped: Job[] = [...standalone];
  for (const runJobs of byRun.values()) {
    // Keep the plan-parent "plan|" row as-is (it's not a node dupe
    // candidate) plus the deduped node/raw-job set.
    const planRows = runJobs.filter(j => j.trioId.startsWith('plan|'));
    const nodeCandidates = runJobs.filter(j => !j.trioId.startsWith('plan|'));
    deduped.push(...planRows, ...dedupeRunNodeJobs(nodeCandidates));
  }
  return deduped;
}

function mapKeyPush<K, V>(map: Map<K, V[]>, key: K, val: V) {
  const arr = map.get(key) || [];
  arr.push(val);
  map.set(key, arr);
}

export function groupJobsBySession(jobs: Job[]): SessionGroup[] {
  const map = new Map<string, Job[]>();
  // Collect plan parent entries to extract display names and start times for run groups
  const planDisplayNames = new Map<string, string>();
  const planStartTimes = new Map<string, string>();
  for (const job of jobs) {
    if (job.trioId.startsWith('plan|')) {
      // Extract display_name from trioId "plan|display_name"
      const name = job.trioId.split('|')[1] || '';
      if (job.runId) {
        planDisplayNames.set(job.runId, name);
        if (job.startAt) planStartTimes.set(job.runId, job.startAt);
      }
      continue; // Skip parent "plan" entries — they're the run group header
    }

    // Group by runId first (plan execution group), then by session
    const groupKey = job.runId
      ? `run:${job.runId}`
      : `session:${job.sessionId || '__no_session__'}`;
    mapKeyPush(map, groupKey, job);
  }

  const groups: SessionGroup[] = [];
  for (const [groupKey, groupJobs] of map.entries()) {
    const isRunGroup = groupKey.startsWith('run:');
    // Sort jobs within group by startAt / updatedAt asc so steps flow chronologically
    groupJobs.sort((a, b) => {
      const timeA = a.startAt || a.updatedAt;
      const timeB = b.startAt || b.updatedAt;
      return timeA.localeCompare(timeB);
    });

    const activeCount = groupJobs.filter(j => isActive(j.status)).length;
    const completedCount = groupJobs.filter(j => isTerminalSuccess(j.status)).length;
    const failedCount = groupJobs.filter(j => isTerminalFailure(j.status)).length;

    const runId = isRunGroup ? groupKey.slice(4) : null;
    const sessionId = isRunGroup
      ? (groupJobs[0]?.sessionId || '')
      : groupKey.slice(8); // strip 'session:'

    // Use the plan's display name if available, otherwise fall back to short ID
    const runLabel = runId
      ? (planDisplayNames.get(runId) || `Run ${runId.slice(0, 8)}`)
      : '';

    // Execution start time for the group (earliest startAt)
    let startTime = runId ? (planStartTimes.get(runId) || '') : '';
    for (const j of groupJobs) {
      const t = j.startAt || j.updatedAt;
      if (t && (!startTime || t < startTime)) {
        startTime = t;
      }
    }
    if (!startTime && groupJobs[0]?.updatedAt) {
      startTime = groupJobs[0].updatedAt;
    }

    let latestUpdate = groupJobs[0]?.updatedAt || '';
    for (const j of groupJobs) {
      if (j.updatedAt > latestUpdate) {
        latestUpdate = j.updatedAt;
      }
    }

    groups.push({
      id: groupKey,
      sessionId,
      runId,
      label: isRunGroup
        ? runLabel
        : sessionId === '__no_session__'
          ? 'Unlinked Jobs'
          : `Session ${sessionId.slice(0, 8)}`,
      jobs: groupJobs,
      startTime,
      latestUpdate,
      activeCount,
      completedCount,
      failedCount,
    });
  }

  // Sort groups strictly by execution start time (newest execution first)
  groups.sort((a, b) => {
    const timeA = a.startTime || a.latestUpdate;
    const timeB = b.startTime || b.latestUpdate;
    if (timeA !== timeB) {
      return timeB.localeCompare(timeA);
    }
    return b.latestUpdate.localeCompare(a.latestUpdate);
  });

  return groups;
}

// --- Main Component ---

/**
 * Pending delete confirmation — either a whole run (group header's trash
 * icon) or a single job (a row's trash icon, useful for cleaning up one
 * orphaned/stuck job without nuking the rest of its run). `forceHint` is
 * set after a first delete attempt comes back "job_active"/"run_active"
 * (409) so the dialog can offer a force-delete retry instead of just
 * failing silently.
 */
type PendingDelete =
  | { kind: 'run'; runId: string; label: string; forceHint?: boolean }
  | { kind: 'job'; jobId: string; label: string; forceHint?: boolean }
  | { kind: 'bulk-failed'; groups: SessionGroup[] };

export default function ActivityPanel({ revealRunId, onRevealed }: { revealRunId?: string; onRevealed?: () => void } = {}) {
  // The Workflow page is a history surface, not the compact sidebar: show every
  // job the store holds (the proxy's newest ~100, no time filter) rather than the
  // 24h recency window, so a completed flow doesn't disappear a day after it
  // finished. Active jobs are always visible regardless; this only stops terminal
  // ones aging out of view here.
  const { jobs: rawJobs, connectionState, deleteJob, deleteRun } = useJobs(Infinity);
  const [pendingDelete, setPendingDelete] = useState<PendingDelete | null>(null);
  const [deleteInFlight, setDeleteInFlight] = useState(false);
  const [deleteBanner, setDeleteBanner] = useState<string | null>(null);

  // Collapse per-run node/raw-gateway-job duplicates once, up front, so
  // filter counts and the grouped view never disagree (see
  // dedupeJobsForDisplay / dedupeRunNodeJobs for why the duplication
  // exists).
  const jobs = useMemo(() => dedupeJobsForDisplay(rawJobs), [rawJobs]);

  const [filter, setFilter] = useState<FilterMode>('all');
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  // Scroll container for reveal (expand + scrollIntoView the opened run's group).
  const containerRef = useRef<HTMLDivElement>(null);

  // Filter jobs (by status only). We deliberately do NOT narrow to a single run
  // when one is opened — every flow stays on screen; opening a run reveals it
  // (expand + scroll) rather than hiding the rest. This also keeps the filter-tab
  // counts and the visible list in agreement.
  const filteredJobs = useMemo(() => {
    switch (filter) {
      case 'active': return jobs.filter(j => isActive(j.status));
      case 'completed': return jobs.filter(j => isTerminalSuccess(j.status));
      case 'failed': return jobs.filter(j => isTerminalFailure(j.status));
      default: return jobs;
    }
  }, [jobs, filter]);

  // Group by session
  const groups = useMemo(() => groupJobsBySession(filteredJobs), [filteredJobs]);

  // Reveal (not narrow): when a run is opened from the Jobs panel or the Workflow
  // sidebar, keep EVERY flow on screen and just expand that run's group and scroll
  // it into view. Narrowing to one run hid all the others, desynced the filter
  // counts, and left no clear way back. Reset the reveal target afterward so
  // re-opening the same run re-triggers the scroll.
  useEffect(() => {
    if (!revealRunId) return;
    const job = jobs.find(j => j.jobId === revealRunId) || jobs.find(j => j.runId === revealRunId);
    const gid = job ? (job.runId ? `run:${job.runId}` : `session:${job.sessionId || '__no_session__'}`) : null;
    if (gid) setExpandedGroups(prev => (prev.has(gid) ? prev : new Set(prev).add(gid)));
    const t = setTimeout(() => {
      if (gid && containerRef.current) {
        const el = containerRef.current.querySelector<HTMLElement>(`[data-group-id="${cssEscapeSafe(gid)}"]`);
        el?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
      }
      onRevealed?.();
    }, 0);
    return () => clearTimeout(t);
  }, [revealRunId, jobs, onRevealed]);

  // Counts for filter tabs
  const counts = useMemo(() => ({
    all: jobs.length,
    active: jobs.filter(j => isActive(j.status)).length,
    completed: jobs.filter(j => isTerminalSuccess(j.status)).length,
    failed: jobs.filter(j => isTerminalFailure(j.status)).length,
  }), [jobs]);

  // Groups eligible for the "Clean up failed" bulk action: a run/session
  // group with zero active jobs and at least one failed job. Deliberately
  // excludes groups that are still partially running (a run with one
  // failed node and three still in flight isn't "done failing" yet — the
  // per-row/per-run trash icon covers that case with an explicit
  // force-delete confirmation instead).
  const cleanableFailedGroups = useMemo(
    () => groupJobsBySession(dedupeJobsForDisplay(rawJobs)).filter(
      g => g.activeCount === 0 && g.failedCount > 0,
    ),
    [rawJobs],
  );

  const toggleGroup = (groupId: string) => {
    setExpandedGroups(prev => {
      const next = new Set(prev);
      if (next.has(groupId)) next.delete(groupId);
      else next.add(groupId);
      return next;
    });
  };

  const handleConfirmDelete = async () => {
    if (!pendingDelete) return;
    setDeleteInFlight(true);
    setDeleteBanner(null);
    try {
      if (pendingDelete.kind === 'run') {
        await deleteRun(pendingDelete.runId, { force: pendingDelete.forceHint });
      } else if (pendingDelete.kind === 'job') {
        await deleteJob(pendingDelete.jobId, { force: pendingDelete.forceHint });
      } else {
        // Bulk cleanup: delete every failed run (via its runId) and every
        // failed standalone job (no runId) in this filtered set. Best-
        // effort — one failure shouldn't block the rest, so errors are
        // collected and summarized rather than thrown from the loop.
        const errors: string[] = [];
        for (const group of pendingDelete.groups) {
          try {
            if (group.runId) {
              await deleteRun(group.runId);
            } else {
              await Promise.all(group.jobs.map(j => deleteJob(j.jobId)));
            }
          } catch (err) {
            errors.push(err instanceof Error ? err.message : String(err));
          }
        }
        if (errors.length > 0) {
          setDeleteBanner(`${errors.length} item(s) could not be deleted: ${errors[0]}`);
        }
      }
      setPendingDelete(null);
    } catch (err) {
      const code = (err as JobDeleteError)?.code;
      if (code === 'job_active' || code === 'run_active') {
        // Offer a force-delete retry instead of a dead-end error — the
        // user explicitly asked to clean this up, so re-prompt with the
        // force flag pre-armed rather than making them find a second
        // control somewhere else.
        setPendingDelete({ ...pendingDelete, forceHint: true } as PendingDelete);
        setDeleteBanner(
          (err as JobDeleteError).message ||
          'This is still active. Confirm again to force-delete it.'
        );
      } else {
        setPendingDelete(null);
        setDeleteBanner(err instanceof Error ? err.message : 'Delete failed.');
      }
    } finally {
      setDeleteInFlight(false);
    }
  };

  return (
    <div ref={containerRef} style={containerStyle}>
      <style>{`
        @keyframes activity-dot-pulse { 50% { opacity: 0.35; } }
      `}</style>
      {/* Header */}
      <div style={headerStyle}>
        <div>
          <h2 style={titleStyle}>Workflow</h2>
          <p style={subtitleStyle}>
            Track workflows and async jobs spawned from chat.
          </p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <ConnectionIndicator state={connectionState} />
        </div>
      </div>

      {/* Filter bar */}
      <div style={filterBarStyle}>
        <FilterTab label="All" count={counts.all} active={filter === 'all'} onClick={() => setFilter('all')} />
        <FilterTab label="Active" count={counts.active} active={filter === 'active'} onClick={() => setFilter('active')} />
        <FilterTab label="Completed" count={counts.completed} active={filter === 'completed'} onClick={() => setFilter('completed')} />
        <FilterTab label="Failed" count={counts.failed} active={filter === 'failed'} onClick={() => setFilter('failed')} />
        {cleanableFailedGroups.length > 0 && (
          <button
            onClick={() => setPendingDelete({ kind: 'bulk-failed', groups: cleanableFailedGroups })}
            style={cleanupButtonStyle}
            title="Delete every failed run/job that isn't still active"
          >
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}><TrashIcon /> Clean up failed ({cleanableFailedGroups.length})</span>
          </button>
        )}
      </div>

      {/* Delete-error banner (e.g. a job/run is still active — shown while
          the confirm dialog re-prompts with force armed, and also after
          the dialog closes on a non-recoverable failure). */}
      {deleteBanner && (
        <div style={deleteBannerStyle}>
          <span>{deleteBanner}</span>
          <button onClick={() => setDeleteBanner(null)} style={deleteBannerCloseStyle} aria-label="Dismiss">✕</button>
        </div>
      )}

      {/* Content */}
      <div style={contentStyle}>
        {filteredJobs.length === 0 ? (
          <EmptyState filter={filter} />
        ) : (
          groups.map(group => (
            <SessionGroupCard
              key={group.id}
              group={group}
              expanded={expandedGroups.has(group.id)}
              onToggle={() => toggleGroup(group.id)}
              onRequestDeleteRun={group.runId ? () => setPendingDelete({ kind: 'run', runId: group.runId!, label: group.label }) : undefined}
              onRequestDeleteJob={(job) => setPendingDelete({ kind: 'job', jobId: job.jobId, label: extractToolName(job.trioId) })}
            />
          ))
        )}
      </div>

      <ConfirmDialog
        open={pendingDelete !== null}
        title={
          pendingDelete?.kind === 'run' ? 'Delete run'
            : pendingDelete?.kind === 'bulk-failed' ? 'Clean up failed'
            : 'Delete job'
        }
        message={
          pendingDelete
            ? pendingDelete.kind === 'bulk-failed'
              ? `Delete ${pendingDelete.groups.length} failed run(s)/job(s)? This cannot be undone.`
              : pendingDelete.forceHint
                ? `"${pendingDelete.label}" is still active. Deleting only removes it from this view — it does not stop the underlying job. Delete anyway?`
                : pendingDelete.kind === 'run'
                  ? `Delete the run "${pendingDelete.label}" and all of its jobs? This cannot be undone.`
                  : `Delete "${pendingDelete.label}"? This cannot be undone.`
            : ''
        }
        confirmLabel={pendingDelete?.kind !== 'bulk-failed' && pendingDelete?.forceHint ? 'Force Delete' : 'Delete'}
        onConfirm={handleConfirmDelete}
        onCancel={() => { setPendingDelete(null); setDeleteBanner(null); }}
        loading={deleteInFlight}
      />
    </div>
  );
}

// --- Sub-components ---

function ConnectionIndicator({ state }: { state: string }) {
  const config: Record<string, { color: string; label: string }> = {
    open: { color: theme.colors.success, label: 'Live' },
    connecting: { color: theme.colors.warning, label: 'Connecting' },
    error: { color: theme.colors.error, label: 'Disconnected' },
    closed: { color: theme.colors.textSecondary, label: 'Closed' },
    idle: { color: theme.colors.textSecondary, label: 'Idle' },
  };
  const c = config[state] || config.idle;
  // A healthy live stream needs no chrome — the "Live"/"Idle" badge is just
  // noise. Only surface the indicator when something is actually wrong
  // (connecting / disconnected / closed) so the header stays clean.
  if (state === 'open' || state === 'idle') return null;
  return (
    <div style={connectionStyle}>
      <span style={{ ...connectionDotStyle, background: c.color }} />
      <span style={connectionLabelStyle}>{c.label}</span>
    </div>
  );
}

function FilterTab({ label, count, active, onClick }: {
  label: string; count: number; active: boolean; onClick: () => void;
}) {
  return (
    <button onClick={onClick} style={active ? filterTabActiveStyle : filterTabStyle}>
      {label} <span className="numeric" style={{ marginLeft: 4, opacity: 0.6 }}>{count}</span>
    </button>
  );
}

function EmptyState({ filter }: { filter: FilterMode }) {
  const messages: Record<FilterMode, string> = {
    all: 'No jobs yet. Start a conversation and kick off some async tasks!',
    active: 'No active jobs running.',
    completed: 'No completed jobs in the last 24 hours.',
    failed: 'No failed jobs — everything is running smoothly.',
  };
  return (
    <div style={emptyStyle}>
      <div style={emptyIconStyle}>📋</div>
      <p style={emptyTextStyle}>{messages[filter]}</p>
    </div>
  );
}

function SessionGroupCard({ group, expanded, onToggle, onRequestDeleteRun, onRequestDeleteJob }: {
  group: SessionGroup;
  expanded: boolean;
  onToggle: () => void;
  onRequestDeleteRun?: () => void;
  onRequestDeleteJob: (job: Job) => void;
}) {
  const hasActive = group.activeCount > 0;
  const isRun = group.runId !== null;
  // Auto-expand groups with active jobs
  const isExpanded = expanded || hasActive;
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);

  // Only render the DAG strip when at least one job in this group carries
  // a nodeId (older records / non-plan jobs won't — see PlanDagView).
  const hasDag = useMemo(() => isRun && !!computeDagLayout(group.jobs), [isRun, group.jobs]);

  return (
    <div data-group-id={group.id} style={{ ...groupCardStyle, ...(isRun ? runCardAccentStyle : undefined) }}>
      {/* Group header */}
      <button onClick={onToggle} style={groupHeaderStyle} aria-expanded={isExpanded}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1 }}>
          <span style={chevronStyle}>{isExpanded ? '▾' : '▸'}</span>
          {isRun && <span style={runIconStyle}>⚡</span>}
          <span style={groupLabelStyle}>{group.label}</span>
          {isRun && (
            <span
              role="button"
              title={`Copy plan ID: ${group.runId}`}
              onClick={(e) => {
                e.stopPropagation();
                navigator.clipboard.writeText(group.runId!);
              }}
              style={copyIdButtonStyle}
            >
              📋
            </span>
          )}
          <GroupStatusPills group={group} />
        </div>
        {onRequestDeleteRun && (
          <span
            role="button"
            title="Delete this run"
            aria-label="Delete run"
            onClick={(e) => { e.stopPropagation(); onRequestDeleteRun(); }}
            style={deleteRunButtonStyle}
          >
            <TrashIcon />
          </span>
        )}
        <span style={groupTimeStyle}>{formatTime(group.latestUpdate)}</span>
      </button>

      {/* DAG visualization — shows the plan's actual execution graph
          (dependency edges) instead of leaving the run's job list
          disconnected from the sequence of events. Clicking a node
          selects + expands the matching row below. */}
      {isExpanded && hasDag && (
        <PlanDagView
          jobs={group.jobs}
          selectedJobId={selectedJobId}
          onSelectNode={(jobId) => setSelectedJobId(prev => prev === jobId ? null : jobId)}
        />
      )}

      {/* Job rows */}
      {isExpanded && (
        <div style={groupBodyStyle}>
          {group.jobs.map(job => (
            <JobRow
              key={job.jobId}
              job={job}
              selected={selectedJobId === job.jobId}
              onSelect={() => setSelectedJobId(prev => prev === job.jobId ? null : job.jobId)}
              onRequestDelete={() => onRequestDeleteJob(job)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function GroupStatusPills({ group }: { group: SessionGroup }) {
  return (
    <div style={{ display: 'flex', gap: 4 }}>
      {group.activeCount > 0 && (
        <span style={{ ...pillStyle, background: tint(theme.colors.primary, 10), color: theme.colors.primary }}>
          {group.activeCount} running
        </span>
      )}
      {group.completedCount > 0 && (
        <span style={{ ...pillStyle, background: tint(theme.colors.success, 10), color: theme.colors.success }}>
          {group.completedCount} done
        </span>
      )}
      {group.failedCount > 0 && (
        <span style={{ ...pillStyle, background: tint(theme.colors.error, 10), color: theme.colors.error }}>
          {group.failedCount} failed
        </span>
      )}
    </div>
  );
}

function formatDurationMs(startIso: string, endIso: string): string {
  const start = new Date(startIso).getTime();
  const end = new Date(endIso).getTime();
  if (isNaN(start) || isNaN(end) || end < start) return '';
  const diffMs = end - start;
  if (diffMs < 1000) return `${diffMs}ms`;
  const sec = (diffMs / 1000).toFixed(1);
  if (diffMs < 60_000) return `${sec}s`;
  const min = Math.floor(diffMs / 60_000);
  const remSec = Math.floor((diffMs % 60_000) / 1000);
  return `${min}m ${remSec}s`;
}

function getArtifactIcon(url: string): string {
  const lower = url.toLowerCase();
  if (lower.endsWith('.mp4') || lower.endsWith('.mov') || lower.endsWith('.webm') || lower.includes('video')) return '🎬';
  if (lower.endsWith('.mp3') || lower.endsWith('.wav') || lower.endsWith('.m4a') || lower.includes('audio') || lower.includes('stem')) return '🎵';
  if (lower.endsWith('.json') || lower.endsWith('.txt') || lower.endsWith('.csv')) return '📄';
  if (lower.endsWith('.png') || lower.endsWith('.jpg') || lower.endsWith('.jpeg')) return '🖼️';
  return '📦';
}

function getArtifactFilename(url: string): string {
  try {
    const parsed = new URL(url);
    const pathname = parsed.pathname;
    const parts = pathname.split('/');
    const name = parts[parts.length - 1];
    return name || url;
  } catch {
    const parts = url.split('/');
    return parts[parts.length - 1] || url;
  }
}

function JobRow({ job, selected, onSelect, onRequestDelete }: {
  job: Job; selected?: boolean; onSelect?: () => void; onRequestDelete?: () => void;
}) {
  const [showDetails, setShowDetails] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [copiedField, setCopiedField] = useState<string | null>(null);
  const active = isActive(job.status);
  // A click on the matching DAG node (selected=true) expands this row too,
  // so picking a node in the graph and seeing its detail is a single click.
  const isOpen = showDetails || !!selected;

  const handleClick = () => {
    setShowDetails(!isOpen);
    onSelect?.();
  };

  const copyToClipboard = (text: string, fieldKey: string, e?: React.MouseEvent) => {
    e?.stopPropagation();
    navigator.clipboard.writeText(text);
    setCopiedField(fieldKey);
    setTimeout(() => setCopiedField(null), 2000);
  };

  const toolName = extractToolName(job.trioId);
  const durationStr = formatDurationMs(job.startAt, job.terminalAt || job.updatedAt);
  const hasArtifacts = (job.artifactUrls?.length ?? 0) > 0;

  return (
    <div
      style={{ ...jobRowStyle, ...(selected ? jobRowSelectedStyle : undefined) }}
      data-job-id={job.jobId}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <div style={jobRowMainStyle} onClick={handleClick} role="button" tabIndex={0}>
        {/* Status dot */}
        <span style={{
          ...jobDotStyle,
          background: statusColor(job.status),
          ...(active ? pulseDotStyle : undefined),
        }} />

        {/* Info */}
        <div style={jobInfoStyle}>
          <div style={jobNameStyle}>
            {job.nodeId && <span style={nodeIdTagStyle}>[{job.nodeId}]</span>}
            {toolName}
          </div>
          <div style={jobMetaStyle}>
            {statusLabel(job.status)}
            {' · '}
            <span className="numeric">{formatElapsed(job.startAt, job.updatedAt, job.status)}</span>
            {durationStr && ` (${durationStr})`}
            {active && <span style={liveIndicatorStyle}> ●</span>}
            {hasArtifacts && <span style={artifactBadgeStyle}> 📎 {job.artifactUrls!.length} artifact(s)</span>}
          </div>
        </div>

        {/* Delete (visible on hover) */}
        {onRequestDelete && (
          <span
            role="button"
            title="Delete this job"
            aria-label="Delete job"
            onClick={(e) => { e.stopPropagation(); onRequestDelete(); }}
            style={{ ...deleteJobButtonStyle, opacity: hovered ? 1 : 0 }}
          >
            <TrashIcon />
          </span>
        )}

        {/* Chevron */}
        <span style={jobChevronStyle}>{isOpen ? '▾' : '▸'}</span>
      </div>

      {/* Expanded details */}
      {isOpen && (
        <div style={jobDetailsContainerStyle}>
          {/* Quick Action Toolbar */}
          <div style={quickActionBarStyle}>
            <button
              onClick={(e) => copyToClipboard(job.jobId, 'jobId', e)}
              style={actionBtnStyle}
              title="Copy Job ID / ARN"
            >
              {copiedField === 'jobId' ? '✓ Copied ID' : '📋 Copy Job ID'}
            </button>

            {job.trioId && (
              <button
                onClick={(e) => copyToClipboard(job.trioId, 'trioId', e)}
                style={actionBtnStyle}
                title="Copy full tool trio string"
              >
                {copiedField === 'trioId' ? '✓ Copied Trio' : '📋 Copy Trio ID'}
              </button>
            )}

            {job.lastError && (
              <button
                onClick={(e) => copyToClipboard(job.lastError!, 'error', e)}
                style={{ ...actionBtnStyle, color: theme.colors.error, borderColor: tint(theme.colors.error, 30) }}
                title="Copy error message"
              >
                {copiedField === 'error' ? '✓ Copied Error' : <><WarningIcon /> Copy Error</>}
              </button>
            )}
          </div>

          {/* Detailed Node Information Grid */}
          <div style={detailsGridStyle}>
            {job.nodeId && <DetailRow label="Node ID" value={job.nodeId} mono copyable fieldKey="nodeId" onCopy={copyToClipboard} copiedField={copiedField} />}
            <DetailRow label="Job ID" value={job.jobId} mono copyable fieldKey="jobId" onCopy={copyToClipboard} copiedField={copiedField} />
            <DetailRow label="Tool Trio" value={job.trioId} mono copyable fieldKey="trioId" onCopy={copyToClipboard} copiedField={copiedField} />
            <DetailRow label="Gateway" value={job.gatewayId} />
            <DetailRow label="Status" value={`${statusLabel(job.status)} (${job.status})`} status={job.status} />

            {(job.dependsOn?.length ?? 0) > 0 && (
              <div style={detailRowStyle}>
                <span style={detailLabelStyle}>Depends on</span>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                  {job.dependsOn.map(dep => (
                    <span key={dep} style={depChipStyle}>{dep}</span>
                  ))}
                </div>
              </div>
            )}

            <DetailRow label="Started" value={new Date(job.startAt).toLocaleString()} />
            {job.terminalAt && <DetailRow label="Finished" value={new Date(job.terminalAt).toLocaleString()} />}
            {durationStr && <DetailRow label="Duration" value={durationStr} />}
            {job.sessionId && <DetailRow label="Session" value={job.sessionId} mono />}
            {job.turnId && <DetailRow label="Turn ID" value={job.turnId} mono />}
          </div>

          {/* Artifacts & Output URLs section */}
          {hasArtifacts && (
            <div style={artifactsSectionStyle}>
              <div style={artifactsHeaderStyle}>
                <span>Output Artifacts ({job.artifactUrls!.length})</span>
                {job.hasRenderableArtifacts && (
                  <span style={renderableBadgeStyle}>✓ Renderable</span>
                )}
              </div>
              <div style={artifactsListStyle}>
                {job.artifactUrls!.map((url, idx) => {
                  const name = getArtifactFilename(url);
                  const icon = getArtifactIcon(url);
                  const key = `artifact-${idx}`;
                  return (
                    <div key={idx} style={artifactItemStyle}>
                      <span style={{ fontSize: 'var(--fs-sm)' }}>{icon}</span>
                      <span style={artifactNameStyle} title={url}>{name}</span>
                      <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                        <a
                          href={url}
                          target="_blank"
                          rel="noopener noreferrer"
                          style={artifactOpenLinkStyle}
                          onClick={(e) => e.stopPropagation()}
                          title="Open artifact in new tab"
                        >
                          ↗ Open
                        </a>
                        <button
                          onClick={(e) => copyToClipboard(url, key, e)}
                          style={artifactCopyBtnStyle}
                          title="Copy artifact URL"
                        >
                          {copiedField === key ? '✓ Copied' : '📋 Copy URL'}
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Error Details Callout */}
          {job.lastError && (
            <div style={errorCalloutStyle}>
              <div style={errorCalloutHeaderStyle}>
                <span><WarningIcon /> Error Details</span>
                <button
                  onClick={(e) => copyToClipboard(job.lastError!, 'error', e)}
                  style={errorCopyBtnStyle}
                >
                  {copiedField === 'error' ? '✓ Copied' : '📋 Copy Error'}
                </button>
              </div>
              <pre style={errorTextStyle}>{job.lastError}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DetailRow({ label, value, mono, error, status, copyable, fieldKey, onCopy, copiedField }: {
  label: string;
  value: string;
  mono?: boolean;
  error?: boolean;
  status?: JobStatus;
  copyable?: boolean;
  fieldKey?: string;
  onCopy?: (text: string, key: string, e?: React.MouseEvent) => void;
  copiedField?: string | null;
}) {
  return (
    <div style={detailRowStyle}>
      <span style={detailLabelStyle}>{label}</span>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flex: 1, minWidth: 0 }}>
        <span style={{
          ...detailValueStyle,
          ...(mono ? { fontFamily: theme.typography.fontMono } : undefined),
          ...(error ? { color: theme.colors.error } : undefined),
          ...(status ? { color: statusColor(status), fontWeight: theme.typography.weights.medium } : undefined),
        }}>
          {value}
        </span>
        {copyable && fieldKey && onCopy && (
          <button
            onClick={(e) => onCopy(value, fieldKey, e)}
            style={inlineCopyBtnStyle}
            title={`Copy ${label}`}
          >
            {copiedField === fieldKey ? '✓' : '📋'}
          </button>
        )}
      </div>
    </div>
  );
}

// --- Styles ---

const containerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  height: '100%',
  minHeight: 0,
  flex: 1,
  overflow: 'hidden',
};

const headerStyle: React.CSSProperties = {
  padding: '20px 24px 12px',
  display: 'flex',
  alignItems: 'flex-start',
  justifyContent: 'space-between',
  borderBottom: `1px solid ${theme.colors.border}`,
  flexShrink: 0,
};

const titleStyle: React.CSSProperties = {
  margin: 0,
  fontSize: theme.typography.sizes.sectionTitle,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
};

const subtitleStyle: React.CSSProperties = {
  margin: '4px 0 0',
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
};

const connectionStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  padding: '4px 8px',
  borderRadius: theme.radius.button,
  background: theme.colors.background,
  border: `1px solid ${theme.colors.border}`,
};

const connectionDotStyle: React.CSSProperties = {
  width: 6,
  height: 6,
  borderRadius: '50%',
};

const connectionLabelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.textSecondary,
  fontWeight: theme.typography.weights.medium,
};

const filterBarStyle: React.CSSProperties = {
  padding: '14px 24px',
  display: 'flex',
  gap: 4,
  flexShrink: 0,
};

const filterTabStyle: React.CSSProperties = {
  padding: '5px 10px',
  fontSize: 'var(--fs-2xs)',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  background: theme.colors.surface,
  color: theme.colors.textSecondary,
  cursor: 'pointer',
};

const filterTabActiveStyle: React.CSSProperties = {
  ...filterTabStyle,
  background: theme.colors.primary,
  // Same control class as the Agents/Skills state filter; kept in step with it.
  color: theme.colors.onPrimary,
  borderColor: theme.colors.primary,
};

const cleanupButtonStyle: React.CSSProperties = {
  padding: '5px 10px',
  fontSize: 'var(--fs-2xs)',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  background: theme.colors.surface,
  color: theme.colors.error,
  cursor: 'pointer',
  marginLeft: 'auto',
};

// Safe CSS.escape for building an attribute selector to scroll a group into view;
// falls back to a quote-escape where CSS.escape is unavailable (older test envs).
function cssEscapeSafe(v: string): string {
  return (typeof CSS !== 'undefined' && typeof CSS.escape === 'function')
    ? CSS.escape(v)
    : v.replace(/"/g, '\\"');
}

const contentStyle: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  overflowY: 'auto',
  padding: '16px 24px',
};

const emptyStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  padding: '60px 24px',
  textAlign: 'center',
};

const emptyIconStyle: React.CSSProperties = {
  /*
   * A decorative glyph sized as a graphic, so no rung on the type scale fits —
   * but it still has to track Font Size, or the empty state's icon shrinks
   * relative to its own caption at the Large setting. Derived from the base so
   * it scales; the ratio reproduces the previous 32px exactly at the 14px base.
   * The real fix is an SVG with width/height rather than a glyph.
   */
  fontSize: 'calc(var(--font-size-base) * 2.286)',
  marginBottom: 12,
  opacity: 0.6,
};

const emptyTextStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.body,
  color: theme.colors.textSecondary,
  maxWidth: 300,
  lineHeight: 1.5,
};

const groupCardStyle: React.CSSProperties = {
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  overflow: 'hidden',
  background: theme.colors.surface,
  marginBottom: 8,
  boxShadow: theme.elevation.subtle,
};

const groupHeaderStyle: React.CSSProperties = {
  width: '100%',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  padding: '10px 14px',
  border: 'none',
  background: 'transparent',
  cursor: 'pointer',
  textAlign: 'left',
  fontSize: theme.typography.sizes.body,
};

const chevronStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.textSecondary,
  width: 12,
};

const groupLabelStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.body,
  fontWeight: theme.typography.weights.medium,
  color: theme.colors.textPrimary,
};

const groupTimeStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
};

const groupBodyStyle: React.CSSProperties = {
  borderTop: `1px solid ${theme.colors.border}`,
  padding: '4px 0',
};

const pillStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  padding: '1px 6px',
  borderRadius: theme.radius.badge,
  fontWeight: theme.typography.weights.medium,
};

const jobRowStyle: React.CSSProperties = {
  borderBottom: `1px solid ${theme.colors.border}`,
};

const jobRowSelectedStyle: React.CSSProperties = {
  background: theme.colors.activeBg,
};

const jobRowMainStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  padding: '8px 14px',
  cursor: 'pointer',
};

const jobDotStyle: React.CSSProperties = {
  width: 8,
  height: 8,
  borderRadius: '50%',
  flexShrink: 0,
};

const pulseDotStyle: React.CSSProperties = {
  animation: 'activity-dot-pulse 1.5s infinite ease-in-out',
};

const jobInfoStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
};

const jobNameStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.body,
  fontWeight: theme.typography.weights.medium,
  color: theme.colors.textPrimary,
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
};

const jobMetaStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
  marginTop: 1,
};

const liveIndicatorStyle: React.CSSProperties = {
  color: theme.colors.primary,
  fontSize: 'var(--fs-3xs)',
};

const jobChevronStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.textSecondary,
};

const jobDetailsStyle: React.CSSProperties = {
  padding: '4px 14px 10px 32px',
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
  background: theme.colors.background,
};

const detailRowStyle: React.CSSProperties = {
  display: 'flex',
  gap: 8,
  fontSize: theme.typography.sizes.meta,
  lineHeight: 1.4,
};

const detailLabelStyle: React.CSSProperties = {
  color: theme.colors.textSecondary,
  minWidth: 60,
  flexShrink: 0,
};

const detailValueStyle: React.CSSProperties = {
  color: theme.colors.textPrimary,
  wordBreak: 'break-all',
};

const runCardAccentStyle: React.CSSProperties = {
  borderLeft: `3px solid ${theme.colors.primary}`,
};

const runIconStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
};

const copyIdButtonStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  cursor: 'pointer',
  opacity: 0.4,
  transition: 'opacity 0.15s',
  padding: '2px 4px',
  borderRadius: theme.radius.badge,
};

const deleteRunButtonStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  cursor: 'pointer',
  opacity: 0.5,
  transition: 'opacity 0.15s',
  padding: '2px 6px',
  borderRadius: theme.radius.badge,
  marginRight: 4,
};

const deleteJobButtonStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  cursor: 'pointer',
  padding: '2px 6px',
  borderRadius: theme.radius.badge,
  transition: 'opacity 0.15s',
  flexShrink: 0,
};

const deleteBannerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 8,
  padding: '8px 24px',
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.error,
  background: tint(theme.colors.error, 8),
  borderBottom: `1px solid ${theme.colors.border}`,
  flexShrink: 0,
};

const deleteBannerCloseStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  color: theme.colors.error,
  fontSize: 'var(--fs-xs)',
  padding: '0 4px',
  flexShrink: 0,
};

const nodeIdTagStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.primary,
  marginRight: 6,
  fontFamily: theme.typography.fontMono,
};

const artifactBadgeStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.primary,
  marginLeft: 4,
};

const jobDetailsContainerStyle: React.CSSProperties = {
  padding: '10px 14px 12px 32px',
  display: 'flex',
  flexDirection: 'column',
  gap: 10,
  background: theme.colors.background,
  borderTop: `1px solid ${theme.colors.border}`,
};

const quickActionBarStyle: React.CSSProperties = {
  display: 'flex',
  gap: 6,
  flexWrap: 'wrap',
  marginBottom: 2,
};

const actionBtnStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  padding: '3px 8px',
  borderRadius: theme.radius.button,
  background: theme.colors.surfaceSubtle,
  border: `1px solid transparent`,
  color: theme.colors.primary,
  cursor: 'pointer',
  display: 'flex',
  alignItems: 'center',
  gap: 4,
  fontWeight: theme.typography.weights.medium,
};

const detailsGridStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 6,
};

const depChipStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  fontFamily: theme.typography.fontMono,
  padding: '1px 6px',
  borderRadius: theme.radius.badge,
  background: theme.colors.surface,
  border: `1px solid ${theme.colors.border}`,
  color: theme.colors.textPrimary,
};

const artifactsSectionStyle: React.CSSProperties = {
  marginTop: 4,
  padding: '8px 10px',
  borderRadius: theme.radius.button,
  background: theme.colors.surface,
  border: `1px solid ${theme.colors.border}`,
};

const artifactsHeaderStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  fontSize: theme.typography.sizes.meta,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
  marginBottom: 6,
};

const renderableBadgeStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.success,
  background: tint(theme.colors.success, 10),
  padding: '1px 6px',
  borderRadius: theme.radius.badge,
};

const artifactsListStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 6,
};

const artifactItemStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  fontSize: theme.typography.sizes.meta,
  padding: '4px 8px',
  borderRadius: theme.radius.button,
  background: theme.colors.background,
};

const artifactNameStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  fontFamily: theme.typography.fontMono,
};

const artifactOpenLinkStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.primary,
  textDecoration: 'none',
  padding: '2px 6px',
  borderRadius: theme.radius.badge,
  background: tint(theme.colors.primary, 8),
  fontWeight: theme.typography.weights.medium,
};

const artifactCopyBtnStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  border: 'none',
  background: 'transparent',
  cursor: 'pointer',
  color: theme.colors.textSecondary,
  padding: '2px 4px',
};

const errorCalloutStyle: React.CSSProperties = {
  marginTop: 4,
  padding: '8px 10px',
  borderRadius: theme.radius.button,
  background: tint(theme.colors.error, 6),
  border: `1px solid ${tint(theme.colors.error, 20)}`,
  borderLeft: `3px solid ${theme.colors.error}`,
};

const errorCalloutHeaderStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  fontSize: theme.typography.sizes.meta,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.error,
  marginBottom: 4,
};

const errorCopyBtnStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  border: 'none',
  background: 'none',
  cursor: 'pointer',
  color: theme.colors.error,
};

const errorTextStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 'var(--fs-2xs)',
  fontFamily: theme.typography.fontMono,
  color: theme.colors.error,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
};

const inlineCopyBtnStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  border: 'none',
  background: 'none',
  cursor: 'pointer',
  opacity: 0.6,
  padding: '0 2px',
};
