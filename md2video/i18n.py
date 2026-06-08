"""Language registry for multilingual output.

Adding a language is one entry in LANGUAGES. `default_voice` is the Kokoro voice
used when the UI/CLI doesn't pick one; the actual Kokoro `lang_code` is derived
from the voice-id prefix at synthesis time (see tts._kokoro). `is_source` marks
the language the markdown is authored in — those need no translation.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str            # ISO-ish short code used everywhere (en, es, ...)
    name: str            # English name, used in LLM prompts
    native_name: str     # shown in the UI
    default_voice: str   # Kokoro voice id to default to
    is_source: bool = False   # True for the language docs are written in
    # Spoken fallback caption for a diagram when there's no narration LLM.
    diagram_intro: str = "Here is the {section} diagram."


# The markdown is authored in English, so `en` is the source (no translation).
LANGUAGES: dict[str, Language] = {
    "en": Language("en", "English", "English", "af_heart", is_source=True),
    "es": Language("es", "Spanish", "Español", "ef_dora",
                   diagram_intro="Veamos el diagrama de {section}."),
}

DEFAULT_LANGUAGE = "en"


def get_language(code: str) -> Language:
    return LANGUAGES.get(code, LANGUAGES[DEFAULT_LANGUAGE])


def is_source_language(code: str) -> bool:
    return get_language(code).is_source
