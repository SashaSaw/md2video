"""Shared build pipeline used by both the CLI and the web server.

Two phases, with an editable Storyboard between them:
  Phase 1 (LLM, once):  markdown -> build_storyboard()  -> Storyboard
  Phase 2 (no LLM):      Storyboard -> render_storyboard() -> mp4

`build_video()` runs both back-to-back (the one-shot path). The web app runs the
phases separately so a human can edit the storyboard in between. Progress is
reported via an optional callback so the UI can show a live bar.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import assemble, render, tts
from .animate import expand_slides_to_beats
from .storyboard import build_storyboard

DEFAULTS = {
    "tts": {"backend": "say", "voice": "Daniel", "rate": 180},
    "narration": {"fallback_only": False},
    "translation": {"backend": "mlx", "model": "unsloth/Qwen3.6-27B-UD-MLX-4bit"},
    "distill": {"backend": "mlx", "model": "unsloth/Qwen3.6-27B-UD-MLX-4bit"},
    "style": {"preset": "dark_keynote", "theme": "dark"},
}

# Each stage's share of the 0..100 progress bar (must sum to 100).
_STAGE_WEIGHTS = {"narrate": 25, "render": 40, "tts": 25, "assemble": 10}
_STAGE_ORDER = ["narrate", "render", "tts", "assemble"]


def load_config(path: str | None) -> dict:
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if path and Path(path).exists():
        user = yaml.safe_load(Path(path).read_text()) or {}
        for k, v in user.items():
            cfg.setdefault(k, {})
            cfg[k].update(v) if isinstance(v, dict) else cfg.update({k: v})
    return cfg


def _make_reporter(progress):
    """Build a per-stage callback mapping (done, total) -> overall percent."""
    base, acc = {}, 0
    for stage in _STAGE_ORDER:
        base[stage] = acc
        acc += _STAGE_WEIGHTS[stage]

    def stage_cb(stage: str):
        def cb(done: int, total: int):
            if not progress:
                return
            frac = (done / total) if total else 1.0
            progress(stage, int(base[stage] + frac * _STAGE_WEIGHTS[stage]))
        return cb

    return stage_cb


def render_storyboard(sb, out_path: str, *, cfg: dict, work_dir: str,
                      progress=None, stage_cb=None):
    """Phase 2: turn an (edited) Storyboard into a video. No LLM calls."""
    work = Path(work_dir)
    stage_cb = stage_cb or _make_reporter(progress)

    beats = expand_slides_to_beats(sb.slides, sb.style)
    if not beats:
        raise ValueError("Storyboard has no slides to render.")

    render.render_beats(beats, str(work / "frames"), progress=stage_cb("render"))
    tts.synthesize_scenes(beats, str(work / "audio"), cfg["tts"],
                          progress=stage_cb("tts"))
    assemble.assemble(beats, out_path, str(work / "clips"))
    if progress:
        progress("assemble", 100)
    return beats


def build_video(md_text: str, out_path: str, *, cfg: dict, work_dir: str,
                progress=None, language: str = "en", title: str | None = None,
                style_prompt: str | None = None):
    """One-shot: distill a storyboard then render it. Returns the beats."""
    stage_cb = _make_reporter(progress)
    sb = build_storyboard(md_text, cfg=cfg, language=language, title=title,
                          progress=stage_cb("narrate"),
                          style_prompt=style_prompt)
    return render_storyboard(sb, out_path, cfg=cfg, work_dir=work_dir,
                             progress=progress, stage_cb=stage_cb)
