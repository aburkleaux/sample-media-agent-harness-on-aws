/**
 * Wire contracts for the Improve surface.
 *
 * These mirror `agent/src/improve/contract.py`. The mapping snake_case -> camelCase
 * happens ONCE, server-side, in the Capability API's DTO layer, not here, so this file
 * stays a description of the wire rather than a second definition of the domain. When
 * the API lands these types keep their shape and only the data source changes; today they
 * are served from a generated payload that `improve-data.ts` fetches.
 *
 * Two rules this file exists to enforce:
 *
 * 1. **Sentinels are values, not absence.** A number that could not be measured arrives
 *    as the literal string `"UNMEASURED"` (or `"NO_DATA"` / `"UNKNOWN"`), never as `null`
 *    or `0`. `null` would be styled like any other empty cell and `0` reads as "instant"
 *    or "free". Anything rendering a `Measured<number>` must branch on `isSentinel()`.
 *
 * 2. **Affordances are computed server-side.** The UI never infers whether a suggestion
 *    can be published or tried, permission depends on the Σ registry's mutability
 *    matrix, which the browser does not have. It renders `affordances` and nothing else.
 */

// --- sentinels --------------------------------------------------------------
export const SENTINELS = ['NO_DATA', 'UNKNOWN', 'UNMEASURED'] as const;
export type Sentinel = (typeof SENTINELS)[number];

/** A value that may not have been measurable. Never widen this to `| null`. */
export type Measured<T> = T | Sentinel;

export function isSentinel(value: unknown): value is Sentinel {
  return typeof value === 'string' && (SENTINELS as readonly string[]).includes(value);
}

export type Outcome = 'PASS' | 'FAIL' | 'NOT_APPLICABLE' | 'NO_DATA';
export type Overall = 'PASS' | 'FAIL' | 'UNSCORED';

/**
 * Publishable targets are exactly the three versioned Lab categories, mirroring
 * `lab/_canonical_ops._VERSIONED_CATEGORIES`. `tool_collection` and `hook` are FLAT by
 * design, no published form, `promote` raises, so they can never carry a version badge
 * or a Publish button.
 */
export const VERSIONED_TARGETS = ['skill', 'specialty-agent', 'workflow'] as const;
export type VersionedTarget = (typeof VERSIONED_TARGETS)[number];
export type TargetType = VersionedTarget | 'tool_collection' | 'hook' | 'shared_prompt'
  | 'prompt' | 'memory' | 'tool' | 'control' | 'ingress' | 'infra' | 'code_change';

/** What a proposal is permitted to be, decided by the target's registry mutability. */
export type Disposition = 'publishable' | 'advisory' | 'code_change';

export interface Provenance {
  evaluatedAt: string;
  /** model + rubric + inputSet + threshold + runs. Two verdicts with different hashes
   *  are not comparable. */
  judgeConfigHash: string;
  judgeRuns: number;
  /** e.g. ["aws/spans@2026-08-13", "memory:list_events", "plan-state"] */
  evidenceSources: string[];
  /** How the failure was attributed to a Σ component. `keyword_inferred` is a hypothesis
   *  the registry does not itself assert, and the UI says so. */
  attributionBasis?: 'registry_declared' | 'keyword_inferred';
  /**
   * THE WARRANT FOR THE ACCUSATION: the registry's own declaration that justified blaming this
   * component, in the words it was written in.
   *
   * `attributionBasis: 'registry_declared'` asserts that the Σ registry declares this component fails
   * this way. Without the declaration itself that label is unfalsifiable — the live item says
   * `registry_declared` on both its suggestions and WHICH declaration matched is unrecoverable from
   * the bundle — so this is what turns the label into a claim a reader can check.
   *
   * `""` EXACTLY when the basis is `keyword_inferred`, which is not an absence but a different fact:
   * there was no declaration to quote, and the match came from the component's id, type, notes or
   * path. The card already captions that case as inferred, so an empty sentence must not be captioned
   * a second time. `registry_declared` with an empty sentence is impossible by construction upstream
   * (a declared hit requires a word-boundary match inside the component's failure modes).
   *
   * OPTIONAL, and absent is the live reading: the deployed producer predates the field, so every
   * suggestion on the wire today claims a declared basis and quotes nothing. `undefined` therefore
   * means the report withholds the warrant, which is worth saying; `''` means there was never one to
   * withhold. Do not collapse them with `?? ''`.
   *
   * CONTENT: text from `ideas/sigma-registry.yaml`, a repo document engineers wrote about the
   * harness's own components. Not user input, not model prose, and it names no session — the same
   * class of value as `targetLocation` and `mutability`, so it is prose that may be shown as written.
   */
  matchedFailureMode?: string;
  /** Other components that also matched, best-first. */
  attributionAlternatives?: string[];
  registryHeadSha?: string;
  /** Non-empty means the pattern rests on an inferred facet, not a direct measurement. */
  heuristicFacets?: string[];
}

// --- run window and platform health ----------------------------------------
/**
 * Three states, mirroring `contract.InfraState`. No boolean form on purpose: a `degraded: false`
 * default would report a throttled lookup as a clean platform. `unknown` is not a soft
 * `healthy`, render it as a caveat, never green.
 */
export type PlatformState = 'healthy' | 'degraded' | 'unknown';

/**
 * Wall-clock extent of a run, not `utility.latencyMs`, which sums tool durations. Neither
 * bounds the other: a run can sum far more tool time than its wall-clock window when nodes
 * run in parallel. Only wall clock can be lined up against an account-wide alarm.
 */
export interface RunWindow {
  start: string;
  end: string;
  /**
   * Bounds reconstructed rather than stamped by a collector. Load-bearing: spans vanish first
   * when the platform breaks, so a derived window narrows during the outage it should detect.
   */
  derived: boolean;
  /** e.g. `collector_stamped`, `derived_plan_and_span_bounds`. */
  provenance: string;
}

/**
 * Whether the platform was alarming during a run's window. Ambient, never attributed: alarm
 * state is account-wide, so it supports correlation and never per-run blame. Never a score
 * input, a FAIL stays a FAIL.
 */
export interface PlatformHealth {
  state: PlatformState;
  /** Why this state, in words. Carries the derived-window caveat when one applies. */
  reason: string;
  /** Alarm names seen firing during the window. */
  alarms: string[];
  /**
   * Alarm names the lookup could NOT place against this window — throttled, out of retention, or
   * transition history that does not reach back far enough.
   *
   * The other half of `alarms`, and without it `state` cannot be read: `unknown` with four of these
   * is a lookup that ran and could not decide, `unknown` with none is a lookup that found nothing to
   * decide about. Until the producer kept them (`platform_to_wire`) an unplaceable alarm reached a
   * reader as an alarm that was fine.
   *
   * NAMES here and a count on the population block, because the names are what a reader takes to
   * CloudWatch and this row is about one run. They are CDK-named account infrastructure — this
   * deploy's own queues, Lambdas and sweepers — so no tenant can create one and they are safe on the
   * shared item.
   *
   * OPTIONAL for this file's usual deploy-order reason, and `[]` is NOT the same fact: an empty list
   * is a measured "none could not be placed", absent is a bundle written before the field existed.
   * Branch on `undefined` rather than writing `?? []`.
   */
  alarmsIndeterminate?: string[];
  /** Which alarm source answered, e.g. `harvested-alarm-history`. */
  source: string;
}

/**
 * What the PLATFORM was doing underneath the whole scored population — `Scorecard.platform`.
 *
 * A separate interface from `PlatformHealth` above and deliberately so: that one is about ONE run's
 * window and carries alarm NAMES, this one is about the population and carries COUNTS. The producer
 * documents the asymmetry (`dto._platform_population`): a list here would grow with the account's
 * alarm roster, and the scorecard block is fixed-size precisely so its bytes never come out of the
 * suggestion list's allowance.
 *
 * The four counts PARTITION `runs` and all four are needed. `degraded: 0` on its own is
 * indistinguishable from a population nobody looked at, which is what the offline golden path is.
 */
export interface PlatformPopulation {
  /** Worst state across the population (`degraded` > `unknown` > `healthy`), never the commonest:
   *  the question is whether ANY of the evidence is compromised. An empty population is `unknown`. */
  state: PlatformState;
  /** A lookup ran and found an alarm firing over the run's window. */
  degraded: number;
  /** A lookup ran and conclusively found none. */
  healthy: number;
  /** A lookup RAN and could not tell. Not a soft `healthy`. */
  inconclusive: number;
  /** No alarm source was wired for the run at all — an ABSENT measurement rather than a failed
   *  one, and the only one of the two that configuration can fix. */
  unconsulted: number;
  /** The denominator the four above sum to. */
  runs: number;
  /** DISTINCT alarm names seen firing, unioned across the population. A count, not a list. */
  alarmsInAlarm: number;
  /**
   * DISTINCT alarm names the lookup could not place, unioned across the population.
   *
   * This is what makes the count above readable: `alarmsInAlarm: 0` beside twelve of these is not a
   * quiet platform. Render both or neither.
   */
  alarmsIndeterminate: number;
  /** WHICH alarm sources answered — class names from the producer, bounded to four. The difference
   *  between a live CloudWatch read and a harvested snapshot that may predate the run. */
  sources: string[];
}

