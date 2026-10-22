// UserEditor — the "add user" page (opened like a Hooks/asset editor from
// SettingsView). Collects optional first/last name; only email is required.
// Submits to POST /admin/users (Cognito AdminCreateUser → emails the invite).
import React, { useState } from 'react';
import { theme } from '../../styles/tokens';
import Button from '../ui/Button';
import { Input } from '../ui/Input';
import { addUser, AdminApiError } from '../../services/adminApi';

interface Props {
  onClose: () => void;
  onCreated: (email: string) => void;
}

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export default function UserEditor({ onClose, onCreated }: Props) {
  const [email, setEmail] = useState('');
  const [firstName, setFirstName] = useState('');
  const [lastName, setLastName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const emailValid = EMAIL_RE.test(email.trim());

  const submit = async () => {
    if (!emailValid) { setError('A valid email is required.'); return; }
    setBusy(true); setError(null);
    try {
      await addUser({
        email: email.trim(),
        firstName: firstName.trim() || undefined,
        lastName: lastName.trim() || undefined,
      });
      onCreated(email.trim());
    } catch (e) {
      setError(
        e instanceof AdminApiError && e.status === 403
          ? 'You are not an administrator.'
          : e instanceof Error ? e.message : 'Failed to create user',
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ maxWidth: 560 }}>
      <button onClick={onClose} style={backStyle}>← Back to Users</button>
      <div style={{ fontSize: 'var(--fs-lg)', fontWeight: 600, color: theme.colors.textPrimary, marginTop: 8 }}>Add user</div>
      <p style={{ fontSize: 'var(--fs-sm)', color: theme.colors.textSecondary, marginTop: 4, marginBottom: 20 }}>
        Create a user in the Cognito pool. Cognito emails them a temporary password. Only email is required.
      </p>

      {error && <div role="alert" style={errorStyle}>{error}</div>}

      <div style={fieldStyle}>
        <label style={labelStyle} htmlFor="ue-first">First name</label>
        <Input id="ue-first" value={firstName} onChange={(e) => setFirstName(e.target.value)} placeholder="Ada" />
      </div>
      <div style={fieldStyle}>
        <label style={labelStyle} htmlFor="ue-last">Last name</label>
        <Input id="ue-last" value={lastName} onChange={(e) => setLastName(e.target.value)} placeholder="Lovelace" />
      </div>
      <div style={fieldStyle}>
        <label style={labelStyle} htmlFor="ue-email">Email <span style={{ color: theme.colors.error }}>*</span></label>
        <Input
          id="ue-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void submit(); }}
          placeholder="ada@example.com" invalid={!!email && !emailValid} autoFocus
        />
      </div>

      <div style={{ display: 'flex', gap: 8, marginTop: 24 }}>
        <Button variant="primary" onClick={() => void submit()} busy={busy} busyLabel="Creating…" disabled={!emailValid}>
          Create user
        </Button>
        <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
      </div>
    </div>
  );
}

const backStyle: React.CSSProperties = {
  border: 'none', background: 'none', color: theme.colors.textSecondary,
  cursor: 'pointer', fontSize: 'var(--fs-sm)', padding: 0,
};
const fieldStyle: React.CSSProperties = { marginBottom: 14 };
const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: 'var(--fs-sm)', color: theme.colors.textPrimary,
  fontWeight: 500, marginBottom: 6,
};
const errorStyle: React.CSSProperties = {
  padding: '8px 12px', marginBottom: 16, borderRadius: theme.radius.button,
  background: theme.colors.surfaceRaised, color: theme.colors.error,
  border: `1px solid ${theme.colors.error}`, fontSize: 'var(--fs-sm)',
};
