"""md2video — turn a markdown explainer into a narrated video.

Usage:
    python -m md2video.cli INPUT.md -o explainer.mp4 [--config config.yaml]
    python -m md2video.cli INPUT.md --dry-run        # parse + print scenes only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import build_video, load_config
from .parse import parse_markdown


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Markdown -> narrated explainer video")
    ap.add_argument("input", help="path to the markdown file")
    ap.add_argument("-o", "--output", default="explainer.mp4")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--work", default="build", help="scratch dir for frames/audio")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and list scenes, then exit")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    scenes = parse_markdown(Path(args.input).read_text(encoding="utf-8"))
    print(f"Parsed {len(scenes)} scenes.")

    if args.dry_run:
        for s in scenes:
            print(f"  [{s.index:02d}] {s.kind:8s} {s.title}")
        return 0

    _last = {"stage": None}

    def progress(stage, pct):
        if stage != _last["stage"]:
            labels = {"narrate": "1/4 Writing narration",
                      "render": "2/4 Rendering visuals",
                      "tts": "3/4 Synthesizing speech",
                      "assemble": "4/4 Assembling video"}
            print(f"{labels.get(stage, stage)}...")
            _last["stage"] = stage

    scenes = build_video(Path(args.input).read_text(encoding="utf-8"),
                         args.output, cfg=cfg, work_dir=args.work,
                         progress=progress)

    total = sum(s.duration for s in scenes)
    print(f"Done -> {args.output}  (~{total/60:.1f} min, {len(scenes)} scenes)")
    print(f"Subtitles -> {Path(args.output).with_suffix('.srt')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