// --- runs -------------------------------------------------------------------
export interface TierScore {
  score: Measured<number>;
  outcome: Outcome;
  /**
   * The judge's own sentence about this run.
   *
   * PER-ACTOR ONLY. The judge is fed the goal and the response and asked for one sentence
   * grading the session, so this restates the request by construction — it is the reason the
   * shared bundle can name WHICH gating check failed (`Scorecard.outcomes`) and never why.
   */
  explanation: string;
}

/**
 * One of this actor's runs, from their own bundle (`ActorBundle.runs`).
 *
 * Carries what the loop measured and nothing it did not. An earlier version of this interface
 * also described a span count, a workload family/mode, per-check results, a harness snapshot and
 * an AgentCore reference score; none of those has a source, and a row with plausible-looking
 * blanks in it is worse than a shorter row. They belong back here when something stamps them.
 */
export interface ActorRun {
  runId: string;
  /**
   * Which kind of invocation this was, e.g. `conversational`. Needed to read the verdict at all:
   * gating is per class, so without it a `NOT_APPLICABLE` tier looks like a missing measurement.
   * Always `conversational` today — the other classes have no owner (see `ActorBundle`).
   */
  workloadClass: string;
  /** Wall-clock extent of the run. `UNKNOWN` when no collector stamped one. */
  window: Measured<RunWindow>;
  /** `reason` is structural — `"gating: results_delivered:FAIL"` — not the judge's prose. */
  verdict: { overall: Overall; reason: string };
  tiers: {
    /** Routed and kicked off the right work. Calibrated against AgentCore's
     *  GoalSuccessRate, which is the lenient one. */
    dispatch: TierScore;
    /** A usable result actually arrived in-session. */
    delivery: TierScore;
  };
  /**
   * Per-run spend. `"UNMEASURED"` where the harvest recorded nothing — never `0`, which would read
   * as "free" or "instant".
   *
   * `cost` is PRICED per run, not absent: the producer values each run's token categories per model
   * from a hand-sourced Bedrock rate table. Pricing can still fail per run — a run NO model of
   * which the table can price reports `"UNMEASURED"` rather than a number, and so does a run that
   * recorded no usage at all — so a sum over these rests on fewer runs than the token term does,
   * and must be captioned with the count that actually contributed. The rates' provenance travels
   * on `Scorecard.spend.costBasis` and nowhere on this row: it is a property of the population's
   * pricing run, and the two items refresh independently.
   *
   * The four OPTIONAL terms below are what let a personal figure answer the questions
   * `Scorecard.spend` answers for the population — a cache-reuse share, a context-vs-generated
   * share, which models were used, and whether a price is short. The producer measured all four per
   * session all along (`improve/observe/build_run.usage_for` records twelve terms) and projected
   * only the first four onto this row, so every one of these was measured and then dropped.
   *
   * OPTIONAL because they are newer than the bundles, and a personal bundle is rewritten only when
   * its actor had a session in the harvest window — so a row carrying the older four keys and
   * nothing else is served for as long as that actor stays away. Absent is NOT zero and not an
   * empty split: branch on `undefined`, never `?? 0` or `?? []`, or a bundle that predates a term
   * reports a measurement nobody made.
   */
  spend: {
    latencyMs: Measured<number>;
    tokens: Measured<number>;
    cost: Measured<number>;
    llmCalls: Measured<number>;
    /**
     * This run's own tokens in the four separately-priced categories. They sum to `tokens` exactly
     * — Bedrock's own arithmetic, the same property `Scorecard.spend.byCategory` has one level up —
     * so a share drawn over them needs no residual term. `input` is UNCACHED input only.
     *
     * The reason a personal cache figure needs them: the categories are priced within an order of
     * magnitude of each other in the wrong direction (a cache READ is about a tenth of an uncached
     * input token) and cache reads are 68% of every token on the live population, so one
     * undifferentiated per-run total cannot say whether a session reused context or rebuilt it.
     */
    byCategory?: {
      input: Measured<number>;
      output: Measured<number>;
      cacheRead: Measured<number>;
      cacheWrite: Measured<number>;
    };
    /**
     * Which Bedrock model ids THIS run's tokens went to. A list, where the population carries a
     * count (`Scorecard.spend.modelsSeen`): the count is what makes a single-rate cost
     * indefensible over a population, and on one of your own runs the actionable fact is which
     * model it spent on.
     *
     * `"UNMEASURED"` and never `[]` when nothing named one — an empty list would read as "this run
     * used no model", which no measurement says. A run that spent tokens names at least one id,
     * `"UNKNOWN"` included where a usage row carried no model attribute.
     */
    models?: Measured<string[]>;
    /**
     * How many of this run's tokens the rate table could and could not price.
     *
     * Cost's second denominator, per run, and the only thing that can say a personal bill is SHORT.
     * Pricing is per model and skips a model with no rate row rather than failing the run, so a
     * fully measured run can contribute a partial price — and a sum of `cost` over runs is a silent
     * lower bound unless these say by how much. The population's counts
     * (`Scorecard.spend.cost.tokensPriced`) are the same arithmetic over the same runs; a personal
     * clause taken from these and the shared one cannot disagree.
     *
     * `"UNMEASURED"` on BOTH halves when pricing was never attempted — `0 priced, 0 unpriced` would
     * read as an attempt that found nothing to skip. A measured `0` priced beside a positive
     * unpriced count is the real reading for a run whose every model was unknown to the table.
     */
    costTokensPriced?: Measured<number>;
    costTokensUnpriced?: Measured<number>;
  };
}

// --- the population scorecard -----------------------------------------------
/**
 * Population-level aggregates, emitted by `improve/dto.scorecard_to_wire`.
 *
 * This is what the surface can say when `patterns.admitted` is empty, which it is today: the
 * bundle used to carry zero facts about zero runs, so there was nothing to render but a blank
 * panel. Fixed size regardless of population, so it never participates in bundle trimming.
 *
 * Two rules it exists to keep, and a boundary:
 *
 * 1. **Every average and sum carries its denominator.** `measuredOf` vs `total`. Most terms
 *    here are UNMEASURED for most runs, so a mean over 3 of 174 runs must not render as a
 *    population mean, and a spend total over 3 of 174 must not be captioned "spend".
 * 2. **UNSCORED is neither pass nor fail.** It is its own count and `passRate` is taken over
 *    `pass + fail` alone (`null` when that is 0). An unmeasurable run says something about the
 *    evidence, not about the agent.
 *
 * The boundary: the bundle carrying this is ONE GLOBAL DynamoDB item read by every actor, so it
 * holds counts, rates, enum values, timestamps and evaluator/component NAMES — and nothing
 * derived from user input or model prose. That is why there is no per-run list here and no
 * judge text anywhere in it, and why `ImproveRun` below is still not emitted. `outcomes` is how
 * "why did runs fail" is answered within that boundary: the failing check is NAMED, its sentence
 * is not.
 */
export interface MeasuredMean {
  /** Mean of the MEASURED values. `"UNMEASURED"` when none were, never `0`. */
  mean: Measured<number>;
  /** How many of `total` contributed. Render it: it is the difference between a fact and a
   *  guess. */
  measuredOf: number;
  total: number;
}

export interface MeasuredSum {
  /** Sum of the MEASURED values. `"UNMEASURED"` when none were — a `0` here would read as
   *  "free". */
  sum: Measured<number>;
  measuredOf: number;
  total: number;
}

/**
 * `spend.cost` alone: the sum over RUNS every sibling term carries, plus the two TOKEN counts that
 * say how much of the money that sum actually covers.
 *
 * Two denominators, because cost is the one spend term that can be partly measured WITHIN a single
 * run. Pricing is per model, so a session using one model the rate table has no row for is still a
 * fully measured run: the models it can price are priced, the one it cannot is skipped, and the
 * session contributes a SHORT price rather than the sentinel that used to discard every priced
 * model beside it. `measuredOf`/`total` count RUNS, so on runs alone such a population reads "8 of
 * 8 sessions priced" over a total missing whatever the skipped model spent — complete-looking and
 * short. `tokensPriced`/`tokensUnpriced` count TOKENS, and they are the only thing on the wire that
 * says so: any unpriced tokens mean the figure is short by exactly those.
 *
 * Both are plain counts and never sentinels, deliberately. The producer sums the runs that measured
 * and leaves the unmeasured ones out of both halves, because these counts are what a reader
 * compares a possibly-short figure AGAINST, and a sentinel would leave nothing to compare it to.
 * A measured `0` unpriced is the ordinary reading: on the live population today two models are used
 * and the table prices both, so all 6,813,939 tokens are priced and none skipped.
 *
 * OPTIONAL because the two keys are newer than the bundles. A scorecard harvested from staging
 * today carries `sum`, `measuredOf` and `total` and nothing else under `cost`, and bundles like it
 * are still served — the same way a real emitted bundle turned out to omit `attribution`, which
 * this type declares as required. Absent is not `0`: it means nothing counted a shortfall, not that
 * there is none, so branch on `undefined` rather than writing `?? 0`, which asserts a coverage
 * nobody measured.
 */
