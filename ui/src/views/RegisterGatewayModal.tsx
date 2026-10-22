// RegisterGatewayModal.tsx — Admin form for registering a new MCP gateway
// (R2, R7).
//
// Conventions match the rest of the admin surface:
//
//   - Portal-rendered overlay, like `ConfirmDialog` (the only existing
//     modal in the UI today). Keeps the form out of the table flow and
//     lets the host close it without unmounting `GatewayListView`.
//   - Theme tokens from `styles/tokens.ts` for borders, surfaces, and
//     typography, matching `GatewayListView.tsx` (the host).
//   - Form fields rendered inline rather than as a generic
//     `<FormField>` abstraction — there's no shared form component in
//     this codebase yet, and copying the per-field pattern from
//     `SkillEditor.tsx` keeps the touch surface small.
//
// R2 — secrets never round-trip back into state. The `client_secret`
// field is held in a local `formState.client_secret` slot for exactly the
// duration of one submit, and is wiped (along with the rest of the form)
// the instant the runtime reports success. There is no separate "secret"
// piece of React state; clearing the form state clears the secret. The
// `<input type="password">` rendering also keeps it out of the visible
// DOM and most clipboard managers.
//
// R7 — admin-only. The host (`GatewayListView`) gates entry to this view
// behind `useIsGatewayAdmin()`, but the runtime is the source of truth:
// `gateway_register` returns `{"error": "admin_required"}` when the
// caller isn't in the `gateway-admins` Cognito group. We surface that
// inline so a non-admin who somehow opened the modal sees a clear
// message rather than a generic failure.
//
// Inline errors covered: `admin_required`, `duplicate_id`,
// `invalid_payload`, `actor_unresolved`,
// `gateway_registry_unavailable`, plus a generic fallback for unknown
// codes and an HTTP/network error path.

import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { theme, tint } from '../styles/tokens';
import Button from '../components/ui/Button';
import {
  registerGateway,
  updateGateway,
  type GatewayAuthType,
  type GatewayDescriptor,
  type MutateGatewayErrorCode,
  type RegisterGatewayAuth,
  type RegisterGatewayErrorCode,
  type RegisterGatewayPayload,
  type UpdateGatewayPayload,
} from '../api';

export interface RegisterGatewayModalProps {
  /** When `false`, the modal renders nothing. */
  open: boolean;
  /** Called when the user dismisses the modal without submitting. */
  onCancel: () => void;
  /**
   * Called with the persisted descriptor on successful registration or
   * update. The host is expected to refresh its gateway list and close
   * the modal.
   */
  onSuccess: (gateway: GatewayDescriptor) => void;
  /**
   * When provided, the modal renders in **edit mode**: the form is
   * prefilled from this descriptor, the `id` field is read-only, and
   * submit calls `gateway_update` rather than `gateway_register`. The
   * `client_secret` field stays blank in edit mode so leaving it
   * untouched preserves the existing secret ARN (per the registry's
   * update-time secret-preservation contract); typing a value rotates
   * the secret.
   */
  editing?: GatewayDescriptor | null;
}

/**
 * Local form state. Kept as a single object so a successful submit can
 * reset everything — including the secret — in one assignment.
 */
interface FormState {
  id: string;
  name: string;
  description: string;
  url: string;
  auth_type: GatewayAuthType;
  // jwt-only
  token_endpoint: string;
  client_id: string;
  client_secret: string;
  scope: string;
  // sigv4-only
  region: string;
  service: string;
}

const EMPTY_FORM: FormState = {
  id: '',
  name: '',
  description: '',
  url: '',
  auth_type: 'jwt',
  token_endpoint: '',
  client_id: '',
  client_secret: '',
  scope: '',
  region: '',
  service: '',
};

/**
 * Build the runtime payload from form state. Exported for unit
 * testability — the conditional auth-config branch is the part most
 * likely to drift if a new `auth_type` is added.
 */
export function buildRegisterPayload(form: FormState): RegisterGatewayPayload {
  let auth_config: RegisterGatewayAuth;
  switch (form.auth_type) {
    case 'jwt':
      auth_config = {
        auth_type: 'jwt',
        token_endpoint: form.token_endpoint.trim(),
        client_id: form.client_id.trim(),
        client_secret: form.client_secret,
        scope: form.scope.trim(),
      };
      break;
    case 'sigv4':
      auth_config = {
        auth_type: 'sigv4',
        region: form.region.trim(),
        service: form.service.trim(),
      };
      break;
    case 'none':
    default:
      auth_config = { auth_type: 'none' };
      break;
  }
  return {
    id: form.id.trim(),
    name: form.name.trim(),
    description: form.description.trim(),
    url: form.url.trim(),
    auth_type: form.auth_type,
    auth_config,
    enabled: true,
  };
}

