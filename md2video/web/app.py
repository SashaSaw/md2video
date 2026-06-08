"""FastAPI backend for the md2video studio.

Wraps the shared `pipeline.build_video()` in an HTTP API with a filesystem-backed
library. Builds run on a single-worker queue (Playwright + ffmpeg are heavy, so
we serialize them) and report live progress that the SPA polls via /api/jobs.

Run:  md2video-web        (or: uvicorn md2video.web.app:app --reload)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import render, tts
from ..i18n import LANGUAGES, get_language
from ..pipeline import load_config, render_storyboard
from ..storyboard import Storyboard, build_storyboard
from ..storyboard import _validate_slide

# --------------------------------------------------------------------------- #
# Paths & config
# --------------------------------------------------------------------------- #
STATIC_DIR = Path(__file__).parent / "static"
LIBRARY = Path(os.environ.get("MD2VIDEO_LIBRARY", "library")).resolve()
LIBRARY.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = os.environ.get("MD2VIDEO_CONFIG", "config.yaml")
_CFG = load_config(CONFIG_PATH)
# The web UI drives voice selection, so it always uses the Kokoro backend.
_CFG.setdefault("tts", {})
_CFG["tts"]["backend"] = "kokoro"

# Short, cached voice-preview clips so users can hear a voice before building.
SAMPLES_DIR = LIBRARY / "_voice_samples"
SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
_SAMPLE_TEXT = {
    "en": "Hi — this is how I sound narrating your explainer video.",
    "es": "Hola, así sueno yo narrando tu vídeo explicativo.",
}
_VOICE_LANG = {v["id"]: v.get("language", "en") for v in tts.KOKORO_VOICES}

# --------------------------------------------------------------------------- #
# Job state (in-memory mirror of meta.json status, for fast polling)
# --------------------------------------------------------------------------- #
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=1)  # serialize heavy builds

app = FastAPI(title="md2video studio")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "video"


def _new_id(title: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{_slug(title)}"


def _dir(video_id: str) -> Path:
    return LIBRARY / video_id


def _read_meta(video_id: str) -> dict | None:
    f = _dir(video_id) / "meta.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _write_meta(meta: dict) -> None:
    d = _dir(meta["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta, indent=2))


def _all_meta() -> list[dict]:
    out = []
    for d in LIBRARY.iterdir():
        if d.is_dir():
            m = _read_meta(d.name)
            if m:
                out.append(m)
    out.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return out


def _extract_poster(video: Path, poster: Path) -> None:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.5", "-i", str(video),
             "-vframes", "1", "-q:v", "3", str(poster)],
            check=True)
    except Exception:
        pass  # poster is best-effort


# --------------------------------------------------------------------------- #
# Storyboard + config helpers
# --------------------------------------------------------------------------- #
def _sb_path(video_id: str) -> Path:
    return _dir(video_id) / "storyboard.json"


def _read_storyboard(video_id: str):
    p = _sb_path(video_id)
    if not p.exists():
        return None
    return Storyboard.from_dict(json.loads(p.read_text()))


def _write_storyboard(video_id: str, sb) -> None:
    _sb_path(video_id).write_text(
        json.dumps(sb.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def _cfg_for(meta: dict) -> dict:
    cfg = {k: dict(v) if isinstance(v, dict) else v for k, v in _CFG.items()}
    cfg["tts"] = dict(cfg.get("tts", {}))
    cfg["tts"]["backend"] = "kokoro"
    cfg["tts"]["voice"] = meta.get("voice", "af_heart")
    return cfg


def _meta_updater(video_id: str, meta: dict):
    last = [0.0]

    def upd(stage, pct):
        meta["stage"] = stage
        meta["progress"] = int(pct)
        _set_job(video_id, meta)
        now = time.monotonic()
        if now - last[0] > 1.0:               # throttle disk writes
            _write_meta(meta)
            last[0] = now
    return upd


# --------------------------------------------------------------------------- #
# Phase 1 (distill) and Phase 2 (render) workers
# --------------------------------------------------------------------------- #
def _run_distill(video_id: str) -> None:
    meta = _read_meta(video_id)
    if not meta:
        return
    d = _dir(video_id)
    meta.update(status="distilling", stage="narrate", progress=0, error=None)
    _write_meta(meta)
    _set_job(video_id, meta)
    upd = _meta_updater(video_id, meta)
    try:
        sb = build_storyboard(
            (d / "source.md").read_text(encoding="utf-8"),
            cfg=_cfg_for(meta), language=meta.get("language", "en"),
            title=meta.get("title", ""), source_filename=meta.get("source_filename"),
            progress=lambda done, total: upd("narrate", (done / total * 100) if total else 100))
        _write_storyboard(video_id, sb)
        meta.update(status="storyboard_ready", stage="storyboard", progress=100,
                    error=None, rev=sb.rev, style=sb.style, slide_count=len(sb.slides))
    except Exception as e:  # noqa: BLE001
        meta.update(status="error", error=str(e))
    finally:
        _write_meta(meta)
        _set_job(video_id, meta)


def _run_render(video_id: str) -> None:
    meta = _read_meta(video_id)
    if not meta:
        return
    d = _dir(video_id)
    sb = _read_storyboard(video_id)
    if sb is None:
        meta.update(status="error", error="no storyboard to render")
        _write_meta(meta)
        _set_job(video_id, meta)
        return
    meta.update(status="processing", stage="render", progress=0, error=None)
    _write_meta(meta)
    _set_job(video_id, meta)
    try:
        beats = render_storyboard(sb, str(d / "video.mp4"), cfg=_cfg_for(meta),
                                  work_dir=str(d / "build"),
                                  progress=_meta_updater(video_id, meta))
        _extract_poster(d / "video.mp4", d / "poster.jpg")
        meta.update(status="ready", stage="done", progress=100, error=None,
                    duration=round(sum(b.duration for b in beats), 2),
                    slide_count=len({b.slide_id for b in beats}))
    except Exception as e:  # noqa: BLE001
        meta.update(status="error", error=str(e))
    finally:
        _write_meta(meta)
        _set_job(video_id, meta)


def _set_job(video_id: str, meta: dict) -> None:
    with _JOBS_LOCK:
        _JOBS[video_id] = {
            "id": video_id,
            "status": meta.get("status"),
            "stage": meta.get("stage"),
            "progress": meta.get("progress", 0),
            "error": meta.get("error"),
        }


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    kokoro = False
    base = _CFG.get("tts", {}).get("base_url", "http://127.0.0.1:8000")
    try:
        import urllib.request
        with urllib.request.urlopen(base.rstrip("/") + "/v1/models", timeout=3):
            kokoro = True
    except Exception:
        kokoro = False
    return {
        "kokoro": kokoro,
        "kokoro_url": base,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }


@app.get("/api/languages")
def languages():
    return {"languages": [
        {"code": l.code, "name": l.name, "native": l.native_name,
         "is_source": l.is_source}
        for l in LANGUAGES.values()
    ]}


@app.get("/api/voices")
def voices(language: str | None = None):
    return {"voices": tts.list_voices(_CFG.get("tts", {}), language)}


def _ensure_sample(voice_id: str) -> Path:
    """Synthesize (and cache) a short preview clip for a voice; return its path."""
    path = SAMPLES_DIR / f"{voice_id}.wav"
    if not path.exists():
        cfg = dict(_CFG.get("tts", {}))
        cfg["backend"] = "kokoro"
        cfg["voice"] = voice_id
        lang = _VOICE_LANG.get(voice_id, "en")
        tts.synthesize_clip(_SAMPLE_TEXT.get(lang, _SAMPLE_TEXT["en"]), path, cfg)
    return path


@app.get("/api/voices/{voice_id}/sample")
def voice_sample(voice_id: str):
    if voice_id not in _VOICE_LANG:
        raise HTTPException(404, "unknown voice")
    try:
        path = _ensure_sample(voice_id)
    except Exception as e:  # noqa: BLE001 — surface TTS errors to the UI
        raise HTTPException(503, f"could not synthesize sample: {e}")
    return FileResponse(path, media_type="audio/wav")


def _prewarm_samples() -> None:
    """Best-effort: generate any missing voice samples in the background."""
    for vid in list(_VOICE_LANG):
        try:
            _ensure_sample(vid)
        except Exception:
            pass  # Kokoro may be down at startup; samples render lazily on demand


@app.get("/api/videos")
def list_videos():
    return _all_meta()


@app.get("/api/videos/{video_id}")
def get_video(video_id: str):
    meta = _read_meta(video_id)
    if not meta:
        raise HTTPException(404, "not found")
    return meta


@app.post("/api/videos")
async def create_video(
    file: UploadFile = File(...),
    title: str = Form(""),
    voice: str = Form("af_heart"),
    language: str = Form("en"),
):
    raw = (await file.read()).decode("utf-8", errors="replace")
    if not raw.strip():
        raise HTTPException(400, "markdown file is empty")

    lang = get_language(language)
    title = title.strip() or Path(file.filename or "untitled").stem
    video_id = _new_id(title)
    d = _dir(video_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.md").write_text(raw, encoding="utf-8")

    meta = {
        "id": video_id,
        "title": title,
        "source_filename": file.filename,
        "voice": voice,
        "language": lang.code,
        "language_name": lang.native_name,
        "status": "queued",
        "stage": None,
        "progress": 0,
        "error": None,
        "created_at": _now_iso(),
        "duration": None,
        "slide_count": None,
        "rev": 0,
        "style": _CFG.get("style", {"preset": "dark_keynote", "theme": "dark"}),
    }
    _write_meta(meta)
    _set_job(video_id, meta)
    _EXECUTOR.submit(_run_distill, video_id)        # Phase 1
    return JSONResponse(meta, status_code=201)


@app.get("/api/videos/{video_id}/storyboard")
def get_storyboard(video_id: str):
    sb = _read_storyboard(video_id)
    if sb is None:
        raise HTTPException(404, "no storyboard yet")
    return sb.to_dict()


@app.put("/api/videos/{video_id}/storyboard")
async def put_storyboard(video_id: str, request: Request):
    if not _dir(video_id).exists():
        raise HTTPException(404, "not found")
    sb = Storyboard.from_dict(await request.json())
    sb.slides = [_validate_slide(s) for s in sb.slides]   # re-enforce budgets after edits
    sb.rev = (sb.rev or 0) + 1
    _write_storyboard(video_id, sb)
    meta = _read_meta(video_id) or {}
    meta.update(rev=sb.rev, style=sb.style, slide_count=len(sb.slides))
    _write_meta(meta)
    return {"rev": sb.rev, "slides": len(sb.slides)}


@app.get("/api/videos/{video_id}/preview")
def preview(video_id: str, slide: int = 0):
    sb = _read_storyboard(video_id)
    if sb is None or slide < 0 or slide >= len(sb.slides):
        raise HTTPException(404, "no such slide")
    s = sb.slides[slide]
    preset = sb.style.get("preset", "dark_keynote")
    theme = sb.style.get("theme", "dark")
    pdir = _dir(video_id) / "previews"
    pdir.mkdir(exist_ok=True)
    path = pdir / f"{sb.rev}_{s.id}_{preset}_{theme}_v{render.RENDERER_VERSION}.png"
    if not path.exists():
        render.render_slide_preview(s, sb.style, str(path))
    return FileResponse(path)


@app.post("/api/videos/{video_id}/generate")
def generate(video_id: str):
    meta = _read_meta(video_id)
    if not meta:
        raise HTTPException(404, "not found")
    if _read_storyboard(video_id) is None:
        raise HTTPException(400, "no storyboard to render")
    meta.update(status="queued", stage=None, progress=0, error=None)
    _write_meta(meta)
    _set_job(video_id, meta)
    _EXECUTOR.submit(_run_render, video_id)         # Phase 2
    return JSONResponse(meta, status_code=202)


@app.get("/api/jobs/{video_id}")
def job_status(video_id: str):
    with _JOBS_LOCK:
        if video_id in _JOBS:
            return _JOBS[video_id]
    meta = _read_meta(video_id)
    if not meta:
        raise HTTPException(404, "not found")
    return {"id": video_id, "status": meta.get("status"),
            "stage": meta.get("stage"), "progress": meta.get("progress", 0),
            "error": meta.get("error")}


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str):
    d = _dir(video_id)
    if not d.exists():
        raise HTTPException(404, "not found")
    shutil.rmtree(d, ignore_errors=True)
    with _JOBS_LOCK:
        _JOBS.pop(video_id, None)
    return {"deleted": video_id}


def _media(video_id: str, name: str, *, download: bool = False) -> FileResponse:
    path = _dir(video_id) / name
    if not path.exists():
        raise HTTPException(404, "not found")
    headers = None
    if download:
        meta = _read_meta(video_id) or {}
        fname = _slug(meta.get("title", video_id)) + path.suffix
        headers = {"Content-Disposition": f'attachment; filename="{fname}"'}
    return FileResponse(path, headers=headers)


@app.get("/media/{video_id}/video.mp4")
def media_video(video_id: str):
    return _media(video_id, "video.mp4")


@app.get("/media/{video_id}/poster.jpg")
def media_poster(video_id: str):
    return _media(video_id, "poster.jpg")


@app.get("/media/{video_id}/download")
def media_download(video_id: str):
    return _media(video_id, "video.mp4", download=True)


# Static SPA (mounted last so /api/* take precedence).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def run() -> None:
    """Console-script entrypoint: `md2video-web`."""
    import uvicorn
    host = os.environ.get("MD2VIDEO_HOST", "127.0.0.1")
    port = int(os.environ.get("MD2VIDEO_PORT", "8001"))
    print(f"md2video studio → http://{host}:{port}")
    threading.Thread(target=_prewarm_samples, daemon=True).start()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
