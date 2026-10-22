import { theme } from '../styles/tokens';
import type { JobStatus } from '../types/jobs';

/**
 * Shared status/label/formatting helpers for job-status UI surfaces
 * (ActivityPanel, PlanDagView, JobsPanel). Pulled out of ActivityPanel
 * so the DAG visualization can render nodes with exactly the same
 * status semantics (color, label, elapsed-time formatting) as the
 * flat job list underneath it — a mismatch there would defeat the
 * point of tying the two views together.
 */

export function isActive(status: JobStatus): boolean {
  return status === 'PENDING' || status === 'RUNNING';
}

export function isTerminalSuccess(status: JobStatus): boolean {
  return status === 'SUCCEEDED';
}

export function isTerminalFailure(status: JobStatus): boolean {
  return status === 'FAILED' || status === 'TIMED_OUT' || status === 'ABORTED';
}

export function statusColor(status: JobStatus): string {
  switch (status) {
    case 'PENDING': return theme.colors.warning;
    case 'RUNNING': return theme.colors.primary;
    case 'SUCCEEDED': return theme.colors.success;
    case 'FAILED': return theme.colors.error;
    case 'TIMED_OUT': return theme.colors.error;
    case 'ABORTED': return theme.colors.textSecondary;
    default: return theme.colors.textSecondary;
  }
}

export function statusLabel(status: JobStatus): string {
  switch (status) {
    case 'PENDING': return 'Queued';
    case 'RUNNING': return 'Running';
    case 'SUCCEEDED': return 'Completed';
    case 'FAILED': return 'Failed';
    case 'TIMED_OUT': return 'Timed Out';
    case 'ABORTED': return 'Aborted';
    default: return status;
  }
}

export function formatElapsed(startAt: string, updatedAt: string, status: JobStatus): string {
  const start = Date.parse(startAt);
  const end = isActive(status) ? Date.now() : Date.parse(updatedAt);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return '';
  const diffMs = Math.max(0, end - start);
  if (diffMs < 1000) return '<1s';
  if (diffMs < 60_000) return `${Math.round(diffMs / 1000)}s`;
  if (diffMs < 3600_000) return `${Math.floor(diffMs / 60_000)}m ${Math.round((diffMs % 60_000) / 1000)}s`;
  return `${Math.floor(diffMs / 3600_000)}h ${Math.floor((diffMs % 3600_000) / 60_000)}m`;
}

/**
 * Humanize a job's `trioId` (e.g. "node|t1_separate_stems" ->
 * "Separate Stems", "plan|display-name" -> "Plan: display-name").
 */
export function extractToolName(trioId: string): string {
  const parts = trioId.split('|');
  const raw = parts[parts.length - 1] || trioId;

  if (parts[0] === 'plan') return `Plan: ${raw}`;

  if (parts[0] === 'node') {
    return raw
      .replace(/^t\d+_?/, '') // strip "t1_" prefix
      .replace(/[-_]/g, ' ')
      .replace(/\b\w/g, c => c.toUpperCase()) || raw;
  }

  return raw
    .replace(/[-_]/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase());
}
