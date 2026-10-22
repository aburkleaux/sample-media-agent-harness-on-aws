/**
 * Components, the Σ map, and the permission story told honestly.
 *
 * Two levels, and they must not look alike: BUCKETS (yours / needs an engineer / view only)
 * band across the page, GROUPS (Instructions, Skills, ...) sit inside a bucket, and rows sit
 * inside a group. Rows are a grid so every field lands at the same x down the page, as a flex
 * chain they wrapped to the length of each name, which is the mechanical definition of reading
 * as a paragraph.
 *
 * Everything the user cannot change is split three ways rather than lumped, because they are
 * different claims: `fix_first` is a known defect with a recorded decision to fix it, `admin`
 * is owned by someone who could change it, and `locked` is protected OR never assessed. That
 * last conflation is the point of default-deny — 22 of the registry's 80 components have no
 * recorded mutability at all (8 of them reach `locked`, the rest are `code_change` and reach
 * "needs an engineer") — and undecided must render as un-editable rather than editable-by-omission.
 *
 * Each bucket is BOUNDED and the page says so: the producer caps a non-editable bucket at a fixed
 * number of rows (12 today), so 41 of 80 components are served and the other 39 are reachable from
 * nowhere. Nothing here fetches more, and no route exists that could.
 */
import React from 'react';
import { theme, tint, numericStyle } from '../../styles/tokens';
import type { ComponentRow, ComponentsResponse, Suggestion } from '../../improve-api';
import { SuggestionCard } from './SuggestionCard';
import {
  SentinelValue, UnavailableBadge, VersionBadge, fractionLabel, metaTextStyle, pluralise,
} from './improveChrome';
import { permissionLabel } from './verdictWording';

/**
 * The extra sentence a permission needs, keyed on the registry's value and not on the label.
 *
 * `not applicable` and `not assessed` are one word apart and opposite in meaning, so the row that
 * says the first has to say why. Keyed on the wire value because that is the fact — matching on the
 * rendered phrase would tie a tooltip to wording anyone may reword.
 */
const NOT_APPLICABLE_NOTE = 'The registry recorded that permission does not apply to this '
  + 'component — there is no stored artifact to edit, protect or publish. That is a decision, not '
  + 'a gap: a component nobody has assessed reads "not assessed".';

const PERMISSION_NOTE: Record<string, string> = {
  'N/A': NOT_APPLICABLE_NOTE,
  'n/a': NOT_APPLICABLE_NOTE,
};

/**
 * Row grid. Fixed tracks, so name / version / permission / health / suggestion align down the
 * page regardless of name length. `minmax(0,1fr)` on the name lets it truncate instead of
 * pushing the other columns out of alignment.
 */
const ROW_GRID = 'minmax(0,1fr) 52px 118px 150px 108px';

/**
 * A fraction, `1 of 3`, and not the percentage this row used to print.
 *
 * Two reasons the wire changed shape for it. The populations are single-digit, so a percentage
 * asserts precision the evidence has not got — and it arrived unrounded: `2` of `3` reached this
 * cell as `66.66666666666667` and was set verbatim. And the run count beside it is NOT the
 * denominator, because the producer scores this only over the runs whose delivery tier returned a
 * PASS or a FAIL; the fraction SHOWS the number that is, where a percentage could only have it
 * explained in the tooltip below.
 */
const DELIVERED_TITLE = 'Of the failing runs behind this component\u2019s suggestions, how many '
  + 'delivered a usable result in the session rather than only promising one. Runs whose delivery '
  + 'could not be judged are left out, so this fraction\u2019s denominator is the runs that '
  + 'could be, and not the run count beside it.';

/** Why a row can be silent about delivery without that being a zero or a fault. */
const NO_ATTRIBUTION_TITLE = 'No run in this population attributes to this component, so its '
  + 'delivery was never measured. Not a zero, and not a reading that failed \u2014 there was nothing '
  + 'to read.';

