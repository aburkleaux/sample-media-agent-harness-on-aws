// AddMcpServerModal.tsx — connect a user's own remote MCP server.
//
// Conventions follow RegisterGatewayModal (the existing admin form): a
// portal-rendered overlay, fields inline rather than through a form
// abstraction this codebase does not have.
//
// The token is write-only. It lives in this component's state for exactly as
// long as one submit takes and is never read back from the server — after
// registration it is held by AgentCore Identity, which nothing in this app can
// read. That is why re-testing a saved server has to go through the gateway
// rather than reusing what was typed here.
//
// Registration is asynchronous. The gateway can take minutes to synchronise a
// new target, so this closes on submit and hands the caller a record whose
// status is usually `synchronizing`. Blocking the modal on readiness would look
// like a hang; showing "Added" and closing would be a lie.

import React, { useState, useCallback } from 'react';
import { createPortal } from 'react-dom';
import {
  testMcpServer,
  registerMcpServer,
  type McpProbeResult,
  type McpRegistration,
} from '../api';
import { theme, tint } from '../styles/tokens';
import Button from './ui/Button';

type Mode = 'remote' | 'import';

interface AddMcpServerModalProps {
  open: boolean;
  onClose: () => void;
  /** Called with the created record so the host can show it optimistically. */
  onRegistered: (record: McpRegistration) => void;
}

interface FormState {
  name: string;
  url: string;
  headerName: string;
  /**
   * The WHOLE header value, exactly as the vendor documents it — "Key abc:def",
   * "Bearer tok", or a bare key. Previously this was split into a prefix and a
   * token, which made a trailing space load-bearing and invisible: "Key" and
   * "Key " differ by one character and produce an identical-looking 401. The
   * runtime splits it on the first whitespace instead.
   */
  headerValue: string;
  description: string;
}

const EMPTY: FormState = {
  name: '',
  url: '',
  headerName: 'Authorization',
  headerValue: '',
  description: '',
};

