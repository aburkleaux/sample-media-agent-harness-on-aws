# Tool Collections, connectors, and workflows

Three related ways to extend the assistant with *capabilities* and *repeatable
process*. Here's what each is and when to pick it.

## Connectors (external tools / MCP servers)

A **connector** adds an executable external capability — it lets the assistant
*do* something new by talking to an outside tool or service. Add a connector
when the assistant needs an ability it doesn't have built in (for example, a
specialized data source or an external action).

To see which external tools are available to you right now, ask "what tools /
connectors are available?" — the answer reflects this deployment.

## Tool Collections

A **Tool Collection** is a curated, reusable *set* of capabilities grouped
together so you can grant them as a unit — for instance to a Custom Agent.
Use one when you find yourself giving the same handful of tools to agents
repeatedly, or when you want to control exactly which tools a Custom Agent may
use.

## Workflows

A **workflow** is a repeatable *sequence of steps* for a multi-step task. Use one
when the same process runs again and again in the same order, and you want it
run consistently rather than re-described each time. Multi-step work may run as a
tracked plan with progress you can follow.

## Which should I use?

- Need a **new ability**? → a **connector/tool**.
- Want to **bundle several tools** to grant together? → a **Tool Collection**.
- Want a **reusable persona** that carries its own model, Skills, and tools? →
  a **Custom Agent** (see `custom-agents.md`).
- Want a **repeatable multi-step process**? → a **workflow**.
- Want to change *how* a kind of task is approached? → a **Skill** (see
  `skills.md`).
