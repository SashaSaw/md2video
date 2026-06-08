"""Shared LLM helpers used by translation, slide distillation, etc.

Two backends, selected by `cfg["backend"]`:
  "mlx"       — a local model via mlx-lm (default, fully offline). `cfg["model"]`
                defaults to Qwen3.6-27B (MLX 4-bit); `cfg["max_tokens"]` optional.
  "anthropic" — the Claude API (needs ANTHROPIC_API_KEY).

`complete()` returns text; `complete_json()` returns a parsed object (with one
retry). The MLX model is loaded once per process and cached, so callers that do
many calls (e.g. one per scene) only pay the load cost once.
"""

from __future__ import annotations

import json
import os
import re

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

# Default local model (downloaded on first use). Override via cfg["model"].
# Picked for a 48GB Apple-Silicon machine: 27B dense @ 4-bit.
DEFAULT_MLX_MODEL = "unsloth/Qwen3.6-27B-UD-MLX-4bit"
DEFAULT_ANTHROPIC_MODEL = os.environ.get("MD2VIDEO_MODEL", "claude-opus-4-8")


class LLMUnavailable(RuntimeError):
    """Raised when the requested backend isn't usable (missing dep or API key)."""


def _strip_think(text: str) -> str:
    """Drop any <think>…</think> reasoning a local model might emit."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


# --------------------------------------------------------------------------- #
# Local MLX backend
# --------------------------------------------------------------------------- #
_MLX_CACHE: dict = {}  # model_id -> (model, tokenizer), loaded once per process


def _load_mlx(model_id: str):
    if model_id not in _MLX_CACHE:
        from mlx_lm import load
        _MLX_CACHE[model_id] = load(model_id)
    return _MLX_CACHE[model_id]


def _complete_mlx(system: str, user: str, cfg: dict, max_tokens: int) -> str:
    try:
        from mlx_lm import generate
    except ImportError as e:
        raise LLMUnavailable(
            "mlx-lm is not installed. Install it with `pip install mlx-lm` "
            "(Apple Silicon) to run the local model."
        ) from e

    model_id = cfg.get("model", DEFAULT_MLX_MODEL)
    model, tokenizer = _load_mlx(model_id)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    # Disable "thinking" so the model returns the answer directly.
    try:
        prompt = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False,
            enable_thinking=False)
    except TypeError:
        prompt = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False)

    gen_kwargs = {"max_tokens": max_tokens, "verbose": False}
    try:  # greedy decode for deterministic output (API varies by version)
        from mlx_lm.sample_utils import make_sampler
        gen_kwargs["sampler"] = make_sampler(temp=0.0)
    except Exception:
        pass

    out = generate(model, tokenizer, prompt=prompt, **gen_kwargs)
    return _strip_think(out)


# --------------------------------------------------------------------------- #
# Anthropic backend
# --------------------------------------------------------------------------- #
def _complete_anthropic(system: str, user: str, cfg: dict, max_tokens: int) -> str:
    if Anthropic is None or not os.environ.get("ANTHROPIC_API_KEY"):
        raise LLMUnavailable(
            "The Anthropic backend needs ANTHROPIC_API_KEY. Use the local "
            "'mlx' backend to stay offline."
        )
    client = Anthropic()
    msg = client.messages.create(
        model=cfg.get("model", DEFAULT_ANTHROPIC_MODEL), max_tokens=max_tokens,
        system=system, messages=[{"role": "user", "content": user}],
    )
    out = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return _strip_think(out)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def complete(cfg: dict | None, system: str, user: str, max_tokens: int = 1024) -> str:
    """Run one completion against the configured backend; returns plain text."""
    cfg = cfg or {}
    backend = cfg.get("backend", "mlx")
    if backend == "anthropic":
        return _complete_anthropic(system, user, cfg, max_tokens)
    return _complete_mlx(system, user, cfg, max_tokens)


def _extract_json(text: str):
    """Parse a JSON object out of a model response, tolerating fences/prose."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        return json.loads(t[start:end + 1])
    raise ValueError("no JSON object found in model output")


def complete_json(cfg: dict | None, system: str, user: str, max_tokens: int = 2048):
    """Like complete(), but parse and return a JSON object. One retry on failure.

    Raises ValueError if the model never returns parseable JSON (callers should
    fall back to a heuristic).
    """
    raw = complete(cfg, system, user, max_tokens)
    try:
        return _extract_json(raw)
    except Exception:
        strict = system + ("\n\nIMPORTANT: Return ONLY valid minified JSON — no "
                           "prose, no markdown, no code fences.")
        raw2 = complete(cfg, strict, user, max_tokens)
        return _extract_json(raw2)
