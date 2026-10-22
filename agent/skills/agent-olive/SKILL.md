---
name: agent-olive
description: "Create a multi-segment speaking-character reel by LIP-SYNCING a chosen voice onto ONE locked anchor image. Staged and supervised: (1) lock the character + anchor image (upload or generate); (2) lock the VOICE — audition and pick a catalog voice, or clone one from a short sample, via the text-to-speech micro-workflow; (3) confirm the script; (4) chunk it, synthesize TTS per chunk, and let the user review and trim the leading blip; (5) lip-sync each trimmed clip onto the anchor with fal-ai/sync-lipsync/v3/image-to-video (clip length follows the audio); (6) per-segment review gallery, regenerate flagged; (7) ask for the inter-chunk pad, then stitch. Use for talking-character reels — pets, mascots, presenters, avatars speaking a script. NOT for existing-clip editing, silent images, a one-off talking-avatar clip, non-speaking b-roll, or exact voice reproduction."
---

# Agent Olive

Create a reel in which one character delivers a script as a sequence of independently
lip-synced clips that read as one continuous performance. The method is **audio-first
lip-sync**: synthesize the character's voice, then drive the mouth on a single locked
anchor image. Minimize identity, voice, and cut drift; do not promise perfect continuity
unless every segment passes the review gate below.

## Boundaries — lip-sync IS the method

This workflow deliberately uses a **separate voice (TTS) step + a lip-sync step** — that is
the pipeline that works here, not a substitution to avoid. The voice comes from the M2C
`text-to-speech` micro-workflow (CosyVoice3 zero-shot cloning): either a catalog voice the
user auditions, or a voice cloned from a short reference clip. The mouth is driven onto the
anchor by `fal-ai/sync-lipsync/v3/image-to-video`.

- The voice **approximates** a target; it is not exact reproduction. If the user needs a
  specific real person's voice guaranteed, say plainly that this clones *timbre and delivery*,
  not identity, and that quality depends on the reference clip.
- Do not claim to have *heard* an uploaded reference. You know a clip only through what a tool
  measures (diarization confidence, TTS QC). Ask for a short textual description of the wanted
  voice when you have no measurement.
- Never invent the character, species, wardrobe, or appearance. The anchor image is the
  binding constraint on look; the voice clause is the binding constraint on sound.

## Required inputs and hard gate

Collect these before any paid generation:

