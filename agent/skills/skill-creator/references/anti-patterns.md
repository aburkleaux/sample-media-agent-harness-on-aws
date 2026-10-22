# Skill Anti-Patterns

Common smells in SKILL.md files, grouped by what they break. Each
entry has a **symptom**, a **why it matters**, and a **fix**.

When reviewing a skill, scan for these in order. Use them as a
checklist alongside the spec rules in [spec.md](spec.md).

## Frontmatter smells

### Vague description

**Symptom:**

```
description: Helps with PDFs.
description: Useful for code review.
description: A tool for working with data.
```

**Why it matters:** The agent matches the user's prompt against
the description at startup. "Helps with PDFs" matches "I have a
PDF" equally well as it matches "what is a PDF" — there's no
signal for the model to discriminate. Vague descriptions cause
the skill to trigger on adjacent-but-wrong requests, or fail to
trigger on the right ones.

**Fix:** Restructure the description as **what + when + triggers**:

```
description: Extracts text and tables from PDF files, fills PDF forms, and merges multiple PDFs. Use when the user asks to read a PDF, extract content, fill a form, or combine documents.
```

The three trigger phrases ("read a PDF", "extract content",
"combine documents") give the model concrete patterns to match.

### Description over 1024 characters

**Symptom:** The description carries a full method statement,
multiple paragraphs, or a list of capabilities.

**Why it matters:** Spec violation. The description is metadata
loaded at startup for *every* installed skill, on *every* turn.
Bloating it costs tokens for no benefit — the agent doesn't read
the description's content beyond trigger matching.

**Fix:** Trim to one or two sentences. Push detail into the
body, where it's only paid for when the skill activates.

### Name doesn't match parent directory

**Symptom:**

```
agent/skills/pdf-tools/SKILL.md:
---
name: pdf-processing
---
```

**Why it matters:** Hard spec violation. The validator rejects
this; some runtimes refuse to load the skill at all.

**Fix:** Rename the directory or the field so they match. Choose
the canonical slug first — the directory is the source of truth
on disk, the field is the source of truth in the protocol; they
must agree.

### Compatibility set without a real requirement

**Symptom:**

```
compatibility: Works with any agent
compatibility: Compatible with Claude and similar models
```

**Why it matters:** The spec says most skills don't need
`compatibility`. Adding it with non-information adds token cost
to metadata for zero discrimination value.

**Fix:** Remove the field unless the skill genuinely requires
something specific (a system package, a Python version, network
access, a particular product).

## Body smells

### Wall-of-text body

**Symptom:** SKILL.md is 700+ lines. The user is asked to wait
on a slow first activation.

**Why it matters:** Spec recommends under 500 lines / 5000
tokens. The body is loaded in full on every activation, so the
cost is paid repeatedly. Beyond the cost, large bodies have
worse signal-to-noise — the agent struggles to find the
relevant section.

