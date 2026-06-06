"""Shared build pipeline used by both the CLI and the web server.

`build_video()` runs the full parse -> narrate -> render -> tts -> assemble
chain and reports coarse, weighted progress through an optional callback so a
UI can show a live progress bar. Keeping this in one place means the CLI and the
web API never drift apart.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import assemble, narrate, render, tts
from .parse import parse_markdown

DEFAULTS = {
    "tts": {"backend": "say", "voice": "Daniel", "rate": 180},
    "narration": {"fallback_only": False},
}

# Each stage's share of the 0..100 progress bar (must sum to 100).
_STAGE_WEIGHTS = {"narrate": 15, "render": 45, "tts": 30, "assemble": 10}
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
    """Build a per-stage callback that maps (done, total) into overall percent.

    `progress` is called as progress(stage: str, percent: int).
    """
    base = {}
    acc = 0
    for stage in _STAGE_ORDER:
        base[stage] = acc
        acc += _STAGE_WEIGHTS[stage]

    def stage_cb(stage: str):
        def cb(done: int, total: int):
            if not progress:
                return
            frac = (done / total) if total else 1.0
            pct = base[stage] + frac * _STAGE_WEIGHTS[stage]
            progress(stage, int(pct))
        return cb

    return stage_cb


def build_video(md_text: str, out_path: str, *, cfg: dict,
                work_dir: str, progress=None):
    """Render `md_text` into a narrated video at `out_path`.

    Returns the list of Scene objects (which carry per-scene duration, etc.).
    `progress(stage, percent)` is invoked throughout if provided.
    """
    work = Path(work_dir)
    stage_cb = _make_reporter(progress)

    scenes = parse_markdown(md_text)
    if not scenes:
        raise ValueError("No scenes parsed from the markdown — is it empty?")

    if progress:
        progress("narrate", 0)
    narrate.narrate_scenes(
        scenes,
        fallback_only=cfg.get("narration", {}).get("fallback_only", False),
        progress=stage_cb("narrate"),
    )
    render.render_scenes(scenes, str(work / "frames"), progress=stage_cb("render"))
    tts.synthesize_scenes(scenes, str(work / "audio"), cfg["tts"],
                          progress=stage_cb("tts"))
    assemble.assemble(scenes, out_path, str(work / "clips"))
    if progress:
        progress("assemble", 100)

    return scenes