1. **Character + anchor image** — a user upload or a concrete description you turn into ONE
   approved still. The anchor sets the frame (resolution and aspect); the reel inherits it.
   **What an upload means:** an upload that comes WITH a description of how the character should
   look (wardrobe, pose, framing, background — "I want this puppy in a pink suit, sitting straight,
   black background") is a REFERENCE: generate two anchor variants from it and stop for the pick.
   Use the upload itself as the anchor only when the user says so in THIS conversation ("use this
   image as is"). A remembered preference (memory says "reuses existing anchors", a past "skip the
   anchor") never decides this — at most it is offered as a question.
2. **Voice** — a catalog `voice_id` the user picked from an audition, or a voice cloned from
   the user's reference clip (see "Lock the voice").
3. **Script** — the monologue, or permission to draft and confirm it.
4. **Look/framing** — read from the anchor; do not ask the user for a resolution.

If the character is missing, ask only: "Describe the character, or upload a reference image."
Do not call a generation tool yet.

## Pipeline pins — the tools this skill uses

- **Anchor image:** the current image-generation skill (`media-references`) for a
  described character, or the user's uploaded image (use its HTTPS/CloudFront URL, never an
  `s3://` URI, as the lip-sync input).
- **Voice (M2C `text-to-speech` micro-workflow — machine-credentialed, the stable path):**
  `text-to-speech___list_voice_collection`, `___get_voice`, `___register_voice`,
  `___synthesize_speech`, `___get_job_status`; and `media-outputs___get_download_urls` to
  presign preview/clip `s3://` URIs for playback. See the `video-processing` skill's
  Text-to-Speech reference for the full contract — it is authoritative for parameters,
  batch bounds, QC, and idempotency.
- **Lip-sync:** `fal-ai/sync-lipsync/v3/image-to-video` (**verified live** — do not substitute).
  Inputs are exactly `image_url` (the anchor still; JPEG/PNG/WebP) + `audio_url` (the chunk's
  trimmed audio) — **both required, both HTTPS URLs, never `s3://`**. It has **no `duration` and
  no aspect param**: the output matches the audio's duration and the image's frame. Read the
  result at `video.url`. It is SLOW (measured p50 ~2.7 min, p90 ~8 min per clip), so ALWAYS
  `submit_job` (async), never `run_model`. Not in the `media-references` shortlist — this skill
  pins it; only run one `get_model_schema` if the submit ever rejects these fields.
- **Trim ONE clip:** `media-processing___process_media` with `mark_in_out=[start,end]` — the
  native M2C trim, valid here because a TTS WAV is written into the M2C bucket `process_media`
  reads. **Stitch / combine MULTIPLE clips:** ffmpeg in `code_interpreter` (the `video-processing`
  skill is authoritative for all combining). `process_media` operates on a single in-bucket asset
  and CANNOT mux/concat several external clips, so never route the stitch through it or through fal.

## Media Agent Harness execution contract

The Harness has no in-plan pause/approval node. Treat every human decision as a boundary
between separate invocations; never dispatch a plan expected to stop midway and wait. Use
**direct dispatch** for a single bounded turn (an anchor edit, one voice audition, one TTS
batch, one lip-sync, one render). Use **`plan_and_execute_tool`** for the parallel per-chunk
lip-sync fan-out. **Assembly is never a node in the generation plan** —
it is a separate dispatch that runs only after the per-segment review is approved.

Supervised phases (each a conversational boundary). Every **stop** below is
NOT waived by remembered or long-term preferences, saved defaults, or choices from a prior
session: only an explicit statement in THIS conversation waives it, and the remembered choice
may be offered as the suggested answer.

1. **Lock the character + anchor, current turn.** Confirm the character; generate the anchor
   (or use the upload) via direct dispatch; report the anchor image cost; **stop for anchor
   approval.** Do not discuss voice or video spend yet.
2. **Lock the voice, next turn.** Audition catalog voices or clone one; **stop for the user to
   pick a `voice_id`** (see "Lock the voice"). If they want inflection, settle the `style` from
   the closed menu now.
3. **Confirm the script + chunking.** Draft/confirm the monologue and split it into good-length
   chunks (see "Chunk the script"). **Stop for script approval.**
4. **Synthesize + review the audio.** One `synthesize_speech` batch for the chunks; presign the
   WAVs; present them for the user to listen and **trim the leading blip** (see "Trim"). **Stop
   for audio approval.** Estimate and present the full lip-sync spend here, before phase 5.
5. **Lip-sync plan — generate ONLY, do NOT stitch.** After spend approval, launch ONE
   tracked plan: one lip-sync node per approved audio chunk (anchor + that clip). No
   validation node and no stitch node in this plan.
6. **Per-segment review, later turn — the gate before stitching.** Build the review gallery of
   every clip; the user confirms sync/identity/voice per segment; flag any to regenerate.
7. **Ask for the pad, then stitch.** Ask the user for the inter-chunk pad (default ~1.0s
   held-frame breath); assemble only accepted clips in script order as a SEPARATE dispatch —
   feeding it the exact approved clip URLs from step 6 verbatim; the stitch NEVER re-searches
   for its inputs (that inflates one concat into dozens of steps and can splice in another
   session's reel).

## Lock the voice — audition, or clone

A `voice_id` is not something the user can pick from a table — they must **hear** it. Never
ask the user to type a `voice_id`, and never invent one (an unknown id fails the synthesis
*job*, not the submit).

**Audition catalog voices (hear-before-commit):**

1. `list_voice_collection` with **no `gender` filter** (a language filter is fine). The tool has
   no pagination and lists oldest first; the harness always asks for its maximum page (100), so one
   call is the whole catalog — if a result carries a "may hold more" warning, it is not. Clones
   registered before 2026-10-20 carry NO gender, and a gender filter silently drops them. ALWAYS
   include every `origin: user-upload` voice (the user's clones), newest first, then narrow the seeded
   ones to a **curated 3–6** that fit the character — never dump all of them. When the user NAMES a
   voice ("agent_olive_voice_v4"), find it by name in that list (or `get_voice` by id); never say it
   does not exist without that search.
2. `get_voice` on the shortlist (all in ONE message, in parallel) for each `preview_uri`. Do NOT presign
   them: `show_media_gallery` takes the `s3://` uri as-is and the player resolves it (a presigned link
   is ~1.5 KB of text you would have to copy out, once per voice).
3. Open a **playable audition gallery** with `show_media_gallery` — one item per candidate
   `{label: "name · gender · language · origin", url: the preview_uri}`, title "Voice audition". The
   label is what the user reads: always the voice NAME, never a file id. **Do steps 1–3 in ONE sub-agent
   dispatch** whose `tools` are `list_voice_collection`, `get_voice` and `show_media_gallery`, and tell
   it to FINISH by calling `show_media_gallery` with every candidate. That gallery IS the audition: no
   other tab opens for the previews.
   To play ONE voice again ("show me v4"), dispatch the same tools for just that voice and end with
   `show_media_gallery` of that one item — its tab comes to the front. Never re-dispatch the Render Agent for
   it. Mark
   `origin: user-upload` voices as **"your clone"** vs seeded. Always offer a **"clone my own
   voice"** option alongside. (Auditioning needs only playback; the gallery's mark-in/out is
   simply unused here.)
4. The user picks by listening; you map the pick back to its `voice_id`.

**Clone a voice (`register_voice`):** required `ref_s3uri` (MUST be in the proxy bucket — upload
first if it is not) + `ref_transcript` + **`gender`** (the harness refuses a registration without one:
an untagged voice never appears in a gender-filtered list and cannot be edited later — take it from the
character or speaker, ask the user if unsure). Also send `name` and `language`. **The reference clip is the #1 driver of clone quality —
get it right or the clone comes back wrong:**
- **Keep the reference SHORT: 3–10 s of ONE clean, single-speaker sentence.** The tool ACCEPTS
  WAV 8–48 kHz anywhere in 1–30 s, but longer is NOT better — a long, multi-sentence reference
  makes CosyVoice zero-shot **bleed the reference's OWN words into the output**: you hear the
  reference's opening spoken back instead of your new script. (Session 4681f2da used the full
  ~30 s, 12-sentence extract and every clone reproduced "Agent Olive reporting…" instead of the
  joke — that is this failure. Short target chunks make the bleed worse, but the root is the long
  reference.) Aim 3–10 s; never hand it the whole clip because it fits under 30 s.
- **Clone from the DIARIZED single-speaker segment** (the `SPEAKER_00`-style output of
  `diarize_speech`), NOT the raw whole `audio_extract.wav`. If you diarized, actually USE that
  clip; pick the cleanest single-speaker stretch and trim leading/trailing silence first.
- **`ref_transcript` must be the EXACT words of THAT clip** (ASR-validated at 0.70). A mismatch is
  rejected — and when a near-miss slips through it is itself a top cause of reference bleed, so fix
  the transcript/clip rather than retrying.

**There is no delete-voice tool** — confirm metadata with the
user before registering. On success the poll returns the new `voice_id` (surface it).

## Voice style — a CLOSED menu, default none

`synthesize_speech`'s `style` is typed as a bare string with **no enum in the schema**, so a bad
name is NOT caught at submit — it **fails the job** on the next poll. Present only these, and
validate the pick before submitting:

| Kind | Values | Cost |
|---|---|---|
| **none** (default) | — | 1× |
| **pace** | `fast`, `slow` | 1× (single-shot) |
| **emotion** | `happy`, `sad`, `angry`, `cute`, `loud`, `soft` | **~3×** (best-of-3 candidates) |

Rules that bite:
- **English voices only.** A non-English `voice_id` + any `style` fails the job. Check the
  voice's `language` (from `get_voice`) FIRST; if it is not English, do not offer styles.
- There is **no `excited`, `whisper`, or `neutral`** — "neutral" means omit `style`. Default to
  **none** unless the user asks for inflection.
- One style applies to the whole batch; two emotions means two calls. An explicit `seed` cancels
  best-of-3 (you lose emotion quality for reproducibility).
- Fold the ~3× emotion cost into the phase-4 spend estimate.

## Chunk the script

Split the monologue into good-length chunks — roughly **one sentence or beat per chunk**, so
each lip-sync clip is a self-contained utterance and a single flagged line can be re-synced
without touching the others. `scripts/calculate_segment_length.py --pack` is the deterministic
helper: it groups consecutive sentences into good-duration segments (chunking only — the clip's
final length is set by the TTS audio, not by a frame count). Confirm the chunking with the user
before spending.

Keep each chunk within the TTS batch bounds (see the TTS reference: a per-batch ~256 KiB payload
ceiling AND a whole-request aggregate bound; short lines are fine, long ones shrink both). One
`synthesize_speech` call renders the whole list, one WAV per `transcript_index`.

## Synthesize the audio, then trim the blip

- One `synthesize_speech(voice_id, transcripts=[...chunks], style?, flow_steps=6)` call. Leave
  `flow_steps` at 6 (the measured quality plateau). Omit `idempotency_token` so a "say it again"
  re-render is honored; omit `seed` for a fresh take (supply one only for reproducibility).
- **`transcripts` MUST be a list of PLAIN STRINGS** — the chunk text itself (`["Hi, I'm Agent
  Olive… woof, woof!", "Welcome back…"]`). NEVER wrap each item as an object or a stringified dict
  (`{'text': "…"}`) — session 4681f2da passed `"{'text': \"…\"}"` strings and the model synthesized
  the wrapper/garbled the line. One clean string per chunk, in script order.
- It is **async and runs as a tracked BACKGROUND job — dispatch it and END THE TURN; do NOT sit
  in a `get_job_status` loop** (inline-polling blocks the turn and burns the whole step budget —
  session 4681f2da looped `get_job_status` 100+ times on ONE 9-chunk batch and had to be stopped).
  Submitting registers the job; the batch finishes on its own and you fetch it in a LATER turn
  (the user's "show the TTS results", or when results surface). When you DO fetch it (via
  `get_job_result`), **read `outputs[]`, not just `status`** — a
  `SUCCEEDED` job can carry per-transcript failures (each entry has its own `uri`, `duration_s`,
  `qc_passed`, `error`, keyed by `transcript_index`). Re-synthesize only flagged transcripts,
  not the whole batch. **QC is a review pointer, not a gate** (`all_passed: false` is normal —
  Whisper is non-deterministic).
- Presign the WAVs (`media-outputs___get_download_urls`, one call) and present them in an
  **interactive clip gallery** with `show_media_gallery` — one item per chunk `{label, text: the chunk
  line, url: the presigned wav, kind: "audio", durationHint}`. Each chunk renders with a waveform + its own **mark-in/out ruler**, and a batch
  **"Trim all marked"** button. **The user marks the keep-range (blip-end → clip-end) on each chunk**
  and triggers one turn: `Trim these N clips to their marked ranges: … [sources: "<label>"=<url>; …]`.
- **Service that batch-trim turn as a fan-out**, NOT a single call: one
  `media-processing___process_media` per clip with `mark_in_out=[in, out]` on THAT clip's own audio
  (the `[sources:]` list pairs each label to its url, so there is no ambiguity about which file to
  trim). This is a fan-out because each clip is its OWN file — N DISTINCT inputs. It does NOT
  generalize to N ranges of ONE file: cutting several clips out of a single source is ONE in-turn
  `code_interpreter` pass (video-processing §4b2), never N jobs and never N nodes. Check the
  `[sources:]` list — if every entry shares the same url, you are in that case.
  **Then collect ALL the trimmed clips back into ONE combined gallery** — a single
  `show_media_gallery` call with **every** trimmed url in that one call (one entry per clip: `{label, text: the chunk line, url: the trimmed wav,
  kind: "audio", durationHint}`), so the whole edited set lands in ONE tab. Do NOT leave the per-clip
  `process_media` outputs as the result: each trim is an async job that auto-renders its OWN
  single-audio tab, and those scattered per-clip tabs are NOT the edit surface — the one combined
  gallery of the trimmed set is (the same move the lip-sync review below makes for per-segment video
  tabs). Carry the **trimmed**
  clips forward — the lip-sync length follows the audio, so an untrimmed blip becomes a blip in the
  video. You MAY pre-suggest a keep-range (e.g. trim leading silence), but the user's marks win.

## Lip-sync each chunk

After audio approval and spend approval, launch ONE tracked plan with a lip-sync node per
approved chunk:

- Each node calls `submit_job` on `fal-ai/sync-lipsync/v3/image-to-video` ONCE with
  `image_url` = the anchor still and `audio_url` = that chunk's trimmed audio (both HTTPS, never
  `s3://`). No duration/aspect param — the clip's length matches the audio and its frame matches
  the anchor. Read the result at `video.url`.
