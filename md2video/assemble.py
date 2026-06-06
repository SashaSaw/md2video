"""Assemble per-scene (image, audio) pairs into a single MP4 with ffmpeg.

Each scene becomes a clip whose length equals its narration (plus a short tail
of silence so it doesn't feel rushed), with a gentle cross-friendly fade. Clips
are concatenated into the final video. We also emit a sidecar .srt subtitle
track built from the narration, which you can burn in or ship alongside.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

TAIL_PAD = 0.6      # seconds of silence appended after each narration
FADE = 0.25         # fade in/out per clip


def _clip(scene, work: Path) -> Path:
    dur = scene.duration + TAIL_PAD
    out = work / f"clip_{scene.index:03d}.mp4"
    fade_out_start = max(0.0, dur - FADE)
    vf = (f"scale=1920:1080:force_original_aspect_ratio=decrease,"
          f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0xfaf9f5,"
          f"fps=30,format=yuv420p,"
          f"fade=t=in:st=0:d={FADE},fade=t=out:st={fade_out_start:.3f}:d={FADE}")
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-i", scene.image_path,
        "-i", scene.audio_path,
        "-filter_complex", f"[1:a]apad=pad_dur={TAIL_PAD}[a]",
        "-map", "0:v", "-map", "[a]",
        "-vf", vf,
        "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        str(out)], check=True)
    return out


def _srt_time(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    ms = int((s - int(s)) * 1000)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"


def _write_srt(scenes, path: Path) -> None:
    lines, t = [], 0.0
    for i, s in enumerate(scenes, 1):
        end = t + s.duration + TAIL_PAD
        text = s.narration.strip() or s.title
        lines.append(f"{i}\n{_srt_time(t)} --> {_srt_time(end)}\n{text}\n")
        t = end
    path.write_text("\n".join(lines), encoding="utf-8")


def assemble(scenes, out_path: str, work_dir: str) -> None:
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    clips = [_clip(s, work) for s in scenes]

    listfile = work / "concat.txt"
    listfile.write_text(
        "".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")

    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listfile),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        out_path], check=True)

    _write_srt(scenes, Path(out_path).with_suffix(".srt"))
