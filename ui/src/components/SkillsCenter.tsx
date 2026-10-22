import React, { useCallback, useEffect, useState } from 'react';
import { theme } from '../styles/tokens';
import { useLabContext } from '../LabContext';
import { deleteSkillDraft, editAsset, publishAsset } from '../lab-api';
import BuiltInSkillsBrowser from './BuiltInSkillsBrowser';
import PurgeAssetDialog, { type PurgeAssetTarget } from './PurgeAssetDialog';
import SkillDetailsView, { type SkillDetailTarget } from './SkillDetailsView';
import CapabilitySwitch from './settings/CapabilitySwitch';
import Button from './ui/Button';
import TestHarnessPanel from './TestHarnessPanel';
import { SkillsTabBody } from './settings/CapabilitiesSettings';
import {
  SkillsSurfaceHeader,
  skillsNestedStyle,
  skillsSurfaceStyle,
} from './SkillsSurfaceHeader';

/**
 * The Skills centre surface — promoted out of Settings → Capabilities.
 *
 * Skills used to be reachable only from Settings, which put an authoring
 * surface behind a gear icon while Agents (the thing skills are attached to)
 * sat in the main nav. This is the same consolidation already done for Agents,
 * and the same rule applies: things lived ONLY in the settings tab, so moving
 * meant bringing them along rather than dropping them —
 *
 *   - **Built-in skills browser**, off by default; these are read-only platform
 *     skills, not things the Owner authors.
 *   - **Skill detail view**, the read-only drill-in for both drafts and
 *     built-ins.
 *
 * The folder importer (`webkitdirectory`) came along too and then moved on: it
 * lived in this header, beside Search, as though it were a way to view the list.
 * It is not — it is the second way to CREATE a skill, so it now sits on the
 * create path in `SkillEditor`, next to the blank form it is an alternative to.
 *
 * `SkillsTabBody` is imported from `settings/CapabilitiesSettings` rather than
 * moved: it sits on ~400 lines of inventory primitives (`SectionHeader`,
 * `PublishedList`, `ListRow`, `filterByName`…) shared with the Tools and Hooks
 * bodies, and relocating those in the same change would put a large unrelated
 * diff in front of a reviewer. Extracting them into a shared `inventory` module
 * is worth doing on its own.
 */
