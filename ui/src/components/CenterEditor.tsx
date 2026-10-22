// CenterEditor.tsx — Routes to the appropriate editor based on category.
// Renders in the center column when a draft is being edited.

import React from 'react';
import { theme } from '../styles/tokens';
import type { DraftCategory } from '../lab-api';
import SkillEditor from './SkillEditor';
import SpecialtyAgentEditor from './SpecialtyAgentEditor';
import ToolCollectionEditor from './ToolCollectionEditor';
import WorkflowEditor from './WorkflowEditor';

interface CenterEditorProps {
  category: DraftCategory;
  assetId: string | null;  // null = create new
  onClose: () => void;
  backLabel?: string;
}

/**
 * Title for the frame. Owned HERE rather than by each editor, because this
 * component is what supplies the surrounding chrome — see the note on
 * `showHeader` below.
 */
const TITLES: Record<DraftCategory, { create: string; edit: string }> = {
  'skill': { create: 'New Skill', edit: 'Edit SKILL.md' },
  'specialty-agent': { create: 'New Agent', edit: 'Edit Agent' },
  'tool-collection': { create: 'New Tool Collection', edit: 'Edit Tool Collection' },
  'workflow': { create: 'New Workflow', edit: 'Edit Workflow' },
  'hook': { create: 'New Hook', edit: 'Edit Hook' },
};

export default function CenterEditor({ category, assetId, onClose, backLabel = 'Back to Chat' }: CenterEditorProps) {
  const isCreate = assetId === null;
  const title = TITLES[category]?.[isCreate ? 'create' : 'edit'] ?? (isCreate ? 'New' : 'Edit');

  /*
   * `showHeader={false}` on every child, because this frame already provides the
   * title and the way out.
   *
   * Editors used to render their own header — a title plus a `✕` — INSIDE this
   * frame, which already had a `← Back`. Stacked with the `Cancel` in each
   * editor's own action row, that gave the user three separate ways to leave one
   * form, two of them within a few pixels of each other, and two competing
   * titles. `SkillEditor` added a fourth layer of its own (`EditorFrame`).
   *
   * The prop rather than deleting those headers outright: `SpecialtyAgentEditor`
   * is ALSO mounted standalone by `WorkspaceView` (custom-agent mode), where its
   * own header is the only exit. So header ownership has to be explicit rather
   * than assumed either way.
   */
  /*
   * Skills render their own chrome: `SkillEditor` puts a plain "← Back" and Save on
   * one toolbar, so this frame's bar would be a second back control and a second
   * title above it. It was also the redundant one — the Skills surface header
   * already says where you are, which made "← Back to Skills / Edit SKILL.md" say it
   * twice more.
   *
   * Keyed on the category because the Skills surface is the only host that opens a
   * skill editor. The other categories keep this bar: in chat mode and in Settings
   * nothing else names the surface, so there `backLabel` is carrying real
   * information rather than repeating a heading.
   */
  const editorOwnsChrome = category === 'skill';

  return (
    <div style={containerStyle}>
      {!editorOwnsChrome && (
        <div style={topBarStyle}>
          <button autoFocus onClick={onClose} style={backButtonStyle}>
            ← {backLabel}
          </button>
          <span style={titleStyle}>{title}</span>
        </div>
      )}

      {/* Editor content */}
      <div style={editorContentStyle}>
        {category === 'skill' && (
          <SkillEditor assetId={isCreate ? null : assetId} onClose={onClose} showHeader={false} />
        )}
        {category === 'specialty-agent' && (
          <SpecialtyAgentEditor assetId={isCreate ? null : assetId} onClose={onClose} showHeader={false} />
        )}
        {category === 'tool-collection' && (
          <ToolCollectionEditor editAssetId={isCreate ? null : assetId} onClose={onClose} showHeader={false} />
        )}
        {category === 'workflow' && (
          <WorkflowEditor assetId={isCreate ? undefined : assetId} onClose={onClose} showHeader={false} />
        )}
      </div>
    </div>
  );
}

// --- Styles ---

const containerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  height: '100%',
  overflow: 'hidden',
};

const topBarStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  padding: '8px 16px',
  borderBottom: `1px solid ${theme.colors.border}`,
  flexShrink: 0,
};

const backButtonStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.body,
  fontWeight: theme.typography.weights.medium,
  color: theme.colors.primary,
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  padding: '4px 0',
  flexShrink: 0,
};

const titleStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.heading,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const editorContentStyle: React.CSSProperties = {
  flex: 1,
  overflowY: 'auto',
  padding: '16px 24px',
};
