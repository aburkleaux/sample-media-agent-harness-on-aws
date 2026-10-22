---
name: skill-creator
description: Author new agent skills, or review and improve existing SKILL.md files, against the agentskills.io specification. Use when the user asks to create a skill, write a SKILL.md, design a skill, review a skill, validate skill frontmatter, or fix a skill that triggers unreliably.
---

# Skill Creator

You help users author skills that conform to the [agentskills.io
specification](https://agentskills.io/specification) and trigger reliably
in production. Two flows: authoring a new skill from scratch via a brief
Q&A, and reviewing an existing skill against the spec to surface fixes.

This skill assumes the agentskills.io specification is the source of
truth. The full rule set lives in [references/spec.md](references/spec.md);
that file is the canonical reference you cite when explaining a
constraint to the user.

## When to activate

**Activate when the user asks to:**

- Create a new skill ("help me make a skill for X", "write a SKILL.md for...")
- Review an existing skill ("is this skill good", "review my SKILL.md", "why isn't my skill triggering")
- Validate skill structure ("does this conform to the spec", "check my frontmatter")
- Improve a description that triggers poorly
- Decide what should go in `references/` versus the SKILL.md body

**Do NOT activate when the user asks to:**

- *Use* an existing skill (the user wants the skill's behavior, not authorship help)
- Write a *Strands tool* (`@tool` Python function) — that's a different artifact
- Edit non-SKILL.md content (a script in `scripts/`, an asset in `assets/`) — those are skill-internal and don't need the methodology

## Decision: authoring vs reviewing

Branch on the user's intent and what they've already produced:

- **No SKILL.md yet** → Authoring path (run the Q&A flow, then synthesize).
- **User pasted SKILL.md text inline** → Reviewing path (apply the
  checklist, point at issues, propose fixes).
- **User named a skill in their Lab inventory** → Reviewing path
  (call `read_skill_draft(asset_id)` to fetch the SKILL.md first;
  `assetId` comes from the Lab Inventory in the prompt — never
  invent one).
- **User has a partial draft** → Reviewing path on what they have,
  then run a shortened Q&A to fill the gaps.

Ask clarifying questions if the intent is ambiguous, but don't
demand a category before the user has answered any substantive
questions. One judgment call at the start beats two clarifications
mid-flow.

## Authoring path

Use the batched Q&A in [references/qa-flow.md](references/qa-flow.md)
rather than asking everything at once. The flow is designed to fit
each round in one turn so the user isn't staring at a wall of
questions.

**The shape of a round:**

- **Round 1 — Purpose & triggers** (always ask)
  - What concrete user requests should activate this skill? (3-5 examples)
  - What requests should *not* activate it? (negative triggers)
  - One-line summary of what the skill does

- **Round 2 — Tools, deps, outputs** (always ask)
  - Which tools does the skill use? (built-in, gateway, code interpreter, etc.)
  - Any external dependencies? (network access, system packages, libraries)
  - What does the skill produce? (a file, structured JSON, a markdown report, etc.)

- **Round 3 — Edge cases & supporting files** (skip when trivial)
  - Failure modes the skill should handle explicitly
  - Long reference material that belongs in `references/`
  - Reusable scripts that belong in `scripts/`

Skip Round 3 for short, single-purpose skills (the majority). When
a user's first answer is "summarize PDFs", you do not need to
extract an architecture review — produce the skill, offer to add
references later.

After the rounds, synthesize the SKILL.md following the spec
([references/spec.md](references/spec.md)) and the anti-patterns
checklist ([references/anti-patterns.md](references/anti-patterns.md)).
Show the user the result before persisting.

## Reviewing path

Apply the checklist to the SKILL.md the user provides. Group
findings by severity so the user sees the must-fixes first.

**Spec-compliance checklist:**

1. **Frontmatter present and parseable** (YAML between `---` markers).
2. **`name` field** — required, max 64 chars, `[a-z0-9-]`, no
   leading/trailing/consecutive hyphens, **must match the parent
   directory name** (this is a hard rule — flag mismatches).
3. **`description` field** — required, max 1024 chars, non-empty,
   includes both "what" *and* "when". Look for trigger keywords:
   without them the skill won't be selected reliably.
4. **Body length** — under **500 lines** and ideally under **5000
   tokens**. If over, recommend moving sections into `references/`.
5. **File references** — relative paths only, **one level deep**
   from SKILL.md. Flag `references/foo/bar.md` style nesting.
6. **Optional fields** — if `compatibility` is set without an
   actual environment requirement, recommend removing it.

Then apply the anti-patterns checklist
([references/anti-patterns.md](references/anti-patterns.md)) for
content smells: vague descriptions, fabricated tool references,
duplicated content between body and references, future-state
language, missing negative triggers, etc.

**Output shape:**

- A short verdict (compliant / mostly compliant / non-compliant)
- A grouped issue list with severity (Critical / Warning / Suggestion)
- Concrete fix proposals for each Critical and Warning
- For non-trivial fixes, propose a full rewrite of the affected
  section rather than a diff — easier for the user to evaluate

## Lab integration (chaining through Lab CRUD)

When the user wants the skill persisted, chain through the existing
Lab tools rather than handing back loose text. The system prompt
already documents the lifecycle model; this skill follows the same
rules.

**Authoring a brand-new skill:**

```
1. user_friendly_name = the human-readable name from Round 1
2. skill_md           = the full SKILL.md content you synthesized
                        (frontmatter + body, as one string)
3. call create_skill_draft(name=user_friendly_name, skill_md=skill_md)
4. report the new assetId and tell the user how to publish:
   "Saved as a working copy. To publish privately, call publish_asset
    with category='skill', asset_id=<id>, scope='private'."
```

The `name` field in Lab is the user-friendly display name; the
`name:` in your generated frontmatter is the spec-compliant slug
(lowercase, hyphens). Slugify Round 1's display name into a
spec-compliant frontmatter `name:` — these can differ legitimately.

**Editing an existing draft:**

```
1. assetId = the user's Lab Inventory entry, never invented
2. Optional: read_skill_draft(asset_id=assetId) to fetch current SKILL.md
3. Apply the changes
4. call update_skill_md(asset_id=assetId, skill_md=new_skill_md)
```

**Editing a published version (no working copy yet):**

The user wants to change `v3` of `pdf-export` but no draft exists.
Per the system prompt's lifecycle rules, fork it first:

```
1. call edit_asset(category="skill", asset_id=assetId)
2. Tell the user: "Forking version V of <name> as your working copy.
   Only you can see this until you publish."
3. call update_skill_md(asset_id=assetId, skill_md=new_skill_md)
```

**Caveat — supporting files (`references/`, `scripts/`, `assets/`)
are not yet writable through Lab tools.** If the methodology
recommends a `references/spec.md` (because the SKILL.md would
otherwise exceed budget), produce the file content and tell the
user it must be added manually until the Lab API supports it.
This is tracked in `docs/backlog.md`.

## Spec essentials cheat-sheet

The full rules live in [references/spec.md](references/spec.md).
The most common gotchas:

| Constraint | Rule |
|---|---|
| `name` length | ≤ 64 chars |
| `name` charset | `[a-z0-9-]` |
| `name` ↔ directory | Must match exactly |
| `description` length | ≤ 1024 chars |
| `compatibility` | ≤ 500 chars; most skills don't need it |
| Body length | < 500 lines / < 5000 tokens recommended |
| Reference depth | One level from SKILL.md |
| File paths | Relative, from skill root |

## Anti-patterns

Quick pointers — full discussion in
[references/anti-patterns.md](references/anti-patterns.md):

- **Vague description** — "Helps with X" is not a trigger. Include
  what it does, when to activate, and 3+ trigger keywords.
- **Wall-of-text body** — over 500 lines means the user pays the
  full cost on every activation. Move detail into `references/`.
- **Fabricated tools** — listing tools the runtime doesn't have.
  Verify against the actual tool inventory before naming them.
- **Future-state notes** — "this skill will eventually support…"
  helps no one and inflates token cost. Strip aspirational text.
- **No negative triggers** — without "do NOT activate when X", the
  skill fires on adjacent-but-wrong requests.
- **Duplicated content** — the same instructions in body and in
  `references/`. Pick one place; the body should orient, the
  reference should detail.

## Worked examples

For a representative bad SKILL.md and the corresponding good
rewrite, see [references/examples.md](references/examples.md). Use
this as a teaching artifact when explaining the gap to the user —
showing both forms side-by-side communicates faster than abstract
rules.

## Validation

The agentskills.io project ships a CLI validator:

```bash
skills-ref validate ./my-skill
```

Mention this when the user wants stronger guarantees than the
checklist above. The validator confirms frontmatter shape and
naming conventions; it does not evaluate content quality (that's
what the anti-patterns review covers). Source:
https://github.com/agentskills/agentskills/tree/main/skills-ref

## Reference index

- [references/spec.md](references/spec.md) — Full agentskills.io
  rules, organized for lookup by field.
- [references/qa-flow.md](references/qa-flow.md) — The batched
  question rounds for new-skill authoring.
- [references/anti-patterns.md](references/anti-patterns.md) —
  Common smells with symptom / why it matters / fix.
- [references/examples.md](references/examples.md) — Annotated
  bad/good SKILL.md pair for a representative skill.