export function ImproveComponents({
  data, suggestions = [],
}: {
  data: ComponentsResponse;
  /** Suggestions, so a row can expand its own rather than navigating to the Inbox. */
  suggestions?: Suggestion[];
}) {
  const [collapsed, setCollapsed] = React.useState<Record<string, boolean>>({});
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  const toggle = (key: string) =>
    setCollapsed((prev) => ({ ...prev, [key]: !prev[key] }));

  const byTarget = React.useMemo(() => {
    const map: Record<string, Suggestion[]> = {};
    for (const suggestion of suggestions) {
      (map[suggestion.targetId] ??= []).push(suggestion);
    }
    return map;
  }, [suggestions]);

  const shown = [
    ...data.yours.flatMap((s) => s.components),
    ...data.fixFirst.components, ...data.admin.components, ...data.locked.components,
  ];
  const carrying = shown.filter((c) => c.suggestionCount > 0).length;

  // Expanding the row IS the whole of what a click does. There was a second half — a callback
  // reporting the target so a host could filter the Inbox to it — and no host ever passed one, so
  // the filter it fed was never set to anything but `null`.
  const select = (componentId: string) =>
    setExpandedId((prev) => (prev === componentId ? null : componentId));

  const renderRows = (components: ComponentRow[], editable: boolean) => components.map((c) => (
    <React.Fragment key={c.componentId}>
      <Row component={c} editable={editable} onSelect={() => select(c.componentId)} />
      {expandedId === c.componentId && (byTarget[c.componentId] ?? []).map((suggestion) => (
        <div
          key={suggestion.suggestionId}
          data-testid={`inline-suggestion-${c.componentId}`}
          style={{ margin: '4px 0 10px 16px' }}
        >
          <SuggestionCard
            suggestion={suggestion}
            expanded
            onToggle={() => setExpandedId(null)}
            onAction={() => {}}
          />
        </div>
      ))}
    </React.Fragment>
  ));

  return (
    <div>
      <StatStrip
        stats={[
          { value: String(data.registryMeta.componentCount), label: 'in the registry' },
          { value: String(shown.length), label: 'shown here' },
          { value: String(carrying), label: 'carry a suggestion' },
        ]}
        note={`revalidated ${data.registryMeta.revalidated} against `
          + `${data.registryMeta.headSha}`}
      />

      <BucketBand label="Yours" count={data.yours.reduce((n, s) => n + s.components.length, 0)} />
      {data.yours.map((section) => (
        <GroupBlock
          key={section.layer}
          label={section.label}
          count={section.components.length}
          collapsed={collapsed[section.layer]}
          onToggle={() => toggle(section.layer)}
        >
          {section.components.length === 0
            ? <Empty />
            : renderRows(section.components, true)}
        </GroupBlock>
      ))}

      <LockedBucket
        testId="section-fix-first"
        label="Needs an engineer"
        count={data.fixFirst.count}
        note={data.fixFirst.note}
        components={data.fixFirst.components}
        collapsed={collapsed.fixFirst}
        onToggle={() => toggle('fixFirst')}
        renderRows={renderRows}
        tone={theme.colors.warning}
      />
      <LockedBucket
        testId="section-admin"
        label="Managed by admins"
        count={data.admin.count}
        note={data.admin.note}
        components={data.admin.components}
        collapsed={collapsed.admin}
        onToggle={() => toggle('admin')}
        renderRows={renderRows}
        tone={theme.colors.textTertiary}
      />
      <LockedBucket
        testId="section-locked"
        label="Protected or not assessed"
        count={data.locked.count}
        note={data.locked.note}
        components={data.locked.components}
        collapsed={collapsed.locked}
        onToggle={() => toggle('locked')}
        renderRows={renderRows}
        tone={theme.colors.textTertiary}
      />
    </div>
  );
}

/** Counts as counts, not as a sentence. Three cells beat a paragraph of statistics. */
function StatStrip({
  stats, note,
}: { stats: { value: string; label: string }[]; note: string }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: 'flex', gap: 28 }}>
        {stats.map((stat) => (
          <div key={stat.label}>
            <div style={{
              ...numericStyle,
              fontSize: theme.typography.sizes.sectionTitle,
              color: theme.colors.textPrimary,
            }}>
              {stat.value}
            </div>
            <div style={{
              fontSize: theme.typography.sizes.badge, color: theme.colors.textTertiary,
            }}>
              {stat.label}
            </div>
          </div>
        ))}
      </div>
      <div style={{
        fontSize: theme.typography.sizes.badge, color: theme.colors.textTertiary, marginTop: 6,
      }}>
        <span style={numericStyle}>{note}</span>
      </div>
    </div>
  );
}