export interface MeasuredCost extends MeasuredSum {
  tokensPriced?: number;
  tokensUnpriced?: number;
}

export interface PopulationCounts {
  total: number;
  pass: number;
  fail: number;
  /** Nothing scorable: a gating evaluator could not measure. Not a failure, not a pass, and
   *  never folded into either. */
  unscored: number;
}

/**
 * What discovery found against what it handed on — the SAMPLE every figure on this scorecard is over.
 *
 * `PopulationCounts.total` is the size of the SCORED set, and on a discovered population that set is
 * a sample twice over: the span floor drops a session with too few spans to be worth judging, and the
 * population cap then takes the newest of what is left. Neither exclusion was counted anywhere, so
 * `total: 25` was captioned "the population" while covering an unknown fraction of one, and the pass
 * rate, the spend and every day row folded from those runs inherited the caption.
 *
 * `admitted` is what `total` counts, and `discoverable + belowMinSpans` is the whole harvest — so
 * neither bound can hide inside the other.
 *
 * Plain numbers rather than `Measured`, because the producer emits the block only when all three were
 * counted and drops it whole otherwise: two of three counts is not a denominator.
 */
export interface DiscoveredSessions {
  /** Sessions in the harvest with enough spans to be worth judging: `admitted`'s denominator. */
  discoverable: number;
  /** How many of those the cap let through. This is what `PopulationCounts.total` counts. */
  admitted: number;
  /** Sessions the span floor excluded, so they sit OUTSIDE `discoverable` rather than inside it. */
  belowMinSpans: number;
}

/**
 * The scored population's counts, plus what they are a sample of.
 *
 * `sessions` is ABSENT, never zeroed, when there was no discovery step at all — the labelled
 * benchmark has fixed membership and no cap — because zeros would claim a step ran and discarded
 * nothing. Branch on `undefined`.
 *
 * Its own interface rather than a field on `PopulationCounts`, which `WorkloadClassScore` also
 * extends: a per-class entry carries no discovery counts, and typing it as though it might invites a
 * reader to look for one.
 */
export interface ScoredPopulation extends PopulationCounts {
  sessions?: DiscoveredSessions;
}

export interface WorkloadClassScore extends PopulationCounts {
  /** `pass / (pass + fail)`, or `null` when nothing in the class was scored. */
  passRate: number | null;
  /**
   * Every DISTINCT gating set seen in this class, so `tool_discovery: 19 pass` cannot be
   * confused with a class that was never checked. A list of sets rather than one set because a
   * mixed population is a fact about the data, not something to collapse.
   */
  gating: string[][];
  gatingIsMixed: boolean;
  /** How the class was determined, e.g. `inferred_from_spans`. */
  basis: string[];
}

/**
 * Token spend the HARVEST measured and could not hand on, beside the sum it is missing FROM.
 *
 * `spend.tokens` sums the sessions the population admitted and its `measuredOf`/`total` are over those
 * alone — so a session whose usage was measured while the harvest's row cap dropped its spans is not
 * SHORT in that fraction, it is absent from both halves of it. The live item reads
 * `{sum: 10320108, measuredOf: 25, total: 25}`, complete coverage, on a harvest that had thrown
 * tokens away. This block is the only term that can say so.
 *
 * `tokensMeasured` is the honest denominator for `tokensDiscarded`: what the usage query saw across
 * the whole window. The two do NOT close the gap to `spend.tokens.sum` by subtraction and must not be
 * read as if they did — the span floor and the population cap narrow the set again afterwards, and
 * that shortfall is `ScoredPopulation.sessions`. One statement is licensed: the money is short by at
 * least `tokensDiscarded`.
 *
 * Every term is `Measured`, and `"UNMEASURED"` throughout is what a harvest that never audited itself
 * reads as — one whose usage query was skipped, or one taken before the audit existed. A measured `0`
 * is the cap not binding, which is a different fact and the only good one.
 */
export interface UsageShortfall {
  tokensDiscarded: Measured<number>;
  tokensMeasured: Measured<number>;
  sessionsDiscarded: Measured<number>;
  sessionsMeasured: Measured<number>;
}

/**
 * One Bedrock model's share of the population's spend — a row of `spend.byModel`.
 *
 * The actionable half of `modelsSeen`. That count says how many models exist; this says which one
 * the money went to, and the distribution is nowhere near even — on the live population one of two
 * ids carries 97% of the tokens. A count of two cannot show that, so a reader with the count alone
 * cannot tell which step to move onto a cheaper model.
 *
 * THE DENOMINATOR IS NOT THE POPULATION, and misreading it overstates every row. `tokens.measuredOf`
 * counts the runs in which THIS model appears; `tokens.total` is `spend.tokens.measuredOf` — the runs
 * whose usage was measured at all, NOT `spend.tokens.total`. A run with no usage block never recorded
 * which model it did not use, so it cannot sit in a per-model denominator without asserting an
 * absence nobody observed. So `14 of 25` on a row means 14 of the runs that recorded usage, and the
 * measured-share of the population is `spend.tokens`' own fraction one level up. The two compose;
 * neither is the other.
 *
 * The rows are ranked by measured tokens, ties broken by id, so the first row is the biggest spender
 * and the order is stable across passes. They are bounded — see `ModelSpendRemainder`.
 *
 * `model` is a RAW Bedrock id, cross-region routing prefix included (`global.anthropic.…`,
 * `us.anthropic.…`). The producer strips that prefix for pricing alone
 * (`observe/build_run._normalise_model`) and keys these rows on what the telemetry recorded, so the
 * same model reached through two regions is TWO rows here. Do not fold them: `measuredOf` counts
 * runs, a run can appear in both rows, and adding those counts would report a coverage nobody
 * measured. The id is an identifier and is shown as it arrived.
 */
export interface ModelSpend {
  model: string;
  tokens: MeasuredSum;
  /** This model's tokens in the four separately-priced categories. Nothing renders these yet: a
   *  four-way split per row is a table, and the rows are shown as a sentence. */
  byCategory: {
    input: MeasuredSum;
    output: MeasuredSum;
    cacheRead: MeasuredSum;
    cacheWrite: MeasuredSum;
  };
  llmCalls: MeasuredSum;
}

/**
 * The models `spend.byModel` did not name, as a count and a sum — `spend.byModelOther`.
 *
 * The bound's own denominator, and the reason the named rows can be read as shares at all: without
 * it a roster of twenty rows could be twenty of twenty or twenty of two hundred, and every
 * percentage drawn over it would be an overstatement of unknown size. So it must render whenever it
 * stands for anything.
 *
 * `models: 0` is a MEASURED zero — the producer emits this block on every pass, and zero is how it
 * says the bound withheld nothing, which is the live reading today. That is not the same as the key
 * being absent, which means the bundle predates it. When the count is zero the sum beside it is
 * `"UNMEASURED"` rather than `0`, because there were no values to sum and a `0` would read as a
 * measured remainder of nothing; so the COUNT decides whether this block stands for anything, never
 * the sum.
 */
export interface ModelSpendRemainder {
  models: number;
  tokens: MeasuredSum;
}

/** One gating check's outcome counts for one model: `rate` is PASS / (PASS + FAIL), `null` with none. */
export interface ModelCheckScore {
  pass: number;
  fail: number;
  /** NOT_APPLICABLE / NO_DATA: the check had nothing to measure — in neither term of `rate`. */
  notMeasured: number;
  rate: number | null;
}

/**
 * Every score the scorecard reports, again for ONE model — the runs whose tokens mostly went to it
 * (`dto._by_model_scores`). Nothing is re-judged; this regroups the evaluators' results by model.
 */
export interface ModelScores {
  model: string;
  population: ScoredPopulation;
  passRate: number | null;
  tiers: { dispatch: MeasuredMean; delivery: MeasuredMean };
  /** By evaluator name (`DagCompletionValidator`, …); a check this model never needed is absent. */
  checks: Record<string, ModelCheckScore>;
  perRun: { tokens: MeasuredMean; llmCalls: MeasuredMean; latencyMs: MeasuredMean };
  /**
   * AgentCore online-evaluation scores (0..1) by evaluator id (`Builtin.Helpfulness`, …): the mean
   * over this model's runs AgentCore scored, one value per run, `measuredOf` = how many it scored.
   * An evaluator no run carries is absent. Absent altogether on snapshots before 2026-10-17.
   */
  agentcore?: Record<string, MeasuredMean>;
}

