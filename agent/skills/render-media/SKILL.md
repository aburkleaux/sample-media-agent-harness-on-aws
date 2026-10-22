---
name: render-media
description: Turn any media artifact or analysis result into an interactive render manifest (NLE timeline, video annotations/boxes, marker/segment/lane timelines, tables, document/audio/image views). Use for EVERY renderable tool output — video, audio, image, PDF, and analysis JSON from shot-segmentation, face-recognition, face-people, speaker-diarization, and media-processing.
handles: video/mp4, video/webm, video/quicktime, audio/mpeg, audio/wav, audio/x-wav, audio/flac, application/json, application/pdf, text/html, .mp4, .webm, .mov, .mp3, .wav, .flac, .json, .pdf, .html
---

# Render: media

One skill that renders every media/analysis artifact. **Everything you need is in this file — do
NOT call the skills tool again after reading this, and do not try to open other files.** Pick the
ONE row below that matches the artifact you were given, build that template, render only its
deliverable, and finalize.

## Choose by the artifact (detect key → template → what to render)

| Artifact / detect | Template | Render | Deliverable file(s) | Scratch (never render) |
|---|---|---|---|---|
| video file (.mp4/.mov/.webm) | `nle-timeline` | one video track | transcoded.mp4 | — |
| audio file (.wav/.mp3/.flac) | `nle-timeline` | one audio track (+ peaks) | audio_music.wav, audio_speech.wav, audio_sfx.wav, audio.wav | chunk_*.wav, audio_16k_mono.wav |
| image file(s) (.png/.jpg/.gif/.webp) | `image-annotated` | the image — or ALL of them as one gallery when several were given | the generated image(s) | — |
| PDF / HTML | `document-viewer` | the document | the pdf/html | — |
| JSON with `shots` | `nle-timeline` | shots as markers on the source video's timeline (time=start_time end=end_time label=shot_id category=shot_type; duration=actual_duration_sec) | shots.json | recurrence.json, seg*_win*, segment_*_pts |
| JSON with `tracks`/`clusters` (face boxes) | `nle-timeline` | per-face bounding-box overlays on the SOURCE video (boxes: tracks[].faces[] box=box time=timestamp label=gender frame=frame_key) | face_tracks.json, facemetadata.json, identity_clusters.json | frame_index, detection_plan, search_index_* |
| JSON with `people` | `composite-timeline` | one lane per person + a table | facemetadata.json | — |
| JSON with `diarization`/`sentence_speakers` | `composite-timeline` | one lane per speaker + segments | speaker_manifest.json, global_speakers.json, final_transcript.json | dynamic_segments_*, embeddings_*, SPEAKER_*.wav |

Notes:
- **MULTIPLE images given (a set to view TOGETHER — e.g. a frame gallery, or several
  generated options): render ALL of them, not one.** Pass EVERY image url to
  `build_render_manifest(candidate_urls=[...])` — the `image-annotated` template shows the
  whole set as one steppable gallery in a SINGLE tab. Do NOT pick just one, and do NOT stitch
  them into a contact-sheet image (the user wants separate images viewed together, not merged).
- **Face boxes need the SOURCE video** as the canvas — draw the timeline container's box overlays on it.
- **Always pass the analyzed source media in `source_urls`** (the video for shots/faces/diarization; the clip/source for stems/audio extraction). `build_render_manifest` stamps it as the manifest's `sourceKey`, and the UI uses that to MERGE every analysis of the same source onto ONE composite timeline (stems/shots/faces become lanes on the source's tab). A plain clip/transcode has no source, so it keys on its OWN output and becomes its own tab.
- `probe.json` is the source SPEC: read it if you need codec/dimensions, but NEVER render it as a tab.
- If a job produced several candidate files, use RECENT USER REQUESTS to pick the one the user asked
  for; otherwise render the primary deliverable for the detected type.

## Self-recovery (required)
1. DECIDE which url(s) to render (match RECENT USER REQUESTS + the deliverable for the detected
   type; skip scratch), then call `validate_render_urls` with your chosen urls.
2. Call `build_render_manifest(candidate_urls=[...], manifest_id=..., session_id=..., source_urls=[...])`
   to assemble + write the manifest. It projects boxes/markers/lanes deterministically — do NOT
   hand-build the manifest. Pass the source video in `source_urls` when rendering face boxes.
3. Call `review_render_manifest`. If it flags that the data supports boxes but the manifest has
   none, re-run `build_render_manifest` with the `source_urls` video, then re-review.
4. Finalize on "OK" or after one honest fix.
