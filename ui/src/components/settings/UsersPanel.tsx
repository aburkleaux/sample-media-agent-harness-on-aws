// UsersPanel — admin-only user directory + management (Epic D, R7).
//
// Reads GET /admin/users (Cognito ListUsers + ListUsersInGroup, marked with an `admin`
// flag). Drives grant/revoke admin, reset password, and SOFT delete (disable) with a 10s
// Undo (re-enable). Creating a user opens a dedicated page (onAddUser → SettingsView renders
// UserEditor). All controls are the shared design-system Button/Badge/Input for consistency;
// the proxy re-checks the gateway-admins group server-side (requireAdmin).
import React, { useCallback, useEffect, useState } from 'react';
import { theme } from '../../styles/tokens';
import Button from '../ui/Button';
import Badge, { type BadgeVariant } from '../ui/Badge';
import { Input } from '../ui/Input';
import {
  listUsers, setUserAdmin, resetUserPassword, disableUser, enableUser,
  AdminApiError, type AdminUser,
} from '../../services/adminApi';

type Notice = { kind: 'success' | 'error'; text: string };

/** Undo window for a soft delete: 10s to re-enable, then it stands. Shared by the
 *  auto-dismiss timer and the countdown-bar animation so they can't drift. */
const UNDO_TTL_MS = 10000;

function statusVariant(status?: string, enabled?: boolean): BadgeVariant {
  if (enabled === false) return 'neutral';
  switch (status) {
    case 'CONFIRMED': return 'success';
    case 'FORCE_CHANGE_PASSWORD':
    case 'RESET_REQUIRED': return 'warning';
    default: return 'neutral';
  }
}
function statusLabel(status?: string, enabled?: boolean): string {
  if (enabled === false) return 'Disabled';
  switch (status) {
    case 'CONFIRMED': return 'Confirmed';
    case 'FORCE_CHANGE_PASSWORD': return 'Invited';
    case 'RESET_REQUIRED': return 'Reset required';
    default: return status ?? '—';
  }
}

