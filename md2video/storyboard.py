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
from .ids import new_id
from .parse import parse_markdown
from .translate import translate_markdown

# ---- budgets ---------------------------------------------------------------
MAX_KICKER_WORDS = 5
MAX_HEADLINE_WORDS = 10
MAX_POINT_WORDS = 9
MAX_POINTS = 4
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
    kind: str                                  # title|points|statement|diagram|table|code|image|gif
    animation: str = ""
    kicker: str = ""
    headline: str = ""
    points: list = field(default_factory=list)   # [{text, emphasis:[], narration}]
    code: dict | None = None                     # {lang, lines:[], narration}
    table: dict | None = None                    # {layout, cards:[...], }
    diagram: dict | None = None                  # {mermaid, ir, steps:[...]}
    image: dict | None = None                    # {prompt, negative_prompt, seed, model, steps, path}
    gif: dict | None = None                      # {query, provider, gif_id, url, still_url, path, result_index}
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

_USER_STYLE_SYSTEM = (
    "\n\nOptional generation instruction from the user:\n"
    "{instruction}\n"
    "Apply this instruction to both the visible slide choices and the spoken narration, "
    "while still obeying every HARD RULE above and preserving the source meaning."
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


def _style_instruction(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:800]


def _with_style_instruction(user: str, style_prompt: str = "") -> str:
    if not style_prompt:
        return user
    return (
        f"Generation instruction for this slide: {style_prompt}\n"
        "Follow it when choosing visible slide text and narration, while preserving "
        "the source meaning and obeying the JSON schema.\n\n"
        f"{user}"
    )


def _distill_scene(scene, cfg: dict, language: str, sid: str, style_prompt: str = ""):
    """Distill one scene into a Slide, or None if it should be skipped (agenda)."""
    lang = get_language(language)
    dcfg = _distill_cfg(cfg)
    system = _DISTILL_SYSTEM.format(language=lang.name)
    if style_prompt:
        system += _USER_STYLE_SYSTEM.format(instruction=style_prompt)

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
                user = _with_style_instruction(user, style_prompt)
                obj = llm.complete_json(dcfg, system, user, max_tokens=1800)
                steps = obj.get("steps") or []
                if steps:
                    return Slide(id=sid, kind="diagram", animation="walkthrough",
                                 kicker=obj.get("kicker", ""), headline=obj.get("headline", ""),
                                 diagram={"mermaid": scene.mermaid, "ir": ir, "steps": steps},
                                 narration_full=" ".join(st.get("narration", "") for st in steps))
            # fallback: show the diagram whole with a walkthrough narration
            user = _DIAGRAM_USER.format(title=_strip_suffix(scene.title), mermaid=scene.mermaid)
            user = _with_style_instruction(user, style_prompt)
            obj = llm.complete_json(dcfg, system, user, max_tokens=1200)
            return Slide(id=sid, kind="diagram", animation="whole",
                         kicker=obj.get("kicker", ""), headline=obj.get("headline", ""),
                         diagram={"mermaid": scene.mermaid, "ir": ir, "steps": []},
                         narration_full=obj.get("narration", ""))
        if scene.kind == "table":
            user = _TABLE_USER.format(title=_strip_suffix(scene.title), table_md=scene.table_md)
            user = _with_style_instruction(user, style_prompt)
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
            user = _with_style_instruction(user, style_prompt)
            obj = llm.complete_json(dcfg, system, user, max_tokens=1200)
            c = obj.get("code") or {}
            return Slide(id=sid, kind="code", animation=DEFAULT_ANIMATION["code"],
                         headline=obj.get("headline", ""),
                         code={"lang": c.get("lang", lang_id),
                               "lines": c.get("lines", [])[:8],
                               "narration": c.get("narration", "")},
                         narration_full=c.get("narration", ""))

        user = _TEXT_USER.format(title=_strip_suffix(scene.title), body=scene.body) + _SKIP_HINT
        user = _with_style_instruction(user, style_prompt)
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
def _ensure_unit_ids(s: Slide) -> None:
    """Give the slide and its editable units stable IDs (idempotent)."""
    if not s.id:
        s.id = new_id("sl")
    for p in s.points or []:
        p.setdefault("id", new_id("p"))
    if s.table and s.table.get("cards"):
        for c in s.table["cards"]:
            c.setdefault("id", new_id("c"))
    if s.diagram and s.diagram.get("steps"):
        for st in s.diagram["steps"]:
            st.setdefault("id", new_id("st"))


def _validate_slide(s: Slide) -> Slide:
    _ensure_unit_ids(s)
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
            clean.append({"id": p.get("id") or new_id("p"), "text": text,
                          "emphasis": emph, "narration": narration})
        # preserve overflow detail in the spoken track
        overflow_narr = " ".join((p.get("narration") or p.get("text") or "") for p in overflow)
        s.points = clean
        s.narration_full = (s.narration_full or " ".join(p["narration"] for p in clean))
        if overflow_narr:
            s.narration_full = (s.narration_full + " " + overflow_narr).strip()

    if s.kind == "table" and s.table and s.table.get("cards"):
        cards = [c for c in s.table["cards"] if (c.get("label") or c.get("value"))]
        for c in cards:
            c["label"] = _trim_words(c.get("label", ""), 4)[0]
            c["value"] = _trim_words(c.get("value", ""), 6)[0]
            c["narration"] = (c.get("narration") or f"{c['label']}: {c['value']}").strip()
        s.table["cards"] = cards
        s.narration_full = s.narration_full or " ".join(c["narration"] for c in cards)

    if s.kind == "code" and s.code:
        s.code["lines"] = [l for l in (s.code.get("lines") or []) if l is not None][:8]
        s.code["narration"] = (s.code.get("narration") or s.headline or "").strip()
        s.narration_full = s.narration_full or s.code["narration"]

    if s.kind == "image":
        s.image = s.image or {"prompt": "", "negative_prompt": "", "seed": None,
                              "model": "", "steps": 4, "path": ""}
        s.narration_full = (s.narration_full or s.headline or "").strip()

    if s.kind == "gif":
        s.gif = s.gif or {"query": "", "provider": "", "gif_id": "", "url": "",
                          "still_url": "", "path": "", "result_index": 0}
        s.narration_full = (s.narration_full or s.headline or s.gif.get("query") or "").strip()

    if s.kind == "diagram" and s.diagram is not None:
        ir = s.diagram.get("ir") or {}
        valid = {n["id"] for n in ir.get("nodes", [])}
        steps = []
        for st in s.diagram.get("steps", []):
            reveal = [r for r in st.get("reveal", []) if r in valid]
            narr = (st.get("narration") or "").strip()
            if reveal and narr:
                focus = st.get("focus") if st.get("focus") in valid else reveal[-1]
                steps.append({"id": st.get("id") or new_id("st"), "reveal": reveal,
                              "focus": focus, "narration": narr})
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
    "return the FULL revised slide as JSON with the same \"kind\". Write any text "
    "in {language}. You MAY add or remove points (max 4), rewrite the headline, "
    "points, or narration, edit table cards, reorder/rewrite diagram steps, and "
    "shift tone. Keep budgets: kicker <=5 words, headline <=10 words, each visible "
    "point <=9 words; put detail in narration, never full sentences on screen. "
    "Keep a non-empty \"narration\" for every point/card/step. PRESERVE the \"id\" "
    "field on any point/card/step you keep; omit id for new ones. For a diagram "
    "slide, keep the \"diagram\" mermaid/ir unchanged and only change \"steps\" — "
    "each step's \"reveal\" must use the listed node ids. Return ONLY JSON."
)


