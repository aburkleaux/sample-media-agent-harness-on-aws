import React, { useEffect, useId, useMemo, useState } from 'react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { getSkill } from '../api';
import { fetchBuiltInSkill } from '../capability-api';
import { readSkillDraft, readPublishedSkill } from '../lab-api';
import { theme } from '../styles/tokens';
import { BELL_SAFE_AREA } from '../utils/layout';
import Button from './ui/Button';
import { parseSkillDocument } from '../utils/skillPackage';

export type SkillDetailTarget =
  | { kind: 'draft'; assetId: string }
  /**
   * An immutable published version. `version` is carried from the list row
   * rather than read back off the response so the label always names the
   * version the user clicked; omitting it resolves the current one.
   */
  | { kind: 'published'; assetId: string; version?: number }
  | { kind: 'builtin'; name: string };

interface SkillDetailData {
  name: string;
  skillMd: string;
  supportingFiles: string[];
  sourceLabel: string;
  statusLabel: string;
}

export interface SkillDetailsViewProps {
  target: SkillDetailTarget;
  onBack: () => void;
  onEdit?: (assetId: string) => void;
}

type DetailTab = 'preview' | 'source' | 'package';

export default function SkillDetailsView({ target, onBack, onEdit }: SkillDetailsViewProps) {
  const [data, setData] = useState<SkillDetailData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<DetailTab>('preview');
  const [highlightedPath, setHighlightedPath] = useState<string | null>(null);
  const tabSetId = useId();
  const tabId = (tab: DetailTab) => `${tabSetId}-${tab}-tab`;
  const panelId = (tab: DetailTab) => `${tabSetId}-${tab}-panel`;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setActiveTab('preview');
    const request = target.kind === 'draft'
      ? readSkillDraft(target.assetId).then(skill => ({
          name: skill.name,
          skillMd: skill.skillMd,
          supportingFiles: skill.supportingFiles ?? [],
          sourceLabel: 'Your Skill',
          statusLabel: 'Draft',
        }))
      : target.kind === 'published'
      ? readPublishedSkill(target.assetId, target.version).then(skill => ({
          name: skill.name,
          skillMd: skill.skillMd,
          supportingFiles: skill.supportingFiles ?? [],
          sourceLabel: 'Your Skill',
          statusLabel: target.version !== undefined
            ? `Published v${target.version} · read only`
            : 'Published · read only',
        }))
      // Built-in: the control plane first, the runtime only if it is not a shipped skill.
      //
      // The catalog is written at deploy time, so it holds what SHIPS — a ~200ms read
      // instead of the `get_skill` runtime invoke this used to make, which paid a cold
      // start on EVERY open (that call has no client cache, so the second open was as
      // slow as the first).
      //
      // The fallback is not belt-and-braces: this same panel lists skills the runtime
      // loaded from S3 or authored in memory, and those are legitimately absent from a
      // deploy-time catalog. Without it, fixing the shipped case would break theirs.
      : fetchBuiltInSkill(target.name).then(pkg => pkg
          ? {
              name: pkg.name,
              skillMd: pkg.skillMd,
              supportingFiles: pkg.supportingFiles,
              sourceLabel: 'Built-in',
              statusLabel: 'Read only',
            }
          : getSkill(target.name).then(skill => ({
              name: skill.name,
              skillMd: skill.skillMd,
              supportingFiles: skill.supportingFiles ?? [],
              sourceLabel: skill.source === 's3' ? 'Loaded from S3' : 'Built-in',
              statusLabel: 'Read only',
            })));

    request
      .then(result => { if (!cancelled) setData(result); })
      .catch(reason => { if (!cancelled) setError(reason instanceof Error ? reason.message : 'Failed to load Skill'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [target]);

  const parsed = useMemo(() => parseSkillDocument(data?.skillMd ?? ''), [data?.skillMd]);
  const fileCount = (data?.supportingFiles.length ?? 0) + 1;

  if (loading) return <NestedFrame onBack={onBack}><div role="status" style={stateStyle}>Loading Skill…</div></NestedFrame>;
  if (error || !data) return <NestedFrame onBack={onBack}><div role="alert" style={{ ...stateStyle, color: theme.colors.error }}>{error ?? 'Skill not found'}</div></NestedFrame>;

  const openPackagePath = (path: string) => {
    setHighlightedPath(path);
    setActiveTab('package');
  };

  return (
    <NestedFrame
      onBack={onBack}
      title={data.name}
      description={parsed.description || 'No description provided.'}
      /* A published version is immutable, so its Edit forks a fresh working
         copy — the same contract as the published-agent detail view. The caller
         decides which of the two it does; the label says which. */
      actions={target.kind !== 'builtin' && onEdit && (
        <Button variant="primary" size="md" onClick={() => onEdit(target.assetId)}>
          {target.kind === 'published' ? 'Edit' : 'Edit SKILL.md'}
        </Button>
      )}
    >
      <div style={metadataRowStyle} aria-label="Skill metadata">
        <span>{data.sourceLabel}</span>
        <span aria-hidden="true">·</span>
        <span>{data.statusLabel}</span>
        <span aria-hidden="true">·</span>
        <span>{fileCount} {fileCount === 1 ? 'file' : 'files'}</span>
        {parsed.allowedTools.length > 0 && <><span aria-hidden="true">·</span><span>{parsed.allowedTools.length} allowed {parsed.allowedTools.length === 1 ? 'tool' : 'tools'}</span></>}
      </div>

      <div role="tablist" aria-label="Skill detail" style={tabsStyle}>
        {([
          ['preview', 'Preview'],
          ['source', 'Source'],
          ['package', `Package (${fileCount})`],
        ] as Array<[DetailTab, string]>).map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            id={tabId(key)}
            aria-controls={panelId(key)}
            aria-selected={activeTab === key}
            onClick={() => setActiveTab(key)}
            style={{ ...tabStyle, ...(activeTab === key ? activeTabStyle : {}) }}
          >
            {label}
          </button>
        ))}
      </div>

      {activeTab === 'preview' && (
        <section role="tabpanel" id={panelId('preview')} aria-labelledby={tabId('preview')} aria-label="Rendered SKILL.md" style={contentSurfaceStyle}>
          {parsed.errors.length > 0 && (
            <div role="alert" style={warningStyle}>
              This SKILL.md has validation issues. Source is shown as safely as possible.
            </div>
          )}
          <SkillMarkdownBody content={parsed.body} onOpenPackagePath={openPackagePath} />
        </section>
      )}

      {activeTab === 'source' && (
        <section role="tabpanel" id={panelId('source')} aria-labelledby={tabId('source')} aria-label="SKILL.md source" style={contentSurfaceStyle}>
          <pre style={sourceStyle}><code>{data.skillMd}</code></pre>
        </section>
      )}

      {activeTab === 'package' && (
        <section role="tabpanel" id={panelId('package')} aria-labelledby={tabId('package')} aria-label="Skill package files" style={contentSurfaceStyle}>
          <SkillPackageFileList files={data.supportingFiles} highlightedPath={highlightedPath} onSelectSkillMd={() => setActiveTab('preview')} />
        </section>
      )}
    </NestedFrame>
  );
}