**Fix:** Move the bulk into `references/*.md` files and link
from the body. The body should orient ("here's the structure,
here's the decision tree, look at references/x.md for the
details"); references should hold the detail.

### Fabricated tool references

**Symptom:**

```
Use the `pdf_extract_tables` tool to extract tables from the PDF.
```

…but `pdf_extract_tables` does not exist in the runtime.

**Why it matters:** When the agent activates the skill and tries
to call the named tool, the call fails — usually with a generic
"tool not found" error that doesn't immediately implicate the
skill. The user sees a broken behavior with no clear cause.

**Fix:** When authoring, verify every named tool against the
runtime's actual tool list. When reviewing, pick the most
specific tool name from the body and confirm it appears in
`_all_builtin_tools` (built-ins) or the gateway tool list. If a
tool was renamed, update the skill. If the desired tool doesn't
exist, either pick a different approach or open a backlog entry
to add it.

### Future-state language

**Symptom:**

```
This skill will eventually support OCR for scanned PDFs.
A future version will integrate with the cloud watch metrics tool.
```

**Why it matters:** Aspirational text helps no one. The agent
can't act on it; the user already trusts the skill to do what
the body says; you're just spending tokens on documentation
that doesn't change behavior.

**Fix:** Strip future-state text. If a capability isn't shipped,
it doesn't belong in the skill. Track it in a backlog entry
instead.

### Missing negative triggers

**Symptom:** The skill has activation criteria but no "do NOT
activate when…" section.

**Why it matters:** Skills that share keywords with adjacent
intents will fire on the wrong requests. Example: a
`code-review` skill that fires on "what does this code do"
(which is reading, not reviewing). Without negative triggers,
the model has no way to discriminate.

**Fix:** Add at least two negative triggers per skill. The
authoring Q&A flow in [qa-flow.md](qa-flow.md) Round 1 forces
this — apply it during review too.

### Duplicated content between body and references

**Symptom:** SKILL.md body has a "Frontmatter Rules" section
that restates the same content found in `references/spec.md`.

**Why it matters:** Tokens spent twice for the same information.
Worse, the two copies drift over time, and the agent has to
guess which one is authoritative.

**Fix:** Keep one source of truth. The body orients ("see
`references/spec.md` for the full rules"); the reference holds
the rules. When reviewing, look for repeated section headers
between the two files.

### Imperative voice inconsistent

**Symptom:** The body mixes voices:

> The agent must read the file. You should validate the output.
> This skill processes images.

**Why it matters:** Strands skills are read *as if* the agent is
the addressee — the body's instructions become the agent's
mental model. Mixing "you" (addressed) with "the agent" (third
person) with "this skill" (passive) makes the instructions
harder to follow.

**Fix:** Pick one voice and stick with it. The convention in
this repo (`agent/skills/`) is **direct address** — "You read
the file… You validate the output…" — matching the system
prompt style.

## Structure smells

### Reference files nested too deeply

**Symptom:**

```
my-skill/
├── SKILL.md
└── references/
    └── topics/
        └── advanced/
            └── error-handling.md
```

**Why it matters:** Spec says one level deep from SKILL.md.
Deeper nesting confuses the agent (relative paths matter) and
the validator may reject it.

**Fix:** Flatten. Either inline the content or rename:

```
references/advanced-error-handling.md
```

### Scripts that aren't scripts

**Symptom:** `scripts/` contains Markdown files, configuration
templates, or pseudocode.

**Why it matters:** Spec convention: `scripts/` is for code the
agent runs. Markdown belongs in `references/`. Templates belong
in `assets/`. Putting non-executable content in `scripts/`
breaks the convention and confuses agents that try to execute
what they find there.

**Fix:** Move content to the right directory.
- Documentation → `references/`
- Templates / fixtures → `assets/`
- Real executable code → `scripts/`

### Assets that should be inlined

**Symptom:** `assets/header.txt` contains a 50-char banner string
that's used once in SKILL.md.

**Why it matters:** Adds load cost on demand for content that
fits in the body. The asset-loading round trip costs more than
inlining a short string.

**Fix:** Inline short, single-use strings. Reserve `assets/`
for files that wouldn't fit cleanly in Markdown — diagrams,
data files, structured templates.

## Triggering smells

### Description carries no trigger keywords

**Symptom:**

```
description: A comprehensive solution for managing your documents.
```

**Why it matters:** The model has nothing concrete to match. No
specific intent words ("PDF", "Word", "extract", "merge")
appear in the description.

**Fix:** Add 3-5 trigger phrases the model can match against
user prompts. The phrases should be the words a user would
*actually use* — "manage your documents" is something the
skill author would say, not the user.

### Positive triggers only

**Symptom:** Activation criteria listed, no exclusions.

**Why it matters:** See "Missing negative triggers" above.

**Fix:** Add an explicit `Do NOT activate when…` section to the
body, with 2+ examples.

### Triggers describe internals, not user intent

**Symptom:**

```
Use this skill for tasks that require the AST parser.
```

**Why it matters:** The user does not know they need an AST
parser. They know they need to refactor some code. The trigger
should match the user's vocabulary, not the implementation's.

**Fix:** Restate triggers in user-facing terms. "Use when the
user asks to refactor code, rename a symbol, or restructure a
class hierarchy."

## How to review

Run through the smells in order:

1. **Frontmatter** first — these are hard violations.
2. **Triggering** smells — these break activation, so they're
   high-impact even if the body is otherwise great.
3. **Body** smells — content quality.
4. **Structure** smells — directory layout.

For each finding, name the smell, point at the line/section,
and propose a concrete fix (not "rewrite this" — actually show
the suggested replacement text). Group findings by severity:

- **Critical** — spec violation. Skill won't validate.
- **Warning** — content smell. Skill will work but won't perform
  well.
- **Suggestion** — quality improvement. Optional but valuable.