def revise_slide(slide: Slide, prompt: str, cfg: dict | None = None,
                 language: str = "en") -> Slide:
    """Apply a natural-language instruction to one slide via the LLM."""
    cfg = cfg or {}
    dcfg = _distill_cfg(cfg)
    lang = get_language(language)
    system = _REVISE_SYSTEM.format(language=lang.name)
    extra = ""
    if slide.kind == "diagram" and slide.diagram and slide.diagram.get("ir"):
        nodes = (slide.diagram["ir"] or {}).get("nodes", [])
        extra = "\nDiagram node ids: " + ", ".join(
            f"{n['id']}({n['label']})" for n in nodes)
    user = (f"Current slide JSON:\n{json.dumps(asdict(slide), ensure_ascii=False)}{extra}\n\n"
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


# --------------------------------------------------------------------------- #
# Tone (rewrite narration only, meaning-preserving)
# --------------------------------------------------------------------------- #
_RETONE_SYSTEM = (
    "You rewrite ONLY the spoken NARRATION of a slide in a {tone} tone of voice, "
    "in {language}. You are given a JSON object mapping keys to narration text; "
    "return a JSON object with the SAME keys and rewritten values.\n"
    "STRICT: do not change meaning; keep every code identifier, file name, URL, "
    "API/product name, and number EXACTLY as-is; do not add or drop keys; do not "
    "reorder. Only change wording/voice. Return ONLY JSON."
)


def _narration_units(slide: Slide) -> dict:
    u = {}
    for p in slide.points or []:
        if (p.get("narration") or "").strip():
            u[f"p:{p['id']}"] = p["narration"]
    for st in ((slide.diagram or {}).get("steps") or []):
        if (st.get("narration") or "").strip():
            u[f"st:{st['id']}"] = st["narration"]
    for c in ((slide.table or {}).get("cards") or []):
        if (c.get("narration") or "").strip():
            u[f"c:{c['id']}"] = c["narration"]
    if (slide.narration_full or "").strip():
        u["full"] = slide.narration_full
    if slide.code and (slide.code.get("narration") or "").strip():
        u["code"] = slide.code["narration"]
    return u


def retone_slide(slide: Slide, tone: str, cfg: dict | None = None,
                 language: str = "en") -> Slide:
    """Rewrite only this slide's narration in `tone`; structure/text/meaning preserved."""
    units = _narration_units(slide)
    if not units:
        return slide
    dcfg = _distill_cfg(cfg or {})
    lang = get_language(language)
    system = _RETONE_SYSTEM.format(tone=tone, language=lang.name)
    user = "Rewrite each value; keep keys identical:\n" + json.dumps(units, ensure_ascii=False)
    try:
        out = llm.complete_json(dcfg, system, user, max_tokens=2200)
    except Exception:
        return slide
    for p in slide.points or []:
        if out.get(f"p:{p['id']}"):
            p["narration"] = out[f"p:{p['id']}"]
    for st in ((slide.diagram or {}).get("steps") or []):
        if out.get(f"st:{st['id']}"):
            st["narration"] = out[f"st:{st['id']}"]
    for c in ((slide.table or {}).get("cards") or []):
        if out.get(f"c:{c['id']}"):
            c["narration"] = out[f"c:{c['id']}"]
    if out.get("full"):
        slide.narration_full = out["full"]
    if slide.code and out.get("code"):
        slide.code["narration"] = out["code"]
    return _validate_slide(slide)


# --------------------------------------------------------------------------- #
# Storyboard-level "Ask" — propose ops, then apply
# --------------------------------------------------------------------------- #
_ASK_SYSTEM = (
    "You plan edits to a slide deck (storyboard) for an explainer video. Given the "
    "current slides and an instruction, return JSON {\"summary\":\"one line\","
    "\"ops\":[...]} with AT MOST 6 operations. Allowed ops (use exact slide ids):\n"
    "{\"op\":\"add_slide\",\"at\":\"start\"|\"after\",\"after\":\"<id>\",\"kind\":\"title|points|statement\",\"headline\":\"\",\"prompt\":\"what it should say\"}\n"
    "{\"op\":\"delete_slide\",\"id\":\"<id>\"}\n"
    "{\"op\":\"reorder\",\"order\":[\"<id>\",...]}  (include ALL slide ids)\n"
    "{\"op\":\"edit_slide\",\"id\":\"<id>\",\"instruction\":\"...\"}\n"
    "{\"op\":\"add_gif\",\"after\":\"<id>\",\"query\":\"short giphy search e.g. mind blown\",\"caption\":\"\",\"narration\":\"spoken joke/anecdote over the gif\"}\n"
    "{\"op\":\"retone\",\"tone\":\"...\",\"scope\":\"all\"}\n"
    "{\"op\":\"set_style\",\"preset\":\"dark_keynote|editorial_light|minimal_statement\",\"theme\":\"dark|light\"}\n"
    "Never delete every slide. Return ONLY JSON."
)


def propose_ops(sb: Storyboard, prompt: str, cfg: dict | None = None,
                language: str = "en") -> dict:
    summary = [{"id": s.id, "kind": s.kind, "headline": s.headline} for s in sb.slides]
    user = "Slides:\n" + json.dumps(summary, ensure_ascii=False) + f"\n\nInstruction: {prompt}"
    try:
        out = llm.complete_json(_distill_cfg(cfg or {}), _ASK_SYSTEM, user, max_tokens=1400)
    except Exception:
        return {"ops": [], "summary": "Could not turn that into editable steps."}
    return {"ops": (out.get("ops") or [])[:6], "summary": out.get("summary", "")}


# --------------------------------------------------------------------------- #
# "Make it funnier" — let the LLM pick well-known reaction GIFs to drop in
# --------------------------------------------------------------------------- #
_GAGS_SYSTEM = (
    "You punch up an explainer video with funny, well-known reaction GIFs used as "
    "anecdotal asides — a gif plays full-screen while the narrator cracks a joke or "
    "gives a quick funny example that lands the point, YouTuber-style.\n"
    "{gate}"
    "When you add them, choose up to {n} spots where a WELL-KNOWN gif fits the moment "
    "(match the gif to what's being said — don't be random). For each return:\n"
    "- after: the id of the slide it should follow\n"
    "- query: a SHORT Giphy search for a popular gif (2-4 words IN ENGLISH, e.g. "
    "'mind blown', 'this is fine', 'mic drop', 'shocked pikachu')\n"
    "- caption: a punchy on-screen line (<=6 words, in {language})\n"
    "- narration: 1-2 sentences of VOICEOVER (in {language}) — the joke or a funny "
    "anecdotal example that lands the point while the gif plays. It's spoken aloud, "
    "so make it sound natural.\n"
    "Return ONLY JSON: {{\"gags\":[{{\"after\":\"<id>\",\"query\":\"...\","
    "\"caption\":\"...\",\"narration\":\"...\"}}]}}."
)

# Auto mode (generation time): only add gifs if the style/topic actually wants humour.
_GAGS_GATE_AUTO = (
    "FIRST decide whether this video should be entertaining/funny at all, based on the "
    "requested style — \"{style}\" — and the subject matter. If a serious, formal, or "
    "neutral tone fits better, return {{\"gags\":[]}} and add nothing. Only add gifs "
    "when humour genuinely suits it.\n"
)
# Manual mode ("Make it funnier" button): the user asked for it, so always add some.
_GAGS_GATE_FORCE = (
    "The user explicitly asked to make this funnier, so add gifs even if the topic is "
    "dry — find the best spots for them.\n"
)


def propose_gags(sb: Storyboard, cfg: dict | None = None, language: str = "en",
                 max_gags: int = 4, *, style_prompt: str = "", force: bool = True) -> list[dict]:
    """Ask the LLM where funny GIFs + anecdotal voiceover would land.

    Returns [{after, query, caption, narration}]. With force=False the model first
    decides whether humour fits (given `style_prompt` + content) and may return []
    — that's how generation infers when a deck should be funny.
    """
    lang = get_language(language)
    gate = (_GAGS_GATE_FORCE if force
            else _GAGS_GATE_AUTO.format(style=(style_prompt or "general explainer").strip()))
    system = _GAGS_SYSTEM.format(n=max_gags, language=lang.name, gate=gate)
    summary = [{"id": s.id, "kind": s.kind, "headline": s.headline,
                "narration": (s.narration_full or "")[:240]} for s in sb.slides]
    user = "Slides:\n" + json.dumps(summary, ensure_ascii=False)
    try:
        out = llm.complete_json(_distill_cfg(cfg or {}), system, user, max_tokens=1200)
    except Exception:
        return []
    ids = {s.id for s in sb.slides}
    clean = []
    for g in (out.get("gags") or [])[:max_gags]:
        q = (g.get("query") or "").strip()
        after = g.get("after")
        if q and after in ids:
            clean.append({"after": after, "query": q,
                          "caption": (g.get("caption") or "").strip(),
                          "narration": (g.get("narration") or "").strip()})
    return clean


def _default_slide(kind: str, headline: str = "") -> Slide:
    sid = new_id("sl")
    if kind == "image":
        return Slide(id=sid, kind="image", animation="fade", headline=headline or "",
                     image={"prompt": "", "negative_prompt": "", "seed": None,
                            "model": "", "steps": 4, "path": ""},
                     narration_full=headline or "")
    if kind == "gif":
        return Slide(id=sid, kind="gif", animation="fade", headline=headline or "",
                     gif={"query": "", "provider": "", "gif_id": "", "url": "",
                          "still_url": "", "path": "", "result_index": 0},
                     narration_full=headline or "")
    if kind == "title":
        return Slide(id=sid, kind="title", animation="fade", headline=headline or "Title",
                     points=[{"id": new_id("p"), "text": "", "emphasis": [], "narration": ""}],
                     narration_full=headline or "")
    if kind == "statement":
        return Slide(id=sid, kind="statement", animation="spotlight", headline="",
                     points=[{"id": new_id("p"), "text": headline or "Statement",
                              "emphasis": [], "narration": headline or ""}])
    return Slide(id=sid, kind="points", animation="sequential", headline=headline or "Section",
                 points=[{"id": new_id("p"), "text": "Point", "emphasis": [], "narration": "…"}])


def _slide_from_spec(op: dict, cfg: dict, language: str) -> Slide:
    s = _default_slide(op.get("kind", "points"), op.get("headline", ""))
    if op.get("prompt"):
        try:
            s = revise_slide(s, op["prompt"], cfg, language)
        except Exception:
            pass
    return _validate_slide(s)


def apply_ops(sb: Storyboard, ops: list, cfg: dict | None = None,
              language: str = "en") -> tuple[Storyboard, list]:
    """Apply a validated op list to the storyboard. Returns (sb, applied-op-names)."""
    cfg = cfg or {}
    applied = []
    for op in (ops or [])[:6]:
        t = op.get("op")
        try:
            if t == "set_style":
                if op.get("preset"):
                    sb.style["preset"] = op["preset"]
                if op.get("theme"):
                    sb.style["theme"] = op["theme"]
                applied.append("set_style")
            elif t == "delete_slide":
                ids = {s.id for s in sb.slides}
                if op.get("id") in ids and len(sb.slides) > 1:
                    sb.slides = [s for s in sb.slides if s.id != op["id"]]
                    applied.append("delete_slide")
            elif t == "reorder":
                ids = {s.id for s in sb.slides}
                order = [i for i in op.get("order", []) if i in ids]
                if set(order) == ids and len(order) == len(sb.slides):
                    pos = {i: k for k, i in enumerate(order)}
                    sb.slides.sort(key=lambda s: pos[s.id])
                    applied.append("reorder")
            elif t == "add_slide":
                ns = _slide_from_spec(op, cfg, language)
                ids = [s.id for s in sb.slides]
                if op.get("at") == "start":
                    sb.slides.insert(0, ns)
                elif op.get("after") in ids:
                    sb.slides.insert(ids.index(op["after"]) + 1, ns)
                else:
                    sb.slides.append(ns)
                applied.append("add_slide")
            elif t == "edit_slide":
                for k, s in enumerate(sb.slides):
                    if s.id == op.get("id"):
                        sb.slides[k] = revise_slide(s, op.get("instruction", ""), cfg, language)
                        applied.append("edit_slide")
                        break
            elif t == "add_gif":
                ns = _default_slide("gif", op.get("caption", ""))
                ns.gif["query"] = (op.get("query") or "").strip()
                ns.headline = (op.get("caption") or "").strip()
                ns.narration_full = (op.get("narration") or "").strip() or ns.headline or ns.gif["query"]
                ids = [s.id for s in sb.slides]
                if op.get("at") == "start":
                    sb.slides.insert(0, ns)
                elif op.get("after") in ids:
                    sb.slides.insert(ids.index(op["after"]) + 1, ns)
                else:
                    sb.slides.append(ns)
                applied.append("add_gif")
            elif t == "retone":
                scope = op.get("scope", "all")
                tone = op.get("tone", "clear")
                for k, s in enumerate(sb.slides):
                    if scope == "all" or s.id in scope:
                        sb.slides[k] = retone_slide(s, tone, cfg, language)
                applied.append("retone")
        except Exception:
            continue
    if not sb.slides:                       # never empty the deck
        sb.slides = [_default_slide("points", "Slide")]
    return sb, applied


def build_storyboard(md_text: str, cfg: dict | None = None, language: str = "en",
                     title: str | None = None, source_filename: str | None = None,
                     progress=None, style_prompt: str | None = None) -> Storyboard:
    cfg = cfg or {}
    style_prompt = _style_instruction(style_prompt)
    md_text = translate_markdown(md_text, language, cfg.get("translation", {}))
    parsed = parse_markdown(md_text)
    doc_title = (parsed[0].doc_title if parsed else "") or (title or "")
    scenes = _structure(parsed)

    intro_scenes = [s for s in scenes if s.is_intro]
    body_scenes = [s for s in scenes if not s.is_intro]

    slides: list[Slide] = []
    has_real_title = doc_title and doc_title.strip().lower() != "overview"
    if intro_scenes or has_real_title:
        slides.append(_validate_slide(_title_slide(doc_title, intro_scenes, sid=new_id("sl"))))

    total = len(body_scenes) or 1
    for i, scene in enumerate(body_scenes):
        slide = _distill_scene(scene, cfg, language, sid=new_id("sl"),
                               style_prompt=style_prompt)
        if slide is not None:
            slides.append(_validate_slide(slide))   # stable IDs, never renumbered
        if progress:
            progress(i + 1, total)

    style = {"preset": cfg.get("style", {}).get("preset", "dark_keynote"),
             "theme": cfg.get("style", {}).get("theme", "dark")}
    return Storyboard(style=style, slides=slides,
                      meta={"title": doc_title or title or "", "language": language,
                            "source_filename": source_filename or "",
                            "style_prompt": style_prompt})
