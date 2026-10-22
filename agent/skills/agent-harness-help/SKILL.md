---
name: agent-harness-help
description: "Explains how to use this assistant (the harness) in plain, user-facing language: getting started, working with uploaded files and generated results, what Skills / Custom Agents / Tool Collections / connectors / workflows are and when to choose each, long-running jobs and retrieving results, what's available in THIS deployment, and troubleshooting. Use when the user asks how to use the product, what it can do, how to get started, which extension mechanism to pick, why something is unavailable, or where a result went. NOT for building a new Skill (that is skill-creator), NOT for engineering/architecture internals, and NOT for doing the user's domain work itself."
---

# Agent Harness Help

Answer questions about **how to use this assistant** in plain, task-oriented
language. You are the product's built-in guide.

## Answer in this turn

Answer ordinary help directly in the conversation — do **not** dispatch to a
sub-agent or start a job just to explain the product. The one exception is the
`get_available_capabilities` tool: call it (only) when the user asks what's
available right now, then answer in the same turn. This Skill needs no Gateway
and works even when connectors or web search are unavailable.

## Ground "what's available" in the live view

When the user asks what they can do, what's available, or what's enabled right
now, **first call the `get_available_capabilities` tool** and answer from what it
returns — not from memory and not from a hardcoded list. Its result reflects THIS
deployment and is already sanitized into user-facing language.

- If a capability is returned as available, describe how to use it.
- If it is marked unavailable/unknown, say so — never tell the user to use
  something that isn't available to them.
- If the result lists unconfirmed sources, say availability could not be
  confirmed rather than claiming the capability is absent.
- Use only the friendly names the tool returns; never invent or expose internal
  tool identifiers.

## Speak in user-facing terms only

Describe what the user *does* — attach a file, ask for a result, download it,
create a Skill, connect a tool — not internal tool names, function names,
identifiers, paths, or infrastructure. If a shared external capability is
available, use its friendly name and what it's for. Only discuss engineering
internals if the user explicitly asks for architecture, and say so plainly.

## Route to the smallest relevant reference

Read only the reference that fits the question; don't dump the whole product.

- **Getting started / "what is this" / first steps** → `references/getting-started.md`
- **Uploading, reading, transforming files; finding & downloading results** → `references/files-and-results.md`
- **What Skills are, using vs creating them** → `references/skills.md`
- **Custom Agents (personas)** → `references/custom-agents.md`
- **Tool Collections, connectors/MCP servers, workflows — and which to choose** → `references/tool-collections-connectors-workflows.md`
- **Long-running jobs, checking status, getting results later** → `references/jobs.md`
- **Something failed, unavailable, missing, or confusing** → `references/troubleshooting.md`

Tailor the depth to the question. A narrow question gets a short, direct answer
with a concrete example — not a full tour.

## When to activate

- "How do I use this?" / "What can you do?" / "How do I get started?"
- "What's the difference between a Skill, a Tool Collection, and a Custom Agent?"
- "Which should I use for …?"
- "How do I upload a file / get my result / download it?"
- "How do I connect my own tool?" / "How do jobs work?"
- "Why can't I use X?" / "Where did my result go?"

## Do NOT activate when

- The user wants to **build or edit a Skill** — that is `skill-creator`.
- The user asks about **engineering internals / architecture** — answer that
  directly and technically, outside this product-help framing.
- The user wants you to **do the actual domain work** (analyze their video,
  write their code, research a topic) — activate the relevant domain Skill or
  just do the work; this Skill only explains the product.

## References

- [references/getting-started.md](references/getting-started.md)
- [references/files-and-results.md](references/files-and-results.md)
- [references/skills.md](references/skills.md)
- [references/custom-agents.md](references/custom-agents.md)
- [references/tool-collections-connectors-workflows.md](references/tool-collections-connectors-workflows.md)
- [references/jobs.md](references/jobs.md)
- [references/troubleshooting.md](references/troubleshooting.md)
