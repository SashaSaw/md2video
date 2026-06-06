"""Parse a markdown doc into an ordered list of Scenes.

A "scene" is the atomic unit of the video: one visual + one narration segment.
We split a document by H2 (## ) sections, then break each section into scenes:

    - the lead prose before the first diagram/table  -> a TEXT scene
    - each ```mermaid block                          -> a DIAGRAM scene
    - each markdown table                            -> a TABLE scene
    - any prose that follows                         -> further TEXT scenes

This keeps each on-screen visual paired with a single chunk of speech, which
is what makes the final video feel paced rather than a wall of narration over
one static image.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Scene:
    index: int
    kind: str                 # "text" | "diagram" | "table"
    section: str              # the H2 heading this scene belongs to
    title: str                # short heading shown on the slide
    body: str = ""            # markdown prose (text scenes) / caption
    mermaid: str = ""         # raw mermaid source (diagram scenes)
    table_md: str = ""        # raw markdown table (table scenes)
    narration: str = ""       # filled in later by narrate.py
    image_path: str = ""      # filled in later by render.py
    audio_path: str = ""      # filled in later by tts.py
    duration: float = 0.0     # filled in later by tts.py (seconds)


_MERMAID_RE = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.*)$", re.MULTILINE)


def _is_table_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|")


def _split_blocks(section_body: str):
    """Yield ('prose'|'mermaid'|'table', text) blocks in document order."""
    # First, pull mermaid fences out, replacing them with sentinels so the
    # surrounding prose keeps its position.
    parts = []
    last = 0
    for m in _MERMAID_RE.finditer(section_body):
        if m.start() > last:
            parts.append(("prose", section_body[last:m.start()]))
        parts.append(("mermaid", m.group(1).strip()))
        last = m.end()
    if last < len(section_body):
        parts.append(("prose", section_body[last:]))

    # Now walk prose blocks and peel out tables line-by-line.
    for kind, text in parts:
        if kind != "prose":
            yield kind, text
            continue
        buf, table = [], []
        for line in text.splitlines():
            if _is_table_line(line):
                if buf:
                    yield "prose", "\n".join(buf).strip()
                    buf = []
                table.append(line)
            else:
                if table:
                    yield "table", "\n".join(table).strip()
                    table = []
                buf.append(line)
        if table:
            yield "table", "\n".join(table).strip()
        if buf:
            yield "prose", "\n".join(buf).strip()


def parse_markdown(md: str) -> list[Scene]:
    h1 = _H1_RE.search(md)
    doc_title = h1.group(1).strip() if h1 else "Overview"

    # Split into H2 sections, keeping an intro chunk for everything before the
    # first H2 (the document lead-in).
    chunks = re.split(r"^##\s+(.*)$", md, flags=re.MULTILINE)
    sections = [("", chunks[0])]
    for i in range(1, len(chunks), 2):
        sections.append((chunks[i].strip(), chunks[i + 1]))

    scenes: list[Scene] = []
    idx = 0
    for heading, body in sections:
        section_title = heading or doc_title
        # Strip horizontal rules and the H1 from the intro chunk.
        body = re.sub(r"^#\s+.*$", "", body, flags=re.MULTILINE)
        body = re.sub(r"^\s*---\s*$", "", body, flags=re.MULTILINE)

        prose_seen = False
        for kind, text in _split_blocks(body):
            text = text.strip()
            if not text:
                continue
            if kind == "prose":
                # Skip prose that is only sub-headings or stray markup.
                if re.fullmatch(r"(#+.*\s*)+", text):
                    continue
                title = section_title if not prose_seen else f"{section_title} (cont.)"
                scenes.append(Scene(idx, "text", section_title, title, body=text))
                prose_seen = True
            elif kind == "mermaid":
                scenes.append(Scene(idx, "diagram", section_title,
                                    f"{section_title} — diagram", mermaid=text))
            elif kind == "table":
                scenes.append(Scene(idx, "table", section_title,
                                    f"{section_title} — table", table_md=text))
            idx += 1
    return scenes


if __name__ == "__main__":
    import sys
    scenes = parse_markdown(open(sys.argv[1]).read())
    for s in scenes:
        preview = (s.body or s.mermaid or s.table_md).replace("\n", " ")[:60]
        print(f"[{s.index:02d}] {s.kind:8s} | {s.title[:34]:34s} | {preview}")