export default function SkillsCenter({
  onOpenEditor,
  openDetail,
  onDetailOpened,
}: {
  /** Opens `CenterEditor` for a skill. `assetId: null` means create. */
  onOpenEditor: (target: { category: 'skill'; assetId: string | null }) => void;
  /**
   * A detail view to open from OUTSIDE this component — the sidebar selecting a
   * published skill, which has no draft to edit.
   *
   * A ONE-SHOT request, not state to mirror. `onDetailOpened` fires as soon as it is
   * applied so the host can drop it, and that is load-bearing: while the host held
   * the target, every later mount of this component re-applied it, so opening the
   * Skills nav — or closing a skill editor — reopened a published skill instead of
   * landing on the list.
   */
  openDetail?: SkillDetailTarget | null;
  /** Fired once `openDetail` has been applied, so the host can clear it. */
  onDetailOpened?: () => void;
}) {
  const {
    skills,
    publishedSkills,
    loadingCategories,
    categoryErrors,
    refreshCategory,
    optimisticRemoveSkill,
    setHarnessTarget,
  } = useLabContext();

  const [search, setSearch] = useState('');
  const [showBuiltIn, setShowBuiltIn] = useState(false);
  const [detailTarget, setDetailTarget] = useState<SkillDetailTarget | null>(null);
  const [purgeTarget, setPurgeTarget] = useState<PurgeAssetTarget | null>(null);
  /*
   * Session-local tombstones for purged lineages, mirroring the Agents centre.
   * Needed, not belt-and-braces: `optimisticRemoveSkill` filters the DRAFTS
   * array only and never touches `publishedSkills`, so without this the row the
   * user just deleted stays on screen until the refresh lands — and when the
   * category is Registry-served, briefly after that too.
   */
  const [purgedIds, setPurgedIds] = useState<ReadonlySet<string>>(() => new Set());
  const [harnessOpen, setHarnessOpen] = useState(false);

  useEffect(() => {
    if (!openDetail) return;
    setDetailTarget(openDetail);
    onDetailOpened?.();
  }, [openDetail, onDetailOpened]);

  const loading = !!loadingCategories['skill'];
  const categoryError = categoryErrors['skill'];

  // Tombstoned lineages are dropped before the list ever sees them.
  const visiblePublishedSkills = purgedIds.size
    ? publishedSkills.filter(s => !purgedIds.has(s.assetId))
    : publishedSkills;


  /*
   * Above the early returns below, with the other hooks. Placed after them it is
   * skipped whenever the harness or detail view renders, and React fails the next
   * render with "rendered fewer hooks than expected".
   */
  const handlePurged = useCallback(async (assetId: string) => {
    setPurgedIds(prev => new Set(prev).add(assetId));
    setPurgeTarget(null);
    // The purged lineage may be the one open in the detail view; leaving it
    // mounted would show a version that no longer exists.
    setDetailTarget(prev => (
      prev && prev.kind !== 'builtin' && prev.assetId === assetId ? null : prev
    ));
    await refreshCategory('skill');
  }, [refreshCategory]);


  /** Every branch is the header plus one child that takes the remaining height. */
  const surface = (children: React.ReactNode, regionProps?: React.HTMLAttributes<HTMLDivElement>) => (
    <div style={skillsSurfaceStyle} {...regionProps}>
      <SkillsSurfaceHeader />
      {children}
    </div>
  );

  if (harnessOpen) {
    return surface(
      <div style={skillsNestedStyle}>
        {/* The panel supplies its own Back, on the same row as Run — the wrapper used
            to add a second one above it. */}
        <TestHarnessPanel onClose={() => setHarnessOpen(false)} />
      </div>,
      { role: 'region', 'aria-label': 'Test Harness view' },
    );
  }

  // EditAsset forks the current published version into the OWNED partition.
  // Refresh first so the new working copy is in the list the editor returns to.
  const forkPublished = async (assetId: string) => {
    const workingCopy = await editAsset('skill', assetId);
    await refreshCategory('skill');
    onOpenEditor({ category: 'skill', assetId: workingCopy.assetId });
  };


  if (detailTarget) {
    return surface(
      // `SkillDetailsView` is `height: 100%` with its own scrolling body, so it
      // needs a flex child that yields the remaining height — `minHeight: 0`,
      // without which the child's content floor wins and the header is pushed off.
      <div style={skillsNestedStyle}>
        <SkillDetailsView
          target={detailTarget}
          onBack={() => setDetailTarget(null)}
          // A published version has no draft to open: routing its Edit straight
          // to the editor would read the draft-only partition, which publishing
          // emptied, and render a blank form. Fork first, exactly as the row's
          // Edit does.
          onEdit={detailTarget.kind === 'published'
            ? forkPublished
            : assetId => onOpenEditor({ category: 'skill', assetId })}
        />
      </div>,
    );
  }


  return surface(
    <>
      <div style={toolbarStyle}>
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search skills..."
          aria-label="Search skills"
          style={searchStyle}
        />
        <CapabilitySwitch checked={showBuiltIn} onChange={setShowBuiltIn} label="Built-in skills" />
      </div>

      <div style={bodyStyle} data-testid="skills-center-scroll">
        {categoryError && (
          <div role="alert" style={errorBannerStyle}>
            {categoryError}
            <button onClick={() => void refreshCategory('skill')} style={dismissBtnStyle}>
              Retry
            </button>
          </div>
        )}

        {showBuiltIn && (
          <div style={builtInWrapStyle}>
            <BuiltInSkillsBrowser
              externalSearch={search}
              onOpenSkill={name => setDetailTarget({ kind: 'builtin', name })}
            />
          </div>
        )}

        <SkillsTabBody
          createLabel="+ New skill"
          skills={skills}
          publishedSkills={visiblePublishedSkills}
          search={search}
          loading={loading}
          onView={assetId => setDetailTarget({ kind: 'draft', assetId })}
          onViewPublished={(assetId, version) => setDetailTarget({ kind: 'published', assetId, version })}
          onEdit={assetId => onOpenEditor({ category: 'skill', assetId })}
          onDelete={async assetId => {
            await deleteSkillDraft(assetId);
            optimisticRemoveSkill(assetId);
          }}
          onTest={assetId => {
            setHarnessTarget({ category: 'skill', assetId });
            setHarnessOpen(true);
          }}
          onPublish={async assetId => {
            await publishAsset('skill', assetId);
            await refreshCategory('skill');
          }}
          onForkPublished={forkPublished}
          onPurge={(assetId, name) => setPurgeTarget({ id: assetId, name })}
        />
      </div>

      <PurgeAssetDialog
        category="skill"
        agent={purgeTarget}
        onCancel={() => setPurgeTarget(null)}
        onPurged={result => handlePurged(result.assetId)}
      />
    </>,
  );
}

// --- Styles (mirroring the Agents centre in WorkspaceView) ---
//
// The header's own styles moved to `SkillsSurfaceHeader` with it — see there for
// why the bell's safe area is reserved in its right padding.



const toolbarStyle: React.CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: 10,
  alignItems: 'center',
  justifyContent: 'space-between',
  padding: '14px clamp(12px, 3vw, 24px)',
  flexShrink: 0,
};

const searchStyle: React.CSSProperties = {
  flex: '1 1 220px',
  minWidth: 0,
  padding: '7px 10px',
  fontSize: 'var(--fs-sm)',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  background: theme.colors.surface,
  color: theme.colors.textPrimary,
  fontFamily: theme.typography.fontFamily,
};

const bodyStyle: React.CSSProperties = {
  flex: 1,
  // Load-bearing: a flex child will not shrink below its content height, which
  // leaves `overflowY: auto` inert and pushes a long inventory off the pane.
  minHeight: 0,
  overflowY: 'auto',
  padding: 24,
};

const builtInWrapStyle: React.CSSProperties = {
  marginBottom: 20,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  overflow: 'hidden',
};

const errorBannerStyle: React.CSSProperties = {
  marginBottom: 12,
  padding: '8px 10px',
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.error}`,
  color: theme.colors.error,
  fontSize: 'var(--fs-xs)',
};

const dismissBtnStyle: React.CSSProperties = {
  marginLeft: 8,
  padding: '3px 8px',
  fontSize: 'var(--fs-2xs)',
  background: 'transparent',
  color: theme.colors.textSecondary,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  cursor: 'pointer',
};
