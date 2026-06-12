# md2video

Turn a markdown explainer (prose + tables + Mermaid diagrams) into a narrated
video. It renders your *actual* diagrams, writes a plain-language script with an
LLM, speaks it with a TTS engine, and stitches everything together with ffmpeg.

It does **not** use generative video models. For diagram-heavy explainers those
hallucinate and can't reproduce your real diagrams or text. The substance comes
from a deterministic slide-and-voiceover pipeline; generative video is only
worth adding later as optional decorative B-roll.

## Pipeline

```
markdown ─▶ parse ─▶ ┬─▶ render visuals (Chromium: Mermaid + slides → PNG) ─┐
                     └─▶ narrate (Claude → script) ─▶ TTS (voice + length) ──┴─▶ ffmpeg ─▶ mp4 + srt
```

The visual track and the narration track are produced independently, then each
slide is held on screen for exactly as long as its narration audio runs.

## What each module does

| Module | Responsibility |
|---|---|
| `parse.py` | Split the doc into ordered *scenes*: lead prose, each Mermaid block, each table. One scene = one visual + one narration chunk. |
| `narrate.py` | Ask Claude to rewrite each scene as spoken voiceover. Diagrams get a step-by-step walkthrough from the Mermaid source. Falls back to raw text with no API key. |
| `render.py` | Render each scene to a 1920×1080 PNG in headless Chromium. Mermaid renders natively in the browser; content that overflows is scaled to fit. |
| `tts.py` | Synthesize each script to a wav and probe its true duration. Backends: `kokoro`, `say`, `pyttsx3`. |
| `images.py` | Local AI image generation for image slides via mflux (Z-Image / FLUX on Apple Silicon). |
| `gifs.py` | Fetch well-known reaction GIFs from Giphy for funny GIF slides (provider-abstracted). |
| `assemble.py` | ffmpeg: build one clip per scene (image held for its audio length, GIF slides looped + caption-composited, gentle fades), concatenate, emit `.mp4` + sidecar `.srt`. |
| `cli.py` | Orchestrates the run. |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .                     # installs the package + deps
playwright install chromium          # one-time, for the renderer
# ffmpeg + ffprobe must be on PATH (brew install ffmpeg)
cp config.example.yaml config.yaml
export ANTHROPIC_API_KEY=sk-...      # optional; omit for verbatim narration
```

## Usage

### Web studio (recommended)

A browser UI to import/drop a markdown file, pick a voice, watch the result, and
browse a library of everything you've made — with live build progress, downloads,
and a dark/light theme.

```bash
md2video-web                 # serves http://127.0.0.1:8001
```

Then open the URL, drag in a `.md`, pick a **language** and a Kokoro voice, and
hit **Generate**. The doc is distilled into an editable **storyboard**: review the
slides, tweak text, pick a **style preset** + theme and a **per-slide animation**,
then generate the video — changing styles/animations never re-runs the LLM.
Videos are kept under `library/<id>/` (git-ignored). Requires the local Kokoro TTS
server running (see *TTS backends*). See [VISION.md](VISION.md) for the design.

### CLI

```bash
# See how the doc splits into scenes (no rendering, no API calls):
md2video examples/sample.md --dry-run

# Full build (English, fully local), choosing a style preset:
md2video examples/sample.md -o explainer.mp4 --style dark_keynote --theme dark

# Inspect / edit the storyboard before rendering (the human-in-the-loop checkpoint):
md2video examples/sample.md --storyboard sb.json      # Phase 1: distill only
#   ...edit sb.json (headlines, points, per-slide `animation`, `style`)...
md2video --from-storyboard sb.json -o explainer.mp4   # Phase 2: render, no LLM