- Siblings have no dependencies and fan out in parallel. **No validation node and no stitch
  node here** — the per-segment human review (below) is the gate.
- A generation node calls the model once and stops; the Harness registers the handle, polls,
  and injects the resolved output. Do not loop on status inside a node.

## Review, then stitch

**Per-segment review is the gate before assembly — nothing is stitched until the user approves.**
After the lip-sync plan is terminal, call `get_plan_results` and build **ONE combined review
gallery of ALL segments, in script order** — do NOT leave the per-clip render tabs as the review
(each lip-sync node auto-renders its own single-video tab; scattered tabs are not a review).
**This is a REQUIRED step, not optional. You MUST make exactly ONE `show_media_gallery` call whose
`items` hold EVERY segment (label "Segment N", text: its line), before you present the review or move on
to stitch.** The per-clip `nle-timeline` tabs the lip-sync nodes already emitted do NOT satisfy this —
if the only render output for the review is one-tab-per-video, you have SKIPPED this step: stop and
emit the single combined `clip-gallery` first. (Self-check: the review render must be ONE manifest
with `template: "clip-gallery"` and N `galleryClips`, not N single-video manifests.)
A detached plan/DAG node can make the same call: name `show_media_gallery` in that node's `tools` (a plan node
gets only the tools it names); it writes the gallery straight into the chat's tabs.
Collect every segment into one gallery so the user watches the reel as a set. Use an
**interactive clip gallery** with `show_media_gallery` — one item per segment `{label, text: the chunk line, url: the clip, kind: "video",
status: "flagged" for any segment the user flags}`. Each segment plays inline with its own mark-in/out ruler;
the user confirms each (mouth tracks the words, identity/wardrobe stays consistent, voice/pace is
right, no leading blip), flags any to regenerate, and can **Clip** a segment to tighten its
head/tail (same batch loop as the audio trim). For a video gallery two more per-clip affordances
come free from the renderer (no skill code): **⤢ Expand** opens one clip in its own timeline tab
for frame-accurate analysis, and a batch **🎞 Extract frame** pulls a still from each marked clip
at its mark-in. This interactive gallery is the SUPERSET of a
static review page — same playable per-segment grid, plus trim/clip/flag. For a shareable,
self-contained page you can also produce the `build_comparison.py` + `assets/comparison-page.html`
HTML (the bundled `agent-olive-james/olive-lipsync-review.html` is the worked example — a
`<video>` grid with "play all in sequence"), but the interactive gallery is the primary review.

