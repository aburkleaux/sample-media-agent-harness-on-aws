// GatewayListView.tsx — Admin surface for the gateway registry (R3, R4, R7).
//
// Renders the list of registered gateways for members of the
// `gateway-admins` Cognito group. Non-admins see a 403-style "Forbidden"
// message in place of the table; the runtime is the authoritative gate
// (every mutation action checks `_is_admin` server-side), this view just
// keeps the chrome out of non-admin DOM. There is no router in the Lab UI
// today (see `ui/src/main.tsx` — it dispatches by `window.location.pathname`
// and otherwise renders `<LabApp />`), so the "redirect to /403" idea from
// the task description doesn't apply here. Inline 403 messaging matches the
// existing convention used by `AuthGate` for non-authenticated states.
//
// Task 8.4 wired up the register / edit / disable / delete row actions:
//   - "Register new gateway" button in the top bar opens
//     `RegisterGatewayModal` in register mode.
//   - "Edit" row action opens the same modal in edit mode (id read-only).
//   - The enabled-column toggle calls `gateway_update({id, enabled: !x})`.
//   - "Delete" row action opens a `ConfirmDialog` and calls
//     `gateway_delete(id)` on confirm.
//
// Task 8.5 added the "Test" row action. Clicking it calls the
// `gateway_test_connection` runtime action and renders the resulting
// tool count (with a tooltip listing sample tool names) — or the error
// string — inline in place of the row's "Last connected" column. The
// result is transient state on the row: it does not refresh the list and
// is cleared when the user re-tests or navigates away.

import React, { useCallback, useEffect, useState } from 'react';
import { theme, tint } from '../styles/tokens';
import Button from '../components/ui/Button';
import { useIsGatewayAdmin } from '../auth/isGatewayAdmin';
import ConfirmDialog from '../components/ConfirmDialog';
import { SETTINGS_CLOSE_BUTTON_GUTTER } from '../components/settings/panelChrome';
import RegisterGatewayModal from './RegisterGatewayModal';
import {
  deleteGateway,
  listGateways,
  testGatewayConnection,
  updateGateway,
  type GatewayAuthType,
  type GatewayDescriptor,
  type TestGatewayConnectionResult,
} from '../api';

/**
 * Format a `GatewayAuthType` discriminator into a short, human-readable
 * label suitable for the auth-type column. Exported for unit testability.
 */
export function formatAuthType(authType: GatewayAuthType): string {
  switch (authType) {
    case 'jwt':
      return 'OAuth (JWT)';
    case 'sigv4':
      return 'AWS SigV4';
    case 'none':
      return 'None';
    default: {
      // Defensive: a future auth_type added server-side should still render
      // something readable rather than blank or `[object Object]`.
      const exhaustive: string = String(authType);
      return exhaustive;
    }
  }
}

interface GatewayListViewProps {
  /** Optional click-handler so the wrapping shell can wire up a back button. */
  onBack?: () => void;
  /**
   * When true, render without the 100vh wrapper or top "Back to Workspace"
   * bar so the view fits inside an existing chrome (e.g. SettingsView's
   * tab content pane). The register/refresh actions move into a header
   * row above the title. Defaults to false (standalone full-page mode).
   */
  embedded?: boolean;
}

/**
 * Top-level state for the modal. The modal is reused for both register
 * and edit; `mode` distinguishes which one is active. Keeping it as a
 * single union simplifies open/close transitions — closing always
 * resolves to `closed`, opening sets the right variant explicitly.
 */
type ModalState =
  | { mode: 'closed' }
  | { mode: 'register' }
  | { mode: 'edit'; descriptor: GatewayDescriptor };

interface DeleteState {
  descriptor: GatewayDescriptor;
  loading: boolean;
}

interface TogglePending {
  /** Set of gateway ids whose toggle is currently in flight. */
  ids: Set<string>;
}

/**
 * Per-row state for the test-connection action. Three variants:
 *
 *   - `testing`  — request in flight; the row replaces "Last connected"
 *                  with a spinner-style "Testing…" placeholder.
 *   - `success`  — render `tool_count` with a tooltip of
 *                  `sample_tool_names`.
 *   - `error`    — render the formatted error string.
 *
 * State is keyed by gateway id and cleared when the row re-tests; nothing
 * here is persisted, in line with the task's "transient" requirement.
 */
