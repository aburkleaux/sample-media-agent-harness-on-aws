/**
 * The chat composer's upload sheet (registered into `composerSlots` by `projects/upload/registerUploadUi.ts`).
 *
 * Lists the session's chosen files that are not attachment chips yet, each with its progress, Cancel, Retry,
 * and Dismiss, and carries the `Add to <Active_Project>` switch (default ON). ON: files go into the project and
 * are copied into this chat. OFF: "this chat only", the plain chat upload. Flipping the switch moves files that
 * are still transferring to the other path. A finished batch is announced through a polite live region.
 */
import React, { useCallback } from 'react';
import { theme } from '../../styles/tokens';
import CapabilitySwitch from '../settings/CapabilitySwitch';
import CloseIcon from '../icons/CloseIcon';
import { UploadRow, srOnlyStyle, useBatchAnnouncement } from '../projects/UploadFileList';
import {
  cancelAttachment,
  chatBatchSummary,
  removeAttachment,
  retryAttachment,
  useChatAttachments,
} from '../../projects/upload/chatAttachments';
import { isInProgress } from '../../projects/upload/uploadQueue';
import type { UploadSheetProps } from './composerSlots';

export const UPLOAD_SHEET_LABEL = 'Files for this chat';

function uploadingTitle(n: number): string {
  return `Uploading ${n} file${n === 1 ? '' : 's'}`;
}

export default function ChatUploadSheet({
  sessionId,
  projectId,
  projectName,
  addToProject,
  locked,
  onAddToProjectChange,
  onClose,
  onViewInProjects,
}: UploadSheetProps) {
  const { items } = useChatAttachments(sessionId);
  const pending = items.filter((it) => it.status !== 'ready');
  const busy = items.some((it) => isInProgress(it.status));
  const summarize = useCallback((batchId: string) => chatBatchSummary(sessionId, batchId), [sessionId]);
  const announcement = useBatchAnnouncement(items, summarize);
  const projectTargetId = items.find((it) => it.target.kind === 'project')?.target;
  const viewProjectId = projectTargetId && projectTargetId.kind === 'project' ? projectTargetId.projectId : null;

  return (
    <section style={sheetStyle} aria-label={UPLOAD_SHEET_LABEL} data-testid="chat-upload-sheet">
      <div style={headerStyle}>
        <span style={titleStyle}>
          {busy ? uploadingTitle(items.filter((it) => isInProgress(it.status)).length) : UPLOAD_SHEET_LABEL}
        </span>
        {projectId && projectName ? (
          <CapabilitySwitch
            checked={addToProject}
            onChange={onAddToProjectChange}
            label={`Add to ${projectName}`}
            disabled={locked}
          />
        ) : null}
        {!busy && (
          <button type="button" style={closeBtnStyle} onClick={onClose} aria-label="Close upload sheet">
            <CloseIcon size={14} />
          </button>
        )}
      </div>
      {projectId && projectName && (
        <p style={hintStyle}>
          {addToProject
            ? `Files are added to ${projectName} and attached to this chat.`
            : 'This chat only: files are attached to this chat and not added to a project.'}
          {viewProjectId && !busy && (
            <>
              {' '}
              <button type="button" style={linkStyle} onClick={() => onViewInProjects(viewProjectId)}>
                View in Projects
              </button>
            </>
          )}
        </p>
      )}
      {pending.length > 0 && (
        <ul style={listStyle} aria-label="Chosen files">
          {pending.map((it) => (
            <UploadRow
              key={it.id}
              name={it.name}
              size={it.size}
              status={it.status}
              pct={it.pct}
              error={it.error}
              rejected={it.rejected}
              registeringLabel={
                it.target.kind === 'project' && !it.reused
                  ? `Adding to ${it.target.projectName} and this chat`
                  : 'Attaching'
              }
              onCancel={() => cancelAttachment(sessionId, it.id)}
              onRetry={() => retryAttachment(sessionId, it.id)}
              onDismiss={() => removeAttachment(sessionId, it.id)}
            />
          ))}
        </ul>
      )}
      <div aria-live="polite" aria-atomic="true" style={srOnlyStyle} data-testid="chat-upload-summary">
        {announcement}
      </div>
    </section>
  );
}

const sheetStyle: React.CSSProperties = {
  marginBottom: 8,
  padding: 8,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  background: theme.colors.surfaceRaised,
  display: 'flex',
  flexDirection: 'column',
  gap: 6,
  maxHeight: 260,
  overflowY: 'auto',
};
const headerStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' };
const titleStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  fontSize: 'var(--fs-xs)',
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
};
const hintStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
};
const listStyle: React.CSSProperties = {
  listStyle: 'none',
  margin: 0,
  padding: 0,
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
};
const closeBtnStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  background: 'none',
  border: 'none',
  padding: 2,
  cursor: 'pointer',
  color: theme.colors.textSecondary,
};
const linkStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  padding: 0,
  color: theme.colors.primary,
  cursor: 'pointer',
  fontFamily: 'inherit',
  fontSize: 'inherit',
};
