/**
 * The reader's vocabulary for what the loop measured and what the registry permits: check names,
 * workload classes, outcomes, the structural verdict string, and a component's permission.
 *
 * One module rather than a copy in each block, for the reason `improveChrome`'s formatters are
 * shared: the population scorecard names a check that FAILED, a run's own row names the same check
 * as a requirement it MET, and the two must not phrase it differently — a reader comparing the two
 * would take them for different checks.
 *
 * Nothing here is prose the producer wrote. Everything the wire carries in this area is an
 * identifier — `results_delivered`, `plan_node`, `NO_DATA`, `gating: results_delivered:FAIL` — and
 * an identifier on screen reads as a leak. The one piece of real prose in this area, the judge's
 * `TierScore.explanation`, is a sentence written for a human and is shown as written; it is
 * deliberately not passed through anything here.
 *
 * A name this module has no phrase for is named as unrecognised rather than dressed up: the producer
 * can add an evaluator and ship before this file does, and a blank cell would read as a render fault.
 */
import type { Outcome, Overall } from '../../improve-api';
import { sharePercent } from './improveChrome';

/**
 * What each evaluator asserts, and what it means when it did not hold.
 *
 * Keyed by the evaluator's own name, which is what the wire carries in four separate places —
 * `Scorecard.outcomes.failedGating`, `Scorecard.outcomes.unmeasured`, `WorkloadClassScore.gating`
 * and inside `ActorRun.verdict.reason`.
 *
 * `label` is the requirement in the affirmative and `failed` is its negation, rather than one string
 * used for both: "5 requests — delivered a usable result" reads as five successes.
 *
 * The wording follows what each evaluator measures (`agent/src/improve/evaluators_improve.py`). The
 * DAG check is the one worth reading twice: it fails on a node that never reached terminal success
 * AND on a node that completed before a dependency it consumes, which is why its phrase names both.
 * The two judged tiers are keyed here under their evaluator names, not under the shorter
 * `dispatch`/`delivery` the scorecard nests them as.
 */
const CHECK: Record<string, { label: string; failed: string }> = {
  dispatch_correct: {
    label: 'started the right work',
    failed: 'the wrong work was started, or none',
  },
  results_delivered: {
    label: 'delivered a usable result in the session',
    failed: 'no usable result reached the user',
  },
  DagCompletionValidator: {
    label: 'every planned step finished, and after what it depends on',
    failed: 'a planned step failed, stalled, or ran ahead of what it depends on',
  },
  AsyncJobFlowValidator: {
    label: 'every job it started was followed up',
    failed: 'a job it started was never followed up',
  },
  ToolDiscoveryValidator: {
    label: 'the tool handshakes succeeded',
    failed: 'a tool handshake failed',
  },
  ControlPlaneOpValidator: {
    label: 'the control-plane operation succeeded',
    failed: 'the control-plane operation failed',
  },
};

/** The reader's phrase for a check, or one saying this screen has none. The identifier survives
 *  inside that phrase because a reader reporting an unphrased check needs to be able to name it. */
export function checkLabel(name: string, key: 'label' | 'failed' = 'label'): string {
  const known = CHECK[name];
  if (known) return known[key];
  return `a check this screen has no phrase for (${name || 'unnamed'})`;
}

/**
 * Permission, in words. The registry's own vocabulary is SHOUTY_SNAKE_CASE, `AUTO_CANDIDATE`,
 * `LOCKED_TODAY`, `FIX_FIRST`, and printing it put six enum spellings in a column that was
 * not a column. `improveChrome`'s disposition badges already speak plain English about the same
 * concept, so the screen was speaking two languages about one idea.
 *
 * Keyed on `string` rather than on `Mutability` deliberately, because the registry carries values
 * that union does not name and the producer passes them through verbatim (`component.mutability or
 * "UNRECORDED"`): `N/A` on three components, `n/a` on a fourth, `BUG` and `INERT` on one each. Only
 * the first reaches a served row today. What is NOT here is a translation invented for the rest —
 * see `permissionLabel`, which answers `undefined` so each caller can show an untranslated value as
 * the registry spells it.
 *
 * HERE rather than beside either caller, because two surfaces name one component's permission — the
 * Σ map's row and a suggestion's card — and a screen that phrased the same registry value two ways
 * would read as two different permissions.
 */
