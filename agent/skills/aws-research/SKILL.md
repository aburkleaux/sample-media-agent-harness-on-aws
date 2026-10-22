---
name: aws-research
description: Looks up AWS service facts in the official documentation and cites them. Answers most questions with one search and one read; escalates to comprehensive cross-checking only when the user asks to verify, compare, or pin down specifics.
---

# AWS Research Skill

Ground AWS facts in the official documentation instead of recalling them.

## Pick a depth first

Most questions are simple lookups. Choose before you start, and say which you chose
if it is not obvious.

### Simple lookup — the DEFAULT

For "what is X", "what are the X services", "does X support Y", and other
single-answer questions.

- **One search, then one read** of the best hit. Stop there.
- Two or three searches are acceptable when the first returns nothing on point.
- Do **not** look up each item in a list separately. A question naming a service
  *family* wants one authoritative overview page, not one lookup per service.
- Do **not** add web research, blog posts, or announcements.
- Answer from what you read and cite the URL.

**Disambiguate first when the term is ambiguous.** If a term could mean two different things — a
specific service vs. a generic pattern (e.g. "AWS Elemental inference": a named Elemental capability
vs. a generic ML-on-video pattern) — spend ONE search to resolve *which* is meant, then answer. This
is the common middle case between a clean lookup and deep research; it is still a simple lookup, just
with a disambiguating first hop.

Budget: under ~5 tool calls. If you are past that on a simple lookup, you have
misjudged the depth — answer with what you have and say what is unconfirmed.

### Deep research — only when ASKED

Escalate only when the user explicitly asks to verify, compare, cross-check,
audit, or nail down exact figures, or when a decision plainly rests on precision
(quotas, limits, lifecycle dates).

- Read multiple pages and cross-check them against each other.
- Report contradictions between AWS's own pages rather than silently picking one.
  A quota table and a performance page disagreeing is a finding, not noise.
- Supplement with authoritative AWS sources (blogs, whitepapers, architecture
  center) when documentation is sparse or the service is in preview.
- Note publication dates and version differences.

## Model and timing

- **Model.** A simple lookup can run on a lighter/faster model — a cheap latency lever. Reserve a
  stronger model for deep/cross-check research, where reconciling sources and spotting
  contradictions is the actual work.
- **It's synchronous.** Research + synthesis is inline LLM time, not a queued job. There is no
  async / "run it in the background" path here — set expectations accordingly rather than promising
  a job id.

## Grounding and labelling — both depths

Claims about capabilities, regional availability, deprecation or lifecycle status,
and pricing go stale. Look them up rather than recalling them: unsourced recall
reads exactly as confidently as a cited answer even when it is wrong.

If you state such a fact without looking it up, **label it as unverified recall**
so it is distinguishable from a sourced one. Same for a date or figure you cannot
reconcile — say so plainly instead of re-searching to resolve it.

Conversational and general-explanation turns need no lookup at all.

## Output

- Lead with the answer, then supporting detail.
- Cite source URLs.
- Quote documentation directly where exact wording matters.
- Flag anything outdated, conflicting, or unverified.

## Available tools

`mediaworks-harness-aws-mcp___aws___search_documentation` and
`mediaworks-harness-aws-mcp___aws___read_documentation`.

Documentation reads only. `call_aws` is deliberately not exposed: these tools reach
AWS through the harness gateway, whose interceptor drops write-capable tools. Use
the code interpreter or an explicitly scoped tool for AWS API calls.
