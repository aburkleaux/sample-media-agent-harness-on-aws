import React, { useCallback, useEffect, useState } from 'react';
import ConfirmDialog from './ConfirmDialog';
import {
  CapabilityApiError,
  previewPurge,
  purgeAsset,
  type PurgeableCategory,
  type PurgePreflight,
  type PurgeResult,
} from '../capability-api';
import { theme } from '../styles/tokens';

export interface PurgeAssetTarget {
  id: string;
  name: string;
}

/** Kept for the Agents call site, which named the type before skills existed. */
export type PurgeAgentTarget = PurgeAssetTarget;

/** What to call the thing in the copy. Both categories read as a plain noun. */
const NOUN: Record<PurgeableCategory, string> = {
  'specialty-agent': 'agent',
  skill: 'skill',
};

/**
 * Confirmation for the one irreversible action in the app.
 *
 * Purging destroys every immutable published version of a lineage and its S3
 * artifacts. There is no undo, so the dialog's job is to make the blast radius
 * visible *before* the user commits, and to make committing deliberate: the
 * asset's name must be typed, and Enter alone cannot do it.
 *
 * The preview is fetched from the same endpoint with `dryRun: true`, which on
 * the server shares the authorization and enumeration path with the purge
 * itself. So a preview that renders means the purge will not be refused for
 * authority or a malformed prefix — the user cannot type the phrase and then be
 * told they were never allowed.
 *
 * On failure the dialog STAYS OPEN with the error inline. A 409 after a partial
 * purge means "retry to finish", which is impossible to act on if the dialog and
 * its reference list have vanished.
 */