/**
 * Build the runtime patch for `gateway_update` from form state. Mirrors
 * `buildRegisterPayload` but omits the `id` (the runtime reads it from
 * the top-level payload, not from the patch) and only includes
 * `client_secret` on the JWT branch when the user actually typed a
 * value. Per the registry's update-time secret-preservation contract
 * (see `GatewayRegistry.update` in `agent/src/gateway_registry.py`),
 * leaving `client_secret_arn` out of the JWT auth_config preserves the
 * existing ARN — which is what we want when the user leaves the secret
 * field blank in edit mode. Secret rotation is a separate flow today
 * (design "Risks" §2 calls it out as a future "rotate secret" action),
 * so even when a value is typed it's treated as opaque by this layer
 * and forwarded for the runtime to decide on.
 */
export function buildUpdatePayload(form: FormState): UpdateGatewayPayload {
  let auth_config: RegisterGatewayAuth;
  switch (form.auth_type) {
    case 'jwt': {
      const jwt: RegisterGatewayAuth = {
        auth_type: 'jwt',
        token_endpoint: form.token_endpoint.trim(),
        client_id: form.client_id.trim(),
        client_secret: form.client_secret,
        scope: form.scope.trim(),
      };
      // Drop the empty secret slot so the registry's "key not in patch"
      // path is taken (preserves existing ARN). This is the contract
      // documented on `GatewayRegistry.update`: when an inner JWT
      // auth_config dict is missing `client_secret_arn`, the existing
      // value is copied forward.
      if (!form.client_secret) {
        delete (jwt as { client_secret?: string }).client_secret;
      }
      auth_config = jwt;
      break;
    }
    case 'sigv4':
      auth_config = {
        auth_type: 'sigv4',
        region: form.region.trim(),
        service: form.service.trim(),
      };
      break;
    case 'none':
    default:
      auth_config = { auth_type: 'none' };
      break;
  }
  return {
    name: form.name.trim(),
    description: form.description.trim(),
    url: form.url.trim(),
    auth_type: form.auth_type,
    auth_config,
  };
}

/**
 * Prefill form state from an existing descriptor (edit mode). The
 * `client_secret` slot stays blank — the runtime never returns the
 * existing secret to the UI, and leaving it blank in `buildUpdatePayload`
 * keeps the existing ARN intact.
 */
export function formStateFromDescriptor(descriptor: GatewayDescriptor): FormState {
  const base = { ...EMPTY_FORM };
  base.id = descriptor.id;
  base.name = descriptor.name;
  base.description = descriptor.description ?? '';
  base.url = descriptor.url;
  base.auth_type = descriptor.auth_type;
  const cfg = descriptor.auth_config;
  if (cfg.auth_type === 'jwt') {
    base.token_endpoint = cfg.token_endpoint ?? '';
    base.client_id = cfg.client_id ?? '';
    base.scope = cfg.scope ?? '';
    // `client_secret` intentionally stays empty — never round-tripped.
  } else if (cfg.auth_type === 'sigv4') {
    base.region = cfg.region ?? '';
    base.service = cfg.service ?? '';
  }
  return base;
}

/**
 * Map a runtime error code (or thrown message) to a human-readable
 * banner. Exported so unit tests can assert the message contract without
 * driving the React tree. Handles the union of `gateway_register` and
 * `gateway_update` error codes — the modal renders both depending on
 * whether it's in register or edit mode.
 */
export function formatRegisterError(
  code: RegisterGatewayErrorCode | MutateGatewayErrorCode,
  message?: string,
): string {
  switch (code) {
    case 'admin_required':
      return "You aren't a member of the `gateway-admins` group. Ask an existing admin to add you.";
    case 'duplicate_id':
      return 'A gateway with this id already exists. Pick a different id or edit the existing entry.';
    case 'not_found':
      return 'This gateway no longer exists. Refresh the list and try again.';
    case 'invalid_payload':
      return message
        ? `Invalid payload: ${message}`
        : 'One or more fields are invalid. Check the form and try again.';
    case 'actor_unresolved':
      return "Couldn't determine your identity from the session. Sign out and sign back in, then retry.";
    case 'gateway_registry_unavailable':
      return 'The gateway registry is not configured on this runtime. Contact an operator.';
    case 'unknown':
    default:
      return message ?? 'Registration failed for an unknown reason. Please retry.';
  }
}

