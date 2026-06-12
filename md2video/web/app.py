"""FastAPI backend for the md2video studio.

Wraps the shared `pipeline.build_video()` in an HTTP API with a filesystem-backed
library. Builds run on a single-worker queue (Playwright + ffmpeg are heavy, so
we serialize them) and report live progress that the SPA polls via /api/jobs.

Run:  md2video-web        (or: uvicorn md2video.web.app:app --reload)
"""

from __future__ import annotations

import hashlib
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

from dataclasses import asdict

from .. import gifs, images, render, tts
from ..i18n import LANGUAGES, get_language
from ..ids import new_id
from ..pipeline import load_config, render_storyboard
from ..storyboard import Storyboard, build_storyboard, revise_slide
from ..storyboard import apply_ops, propose_gags, propose_ops, retone_slide
from ..storyboard import _default_slide, _validate_slide

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
_IMG_JOBS: dict[str, dict] = {}                # per-slide image-generation status
_GIF_JOBS: dict[str, dict] = {}                # per-slide gif-fetch status
_FUNNIER_JOBS: dict[str, dict] = {}            # per-video "make it funnier" status
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
    data = json.dumps(sb.to_dict(), indent=2, ensure_ascii=False)
    _sb_path(video_id).write_text(data, encoding="utf-8")
    # Snapshot this revision for undo/restore; keep the last ~20.
    snaps = _dir(video_id) / "snapshots"
    snaps.mkdir(exist_ok=True)
    (snaps / f"{sb.rev}.json").write_text(data, encoding="utf-8")
    old = sorted((p for p in snaps.glob("*.json") if p.stem.isdigit()),
                 key=lambda p: int(p.stem))
    for p in old[:-20]:
        p.unlink(missing_ok=True)


def _log_action(video_id: str, action: str, detail: str = "") -> None:
    """Typed audit trail: manual_edit|render|tts|llm_revise|llm_retone|llm_ask|image_generate."""
    print(f"[action] {action} {video_id} {detail}".rstrip())


def _slide_hash(slide_dict: dict, style: dict) -> str:
    blob = json.dumps([slide_dict, style], sort_keys=True, ensure_ascii=False)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:10]


# --- image assets -------------------------------------------------------- #
def _resolve_images(sb, video_id: str) -> None:
    """In-memory: make image/gif-slide paths absolute so the renderer can read them."""
    for s in sb.slides:
        if s.kind == "image" and s.image and s.image.get("path"):
            p = s.image["path"]
            if not os.path.isabs(p):
                s.image["path"] = str(_dir(video_id) / p)
        if s.kind == "gif" and s.gif and s.gif.get("path"):
            p = s.gif["path"]
            if not os.path.isabs(p):
                s.gif["path"] = str(_dir(video_id) / p)


def _gc_images(video_id: str, sb) -> None:
    """Delete generated images no longer referenced by any slide (conservative)."""
    imgdir = _dir(video_id) / "images"
    if not imgdir.exists():
        return
    keep = {os.path.basename(s.image["path"]) for s in sb.slides
            if s.kind == "image" and s.image and s.image.get("path")}
    for f in imgdir.glob("*.png"):
        if f.name not in keep:
            f.unlink(missing_ok=True)


def _gc_gifs(video_id: str, sb) -> None:
    """Delete fetched gifs no longer referenced by any slide (conservative)."""
    gifdir = _dir(video_id) / "gifs"
    if not gifdir.exists():
        return
    keep = {os.path.basename(s.gif["path"]) for s in sb.slides
            if s.kind == "gif" and s.gif and s.gif.get("path")}
    for f in gifdir.glob("*.gif"):
        if f.name not in keep:
            f.unlink(missing_ok=True)