export interface Scorecard {
  population: ScoredPopulation;
  passRate: number | null;
  /** The same scores per model, most runs first (absent from snapshots written before 2026-10-17). */
  byModelScores?: ModelScores[];
  /** Models beyond the named ones, and judged runs no model could be credited for. */
  byModelScoresOther?: { models: number; runs: number; unattributed: number };
  tiers: {
    /** Mean `dispatch_correct` score across the population. */
    dispatch: MeasuredMean;
    /** Mean `results_delivered` score across the population. */
    delivery: MeasuredMean;
  };
  /**
   * Totals, each with its denominator.
   *
   * `tokens` is a REAL measurement on the deployed producer — the sum of the session's `chat`
   * spans, harvested per session and per model. It still carries `measuredOf`/`total` because
   * roughly a third of harvested sessions record no LLM usage at all, so a bare sum would
   * understate a partly-harvested population.
   *
   * `cost` is PRICED, and priced in PART where it has to be: the producer values each session per
   * model and per token category from a hand-sourced Bedrock rate table, and `costBasis` says where
   * those rates came from and when. It is a `MeasuredCost` rather than a plain `MeasuredSum`
   * because the coverage of a price has to be read twice over.
   *
   * `measuredOf` counts the sessions priced AT ALL, which can be narrower than the sessions that
   * recorded tokens: a session no model of which the table can price is not priced, so captioning a
   * cost with the token term's denominator reads a partial bill as the population's. And a session
   * the table prices only in part counts as priced here — one skipped model does not disqualify the
   * models beside it — so the run denominator can read complete over a sum that is short.
   * `cost.tokensPriced` / `cost.tokensUnpriced` are what say by how much, and they are why a reader
   * must not treat `measuredOf === total` as a complete bill. A population where nothing priced
   * reports `"UNMEASURED"` over `measuredOf: 0`, with `costBasis` naming the missing input instead;
   * that is also what every bundle written before the rates shipped carries, so both readings are
   * live and a reader must handle each.
   *
   * **Do not compute cost in the UI by multiplying `tokens` by a rate** — not even to fill in an
   * unpriced population. The four `byCategory` terms are priced very differently — a cache READ is
   * about a tenth of an uncached input token and cache reads are the large majority of prompt tokens
   * here — and the population spans `modelsSeen` distinct Bedrock models whose rates differ
   * severalfold. A single-rate figure is wrong by most of an order of magnitude, not slightly off.
   */
  spend: {
    tokens: MeasuredSum;
    cost: MeasuredCost;
    /**
     * Where `cost`'s rates came from and when, in one sentence — or, on a population nothing could
     * price, which input was missing.
     *
     * Render it BESIDE the figure, not in place of one. A dollar amount computed from rates typed
     * in by hand reads as billed truth, and this sentence is the whole of what tells a reader it is
     * an estimate, which page the rates came off, and on what date — so it is as load-bearing under
     * a priced figure as it was in place of an unpriced one.
     *
     * It is also 839 characters long and says at the END of itself that pricing can be partial and
     * that the token counts are how a reader spots a short figure. Anything showing a HEAD of it
     * therefore drops that pointer, which is why the shortfall has to be stated as its own clause
     * rather than left to this sentence to explain.
     */
    costBasis: string;
    latencyMs: MeasuredSum;
    /**
     * The four separately-priced token categories. They sum to `tokens` exactly (Bedrock's own
     * arithmetic: total = input + output + cacheRead + cacheWrite), so they stack without a
     * residual. `input` is UNCACHED input only.
     */
    byCategory: {
      input: MeasuredSum;
      output: MeasuredSum;
      cacheRead: MeasuredSum;
      cacheWrite: MeasuredSum;
    };
    /** LLM calls made across the population. */
    llmCalls: MeasuredSum;
    /**
     * How many DISTINCT Bedrock model ids the population's tokens were spent on. A count, not a
     * list: the count is what makes a single-rate cost indefensible, and it is all a reader needs.
     */
    modelsSeen: number;
    /**
     * WHICH models those tokens went to, biggest first — the same asymmetry `byCategory` is, one
     * level down, and the one figure in this block a reader can act on with no rate table at all.
     *
     * OPTIONAL for this file's usual deploy-order reason: an item stored before the key existed is
     * served back unchanged, and this SPA ships separately from the producer. Absent is not an empty
     * roster — `[]` would say the producer looked and found no model, which is a different fact from
     * nobody having looked — so branch on `undefined` rather than writing `?? []`.
     *
     * The rows sum to `tokens.sum` exactly, `byModelOther` included, which is what makes a share over
     * them need no residual term. Take that share over the ROSTER'S OWN sum and never over
     * `tokens.sum`: the two agree today and are computed from different folds, so a share taken
     * against the larger number would silently report the roster as more complete than it is.
     */
    byModel?: ModelSpend[];
    /**
     * The models the roster's bound withheld, folded. Render it whenever `models` is positive: it is
     * the denominator that makes the named rows readable. See `ModelSpendRemainder`.
     */
    byModelOther?: ModelSpendRemainder;
    /**
     * What the harvest measured and never handed to the terms above.
     *
     * OPTIONAL because it is newer than the bundles: the live item carries no such key, and every
     * figure on it therefore reads as complete coverage of a population a third of whose measured
     * tokens are missing. Absent is not a clean bill — branch on `undefined` rather than writing
     * `?? 0`, which would report a harvest nobody audited as one that dropped nothing.
     */
    usageShortfall?: UsageShortfall;
  };
  /**
   * WHY runs are failing, keyed by the GATING evaluator's name — the structural half of a
   * verdict, derived from each check's outcome and never from parsing `Verdict.reason`.
   *
   * Two properties to render correctly. A run failing two gating checks counts under BOTH, so
   * these do not sum to `population.fail` — the question they answer is "which check is
   * failing", and `population` is right there to compare against. And a `0` means "gated, and
   * it never failed", which is not the same as a name being absent ("never gated at all").
   *
   * `unmeasured` is the NO_DATA half: a gating check whose evidence was missing. Those runs are
   * `unscored`, not failures.
   */
  outcomes: {
    failedGating: Record<string, number>;
    unmeasured: Record<string, number>;
  };
  /** Per workload class, because each class is gated on a different objective and one number
   *  over all of them means nothing. */
  byWorkloadClass: Record<string, WorkloadClassScore>;
  /** Wall-clock extent of the scored population, or `UNKNOWN`. */
  window: Measured<RunWindow>;
  /** How many member windows were RECONSTRUCTED rather than stamped. */
  derivedWindows: number;
  /**
   * How much of the population's own evidence is KNOWN missing, because a harvest window hit the
   * CloudWatch Logs Insights row cap and part of those runs' trajectories was never read.
   *
   * Render all three numbers or none. `incomplete: 0` on its own is indistinguishable from "no
   * harvester recorded completeness for any run", which is what every bundle produced before
   * adaptive windowing looks like — so `measuredOf: 0` means NOT CHECKED, not all-clear. Runs
   * counted in `incomplete` still appear in `population`; their trajectory-derived observations
   * are marked unobserved, which routes them out of pattern control pools rather than out of the
   * population.
   *
   * A count, never a list: this rides the one global SNAPSHOT item, so which sessions were
   * affected may not travel here.
   */
  evidenceComplete: {
    incomplete: number;
    measuredOf: number;
    total: number;
  };
  /**
   * How many actors these numbers are aggregated over, and how much of the population has a
   * per-actor bundle behind it. Counts only, never a list — actor ids are Cognito subjects, and
   * naming them here would tell every tenant who else is on the harness.
   *
   * `actors` is what makes the platform-wide reading checkable: this scorecard mixes every
   * tenant's runs, so it CANNOT be captioned "your agent is N% efficient".
   *
   * All five counts reach a reader through `QualityBlock`, the population's own block behind
   * Advanced — `actors` in its tenant line, the other four in its ownership line — and they are read
   * there as one group deliberately: how many people "everyone" is, and how many of the sessions
   * belong to nobody, is what says why a shared figure can look like one's own. The shared figures on
   * the default view are still labelled by scope alone, so anything that comes to caption THOSE owes
   * them the same reading.
   *
   * `unattributable` is the number that must not be hidden: those runs are conversational, scored
   * and counted, and no actor's surface will ever show them, because AgentCore Memory named no
   * owner for the session. `ownable` is its denominator — the conversational count, the only class
   * that can have an owner at all.
   */
  attribution: {
    actors: number;
    attributed: number;
    unattributable: number;
    ownable: number;
    runs: number;
  };
  /**
   * What the platform was doing underneath every one of these numbers — the fact that decides
   * whether a day with no findings was a quiet day or an ops day.
   *
   * OPTIONAL for the deploy-order reason every recent key here carries: the served item predates it,
   * and absent is "nobody published this block", never a healthy platform. See `PlatformPopulation`.
   */
  platform?: PlatformPopulation;
}

/**
 * One day of the WHOLE population's spend — the shared counterpart to `ActorDay`.
 *
 * Same spend terms and the same `coverage` block as a per-actor row, and deliberately so: one summing
 * rule serves both scopes and neither can drift into captioning itself differently. The two
 * differences are both historical rather than principled:
 *
 *  - the category sums are FLAT here (`input` beside `cacheRead`) where a per-actor row nests them
 *    under `byCategory`. This block was already deployed when the two were reconciled, and a rename
 *    would have made every row the producer had already written unreadable.
 *  - the session count is `population`, matching what a scorecard calls it.
 *
 * `coverage.byCategory` nests on BOTH rows even so, because that block is new and had no deployed
 * shape to preserve.
 *
 * `passRate` and `runsWithWaste` are the TREND half of this block, and no spend metric reads them: a
 * rate over a month is not a spend total and does not belong on a tile beside one. What reads them is
 * the outcome band's day-over-day caption and its sparkline, which compare the latest row against the
 * one before it and plot the series. Neither computes a mean over the rows: the producer folds
 * `passRate` WEIGHTED by `population` when a day ages out of the retained window
 * (`aws_lambda._ROW_WEIGHTED_FIELDS`), so an unweighted average taken here would disagree with the
 * producer's own arithmetic about the same days.
 */
