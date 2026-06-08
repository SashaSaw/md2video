"""Synthesize narration to per-scene audio clips and measure their length.

Backends (pick one in config.yaml -> tts.backend):

  "kokoro"  - your local mlx-audio + Kokoro setup (same engine as speak-mcp).
              Runs a configurable shell command; defaults to mlx_audio's CLI.
              Verify the command against your own speak-mcp invocation.
  "say"     - macOS built-in `say` -> aiff, converted to wav by ffmpeg.
              Zero-dependency default on your M-series Mac.
  "pyttsx3" - cross-platform offline fallback (pip install pyttsx3).

Every backend writes a wav and we read its true duration with ffprobe, which
is what drives how long each slide stays on screen.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path


def _ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", str(path)],
        capture_output=True, text=True, check=True)
    return float(json.loads(out.stdout)["format"]["duration"])


def _say(text: str, wav: Path, cfg: dict) -> None:
    aiff = wav.with_suffix(".aiff")
    voice = cfg.get("voice", "Daniel")
    rate = str(cfg.get("rate", 180))
    subprocess.run(["say", "-v", voice, "-r", rate, "-o", str(aiff), text],
                   check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff),
                    "-ar", "44100", "-ac", "2", str(wav)], check=True)
    aiff.unlink(missing_ok=True)


def _pyttsx3(text: str, wav: Path, cfg: dict) -> None:
    import pyttsx3
    engine = pyttsx3.init()
    engine.setProperty("rate", cfg.get("rate", 175))
    engine.save_to_file(text, str(wav))
    engine.runAndWait()


def _kokoro(text: str, wav: Path, cfg: dict) -> None:
    # Talk to the locally-running mlx-audio Kokoro server (OpenAI-compatible
    # /v1/audio/speech endpoint) -- the same engine speak-mcp uses. Start it
    # with `mlx_audio.server --host 127.0.0.1 --port 8000`.
    base_url = cfg.get("base_url", "http://127.0.0.1:8000").rstrip("/")
    model = cfg.get("model", "mlx-community/Kokoro-82M-bf16")
    voice = cfg.get("voice", "af_heart")
    speed = float(cfg.get("speed", 1.0))
    # Kokoro's lang_code is the voice-id prefix (a/b = English, e = Spanish, ...).
    lang_code = cfg.get("lang_code") or (voice[:1] if voice else "a")
    payload = json.dumps({
        "model": model,
        "input": text,
        "voice": voice,
        "lang_code": lang_code,
        "speed": speed,
        "response_format": "wav",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/v1/audio/speech", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        wav.write_bytes(resp.read())


_BACKENDS = {"kokoro": _kokoro, "say": _say, "pyttsx3": _pyttsx3}


def synthesize_clip(text: str, out_path, cfg: dict) -> None:
    """Synthesize a single short clip (e.g. a voice preview) to `out_path`."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    _BACKENDS[cfg.get("backend", "say")](text, out, cfg)


def synthesize_scenes(scenes, out_dir: str, cfg: dict, progress=None) -> None:
    backend = cfg.get("backend", "say")
    fn = _BACKENDS[backend]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    total = len(scenes)
    for i, s in enumerate(scenes):
        wav = out / f"scene_{s.index:03d}.wav"
        text = s.narration.strip() or s.title
        fn(text, wav, cfg)
        s.audio_path = str(wav)
        s.duration = _ffprobe_duration(str(wav))
        if progress:
            progress(i + 1, total)


# Curated Kokoro voices (the mlx-audio server doesn't expose a voice-list
# endpoint, so we ship a known-good catalogue). The id prefix encodes language +
# gender: a_/b_ = American/British English, e_ = Spanish (Español); the first
# letter is also the Kokoro `lang_code`.
KOKORO_VOICES = [
    {"id": "af_heart", "name": "Heart", "accent": "American", "gender": "Female", "language": "en"},
    {"id": "af_bella", "name": "Bella", "accent": "American", "gender": "Female", "language": "en"},
    {"id": "af_nicole", "name": "Nicole", "accent": "American", "gender": "Female", "language": "en"},
    {"id": "af_sarah", "name": "Sarah", "accent": "American", "gender": "Female", "language": "en"},
    {"id": "af_sky", "name": "Sky", "accent": "American", "gender": "Female", "language": "en"},
    {"id": "am_adam", "name": "Adam", "accent": "American", "gender": "Male", "language": "en"},
    {"id": "am_michael", "name": "Michael", "accent": "American", "gender": "Male", "language": "en"},
    {"id": "am_echo", "name": "Echo", "accent": "American", "gender": "Male", "language": "en"},
    {"id": "bf_emma", "name": "Emma", "accent": "British", "gender": "Female", "language": "en"},
    {"id": "bf_isabella", "name": "Isabella", "accent": "British", "gender": "Female", "language": "en"},
    {"id": "bm_george", "name": "George", "accent": "British", "gender": "Male", "language": "en"},
    {"id": "bm_lewis", "name": "Lewis", "accent": "British", "gender": "Male", "language": "en"},
    {"id": "ef_dora", "name": "Dora", "accent": "Spanish", "gender": "Female", "language": "es"},
    {"id": "em_alex", "name": "Alex", "accent": "Spanish", "gender": "Male", "language": "es"},
    {"id": "em_santa", "name": "Santa", "accent": "Spanish", "gender": "Male", "language": "es"},
]


def list_voices(cfg: dict, language: str | None = None) -> list[dict]:
    """Voices selectable for the configured backend, optionally filtered by language.

    For Kokoro we probe the server's catalogue endpoint and fall back to the
    curated list above. Other backends return [] (their voice is set in config).
    """
    backend = cfg.get("backend", "say")
    if backend != "kokoro":
        return []
    base_url = cfg.get("base_url", "http://127.0.0.1:8000").rstrip("/")
    voices = None
    try:
        with urllib.request.urlopen(f"{base_url}/v1/audio/voices", timeout=4) as r:
            data = json.loads(r.read())
        ids = data.get("voices") or data.get("data") or []
        if ids and isinstance(ids[0], str):
            voices = [{"id": v, "name": v, "accent": "", "gender": "", "language": ""}
                      for v in ids]
    except Exception:
        pass
    if voices is None:
        voices = KOKORO_VOICES
    if language:
        # Filter to the language; if the probed list lacked language tags, keep all.
        filtered = [v for v in voices if v.get("language") == language]
        return filtered or voices
    return voices
