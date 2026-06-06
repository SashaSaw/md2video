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

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import tts
from ..pipeline import build_video, load_config

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
# Build worker
# --------------------------------------------------------------------------- #
def _run_build(video_id: str) -> None:
    meta = _read_meta(video_id)
    if not meta:
        return
    d = _dir(video_id)
    source = d / "source.md"
    out = d / "video.mp4"

    meta.update(status="processing", stage="narrate", progress=0, error=None)
    _write_meta(meta)
    _set_job(video_id, meta)

    last_save = [0.0]

    def progress(stage, pct):
        meta["stage"] = stage
        meta["progress"] = pct
        _set_job(video_id, meta)
        now = time.monotonic()
        if now - last_save[0] > 1.0:          # throttle disk writes
            _write_meta(meta)
            last_save[0] = now

    cfg = {k: dict(v) if isinstance(v, dict) else v for k, v in _CFG.items()}
    cfg["tts"] = dict(cfg.get("tts", {}))
    cfg["tts"]["backend"] = "kokoro"
    cfg["tts"]["voice"] = meta.get("voice", "af_heart")

    try:
        scenes = build_video(source.read_text(encoding="utf-8"), str(out),
                             cfg=cfg, work_dir=str(d / "build"),
                             progress=progress)
        _extract_poster(out, d / "poster.jpg")
        meta.update(
            status="ready", stage="done", progress=100, error=None,
            duration=round(sum(s.duration for s in scenes), 2),
            scene_count=len(scenes),
        )
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI
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


@app.get("/api/voices")
def voices():
    return {"voices": tts.list_voices(_CFG.get("tts", {}))}


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
):
    raw = (await file.read()).decode("utf-8", errors="replace")
    if not raw.strip():
        raise HTTPException(400, "markdown file is empty")

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
        "status": "queued",
        "stage": None,
        "progress": 0,
        "error": None,
        "created_at": _now_iso(),
        "duration": None,
        "scene_count": None,
    }
    _write_meta(meta)
    _set_job(video_id, meta)
    _EXECUTOR.submit(_run_build, video_id)
    return JSONResponse(meta, status_code=201)


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
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