export interface PopulationDay extends DaySpend {
  /** `YYYY-MM-DD`, UTC. The only thing that places a row in time. */
  date: string;
  /** Sessions in the population that day. `runs` on `DaySpend` is unused here — a scorecard has
   *  always called this `population`, and the normaliser reads it across. */
  population?: Measured<number>;
  passRate?: Measured<number>;
  runsWithWaste?: Measured<number>;
  input?: Measured<number>;
  output?: Measured<number>;
  cacheRead?: Measured<number>;
  cacheWrite?: Measured<number>;
}

// --- the efficiency path ----------------------------------------------------
/**
 * What one waste KIND observed, emitted by `improve/dto.waste_to_wire`.
 *
 * The kinds are `repeated_sequence` (the same ordered block of tool calls ran more than once),
 * `abandoned_dispatch` (work dispatched and never collected before the session ended) and
 * `error_then_retry` (a tool call failed and the same tool later succeeded). Every kind the
 * producer HAS is always present, with zeros when it found nothing — so a missing key means the
 * producer has no such signal, not that the signal was clean.
 */
export interface WasteKindTotals {
  /** How many findings of this kind, across the population. */
  findings: number;
  /** How many distinct runs carried at least one. */
  runs: number;
  /** Tool calls charged. Charged, not observed: for a repeat it is the earlier occurrence(s). */
  calls: number;
  /** Wall time of those calls. `"UNMEASURED"` when no contributing span recorded a duration. */
  wallMs: MeasuredSum;
  /**
   * What this kind IS, in a sentence a reader who has never seen the harness can act on — "The same
   * series of steps ran more than once". Written in the producer from a closed table, so the browser
   * cannot invent one and the three sentences cannot drift apart across surfaces.
   *
   * OPTIONAL, and the absence is load-bearing rather than defensive: this SPA and the producer are
   * separate artifacts that deploy in either order, and a kind added to the detectors before its
   * sentence exists carries none either. A consumer must OMIT such a kind rather than fall back to
   * the key, which is an engine identifier (`repeated_sequence`) and reaches a reader as one.
   */
  plainSummary?: string;
}

/**
 * The efficiency path's population totals.
 *
 * Read this as REPEATED, not as WASTED. A finding says a run performed work whose output was
 * discarded — but spans carry no tool arguments for harness builtins, so a second run over
 * different inputs is indistinguishable from a redo. The producer states that per finding; only
 * the counts reach this bundle.
 *
 * Deliberately counts-only, and it must stay that way. Each finding names the tool names it
 * observed, and a tool name from a user-registered MCP server is user-derived data — this bundle
 * is ONE global DynamoDB item served to every actor, so a named sequence, a session id or a list
 * of run ids may not travel on it. The per-finding carrier is the per-actor bundle.
 *
 * Unlike everything above, this is NOT filtered by pass/fail: waste is orthogonal to correctness,
 * so these runs include passing ones, and most of them are. Do not caption it as failures.
 */
export interface WasteTotals {
  /** Runs carrying at least one finding — read against `runs`, never alone. */
  runsWithWaste: number;
  /** The population this was measured over. */
  runs: number;
  /**
   * Runs whose repeated-block search was SKIPPED because they held too many tool calls. Not
   * clean runs: nobody looked. Render it whenever it is non-zero.
   */
  runsNotScanned: number;
  /**
   * Distinct calls charged, unioned per run across kinds — a call two kinds both charged counts
   * once. So `byKind[*].calls` do NOT sum to this, on purpose: the per-kind numbers are exact for
   * their kind and this one refuses to double-charge.
   */
  totalCalls: number;
  totalWallMs: MeasuredSum;
  byKind: Record<string, WasteKindTotals>;
}

// --- suggestions ------------------------------------------------------------
/**
 * The mined (shape -> symptom) hypothesis a suggestion rests on — the CLAIM half.
 *
 * Two fields deliberately absent, and both live on the reader's own bundle instead
 * (`PatternEvidence`): the member session ids, because a per-row id list on a shared item is
 * per-row exposure even with no text attached; and `rootCauseHypo`, the facet evidence prose,
 * because it interpolates plan node ids, span indices and timestamps out of one tenant's session.
 * A span name or a node id explains nothing in a suggestion anyway — it belongs beside the
 * evidence it came from.
 */
export interface FailurePattern {
  patternId: string;
  /** Plain-English rendering of the shape conjunction. */
  shape: string;
  /** Facet names, for the admin surface. */
  shapeFacets: string[];
  /**
   * The CONSEQUENT: what this shape predicts, as the symptom facet names the evaluators produced
   * (`tier_failed:<tier>`, `check_failed:<check>`).
   *
   * A signature is a (shape -> symptom) claim, and `shape` above is only its antecedent — so without
   * this the row publishes half a hypothesis, and two patterns differing only in which gating check
   * failed render identically. Every other mined bucket on this wire already carries the pair
   * (`ResidueEntry.symptom`).
   *
   * DECLARED HERE AS WELL AS ON `ResidueEntry`, and the duplication is load-bearing rather than an
   * oversight: the two are different objects and a name declared on one satisfies no check for the
   * other. `scripts/improve-wire-census.py` matches NAMES, so this addition moves no number in it.
   *
   * Named after the EVALUATORS, so these are harness identifiers — the same argument that lets
   * `Scorecard.outcomes` name a failing evaluator on a shared item. OPTIONAL for the deploy-order
   * reason every recent key here is: absent means the bundle predates it, never that the shape
   * predicts nothing.
   */
  symptom?: string[];
  /** How many runs the pattern rests on. The count is the useful half of the member list: "one
   *  run" versus "eight" is the difference between an anecdote and a rate. */
  runCount: number;
  /** Must be 0. A non-zero value means the pattern also fires on a passing
   *  run, and the suggestion should not have been generated. */
  passHits: number;
  /**
   * How many of the offered passing runs this shape CONCLUSIVELY did not fire on.
   *
   * `passHits` is meaningless without this and the two below. `passHits: 0` against one control is
   * nearly free and against twelve is a real claim, and the live item ships the zero with no control
   * count at all — so a shape tested once and a shape tested twelve times render identically.
   */
  controlsExcluded?: number;
  /**
   * How many offered controls the gate could NOT place, because those runs' own evidence was missing.
   *
   * A COUNT and never ids, and there is no field for the ids anywhere on this wire. An inconclusive
   * control is another run in the population and belongs to whichever tenant ran it, so its id is
   * safe neither on the shared item nor on the failing run's owner's — the same argument
   * `ResidueEntry.passHits` gives for withholding the pass-hit runs.
   */
  controlsInconclusive?: number;
  /**
   * The DENOMINATOR of the two above: how many passing runs the gate was offered as controls, after
   * the same-class and one-per-parent-request restrictions narrowed the population.
   *
   * `0` is a measured zero and is the WORST reading here, not a clean one: no passing run was
   * available, so `passHits: 0` establishes nothing. It must never render as a gate that was passed.
   *
   * The triple closes — `excluded + inconclusive === considered` — only WITHIN one refinement. A
   * pattern groups several runs and takes the triple from its least-tested member, so the three
   * describe one run's gate and need not add up across the set. Do not render that sum as an identity
   * that must hold, and do not report a shortfall against it.
   */
  controlsConsidered?: number;
  /**
   * Other facets that reached `passHits: 0` just as well on the same population.
   *
   * Non-empty means the evidence does NOT distinguish this explanation from those, and the shape
   * named above was chosen by the miner's facet precedence rather than by the data — a caveat on the
   * claim, and one a reader cannot infer from anything else on the card. A UNION across the pattern's
   * member runs, because failing to distinguish is a defect of the pattern if it happened for any one
   * of them. Facet names, so harness identifiers.
   */
  equallyDiscriminative?: string[];
}

export interface Candidate {
  candidateId: string;
  label: string;
  /** Unified diff computed server-side (Python difflib); empty when there is no text to
   *  diff. No diff engine exists in the browser and none is wanted. */
  diffText: string;
  before: string;
  after: string;
  claim: {
    utilityTerm: string;
    direction: 'increase' | 'decrease';
    magnitude: number | null;
    onPattern: string;
  };
  risk: string | null;
  disposition: Disposition;
}

export interface Affordances {
  canTryBoth: boolean;
  tryBothBlockedReason: 'no_host_agent' | 'flat_category' | 'locked_today' | null;
  canPublish: boolean;
  /** True when blastRadius is wider than a single instance. */
  requiresTrialBeforePublish: boolean;
  /** Revert needs `RollbackAsset`, which is currently unwired everywhere, so this is
   *  false until that lands, and the card must not promise a revert it cannot do. */
  canRollback: boolean;
  rollbackToVersion: number | null;
  canSendToTeam: boolean;
  hostAgentId: string | null;
  /** Publishing consumes the working copy, so a Try-both needs `EditAsset` to fork
   *  first. */
  forkNeeded: boolean;
}