type TestConnectionState =
  | { kind: 'testing' }
  | {
      kind: 'success';
      toolCount: number;
      sampleToolNames: string[];
    }
  | { kind: 'error'; message: string };

/**
 * Format a `testGatewayConnection` failure into a human-readable string
 * for inline display on the row.
 *
 * The connection-related cases (`registration_failed`, `probe_failed`) collapse
 * to one "Couldn't connect" wording, matching the MCP server form, because a
 * connection test is now one thing everywhere: `initialize` + `tools/list`. The
 * remaining cases are deliberately kept distinct — they are not connection
 * outcomes but action failures with different fixes (join a group, refresh the
 * list, sign in again, configure the runtime), and collapsing them would throw
 * away the only guidance the user gets.
 */
export function describeTestConnectionError(
  code: string,
  message: string | undefined,
  gatewayName: string,
): string {
  switch (code) {
    case 'admin_required':
      return "You aren't a member of the `gateway-admins` group.";
    case 'not_found':
      return `Gateway "${gatewayName}" no longer exists. Refresh the list and try again.`;
    case 'invalid_payload':
      return message
        ? `Invalid request: ${message}`
        : 'The runtime rejected the request as invalid.';
    case 'actor_unresolved':
      return "Couldn't determine your identity from the session. Sign out and sign back in, then retry.";
    case 'gateway_registry_unavailable':
      return 'The gateway registry is not configured on this runtime.';
    case 'registration_failed':
    case 'probe_failed':
      return message ? `Couldn't connect: ${message}` : "Couldn't connect.";
    default:
      return message ?? `Failed to test "${gatewayName}".`;
  }
}

