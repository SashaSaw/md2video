"""Translate a whole markdown document into another language.

We translate the *source markdown* up front, then run the normal pipeline, so
prose, tables, and Mermaid labels all come out in the target language and appear
on the slides, not just in the audio. Source-language docs are returned
untouched. Backend selection (local mlx / anthropic) lives in `llm.py`.
"""

from __future__ import annotations

from . import llm
from .i18n import get_language, is_source_language

_SYSTEM = (
    "You are a professional technical translator. Translate the user's Markdown "
    "document into {language}. Translate all human-readable text: prose, "
    "headings, list items, and table cells. Preserve the document structure "
    "EXACTLY — keep all Markdown syntax, link URLs, and inline/fenced code "
    "blocks intact.\n\n"
    "Inside ```mermaid``` blocks, keep ALL Mermaid keywords and syntax in "
    "English and completely unchanged — including the diagram type "
    "(sequenceDiagram, flowchart, etc.), the keywords participant, actor, as, "
    "note, loop, alt, opt, par, end, the arrows (->>, -->>, -->, etc.), and all "
    "node IDs. In particular NEVER translate the word 'as'. Translate ONLY the "
    "human-readable display text: the message after a colon, and text inside "
    "quotes or square brackets.\n\n"
    "Do not add notes, comments, or reasoning, and do not wrap your answer in a "
    "code fence. Return only the translated Markdown."
)


class TranslationUnavailable(RuntimeError):
    """Raised when a non-source language is requested but no translator is available."""


def translate_markdown(md_text: str, language: str, cfg: dict | None = None) -> str:
    """Return `md_text` translated into `language` (passthrough for the source language)."""
    if is_source_language(language):
        return md_text
    cfg = cfg or {}
    lang = get_language(language)
    max_tokens = int(cfg.get("max_tokens", 16000))
    try:
        return llm.complete(cfg, _SYSTEM.format(language=lang.name), md_text,
                            max_tokens=max_tokens)
    except llm.LLMUnavailable as e:
        raise TranslationUnavailable(
            f"Translating to {lang.name} is unavailable: {e}"
        ) from e