export default function RegisterGatewayModal({
  open,
  onCancel,
  onSuccess,
  editing,
}: RegisterGatewayModalProps) {
  const isEditing = !!editing;
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [errorBanner, setErrorBanner] = useState<string | null>(null);
  const idInputRef = useRef<HTMLInputElement>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);

  // Reset form whenever the modal transitions closed -> open. This keeps
  // the secret-field-cleared invariant tight: even if the host re-opens
  // the modal after a previous submit, no stale value can leak in. In
  // edit mode, prefill from the descriptor; the secret slot stays empty
  // (the runtime never echoes the existing secret back).
  useEffect(() => {
    if (open) {
      setForm(editing ? formStateFromDescriptor(editing) : EMPTY_FORM);
      setErrorBanner(null);
      setSubmitting(false);
      // Focus the first editable field shortly after the portal mounts.
      // In edit mode the id is read-only so we skip past it to the name.
      const handle = window.setTimeout(() => {
        if (editing) {
          nameInputRef.current?.focus();
        } else {
          idInputRef.current?.focus();
        }
      }, 0);
      return () => window.clearTimeout(handle);
    }
  }, [open, editing]);

  // Escape closes the modal, matching ConfirmDialog's behavior. Disabled
  // while a submit is in flight so we don't leave the host with a
  // half-registered gateway in flight + a closed modal.
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) {
        onCancel();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [open, submitting, onCancel]);

  if (!open) return null;

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  /**
   * Client-side check for the always-required core fields. Auth-specific
   * sub-fields are validated server-side via `invalid_payload`; we keep
   * the local check minimal to avoid drifting from the runtime contract.
   * In edit mode the id is read-only so we don't re-check it here.
   */
  const requiredMissing = (): string | null => {
    if (!isEditing && !form.id.trim()) return 'id';
    if (!form.name.trim()) return 'name';
    if (!form.url.trim()) return 'url';
    return null;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;

    const missing = requiredMissing();
    if (missing) {
      setErrorBanner(`Missing required field: ${missing}`);
      return;
    }

    setSubmitting(true);
    setErrorBanner(null);

    try {
      if (isEditing && editing) {
        const patch = buildUpdatePayload(form);
        const result = await updateGateway(editing.id, patch);
        // R2: clear the form (including the secret slot) the moment we
        // have a definitive result. Wiping form state wipes the secret.
        setForm(EMPTY_FORM);
        if (result.ok) {
          onSuccess(result.gateway);
          return;
        }
        setErrorBanner(formatRegisterError(result.error, result.message));
      } else {
        const payload = buildRegisterPayload(form);
        const result = await registerGateway(payload);
        setForm(EMPTY_FORM);
        if (result.ok) {
          onSuccess(result.gateway);
          return;
        }
        setErrorBanner(formatRegisterError(result.error, result.message));
      }
    } catch (err) {
      // Network / HTTP / parse failure. Still wipe the form so the secret
      // doesn't linger across a retry.
      setForm(EMPTY_FORM);
      setErrorBanner(
        err instanceof Error ? err.message : 'Request failed.',
      );
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancel = () => {
    if (submitting) return;
    setForm(EMPTY_FORM);
    setErrorBanner(null);
    onCancel();
  };

  return createPortal(
    <div
      style={overlayStyle}
      role="dialog"
      aria-modal="true"
      aria-labelledby="register-gateway-title"
      onMouseDown={(e) => {
        // Click on backdrop dismisses (only when the click started on the
        // backdrop itself — guards against drag-to-select inside the form
        // ending on the backdrop).
        if (e.target === e.currentTarget && !submitting) {
          handleCancel();
        }
      }}
    >
      <form style={dialogStyle} onSubmit={handleSubmit} noValidate>
        <div style={headerStyle}>
          <h3 id="register-gateway-title" style={titleStyle}>
            {isEditing ? 'Edit Gateway' : 'Register Gateway'}
          </h3>
          <button
            type="button"
            onClick={handleCancel}
            disabled={submitting}
            aria-label="Close"
            style={closeBtnStyle}
          >
            ✕
          </button>
        </div>

        {errorBanner && (
          <div role="alert" style={errorBannerStyle}>
            {errorBanner}
          </div>
        )}

        <div style={fieldStyle}>
          <label htmlFor="rg-id" style={labelStyle}>
            Gateway id
          </label>
          <input
            id="rg-id"
            ref={idInputRef}
            type="text"
            value={form.id}
            onChange={(e) => update('id', e.target.value)}
            placeholder="audio-processing"
            autoComplete="off"
            spellCheck={false}
            style={isEditing ? readOnlyInputStyle : inputStyle}
            disabled={submitting}
            readOnly={isEditing}
            required
          />
          <span style={hintStyle}>
            {isEditing
              ? 'The gateway id cannot be changed after registration.'
              : 'Slug, unique. Used in tool prefixes and URLs.'}
          </span>
        </div>

        <div style={fieldStyle}>
          <label htmlFor="rg-name" style={labelStyle}>
            Display name
          </label>
          <input
            id="rg-name"
            ref={nameInputRef}
            type="text"
            value={form.name}
            onChange={(e) => update('name', e.target.value)}
            placeholder="Audio Processing Gateway"
            style={inputStyle}
            disabled={submitting}
            required
          />
        </div>

        <div style={fieldStyle}>
          <label htmlFor="rg-description" style={labelStyle}>
            Description
          </label>
          <textarea
            id="rg-description"
            value={form.description}
            onChange={(e) => update('description', e.target.value)}
            placeholder="What this gateway does and when to use it."
            rows={2}
            style={textareaStyle}
            disabled={submitting}
          />
        </div>

        <div style={fieldStyle}>
          <label htmlFor="rg-url" style={labelStyle}>
            Endpoint URL
          </label>
          <input
            id="rg-url"
            type="url"
            value={form.url}
            onChange={(e) => update('url', e.target.value)}
            placeholder="https://gateway.example.com/mcp"
            autoComplete="off"
            spellCheck={false}
            style={inputStyle}
            disabled={submitting}
            required
          />
        </div>

        <div style={fieldStyle}>
          <label htmlFor="rg-auth-type" style={labelStyle}>
            Auth type
          </label>
          <select
            id="rg-auth-type"
            value={form.auth_type}
            onChange={(e) =>
              update('auth_type', e.target.value as GatewayAuthType)
            }
            style={inputStyle}
            disabled={submitting}
          >
            <option value="jwt">OAuth (JWT)</option>
            <option value="sigv4">AWS SigV4</option>
            <option value="none">None</option>
          </select>
        </div>

        {form.auth_type === 'jwt' && (
          <JwtAuthFields
            form={form}
            update={update}
            disabled={submitting}
            isEditing={isEditing}
          />
        )}
        {form.auth_type === 'sigv4' && (
          <SigV4AuthFields form={form} update={update} disabled={submitting} />
        )}

        <div style={actionsStyle}>
          <Button variant="ghost" size="md" type="button" onClick={handleCancel} disabled={submitting}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="md"
            type="submit"
            disabled={submitting}
            busy={submitting}
            busyLabel={isEditing ? 'Saving…' : 'Registering…'}
          >
            {isEditing ? 'Save Changes' : 'Register'}
          </Button>
        </div>
      </form>
    </div>,
    document.body,
  );
}

interface SubFieldProps {
  form: FormState;
  update: <K extends keyof FormState>(key: K, value: FormState[K]) => void;
  disabled: boolean;
  isEditing?: boolean;
}

function JwtAuthFields({ form, update, disabled, isEditing }: SubFieldProps) {
  return (
    <div style={subSectionStyle}>
      <div style={subSectionTitleStyle}>OAuth (JWT) credentials</div>
      <div style={fieldStyle}>
        <label htmlFor="rg-token-endpoint" style={labelStyle}>
          Token endpoint
        </label>
        <input
          id="rg-token-endpoint"
          type="url"
          value={form.token_endpoint}
          onChange={(e) => update('token_endpoint', e.target.value)}
          placeholder="https://auth.example.com/oauth/token"
          autoComplete="off"
          spellCheck={false}
          style={inputStyle}
          disabled={disabled}
        />
      </div>
      <div style={fieldStyle}>
        <label htmlFor="rg-client-id" style={labelStyle}>
          Client id
        </label>
        <input
          id="rg-client-id"
          type="text"
          value={form.client_id}
          onChange={(e) => update('client_id', e.target.value)}
          autoComplete="off"
          spellCheck={false}
          style={inputStyle}
          disabled={disabled}
        />
      </div>
      <div style={fieldStyle}>
        <label htmlFor="rg-client-secret" style={labelStyle}>
          Client secret{isEditing ? ' (leave blank to keep current)' : ''}
        </label>
        <input
          id="rg-client-secret"
          type="password"
          value={form.client_secret}
          onChange={(e) => update('client_secret', e.target.value)}
          placeholder={isEditing ? '••••••••  (unchanged)' : ''}
          autoComplete="new-password"
          spellCheck={false}
          style={inputStyle}
          disabled={disabled}
        />
        <span style={hintStyle}>
          {isEditing
            ? 'Leave blank to keep the current secret (the existing ARN is preserved). Today the runtime ignores raw secret rotation in updates; a dedicated rotate-secret action is planned (design "Risks" §2).'
            : 'Stored in Secrets Manager. Never written to DDB and not echoed back to this form after submit.'}
        </span>
      </div>
      <div style={fieldStyle}>
        <label htmlFor="rg-scope" style={labelStyle}>
          Scope
        </label>
        <input
          id="rg-scope"
          type="text"
          value={form.scope}
          onChange={(e) => update('scope', e.target.value)}
          placeholder="gateway/invoke"
          autoComplete="off"
          spellCheck={false}
          style={inputStyle}
          disabled={disabled}
        />
      </div>
    </div>
  );
}

function SigV4AuthFields({ form, update, disabled }: SubFieldProps) {
  return (
    <div style={subSectionStyle}>
      <div style={subSectionTitleStyle}>AWS SigV4 credentials</div>
      <div style={fieldStyle}>
        <label htmlFor="rg-region" style={labelStyle}>
          Region
        </label>
        <input
          id="rg-region"
          type="text"
          value={form.region}
          onChange={(e) => update('region', e.target.value)}
          placeholder="us-east-1"
          autoComplete="off"
          spellCheck={false}
          style={inputStyle}
          disabled={disabled}
        />
      </div>
      <div style={fieldStyle}>
        <label htmlFor="rg-service" style={labelStyle}>
          Service
        </label>
        <input
          id="rg-service"
          type="text"
          value={form.service}
          onChange={(e) => update('service', e.target.value)}
          placeholder="bedrock-agentcore"
          autoComplete="off"
          spellCheck={false}
          style={inputStyle}
          disabled={disabled}
        />
      </div>
    </div>
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
  padding: 16,
};

const dialogStyle: React.CSSProperties = {
  background: theme.colors.surface,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  padding: '20px 24px',
  width: '100%',
  maxWidth: 520,
  maxHeight: '90vh',
  overflowY: 'auto',
  display: 'flex',
  flexDirection: 'column',
  gap: 12,
  fontFamily: theme.typography.fontFamily,
  boxShadow: theme.elevation.elevated,
};

const headerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
};

const titleStyle: React.CSSProperties = {
  margin: 0,
  fontSize: theme.typography.sizes.sectionTitle,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
};

const closeBtnStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  color: theme.colors.textSecondary,
  fontSize: 'var(--fs-md)',
  cursor: 'pointer',
  padding: '4px 8px',
  borderRadius: theme.radius.badge,
};

