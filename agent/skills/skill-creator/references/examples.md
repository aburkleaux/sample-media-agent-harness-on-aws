# Worked Example — Bad vs Good SKILL.md

A side-by-side pair for a representative skill (`data-export`)
that exports query results from a data warehouse to a chosen
file format. The bad version is annotated with the smells it
exhibits; the good version shows the same intent done right.

Both examples target an environment that has `code_interpreter`,
`artifact`, and a gateway tool `warehouse-gateway___run_query`
available — tools you would have verified exist before naming
them in the skill.

---

## The bad version

> File: `agent/skills/data-exporter/SKILL.md` (directory name
> is `data-exporter`, frontmatter `name:` is `data-export` —
> already a violation before the body starts.)

```markdown
---
name: data-export
description: A comprehensive utility for working with data exports. This skill helps users export, format, and deliver data from various sources to various destinations including but not limited to CSV files, Excel spreadsheets, JSON documents, and other tabular formats. Use this skill whenever the user has any need related to data, exports, files, or business intelligence operations. The skill supports a wide range of use cases and is designed to be the one-stop solution for all your data export needs.
compatibility: Works with any agent that supports tool calling
---

# Data Export

This skill is a comprehensive solution for exporting data from our
warehouse to a variety of formats. It supports CSV, Excel (XLSX),
JSON, Parquet, and we are working on adding Avro support in a
future release.

## How to use

The skill takes a SQL query as input and produces an exported file
as output. You should use the `query_warehouse` tool to run the
query, then the `export_to_csv` or `export_to_excel` tool to
format the results, and finally the `upload_file` tool to deliver
the file to the user.

## Spec / Frontmatter Rules

The skill's frontmatter must include a `name` field that uses
lowercase letters and hyphens. The `description` field must be
descriptive. The skill should follow the agentskills.io
specification, which requires that name be at most 64 characters,
description be at most 1024 characters, that the directory name
match the name field, and so on.

(continues for another 400 lines covering: SQL injection prevention
guidelines, exhaustive list of every column type the warehouse
supports, an essay on data privacy laws across 12 jurisdictions,
a comparison of CSV vs TSV vs PSV delimiters, a history of the
Excel file format, and inline copies of the spec rules from
agentskills.io.)
```

### What's wrong

| # | Smell | Line | Severity |
|---|---|---|---|
| 1 | Frontmatter `name` doesn't match the parent directory (`data-export` vs `data-exporter`) | 2 | Critical |
| 2 | `description` is 510+ chars of vague generalities; no concrete trigger keywords | 3 | Warning |
| 3 | `description` says "various sources to various destinations" — no specific intent | 3 | Warning |
| 4 | `compatibility` is set but says nothing meaningful | 4 | Warning |
| 5 | Future-state language ("we are working on adding Avro support") | body | Warning |
| 6 | Fabricated tool names: `query_warehouse`, `export_to_csv`, `export_to_excel`, `upload_file` (none exist in the runtime) | body | Critical |
| 7 | Body restates the agentskills.io spec — duplicated content with `references/spec.md` if a reference existed, and irrelevant either way | body §"Spec / Frontmatter Rules" | Warning |
| 8 | Body is 400+ lines of off-topic material (privacy law, format history) — overflow that should live in `references/` if anywhere, but most of it shouldn't exist at all | body | Critical |
| 9 | No negative triggers — fires on every "data" / "export" / "file" prompt | — | Warning |
| 10 | No concrete input/output example | — | Suggestion |

The skill would technically validate the directory-name issue
aside, but it would activate on the wrong requests, fail at
runtime when the agent called the fabricated tools, and burn
~5000 tokens per activation for content that doesn't help the
task.

---

## The good version

> File: `agent/skills/data-export/SKILL.md` (directory name and
> frontmatter `name:` agree.)

