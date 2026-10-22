---
name: capability-editor
description: Create, inspect, update, test, publish, or delete Lab capabilities through chat, including skills, specialty agents, tool collections, workflows, and declarative hooks. Use when a user asks to add or change an agent capability, manage a draft or published capability, configure an event hook, assemble gateway tools, or decide whether a capability is ready to publish.
---

# Capability Editor

Manage capabilities through the Lab tools while preserving the draft-first lifecycle and the user's control over publishing.

## Resolve the request

1. Identify the capability type: skill, specialty agent, tool collection, workflow, or hook.
2. Ask only for missing fields required to make a valid draft. If the user names an existing item but gives no `assetId`, list that type and resolve it by name; never invent an ID.
3. For an update, read the current item before changing it. If it is published and has no working copy, call `edit_asset` before the category-specific update tool.
4. Summarize the intended change before destructive operations. Require the user's explicit approval before calling any delete tool with `confirm=true`. That approval is NOT waived by remembered or long-term preferences, saved defaults, or a prior session's approval: only an explicit statement in THIS conversation waives it; a remembered choice may be offered as the suggested answer.

## Create and update by type

### Skills

Use `create_skill_draft`, `read_skill_draft`, and `update_skill_md`. Write SKILL.md as capability, knowledge, and workflow guidance—not as output dictation such as “when asked X, reply Y.” Include valid frontmatter and describe what the skill does and when it should activate. Apply the `skill-creator` methodology when authoring or reviewing substantial skill content.

### Specialty agents

Use the specialty-agent draft tools. Define a focused persona and bind only compatible skill names that actually exist. Keep broad orchestration with the main agent rather than duplicating it in every persona.

### Tool collections

Use `list_tool_collection_drafts` and the tool-collection CRUD tools. Resolve tool references from the gateway inventory and preserve each reference's exact name and version. Do not fabricate gateway tools.

### Workflows

Use the workflow draft tools. Give every step a stable `stepId`, valid agent/skill/tool-collection references, and explicit dependencies or inputs where required. Run workflow readiness before recommending publish when that check is available.

### Hooks

Use `list_hooks`, `create_hook`, `read_hook`, `update_hook`, `delete_hook`, and `validate_hook_condition`.

- Built-in hooks are read-only. To customize one, create a user hook with the same name; it becomes an override.
- Validate every non-trivial condition before create/update. Include a representative `sample_event` when the user provides enough context so validation also dry-runs the match.
- Conditions use the restricted expression language; never suggest arbitrary Python or private/dunder attribute access.
- Hook changes are effective immediately when enabled and do not use `publish_asset`. Make that distinction explicit before enabling an automation.

## Test and publish

After creating or updating a skill, specialty agent, or tool collection, tell the user the working copy is ready and offer the inline **Capabilities → Test** harness. Testing is optional: never imply that a passing harness run is required to publish.

Publish only when the user asks. Call `publish_asset` with the correct category and requested scope; default to `private` only when no broader scope was requested. Backend configuration or validation errors are the only publish blockers. Report those errors without deleting or discarding the working copy.

For published changes, explain that publishing creates an immutable version and removes the working copy. For an abandoned edit, use `discard_changes` only after explicit confirmation.

## Report results

Return the capability name, category, `assetId`, working-copy or version state, and the next available choices: test, continue editing, publish, leave as draft, or delete. Do not auto-publish, auto-enable hooks, or delete on the user's behalf without a direct request.
