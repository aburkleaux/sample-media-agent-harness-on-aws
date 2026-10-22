---
name: render-shot-segmentation
description: Renders shot segmentation JSON as a timeline of detected shots with their start and end times. Used when a media analysis result contains shot boundaries.
handles: application/json, .json
detect: shots
template: nle-timeline
tool: shot-segmentation
deliverable: shots.json
markers: shots[] time=start_time end=end_time label=shot_id category=shot_type
duration: actual_duration_sec
summary: n_shots, actual_duration_sec, actual_frame_count
---

# Render: shot segmentation (catalog declaration)

> **Not an agent skill.** The deterministic renderer reads the **frontmatter above**
> (`detect`/`template`/`markers`/`duration`/`summary`) to build the manifest. This body is human
> reference only.

Projects `shots.json` onto the timeline as **markers**, one per detected shot.
`actual_duration_sec` is authoritative (it is why the timeline reads 30.03s instead of the 600s
fallback); `shot_type` becomes the marker category so black frames and content shots are
visually distinguishable.

The same mapping is also built into the renderer in code (`render_agent._shot_markers_from_shots_json`),
so a `shots.json` always renders as shot markers — even alone, with no media, and even if this
catalog entry is unavailable. Never hand-write a shots manifest or a marker like
`{track_id, source: "shots.json"}`: the UI cannot draw it, and a richer existing manifest is kept
over a poorer one anyway.

When the source video is known it is the FIRST track (the job's proxy `segment_*.mp4` rides
beside it), and the manifest's `sourceLocation` is that video's canonical `s3://bucket/key`, so
the shots fold onto the source video's tab.

Shape:
```json
{ "n_shots": 9, "actual_duration_sec": 30.03,
  "shots": [ { "shot_id": 0, "start_time": 0.0, "end_time": 0.967, "shot_type": "black" } ] }
```