export default function PurgeAssetDialog({
  category,
  agent,
  onCancel,
  onPurged,
}: {
  /** Which kind of asset — selects the route and the noun in the copy. */
  category: PurgeableCategory;
  /** The lineage to purge, or `null` when the dialog is closed. */
  agent: PurgeAssetTarget | null;
  onCancel: () => void;
  onPurged: (result: PurgeResult) => void | Promise<void>;
}) {
  const noun = NOUN[category];
  const [preflight, setPreflight] = useState<PurgePreflight | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [purging, setPurging] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const assetId = agent?.id ?? null;

  useEffect(() => {
    if (!assetId) {
      setPreflight(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setPreflight(null);
    setError(null);
    setLoadingPreview(true);
    previewPurge(category, assetId)
      .then((result) => {
        if (!cancelled) setPreflight(result);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(describe(err, 'Could not load what this would delete.', noun));
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingPreview(false);
      });
    // Ignore a resolved preview for an asset the user has since switched away
    // from — otherwise a slow response could describe the wrong lineage under a
    // confirmation the user is about to type.
    return () => {
      cancelled = true;
    };
  }, [assetId, category, noun]);

  const confirm = useCallback(async () => {
    if (!assetId) return;
    setError(null);
    setPurging(true);
    try {
      await onPurged(await purgeAsset(category, assetId));
    } catch (err) {
      setError(describe(err, 'Purge failed.', noun));
    } finally {
      setPurging(false);
    }
  }, [assetId, category, noun, onPurged]);

  if (!agent) return null;

  return (
    <ConfirmDialog
      open
      title={`Delete ${agent.name} permanently?`}
      message={
        `This destroys every published version of this ${noun} and its stored ` +
        'files. It cannot be undone.'
      }
      confirmLabel="Delete permanently"
      onConfirm={() => void confirm()}
      onCancel={onCancel}
      loading={purging}
      // Nothing may be confirmed before the blast radius has actually rendered.
      confirmDisabled={loadingPreview || !preflight}
      requiredPhrase={agent.name}
      error={error ?? undefined}
      maxWidth={560}
    >
      {loadingPreview && <p style={noteStyle}>Checking what this would delete…</p>}
      {preflight && <Blast preflight={preflight} noun={noun} />}
    </ConfirmDialog>
  );
}

function Blast({ preflight, noun }: { preflight: PurgePreflight; noun: string }) {
  const { versions, scopes, workingCopyExists, externalAccessLive, references, partial } =
    preflight;
  const shared = scopes.filter((s) => s === 'group' || s === 'org');

  return (
    <div style={blastStyle}>
      <ul style={listStyle}>
        <li>
          {versions.length === 1 ? '1 published version' : `${versions.length} published versions`}
          {versions.length > 0 && ` (v${versions.join(', v')})`}
          {workingCopyExists && ', plus the unpublished working copy'}
        </li>
        {shared.length > 0 && (
          <li style={warnStyle}>
            Shared at {shared.join(' and ')} scope — it disappears for everyone
            who can see it, not just you.
          </li>
        )}
        {/* Agent-only in practice — the server reports this off an agent's
            integration config, so it never fires for a skill. */}
        {externalAccessLive && (
          <li style={warnStyle}>
            External access is live. Outside callers using this agent's invoke
            endpoint will start failing immediately.
          </li>
        )}
      </ul>

      {references.length > 0 && (
        <>
          <p style={sectionStyle}>Still referenced by:</p>
          <ul style={listStyle}>
            {references.map((ref, i) => (
              <li key={`${ref.assetId}-${ref.detail}-${i}`}>
                {ref.name} <span style={dimStyle}>({ref.status})</span> — {ref.detail}
              </li>
            ))}
          </ul>
          <p style={noteStyle}>
            Those references are not updated. They will fail to resolve.
          </p>
        </>
      )}

      {partial.length > 0 ? (
        // Never render "nothing references this" off an incomplete scan.
        <p style={warnStyle}>
          This list covers only what you can see. Not checked: {partial.join('; ')}.
        </p>
      ) : (
        references.length === 0 && (
          <p style={noteStyle}>Nothing you can see references this {noun}.</p>
        )
      )}
    </div>
  );
}

/**
 * Turn a failure into something the user can act on.
 *
 * The typed code matters more than the message here: 403 means stop, 409 means a
 * retry can finish what was started, 503 means the deployment is incomplete and
 * nothing was touched.
 */
function describe(err: unknown, fallback: string, noun: string): string {
  if (err instanceof CapabilityApiError) {
    if (err.code === 'conflict') {
      return `${err.message} (partially deleted — try again to finish)`;
    }
    if (err.code === 'authority_denied') {
      return `You are not allowed to delete every version of this ${noun}.`;
    }
    if (err.code === 'not_found') {
      return `This ${noun} no longer exists.`;
    }
    if (err.code === 'artifacts_bucket_not_configured') {
      return 'Deleting is unavailable in this environment. Nothing was changed.';
    }
    return err.message;
  }
  return err instanceof Error ? err.message : fallback;
}

// --- Styles ---

const blastStyle: React.CSSProperties = {
  margin: '0 0 16px 0',
  padding: '10px 12px',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  backgroundColor: theme.colors.surfaceSubtle,
  maxHeight: 260,
  overflowY: 'auto',
};

const listStyle: React.CSSProperties = {
  margin: 0,
  paddingLeft: 18,
  fontSize: 'var(--fs-xs)',
  lineHeight: 1.6,
  color: theme.colors.textPrimary,
};

const sectionStyle: React.CSSProperties = {
  margin: '10px 0 4px',
  fontSize: 'var(--fs-xs)',
  fontWeight: theme.typography.weights.medium,
  color: theme.colors.textPrimary,
};

const noteStyle: React.CSSProperties = {
  margin: '8px 0 0',
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
};

const warnStyle: React.CSSProperties = {
  margin: '8px 0 0',
  fontSize: 'var(--fs-xs)',
  color: theme.colors.warning,
  lineHeight: 1.5,
};

const dimStyle: React.CSSProperties = {
  color: theme.colors.textSecondary,
};