/** Top level: a band, so it cannot be mistaken for a group header or a row. */
function BucketBand({
  label, count, tone = theme.colors.textSecondary, testId,
}: { label: string; count: number; tone?: string; testId?: string }) {
  return (
    <div
      data-testid={testId}
      style={{
        display: 'flex', alignItems: 'center', gap: 8,
        // A ground rather than a rule: every row already carries a 1px line, and a fourth
        // horizontal rule made the header parse as one more row.
        background: theme.colors.surfaceSubtle,
        borderRadius: theme.radius.button,
        padding: '7px 10px',
        margin: '18px 0 2px',
        fontSize: theme.typography.sizes.meta,
        fontWeight: theme.typography.weights.semibold,
        color: tone,
        textTransform: 'uppercase',
        letterSpacing: 0.5,
      }}
    >
      <span>{label}</span>
      <span style={{ ...panelCountPill, color: tone }}>{count}</span>
    </div>
  );
}

const panelCountPill: React.CSSProperties = {
  marginLeft: 'auto',
  ...numericStyle,
  fontSize: theme.typography.sizes.badge,
  background: 'color-mix(in srgb, var(--color-text-secondary) 12%, transparent)',
  borderRadius: theme.radius.pill,
  padding: '1px 7px',
  minWidth: 20,
  textAlign: 'center',
};

/**
 * A group inside a bucket. Steps UP to `heading` (14) so it outranks its own rows (13),
 * previously the header was 11px uppercase, i.e. the data was two sizes larger than the thing
 * naming it, and uppercase at 11px reads smaller still.
 *
 * No colour dot. `panelChrome.groupColour` hashes the layer name into a palette that includes
 * success/warning/error, so `TOOL COLLECTIONS` drew a red dot and `HOOKS` a green one, identity
 * colour posing as status, beside buckets whose colour genuinely means something.
 */
function GroupBlock({
  label, count, collapsed, onToggle, children,
}: {
  label: string;
  count: number;
  collapsed?: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        style={{
          display: 'flex', alignItems: 'center', gap: 7, width: '100%',
          background: 'none', border: 'none', cursor: 'pointer', textAlign: 'left',
          padding: '10px 10px 4px',
          fontSize: theme.typography.sizes.heading,
          fontWeight: theme.typography.weights.semibold,
          color: theme.colors.textPrimary,
        }}
      >
        <span style={{
          width: 12, flexShrink: 0, fontSize: theme.typography.sizes.badge,
          color: theme.colors.textTertiary,
        }}>
          {collapsed ? '▸' : '▾'}
        </span>
        <span>{label}</span>
        <span style={panelCountPill}>{count}</span>
      </button>
      {!collapsed && (
        // One vertical line groups the rows. Horizontal ruling per row competed with the
        // header for the same visual job.
        <div style={{
          borderLeft: `1px solid ${theme.colors.border}`,
          marginLeft: 16,
          paddingLeft: 8,
        }}>
          {children}
        </div>
      )}
    </div>
  );
}

