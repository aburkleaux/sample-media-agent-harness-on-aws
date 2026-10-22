---
name: render-document
description: Render document artifacts (PDF) as an embedded page preview with optional outline and metadata
handles: application/pdf, text/html, .pdf, .html
template: document-viewer
---

# Render: document (catalog declaration)

> **Not an agent skill.** The deterministic renderer reads the **frontmatter above** to build the
> manifest. This body is human reference only.

Renders a PDF/HTML as a `document-viewer` page preview (`document.previewUrl` → the artifact,
served via the same-origin proxy). `pageCount` / `outline` / `metadata` are populated only from a
sidecar, never fabricated.
