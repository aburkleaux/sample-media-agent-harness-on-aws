# Custom Agents

A **Custom Agent** is a saved persona for a focused job. It bundles:

- a **role/persona** (how it should behave and what it's for),
- a **model** it uses,
- the **Skills** it should apply, and
- the **capabilities** (tools) it is permitted to use.

Use a Custom Agent when you want a consistent, purpose-built assistant you can
return to — for example "release-notes writer", "data-cleanup assistant", or a
domain reviewer — instead of re-explaining the setup each time.

## Using one

Pick the Custom Agent for the task and give it your request; it already knows its
persona, Skills, and permitted tools. To see which Custom Agents you have, ask
"what custom agents are available?" — the answer reflects your environment and
shows only agents available to you.

## Creating / publishing

You create a Custom Agent by choosing its persona, model, Skills, and the tools
it may use, then publishing it. A published version is fixed; to change what it
can do, publish a new version.

## Sharing it for outside use

A published Custom Agent can optionally be exposed for use outside this app by
callers who bring their own sign-in. If you do that, the agent still runs with
**your** permitted Skills and tools, and external callers only get the tool
sets you explicitly opt in — selecting a tool for the agent is not enough on its
own. This keeps an externally-callable agent from doing more than you intended.

## Custom Agent vs Skill vs Tool Collection

- A **Skill** adds method/knowledge.
- A **Tool Collection** is a reusable bundle of capabilities.
- A **Custom Agent** ties a persona + model + Skills + permitted tools together
  into one reusable assistant.
