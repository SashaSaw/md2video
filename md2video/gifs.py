"""Fetch well-known GIFs/memes from Giphy for humorous slides.

Optional — needs a free Giphy API key in the GIPHY_API_KEY env var (or
`gif.api_key` in config.yaml). This powers the LLM "make it funnier" pass: the
model emits a short search query (e.g. "mind blown") and we resolve it to a
real, popular GIF. A popular search result ≈ a well-known meme, which is exactly
what we want. The provider is abstracted behind search_gifs()/download_gif() so
Tenor (or another source) can drop in later without touching callers.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_PROVIDER = "giphy"
DEFAULT_RATING = "pg-13"        # the model picks unattended; keep it tame
DEFAULT_LIMIT = 12
_GIPHY_SEARCH = "https://api.giphy.com/v1/gifs/search"
_TIMEOUT = 20


class GifUnavailable(RuntimeError):
    """Raised when GIF search can't run (no API key, disabled, or unknown provider)."""


def _api_key(cfg: dict) -> str:
    key = (cfg or {}).get("api_key") or os.environ.get("GIPHY_API_KEY", "")
    return (key or "").strip()


def available(cfg: dict | None = None) -> bool:
    """True if GIF features can run (enabled + a provider + an API key)."""
    cfg = cfg or {}
    if cfg.get("enabled") is False or cfg.get("provider", DEFAULT_PROVIDER) == "none":
        return False
    return bool(_api_key(cfg))


def _giphy_search(query: str, key: str, rating: str, limit: int) -> list[dict]:
    params = urllib.parse.urlencode({
        "api_key": key, "q": query, "limit": limit,
        "rating": rating, "bundle": "messaging_non_clips",
    })
    req = urllib.request.Request(f"{_GIPHY_SEARCH}?{params}",
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:   # noqa: S310 (trusted host)
        data = json.loads(r.read().decode("utf-8"))
    out = []
    for g in data.get("data", []):
        imgs = g.get("images", {}) or {}
        orig = imgs.get("original", {}) or {}
        still = imgs.get("original_still", {}) or {}
        url = orig.get("url")
        if not url:
            continue
        out.append({
            "provider": "giphy",
            "gif_id": g.get("id", ""),
            "url": url,
            "still_url": still.get("url", ""),
            "width": int(orig.get("width") or 0),
            "height": int(orig.get("height") or 0),
            "title": g.get("title", ""),
        })
    return out


def search_gifs(query: str, cfg: dict | None = None, *, rating: str | None = None,
                limit: int | None = None) -> list[dict]:
    """Return ranked GIF candidates for `query` (most relevant first)."""
    cfg = cfg or {}
    query = (query or "").strip()
    if not query:
        raise ValueError("empty gif query")
    provider = cfg.get("provider", DEFAULT_PROVIDER)
    if provider == "none" or cfg.get("enabled") is False:
        raise GifUnavailable("GIF search is disabled (gif.enabled = false).")
    key = _api_key(cfg)
    if not key:
        raise GifUnavailable(
            "No Giphy API key. Set GIPHY_API_KEY (or gif.api_key in config.yaml).")
    rating = rating or cfg.get("rating", DEFAULT_RATING)
    limit = int(limit or cfg.get("limit", DEFAULT_LIMIT))
    if provider == "giphy":
        return _giphy_search(query, key, rating, limit)
    raise GifUnavailable(f"Unknown gif provider: {provider}")


def download_gif(url: str, out_path: str) -> str:
    """Download a GIF (or still image) to `out_path`."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "md2video/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:   # noqa: S310 (trusted host)
        data = r.read()
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path


def fetch_gif(query: str, out_path: str, cfg: dict | None = None, *,
              index: int = 0, rating: str | None = None) -> dict:
    """Search for `query`, download the `index`-th candidate to `out_path`.

    Returns provenance (query, provider, url, dimensions, which result was used)
    so the caller can store it on the slide and offer a "reroll" to the next one.
    """
    results = search_gifs(query, cfg, rating=rating)
    if not results:
        raise GifUnavailable(f"No GIFs found for: {query!r}")
    idx = index % len(results)
    pick = results[idx]
    download_gif(pick["url"], out_path)
    return {
        "query": query,
        "provider": pick["provider"],
        "gif_id": pick["gif_id"],
        "url": pick["url"],
        "still_url": pick.get("still_url", ""),
        "width": pick.get("width", 0),
        "height": pick.get("height", 0),
        "result_index": idx,
        "result_count": len(results),
    }
