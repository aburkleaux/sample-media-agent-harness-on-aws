/**
 * @vitest-environment jsdom
 *
 * Two rules `improveChrome` keeps for the whole surface: which alarm source answered a platform
 * badge, and how every share on it is rounded.
 *
 * Its own file, because the surface's other chrome is exercised through the screens that mount it
 * and `EvidenceQualityBlock`'s own home is `ImproveCenter.test.tsx`. What is asserted here is not a
 * screen's behaviour but rules that have to hold the same way for every caller — a three-way absence
 * inside one badge, and a share rule that two components each used to keep a copy of.
 *
 * `vitest.setup.ts` only stubs `fetch`, so the jsdom pragma above and the jest-dom import below are
 * both required.
 */
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { EvidenceQualityBlock, sharePercent, shareSplit } from './improveChrome';
import type { EvidenceQuality } from '../../improve-api';

/**
 * One suggestion's evidence block as the producer builds it, with the platform half real.
 *
 * `harvested-alarm-history` is the live value — `HarvestedAlarmSource.SOURCE`, the snapshot path the
 * deployed producer takes. The sibling `cloudwatch-describe-alarm-history` is the only other value
 * `infra_health_for_window` can put here, and the difference between them is the whole point of the
 * line under test: one may predate the run, the other cannot.
 */
const QUALITY: EvidenceQuality = {
  runCount: 1,
  window: 'UNKNOWN',
  derivedWindows: 0,
  platform: {
    state: 'healthy',
    reason: 'no alarm transitioned into ALARM during this window',
    alarms: [],
    source: 'harvested-alarm-history',
  },
  caveats: [],
};

describe('which alarm source answered', () => {
  it('names the source beside the badge, because the badge cannot say on whose authority', () => {
    /*
     * `platform healthy` off a harvested snapshot and off a live read are different strengths of the
     * same word, and the badge renders identically for both. This is the only thing on the wire that
     * separates them, and until it rendered a reader had no way to ask.
     */
    render(<EvidenceQualityBlock quality={QUALITY} />);

    expect(screen.getByTestId('platform-source').textContent).toBe('via harvested-alarm-history');
    // The badge is still there and still says the state: the source qualifies it, never replaces it.
    expect(screen.getByTestId('platform-healthy')).toBeInTheDocument();
    expect(screen.queryByTestId('platform-source-none')).toBeNull();
    expect(screen.queryByTestId('platform-source-absent')).toBeNull();
  });

  it('reads the live source through, rather than translating it into words of its own', () => {
    // A producer class name, shown as the producer spells it — the same rule the Σ map's untranslated
    // mutability values follow. Inventing a phrase per source would silently drop the next one.
    render(<EvidenceQualityBlock quality={{
      ...QUALITY,
      platform: { ...QUALITY.platform, source: 'cloudwatch-describe-alarm-history' },
    }} />);

    expect(screen.getByTestId('platform-source').textContent)
      .toBe('via cloudwatch-describe-alarm-history');
  });

  it('separates a lookup that named no source from a bundle that records none', () => {
    /*
     * THE rule this component exists to keep. `''` is the producer's measured absence — written when
     * no run window was stamped, so no interval existed to correlate against alarm history — and
     * `undefined` is an item stored before the field. Absent, empty and named are three facts, and a
     * `?? ''` here would report a bundle nobody had audited as a lookup that found no source.
     */
    const { unmount } = render(<EvidenceQualityBlock quality={{
      ...QUALITY, platform: { ...QUALITY.platform, state: 'unknown', source: '' },
    }} />);
    expect(screen.getByTestId('platform-source-none').textContent).toBe('no alarm source answered');
    expect(screen.queryByTestId('platform-source-absent')).toBeNull();
    unmount();

    // The declared type says `source: string`, and the runtime shape of a stored item does not — so
    // the case is constructed by DELETING the key, which is what a legacy bundle serves back.
    const { source: _absent, ...withoutSource } = QUALITY.platform;
    render(<EvidenceQualityBlock quality={{
      ...QUALITY, platform: withoutSource as EvidenceQuality['platform'],
    }} />);
    expect(screen.getByTestId('platform-source-absent').textContent)
      .toBe('no alarm source recorded on this bundle');
    expect(screen.queryByTestId('platform-source-none')).toBeNull();
  });
});

/**
 * `0%` and `100%` reserved for exactly none and exactly all.
 *
 * The boundaries asserted here and nowhere else, because the rule is about honesty rather than about
 * formatting and a second copy of it is a second chance to print a figure that says the opposite of
 * what happened. The magnitude that forced it is real: uncached `input` tokens are 0.0105% of the
 * harvest, and `0% uncached input` states that no uncached token was ever sent — false, and about a
 * term priced ten times the one beside it.
 *
 * `22_163 / 211_662_174` is that share, computed rather than transcribed so the numbers cannot
 * disagree with the figure taken from them.
 */
describe('a share that rounds to nothing, and one that rounds to everything', () => {
  /** Uncached input over the harvest's total tokens: 0.0105%, the thinnest real share on the wire. */
  const THINNEST = 22_163 / 211_662_174;

  it('says a share exists rather than printing 0% for it', () => {
    expect(sharePercent(THINNEST)).toBe('<1%');
    // The complement is the other half of the same rule, and the reason a pair caller must not
    // compute `100 - pct` for itself: naively it reads `100%`, which is exactly as false.
    expect(shareSplit(THINNEST)).toEqual(['<1%', '>99%']);
  });

  it('says a share falls short rather than printing 100% for it', () => {
    expect(sharePercent(249 / 250)).toBe('>99%');
    expect(shareSplit(249 / 250)).toEqual(['>99%', '<1%']);
  });

  it('keeps 0% and 100% for the two inputs entitled to them', () => {
    // The reservation cuts both ways: withholding these from exactly none and exactly all would be
    // its own lie, and a measured `0` is a reading rather than an absence.
    expect(shareSplit(0)).toEqual(['0%', '100%']);
    expect(shareSplit(1)).toEqual(['100%', '0%']);
    expect(sharePercent(0)).toBe('0%');
    expect(sharePercent(1)).toBe('100%');
  });

  it('makes the two sides add up to 100', () => {
    // A rounding-boundary probe rather than a real magnitude: rounding the complement independently
    // prints `1% / 100%` here, and a reader who adds them up is entitled to distrust both.
    expect(shareSplit(0.005)).toEqual(['1%', '99%']);
    expect(shareSplit(0.983)).toEqual(['98%', '2%']);
  });

  it('serves the one-sided caller with the pair leading side and not a second rule', () => {
    for (const ratio of [0, THINNEST, 0.005, 0.5, 0.983, 249 / 250, 1]) {
      expect(sharePercent(ratio), `ratio ${ratio}`).toBe(shareSplit(ratio)[0]);
    }
  });

  it('lets a figure that cannot be a share through as the figure it is', () => {
    // Not clamped and not suppressed: what to DO about a producer bug differs by caller — a bar
    // withholds its fill, a rate tile refuses the figure — so the real number stays readable and
    // the decision stays with them.
    expect(sharePercent(1.4)).toBe('140%');
    expect(sharePercent(-0.2)).toBe('-20%');
  });
});