/**
 * How trustworthy a suggestion's evidence is. Server-computed like `affordances`, it depends on
 * the member runs' windows and the account's alarm history, which the browser does not hold.
 *
 * Exists because a recommendation renders identically whether it rests on four verified runs or
 * on one whose timing was guessed and whose platform state is unknowable.
 */
export interface EvidenceQuality {
  /** How many runs the pattern rests on. Zero for a fabricated example. */
  runCount: number;
  /** Wall-clock extent covered by the member runs, or `UNKNOWN`. */
  window: Measured<RunWindow>;
  /** How many member runs had a RECONSTRUCTED rather than stamped window. */
  derivedWindows: number;
  /**
   * Worst state across member runs (`degraded` > `unknown` > `healthy`). Worst, not most-common:
   * the question is whether ANY evidence is compromised.
   */
  platform: PlatformHealth;
  /** Plain-English caveats, priority order, empty when the evidence is clean. Server-computed
   *  for the same reason as `plainReason`. */
  caveats: string[];
}

export interface Suggestion {
  suggestionId: string;
  createdAt: string;
  status: 'open' | 'accepted' | 'dismissed' | 'superseded';
  targetType: TargetType;
  targetId: string;
  targetName: string;
  /** Where the component lives, shown for code_change so "needs an engineer" is
   *  actionable rather than a shrug. */
  targetLocation: string | null;
  baseVersion: number | null;
  /** From the Σ registry. Anything not explicitly cleared is view-only. */
  mutability: string;
  operation: 'add' | 'edit' | 'remove';
  /** ONE plain-English line. No session ids, no facet names. */
  summary: string;
  evidenceSummary: string;
  /**
   * WHAT HAPPENED, in at most eight words — the line a card leads with.
   *
   * `evidenceSummary` above is the same observation stated in full, and keeps its text and its
   * meaning: the headline leads, that explains. Generated in the producer from the shape's leading
   * facet (`improve/propose.headline_for`) so a browser cannot assemble one and drift from what the
   * facet measured, and never interpolated — it is a constant per facet, riding to the one shared
   * item every reader reads.
   *
   * OPTIONAL, and the absence is the producer's own: it is omitted, never blanked, for a leading
   * facet the table has no phrase for, since the alternative is a facet name in front of a reader.
   * A bundle written before the field lands on the same absence, and so does a deploy serving an
   * older producer than this SPA. So a consumer must degrade to `evidenceSummary` rather than
   * render a hole, and must never fall back to a facet name.
   */
  evidenceHeadline?: string;
  pattern: FailurePattern;
  candidates: Candidate[];
  /** Set when the several-candidates rule could not be met, with the reason,
   *  preferred over padding the list with a variant nobody would pick. */
  singleCandidateForced: string | null;
  blastRadius: { class: string; instanceCount: number };
  /**
   * When the loop FIRST mined this, and the last run that reproduced it. Both come from the
   * store, not from a pipeline run, because a finding outlives the evidence it was mined from:
   * plan-state and alarm history both age out of retention, so a suggestion that cannot
   * be re-derived is still real.
   */
  firstSeenAt?: string;
  lastSeenAt?: string;
  /**
   * Did the most recent pipeline run find this again?
   *
   * `false` is NOT "fixed" and NOT "dismissed". It means the last run did not reproduce it, and
   * that has several possible causes: the defect was fixed, the traffic that triggered it did
   * not recur, or the evidence aged out of retention. The UI says "not seen in the latest run"
   * and stops there, because the loop cannot distinguish those and should not imply it can.
   */
  reproducing?: boolean;
  affordances: Affordances;
  /** How good the evidence is. Optional until the API computes it. */
  evidenceQuality?: EvidenceQuality;
  provenance: Provenance;
}

// --- components (the Σ map) -------------------------------------------------
export type Mutability = 'THE_ACTUATOR' | 'AUTO_CANDIDATE' | 'REVIEW' | 'MANUAL'
  | 'LOCKED_TODAY' | 'LOCKED' | 'PROTECTED' | 'FIX_FIRST' | 'UNRECORDED';

export interface ComponentRow {
  componentId: string;
  name: string;
  layer: string;
  assetId: string | null;
  currentVersion: number | null;
  /** false => no version badge, no Publish. Flat categories and platform code. */
  versioned: boolean;
  mutability: Mutability;
  sourceLocation: string;
  suggestionCount: number;
  /**
   * The four REGISTRY facts a reader needs to weigh an accusation against the component named in it.
   * All four OPTIONAL for the deploy-order reason: a row stored before they existed is served back
   * unchanged, so branch on `undefined` rather than defaulting.
   *
   * `type` is what KIND of component this is in the registry's own vocabulary (`system_prompt`,
   * `gate`, `budget`), where `layer` says where in the harness it sits. A reader deciding whether an
   * accusation is plausible needs both.
   *
   * DECLARED HERE THOUGH THREE OF THE FOUR MOVE NO CENSUS NUMBER. `type`, `blastRadius` and `notes`
   * are each already declared on some OTHER object, and `scripts/improve-wire-census.py` matches
   * NAMES — so the producer has been emitting them on this row against no declaration at all, and
   * only `knownFailureModes` was visible in the count. Same blind spot the census documents for
   * `symptom`.
   */
  type?: string;
  /** How far a change here reaches, in the registry's words (`single`, `all sessions`). Published
   *  per SUGGESTION as `blastRadius.class` too; here it is a property of the component, so a
   *  component with no suggestion still says how dangerous it is. */
  blastRadius?: string;
  /**
   * THE WARRANT, on the accused rather than on the accusation: the failure modes the registry
   * DECLARES for this component.
   *
   * `SigmaRegistry.match` attributes a failure by finding a word-boundary hit inside this text, so it
   * is what makes a suggestion's `attributionBasis: "registry_declared"` checkable from the Σ map as
   * well as from the suggestion — byte-identical to that suggestion's
   * `provenance.matchedFailureMode`, one definition in `Component.declared_failure_mode`.
   *
   * Bounded to a first sentence by the producer, and `''` for the 37 of 80 real components that
   * declare none — an absence the attribution basis already accounts for, and NOT the same fact as
   * the key being absent.
   */
  knownFailureModes?: string;
  /** The other half of what `match` searches: a `keyword_inferred` attribution can hit the id, the
   *  type, the source location or these notes. Bounded to a first sentence by the producer. */
  notes?: string;
  /**
   * `null` means nothing attributes here, so nothing is known — never a measured zero.
   *
   * `deliveredOf` is the delivered share's denominator and is NOT `runs`: only the attributing
   * runs whose delivery tier returned a PASS or a FAIL count towards it, so `deliveredOf: 0`
   * with a positive `runs` is the unmeasured state. A count pair rather than the percentage
   * this carried before, because `2` of `3` arrived as `66.66666666666667` and rendered so.
   */
  health: { runs: number; delivered: number; deliveredOf: number } | null;
}

export interface ComponentSection {
  layer: string;
  label: string;
  editable: boolean;
  components: ComponentRow[];
}

export interface ComponentsResponse {
  /** The five sections a user can act on. */
  yours: ComponentSection[];
  /** Known defects with a recorded decision to fix them, surfaced as "needs an
   *  engineer", never as something the user can edit. */
  fixFirst: { count: number; components: ComponentRow[]; note: string };
  admin: { count: number; components: ComponentRow[]; note: string };
  /** PROTECTED plus every component whose mutability the registry has not recorded.
   *  Default-deny: undecided is not editable. */
  locked: { count: number; components: ComponentRow[]; note: string };
  registryMeta: { componentCount: number; revalidated: string; headSha: string };
}

// --- admin: what mining rejected -------------------------------------------
/**
 * A failure that could NOT be explained. Showing these is the point: an unexplained
 * failure must stay visible rather than vanish, and "1 of 2 failures produced a
 * recommendation" is a more honest claim than two confident suggestions.
 */
export interface ResidueEntry {
  symptom: string[];
  bestShape: string[];
  /** How many PASSING runs the best shape also matched. The ids of those runs travel nowhere:
   *  a control belongs to whichever tenant ran it. */
  passHits: number;
  /**
   * The engine's own words, e.g. `"no shape conjunction (<= 4 facets) separates this failure from
   * 2 passing run(s)"`. Precise, and unreadable to anyone who does not know what a facet is, so
   * it belongs on the admin Findings surface, not the Inbox.
   *
   * It carries COUNTS, never session ids — the runs that disqualified a shape are named only on
   * `pass_hit_runs`, server-side, which is why this sentence is safe on a shared item at all.
   */
  reason: string;
  /**
   * The same fact in plain English, for a user-facing surface. Optional because the producer does
   * not supply it: the mining stage writes only `reason`, and a rewrite must come from the server
   * rather than be assembled in a component, or the two will disagree about what a run's failure
   * means. Until it exists the Inbox falls back to `reason`, jargon and all, which is the honest
   * degradation, and visible enough to motivate closing the gap.
   */
  plainReason?: string;
}

