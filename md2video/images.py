"""Local AI image generation for image slides, via mflux (FLUX on MLX).

Optional — requires `pip install mflux` (Apple Silicon). The model is loaded once
and cached; FLUX.1-schnell (Apache-2.0) is the default. Generation is heavy
(~seconds on an M-series Mac) and the weights download on first use, so callers
should run this off the request thread and report status.
"""

from __future__ import annotations

import random
from pathlib import Path

# Default = Z-Image-Turbo: non-gated (no HF login), fast, Apple-Silicon friendly.
# FLUX.1-schnell ("schnell") also works but is gated on HuggingFace (needs auth).
DEFAULT_MODEL = "z-image-turbo"
DEFAULT_STEPS = 8
DEFAULT_QUANTIZE = 4
W, H = 1280, 720          # 16:9, multiples of 16; rendered "cover" to 1080p

# Keep the user's prompt; append a style cue so generated images suit the deck.
# "no text" matters — the slide overlays its own headline.
_STYLE_AUG = {
    "dark_keynote": "cinematic, high contrast, dramatic lighting, sleek dark backdrop, no text",
    "editorial_light": "warm natural light, editorial magazine photography, airy, no text",
    "minimal_statement": "clean minimal composition, lots of negative space, single subject, no text",
}

_CACHE: dict = {}


class ImageUnavailable(RuntimeError):
    """Raised when image generation can't run (mflux missing or disabled)."""


def augment_prompt(prompt: str, style: dict | None) -> str:
    aug = _STYLE_AUG.get((style or {}).get("preset", ""), "")
    prompt = (prompt or "").strip()
    return f"{prompt}. {aug}" if aug else prompt


def _is_zimage(model: str) -> bool:
    return model.lower().replace("_", "-").startswith("z-image")


def _load(model: str, quantize: int):
    """Return (kind, model_obj). kind is 'zimage' or 'flux' (different generate APIs)."""
    key = (model, quantize)
    if key not in _CACHE:
        try:
            from mflux.models.common.config import ModelConfig
        except ImportError as e:
            raise ImageUnavailable("mflux is not installed. Run: pip install mflux") from e
        if _is_zimage(model):
            from mflux.models.z_image.variants.z_image import ZImage
            try:
                mc = ModelConfig.from_name(model_name=model)
            except Exception:
                mc = ModelConfig.z_image_turbo()
            _CACHE[key] = ("zimage", ZImage(model_config=mc, quantize=quantize))
        else:
            from mflux.models.flux.variants.txt2img.flux import Flux1
            try:
                mc = ModelConfig.from_name(model_name=model)
            except Exception:
                mc = ModelConfig.schnell()
            _CACHE[key] = ("flux", Flux1(model_config=mc, quantize=quantize))
    return _CACHE[key]


def generate_image(prompt: str, out_path: str, cfg: dict | None = None, *,
                   seed: int | None = None, steps: int | None = None,
                   style: dict | None = None, negative_prompt: str = "") -> dict:
    """Generate an image to `out_path`. Returns provenance (prompt/seed/model/steps)."""
    cfg = cfg or {}
    if cfg.get("backend", "mflux") == "none":
        raise ImageUnavailable("Image generation is disabled (image.backend = none).")
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("empty image prompt")

    model = cfg.get("model", DEFAULT_MODEL)
    quantize = int(cfg.get("quantize", DEFAULT_QUANTIZE))
    steps = int(steps or cfg.get("steps", DEFAULT_STEPS))
    if seed is None:
        seed = random.randint(0, 2**31 - 1)

    full = augment_prompt(prompt, style)
    kind, model_obj = _load(model, quantize)
    if kind == "zimage":
        image = model_obj.generate_image(seed=int(seed), prompt=full,
                                         num_inference_steps=steps, height=H, width=W)
    else:
        from mflux.models.common.config.config import Config
        image = model_obj.generate_image(seed=int(seed), prompt=full,
                                         config=Config(num_inference_steps=steps, height=H, width=W))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    image.save(path=str(out_path))
    return {"prompt": prompt, "negative_prompt": negative_prompt,
            "seed": int(seed), "model": model, "steps": steps}