For each flagged clip, regenerate — re-synthesize that chunk (new take) and/or re-run the
lip-sync — and refresh the gallery. Assemble only once every segment is approved. Before
building the gallery, resolve each clip's job result and **drop any clip whose job returned an
error or no video URL** (a terminal job status is not proof of a usable clip) — this is cheap
result hygiene, not an automated quality gate.

**Assemble only after approval, as a separate dispatch.** Assemble accepted clips in script
order. **Feed the stitch the EXACT segment clip URLs you already resolved at the Phase 6 review
gate — the `galleryClips` urls / the `get_plan_results` lip-sync outputs you just approved — and
write them into the `assemble_reel.py` manifest VERBATIM, in script order. Do NOT re-discover the
inputs on the stitch dispatch: no `list_objects`/S3 listing, no `code_interpreter` "find the
clips", no searching for "the final reel" or a prior `*reel*`/`*crossfade*` file, no re-reading
other jobs — and NEVER a clip from another session (only this session's own output prefix, `artifacts/<this-session>/` or `projects/<project>/sessions/<this-session>/`,
or the shared `external/` outputs you produced this run). The approved N clips are already in hand;
the stitch just concatenates them into ONE output and returns — dispatch-and-return, no polling
loop. Re-hunting for the segments is exactly what turns this single concat into dozens of steps and
is how a DIFFERENT session's reel gets spliced in.** Each lip-sync clip already carries its own
audio, so assembly is a **concatenation, not a
mux**. **Ask the user for the inter-chunk pad** (unless they already gave it — "default" means 1.0 s:
use it, do not ask again) — a short **held-frame "breath"** on each clip's
tail so lines do not run together (default ~1.0s; a user may want 0 for a tight cut or more for a
deliberate beat). **The stitch is EXACTLY ONE action: RUN `scripts/assemble_reel.py`. Do NOT
hand-build the ffmpeg graph, and do NOT write your own concat/ffmpeg/ffprobe code in
`code_interpreter` cells (a `shots={...}` dict + loop, a manual `ffmpeg -f concat`, an ad-hoc
`ffprobe` probe — all forbidden).** Hand-rolling is the documented failure mode here: the sandbox
has no system ffmpeg/ffprobe and `imageio-ffmpeg` ships ffmpeg ONLY (no
ffprobe), which is why hand-rolling skips the probe step and then throws — the script
**self-provisions BOTH ffmpeg and ffprobe** (downloads the harness's staged ffmpeg build when missing), so
`FFMPEG_S3_URI=s3://<skills-bucket>/tools/ffmpeg python scripts/assemble_reel.py <manifest.json> s3://<skills-bucket>/<SESSION OUTPUT prefix>/olive_reel_final.mp4 --stitch-mode concat --pad-seconds <pad> --execute`
(the output is an `s3://` uri under the SESSION OUTPUT prefix the S3 CONTEXT names: the script renders
locally and UPLOADS it, and its report's `output` + `"uploaded": true` is the proof — a plain local path
is never uploaded, so never hand the user a reel link the report does not show as uploaded. The manifest
is `{"clips": [{"path": <the clip's https url or s3:// uri, as approved>}, ...]}` in script order — the
script downloads the clips itself, so do NOT download them first. Its frame rate comes from the clips;
pass `--fps` only to force one.)
(the RELATIVE path `scripts/assemble_reel.py`, exactly — the harness copies it into the sandbox's working
directory; the skill's `Location` (`/var/task/...`) is the agent's machine, not the sandbox; never go
looking for the script in S3)
runs with no setup — pass `FFMPEG_S3_URI` (the workspace bucket from the S3 CONTEXT guidance,
`SKILLS_S3_BUCKET`) so the sandbox reads ffmpeg/ffprobe with its own IAM role: in-account, no
public link, no external dependency. It writes the concat with a held last frame (`tpad`/freeze) + matching silence
between clips, H.264 `yuv420p` + AAC + `+faststart`, to the session output prefix, returning the
permanent HTTPS URL. Running that script IS the code_interpreter job — invoke it as a script; do
not reimplement its ffmpeg graph inline.

**ONE code_interpreter execution — no preparatory cells (this is the step-blowup trap).** The
stitch is a SINGLE `code_interpreter` run that invokes `assemble_reel.py` once with `--execute`,
then returns. Do NOT split it across cells and do NOT run any prep cell before it: no separate
`ffprobe`/inspection cell, no "download ffmpeg first" cell, no "list/find the clips" cell, no
per-clip loop. **Every code_interpreter cell is a fresh, empty sandbox** — each one re-downloads
the static ffmpeg/ffprobe build (~tens of MB) and re-fetches/re-probes all N input clips from
scratch. The script already does the download AND the probing internally in that single run, so
extra cells buy nothing and each added round-trip re-pays the full ffmpeg+clip cost — that
compounding is exactly what turned one concat into ~130+ steps. One cell in, one reel URL out.

**On a script error, FAIL LOUD — do not hand-roll a fallback.** If the run returns
`{"error": ...}` (or a non-zero exit), surface that error message to the user and STOP; fix the
manifest (usually a bad/expired clip URL or an fps/dimension mismatch the probe flagged) and
re-run the SAME script. Do NOT "recover" by writing your own `ffmpeg -f concat`/`ffprobe` cells —
the sandbox has no system ffmpeg/ffprobe and hand-rolling skips the probe the script depends on,
so it fails anyway after burning more steps. The script is the only supported path; a script
error is a signal to correct its input, never a licence to improvise a graph.

Follow the `video-processing`
skill for current ffmpeg/size constraints; do not assemble in `code_interpreter` when the total
input exceeds its documented limit.

## Cost — report actual for the anchor image, estimate before the paid batches

- **Anchor image:** report the ACTUAL cost after generating it (read the returned dimensions,
  one `get_pricing`, compute with the **media-references** cost preflight
  `media-references/scripts/estimate_cost.py`). One line, then stop for approval.
- **Voice + lip-sync:** estimate BEFORE generating and stop for approval. `synthesize_speech`
  reports `total_estimated_cost` per job; for lip-sync, read the model meter with
  `get_pricing`/`get_model_schema` and multiply by clip count / seconds. Emotion styles are ~3×
  the TTS synthesis cost — include that. Never generate a sample clip to "measure" cost.

## Validate every segment

Removed. Segment quality is confirmed by the **human per-segment review gallery** (Phase 6),
not by an automated validation node. Just apply the cheap result hygiene noted in "Review, then
stitch" — drop any clip whose job returned an error or no URL — then let the user approve or flag
each clip.

## Output contract

A `plan_and_execute_tool` call returns an async tracking acknowledgment, not the finished media;
after it is terminal call `get_plan_results` once. Deliver, in order:

1. The **per-segment review gallery** URL (one clip per chunk, flagged marked) plus
   an inline render, and ask the user to confirm or flag each segment. **Do not stitch yet.**
2. Only after every segment is approved (flagged ones regenerated) and the pad is chosen, stitch
   and return the final reel:
   - The permanent HTTPS reel URL.
   - The voice used (`voice_id` + whether cloned or catalog) and any `style`.
   - The lip-sync model (`fal-ai/sync-lipsync/v3/*`), the accepted segment count, and any
     regenerations.
   - The inter-chunk pad applied.
   - Remaining limitations, especially that the voice is an approximation.

Never put media bytes in the conversation. Never expose credentials, private bucket names, or
unverified claims of continuity.

## Bundled resources

Tested reference contracts (a detached Harness node applies the contract; it does not assume the
relative file is executable):

- `scripts/calculate_segment_length.py`: deterministic script **chunking** — `--pack` combines
  sentences into good-length segments. (Clip length is set by the TTS audio, so its frame/grid
  and duration-enum sizing modes are not used by this lip-sync workflow.)
- `scripts/build_comparison.py` + `assets/comparison-page.html`: a STATIC, shareable
  review/audition HTML page (video/audio cards) from a JSON manifest — escape every label + URL.
  The INTERACTIVE audition / review / **trim** surface is `show_media_gallery` (`clip-gallery`
  template: per-clip player, waveform, mark-in/out ruler, and batch "Trim/Clip all marked"), NOT this
  static page.
- `scripts/assemble_reel.py`: the ffmpeg **concatenation** graph with a held-frame breath pad
  between clips (each clip already carries its own audio — this is concat, not a mux). RUN it
  (`--stitch-mode concat --pad-seconds <pad> --execute`); it self-provisions ffmpeg+ffprobe in the
  sandbox (downloads the harness's staged ffmpeg build, which has ffprobe, when missing — `imageio-ffmpeg` has none).
- Cost preflight lives in **media-references** (`media-references/scripts/estimate_cost.py`), not
  bundled here: per-unit rate × units → per-clip and total USD (rate always from a live
  `get_pricing`, never baked in). Agent Olive uses it in the same turns it calls media-references.