def _run_image(video_id: str, slide_id: str, prompt: str, negative: str, seed) -> None:
    key = f"{video_id}:{slide_id}"
    sb = _read_storyboard(video_id)
    meta = _read_meta(video_id) or {}
    if sb is None:
        _IMG_JOBS[key] = {"status": "error", "error": "no storyboard"}
        return
    slide = next((s for s in sb.slides if s.id == slide_id), None)
    if slide is None:
        _IMG_JOBS[key] = {"status": "error", "error": "no such slide"}
        return
    asset = f"images/{new_id('img')}.png"
    out = _dir(video_id) / asset
    try:
        prov = images.generate_image(prompt, str(out), cfg=_CFG.get("image", {}),
                                     seed=seed, negative_prompt=negative, style=sb.style)
        slide.kind = "image"
        slide.image = {**prov, "path": asset}          # store RELATIVE path
        slide.narration_full = slide.narration_full or slide.headline or prompt[:80]
        sb.rev = (sb.rev or 0) + 1
        _write_storyboard(video_id, sb)
        _gc_images(video_id, sb)
        meta["rev"] = sb.rev
        _write_meta(meta)
        _log_action(video_id, "image_generate", slide_id)
        _IMG_JOBS[key] = {"status": "ready", "rev": sb.rev}
    except images.ImageUnavailable as e:
        _IMG_JOBS[key] = {"status": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        _IMG_JOBS[key] = {"status": "error", "error": str(e)}


def _fetch_gif_for_slide(slide, video_id: str, query: str, index: int) -> None:
    """Fetch a gif for `query` and store it (relative path) on `slide`. Raises on failure."""
    asset = f"gifs/{new_id('gif')}.gif"
    out = _dir(video_id) / asset
    prov = gifs.fetch_gif(query, str(out), cfg=_CFG.get("gif", {}), index=index)
    slide.kind = "gif"
    slide.gif = {**prov, "path": asset}              # store RELATIVE path
    slide.narration_full = slide.narration_full or slide.headline or query[:80]


def _run_gif(video_id: str, slide_id: str, query: str, index: int) -> None:
    key = f"{video_id}:{slide_id}"
    sb = _read_storyboard(video_id)
    meta = _read_meta(video_id) or {}
    if sb is None:
        _GIF_JOBS[key] = {"status": "error", "error": "no storyboard"}
        return
    slide = next((s for s in sb.slides if s.id == slide_id), None)
    if slide is None:
        _GIF_JOBS[key] = {"status": "error", "error": "no such slide"}
        return
    try:
        _fetch_gif_for_slide(slide, video_id, query, index)
        sb.rev = (sb.rev or 0) + 1
        _write_storyboard(video_id, sb)
        _gc_gifs(video_id, sb)
        meta["rev"] = sb.rev
        _write_meta(meta)
        _log_action(video_id, "gif_fetch", slide_id)
        _GIF_JOBS[key] = {"status": "ready", "rev": sb.rev}
    except gifs.GifUnavailable as e:
        _GIF_JOBS[key] = {"status": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        _GIF_JOBS[key] = {"status": "error", "error": str(e)}


def _run_funnier(video_id: str) -> None:
    """LLM picks where funny gifs fit, we insert gif slides and fetch each one."""
    sb = _read_storyboard(video_id)
    meta = _read_meta(video_id) or {}
    if sb is None:
        _FUNNIER_JOBS[video_id] = {"status": "error", "error": "no storyboard"}
        return
    try:
        gags = propose_gags(sb, _cfg_for(meta), meta.get("language", "en"))
        if not gags:
            _FUNNIER_JOBS[video_id] = {"status": "ready", "added": 0,
                                       "note": "No good gif moments found."}
            return
        added = 0
        for g in gags:
            slide = _default_slide("gif", g.get("caption", ""))
            slide.gif["query"] = g["query"]
            ids = [s.id for s in sb.slides]
            try:
                idx = ids.index(g["after"]) + 1
            except ValueError:
                idx = len(sb.slides)
            sb.slides.insert(idx, slide)
            try:
                _fetch_gif_for_slide(slide, video_id, g["query"], 0)
                added += 1
                _FUNNIER_JOBS[video_id] = {"status": "fetching", "added": added,
                                           "total": len(gags)}
            except Exception:  # noqa: BLE001 — keep the slide; it shows a placeholder
                continue
        sb.rev = (sb.rev or 0) + 1
        _write_storyboard(video_id, sb)
        _gc_gifs(video_id, sb)
        meta.update(rev=sb.rev, slide_count=len(sb.slides))
        _write_meta(meta)
        _log_action(video_id, "gif_funnier", f"added {added}")
        _FUNNIER_JOBS[video_id] = {"status": "ready", "added": added, "rev": sb.rev}
    except Exception as e:  # noqa: BLE001
        _FUNNIER_JOBS[video_id] = {"status": "error", "error": str(e)}


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
            style_prompt=meta.get("style_prompt", ""),
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
    _resolve_images(sb, video_id)                 # absolute paths for the renderer
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
    style_prompt: str = Form(""),
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
        "style_prompt": style_prompt.strip()[:800],
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
    _gc_images(video_id, sb)                       # drop images no longer referenced
    meta = _read_meta(video_id) or {}
    meta.update(rev=sb.rev, style=sb.style, slide_count=len(sb.slides))
    _write_meta(meta)
    _log_action(video_id, "manual_edit", f"rev {sb.rev}")
    return {"rev": sb.rev, "slides": len(sb.slides)}


@app.post("/api/videos/{video_id}/slides/{slide_id}/revise")
async def revise(video_id: str, slide_id: str, request: Request):
    sb = _read_storyboard(video_id)
    if sb is None:
        raise HTTPException(404, "no storyboard")
    idx = next((i for i, s in enumerate(sb.slides) if s.id == slide_id), None)
    if idx is None:
        raise HTTPException(404, "no such slide")
    prompt = (await request.json()).get("prompt", "").strip()
    if not prompt:
        raise HTTPException(400, "empty prompt")
    meta = _read_meta(video_id) or {}
    try:
        sb.slides[idx] = revise_slide(sb.slides[idx], prompt, cfg=_cfg_for(meta),
                                      language=meta.get("language", "en"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"could not apply edit: {e}")
    sb.rev = (sb.rev or 0) + 1
    _write_storyboard(video_id, sb)
    meta["rev"] = sb.rev
    _write_meta(meta)
    _log_action(video_id, "llm_revise", slide_id)
    return {"slide": asdict(sb.slides[idx]), "rev": sb.rev}


@app.post("/api/videos/{video_id}/retone")
async def retone(video_id: str, request: Request):
    sb = _read_storyboard(video_id)
    if sb is None:
        raise HTTPException(404, "no storyboard")
    body = await request.json()
    tone = (body.get("tone") or "").strip()
    if not tone:
        raise HTTPException(400, "empty tone")
    meta = _read_meta(video_id) or {}
    cfg, lang = _cfg_for(meta), meta.get("language", "en")
    scope = body.get("scope")  # None/"all" or list of ids
    try:
        for k, s in enumerate(sb.slides):
            if not scope or scope == "all" or s.id in scope:
                sb.slides[k] = retone_slide(s, tone, cfg, lang)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"retone failed: {e}")
    sb.rev = (sb.rev or 0) + 1
    sb.meta["tone"] = tone
    _write_storyboard(video_id, sb)
    meta["rev"] = sb.rev
    _write_meta(meta)
    _log_action(video_id, "llm_retone", tone)
    return {"rev": sb.rev, "storyboard": sb.to_dict()}


@app.post("/api/videos/{video_id}/ask")
async def ask(video_id: str, request: Request):
    sb = _read_storyboard(video_id)
    if sb is None:
        raise HTTPException(404, "no storyboard")
    prompt = (await request.json()).get("prompt", "").strip()
    if not prompt:
        raise HTTPException(400, "empty prompt")
    meta = _read_meta(video_id) or {}
    _log_action(video_id, "llm_ask", "propose")
    return propose_ops(sb, prompt, _cfg_for(meta), meta.get("language", "en"))


@app.post("/api/videos/{video_id}/ask/apply")
async def ask_apply(video_id: str, request: Request):
    sb = _read_storyboard(video_id)
    if sb is None:
        raise HTTPException(404, "no storyboard")
    ops = (await request.json()).get("ops", [])
    meta = _read_meta(video_id) or {}
    try:
        sb, applied = apply_ops(sb, ops, _cfg_for(meta), meta.get("language", "en"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"apply failed: {e}")
    sb.rev = (sb.rev or 0) + 1
    _write_storyboard(video_id, sb)
    meta.update(rev=sb.rev, style=sb.style, slide_count=len(sb.slides))
    _write_meta(meta)
    _log_action(video_id, "llm_ask", f"applied {','.join(applied)}")
    return {"rev": sb.rev, "applied": applied, "storyboard": sb.to_dict()}


@app.post("/api/videos/{video_id}/slides/{slide_id}/image")
async def gen_image(video_id: str, slide_id: str, request: Request):
    sb = _read_storyboard(video_id)
    if sb is None or not any(s.id == slide_id for s in sb.slides):
        raise HTTPException(404, "no such slide")
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "empty prompt")
    key = f"{video_id}:{slide_id}"
    _IMG_JOBS[key] = {"status": "generating"}
    _EXECUTOR.submit(_run_image, video_id, slide_id, prompt,
                     body.get("negative_prompt", ""), body.get("seed"))
    return JSONResponse({"status": "generating"}, status_code=202)


@app.get("/api/videos/{video_id}/slides/{slide_id}/image/status")
def gen_image_status(video_id: str, slide_id: str):
    return _IMG_JOBS.get(f"{video_id}:{slide_id}", {"status": "idle"})


# --- gif assets ---------------------------------------------------------- #
@app.get("/api/gif/available")
def gif_available():
    """Whether the LLM can fetch gifs (provider set + API key present)."""
    return {"available": gifs.available(_CFG.get("gif", {}))}


@app.post("/api/videos/{video_id}/slides/{slide_id}/gif")
async def fetch_gif(video_id: str, slide_id: str, request: Request):
    """Fetch (or reroll) a gif for a slide. body: {query, index}."""
    sb = _read_storyboard(video_id)
    if sb is None or not any(s.id == slide_id for s in sb.slides):
        raise HTTPException(404, "no such slide")
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "empty query")
    index = int(body.get("index") or 0)
    key = f"{video_id}:{slide_id}"
    _GIF_JOBS[key] = {"status": "fetching"}
    _EXECUTOR.submit(_run_gif, video_id, slide_id, query, index)
    return JSONResponse({"status": "fetching"}, status_code=202)


@app.get("/api/videos/{video_id}/slides/{slide_id}/gif/status")
def fetch_gif_status(video_id: str, slide_id: str):
    return _GIF_JOBS.get(f"{video_id}:{slide_id}", {"status": "idle"})


@app.get("/api/videos/{video_id}/slides/{slide_id}/gif/file")
def gif_file(video_id: str, slide_id: str):
    """Serve the raw (animated) gif so the editor can show it playing."""
    sb = _read_storyboard(video_id)
    s = next((x for x in (sb.slides if sb else []) if x.id == slide_id), None)
    p = (s.gif or {}).get("path") if s and s.kind == "gif" else None
    if not p:
        raise HTTPException(404, "no gif")
    full = Path(p) if os.path.isabs(p) else _dir(video_id) / p
    if not full.exists():
        raise HTTPException(404, "no gif file")
    return FileResponse(full, media_type="image/gif")


@app.post("/api/videos/{video_id}/funnier")
def make_funnier(video_id: str):
    """Kick off the LLM 'make it funnier' pass (inserts gif slides)."""
    if _read_storyboard(video_id) is None:
        raise HTTPException(404, "no storyboard")
    if not gifs.available(_CFG.get("gif", {})):
        raise HTTPException(400, "GIFs unavailable — set a Giphy API key (GIPHY_API_KEY).")
    _FUNNIER_JOBS[video_id] = {"status": "thinking"}
    _EXECUTOR.submit(_run_funnier, video_id)
    return JSONResponse({"status": "thinking"}, status_code=202)


@app.get("/api/videos/{video_id}/funnier/status")
def make_funnier_status(video_id: str):
    return _FUNNIER_JOBS.get(video_id, {"status": "idle"})


@app.get("/api/videos/{video_id}/preview")
def preview(video_id: str, slide_id: str | None = None, slide: int | None = None):
    sb = _read_storyboard(video_id)
    if sb is None:
        raise HTTPException(404, "no storyboard")
    s = None
    if slide_id:
        s = next((x for x in sb.slides if x.id == slide_id), None)
    elif slide is not None and 0 <= slide < len(sb.slides):
        s = sb.slides[slide]
    if s is None:
        raise HTTPException(404, "no such slide")
    pdir = _dir(video_id) / "previews"
    pdir.mkdir(exist_ok=True)
    # Cache key = content hash + style + renderer version (not slide index).
    path = pdir / f"{s.id}_{_slide_hash(asdict(s), sb.style)}_v{render.RENDERER_VERSION}.png"
    if not path.exists():
        if s.kind in ("image", "gif"):
            _resolve_images(sb, video_id)        # make image/gif path absolute for render
        render.render_slide_preview(s, sb.style, str(path))
    return FileResponse(path)


@app.get("/api/videos/{video_id}/history")
def history(video_id: str):
    snaps = _dir(video_id) / "snapshots"
    revs = sorted(int(p.stem) for p in snaps.glob("*.json") if p.stem.isdigit())
    return {"revs": revs, "current": (_read_meta(video_id) or {}).get("rev")}


@app.post("/api/videos/{video_id}/restore")
async def restore(video_id: str, request: Request):
    rev = (await request.json()).get("rev")
    snap = _dir(video_id) / "snapshots" / f"{rev}.json"
    if not snap.exists():
        raise HTTPException(404, "no such snapshot")
    sb = Storyboard.from_dict(json.loads(snap.read_text()))
    sb.rev = ((_read_meta(video_id) or {}).get("rev") or sb.rev) + 1   # restore = new rev
    _write_storyboard(video_id, sb)
    meta = _read_meta(video_id) or {}
    meta.update(rev=sb.rev, style=sb.style, slide_count=len(sb.slides))
    _write_meta(meta)
    _log_action(video_id, "restore", f"from rev {rev}")
    return {"rev": sb.rev, "storyboard": sb.to_dict()}


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
