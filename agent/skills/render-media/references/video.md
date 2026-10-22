---
name: render-video
description: Render video artifacts (mp4, webm, mov) as an NLE multi-track timeline with waveforms, captions, and clips
handles: video/mp4, video/webm, video/quicktime, .mp4, .webm, .mov
template: nle-timeline
tool: media-processing
deliverable: transcoded.mp4
---

# Render: video (catalog declaration)

> **Not an agent skill.** The deterministic renderer (`build_render_manifest`) reads the
> **frontmatter above** to pick the template and build the manifest. This body is human
> reference only — nothing loads it, and the agent never selects among these files.

MEDIUM view: any `.mp4/.webm/.mov` renders as an `nle-timeline` — one video track per URL, plus
waveform peaks (WAV only) and captions when a real transcript sidecar is supplied. Analysis
overlays (markers, boxes, per-speaker lanes) come from the analysis declarations
(`render-shot-segmentation`, `render-face-recognition`, `render-speaker-diarization`), not from
here.