const PERMISSION_LABEL: Record<string, string> = {
  THE_ACTUATOR: 'you can edit',
  AUTO_CANDIDATE: 'you can edit',
  REVIEW: 'needs review',
  MANUAL: 'recommend only',
  LOCKED_TODAY: 'locked for now',
  LOCKED: 'locked',
  PROTECTED: 'protected',
  FIX_FIRST: 'needs an engineer',
  UNRECORDED: 'not assessed',
  // Assessed, and the answer was that permission does not APPLY: `dag-node-prompts` is generated
  // per run by the design agent, so there is no stored artifact to permit or protect. The opposite
  // claim to `UNRECORDED` above, which is why the two may not share a word. Both spellings, because
  // the registry uses both and neither is this screen's to normalise.
  'N/A': 'not applicable',
  'n/a': 'not applicable',
};

/**
 * The reader's phrase for a registry permission, or `undefined` when this screen has none.
 *
 * `undefined` rather than a fallback phrase, unlike everything else in this module: a permission is
 * the one value here where guessing changes what a reader believes they may do, and the fallback
 * used to be `UNRECORDED`'s own wording — so every value off the union was reported as the one state
 * default-deny is ABOUT. Each caller shows an untranslated value as the registry spells it, which
 * reads as untranslated rather than as a decision.
 */
export function permissionLabel(mutability: string): string | undefined {
  return PERMISSION_LABEL[mutability];
}

/**
 * What each workload class IS, in a reader's words.
 *
 * `plan_node` is the one that matters: those are steps inside a plan rather than requests somebody
 * made, so a population mixing them with conversational requests is counting two different things.
 */
const CLASS_LABEL: Record<string, string> = {
  conversational: 'requests somebody made',
  plan_node: 'steps inside a plan',
  tool_discovery: 'tool-discovery handshakes',
  control_plane: 'control-plane operations',
  unclassified: 'work nothing could classify',
};

export function classLabel(name: string): string {
  return CLASS_LABEL[name] ?? `a kind of work this screen has no phrase for (${name || 'unnamed'})`;
}

/**
 * A single check's outcome as a word.
 *
 * `NO_DATA` and `NOT_APPLICABLE` are kept apart because the engine keeps them apart and the
 * difference decides a verdict: the first means the dimension applies and its evidence is missing,
 * which blocks a pass; the second means the dimension does not exist for this run and cannot block
 * anything. Spelled lowercase, matching `SentinelValue`, which already renders `NO_DATA` as
 * `no data` rather than as itself.
 */
export function outcomeWord(outcome: Outcome | string): string {
  switch (outcome) {
    case 'PASS': return 'met';
    case 'FAIL': return 'not met';
    case 'NO_DATA': return 'no evidence';
    case 'NOT_APPLICABLE': return 'does not apply';
    default: return `an outcome this screen has no phrase for (${outcome || 'unnamed'})`;
  }
}

/** A run's overall verdict: the word, and what the word rests on. `UNSCORED` is emphatically not a
 *  soft pass, so it gets its own word and its own caveat rather than sharing either. */
export const VERDICT: Record<string, { word: string; title: string }> = {
  PASS: {
    word: 'passed',
    title: 'Every check that decides this kind of work measured a pass.',
  },
  FAIL: {
    word: 'failed',
    title: 'A check that decides the verdict measured a failure.',
  },
  UNSCORED: {
    word: 'not judged',
    title: 'No deciding check measured a failure and at least one could not measure at all. '
      + 'Absent evidence is not a failure, and this is not a pass.',
  },
};

export function verdictWord(overall: Overall | string): string {
  return VERDICT[overall]?.word ?? `a verdict this screen has no phrase for (${overall || 'unnamed'})`;
}

/** `0.94`, and `0.00` for a measured zero — which is a score and not an absence. */
export function formatScore(score: number): string {
  return Number.isFinite(score) ? score.toFixed(2) : 'not a score';
}

/**
 * A pass rate as a share, or a refusal for a figure that is not one.
 *
 * The share is `sharePercent`'s, rule and reasoning both — nothing about the formatting is decided
 * here. What is decided here is the refusal, which the shared rule has no equivalent of: it does not
 * validate on purpose, since a caller holding a figure that cannot be a share still wants to read
 * the figure, so it renders whatever it is handed into the label — `NaN%`, which looks enough like a
 * reading to be taken for one. Both callers have already branched on the wire's `null`, the producer
 * saying nothing was judged, so what is left for this one to refuse is a number that is not finite.
 */
export function formatRate(rate: number): string {
  if (!Number.isFinite(rate)) return 'not a rate';
  return sharePercent(rate);
}

