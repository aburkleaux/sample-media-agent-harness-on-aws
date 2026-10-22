# Skills

A **Skill** gives the assistant domain instructions and a method for a kind of
work — how to approach it, what steps to follow, what good output looks like. A
Skill is knowledge and procedure, not an external tool.

## Using Skills

You don't turn Skills on manually. When your request matches a Skill's purpose,
the assistant applies it automatically. To see which Skills are available in
this deployment, ask "what skills are available?" — the answer reflects your
current environment.

Examples of what a Skill might cover: analyzing media for compliance, producing
a specific kind of report, following a house style, or governing how web
research is done and cited.

## Creating your own Skill

You can author a Skill so the assistant handles your recurring work your way. To
build one, ask to **create a skill** — that starts the skill-authoring guide,
which is a separate flow from this help. Creating a Skill is the right choice
when:

- you repeat the same kind of task and want consistent method and output, and
- the task is mostly about *how to think/act*, not about adding a new external
  tool (for that, see connectors in
  `tool-collections-connectors-workflows.md`).

## Skill vs the other extensions

- **Skill** = instructions + method (this page).
- **Tool / connector** = an executable external capability.
- **Tool Collection** = a curated set of capabilities.
- **Custom Agent** = a persona bundling a model, Skills, and permitted tools.
- **Workflow** = a repeatable sequence of steps.

See `tool-collections-connectors-workflows.md` and `custom-agents.md` to choose.