/**
 * The same row on the reader's OWN bundle, plus the session it is about.
 *
 * Nothing else is added. `refinementLog` reaches NEITHER item: it is the search trace, and it names
 * both the control set — other tenants' runs — and the tools a gateway reported failing, where a
 * tool name can come from a user-registered MCP server.
 */
export interface ResidueEvidence extends ResidueEntry {
  runId: string;
}

/**
 * A failure diverted because the PLATFORM was degraded during its window. NOT "unexplained"
 * and NOT an observability gap: we saw the run fine, but an account-wide outage underneath
 * the agent means we stand down from attribution. Routed to ops / incident review, never to
 * the proposer or the defect register, so it carries its own wording in the UI.
 */
export type EnvironmentEntry = ResidueEntry;

/**
 * An ADMITTED pattern that no Σ component owns.
 *
 * The opposite failure from `ResidueEntry`: the pattern DID survive discrimination, so it
 * really does separate failing runs from passing ones — and then attribution found nothing in
 * the registry to hang it on, so no suggestion could be written. Keyed on the shape rather than
 * on a run, because grouping happens before attribution.
 *
 * Shown so the count is honest. "15 failures seen, 8 not yet attributable, and here is why" is
 * a true claim; a panel that lists only the attributable ones implies coverage it does not have.
 */
export interface UnattributedPattern {
  /** Facet names joined, the same spelling `FailurePattern.shape` uses. */
  shape: string;
  /** Facet names. These are the input to the map-or-refuse decision this bucket exists to
   *  inform: whether each unmapped facet gets registry search terms or gets refused at
   *  admission. */
  shapeFacets: string[];
  /** How many runs carry this shape. The ids reach their owners, on their own bundles. */
  runCount: number;
  /**
   * WHY nothing owns it, in the engine's own words — which facets have no registry search
   * terms, or that terms exist and matched no component, plus whether the registry was
   * reachable at all. A template over facet names and a registry-availability flag, so unlike a
   * residue `reason` it interpolates nothing tenant-derived and can travel on the shared item.
   * Carries facet names, so this is the ADMIN wording (Findings), not the Inbox's.
   */
  reason: string;
  /**
   * The same fact in plain English, for the user-facing Inbox. Unlike
   * `ResidueEntry.plainReason` the producer DOES supply this one, and the difference is not
   * effort: mining's reasons are free-form, while an unowned pattern has exactly three
   * possible causes that the producer already branches on, so its plain sentence is a
   * translation of an enum rather than a second claim about what went wrong.
   *
   * Still optional, for the same deploy-order reason as everything else here — a bundle
   * written before the cause label existed has none. Render `plainReason ?? reason`.
   */
  plainReason?: string;
}

export interface PatternsResponse {
  admitted: FailurePattern[];
  residue: ResidueEntry[];
  /** Failures we could not observe at all, an observability defect, not a behaviour
   *  to change. Routed to the defect register. */
  evidenceGaps: ResidueEntry[];
  /** Failures under a degraded platform, not the agent's fault. Routed to ops. */
  environment: EnvironmentEntry[];
  /**
   * Admitted patterns nothing in the registry owns. OPTIONAL for the same deploy-order reason
   * as `ImproveData.scorecard`: the producer is a scheduled Lambda and this SPA is a separate
   * artifact, so a UI build can ship first and must not be typed to a key the served SNAPSHOT
   * does not carry yet.
   *
   * So branch on `undefined` rather than reaching through it, and do not collapse the two
   * cases: `undefined` means "this bundle predates the key", `[]` means "the producer ran and
   * every admitted pattern found an owner". Only the second is a measurement.
   */
  unattributed?: UnattributedPattern[];
}

// --- this actor's own evidence ----------------------------------------------
/**
 * THE SECOND ITEM. Everything above describes the shared bundle, one DynamoDB row at
 * `actorId="__system__"` that every actor reads; everything below comes from the reader's OWN
 * partition and is served only to the holder of their token.
 *
 * That is the whole reason these types are separate. The shared item may carry numbers, counts,
 * enum values and harness NAMES — nothing derived from a user's session, no judge prose, no
 * session id. This one carries exactly those, for one actor, about their own runs. If a field
 * appears in both halves, one of the two is wrong.
 *
 * OPTIONAL on the wire and usually absent. The producer is a daily cron over a bounded harvest
 * window, so after any deploy there is a window where nobody has one, and an actor with no
 * conversational session never has one at all. Branch on `undefined` rather than reaching through
 * it, and render the shared answer alone — never a blank surface.
 */

/** The per-actor half of a mined pattern: which of THIS actor's runs, and what was observed. */
export interface PatternEvidence {
  /** The same facet names the shared row carries, so the two can be lined up. */
  shapeFacets: string[];
  /** This actor's member runs only. A pattern spanning two actors appears on both bundles, each
   *  listing its own. */
  memberRunIds: string[];
  /**
   * The facet evidence behind the FIRST run listed — plan node ids, span indices, timestamps.
   * One run's detail, which is what this field has always been; the difference is that the run is
   * now guaranteed to be one the reader owns. `memberRunIds.length` says how many there are.
   * Empty string when no facet evidence was recorded, never the shape prose: the shared row
   * already carries that, and repeating it would make absence look like an explanation.
   */
  rootCauseHypo: string;
}

/** One waste finding WITH the tool sequence named. See `WasteTotals` for why the shared item
 *  carries only counts: the argument is about who reads it, and here the reader owns the run. */
export interface WasteFinding {
  runId: string;
  /** `repeated_sequence` | `abandoned_dispatch` | `error_then_retry`. */
  kind: string;
  /** Tool calls charged. For a repeat this is the earlier occurrence(s). */
  calls: number;
  /** `"UNMEASURED"` when any contributing span recorded no duration — never a partial sum. */
  wallMs: Measured<number>;
  occurrences: number;
  /** The named sequence, e.g. `run_model x6 -> dispatch_tool`, truncated with `+N more`. */
  detail: string;
  /** What this kind can and cannot establish. Render it: spans carry no tool arguments, so a
   *  second run over different inputs is indistinguishable from a redo. */
  confidence: string;
}

export interface ActorWasteTotals extends WasteTotals {
  findings: WasteFinding[];
}

/**
 * How much of a period each of its spend terms was measured on.
 *
 * The pair `MeasuredSum` carries per term (`measuredOf`/`total`), factored out of the terms and onto
 * the period — not reshaped, so `coverage.tokens of coverage.total` reads exactly as
 * `spend.tokens.measuredOf of spend.tokens.total` and every scalar term keeps being a plain number or
 * a sentinel.
 *
 * PER TERM rather than one number for the block, and that is not a nicety. `latencyMs` is summed from
 * spans while every token term comes off the usage record, so a session can be measured for one and
 * not the other — `latencyMs 2/2` beside `tokens 1/2` on the same day is a measured shape, and a
 * single count would caption both wrongly.
 *
 * Two invariants hold across every field, and both are what let a caption be built from them without
 * branching:
 *
 *  - NEVER a sentinel. Every value is an integer and `0` is a measured zero.
 *  - never `0 of 0`. A day whose sessions all recorded nothing reports `0` per term against its REAL
 *    session count in `total`, so a fraction over it says "none of five" rather than "nothing to
 *    measure".
 *
 * Whole-block absence is still possible, and is the one case to branch on: a row written before this
 * existed carries no `coverage` at all, and a sum over such rows cannot state what it covers.
 */
export interface DayCoverage {
  /**
   * The denominator: how many SESSIONS the period held.
   *
   * Deliberately not the same number as a per-actor row's `runs`, which counts REQUESTS — a request
   * that fanned out into plan sub-sessions is one run and several sessions, so a real day reads
   * `runs: 17, coverage.total: 19`. They answer different questions and both are rendered: `runs` says
   * how much of the reader's own activity a total is drawn from, and this is what a coverage fraction
   * is out of.
   */
  total?: number;
  tokens?: number;
  llmCalls?: number;
  latencyMs?: number;
  cost?: number;
  costTokensPriced?: number;
  costTokensUnpriced?: number;
  /** Nested on BOTH rows, even though the population row's category SUMS are flat: this block is new,
   *  so it had no deployed shape to preserve and could be spelled the same way on each. */
  byCategory?: {
    input?: number;
    output?: number;
    cacheRead?: number;
    cacheWrite?: number;
  };
}

/**
 * One period's spend, whether that period is a single day or a bag of evicted ones.
 *
 * Split out from the two shapes below because they carry exactly the same terms and differ only in
 * how they say WHICH days they are about. A reading that sums them must treat them identically, and
 * a second copy of these six fields is how one of them would come to be summed and the other
 * dropped.
 *
 * Every term is OPTIONAL, and that is a third state rather than caution. `undefined` means the
 * producer did not put this term on the row at all — a row written before it existed — and is the
 * one case that must never render as a figure. A sentinel means the producer wrote the row and could
 * not measure the term. A number, `0` included, is a measurement. The surface renders all three
 * differently, so nothing here may collapse them with `?? 0`.
 */
