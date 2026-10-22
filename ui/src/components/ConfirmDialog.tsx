import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { theme, tint } from '../styles/tokens';
import Button from './ui/Button';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
  loading?: boolean;
  /**
   * When set, the user must type this exactly before Confirm enables.
   *
   * For irreversible actions only. The point is not friction for its own sake:
   * a two-step confirm that Enter can complete is one keystroke away from an
   * accident, and there is no undo for a purge.
   */
  requiredPhrase?: string;
  /** Extra content between the message and the confirmation input. */
  children?: React.ReactNode;
  /** Disable Confirm for a caller-owned reason (e.g. a preview still loading). */
  confirmDisabled?: boolean;
  /**
   * Shown inside the dialog when the action failed.
   *
   * Rendered here rather than replacing the dialog, because the dialog holds the
   * typed phrase and the reference list the user was reading — closing on error
   * would make them start over.
   */
  error?: string;
  /** Widen the dialog; the default 400 is too narrow for a blast-radius list. */
  maxWidth?: number;
  /** The confirm button's style: `danger` (default — this dialog guards deletes) or `primary` for a safe action. */
  confirmVariant?: 'danger' | 'primary';
}

/**
 * ConfirmDialog — A reusable confirmation dialog rendered as a portal overlay.
 * Used by all delete actions across drawers to require explicit user confirmation.
 *
 * Renders a semi-transparent backdrop that prevents interaction with content behind it,
 * with confirm and cancel buttons. Supports a loading state for async operations.
 */
export default function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  onConfirm,
  onCancel,
  loading = false,
  requiredPhrase,
  children,
  confirmDisabled = false,
  error,
  maxWidth,
  confirmVariant = 'danger',
}: ConfirmDialogProps) {
  const confirmButtonRef = useRef<HTMLButtonElement>(null);
  const phraseInputRef = useRef<HTMLInputElement>(null);
  const [typed, setTyped] = useState('');

  // Clear the typed phrase every time the dialog opens. Without this, a dialog
  // that was cancelled and reopened would come back already armed.
  useEffect(() => {
    if (open) setTyped('');
  }, [open, requiredPhrase]);

  // Focus the input when a phrase is required, the confirm button otherwise.
  // Focusing Confirm on a purge dialog would put an irreversible action one
  // Enter away.
  useEffect(() => {
    if (!open) return;
    if (requiredPhrase) phraseInputRef.current?.focus();
    else confirmButtonRef.current?.focus();
  }, [open, requiredPhrase]);

  // Handle Escape key to cancel
  useEffect(() => {
    if (!open) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !loading) {
        onCancel();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [open, loading, onCancel]);

  if (!open) return null;

  const phraseMatches = !requiredPhrase || typed.trim() === requiredPhrase;
  const blocked = loading || confirmDisabled || !phraseMatches;

  return createPortal(
    <div style={overlayStyle} role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title">
      <div style={{ ...dialogStyle, ...(maxWidth ? { maxWidth } : {}) }}>
        <h3 id="confirm-dialog-title" style={titleStyle}>{title}</h3>
        <p style={messageStyle}>{message}</p>
        {children}
        {requiredPhrase && (
          <label style={phraseLabelStyle}>
            Type <code style={phraseCodeStyle}>{requiredPhrase}</code> to confirm
            <input
              ref={phraseInputRef}
              style={phraseInputStyle}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              disabled={loading}
              autoComplete="off"
              spellCheck={false}
              aria-label={`Type ${requiredPhrase} to confirm`}
            />
          </label>
        )}
        {error && <p style={errorStyle}>{error}</p>}
        <div style={actionsStyle}>
          <Button
            ref={confirmButtonRef}
            variant={confirmVariant}
            size="md"
            onClick={onConfirm}
            disabled={blocked}
            busy={loading}
            busyLabel={'Processing\u2026'}
          >
            {confirmLabel}
          </Button>
          <Button variant="ghost" size="md" onClick={onCancel} disabled={loading}>
            {cancelLabel}
          </Button>
        </div>
      </div>
    </div>,
    document.body
  );
}

// --- Styles ---

const overlayStyle: React.CSSProperties = {
  position: 'fixed',
  top: 0,
  left: 0,
  right: 0,
  bottom: 0,
  backgroundColor: theme.colors.overlay,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: theme.zIndex.modal,
};

const dialogStyle: React.CSSProperties = {
  backgroundColor: theme.colors.surface,
  border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.panel,
  padding: '20px 24px',
  maxWidth: 400,
  width: '90%',
  boxShadow: theme.elevation.elevated,
};

const titleStyle: React.CSSProperties = {
  margin: '0 0 8px 0',
  fontSize: 'var(--fs-lg)',
  fontWeight: 600,
  // Was #e0e0e0 — light text for a permanently-dark dialog. The token migration
  // read that as a hairline grey and made the TITLE a border colour, which is
  // near-invisible now the dialog follows the theme.
  color: theme.colors.textPrimary,
};

const messageStyle: React.CSSProperties = {
  margin: '0 0 16px 0',
  fontSize: 'var(--fs-sm)',
  color: theme.colors.textTertiary,
  lineHeight: 1.5,
};

const phraseLabelStyle: React.CSSProperties = {
  display: 'block',
  margin: '0 0 16px 0',
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textTertiary,
};

const phraseCodeStyle: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 'var(--fs-xs)',
  // The exact phrase the user must retype to confirm — it has to be readable.
  color: theme.colors.textPrimary,
  backgroundColor: theme.colors.surfaceRaised,
  padding: '1px 5px',
  borderRadius: theme.radius.badge,
};

const phraseInputStyle: React.CSSProperties = {
  display: 'block',
  width: '100%',
  marginTop: 6,
  padding: '6px 8px',
  fontSize: 'var(--fs-sm)',
  fontFamily: 'var(--font-mono)',
  color: theme.colors.textPrimary,
  backgroundColor: theme.colors.background,
  border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.button,
  boxSizing: 'border-box',
};

const errorStyle: React.CSSProperties = {
  margin: '0 0 16px 0',
  fontSize: 'var(--fs-xs)',
  color: tint(theme.colors.error, 40),
  lineHeight: 1.5,
};

const actionsStyle: React.CSSProperties = {
  display: 'flex',
  gap: 10,
  justifyContent: 'flex-end',
};
