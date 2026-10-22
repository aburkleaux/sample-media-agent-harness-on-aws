/**
 * Members tab of the Projects_Surface (media-harness-projects-access task 13.6; design Component 11,
 * Req 1.9–1.15, 1.20, 10.9).
 *
 * Shows the Owner and Members (by email or username). The Owner gets invite and remove controls; a Member
 * gets `Leave project` behind {@link LeaveProjectDialog}. A Personal_Project shows only its Owner with no
 * invite control.
 *
 * Hidden on the Projects_Surface while `SHOW_PROJECT_MEMBERS` is false; Leave stays reachable from the
 * surface header's overflow menu. Every denial from the registry is the uniform not-found, which triggers {@link onNotFound}.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { theme } from '../../styles/tokens';
import Button from '../ui/Button';
import ConfirmDialog from '../ConfirmDialog';
import {
  getProject,
  inviteMember,
  leaveProject,
  removeMember,
  type ProjectDetail,
  type ProjectMember,
  type ProjectsResult,
} from '../../projects/api';

export interface MembersTabProps {
  project: { id: string; name: string; personal: boolean };
  role: 'owner' | 'member';
  onNotFound(): void;
  /** Re-list after a membership change (invite, remove, leave) so counts and the list pane stay current. */
  onChanged?(): void;
}

export default function MembersTab({ project, role, onNotFound, onChanged }: MembersTabProps) {
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [inviteValue, setInviteValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmLeave, setConfirmLeave] = useState(false);

  const isOwner = role === 'owner';

  const settle = useCallback(
    <T,>(r: ProjectsResult<T>): T | null => {
      if (r.ok) {
        setError(null);
        return r.value;
      }
      if (r.kind === 'not_found') onNotFound();
      else if (r.kind === 'error') setError(r.message);
      return null;
    },
    [onNotFound],
  );

  const load = useCallback(async () => {
    const r = await getProject(project.id);
    const v = settle(r);
    if (v) setDetail(v);
  }, [project.id, settle]);

  useEffect(() => {
    setDetail(null);
    setError(null);
    void load();
  }, [load]);

  const onInvite = useCallback(async () => {
    const value = inviteValue.trim();
    if (!value || busy) return;
    setBusy(true);
    const r = await inviteMember(project.id, value);
    setBusy(false);
    if (settle(r)) {
      setInviteValue('');
      await load();
      onChanged?.();
    }
  }, [busy, inviteValue, project.id, settle, load, onChanged]);

  const onRemove = useCallback(
    async (sub: string) => {
      setBusy(true);
      const r = await removeMember(project.id, sub);
      setBusy(false);
      if (settle(r)) {
        await load();
        onChanged?.();
      }
    },
    [project.id, settle, load, onChanged],
  );

  const onLeave = useCallback(async () => {
    setBusy(true);
    const r = await leaveProject(project.id);
    setBusy(false);
    setConfirmLeave(false);
    if (r.ok) {
      // The Member no longer has access: let the surface drop it and fall back to Personal.
      onChanged?.();
      onNotFound();
    } else if (r.kind === 'not_found') {
      onNotFound();
    } else if (r.kind === 'error') {
      setError(r.message);
    }
  }, [project.id, onChanged, onNotFound]);

  const memberLabel = (m: ProjectMember) => m.display || m.sub;

  return (
    <div style={containerStyle} data-testid="members-tab">
      {error && <p style={errorStyle} role="alert">{error}</p>}

      <section>
        <h3 style={headingStyle}>Owner</h3>
        <div style={rowStyle}>
          <span>{detail ? memberLabel(detail.owner) : '…'}</span>
          <span style={badgeStyle}>owner</span>
        </div>
      </section>

      {!project.personal && (
        <section>
          <h3 style={headingStyle}>Members</h3>
          {detail && detail.members.length === 0 && (
            <p style={mutedStyle}>No members yet.</p>
          )}
          {detail?.members.map((m) => (
            <div key={m.sub} style={rowStyle}>
              <span>{memberLabel(m)}</span>
              <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <span style={badgeStyle}>member</span>
                {isOwner && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => onRemove(m.sub)}
                    disabled={busy}
                    aria-label={`Remove ${memberLabel(m)}`}
                  >
                    Remove
                  </Button>
                )}
              </span>
            </div>
          ))}

          {isOwner && (
            <div style={inviteRowStyle}>
              <input
                style={inputStyle}
                value={inviteValue}
                onChange={(e) => setInviteValue(e.target.value)}
                placeholder="Email or username"
                aria-label="Invite by email or username"
                disabled={busy}
              />
              <Button variant="primary" size="sm" onClick={onInvite} disabled={busy || !inviteValue.trim()}>
                Invite
              </Button>
            </div>
          )}
        </section>
      )}

      {!project.personal && !isOwner && (
        <section>
          <Button variant="danger" size="sm" onClick={() => setConfirmLeave(true)} disabled={busy}>
            Leave project
          </Button>
        </section>
      )}

      <LeaveProjectDialog
        open={confirmLeave}
        projectName={project.name}
        loading={busy}
        onConfirm={onLeave}
        onCancel={() => setConfirmLeave(false)}
      />
    </div>
  );
}

/**
 * The `Leave project` confirmation. Shared with the Projects_Surface header's overflow menu, which is where a
 * Member leaves while the Members section is hidden (`SHOW_PROJECT_MEMBERS`).
 */
export function LeaveProjectDialog({
  open,
  projectName,
  loading,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  projectName: string;
  loading: boolean;
  onConfirm(): void;
  onCancel(): void;
}) {
  return (
    <ConfirmDialog
      open={open}
      title="Leave project"
      message={`Leave "${projectName}"? You will lose access until you are invited again.`}
      confirmLabel="Leave"
      loading={loading}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  );
}

const containerStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 20, padding: 4 };
const headingStyle: React.CSSProperties = {
  margin: '0 0 8px',
  fontSize: 'var(--fs-sm)',
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
};
const rowStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  padding: '6px 8px',
  borderBottom: `1px solid ${theme.colors.border}`,
  fontSize: 'var(--fs-sm)',
  color: theme.colors.textPrimary,
};
const badgeStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
  textTransform: 'uppercase',
};
const inviteRowStyle: React.CSSProperties = { display: 'flex', gap: 8, marginTop: 10 };
const inputStyle: React.CSSProperties = {
  flex: 1,
  padding: '6px 8px',
  fontSize: 'var(--fs-sm)',
  color: theme.colors.textPrimary,
  backgroundColor: theme.colors.background,
  border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.button,
};
const mutedStyle: React.CSSProperties = { color: theme.colors.textSecondary, fontSize: 'var(--fs-xs)', margin: 0 };
const errorStyle: React.CSSProperties = { color: theme.colors.error, fontSize: 'var(--fs-sm)', margin: 0 };