/**
 * Page shell for the Skill detail view.
 *
 * Deliberately the same chrome as the Skills inventory and the published-agent
 * view: a padded header band with the title and description on the left and the
 * actions grouped on the right, then a scrolling padded body. It previously
 * rendered bare against the pane — no header band, no page padding, and Back as
 * a bare text link above the title — which is what made this screen read as a
 * different, plainer app than everything around it.
 *
 * Back sits with Edit rather than above the title, and in that order, matching
 * the published-agent view's `[Back] [Edit]` pair.
 */
function NestedFrame({ onBack, title, description, actions, children }: {
  onBack: () => void;
  title?: string;
  description?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div role="region" aria-label="Skill detail view" style={pageStyle}>
      <div style={headerStyle}>
        <div style={{ minWidth: 0 }}>
          <h2 style={titleStyle}>{title ?? 'Skill'}</h2>
          {description && <p style={descriptionStyle}>{description}</p>}
        </div>
        <div style={headerActionsStyle}>
          <Button autoFocus variant="secondary" size="md" onClick={onBack}>Back</Button>
          {actions}
        </div>
      </div>
      <div style={bodyStyle} data-testid="skill-detail-scroll">{children}</div>
    </div>
  );
}

export function SkillMarkdownBody({ content, onOpenPackagePath }: { content: string; onOpenPackagePath: (path: string) => void }) {
  return (
    <article style={markdownStyle}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={(url) => defaultUrlTransform(url)}
        components={{
          h1: ({ children }) => <h1 style={h1Style}>{children}</h1>,
          h2: ({ children }) => <h2 style={h2Style}>{children}</h2>,
          h3: ({ children }) => <h3 style={h3Style}>{children}</h3>,
          p: ({ children }) => <p style={paragraphStyle}>{children}</p>,
          ul: ({ children }) => <ul style={listStyle}>{children}</ul>,
          ol: ({ children }) => <ol style={listStyle}>{children}</ol>,
          li: ({ children }) => <li style={listItemStyle}>{children}</li>,
          blockquote: ({ children }) => <blockquote style={blockquoteStyle}>{children}</blockquote>,
          table: ({ children }) => <div style={tableScrollerStyle}><table style={tableStyle}>{children}</table></div>,
          th: ({ children }) => <th style={tableHeaderStyle}>{children}</th>,
          td: ({ children }) => <td style={tableCellStyle}>{children}</td>,
          pre: ({ children }) => <pre style={codeBlockStyle}>{children}</pre>,
          code: ({ children }) => <code style={inlineCodeStyle}>{children}</code>,
          hr: () => <hr style={hrStyle} />,
          img: ({ alt }) => <span style={imageNoticeStyle}>Image omitted{alt ? `: ${alt}` : ''}</span>,
          a: ({ href = '', children }) => {
            const isPackagePath = /^(references|scripts|assets)\//.test(href);
            return (
              <a
                href={isPackagePath ? '#package' : href}
                target={isPackagePath ? undefined : '_blank'}
                rel={isPackagePath ? undefined : 'noopener noreferrer'}
                onClick={isPackagePath ? event => { event.preventDefault(); onOpenPackagePath(href); } : undefined}
                style={linkStyle}
              >
                {children}
              </a>
            );
          },
        }}
      >
        {content || '_No Skill instructions provided._'}
      </ReactMarkdown>
    </article>
  );
}