export interface DaySpend {
  /**
   * How many runs the period holds: the extent every cumulative footnote is stated against.
   *
   * NOT the divisor of a per-session figure, which is `coverage`'s own count. This counts REQUESTS —
   * see `DayCoverage.total` — so dividing a period's tokens by it answers per REQUEST and reads 12%
   * high on a day whose requests fanned out.
   */
  runs?: Measured<number>;
  tokens?: Measured<number>;
  llmCalls?: Measured<number>;
  latencyMs?: Measured<number>;
  cost?: Measured<number>;
  /** The four separately-priced categories, summed over the period. `input` is UNCACHED input, as
   *  on a run row, so the four add to `tokens` exactly and a share over them needs no residual. */
  byCategory?: {
    input?: Measured<number>;
    output?: Measured<number>;
    cacheRead?: Measured<number>;
    cacheWrite?: Measured<number>;
  };
  /** Which model ids the period's tokens went to. A LIST and never a count, because the count is
   *  over a set and a set cannot be recovered from per-day counts. `"UNMEASURED"` and never `[]`
   *  when nothing named one — an empty list would say the period used no model. */
  models?: Measured<string[]>;
  /**
   * How many of the period's tokens the rate table could and could not price.
   *
   * `cost`'s second denominator, and the only thing that can say a total is SHORT. Pricing is per
   * model and skips a model with no rate row rather than failing the session, so a period can be
   * fully covered by run and still be missing whatever that model spent. Both halves sum, so a
   * rolled-up bucket carries them too and the clause works at every period.
   *
   * These are DENOMINATORS, so `0` is a measurement rather than absence: a session that recorded no
   * usage spent nothing and skipped nothing, and contributes `0` to both halves. Which is why a
   * `costTokensUnpriced` of `0` must NOT be read as "the bill is complete" on its own — that is only
   * true when `cost` beside it is a number. A fully unmeasured day carries `cost: "UNMEASURED"` with
   * `0` and `0`, and the honest reading of that is that nobody priced anything and nobody skipped
   * anything.
   *
   * Flat here, on both the per-actor and the population row, even though the scorecard nests the same
   * pair inside `spend.cost`. Every term on a day row is flat and the two rows use the same names, so
   * one summing rule serves both.
   */
  costTokensPriced?: Measured<number>;
  costTokensUnpriced?: Measured<number>;
  /**
   * How many of the period's sessions recorded each term above.
   *
   * Declared here because the producer writes it onto the row beside those terms. Absent on any row
   * written before it existed, which is the case a reader of it has to branch on — see `DayCoverage`.
   */
  coverage?: DayCoverage;
}

/**
 * How a period's REQUESTS landed, as three counts that partition `runs`.
 *
 * Declared once and carried by a day row and by the rolled-up bucket alike, because the three sum
 * across days exactly as every spend term does — which is the reason they are counts and not a rate.
 * A rate needs its weight carried beside it and cannot be recomputed from the row if that weight is
 * ever wrong, so the division belongs at read time over whatever span was asked for. `pass / (pass +
 * fail)` is that division, and it excludes `unscored` for the reason below.
 *
 * Three properties, and a reading that drops any of them says something false:
 *
 *  - `pass + fail + unscored === runs` on a row the current producer wrote. A fanned-out request
 *    takes its parent's verdict: the unit is the request, so a plan sub-session contributes its
 *    tokens to the day and no outcome of its own. `coverage.total` beside them counts SESSIONS, so
 *    `runs: 6, coverage.total: 13` is a real day with seven sub-sessions in it.
 *  - `unscored` is a THIRD count and never folds into either of the others. A gating evaluator that
 *    could not measure says something about the evidence and nothing about the agent, so it is
 *    neither a pass nor a failure — the same rule `Scorecard.population` and `passRate` hold.
 *  - ALL THREE ABSENT means no outcome was recorded for that period, which is every row written
 *    before the producer emitted them. `0` is a measured zero — the day was scored and nothing
 *    landed in that bucket — so absence must render as "no outcome recorded" and never as three
 *    zeros or a 0% rate. There is nothing to back-fill from: `aws/spans` retains 30 days and each
 *    row is the only copy of its day, so a bucket folding rows from either side of the change
 *    reports a partition SHORT of its `runs`, and that shortfall is the honest answer.
 */
export interface DayOutcome {
  pass?: number;
  fail?: number;
  unscored?: number;
}

/**
 * One day of this actor's own activity: what they ran, whether it worked, and what it cost.
 *
 * The row is the unit of storage as well as of display, which is what makes a re-run idempotent: a
 * producer pass over an already-recorded day REPLACES that day's row rather than adding to a running
 * total, so the same day mined twice moves no figure. Cumulative and trailing are then two sums over
 * the same rows and the display is not a storage decision.
 */
export interface ActorDay extends DaySpend, DayOutcome {
  /** `YYYY-MM-DD`, UTC. The only thing that places a row in time, so a row without one cannot be
   *  read at all — not even as an unmeasured day. */
  date: string;
}

/**
 * The days retention has evicted, summed into one bucket.
 *
 * Without it "all time" would silently mean "as far back as retention reaches", which is the
 * failure a cumulative total exists to end: a figure that shrinks as old rows fall off the end
 * while presenting itself as everything. The date pair and the day count are what let the basis label
 * say how far back the bucket goes rather than implying the first retained row is the beginning.
 *
 * ONE type for two buckets — an actor's and the population's — because the fold is one mechanism, so
 * `DayOutcome` rides along: an evicted per-actor day row carries those counts and they sum like any
 * other, while the population's bucket folds rows that never had them and leaves all three absent,
 * which `DayOutcome` already defines as no outcome recorded. The population's own rate travels as
 * `PopulationDay.passRate` and is folded weighted, not summed.
 */
export interface RolledUpDays extends DaySpend, DayOutcome {
  /** `YYYY-MM-DD` of the earliest evicted day, and of the latest. */
  fromDate?: string;
  toDate?: string;
  /** How many days the bucket stands for. */
  days?: Measured<number>;
}

export interface ActorBundle {
  /** Newest first. Shorter than `runsTotal` when the bundle had to be trimmed to fit one item. */
  runs: ActorRun[];
  /** How many runs this actor really had in the population — the denominator for `runs.length`,
   *  so a trim reads as a trim and not as a smaller population. */
  runsTotal: number;
  /**
   * Keyed by `Suggestion.suggestionId`. A suggestion with NO entry here is one none of this
   * actor's runs support — the ordinary case on a shared harness. There is no empty state to
   * render: the shared suggestion's own `evidenceQuality` already says how much evidence exists
   * and caveats it.
   */
  evidence: Record<string, PatternEvidence>;
  /**
   * This actor's findings and their totals. Denominators are THIS actor's population, not the
   * platform's, so these numbers are not a slice of `ImproveData.waste`.
   *
   * OPTIONAL because the wire can omit it. Today's producer requires the key
   * (`aws_lambda._REQUIRED_ACTOR_BUNDLE_KEYS`) and every live item carries one, but an item stored
   * before the efficiency path existed is served back unchanged — and the difference matters: no
   * `waste` key means nobody looked, while `waste` with no findings means somebody looked at a named
   * number of runs and found nothing. Declared required, the reader that already branches on
   * `undefined` had to be written against a type that said it could not.
   */
  waste?: ActorWasteTotals;
  patterns: {
    admitted: PatternEvidence[];
    residue: ResidueEvidence[];
    evidenceGaps: ResidueEvidence[];
    environment: ResidueEvidence[];
    unattributed: PatternEvidence[];
  };
  /**
   * When the producer last wrote THIS actor's bundle. Absent when the stored item carried no
   * stamp. It can legitimately be older than the shared bundle's: the global one refreshes every
   * run, an actor's only refreshes when they had a session in the harvest window, so a reader
   * comparing the two is seeing real staleness rather than an error.
   */
  createdAt?: string;
  /**
   * One row per day this actor had activity, OLDEST FIRST.
   *
   * The reason the personal figures can be totals at all. `runs` above is one harvest window, so a
   * reader who had no session yesterday has an empty list there and every personal figure reported
   * itself unavailable — on a shared harness that is most readers most days, which made the empty
   * state the common state. These rows accumulate instead: the producer reads the item it is about
   * to replace and carries them forward.
   *
   * OPTIONAL, and its absence is the one thing that must not be read as a zero. The producer is a
   * daily cron and this SPA is a separate artifact, so after any deploy there is a window where the
   * key is not there yet, and a bundle written before it never gains one. `[]` is a MEASUREMENT —
   * the producer swept this actor and found no day of activity — and the two render differently:
   * `[]` shows `0`, absent shows the hatched sentinel.
   */
  history?: ActorDay[];
  /** The days retention evicted, folded into one bucket, so an all-time total does not quietly
   *  shrink to what retention reaches. Absent when nothing has been evicted yet. */
  rolledUp?: RolledUpDays;
  /**
   * `YYYY-MM-DD` of the earliest day any figure here covers, `rolledUp` included.
   *
   * The basis label's own source. "All time" over rows that only reach back a fortnight is not all
   * time, and the reader cannot tell the difference from the number — so the label states this date
   * and lets a total be read for what it is. Absent when the bundle carries no history at all, and
   * therefore never a stand-in for "the beginning".
   */
  historyFirstDate?: string;
}
