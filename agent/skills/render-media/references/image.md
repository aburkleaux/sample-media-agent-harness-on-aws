---
name: render-image
description: Render standalone image artifacts (png, jpeg, webp, gif) as a gallery, with optional per-image annotation overlays when a sidecar supplies boxes
handles: image/png, image/jpeg, image/jpg, image/webp, image/gif, .png, .jpg, .jpeg, .webp, .gif
template: image-annotated
---

# Render: image (catalog declaration)

> **Not an agent skill.** The deterministic renderer reads the **frontmatter above** to build the
> manifest. This body is human reference only.

MEDIUM view — deliberately declares no `detect:` and no `tool:`, so it applies on media type
alone and any tool's PNG gets a correct view. Every supplied image goes into `images[]` as ONE
gallery (a two-option generation shows both; a set of stills is one gallery, never one tab per
file); per-image `annotations[]` are populated only when a sidecar supplies boxes.