export function SkillPackageFileList({ files, highlightedPath, onSelectSkillMd }: { files: string[]; highlightedPath: string | null; onSelectSkillMd: () => void }) {
  const groups = new Map<string, string[]>();
  for (const path of [...files].sort((a, b) => a.localeCompare(b))) {
    const group = path.includes('/') ? `${path.split('/')[0]}/` : 'Other';
    groups.set(group, [...(groups.get(group) ?? []), path]);
  }

  return (
    <div style={packageListStyle}>
      <button onClick={onSelectSkillMd} style={rootFileStyle}>
        <span style={fileIconStyle}>▣</span>
        <span><strong>SKILL.md</strong><small style={fileHintStyle}>Required · rendered in Preview</small></span>
      </button>
      {[...groups.entries()].map(([group, paths]) => (
        <div key={group} style={fileGroupStyle}>
          <div style={fileGroupTitleStyle}>{group}<span style={fileCountStyle}>{paths.length}</span></div>
          {paths.map(path => (
            <div key={path} style={{ ...filePathRowStyle, ...(highlightedPath === path ? highlightedFileStyle : {}) }}>
              <span style={fileIconStyle}>—</span><code style={filePathCodeStyle}>{path}</code>
            </div>
          ))}
        </div>
      ))}
      {files.length === 0 && <div style={emptyPackageStyle}>This Skill contains only SKILL.md.</div>}
    </div>
  );
}

const pageStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  height: '100%',
  color: theme.colors.textPrimary,
};
// Same values as SkillsCenter's `headerStyle` — including the 52px right inset
// that keeps actions clear of the floating notification bell — so the inventory
// and this view line up when you move between them.
const headerStyle: React.CSSProperties = {
  padding: `20px ${BELL_SAFE_AREA}px 12px 24px`,
  display: 'flex',
  alignItems: 'flex-start',
  justifyContent: 'space-between',
  gap: 16,
  flexShrink: 0,
};
const headerActionsStyle: React.CSSProperties = {
  display: 'flex',
  gap: 8,
  alignItems: 'center',
  flexShrink: 0,
};
const bodyStyle: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  overflowY: 'auto',
  padding: 24,
  display: 'flex',
  flexDirection: 'column',
  gap: 15,
};
const titleStyle: React.CSSProperties = { margin: 0, fontSize: 'var(--fs-xl)', fontWeight: 600, color: theme.colors.textPrimary, overflowWrap: 'anywhere' };
const descriptionStyle: React.CSSProperties = { margin: '5px 0 0', maxWidth: 720, color: theme.colors.textSecondary, fontSize: 'var(--fs-sm)', lineHeight: 1.5 };
const metadataRowStyle: React.CSSProperties = { flexShrink: 0, display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 7, color: theme.colors.textSecondary, fontSize: 'var(--fs-2xs)' };
// Chrome inside the scrolling body: never compress these to make room.
const tabsStyle: React.CSSProperties = { flexShrink: 0, display: 'flex', gap: 0, borderBottom: `1px solid ${theme.colors.border}` };
const tabStyle: React.CSSProperties = { padding: '8px 13px', border: 'none', borderBottom: '2px solid transparent', background: 'none', color: theme.colors.textSecondary, fontSize: 'var(--fs-xs)', cursor: 'pointer' };
const activeTabStyle: React.CSSProperties = { color: theme.colors.primary, borderBottomColor: theme.colors.primary, fontWeight: 600 };
// `flexShrink: 0` is load-bearing, and subtle. The body is a flex column, and a
// flex item whose `overflow` is not `visible` gets an automatic minimum size of
// ZERO — so this box shrank to the body's height and CLIPPED a long SKILL.md
// rather than growing and letting the body scroll. Short skills fit and hid it.
const contentSurfaceStyle: React.CSSProperties = { flexShrink: 0, minHeight: 240, padding: '8px 2px 20px', overflow: 'hidden' };
const stateStyle: React.CSSProperties = { padding: 28, textAlign: 'center', color: theme.colors.textSecondary, fontSize: 'var(--fs-sm)' };
const warningStyle: React.CSSProperties = { marginBottom: 14, padding: '8px 10px', border: `1px solid ${theme.colors.warning}`, borderRadius: theme.radius.button, color: theme.colors.warning, fontSize: 'var(--fs-2xs)' };
const markdownStyle: React.CSSProperties = { maxWidth: 780, margin: '0 auto', color: theme.colors.textPrimary, fontSize: 'var(--fs-doc-body)', lineHeight: 1.65, overflowWrap: 'anywhere' };
const h1Style: React.CSSProperties = { margin: '12px 0 18px', fontSize: 'var(--fs-doc-h1)', lineHeight: 1.25, fontWeight: 650, letterSpacing: '-0.01em' };
const h2Style: React.CSSProperties = { margin: '28px 0 10px', paddingBottom: 6, borderBottom: `1px solid ${theme.colors.border}`, fontSize: 'var(--fs-doc-h2)', lineHeight: 1.3, fontWeight: 600 };
const h3Style: React.CSSProperties = { margin: '22px 0 8px', fontSize: 'var(--fs-doc-h3)', lineHeight: 1.35, fontWeight: 600 };
const paragraphStyle: React.CSSProperties = { margin: '9px 0' };
const listStyle: React.CSSProperties = { margin: '8px 0', paddingLeft: 23 };
const listItemStyle: React.CSSProperties = { margin: '3px 0' };
const blockquoteStyle: React.CSSProperties = { margin: '14px 0', padding: '4px 12px', borderLeft: `3px solid ${theme.colors.primary}`, color: theme.colors.textSecondary, background: theme.colors.surfaceSubtle };
const tableScrollerStyle: React.CSSProperties = { maxWidth: '100%', overflowX: 'auto', margin: '14px 0', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.button };
const tableStyle: React.CSSProperties = { width: '100%', borderCollapse: 'collapse', fontSize: 'var(--fs-doc-table)' };
const tableHeaderStyle: React.CSSProperties = { padding: '8px 10px', textAlign: 'left', background: theme.colors.surfaceSubtle, borderBottom: `1px solid ${theme.colors.border}`, fontWeight: 600 };
const tableCellStyle: React.CSSProperties = { padding: '8px 10px', borderBottom: `1px solid ${theme.colors.border}`, verticalAlign: 'top' };
const codeBlockStyle: React.CSSProperties = { margin: '13px 0', padding: 13, overflowX: 'auto', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.button, background: theme.colors.background, fontFamily: theme.typography.fontMono, fontSize: 'var(--fs-xs)', lineHeight: 1.55 };
const inlineCodeStyle: React.CSSProperties = { padding: '1px 4px', borderRadius: theme.radius.badge, background: theme.colors.surfaceSubtle, fontFamily: theme.typography.fontMono, fontSize: '0.92em' };
const hrStyle: React.CSSProperties = { margin: '22px 0', border: 'none', borderTop: `1px solid ${theme.colors.border}` };
const linkStyle: React.CSSProperties = { color: theme.colors.primary, textDecoration: 'none' };
const imageNoticeStyle: React.CSSProperties = { display: 'inline-block', padding: '5px 8px', border: `1px dashed ${theme.colors.border}`, borderRadius: theme.radius.button, color: theme.colors.textSecondary, fontSize: 'var(--fs-2xs)' };
const sourceStyle: React.CSSProperties = { maxWidth: '100%', margin: 0, padding: 14, overflow: 'auto', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, background: theme.colors.background, color: theme.colors.textPrimary, fontFamily: theme.typography.fontMono, fontSize: 'var(--fs-xs)', lineHeight: 1.55, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' };
const packageListStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 780, margin: '0 auto' };
const rootFileStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 9, width: '100%', padding: '9px 10px', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.button, background: theme.colors.surfaceSubtle, color: theme.colors.textPrimary, textAlign: 'left', fontSize: 'var(--fs-xs)', cursor: 'pointer' };
const fileHintStyle: React.CSSProperties = { display: 'block', marginTop: 2, color: theme.colors.textSecondary, fontSize: 'var(--fs-3xs)', fontWeight: 400 };
const fileIconStyle: React.CSSProperties = { width: 14, flexShrink: 0, color: theme.colors.primary };
const fileGroupStyle: React.CSSProperties = { marginLeft: 7, paddingLeft: 15, borderLeft: `1px solid ${theme.colors.border}` };
const fileGroupTitleStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 7, padding: '5px 0', color: theme.colors.textPrimary, fontSize: 'var(--fs-xs)', fontWeight: 600 };
const fileCountStyle: React.CSSProperties = { color: theme.colors.textSecondary, fontSize: 'var(--fs-3xs)', fontWeight: 400 };
const filePathRowStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 7, padding: '6px 8px', borderTop: `1px solid ${theme.colors.border}` };
const highlightedFileStyle: React.CSSProperties = { background: theme.colors.activeBg };
const filePathCodeStyle: React.CSSProperties = { minWidth: 0, color: theme.colors.textPrimary, fontFamily: theme.typography.fontMono, fontSize: 'var(--fs-2xs)', overflowWrap: 'anywhere' };
const emptyPackageStyle: React.CSSProperties = { padding: '12px 4px', color: theme.colors.textSecondary, fontSize: 'var(--fs-xs)' };
