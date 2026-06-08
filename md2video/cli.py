"""md2video — turn a markdown explainer into a narrated video.

Usage:
    python -m md2video.cli INPUT.md -o explainer.mp4 [--config config.yaml]
    python -m md2video.cli INPUT.md --dry-run        # parse + print scenes only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .i18n import LANGUAGES, get_language
from .pipeline import build_video, load_config, render_storyboard
from .parse import parse_markdown
from .storyboard import Storyboard, build_storyboard


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Markdown -> narrated explainer video")
    ap.add_argument("input", nargs="?", help="path to the markdown file")
    ap.add_argument("-o", "--output", default="explainer.mp4")
    ap.add_argument("--from-storyboard", metavar="IN.json",
                    help="render an (edited) storyboard JSON to video (Phase 2 only, no LLM)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--language", default="en",
                    help=f"output language ({', '.join(LANGUAGES)}); non-source needs an LLM")
    ap.add_argument("--voice", default=None,
                    help="Kokoro voice id (defaults to the language's default voice)")
    ap.add_argument("--work", default="build", help="scratch dir for frames/audio")
    ap.add_argument("--style", choices=["dark_keynote", "editorial_light", "minimal_statement"],
                    help="slide style preset")
    ap.add_argument("--theme", choices=["dark", "light"], help="slide theme")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and list scenes, then exit")
    ap.add_argument("--storyboard", metavar="OUT.json",
                    help="distill into an editable storyboard JSON and exit (Phase 1 only)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    cfg.setdefault("style", {})
    if args.style:
        cfg["style"]["preset"] = args.style
    if args.theme:
        cfg["style"]["theme"] = args.theme

    lang = get_language(args.language)
    if args.voice:
        cfg["tts"]["voice"] = args.voice
    elif not lang.is_source:
        cfg["tts"]["voice"] = lang.default_voice

    if args.from_storyboard:
        sb = Storyboard.from_dict(json.loads(Path(args.from_storyboard).read_text()))
        if args.style:
            sb.style["preset"] = args.style
        if args.theme:
            sb.style["theme"] = args.theme
        sblang = get_language(sb.meta.get("language", "en"))
        if not args.voice and not sblang.is_source:
            cfg["tts"]["voice"] = sblang.default_voice
        out_path = args.output
        if not sblang.is_source:
            p = Path(out_path)
            out_path = str(p.with_suffix("")) + f".{sblang.code}" + p.suffix

        _last = {"stage": None}

        def rprogress(stage, pct):
            if stage != _last["stage"]:
                labels = {"render": "1/3 Rendering slides",
                          "tts": "2/3 Synthesizing speech",
                          "assemble": "3/3 Assembling video"}
                print(f"{labels.get(stage, stage)}...")
                _last["stage"] = stage

        beats = render_storyboard(sb, out_path, cfg=cfg, work_dir=args.work,
                                  progress=rprogress)
        total = sum(b.duration for b in beats)
        nslides = len({b.slide_id for b in beats})
        print(f"Done -> {out_path}  (~{total/60:.1f} min, {nslides} slides, {len(beats)} beats)")
        print(f"Subtitles -> {Path(out_path).with_suffix('.srt')}")
        return 0

    if not args.input:
        ap.error("input markdown file is required (unless --from-storyboard)")

    if args.storyboard:
        print("Distilling storyboard...")
        sb = build_storyboard(Path(args.input).read_text(encoding="utf-8"),
                              cfg=cfg, language=args.language,
                              title=Path(args.input).stem,
                              source_filename=Path(args.input).name)
        Path(args.storyboard).write_text(
            json.dumps(sb.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Storyboard ({len(sb.slides)} slides) -> {args.storyboard}")
        for s in sb.slides:
            extra = f"{len(s.points)} pts" if s.points else (s.kind)
            print(f"  [{s.id}] {s.kind:9s} {s.headline!r}  ({extra})")
        return 0

    scenes = parse_markdown(Path(args.input).read_text(encoding="utf-8"))
    print(f"Parsed {len(scenes)} scenes.")

    if args.dry_run:
        for s in scenes:
            print(f"  [{s.index:02d}] {s.kind:8s} {s.title}")
        return 0

    _last = {"stage": None}

    def progress(stage, pct):
        if stage != _last["stage"]:
            labels = {"narrate": "1/4 Distilling storyboard",
                      "render": "2/4 Rendering slides",
                      "tts": "3/4 Synthesizing speech",
                      "assemble": "4/4 Assembling video"}
            print(f"{labels.get(stage, stage)}...")
            _last["stage"] = stage

    out_path = args.output
    if not lang.is_source:
        p = Path(out_path)
        out_path = str(p.with_suffix("")) + f".{lang.code}" + p.suffix

    beats = build_video(Path(args.input).read_text(encoding="utf-8"),
                        out_path, cfg=cfg, work_dir=args.work,
                        progress=progress, language=args.language,
                        title=Path(args.input).stem)

    total = sum(b.duration for b in beats)
    nslides = len({b.slide_id for b in beats})
    print(f"Done -> {out_path}  (~{total/60:.1f} min, {nslides} slides, "
          f"{len(beats)} beats, {lang.name})")
    print(f"Subtitles -> {Path(out_path).with_suffix('.srt')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