export default function AddMcpServerModal({
  open, onClose, onRegistered,
}: AddMcpServerModalProps) {
  const [mode, setMode] = useState<Mode>('remote');
  const [form, setForm] = useState<FormState>(EMPTY);
  const [secretHidden, setSecretHidden] = useState(true);
  const [probe, setProbe] = useState<McpProbeResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [importText, setImportText] = useState('');
  const [importError, setImportError] = useState<string | null>(null);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm(f => ({ ...f, [key]: value }));
    // A probe result describes the values that were tested. Editing any of them
    // makes it stale, and a stale green tick is worse than none.
    setProbe(null);
  };

  const reset = () => {
    setForm(EMPTY);
    setProbe(null);
    setError(null);
    setImportText('');
    setImportError(null);
    setMode('remote');
  };

  const close = useCallback(() => { reset(); onClose(); }, [onClose]);

  /**
   * Parse pasted JSON into the form rather than submitting it directly, so
   * there is one validation path and the user sees what they are about to
   * create. Accepts a bare object or the `{"mcpServers": {name: {...}}}` shape
   * that MCP clients tend to export.
   */
  const applyImport = () => {
    setImportError(null);
    let parsed: any;
    try {
      parsed = JSON.parse(importText);
    } catch (err) {
      setImportError(err instanceof Error ? err.message : 'Not valid JSON');
      return;
    }
    let name = '';
    let node = parsed;
    if (parsed && typeof parsed === 'object' && parsed.mcpServers) {
      const entries = Object.entries(parsed.mcpServers as Record<string, any>);
      if (entries.length === 0) {
        setImportError('No servers found in that config');
        return;
      }
      if (entries.length > 1) {
        setImportError(
          `That config has ${entries.length} servers. Paste one at a time.`
        );
        return;
      }
      [name, node] = entries[0];
    }
    const url = node?.url ?? node?.endpoint ?? '';
    if (!url) {
      setImportError('That config has no url');
      return;
    }
    // Take the first header if one is present; the form edits it from there.
    const headers = (node?.headers ?? {}) as Record<string, string>;
    const [headerName] = Object.entries(headers)[0] ?? [];
    setForm({
      name: node?.name ?? name ?? '',
      url: String(url),
      headerName: headerName ?? 'Authorization',
      // Never carry a credential in from a pasted blob: make the user retype it
      // so they know what they are handing over.
      headerValue: '',
      description: node?.description ?? '',
    });
    setProbe(null);
    setMode('remote');
  };

  const runTest = async () => {
    setTesting(true);
    setError(null);
    setProbe(null);
    try {
      setProbe(await testMcpServer({
        url: form.url,
        header_name: form.headerName,
        header_value: form.headerValue,
      }));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Test failed');
    } finally {
      setTesting(false);
    }
  };

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const record = await registerMcpServer({
        name: form.name,
        url: form.url,
        header_name: form.headerName,
        header_value: form.headerValue,
        description: form.description,
      });
      onRegistered(record);
      close();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add this server');
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) return null;

  const canSubmit = !!form.name.trim() && !!form.url.trim() && !submitting;

  return createPortal(
    <div style={overlayStyle} role="dialog" aria-modal="true" aria-label="Add MCP server">
      <div style={panelStyle}>
        <div style={headerRowStyle}>
          <div>
            <h2 style={titleStyle}>Add MCP server</h2>
            <p style={subtitleStyle}>
              Connect a Model Context Protocol server. Only you can see and use
              its tools.
            </p>
          </div>
          <button onClick={close} style={closeBtnStyle} aria-label="Close">✕</button>
        </div>

        <div style={bodyStyle}>
          <div style={segmentedStyle} role="tablist">
            {(['remote', 'import'] as Mode[]).map(m => (
              <button
                key={m}
                role="tab"
                aria-selected={mode === m}
                onClick={() => setMode(m)}
                style={mode === m ? segmentActiveStyle : segmentStyle}
              >
                {m === 'remote' ? 'Remote' : 'Import'}
              </button>
            ))}
          </div>

          {mode === 'import' ? (
            <>
              <label style={labelStyle} htmlFor="mcp-import">Paste JSON config</label>
              <textarea
                id="mcp-import"
                value={importText}
                onChange={e => setImportText(e.target.value)}
                rows={8}
                spellCheck={false}
                placeholder={'{\n  "mcpServers": {\n    "my-server": { "url": "https://mcp.example.com/mcp" }\n  }\n}'}
                style={textareaStyle}
              />
              {importError && <div style={inlineErrorStyle}>{importError}</div>}
              <Button size="md" onClick={applyImport} disabled={!importText.trim()}>
                Fill the form from this
              </Button>
              <p style={hintStyle}>
                Imported settings open in the form so you can check them before
                anything is created. Tokens are never imported — you will be
                asked to enter one.
              </p>
            </>
          ) : (
            <>
              <label style={labelStyle} htmlFor="mcp-name">Name</label>
              <input
                id="mcp-name"
                value={form.name}
                onChange={e => set('name', e.target.value)}
                placeholder="My Notes MCP"
                style={inputStyle}
              />

              <label style={labelStyle} htmlFor="mcp-url">URL</label>
              <input
                id="mcp-url"
                value={form.url}
                onChange={e => set('url', e.target.value)}
                placeholder="https://mcp.example.com/mcp"
                style={inputStyle}
              />
              <p style={hintStyle}>
                Must be https and publicly reachable. This is usually a path like
                /mcp or /sse, not the site root.
              </p>

              <label style={labelStyle}>Auth header</label>
              <div style={headerRowFieldsStyle}>
                <input
                  value={form.headerName}
                  onChange={e => set('headerName', e.target.value)}
                  placeholder="Authorization"
                  aria-label="Header name"
                  style={{ ...inputStyle, flex: '0 0 34%' }}
                />
                <input
                  type={secretHidden ? 'password' : 'text'}
                  value={form.headerValue}
                  onChange={e => set('headerValue', e.target.value)}
                  placeholder="Key abc123:def456"
                  aria-label="Header value"
                  autoComplete="off"
                  spellCheck={false}
                  style={{ ...inputStyle, flex: 1 }}
                />
                <button
                  onClick={() => setSecretHidden(h => !h)}
                  style={lockBtnStyle}
                  aria-label={secretHidden ? 'Show header value' : 'Hide header value'}
                  title={secretHidden ? 'Show header value' : 'Hide header value'}
                >
                  {secretHidden ? '🔒' : '🔓'}
                </button>
              </div>
              <p style={hintStyle}>
                Paste the value exactly as the provider documents it — e.g.
                <code> Key abc:def</code>, <code>Bearer abc</code>, or a bare
                API key. Stored by AWS and never shown again; re-enter it to
                change it.
              </p>

              <label style={labelStyle} htmlFor="mcp-desc">Description (optional)</label>
              <input
                id="mcp-desc"
                value={form.description}
                onChange={e => set('description', e.target.value)}
                placeholder="What does this server do?"
                style={inputStyle}
              />
            </>
          )}

          {probe && (
            <div style={probe.ok ? probeOkStyle : probeFailStyle} role="status">
              {probe.ok ? (
                <>
                  <strong>
                    Connected{probe.tool_count > 0 && ` · ${probe.tool_count} tool${probe.tool_count === 1 ? '' : 's'}`}
                  </strong>
                  {probe.tools.length > 0 && (
                    <div style={probeToolsStyle}>
                      {probe.tools.slice(0, 6).join(', ')}
                      {probe.tool_count > 6 && ` +${probe.tool_count - 6} more`}
                    </div>
                  )}
                  {probe.detail && <div style={probeToolsStyle}>{probe.detail}</div>}
                </>
              ) : (
                <>
                  <strong>Couldn't connect</strong>
                  <div style={probeToolsStyle}>{probe.detail}</div>
                </>
              )}
            </div>
          )}

          {error && <div role="alert" style={errorBannerStyle}>{error}</div>}
        </div>

        <div style={footerStyle}>
          <Button variant="ghost" size="md" onClick={close}>Cancel</Button>
          <div style={{ flex: 1 }} />
          <Button
            size="md"
            onClick={runTest}
            disabled={!form.url.trim()}
            busy={testing}
            busyLabel="Testing…"
          >
            Test connection
          </Button>
          {/* Deliberately not gated on a successful test: some servers refuse an
              unauthenticated initialize from an unknown client, and blocking
              those users behind a probe that cannot pass would be worse than
              letting them register and watch the status resolve. */}
          <Button
            variant="primary"
            size="md"
            onClick={submit}
            disabled={!canSubmit}
            busy={submitting}
            busyLabel="Adding…"
          >
            Add MCP server
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

// --- Styles ---

const overlayStyle: React.CSSProperties = {
  position: 'fixed', inset: 0, background: theme.colors.overlay,
  display: 'flex', alignItems: 'center', justifyContent: 'center', // A nested dialog: it is opened from within the settings modal, so it has to
  // outrank it. Relying on DOM order here is what makes stacking fragile.
  zIndex: theme.zIndex.modal + 1,
};

const panelStyle: React.CSSProperties = {
  width: 'min(620px, calc(100vw - 32px))', maxHeight: 'calc(100vh - 64px)',
  background: theme.colors.surface, borderRadius: theme.radius.panel, display: 'flex', flexDirection: 'column',
  boxShadow: theme.elevation.elevated, overflow: 'hidden',
};

const headerRowStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'flex-start', gap: 12,
  padding: '16px 18px', borderBottom: `1px solid ${theme.colors.border}`,
};