function LockedBucket({
  testId, label, count, note, components, collapsed, onToggle, renderRows, tone,
}: {
  testId: string;
  label: string;
  count: number;
  note: string;
  components: ComponentRow[];
  collapsed?: boolean;
  onToggle: () => void;
  renderRows: (components: ComponentRow[], editable: boolean) => React.ReactNode;
  tone: string;
}) {
  return (
    <div data-testid={testId}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        style={{ display: 'block', width: '100%', background: 'none', border: 'none', padding: 0, cursor: 'pointer' }}
      >
        <BucketBand label={label} count={count} tone={tone} />
      </button>
      <p style={{
        fontSize: theme.typography.sizes.badge, color: theme.colors.textTertiary,
        margin: '2px 10px 6px', maxWidth: '68ch',
      }}>
        {note}
      </p>
      {!collapsed && (
        <div style={{ borderLeft: `1px solid ${theme.colors.border}`, marginLeft: 16, paddingLeft: 8 }}>
          {renderRows(components, false)}
          {/* What the cut is, and what it is not. This used to promise that "the rest load with the
              full registry": there is no second fetch anywhere in this surface and no route to
              write one against, so the remainder is simply not served — 39 of the registry's 80
              components are unreachable from here. The ordering is the mitigation and is stated,
              because it is what makes a truncated bucket usable. */}
          {components.length < count && (
            <div
              data-testid={`${testId}-capped`}
              title={'The report carries a bounded number of rows per bucket so the whole bundle '
                + 'fits the size limit of the one item it is stored in. The bound is the '
                + 'producer’s, and the rows left out are not on the wire at all.'}
              style={{
                fontSize: theme.typography.sizes.badge, color: theme.colors.textTertiary,
                padding: '6px 10px',
              }}
            >
              showing <span style={numericStyle}>{components.length}</span> of{' '}
              <span style={numericStyle}>{count}</span>
              {' — the rest are not in this report. Rows carrying a suggestion come first, so the '
                + 'cut is not arbitrary.'}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Empty() {
  return (
    <div style={{
      fontSize: theme.typography.sizes.badge, color: theme.colors.textTertiary,
      padding: '5px 10px',
    }}>
      Nothing here yet.
    </div>
  );
}

function Row({
  component, editable, onSelect,
}: { component: ComponentRow; editable: boolean; onSelect: () => void }) {
  const hasSuggestion = component.suggestionCount > 0;
  return (
    <div
      data-testid={`component-${component.componentId}`}
      style={{
        display: 'grid',
        gridTemplateColumns: ROW_GRID,
        alignItems: 'baseline',
        columnGap: theme.spacing.unit * 2,
        padding: '5px 10px',
      }}
    >
      {hasSuggestion ? (
        <button
          type="button"
          onClick={onSelect}
          data-testid={`select-${component.componentId}`}
          style={{
            background: 'none', border: 'none', padding: 0, cursor: 'pointer',
            color: theme.colors.primary, fontSize: theme.typography.sizes.body,
            textAlign: 'left', overflow: 'hidden', textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {component.name}
        </button>
      ) : (
        <span style={{
          fontSize: theme.typography.sizes.body,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {component.name}
        </span>
      )}

      <span style={{ fontSize: theme.typography.sizes.badge }}>
        <VersionBadge version={component.currentVersion} versioned={component.versioned} />
      </span>

      <Permission mutability={component.mutability} editable={editable} />

      <span
        data-testid={`health-${component.componentId}`}
        style={{ fontSize: theme.typography.sizes.badge, color: theme.colors.textTertiary }}
      >
        {component.health ? (
          <>
            <span style={numericStyle}>{component.health.runs}</span>
            {' '}{pluralise(component.health.runs, 'run')}
            {' · '}
            {/* Label FIRST, so the sentinel below has a subject: `delivered not measured`, never
                a bare hatched badge that could be read as the run count being absent. */}
            <span title={DELIVERED_TITLE}>
              delivered{' '}
              {/* A denominator of 0 is the unmeasured state, not a measured zero — no attributing
                  run could be judged on delivery. It keeps the hatched sentinel rather than
                  `fractionLabel`'s own `nothing to measure`, because every other absent value on
                  this surface wears that badge and this one must not be the exception. */}
              {component.health.deliveredOf === 0
                ? <SentinelValue value="UNMEASURED" />
                : (
                  <span style={numericStyle}>
                    {fractionLabel(component.health.delivered, component.health.deliveredOf)}
                  </span>
                )}
            </span>
          </>
        ) : (
          /* Nothing attributes here, so nothing about delivery was ever measured. The
             `UnavailableBadge` and not the hatched sentinel above it: that one says the producer
             took a reading and came back empty-handed, this says there was never a reading to take,
             and they route a reader to different questions. A `-` said neither — and said it on all
             41 live rows, which is every row this surface serves. */
          <span title={NO_ATTRIBUTION_TITLE}><UnavailableBadge /></span>
        )}
      </span>

      <span style={{ textAlign: 'right' }}>
        {hasSuggestion && (
          <span style={{
            fontSize: theme.typography.sizes.badge,
            color: theme.colors.primary,
            background: tint(theme.colors.primary, 12),
            border: `1px solid ${tint(theme.colors.primary, 28)}`,
            borderRadius: theme.radius.pill,
            padding: '1px 7px',
            whiteSpace: 'nowrap',
          }}>
            {component.suggestionCount} {pluralise(component.suggestionCount, 'suggestion')}
          </span>
        )}
      </span>

      {/* LAST in the DOM so it forms a second grid line of its own. Placed among the cells it would
          take a column, and a full-width item cannot fit beside four filled tracks, so everything
          after it would be pushed onto a third line. */}
      <SourceLocation component={component} />
    </div>
  );
}

/**
 * Where the component lives — the answer the "needs an engineer" bucket otherwise withholds.
 *
 * 24 components live say a fix belongs to an engineer, and until now the surface named the place
 * for none of them. `SuggestionCard` already prints the same string for a suggestion
 * (`targetLocation`, plain text in tertiary), so this follows it rather than inventing a second
 * treatment for one fact; a path is not engine prose and is shown exactly as the registry wrote it.
 *
 * On its own line rather than in a sixth column, because these are not all short: the longest served
 * live is 110 characters and several name three files and a role each — 101 of them for
 * `custom_agent_resolver.py:526,568; storage lab/specialty_crud.py:626,683; read
 * task_agent.py:1913,1946`. Wrapped, not abridged: the whole point of the field is that a reader can
 * go and open it, and a path cut mid-token reads as corruption rather than as abridgement.
 *
 * The test id is `location-` and not `source-`, which is not fussiness: `source` is itself a wire
 * key (on `PlatformHealth`, rendered by `improveChrome.PlatformSource`), and
 * `scripts/improve-wire-census.py` matches NAMES — so a `source-` id here would satisfy that key's
 * check from a file that never reads it, and did so for as long as nothing rendered it.
 */
function SourceLocation({ component }: { component: ComponentRow }) {
  // Read as possibly-absent though the type declares it required: a row stored before the field
  // existed is served back unchanged. Empty and absent are not split here, and that is deliberate —
  // for a place name there is no third reading, either the registry names somewhere to look or it
  // does not, where a `0` would have been a measurement.
  const location: string | undefined = component.sourceLocation;
  const style: React.CSSProperties = {
    gridColumn: '1 / -1',
    fontSize: theme.typography.sizes.badge,
    color: theme.colors.textTertiary,
    wordBreak: 'break-word',
  };
  if (!location) {
    return (
      <span data-testid={`location-${component.componentId}`} style={style}>
        the registry names no location for this
      </span>
    );
  }
  return (
    <span data-testid={`location-${component.componentId}`} style={style}>{location}</span>
  );
}

/**
 * Permission in the reader's words, or the registry's own token when this screen has no word for it.
 *
 * The phrasing is `verdictWording`'s, shared with the suggestion card, which names the permission of
 * the same component from the same registry value — one definition, so a row and a card cannot come
 * to say a component is "locked for now" in one place and `LOCKED_TODAY` in the other.
 *
 * The fallback used to be `'not assessed'` — `UNRECORDED`'s wording — so every value off the
 * `Mutability` union was reported as the one state default-deny is ABOUT. `mutability: "N/A"` is
 * live on `dag-node-prompts` and read "not assessed" under a bucket saying admins manage it, which
 * is two claims about one row and neither is what the registry recorded. An untranslated value now
 * shows as the token it is, so it reads as untranslated rather than as a decision.
 */
function Permission({ mutability, editable }: { mutability: string; editable: boolean }) {
  const label: string | undefined = permissionLabel(mutability);
  const note: string | undefined = PERMISSION_NOTE[mutability];
  const permission = editable ? 'You can propose changes to this.'
    : 'View only. The registry has not cleared this for user edits.';
  const style: React.CSSProperties = {
    fontSize: theme.typography.sizes.badge, color: theme.colors.textTertiary,
  };
  if (label === undefined) {
    return (
      // Mono, because an untranslated value is an identifier and the surface sets identifiers in
      // mono everywhere else. It is the one thing here that is not the reader's own words.
      <span
        style={{ ...style, ...numericStyle }}
        title={`${permission} This screen has no phrase for the registry value `
          + `“${mutability}”, so it is shown as the registry spells it.`}
      >
        {mutability || 'no value recorded'}
      </span>
    );
  }
  return (
    <span style={style} title={note === undefined ? permission : `${permission} ${note}`}>
      {label}
    </span>
  );
}

export default ImproveComponents;
