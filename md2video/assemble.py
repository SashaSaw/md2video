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


def _tail(scene) -> float:
    # Full pause after the last beat of a slide; a short breath between reveals.
    return TAIL_PAD if getattr(scene, "is_slide_end", True) else 0.25


_PAD = ("scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0d0e12")


def _fades(dur: float, fade_in: bool, fade_out: bool) -> list[str]:
    f = []
    if fade_in:
        f.append(f"fade=t=in:st=0:d={FADE}")
    if fade_out:
        f.append(f"fade=t=out:st={max(0.0, dur - FADE):.3f}:d={FADE}")
    return f


def _clip_gif(scene, work: Path, gif_path: str) -> Path:
    """Loop an animated gif for the scene's duration, overlay the caption chrome,
    and mux the narration. The gif animates; the caption is a static transparent PNG."""
    tail = _tail(scene)
    dur = scene.duration + tail
    out = work / f"clip_{scene.index:03d}.mp4"
    overlay = getattr(scene, "overlay_path", "")
    fade_in = getattr(scene, "is_slide_start", True)
    fade_out = getattr(scene, "is_slide_end", True)

    inputs = ["-stream_loop", "-1", "-i", gif_path]
    audio_idx = 1
    if overlay:
        inputs += ["-loop", "1", "-i", overlay]
        audio_idx = 2
    inputs += ["-i", scene.audio_path]

    parts = [f"[0:v]{_PAD},fps=30[bg]"]
    last = "bg"
    if overlay:
        parts.append("[bg][1:v]overlay=0:0[ov]")
        last = "ov"
    vchain = _fades(dur, fade_in, fade_out) + ["format=yuv420p"]
    parts.append(f"[{last}]{','.join(vchain)}[v]")
    parts.append(f"[{audio_idx}:a]apad=pad_dur={tail}[a]")

    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        *inputs,
        "-filter_complex", ";".join(parts),
        "-map", "[v]", "-map", "[a]",
        "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        str(out)], check=True)
    return out


def _clip(scene, work: Path) -> Path:
    gif_path = getattr(scene, "gif_path", "")
    if gif_path:
        return _clip_gif(scene, work, gif_path)
    tail = _tail(scene)
    dur = scene.duration + tail
    out = work / f"clip_{scene.index:03d}.mp4"
    fade_out_start = max(0.0, dur - FADE)
    # Only fade at slide boundaries; mid-slide reveals hard-cut so a new point
    # simply appears (reads as a reveal, not a flash). Defaults True for plain scenes.
    fade_in = getattr(scene, "is_slide_start", True)
    fade_out = getattr(scene, "is_slide_end", True)
    pad = ("scale=1920:1080:force_original_aspect_ratio=decrease,"
           "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0d0e12,"
           "fps=30,format=yuv420p")
    fades = []
    if fade_in:
        fades.append(f"fade=t=in:st=0:d={FADE}")
    if fade_out:
        fades.append(f"fade=t=out:st={fade_out_start:.3f}:d={FADE}")
    vf = ",".join([pad] + fades)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-i", scene.image_path,
        "-i", scene.audio_path,
        "-filter_complex", f"[1:a]apad=pad_dur={tail}[a]",
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
        end = t + s.duration + _tail(s)
        text = s.narration.strip() or getattr(s, "title", "") or "…"
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