export default function GatewayListView({ onBack, embedded = false }: GatewayListViewProps) {
  const isAdmin = useIsGatewayAdmin();

  const [gateways, setGateways] = useState<GatewayDescriptor[] | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Banner for inline mutation errors (toggle, delete). The modal owns
  // its own banner for register/edit, so this only surfaces row-action
  // failures — server-side admin gating, not_found races, etc.
  const [actionError, setActionError] = useState<string | null>(null);

  const [modal, setModal] = useState<ModalState>({ mode: 'closed' });
  const [deleteState, setDeleteState] = useState<DeleteState | null>(null);
  const [togglePending, setTogglePending] = useState<TogglePending>({ ids: new Set() });

  // Per-row test-connection state. Keyed by gateway id; an entry exists
  // only after the user has clicked "Test" on that row at least once.
  // The map is cleared on refresh so a freshly fetched list isn't
  // contaminated with results from gateways that may have been edited.
  const [testStateById, setTestStateById] = useState<Map<string, TestConnectionState>>(
    () => new Map(),
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listGateways();
      setGateways(result);
      // Clear transient test results — the underlying list may have
      // changed (edits, deletes), so any retained "tool_count: 12"
      // could be stale. The user can re-click Test to repopulate.
      setTestStateById(new Map());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load gateways');
      setGateways(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!isAdmin) return;
    void refresh();
  }, [isAdmin, refresh]);

  const handleModalSuccess = useCallback(() => {
    setModal({ mode: 'closed' });
    setActionError(null);
    void refresh();
  }, [refresh]);

  const handleEdit = useCallback((descriptor: GatewayDescriptor) => {
    setActionError(null);
    setModal({ mode: 'edit', descriptor });
  }, []);

  const handleRegister = useCallback(() => {
    setActionError(null);
    setModal({ mode: 'register' });
  }, []);

  const handleToggleEnabled = useCallback(
    async (descriptor: GatewayDescriptor) => {
      // Mark this id as in-flight so the row's toggle gets a busy
      // appearance and a second click is ignored. Idempotent set ops.
      setTogglePending((prev) => {
        const next = new Set(prev.ids);
        next.add(descriptor.id);
        return { ids: next };
      });
      setActionError(null);
      try {
        const result = await updateGateway(descriptor.id, {
          enabled: !descriptor.enabled,
        });
        if (!result.ok) {
          setActionError(
            describeMutateError(result.error, result.message, descriptor.name),
          );
        }
      } catch (err) {
        setActionError(
          err instanceof Error
            ? err.message
            : `Failed to update gateway ${descriptor.name}`,
        );
      } finally {
        setTogglePending((prev) => {
          const next = new Set(prev.ids);
          next.delete(descriptor.id);
          return { ids: next };
        });
        // Refresh regardless of success: on failure the optimistic
        // assumption was wrong and the table should reflect the durable
        // state, on success the row needs the new `updated_at` if we
        // ever surface it.
        void refresh();
      }
    },
    [refresh],
  );

  const handleDeleteRequest = useCallback((descriptor: GatewayDescriptor) => {
    setActionError(null);
    setDeleteState({ descriptor, loading: false });
  }, []);

  const handleDeleteCancel = useCallback(() => {
    setDeleteState((prev) => (prev?.loading ? prev : null));
  }, []);

  const handleDeleteConfirm = useCallback(async () => {
    if (!deleteState) return;
    const target = deleteState.descriptor;
    setDeleteState({ descriptor: target, loading: true });
    try {
      const result = await deleteGateway(target.id);
      if (!result.ok) {
        setActionError(
          describeMutateError(result.error, result.message, target.name),
        );
      }
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : `Failed to delete gateway ${target.name}`,
      );
    } finally {
      setDeleteState(null);
      void refresh();
    }
  }, [deleteState, refresh]);

  const handleTestConnection = useCallback(
    async (descriptor: GatewayDescriptor) => {
      // Mark the row as testing immediately so the "Last connected"
      // column flips to "Testing…" without waiting for the network.
      setTestStateById((prev) => {
        const next = new Map(prev);
        next.set(descriptor.id, { kind: 'testing' });
        return next;
      });

      const finalize = (state: TestConnectionState) => {
        setTestStateById((prev) => {
          const next = new Map(prev);
          next.set(descriptor.id, state);
          return next;
        });
      };

      let result: TestGatewayConnectionResult;
      try {
        result = await testGatewayConnection(descriptor.id);
      } catch (err) {
        finalize({
          kind: 'error',
          message:
            err instanceof Error
              ? err.message
              : `Failed to test "${descriptor.name}".`,
        });
        return;
      }

      if (result.ok) {
        finalize({
          kind: 'success',
          toolCount: result.tool_count,
          sampleToolNames: result.sample_tool_names,
        });
      } else {
        finalize({
          kind: 'error',
          message: describeTestConnectionError(
            result.error,
            result.message,
            descriptor.name,
          ),
        });
      }
    },
    [],
  );

  if (!isAdmin) {
    return <ForbiddenScreen onBack={onBack} embedded={embedded} />;
  }

  // The body — title, banners, table — is identical between standalone
  // and embedded modes. Only the wrapper chrome differs: standalone
  // ships a 100vh column with a "Back to Workspace" bar, embedded drops
  // both so the host (e.g. SettingsView) can supply its own.
  const headerActions = (
    <div style={embedded ? embeddedHeaderActionsStyle : topBarStyle}>
      {!embedded && onBack && (
        <Button variant="ghost" size="md" onClick={onBack} aria-label="Back to workspace">
          ← Back to Workspace
        </Button>
      )}
      {!embedded && <div style={{ flex: 1 }} />}
      <Button variant="primary" size="md" onClick={handleRegister}>
        + Register new gateway
      </Button>
      <Button size="md" onClick={() => void refresh()} disabled={loading} busy={loading} busyLabel="Refreshing…">
        Refresh
      </Button>
    </div>
  );

  const body = (
    <>
      <h2 style={titleStyle}>Gateway Registry</h2>
      <p style={subtitleStyle}>
        MCP gateways registered against this runtime. Visible to members of the
        <code style={codeStyle}> gateway-admins</code> group.
      </p>

      {error && (
        <div role="alert" style={errorStyle}>
          {error}
          <Button size="sm" onClick={() => void refresh()}>Retry</Button>
        </div>
      )}

      {actionError && (
        <div role="alert" style={errorStyle}>
          {actionError}
          <Button size="sm" onClick={() => setActionError(null)} aria-label="Dismiss error">Dismiss</Button>
        </div>
      )}

      {loading && gateways === null && (
        <div style={loadingStyle}>Loading gateways…</div>
      )}

      {gateways !== null && gateways.length === 0 && !loading && (
        <div style={emptyStateStyle}>
          No gateways registered yet.
        </div>
      )}

      {gateways !== null && gateways.length > 0 && (
        <GatewayTable
          gateways={gateways}
          togglePending={togglePending}
          testStateById={testStateById}
          onEdit={handleEdit}
          onToggleEnabled={handleToggleEnabled}
          onDelete={handleDeleteRequest}
          onTest={handleTestConnection}
        />
      )}
    </>
  );

  const modals = (
    <>
      <RegisterGatewayModal
        open={modal.mode !== 'closed'}
        editing={modal.mode === 'edit' ? modal.descriptor : null}
        onCancel={() => setModal({ mode: 'closed' })}
        onSuccess={handleModalSuccess}
      />

      <ConfirmDialog
        open={deleteState !== null}
        title="Delete gateway"
        message={
          deleteState
            ? `Disable "${deleteState.descriptor.name}" (${deleteState.descriptor.id})? Its tools will stop appearing in the consent panel. The DDB row is preserved for audit and can be re-enabled later.`
            : ''
        }
        confirmLabel="Delete"
        cancelLabel="Cancel"
        loading={deleteState?.loading ?? false}
        onConfirm={() => void handleDeleteConfirm()}
        onCancel={handleDeleteCancel}
      />
    </>
  );

  if (embedded) {
    return (
      <div style={embeddedContainerStyle}>
        {headerActions}
        {body}
        {modals}
      </div>
    );
  }

  return (
    <div style={containerStyle}>
      {headerActions}
      <div style={bodyStyle}>{body}</div>
      {modals}
    </div>
  );
}

