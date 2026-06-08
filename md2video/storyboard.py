"""Phase 1: turn a markdown doc into an editable, video-native Storyboard.

A Storyboard is the human-in-the-loop checkpoint between content and video. The
LLM runs only here (one distill call per scene). After this, nothing calls the
LLM — editing text, changing animations/styles, and rendering the MP4 are all
LLM-free.

Each Slide carries short visible cues (kicker/headline/points) plus the detailed
spoken `narration` per point. A deterministic validator enforces hard
visible-text budgets so the result is "video slides", not "markdown, but
prettier" — it fails toward *fewer words*, never tiny text, and never silently
drops meaning (trimmed content goes into narration or `source_notes`).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from . import llm
from .diagram import parse_mermaid
from .i18n import get_language
from .parse import parse_markdown
from .translate import translate_markdown

# ---- budgets ---------------------------------------------------------------
MAX_KICKER_WORDS = 5
MAX_HEADLINE_WORDS = 10
MAX_POINT_WORDS = 9
MAX_POINTS = 3
WORDS_PER_SEC = 2.5          # rough TTS rate, for duration estimates
MAX_BEAT_SECONDS = 14        # soft cap; longer narration is flagged

# Default animation per slide kind.
DEFAULT_ANIMATION = {
    "title": "fade", "points": "sequential", "statement": "spotlight",
    "diagram": "whole", "table": "together", "code": "together",
}

_AGENDA_RE = re.compile(
    r"\b(contents|agenda|outline|roadmap|table of contents|what (you|we)['’]?ll? "
    r"(cover|learn)|in this (tutorial|guide|video))\b", re.I)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Slide:
    id: str
    kind: str                                  # title|points|statement|diagram|table|code
    animation: str = ""
    kicker: str = ""
    headline: str = ""
    points: list = field(default_factory=list)   # [{text, emphasis:[], narration}]
    code: dict | None = None                     # {lang, lines:[], narration}
    table: dict | None = None                    # {layout, cards:[...], }
    diagram: dict | None = None                  # {mermaid, ir, steps:[...]}
    narration_full: str = ""
    source_notes: list = field(default_factory=list)


@dataclass
class Storyboard:
    rev: int = 1
    style: dict = field(default_factory=lambda: {"preset": "dark_keynote", "theme": "dark"})
    slides: list = field(default_factory=list)   # [Slide]
    meta: dict = field(default_factory=dict)      # title, language, source_filename

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict) -> "Storyboard":
        slides = [Slide(**s) for s in d.get("slides", [])]
        return Storyboard(rev=d.get("rev", 1),
                          style=d.get("style") or {"preset": "dark_keynote", "theme": "dark"},
                          slides=slides, meta=d.get("meta", {}))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _words(s: str) -> list[str]:
    return (s or "").split()


def _trim_words(s: str, n: int) -> tuple[str, bool]:
    w = _words(s)
    if len(w) <= n:
        return s.strip(), False
    return " ".join(w[:n]), True


def _strip_suffix(title: str) -> str:
    # Drop the parser's "— diagram"/"— table"/"(cont.)" decorations.
    title = re.sub(r"\s*[—-]\s*(diagram|table)\s*$", "", title, flags=re.I)
    title = re.sub(r"\s*\(cont\.\)\s*$", "", title, flags=re.I)
    return title.strip()


def _est_seconds(text: str) -> float:
    return len(_words(text)) / WORDS_PER_SEC


def _first_sentences(text: str, n: int = 2) -> str:
    parts = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text or "").strip())
    return " ".join(parts[:n]).strip()


def _clean_md(text: str) -> str:
    return re.sub(r"[*_`#>]+", "", text or "").strip()


# --------------------------------------------------------------------------- #
# Distillation (LLM)
# --------------------------------------------------------------------------- #
_DISTILL_SYSTEM = (
    "You turn ONE section of a technical document into ONE slide for an explainer "
    "VIDEO. The slide shows only short high-level cues; the spoken NARRATION "
    "carries all the detail. Write the slide text and narration in {language}.\n"
    "HARD RULES:\n"
    f"- kicker: at most {MAX_KICKER_WORDS} words (a short eyebrow label; may be empty)\n"
    f"- headline: at most {MAX_HEADLINE_WORDS} words, punchy, no trailing period\n"
    f"- each visible point: at most {MAX_POINT_WORDS} words — a terse phrase, NOT a sentence\n"
    f"- at most {MAX_POINTS} points\n"
    "- NEVER put full sentences or copied paragraphs in visible text; detail goes in narration\n"
    "- emphasis: 0-2 exact substrings of the point text to highlight (must occur verbatim)\n"
    "- narration: 1-3 spoken, conversational sentences per point that explain it in depth\n"
    "Return ONLY JSON."
)

_TEXT_USER = (
    'Section title: "{title}"\nSection content:\n{body}\n\n'
    'Return JSON: {{"kicker":"","headline":"","points":'
    '[{{"text":"","emphasis":[],"narration":""}}]}}'
)

_TABLE_USER = (
    'A section is a table titled "{title}". Turn it into a slide (NOT a rendered table).\n'
    "Table (markdown):\n{table_md}\n\n"
    'Return JSON, choosing ONE layout. Small table (<=4 rows) -> cards; otherwise takeaways:\n'
    '{{"headline":"","layout":"cards","cards":[{{"label":"","value":"","narration":""}}]}}\n'
    'OR {{"headline":"","layout":"takeaways","points":[{{"text":"","emphasis":[],"narration":""}}]}}'
)

_DIAGRAM_USER = (
    'The viewer sees this diagram, titled "{title}". \nMermaid:\n{mermaid}\n\n'
    'Return JSON: {{"kicker":"","headline":"","narration":"a spoken walkthrough of the '
    'diagram in 3-6 sentences, tracing it in a sensible order"}}'
)

_DIAGRAM_STEPS_USER = (
    'The viewer sees a flow diagram titled "{title}". Its nodes (id: label):\n{nodelist}\n'
    "Edges: {edgelist}\n\n"
    "Produce a step-by-step walkthrough that reveals the diagram one piece at a time in a "
    "sensible reading order. Each step reveals one (occasionally two) NEW node id(s); edges "
    "appear automatically once both endpoints are shown. Cover all nodes.\n"
    'Return JSON: {{"kicker":"","headline":"","steps":[{{"reveal":["<node id>"],'
    '"focus":"<node id>","narration":"1-2 spoken sentences explaining this part"}}]}} '
    "Use the node IDs exactly as given."
)

_CODE_USER = (
    'A section contains a code block (language: {lang}). Make a slide that shows only '
    'the KEY 3-8 lines (you may elide with "...").\nCode:\n{code}\n\nContext:\n{context}\n\n'
    'Return JSON: {{"headline":"","code":{{"lang":"{lang}","lines":["line1","line2"],'
    '"narration":"a spoken explanation of what the code does"}}}}'
)

_SKIP_HINT = (
    ' If this section is purely a table of contents / agenda / "what we\'ll cover" '
    'navigation list with no real explanatory content, return {{"skip": true}} instead.'
)

_CODE_FENCE = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)


def _detect_code(body: str):
    m = _CODE_FENCE.search(body or "")
    if not m:
        return None
    lang, code = (m.group(1) or ""), m.group(2).strip("\n")
    return (lang, code) if len(code) >= 20 else None


def _distill_cfg(cfg: dict) -> dict:
    """LLM config for distillation: prefer cfg['distill'], else cfg['translation']."""
    return cfg.get("distill") or cfg.get("translation") or {"backend": "mlx"}


def _distill_scene(scene, cfg: dict, language: str, sid: str):
    """Distill one scene into a Slide, or None if it should be skipped (agenda)."""
    lang = get_language(language)
    dcfg = _distill_cfg(cfg)
    system = _DISTILL_SYSTEM.format(language=lang.name)

    if dcfg.get("backend") == "none":
        return _heuristic_slide(scene, sid)

    try:
        if scene.kind == "diagram":
            ir = parse_mermaid(scene.mermaid)
            if ir and len(ir["nodes"]) >= 2:
                nodelist = "\n".join(f"  {n['id']}: {n['label']}" for n in ir["nodes"])
                edgelist = ", ".join(
                    f"{e['from']}->{e['to']}" + (f" ({e['label']})" if e["label"] else "")
                    for e in ir["edges"]) or "(none)"
                user = _DIAGRAM_STEPS_USER.format(title=_strip_suffix(scene.title),
                                                  nodelist=nodelist, edgelist=edgelist)
                obj = llm.complete_json(dcfg, system, user, max_tokens=1800)
                steps = obj.get("steps") or []
                if steps:
                    return Slide(id=sid, kind="diagram", animation="walkthrough",
                                 kicker=obj.get("kicker", ""), headline=obj.get("headline", ""),
                                 diagram={"mermaid": scene.mermaid, "ir": ir, "steps": steps},
                                 narration_full=" ".join(st.get("narration", "") for st in steps))
            # fallback: show the diagram whole with a walkthrough narration
            user = _DIAGRAM_USER.format(title=_strip_suffix(scene.title), mermaid=scene.mermaid)
            obj = llm.complete_json(dcfg, system, user, max_tokens=1200)
            return Slide(id=sid, kind="diagram", animation="whole",
                         kicker=obj.get("kicker", ""), headline=obj.get("headline", ""),
                         diagram={"mermaid": scene.mermaid, "ir": ir, "steps": []},
                         narration_full=obj.get("narration", ""))
        if scene.kind == "table":
            user = _TABLE_USER.format(title=_strip_suffix(scene.title), table_md=scene.table_md)
            obj = llm.complete_json(dcfg, system, user, max_tokens=1500)
            if obj.get("layout") == "cards" and obj.get("cards"):
                return Slide(id=sid, kind="table", animation=DEFAULT_ANIMATION["table"],
                             headline=obj.get("headline", ""),
                             table={"layout": "cards", "cards": obj["cards"]})
            return Slide(id=sid, kind="points", animation="sequential",
                         headline=obj.get("headline", ""), points=obj.get("points", []))

        # text — may be a code slide, an agenda to skip, or normal points
        code = _detect_code(scene.body)
        if code:
            lang_id, src = code
            context = _CODE_FENCE.sub("", scene.body).strip()[:400]
            user = _CODE_USER.format(lang=lang_id, code=src, context=context)
            obj = llm.complete_json(dcfg, system, user, max_tokens=1200)
            c = obj.get("code") or {}
            return Slide(id=sid, kind="code", animation=DEFAULT_ANIMATION["code"],
                         headline=obj.get("headline", ""),
                         code={"lang": c.get("lang", lang_id),
                               "lines": c.get("lines", [])[:8],
                               "narration": c.get("narration", "")},
                         narration_full=c.get("narration", ""))

        user = _TEXT_USER.format(title=_strip_suffix(scene.title), body=scene.body) + _SKIP_HINT
        obj = llm.complete_json(dcfg, system, user, max_tokens=1500)
        if obj.get("skip"):
            return None
        return Slide(id=sid, kind="points", animation=DEFAULT_ANIMATION["points"],
                     kicker=obj.get("kicker", ""), headline=obj.get("headline", ""),
                     points=obj.get("points", []))
    except Exception:
        return _heuristic_slide(scene, sid)


# --------------------------------------------------------------------------- #
# Heuristic fallback (no LLM / on failure) — still video-native
# --------------------------------------------------------------------------- #
def _title_slide(doc_title: str, intro_scenes: list, sid: str = "s00") -> Slide:
    headline = _trim_words(_strip_suffix(doc_title or "Overview"), MAX_HEADLINE_WORDS)[0]
    body = " ".join(_clean_md(s.body) for s in intro_scenes if getattr(s, "body", ""))
    subtitle = _trim_words(_first_sentences(body, 1), 16)[0] if body else ""
    narr = _first_sentences(body, 2) if body else headline
    points = [{"text": subtitle, "emphasis": [], "narration": ""}] if subtitle else []
    return Slide(id=sid, kind="title", animation="fade", headline=headline,
                 points=points, narration_full=narr or headline)


def _heuristic_slide(scene, sid: str) -> Slide:
    headline, _ = _trim_words(_strip_suffix(scene.title), MAX_HEADLINE_WORDS)
    code = _detect_code(scene.body) if scene.kind == "text" else None
    if code:
        lang_id, src = code
        lines = [l for l in src.splitlines() if l.strip()][:8]
        ctx = _first_sentences(_clean_md(_CODE_FENCE.sub("", scene.body)), 2)
        return Slide(id=sid, kind="code", animation=DEFAULT_ANIMATION["code"],
                     headline=headline,
                     code={"lang": lang_id, "lines": lines, "narration": ctx or headline},
                     narration_full=ctx or headline)
    if scene.kind == "diagram":
        return Slide(id=sid, kind="diagram", animation=DEFAULT_ANIMATION["diagram"],
                     headline=headline, diagram={"mermaid": scene.mermaid, "ir": None, "steps": []},
                     narration_full=f"{headline}.")
    if scene.kind == "table":
        rows = [r for r in (scene.table_md or "").splitlines()
                if r.strip().startswith("|") and "---" not in r]
        cards = []
        for r in rows[1:4]:
            cells = [c.strip() for c in r.strip("|").split("|")]
            if len(cells) >= 2:
                cards.append({"label": _trim_words(_clean_md(cells[0]), 4)[0],
                              "value": _trim_words(_clean_md(cells[1]), 6)[0],
                              "narration": _clean_md(" — ".join(cells[:2]))})
        return Slide(id=sid, kind="table", animation=DEFAULT_ANIMATION["table"],
                     headline=headline, table={"layout": "cards", "cards": cards})
    # text: list items, else first sentences
    body = scene.body or ""
    items = [re.sub(r"^[-*]\s+", "", l).strip() for l in body.splitlines()
             if re.match(r"^\s*[-*]\s+", l)]
    points = []
    for it in items[:MAX_POINTS]:
        txt = _trim_words(_clean_md(it), MAX_POINT_WORDS)[0]
        points.append({"text": txt, "emphasis": [], "narration": _clean_md(it)})
    if not points:
        sent = _first_sentences(_clean_md(body), 1)
        points = [{"text": _trim_words(sent, MAX_POINT_WORDS)[0], "emphasis": [],
                   "narration": _first_sentences(_clean_md(body), 2) or headline}]
    return Slide(id=sid, kind="points", animation=DEFAULT_ANIMATION["points"],
                 headline=headline, points=points)


# --------------------------------------------------------------------------- #
# Validation / budgets (deterministic; no silent meaning loss)
# --------------------------------------------------------------------------- #
def _validate_slide(s: Slide) -> Slide:
    notes = list(s.source_notes or [])

    if s.kicker:
        s.kicker, cut = _trim_words(s.kicker, MAX_KICKER_WORDS)
        if cut:
            notes.append("kicker trimmed")
    if s.headline:
        s.headline = re.sub(r"[.\s]+$", "", s.headline)
        s.headline, cut = _trim_words(s.headline, MAX_HEADLINE_WORDS)
        if cut:
            notes.append("headline trimmed")

    if s.kind in ("points", "statement") and s.points:
        kept, overflow = s.points[:MAX_POINTS], s.points[MAX_POINTS:]
        for p in overflow:
            notes.append(f"trimmed point: {p.get('text','')}")
        clean = []
        for p in kept:
            text, cut = _trim_words(p.get("text", ""), MAX_POINT_WORDS)
            if cut:
                notes.append(f"point shortened: {p.get('text','')}")
            narration = (p.get("narration") or "").strip() or text
            emph = [e for e in (p.get("emphasis") or []) if e and e in text]
            if _est_seconds(narration) > MAX_BEAT_SECONDS:
                notes.append(f"long narration (~{_est_seconds(narration):.0f}s): {text}")
            clean.append({"text": text, "emphasis": emph, "narration": narration})
        # preserve overflow detail in the spoken track
        overflow_narr = " ".join((p.get("narration") or p.get("text") or "") for p in overflow)
        s.points = clean
        s.narration_full = (s.narration_full or " ".join(p["narration"] for p in clean))
        if overflow_narr:
            s.narration_full = (s.narration_full + " " + overflow_narr).strip()

    if s.kind == "table" and s.table and s.table.get("cards"):
        for c in s.table["cards"]:
            c["label"] = _trim_words(c.get("label", ""), 4)[0]
            c["value"] = _trim_words(c.get("value", ""), 6)[0]
            c["narration"] = (c.get("narration") or f"{c['label']}: {c['value']}").strip()
        s.narration_full = s.narration_full or " ".join(c["narration"] for c in s.table["cards"])

    if s.kind == "code" and s.code:
        s.code["lines"] = [l for l in (s.code.get("lines") or []) if l is not None][:8]
        s.code["narration"] = (s.code.get("narration") or s.headline or "").strip()
        s.narration_full = s.narration_full or s.code["narration"]

    if s.kind == "diagram" and s.diagram is not None:
        ir = s.diagram.get("ir") or {}
        valid = {n["id"] for n in ir.get("nodes", [])}
        steps = []
        for st in s.diagram.get("steps", []):
            reveal = [r for r in st.get("reveal", []) if r in valid]
            narr = (st.get("narration") or "").strip()
            if reveal and narr:
                focus = st.get("focus") if st.get("focus") in valid else reveal[-1]
                steps.append({"reveal": reveal, "focus": focus, "narration": narr})
        s.diagram["steps"] = steps
        if not steps:
            s.animation = "whole"
        s.narration_full = (s.narration_full
                            or " ".join(st["narration"] for st in steps)
                            or s.headline).strip()

    if not s.animation:
        s.animation = DEFAULT_ANIMATION.get(s.kind, "fade")
    s.source_notes = notes
    return s


# --------------------------------------------------------------------------- #
# Structuring (cheap pass; semantic agenda handling deepened in a later step)
# --------------------------------------------------------------------------- #
def _is_agenda(scene) -> bool:
    if _AGENDA_RE.search(scene.title or ""):
        return True
    body = scene.body or ""
    if body:
        lines = [l for l in body.splitlines() if l.strip()]
        link_or_bullet = [l for l in lines if re.match(r"^\s*([-*]\s+|\d+\.\s+)", l) or "](#" in l]
        if lines and len(link_or_bullet) / len(lines) > 0.7 and _AGENDA_RE.search(body):
            return True
    return False


def _structure(scenes: list) -> list:
    return [s for s in scenes if not _is_agenda(s)]


# --------------------------------------------------------------------------- #
# Public entry point (Phase 1)
# --------------------------------------------------------------------------- #
_REVISE_SYSTEM = (
    "You edit ONE slide of an explainer video. Apply the user's instruction and "
    "return the FULL revised slide as JSON with the SAME shape and the same "
    "\"kind\". Write any text in {language}. Keep the budgets: kicker <=5 words, "
    "headline <=10 words, each visible point <=9 words, at most 3 points; put "
    "detail in narration, never full sentences on screen. Every point must keep a "
    "non-empty \"narration\". For a diagram slide, keep the \"diagram\" object and "
    "only adjust its \"steps\" (each with reveal/focus/narration) and the headline. "
    "Return ONLY JSON."
)


def revise_slide(slide: Slide, prompt: str, cfg: dict | None = None,
                 language: str = "en") -> Slide:
    """Apply a natural-language instruction to one slide via the LLM."""
    cfg = cfg or {}
    dcfg = _distill_cfg(cfg)
    lang = get_language(language)
    system = _REVISE_SYSTEM.format(language=lang.name)
    user = (f"Current slide JSON:\n{json.dumps(asdict(slide), ensure_ascii=False)}\n\n"
            f"Instruction: {prompt}\n\nReturn the full revised slide JSON.")
    obj = llm.complete_json(dcfg, system, user, max_tokens=1800)

    merged = Slide(
        id=slide.id, kind=obj.get("kind", slide.kind),
        animation=obj.get("animation", slide.animation),
        kicker=obj.get("kicker", slide.kicker),
        headline=obj.get("headline", slide.headline),
        points=obj.get("points", slide.points),
        code=obj.get("code", slide.code),
        table=obj.get("table", slide.table),
        diagram=slide.diagram,
        narration_full=obj.get("narration_full") or obj.get("narration") or slide.narration_full,
        source_notes=list(slide.source_notes or []),
    )
    if slide.kind == "diagram" and slide.diagram is not None:
        d = obj.get("diagram") or {}
        merged.diagram = {
            "mermaid": slide.diagram.get("mermaid"),
            "ir": slide.diagram.get("ir"),
            "steps": d.get("steps", slide.diagram.get("steps", [])),
        }
    return _validate_slide(merged)


def build_storyboard(md_text: str, cfg: dict | None = None, language: str = "en",
                     title: str | None = None, source_filename: str | None = None,
                     progress=None) -> Storyboard:
    cfg = cfg or {}
    md_text = translate_markdown(md_text, language, cfg.get("translation", {}))
    parsed = parse_markdown(md_text)
    doc_title = (parsed[0].doc_title if parsed else "") or (title or "")
    scenes = _structure(parsed)

    intro_scenes = [s for s in scenes if s.is_intro]
    body_scenes = [s for s in scenes if not s.is_intro]

    slides: list[Slide] = []
    has_real_title = doc_title and doc_title.strip().lower() != "overview"
    if intro_scenes or has_real_title:
        slides.append(_validate_slide(_title_slide(doc_title, intro_scenes)))

    total = len(body_scenes) or 1
    for i, scene in enumerate(body_scenes):
        slide = _distill_scene(scene, cfg, language, sid="tmp")
        if slide is not None:
            slides.append(_validate_slide(slide))
        if progress:
            progress(i + 1, total)

    for i, s in enumerate(slides):       # sequential, stable ids
        s.id = f"s{i:02d}"

    style = {"preset": cfg.get("style", {}).get("preset", "dark_keynote"),
             "theme": cfg.get("style", {}).get("theme", "dark")}
    return Storyboard(style=style, slides=slides,
                      meta={"title": doc_title or title or "", "language": language,
                            "source_filename": source_filename or ""})
