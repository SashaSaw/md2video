# md2video — Vision & Implementation Plan

## 1. Vision

**md2video turns a markdown document into a narrated explainer video you can
actually watch and understand.** Today it's a CLI pipeline; the goal is a small,
self-hosted studio you open in a browser: drop in a markdown file, pick a voice,
hit generate, and watch the result — with a library of everything you've made.

The longer-term bet is **interactive comprehension**: if a part of a video
doesn't land, you ask a question in plain language, and the app finds the
relevant slice of the source, researches it, and regenerates a clearer segment.
The video becomes a conversation, not a one-shot render.

### Principles

- **Deterministic substance, generative polish.** Real diagrams and real text
  drive the video (no hallucinated generative video). LLMs are used for *scripting*
  and *research*, not for inventing the visuals.
- **Local-first.** Rendering (Chromium), speech (Kokoro via mlx-audio), and
  assembly (ffmpeg) all run on the user's machine. No upload of source docs to
  third parties beyond the optional Anthropic narration call.
- **The library is the product.** Every render is kept, browsable, replayable,
  downloadable — the value compounds over time.

## 2. Where we are (existing pipeline)

```
markdown ─▶ parse ─▶ ┬─▶ render visuals (Chromium → PNG) ─┐
                     └─▶ narrate (Claude → script) ─▶ TTS ─┴─▶ ffmpeg ─▶ mp4 + srt
```

| Module | Responsibility |
|---|---|
| `parse.py` | Split the doc into ordered scenes (prose / Mermaid / table). |
| `narrate.py` | Rewrite each scene as spoken voiceover (Claude; verbatim fallback). |
| `render.py` | Render each scene to a 1920×1080 PNG in headless Chromium. |
| `tts.py` | Synthesize narration to wav (Kokoro HTTP / `say` / pyttsx3). |
| `assemble.py` | ffmpeg: one clip per scene, concatenate → mp4 + sidecar srt. |
| `cli.py` | Orchestrates a single run. |

## 3. Target architecture

```
┌──────────────────────────────────────────────────────────┐
│  Browser UI (static SPA: HTML + CSS + vanilla JS)          │
│  • Import / pick .md   • Choose voice   • Player           │
│  • Library grid        • Download       • (later) Ask       │
└───────────────┬──────────────────────────────────────────┘
                │ HTTP / JSON
┌───────────────▼──────────────────────────────────────────┐
│  FastAPI backend (md2video.web.app)                        │
│  • POST /api/videos       → enqueue build job              │
│  • GET  /api/jobs/{id}     → live status / stage / pct     │
│  • GET  /api/videos[/{id}] → library + metadata           │
│  • GET  /media/{id}/...    → stream video / poster        │
│  • DELETE /api/videos/{id} → remove                       │
└───────────────┬──────────────────────────────────────────┘
                │ in-process (single-worker queue)
┌───────────────▼──────────────────────────────────────────┐
│  md2video.pipeline.build_video(...)  ← shared by CLI + web │
│  parse → narrate → render → tts → assemble (+ progress cb) │
└──────────────────────────────────────────────────────────┘

Storage:  library/<id>/{meta.json, source.md, video.mp4, video.srt, poster.jpg}
```

### Why these choices

- **FastAPI + a static vanilla SPA** — no Node build step, runs with one command
  (`md2video-web`), matches the existing Python codebase. The frontend is small
  enough that a framework would add more ceremony than value for the MVP.
- **Single-worker job queue** — Playwright (sync) and ffmpeg are resource-heavy;
  serializing builds avoids browser-concurrency issues and CPU thrash. Jobs queue
  rather than fail.
- **Filesystem library, not a database** — each video is a self-contained folder
  with a `meta.json`. Trivially inspectable, backup-able, and survives restarts.

## 4. Implementation phases

### Phase 1 — MVP (this iteration)
- [x] Refactor the CLI's orchestration into a reusable `pipeline.build_video()`
      with an optional progress callback; per-scene progress in render/tts/narrate.
- [x] FastAPI backend: create/list/get/delete videos, job status, media serving.
- [x] Filesystem library with `meta.json` + poster-frame extraction.
- [x] Curated Kokoro voice list (server probe with fallback).
- [x] SPA: import/pick `.md`, choose voice, generate with live progress, inline
      player, library grid, download, dark/light theme.
- [x] `md2video-web` entrypoint; deps wired into `pyproject.toml`.

### Phase 2 — Quality & feedback
- Per-scene progress with thumbnails as they render.
- Choose narration mode (Claude vs verbatim) and model from the UI.
- Voice preview (synthesize one sample sentence on hover/click).
- Scene-level inspector: see the parsed scenes + scripts before/after a build.
- Burned-in word-level captions (Whisper/Kokoro timings) toggle.

### Phase 3 — Interactive comprehension ("explain this part")
- In the player, the user types a question about a moment/segment.
- Backend maps the timestamp → originating scene(s) → source markdown span.
- A research step (Claude + optional web search) expands that span into a richer
  mini-explainer (more steps, analogies, a clarifying diagram if useful).
- Regenerate **only** the affected scene(s) and splice into a new video version,
  preserving the rest. Versions are tracked under the same library entry.

### Phase 4 — Production polish
- Diagram step-highlighting synced to narration (dim/highlight Mermaid nodes).
- Ken Burns motion on diagram scenes.
- Optional Remotion renderer for animated reveals.
- Multi-user / auth if it ever needs to be shared.

## 5. Data model

```jsonc
// library/<id>/meta.json
{
  "id": "20260606-153012-widget-service",
  "title": "Widget service — how it fits together",
  "source_filename": "sample.md",
  "voice": "af_heart",
  "status": "ready",          // queued | processing | ready | error
  "stage": "assemble",        // last reported stage
  "progress": 100,            // 0..100
  "error": null,
  "created_at": "2026-06-06T15:30:12Z",
  "duration": 60.5,           // seconds
  "scene_count": 5
}
```

## 6. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Kokoro server not running | Detect at startup; surface a clear banner; allow `say` fallback via config. |
| Long builds block UI | Async job + polling; single-worker queue with visible "queued" state. |
| Playwright/ffmpeg missing | Startup health check endpoint reports tool availability. |
| Large media in git | Library dir is git-ignored; only code is versioned. |

## 7. Out of scope (for now)
- Authentication / multi-tenant hosting.
- Generative B-roll / talking-head avatars.
- Cloud rendering. Everything stays local-first.
</content>
</invoke>