/**
 * Format a structured `MutateGatewayErrorCode` for the row-action error
 * banner. Mirrors `formatRegisterError` in `RegisterGatewayModal` but
 * scoped to the codes the update/delete actions actually return.
 */
function describeMutateError(
  code: string,
  message: string | undefined,
  gatewayName: string,
): string {
  switch (code) {
    case 'admin_required':
      return "You aren't a member of the `gateway-admins` group. Ask an existing admin to add you.";
    case 'not_found':
      return `Gateway "${gatewayName}" no longer exists. Refresh the list and try again.`;
    case 'invalid_payload':
      return message
        ? `Invalid request: ${message}`
        : 'The runtime rejected the request as invalid.';
    case 'actor_unresolved':
      return "Couldn't determine your identity from the session. Sign out and sign back in, then retry.";
    case 'gateway_registry_unavailable':
      return 'The gateway registry is not configured on this runtime. Contact an operator.';
    default:
      return message ?? `Failed to update gateway "${gatewayName}".`;
  }
}

/**
 * The 403 surface for non-admins. Inline rather than a route redirect — the
 * Lab UI doesn't have a `/403` route to send them to.
 */
function ForbiddenScreen({
  onBack,
  embedded = false,
}: {
  onBack?: () => void;
  embedded?: boolean;
}) {
  const style = embedded ? embeddedForbiddenStyle : forbiddenStyle;
  return (
    <div role="alert" aria-live="polite" style={style}>
      <div style={forbiddenTitleStyle}>403 — Forbidden</div>
      <div style={forbiddenBodyStyle}>
        You don't have access to the Gateway Registry. Membership in the
        <code style={codeStyle}> gateway-admins</code> Cognito group is
        required.
      </div>
      {!embedded && onBack && (
        <Button variant="ghost" size="md" onClick={onBack}>
          ← Back to Workspace
        </Button>
      )}
    </div>
  );
}

interface GatewayTableProps {
  gateways: GatewayDescriptor[];
  togglePending: TogglePending;
  testStateById: Map<string, TestConnectionState>;
  onEdit: (gateway: GatewayDescriptor) => void;
  onToggleEnabled: (gateway: GatewayDescriptor) => void;
  onDelete: (gateway: GatewayDescriptor) => void;
  onTest: (gateway: GatewayDescriptor) => void;
}