/** What a parsed `verdict.reason` says, in the reader's words. */
export interface ExplainedVerdict {
  /**
   * Whether the string matched the producer's grammar at all.
   *
   * Separate from `sentence` because "recognised, and it adds nothing to the verdict word" and "not
   * recognised" are different facts with different renderings: the first is silence, the second has
   * to be stated, since the alternative — falling back to the wire's own wording — puts
   * `gating: results_delivered:FAIL` on screen, which is the one outcome this module prevents.
   */
  recognised: boolean;
  /** Why the verdict is what it is, or `null` when the recognised form adds nothing to the verdict
   *  word beside it. */
  sentence: string | null;
  /** Checks recorded on this run that did NOT decide its verdict, with their outcomes. */
  informational: { name: string; outcome: string }[];
  /** Deciding checks whose evidence was missing while a sibling measured a failure — the reason a
   *  failure can rest on partial evidence, which is weaker than one resting on all of it. */
  partial: string[];
}

const INFORMATIONAL = /\(informational: ([^)]*)\)/;
const PARTIAL = /\(on partial evidence: ([^)]*)\)/;
const NOT_APPLICABLE_LIST = /every gating evaluator reported NOT_APPLICABLE: ([^)]*)/;

/** `name:OUTCOME` pairs, however the producer joined them — `; ` between gating failures and `, `
 *  between informational ones. Split on both so one parser serves every clause. */
function parsePairs(list: string): { name: string; outcome: string }[] {
  return list.split(/[;,]/)
    .map((entry) => entry.trim())
    .filter((entry) => entry.length > 0)
    .map((entry) => {
      const at = entry.lastIndexOf(':');
      return at < 0
        ? { name: entry, outcome: '' }
        : { name: entry.slice(0, at).trim(), outcome: entry.slice(at + 1).trim() };
    });
}

/**
 * Translate `Verdict.reason` into a sentence a reader can act on.
 *
 * The string is STRUCTURAL, not prose: `gate()` in `agent/src/improve/evaluate.py` builds it from
 * evaluator names and outcome enums, in one place, in four shapes —
 *
 *   `gating: <name>:FAIL[; ...][ (on partial evidence: <name>:NO_DATA[, ...])]`
 *   `could not measure: <name>:NO_DATA[; ...]. Absent evidence is not a failure; …`
 *   `nothing scorable (no gating evaluator was configured)`
 *   `nothing scorable (every gating evaluator reported NOT_APPLICABLE: <name>[, ...])`
 *   `gating passed[ (informational: <name>:<OUTCOME>[, ...])]`
 *
 * — so parsing it is reading a format, not guessing at prose.
 *
 * A PASS with nothing informational is recognised with `sentence: null` deliberately.
 * `gating passed` says exactly what the verdict word beside it already says, and a second copy of it
 * on every passing row is noise.
 */
export function explainVerdict(reason: string): ExplainedVerdict {
  const informational = INFORMATIONAL.exec(reason);
  const partial = PARTIAL.exec(reason);
  const parsed: ExplainedVerdict = {
    recognised: false,
    sentence: null,
    informational: informational ? parsePairs(informational[1]) : [],
    partial: partial ? parsePairs(partial[1]).map((pair) => checkLabel(pair.name)) : [],
  };

  if (reason.startsWith('gating: ')) {
    // The failing names only: the partial-evidence clause is a separate fact and travels separately.
    const failures = parsePairs(reason.slice('gating: '.length).replace(PARTIAL, ''));
    if (failures.length > 0) {
      parsed.recognised = true;
      parsed.sentence = failures.map((pair) => checkLabel(pair.name, 'failed')).join('; ');
    }
    return parsed;
  }

  if (reason.startsWith('could not measure: ')) {
    const missing = parsePairs(reason.slice('could not measure: '.length).split('.')[0]);
    if (missing.length > 0) {
      parsed.recognised = true;
      parsed.sentence = 'nothing measured whether '
        + missing.map((pair) => checkLabel(pair.name)).join('; ');
    }
    return parsed;
  }

  if (reason.startsWith('nothing scorable')) {
    if (NOT_APPLICABLE_LIST.test(reason)) {
      parsed.recognised = true;
      parsed.sentence = 'none of the checks for this kind of work applied to it, so nothing could '
        + 'pass or fail';
    } else if (reason.includes('no gating evaluator was configured')) {
      parsed.recognised = true;
      parsed.sentence = 'no check was configured to judge this kind of work, so nothing could pass '
        + 'or fail';
    }
    return parsed;
  }

  if (reason.startsWith('gating passed')) {
    // Recognised, with nothing to add — see this function's note.
    parsed.recognised = true;
    return parsed;
  }

  return parsed;
}
