"""Turn each Scene into a spoken narration script using the Anthropic API.

This is where "explain it simply" happens. For diagram scenes we hand Claude
the raw Mermaid source and ask for a step-by-step walkthrough in the order a
presenter would actually trace it. For prose/tables we ask for a clean,
conversational simplification.

Falls back to the scene's own text if ANTHROPIC_API_KEY is not set, so the
pipeline still runs end-to-end offline (just with less polished narration).
"""

from __future__ import annotations

import os
import re

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

MODEL = os.environ.get("MD2VIDEO_MODEL", "claude-opus-4-8")

_SYSTEM = (
    "You are scripting voiceover for a short explainer video that walks a "
    "developer through a technical document. Write ONLY the words to be spoken "
    "aloud: no headings, no markdown, no stage directions, no bullet symbols. "
    "Use plain, warm, conversational language. Expand jargon the first time it "
    "appears. Keep each scene's narration tight \u2014 aim for the length given. "
    "Do not say 'this slide' or 'as you can see'; speak as a guide, not a "
    "caption reader."
)

_DIAGRAM_TASK = (
    "The viewer is looking at this Mermaid diagram (titled \"{title}\"). "
    "Narrate a walkthrough: first one sentence on what the whole diagram shows, "
    "then trace the key nodes and arrows in a sensible reading order, then one "
    "sentence on the takeaway. Target about {words} words.\n\nMermaid source:\n{src}"
)

_TEXT_TASK = (
    "Rewrite the following section (\"{title}\") as spoken narration that "
    "explains it simply to a developer new to this stack. Target about {words} "
    "words.\n\nSection:\n{src}"
)

_TABLE_TASK = (
    "The viewer is looking at a table titled \"{title}\". Summarise what the "
    "table conveys and call out the 2-3 most important rows in spoken prose. "
    "Do not read every cell. Target about {words} words.\n\nTable (markdown):\n{src}"
)


def _target_words(scene) -> int:
    raw = scene.mermaid or scene.body or scene.table_md
    if scene.kind == "diagram":
        return min(160, 60 + raw.count("\n") * 6)
    return max(40, min(150, len(raw.split()) // 2 + 30))


def _clean(text: str) -> str:
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def narrate_scenes(scenes, *, fallback_only: bool = False, progress=None) -> None:
    client = None
    if not fallback_only and Anthropic is not None and os.environ.get("ANTHROPIC_API_KEY"):
        client = Anthropic()

    total = len(scenes)
    for i, s in enumerate(scenes):
        words = _target_words(s)
        if client is None:
            s.narration = _clean(s.body or s.table_md or
                                 f"Here is the {s.section} diagram.")
            if progress:
                progress(i + 1, total)
            continue
        if s.kind == "diagram":
            task = _DIAGRAM_TASK.format(title=s.title, words=words, src=s.mermaid)
        elif s.kind == "table":
            task = _TABLE_TASK.format(title=s.title, words=words, src=s.table_md)
        else:
            task = _TEXT_TASK.format(title=s.title, words=words, src=s.body)

        msg = client.messages.create(
            model=MODEL, max_tokens=600, system=_SYSTEM,
            messages=[{"role": "user", "content": task}],
        )
        s.narration = _clean("".join(
            b.text for b in msg.content if getattr(b, "type", "") == "text"))
        if progress:
            progress(i + 1, total)