# Spanish build (translates the whole doc, then distills + speaks it):
md2video examples/sample.md -o explainer.mp4 --language es
# -> writes explainer.es.mp4 + explainer.es.srt
```

## Slides, styles & animations

Slides are **distilled for video**, not dumped from markdown: an LLM writes a short
headline + a few key points per slide (with highlighted keywords) and keeps the
detail in the spoken script. A deterministic validator enforces hard budgets
(≤10-word headlines, ≤3 short points) so slides never become walls of text.

- **Style presets** (`--style`): `dark_keynote`, `editorial_light`, `minimal_statement`, each in `dark`/`light`.
- **Per-slide animations**: points reveal one-at-a-time in sync with narration
  (`sequential`/`spotlight`), tables render as comparison **cards**, code shows a few
  highlighted lines, and **flow diagrams animate step-by-step** — each box appears and
  is highlighted as the narration reaches it.
- The pipeline splits at a human checkpoint: `markdown → distill → storyboard.json`
  (LLM, once) → *edit* → `render → tts → assemble → mp4` (no LLM). Reuses the local
  Qwen from `translation` by default; set `distill.backend: none` for a no-LLM heuristic.

## Editing the storyboard (web)

After distillation the web editor lets you shape the deck before rendering — and
changing animations/styles/manual text never re-runs the LLM:

- **Hand-edit**: rewrite headlines/points/script; add/remove points, table cards,
  and diagram steps; **add slides** (Title / Points / Statement / Image / GIF); reorder
  (↑/↓), delete, and **Undo**. Slides and units have stable IDs; every save snapshots
  for restore.
- **Per-slide natural language**: a prompt box on each slide ("make this punchier",
  "add a point about retries", "rewrite the 2nd step") revises just that slide.
- **Tone of voice**: rewrite the whole script in a tone (Conversational / Formal /
  Energetic / Plain / custom) — meaning, structure, code/URLs/numbers preserved.
- **"Ask" the whole deck**: a top-level box proposes structured edits (add an intro
  title, remove a slide, reorder, retone) which you **review then apply**.
- **AI image slides** (optional): add an Image slide, type a prompt, and generate a
  full-bleed background locally. Install with `pip install -e ".[image]"` (mflux);
  default model is the non-gated **Z-Image-Turbo** (`schnell` also works but is gated
  on HuggingFace). Configure under `image:` in `config.yaml`.
- **Funny GIF slides** (optional): a **✨ Make it funnier** button lets the LLM pick
  the best comedic beats and drop in well-known reaction GIFs (e.g. "mind blown",
  "mic drop") — or add a GIF slide by hand, type a search, and **↻ Another** to cycle
  results. Each gif slide gets an **anecdotal voiceover** (a spoken joke/example that
  lands the point while the gif plays). GIFs are fetched from Giphy and **actually
  animate** in the final video (looped over the narration, caption composited on top).
  - **Auto at generation time**: the **Generation prompt** is also read for intent —
    ask for something "fun", "entertaining", "dryly funny" etc. and distillation
    infers that humour fits and weaves in gif asides automatically (it self-gates, so
    serious/formal decks stay clean). Tune it after with the editor controls.
  - Needs a free key from [developers.giphy.com](https://developers.giphy.com): set
    `GIPHY_API_KEY` (or `gif.api_key` in `config.yaml`). Content rating defaults to
    `pg-13` since the model picks unattended. GIF features stay hidden/inactive until
    a key is configured. (Auto-insertion runs in the web studio; the CLI `--prompt`
    still steers narration tone but doesn't fetch gifs.)

## Languages

The output language is chosen per build (`--language`, or the dropdown in the web
UI). Adding a language is a single entry in [`md2video/i18n.py`](md2video/i18n.py).

- **English** is the source language and runs **fully offline** (no translation step).
- **Other languages** translate the *whole markdown* first — prose, tables, and
  Mermaid labels — so the slides are in the target language too, not just the
  audio. TTS then uses the matching Kokoro voices (e.g. Spanish: `ef_dora`,
  `em_alex`, `em_santa`).

Translation runs **locally by default** via a Qwen LLM through `mlx-lm` (Apple
Silicon) — no cloud, no API key:

```bash
pip install -e ".[local-llm]"     # installs mlx-lm
```

The model (`unsloth/Qwen3.6-27B-UD-MLX-4bit`, ~15 GB) downloads on first use;
change it under `translation:` in `config.yaml`. For higher narration quality,
set `ANTHROPIC_API_KEY` (Claude rewrites each scene); otherwise narration is the
translated slide text spoken verbatim. To translate via Claude instead of
locally, set `translation.backend: anthropic`.

## TTS backends

- **`say`** (default) — macOS built-in. Zero setup on Apple Silicon. `say -v '?'`
  lists voices.
- **`kokoro`** — your local mlx-audio + Kokoro setup, the same engine behind
  `speak-mcp`. Set `tts.backend: kokoro` and confirm the `command` template in
  `config.yaml` matches the invocation your speak-mcp server already uses
  (`{model} {voice} {text} {prefix}` are substituted). This keeps everything
  on-device and gives you a much nicer voice than `say`.
- **`pyttsx3`** — cross-platform offline fallback.

## Upgrade paths (in rough order of payoff)

1. **Diagram step-highlighting.** Right now a diagram is shown whole while its
   walkthrough plays. The renderer already uses a real browser, so you can have
   `narrate.py` return the node IDs per sentence, then dim/highlight each
   Mermaid node (`.node` SVG elements) in sync with TTS word timings. This is
   the single biggest quality jump for "goes through the diagrams".
2. **Word-level captions.** Kokoro/Whisper can give word timestamps; burn them
   in with the `subtitles` filter instead of the sidecar `.srt`.
3. **Ken Burns motion.** A slow `zoompan` on diagram scenes adds life. Left out
   of the default for robustness.
4. **Remotion instead of ffmpeg.** If you want real production value, Remotion
   (React-based programmatic video) is a strong fit for your stack — you'd write
   each scene as a component, render Mermaid in-browser natively, animate
   reveals, and sync audio on a timeline. Keep `parse.py` + `narrate.py` +
   `tts.py` as-is and swap `render.py` + `assemble.py` for a Remotion project.

## Notes

- `parse.py` is pure stdlib and has a `__main__` for quick inspection:
  `python md2video/parse.py orientation.md`.
- Everything except the renderer runs without Chromium, so `--dry-run` works on
  a fresh checkout before you install browsers.
