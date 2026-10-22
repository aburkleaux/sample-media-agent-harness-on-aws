---
name: render-audio
description: Render audio artifacts (mp3, wav, flac) as an NLE multi-track timeline with per-track waveforms and optional captions
handles: audio/mpeg, audio/wav, audio/x-wav, audio/flac, .mp3, .wav, .flac
template: nle-timeline
tool: audio-stem, audio-stem-separation
deliverable: audio_music.wav, audio_speech.wav, audio_sfx.wav, audio.wav
---

# Render: audio (catalog declaration)

> **Not an agent skill.** The deterministic renderer reads the **frontmatter above** to build the
> manifest. This body is human reference only.

MEDIUM view: `.wav/.mp3/.flac` render as an `nle-timeline` — one audio track per URL (e.g. the
speech / music / SFX stems of one job), waveform peaks for WAV tracks, and captions only from a
real transcript sidecar (never fabricated).