export default function UsersPanel({ onAddUser }: { onAddUser: () => void }) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [notice, setNotice] = useState<Notice | null>(null);
  const [undo, setUndo] = useState<{ username: string; email: string } | null>(null);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(t);
  }, [notice]);

  // Undo window for a soft delete — 10s to re-enable, then it stands.
  useEffect(() => {
    if (!undo) return;
    const t = setTimeout(() => setUndo(null), UNDO_TTL_MS);
    return () => clearTimeout(t);
  }, [undo]);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      setUsers(await listUsers());
    } catch (e) {
      setError(
        e instanceof AdminApiError && e.status === 403
          ? 'You are not an administrator.'
          : e instanceof Error ? e.message : 'Failed to load users',
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const toggleAdmin = useCallback(async (u: AdminUser) => {
    const grant = !u.admin;
    setBusy(u.username); setError(null);
    setUsers((prev) => prev.map((x) => (x.username === u.username ? { ...x, admin: grant } : x)));
    try {
      await setUserAdmin(u.username, grant);
    } catch (e) {
      setUsers((prev) => prev.map((x) => (x.username === u.username ? { ...x, admin: u.admin } : x)));
      setError(e instanceof Error ? e.message : 'Update failed');
    } finally {
      setBusy(null);
    }
  }, []);

  const onResetPassword = useCallback(async (u: AdminUser) => {
    setBusy(u.username); setError(null); setNotice(null);
    try {
      await resetUserPassword(u.username);
      setNotice({ kind: 'success', text: `Password reset initiated for ${u.email || u.username}.` });
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Reset failed');
    } finally {
      setBusy(null);
    }
  }, []);

  const setEnabled = useCallback(async (u: AdminUser, enabled: boolean) => {
    setBusy(u.username); setError(null); setNotice(null);
    setUsers((prev) => prev.map((x) => (x.username === u.username ? { ...x, enabled } : x)));
    try {
      if (enabled) await enableUser(u.username); else await disableUser(u.username);
      if (!enabled) setUndo({ username: u.username, email: u.email || u.username });
      else { setUndo(null); setNotice({ kind: 'success', text: `Re-enabled ${u.email || u.username}.` }); }
    } catch (e) {
      setUsers((prev) => prev.map((x) => (x.username === u.username ? { ...x, enabled: !enabled } : x)));
      setError(e instanceof Error ? e.message : 'Update failed');
    } finally {
      setBusy(null);
    }
  }, []);

  const doUndo = useCallback(async () => {
    if (!undo) return;
    const u = undo; setUndo(null); setBusy(u.username);
    setUsers((prev) => prev.map((x) => (x.username === u.username ? { ...x, enabled: true } : x)));
    try {
      await enableUser(u.username);
      setNotice({ kind: 'success', text: `Restored ${u.email}.` });
    } catch (e) {
      setUsers((prev) => prev.map((x) => (x.username === u.username ? { ...x, enabled: false } : x)));
      setError(e instanceof Error ? e.message : 'Undo failed');
    } finally {
      setBusy(null);
    }
  }, [undo]);

  const q = query.trim().toLowerCase();
  const filtered = q
    ? users.filter((u) => (u.email ?? '').toLowerCase().includes(q) || u.username.toLowerCase().includes(q))
    : users;

  return (
    <div style={{ maxWidth: 860 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16, marginBottom: 16 }}>
        <div>
          <div style={{ fontSize: 'var(--fs-lg)', fontWeight: 600, color: theme.colors.textPrimary }}>Users</div>
          <div style={{ fontSize: 'var(--fs-sm)', color: theme.colors.textSecondary, marginTop: 4 }}>
            Manage who can access and administer the harness. Admins are members of the <code>gateway-admins</code> group.
          </div>
        </div>
        <Button variant="primary" onClick={onAddUser} style={{ flexShrink: 0 }}>+ Add user</Button>
      </div>

      <Input type="search" inputSize="md" value={query} onChange={(e) => setQuery(e.target.value)}
        placeholder="Search users..." aria-label="Search users" style={{ width: '100%', marginBottom: 12 }} />

      {undo && (
        <div role="status" style={undoBarStyle} key={undo.username}>
          <style>{`@keyframes users-undo-countdown { from { transform: scaleX(1); } to { transform: scaleX(0); } }`}</style>
          <span style={{ flex: 1, color: theme.colors.textPrimary }}>
            Disabled <strong>{undo.email}</strong> — sign-in is blocked.
          </span>
          <Button variant="secondary" size="sm" onClick={() => void doUndo()}>Undo</Button>
          <div style={undoCountdownStyle} aria-hidden="true" />
        </div>
      )}
      {notice && <div role="status" style={notice.kind === 'success' ? noticeSuccessStyle : noticeErrorStyle}>{notice.text}</div>}
      {error && <div role="alert" style={noticeErrorStyle}>{error}</div>}

      {loading && <div style={mutedCenter}>Loading users…</div>}
      {!loading && users.length === 0 && !error && <div style={emptyStyle}>No users found.</div>}
      {!loading && users.length > 0 && filtered.length === 0 && <div style={emptyStyle}>No users match “{query}”.</div>}

      {filtered.length > 0 && (
        <table style={tableStyle}>
          <thead>
            <tr>
              <th style={thStyle}>User</th>
              <th style={thStyle}>Status</th>
              <th style={{ ...thStyle, textAlign: 'center' }}>Admin</th>
              <th style={{ ...thStyle, textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((u) => (
              <tr key={u.username}>
                <td style={tdStyle}>
                  <div style={{ color: theme.colors.textPrimary, fontWeight: 500 }}>{u.email || u.username}</div>
                  {u.email && <div style={{ fontSize: 'var(--fs-3xs)', color: theme.colors.textSecondary, marginTop: 2 }}>{u.username}</div>}
                </td>
                <td style={tdStyle}>
                  <Badge variant={statusVariant(u.status, u.enabled)}>{statusLabel(u.status, u.enabled)}</Badge>
                </td>
                <td style={{ ...tdStyle, textAlign: 'center' }}>
                  <Button
                    variant={u.admin ? 'primary' : 'secondary'} size="sm"
                    onClick={() => void toggleAdmin(u)} disabled={busy === u.username}
                    aria-pressed={!!u.admin} title={u.admin ? 'Revoke admin' : 'Grant admin'}
                  >
                    {u.admin ? 'Admin ✓' : 'Grant admin'}
                  </Button>
                </td>
                <td style={{ ...tdStyle, textAlign: 'right', whiteSpace: 'nowrap' }}>
                  <Button variant="secondary" size="sm" onClick={() => void onResetPassword(u)}
                    disabled={busy === u.username} title="Send a password reset" style={{ marginLeft: 6 }}>
                    Reset password
                  </Button>
                  {u.enabled === false ? (
                    <Button variant="secondary" size="sm" onClick={() => void setEnabled(u, true)}
                      disabled={busy === u.username} title="Re-enable this user" style={{ marginLeft: 6 }}>
                      Enable
                    </Button>
                  ) : (
                    <Button variant="danger" size="sm" onClick={() => void setEnabled(u, false)}
                      disabled={busy === u.username} title="Disable this user (reversible)" style={{ marginLeft: 6 }}>
                      Disable
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

const noticeSuccessStyle: React.CSSProperties = {
  padding: '8px 12px', marginBottom: 12, borderRadius: theme.radius.button,
  background: theme.colors.surfaceRaised, color: theme.colors.success,
  border: `1px solid ${theme.colors.success}`, fontSize: 'var(--fs-sm)',
};
const noticeErrorStyle: React.CSSProperties = {
  padding: '8px 12px', marginBottom: 12, borderRadius: theme.radius.button,
  background: theme.colors.surfaceRaised, color: theme.colors.error,
  border: `1px solid ${theme.colors.error}`, fontSize: 'var(--fs-sm)',
};
const undoBarStyle: React.CSSProperties = {
  position: 'relative', overflow: 'hidden',
  display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px', marginBottom: 12,
  borderRadius: theme.radius.button,
  background: theme.colors.surfaceRaised,
  border: `1px solid ${theme.colors.border}`,
  color: theme.colors.textPrimary, fontSize: 'var(--fs-sm)',
};
const undoCountdownStyle: React.CSSProperties = {
  position: 'absolute', left: 0, bottom: 0, height: 2, width: '100%',
  background: theme.colors.primary, opacity: 0.35, transformOrigin: 'left center',
  animation: `users-undo-countdown ${UNDO_TTL_MS}ms linear forwards`,
};
const mutedCenter: React.CSSProperties = { padding: 20, textAlign: 'center', color: theme.colors.textSecondary, fontSize: 'var(--fs-sm)' };
const emptyStyle: React.CSSProperties = {
  padding: 28, textAlign: 'center', color: theme.colors.textSecondary,
  border: `1px dashed ${theme.colors.border}`, borderRadius: theme.radius.panel,
};
const tableStyle: React.CSSProperties = { width: '100%', borderCollapse: 'collapse' };
const thStyle: React.CSSProperties = {
  textAlign: 'left', padding: '8px 12px', fontSize: 'var(--fs-3xs)', textTransform: 'uppercase',
  letterSpacing: '0.08em', color: theme.colors.textSecondary, borderBottom: `1px solid ${theme.colors.border}`,
};
const tdStyle: React.CSSProperties = {
  padding: '12px', borderBottom: `1px solid ${theme.colors.border}`, fontSize: 'var(--fs-sm)', verticalAlign: 'middle',
};