const titleStyle: React.CSSProperties = { margin: 0, fontSize: 'var(--fs-lg)', fontWeight: 600, color: theme.colors.textPrimary };
const subtitleStyle: React.CSSProperties = { margin: '4px 0 0', fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary };

const closeBtnStyle: React.CSSProperties = {
  marginLeft: 'auto', background: 'none', border: 'none', fontSize: 'var(--fs-md)',
  cursor: 'pointer', color: theme.colors.textTertiary, padding: 4,
};

const bodyStyle: React.CSSProperties = {
  padding: '14px 18px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 2,
};

const segmentedStyle: React.CSSProperties = {
  display: 'inline-flex', gap: 4, padding: 3, background: theme.colors.surfaceSubtle,
  borderRadius: theme.radius.panel, alignSelf: 'flex-start', marginBottom: 10,
};

const segmentStyle: React.CSSProperties = {
  padding: '5px 14px', fontSize: 'var(--fs-xs)', fontWeight: 600, color: theme.colors.textTertiary,
  background: 'none', border: 'none', borderRadius: theme.radius.button, cursor: 'pointer',
};

const segmentActiveStyle: React.CSSProperties = {
  ...segmentStyle, background: theme.colors.surface, color: theme.colors.textPrimary,
  boxShadow: theme.elevation.subtle,
};

const labelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)', fontWeight: 600, color: theme.colors.textSecondary,
  textTransform: 'uppercase', letterSpacing: 0.4, marginTop: 10,
};

const inputStyle: React.CSSProperties = {
  width: '100%', padding: '7px 9px', fontSize: 'var(--fs-sm)', marginTop: 4,
  border: `1px solid ${theme.colors.borderStrong}`, borderRadius: theme.radius.button, boxSizing: 'border-box',
};

const textareaStyle: React.CSSProperties = {
  ...inputStyle, fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-xs)', resize: 'vertical',
};

const headerRowFieldsStyle: React.CSSProperties = {
  display: 'flex', gap: 6, alignItems: 'center', marginTop: 4,
};

const lockBtnStyle: React.CSSProperties = {
  flex: '0 0 auto', padding: '6px 8px', fontSize: 'var(--fs-sm)', cursor: 'pointer',
  background: theme.colors.surfaceSubtle, border: `1px solid ${theme.colors.borderStrong}`, borderRadius: theme.radius.button,
};

const hintStyle: React.CSSProperties = { margin: '5px 0 0', fontSize: 'var(--fs-2xs)', color: theme.colors.textTertiary, lineHeight: 1.4 };

const probeOkStyle: React.CSSProperties = {
  marginTop: 12, padding: '9px 11px', borderRadius: theme.radius.button, fontSize: 'var(--fs-xs)',
  background: tint(theme.colors.success), color: theme.colors.success, border: `1px solid ${tint(theme.colors.success, 40)}`,
};

const probeFailStyle: React.CSSProperties = {
  ...probeOkStyle, background: tint(theme.colors.error), color: theme.colors.error, border: `1px solid ${tint(theme.colors.error, 40)}`,
};

const probeToolsStyle: React.CSSProperties = {
  marginTop: 3, fontSize: 'var(--fs-2xs)', opacity: 0.9, wordBreak: 'break-word',
};

const inlineErrorStyle: React.CSSProperties = { marginTop: 6, fontSize: 'var(--fs-xs)', color: theme.colors.error };

const errorBannerStyle: React.CSSProperties = {
  marginTop: 12, padding: '9px 11px', borderRadius: theme.radius.button, fontSize: 'var(--fs-xs)',
  background: tint(theme.colors.error), color: theme.colors.error, border: `1px solid ${tint(theme.colors.error, 40)}`,
};

const footerStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8,
  padding: '12px 18px', borderTop: `1px solid ${theme.colors.border}`, background: theme.colors.surfaceSubtle,
};