const errorBannerStyle: React.CSSProperties = {
  padding: '8px 12px',
  border: `1px solid ${theme.colors.error}`,
  borderRadius: theme.radius.button,
  background: tint(theme.colors.error),
  color: theme.colors.error,
  fontSize: 'var(--fs-xs)',
  lineHeight: 1.4,
};

const fieldStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
};

const labelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textSecondary,
  textTransform: 'uppercase',
  letterSpacing: 0.5,
};

const inputStyle: React.CSSProperties = {
  padding: '8px 10px',
  fontSize: 'var(--fs-sm)',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  background: theme.colors.surface,
  color: theme.colors.textPrimary,
  fontFamily: theme.typography.fontFamily,
  outline: 'none',
  boxSizing: 'border-box',
  width: '100%',
};

const readOnlyInputStyle: React.CSSProperties = {
  ...inputStyle,
  background: theme.colors.background,
  color: theme.colors.textSecondary,
  cursor: 'not-allowed',
};

const textareaStyle: React.CSSProperties = {
  ...inputStyle,
  fontFamily: theme.typography.fontFamily,
  resize: 'vertical',
  minHeight: 48,
  lineHeight: 1.4,
};

const hintStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  lineHeight: 1.4,
};

const subSectionStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 10,
  padding: 12,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  background: theme.colors.background,
};

const subSectionTitleStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textSecondary,
  textTransform: 'uppercase',
  letterSpacing: 0.5,
};

const actionsStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'flex-end',
  gap: 8,
  marginTop: 4,
};

