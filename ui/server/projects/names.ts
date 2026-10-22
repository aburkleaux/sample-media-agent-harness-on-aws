// Project name rules (media-harness-projects-access Req 1.2; design Component 1 "Name rules", D9).
// A name is trimmed, then checked in a fixed order; only the first violated rule is reported.
// Length counts Unicode code points (`[...s].length`), so an emoji counts as 1 (D9).

export const MAX_PROJECT_NAME_CODE_POINTS = 64;

/** The fold reserved for the Personal_Project. */
export const PERSONAL_FOLD = "personal";

/** Rule wording, in check order, taken from Req 1.2. */
export const NAME_RULES = {
  empty: "Project name must not be empty after trimming",
  tooLong: `Project name must not be longer than ${MAX_PROJECT_NAME_CODE_POINTS} characters`,
  controlCharacter: "Project name must not contain a Unicode control character",
  reserved: "Project name must not equal `Personal` case-insensitively",
  duplicate:
    "Project name must not equal case-insensitively the name of another Project owned by the same Project_Owner",
} as const;

export type NameRule = (typeof NAME_RULES)[keyof typeof NAME_RULES];

export interface InvalidNameError {
  code: "invalid_name";
  rule: NameRule;
}

const CONTROL_CHAR_RE = /\p{Cc}/u;

/** Case-insensitive comparison key for Project names. */
export function foldName(s: string): string {
  return s.normalize("NFC").toLowerCase();
}

/**
 * Validate a submitted Project name for create or rename.
 *
 * `ownerExistingFolds` holds the folds of the other names the same Owner already holds (for a
 * rename, the caller leaves out the Project's own current name). Returns the trimmed name when
 * every rule holds, otherwise the first violated rule.
 */
export function validateProjectName(
  raw: string,
  ownerExistingFolds: ReadonlySet<string>,
): string | InvalidNameError {
  const name = raw.trim();
  if (name.length === 0) return invalid(NAME_RULES.empty);
  if ([...name].length > MAX_PROJECT_NAME_CODE_POINTS) return invalid(NAME_RULES.tooLong);
  if (CONTROL_CHAR_RE.test(name)) return invalid(NAME_RULES.controlCharacter);
  const fold = foldName(name);
  if (fold === PERSONAL_FOLD) return invalid(NAME_RULES.reserved);
  if (ownerExistingFolds.has(fold)) return invalid(NAME_RULES.duplicate);
  return name;
}

function invalid(rule: NameRule): InvalidNameError {
  return { code: "invalid_name", rule };
}
