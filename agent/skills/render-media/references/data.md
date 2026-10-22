---
name: render-data
description: Render tabular artifacts (csv, tsv) as a sortable data table
handles: text/csv, text/tab-separated-values, application/csv, .csv, .tsv
template: data-table
---

# Render: data (catalog declaration)

> **Not an agent skill.** The deterministic renderer reads the **frontmatter above** to build the
> manifest. This body is human reference only.

MEDIUM view for CSV/TSV → `data-table` (first row as header when non-numeric, else synthesised
`col_1..col_n`; capped at 500 rows with `truncated: true`). A bare `.json` is deliberately NOT
handled here — JSON is what every analysis tool emits, so it must reach the specialised analysis
views or `generic-preview`, never be guessed as a table.