function GatewayTable({
  gateways,
  togglePending,
  testStateById,
  onEdit,
  onToggleEnabled,
  onDelete,
  onTest,
}: GatewayTableProps) {
  return (
    <div style={tableWrapperStyle}>
      <table style={tableStyle}>
        <thead>
          <tr>
            <th style={thStyle}>Name</th>
            <th style={thStyle}>URL</th>
            <th style={thStyle}>Auth</th>
            <th style={thStyle}>Enabled</th>
            <th style={thStyle}>Last connected</th>
            <th style={thStyle}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {gateways.map((gateway) => (
            <GatewayRow
              key={gateway.id}
              gateway={gateway}
              togglePending={togglePending.ids.has(gateway.id)}
              testState={testStateById.get(gateway.id)}
              onEdit={() => onEdit(gateway)}
              onToggleEnabled={() => onToggleEnabled(gateway)}
              onDelete={() => onDelete(gateway)}
              onTest={() => onTest(gateway)}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface GatewayRowProps {
  gateway: GatewayDescriptor;
  togglePending: boolean;
  testState: TestConnectionState | undefined;
  onEdit: () => void;
  onToggleEnabled: () => void;
  onDelete: () => void;
  onTest: () => void;
}

function GatewayRow({
  gateway,
  togglePending,
  testState,
  onEdit,
  onToggleEnabled,
  onDelete,
  onTest,
}: GatewayRowProps) {
  const testing = testState?.kind === 'testing';
  return (
    <tr>
      <td style={tdStyle}>
        <div style={nameCellStyle}>
          <span style={nameStyle}>{gateway.name}</span>
          <span style={idStyle}>{gateway.id}</span>
        </div>
      </td>
      <td style={tdStyle}>
        <code style={urlStyle} title={gateway.url}>
          {gateway.url}
        </code>
      </td>
      <td style={tdStyle}>{formatAuthType(gateway.auth_type)}</td>
      <td style={tdStyle}>
        <EnabledToggle
          enabled={gateway.enabled}
          onClick={onToggleEnabled}
          pending={togglePending}
          gatewayName={gateway.name}
        />
      </td>
      <td style={tdStyle}>
        <TestConnectionStatus state={testState} />
      </td>
      <td style={tdStyle}>
        <div style={actionsCellStyle}>
          <Button
            type="button"
            size="sm"
            onClick={onTest}
            disabled={testing}
            busy={testing}
            busyLabel="Testing…"
            aria-label={`Test connection for ${gateway.name}`}
            title="Probe the gateway and report the live tool count"
          >
            Test
          </Button>
          <Button type="button" size="sm" onClick={onEdit} aria-label={`Edit ${gateway.name}`}>Edit</Button>
          <Button variant="danger" size="sm" type="button" onClick={onDelete} aria-label={`Delete ${gateway.name}`}>Delete</Button>
        </div>
      </td>
    </tr>
  );
}

/**
 * Inline status renderer for the "Last connected" column. Returns a
 * default "Unknown" muted label when there's no test result for the row,
 * a "Testing…" placeholder while the probe is in flight, the tool count
 * with a sample-names tooltip on success, or the formatted error string
 * on failure.
 *
 * The tooltip is rendered via a native `title` attribute — the existing
 * UI doesn't ship a popover primitive and the task description explicitly
 * allows tooltip-or-popover, so the simpler primitive matches the
 * project's conventions (see the URL column's `title` for precedent).
 */
function TestConnectionStatus({
  state,
}: {
  state: TestConnectionState | undefined;
}) {
  if (!state) {
    return <span style={mutedStyle}>Unknown</span>;
  }
  if (state.kind === 'testing') {
    return <span style={mutedStyle}>Testing…</span>;
  }
  if (state.kind === 'success') {
    const { toolCount, sampleToolNames } = state;
    const label =
      toolCount === 1 ? '1 tool' : `${toolCount} tools`;
    const tooltip =
      sampleToolNames.length > 0
        ? `Sample tools: ${sampleToolNames.join(', ')}`
        : 'No tools reported';
    return (
      <span
        style={successPillStyle}
        title={tooltip}
        aria-label={`${label}. ${tooltip}`}
      >
        ✓ {label}
      </span>
    );
  }
  // error
  return (
    <span
      style={errorPillStyle}
      title={state.message}
      role="alert"
      aria-label={`Test connection failed: ${state.message}`}
    >
      ✕ {state.message}
    </span>
  );
}

/**
 * Interactive enabled/disabled toggle. Calls `onClick` when activated and
 * reflects an in-flight state via `pending`. Re-clicks during a pending
 * call are dropped at the handler level (the parent guards on the id-set
 * membership) but the visual cue makes the busy state explicit.
 */
function EnabledToggle({
  enabled,
  onClick,
  pending,
  gatewayName,
}: {
  enabled: boolean;
  onClick: () => void;
  pending: boolean;
  gatewayName: string;
}) {
  const trackBg = enabled ? theme.colors.success : theme.colors.border;
  const knobOffset = enabled ? 18 : 2;
  const label = enabled ? 'Disable' : 'Enable';
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      aria-label={`${label} ${gatewayName}`}
      onClick={onClick}
      disabled={pending}
      style={{
        ...toggleButtonStyle,
        background: trackBg,
        opacity: pending ? 0.6 : 1,
        cursor: pending ? 'wait' : 'pointer',
      }}
      title={pending ? 'Updating…' : enabled ? 'Enabled (click to disable)' : 'Disabled (click to enable)'}
    >
      <span style={{ ...toggleKnobStyle, left: knobOffset }} />
    </button>
  );
}

// --- Styles ---

const containerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  height: '100vh',
  background: theme.colors.background,
  fontFamily: theme.typography.fontFamily,
};

const topBarStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  padding: '12px 20px',
  borderBottom: `1px solid ${theme.colors.border}`,
  background: theme.colors.surface,
  flexShrink: 0,
};

const bodyStyle: React.CSSProperties = {
  flex: 1,
  padding: 24,
  overflowY: 'auto',
  maxWidth: 1100,
  width: '100%',
  alignSelf: 'center',
  boxSizing: 'border-box',
};

const titleStyle: React.CSSProperties = {
  margin: '0 0 4px',
  fontSize: theme.typography.sizes.pageTitle,
  color: theme.colors.textPrimary,
};

const subtitleStyle: React.CSSProperties = {
  margin: '0 0 20px',
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
};

const codeStyle: React.CSSProperties = {
  fontFamily: theme.typography.fontMono,
  fontSize: 'var(--fs-2xs)',
  background: theme.colors.background,
  padding: '1px 4px',
  borderRadius: theme.radius.badge,
};

const errorStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  padding: '8px 12px',
  marginBottom: 16,
  border: `1px solid ${theme.colors.error}`,
  borderRadius: theme.radius.button,
  background: tint(theme.colors.error),
  color: theme.colors.error,
  fontSize: 'var(--fs-xs)',
};

const loadingStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
  fontStyle: 'italic',
  padding: '24px 0',
};