```markdown
---
name: data-export
description: Export query results from the analytics warehouse to CSV or XLSX. Use when the user asks to export data, save query results to a file, run a SQL query and download the output, or generate a report from the warehouse. Do not activate for "show me the data" — that's a read, not an export.
---

# Data Export

You export query results from the analytics warehouse into a file
the user can download. Two formats are supported: CSV (default)
and XLSX.

## When to activate

**Activate when the user asks to:**

- Export query results / data to a file
- Save warehouse output as CSV or Excel
- Generate a downloadable report from a SQL query
- "Pull the [X] data" with a hint that they want a file

**Do NOT activate when the user asks to:**

- Display data inline ("show me the top 10 customers" — that's a
  query, not an export)
- Upload a file *to* the warehouse (that's a different operation)
- Format an existing file (use the appropriate file-format skill)

## Process

1. Confirm format with the user. Default to CSV; ask if the
   query produces >50k rows or the user mentions Excel.
2. Run the query via `warehouse-gateway___run_query`.
3. Convert results to the chosen format in `code_interpreter`
   (use `pandas` for both CSV and XLSX — the runtime has it
   pre-installed).
4. Upload the file via the `artifact` tool and return the
   presigned URL.

## Tools

- `warehouse-gateway___run_query` — runs the SQL, returns rows
- `code_interpreter` — pandas conversion + S3 upload
- `artifact` — generates the presigned download URL

## Output

A short message with:

- The chosen format
- Row count
- A clickable presigned URL (1-hour validity)

Example:

```
Exported 4,182 rows to CSV.
Download: https://...

```

## Edge cases

- **Empty result set:** report "0 rows; no file generated"
  rather than emitting an empty file.
- **Query takes >60s:** the runtime gateway times out at 60s
  for the warehouse target. Tell the user to add a LIMIT or
  narrow the WHERE clause; do not retry blindly.
- **XLSX over 1M rows:** Excel's row limit. Fall back to CSV
  and tell the user why.
- **Format ambiguity:** if the user says "Excel" but the query
  returns >800k rows, ask before exporting — the file will be
  slow to open.
```

### Why this works

| # | Strength | Note |
|---|---|---|
| 1 | `name` matches the directory exactly | Critical rule, satisfied |
| 2 | `description` (276 chars) is concrete: "export query results" + "CSV or XLSX" + "from the analytics warehouse" | Specific enough for the model to match |
| 3 | Description carries the negative trigger inline ("not 'show me the data'") | Helps the model discriminate from adjacent intents |
| 4 | No `compatibility` field — the skill has no unusual environment requirements | Spec recommendation: skip unless needed |
| 5 | Body is ~50 lines, well under the 500-line ceiling | Lower token cost per activation |
| 6 | Every tool name is real and verified | `warehouse-gateway___run_query` is the gateway-namespaced form, matching how AgentCore exposes it |
| 7 | "When to activate" + "Do NOT activate when" are explicit | Two-sided trigger criteria |
| 8 | Edge cases listed with concrete behaviors | The agent knows what to do, not just what could go wrong |
| 9 | Example output shape inline | Lower variance in production output |
| 10 | No future-state text, no spec duplication, no overflow content | Body earns its space |

---

## Side-by-side counts

| Metric | Bad | Good |
|---|---|---|
| Frontmatter chars | 600+ | 280 |
| Body lines | 400+ | 50 |
| Trigger phrases in description | "data, exports, files, business intelligence" (vague) | "export query results", "save to a file", "download SQL output", "generate a report" |
| Negative triggers | 0 | 3 |
| Fabricated tools | 4 | 0 |
| Future-state mentions | 1 | 0 |
| Body sections duplicated with `references/` | 1 (the spec recap) | 0 |

The good version is roughly 1/8 the size with strictly more
useful content. The Q&A flow in [qa-flow.md](qa-flow.md) produces
this shape by design — Round 1 forces explicit triggers and
anti-triggers, Round 2 forces tool verification, optional Round
3 surfaces the edge cases.

---

## Using this example during review

When walking a user through a problematic SKILL.md, you can:

1. Pick 2-3 smells from the user's file that match the bad
   version above.
2. Point at the corresponding good-version section.
3. Propose the rewrite using the good-version shape as a template.

This is more concrete than abstract spec rules. Showing the
delta in size, in trigger specificity, and in edge-case
treatment communicates the gap faster than enumerating
violations.