const emptyStateStyle: React.CSSProperties = {
  padding: 32,
  border: `1px dashed ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  textAlign: 'center',
  color: theme.colors.textSecondary,
  fontSize: 'var(--fs-xs)',
};

// Exported for the unit test rather than for reuse. These three carry layout
// contracts that broke silently once (a clipped table with no scrollbar, and
// Refresh rendered underneath the modal's ✕), are invisible to a type check, and
// this module deliberately has no jsdom render to catch them — so the properties
// themselves are what gets asserted.
export const tableWrapperStyle: React.CSSProperties = {
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  // Was `overflow: 'hidden'`, which CLIPPED the six-column table instead of
  // letting it scroll — the Actions column was simply unreachable in the
  // Settings modal, whose pane is ~864px against a ~904px min-content table.
  //
  // Two things were needed, and either alone leaves it broken:
  //   * `minWidth: 0` — this is a flex item (its parent is a column flex
  //     container), and a flex item's `min-width: auto` floor is its min-content
  //     width. So the wrapper REFUSED to shrink below the table and pushed past
  //     the pane, which then clipped it with its own `overflow-x: hidden`.
  //   * `overflowX: 'auto'` — gives the table its own scrollbar once it does not
  //     fit, which is the behaviour a wide table should have had all along.
  //
  // `overflowY: 'hidden'` keeps the rounded corners clipped on the other axis
  // (the reason `hidden` was here) without suppressing the horizontal scroll.
  minWidth: 0,
  overflowX: 'auto',
  overflowY: 'hidden',
  background: theme.colors.surface,
};

export const tableStyle: React.CSSProperties = {
  width: '100%',
  // Floor, not a fixed width: `width: 100%` alone lets the browser crush URL and
  // Actions into unreadable slivers before it ever overflows. Kept just above the
  // test's 700 guard and below the Settings pane (~840px) so the table now FITS
  // that pane (no horizontal scroll, Actions reachable) once the URL column is
  // capped and the row actions are `sm`; a narrower host still scrolls rather than
  // crushing the columns.
  minWidth: 760,
  borderCollapse: 'collapse',
  fontSize: 'var(--fs-xs)',
};

const thStyle: React.CSSProperties = {
  textAlign: 'left',
  padding: '9px 10px',
  borderBottom: `1px solid ${theme.colors.border}`,
  fontSize: 'var(--fs-2xs)',
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textSecondary,
  textTransform: 'uppercase',
  letterSpacing: 0.5,
  background: theme.colors.background,
};

const tdStyle: React.CSSProperties = {
  padding: '9px 10px',
  borderBottom: `1px solid ${theme.colors.border}`,
  color: theme.colors.textPrimary,
  verticalAlign: 'middle',
};

const nameCellStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
  minWidth: 0,
};

const nameStyle: React.CSSProperties = {
  fontWeight: theme.typography.weights.medium,
};

const idStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.textSecondary,
  fontFamily: theme.typography.fontMono,
};

const urlStyle: React.CSSProperties = {
  fontFamily: theme.typography.fontMono,
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textPrimary,
  display: 'inline-block',
  // Tight ceiling so the six-column table fits the Settings pane without a
  // horizontal scrollbar. The full URL stays reachable via the `title` tooltip
  // (and Edit), so truncating the (long, opaque) gateway host costs nothing.
  maxWidth: 200,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  verticalAlign: 'middle',
};

const mutedStyle: React.CSSProperties = {
  color: theme.colors.textSecondary,
  fontStyle: 'italic',
};

const successPillStyle: React.CSSProperties = {
  display: 'inline-block',
  padding: '2px 8px',
  fontSize: 'var(--fs-2xs)',
  fontWeight: theme.typography.weights.medium,
  color: theme.colors.success,
  background: 'transparent',
  border: `1px solid ${theme.colors.success}`,
  borderRadius: theme.radius.badge,
  whiteSpace: 'nowrap',
};

const errorPillStyle: React.CSSProperties = {
  display: 'inline-block',
  padding: '2px 8px',
  fontSize: 'var(--fs-2xs)',
  fontWeight: theme.typography.weights.medium,
  color: theme.colors.error,
  background: 'transparent',
  border: `1px solid ${theme.colors.error}`,
  borderRadius: theme.radius.badge,
  maxWidth: 280,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const actionsCellStyle: React.CSSProperties = {
  display: 'flex',
  gap: 6,
  alignItems: 'center',
};

const toggleButtonStyle: React.CSSProperties = {
  position: 'relative',
  display: 'inline-block',
  width: 36,
  height: 18,
  borderRadius: theme.radius.panel,
  border: 'none',
  padding: 0,
  transition: 'background 200ms ease',
};

const toggleKnobStyle: React.CSSProperties = {
  position: 'absolute',
  top: 2,
  width: 14,
  height: 14,
  borderRadius: '50%',
  background: theme.colors.surface,
  boxShadow: theme.elevation.subtle,
  transition: 'left 200ms ease',
};

const forbiddenStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 12,
  alignItems: 'center',
  justifyContent: 'center',
  height: '100vh',
  padding: 24,
  fontFamily: theme.typography.fontFamily,
  background: theme.colors.background,
  color: theme.colors.textPrimary,
};

// Embedded variants — used when the view is hosted inside another shell
// (e.g. SettingsView's content pane). They drop the 100vh height so the
// host's own scroll/layout boundaries take over, and tighten the inner
// padding since the host already supplies its own.
const embeddedContainerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 16,
  fontFamily: theme.typography.fontFamily,
  color: theme.colors.textPrimary,
};

export const embeddedHeaderActionsStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'flex-end',
  gap: 8,
  // The host modal's ✕ is absolutely positioned at its top-right and floats over
  // this pane, so right-aligned actions land underneath it — Refresh rendered
  // directly beneath the ✕, reading as one control on top of another. Reserve the
  // button's footprint. Only the embedded variant needs this; standalone has no
  // host chrome above it.
  paddingRight: SETTINGS_CLOSE_BUTTON_GUTTER,
};

const embeddedForbiddenStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 12,
  alignItems: 'center',
  justifyContent: 'center',
  padding: '48px 24px',
  fontFamily: theme.typography.fontFamily,
  color: theme.colors.textPrimary,
};

const forbiddenTitleStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xl)',
  fontWeight: theme.typography.weights.semibold,
};

const forbiddenBodyStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  color: theme.colors.textSecondary,
  textAlign: 'center',
  maxWidth: 480,
  lineHeight: 1.5,
};
