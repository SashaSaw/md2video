"""Expand Storyboard slides into Beats — the per-frame render/tts/assemble unit.

A Beat is one rendered frame paired with one narration chunk. Multi-point slides
expand into several beats (one per revealed point) so the points appear in sync
with the spoken narration; single-shot slides are one beat. Beats carry the
slide + style so the renderer can draw the right reveal state, and mark slide
boundaries so assembly only fades between slides (not between reveals).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .storyboard import DEFAULT_ANIMATION


@dataclass
class Beat:
    index: int
    slide_id: str
    kind: str
    narration: str
    reveal: dict | None          # render reveal state ({visible, active} / None = all)
    slide: object                # the Slide (for rendering)
    style: dict                  # {preset, theme}
    is_slide_start: bool = False
    is_slide_end: bool = False
    image_path: str = ""         # filled by render (static PNG frame)
    gif_path: str = ""           # filled by render for gif slides (animated source)
    overlay_path: str = ""       # filled by render: transparent caption chrome over the gif
    audio_path: str = ""         # filled by tts
    duration: float = 0.0        # filled by tts


def _point_narr(p: dict) -> str:
    return (p.get("narration") or p.get("text") or "").strip()


def _slide_beats(slide, style: dict) -> list[Beat]:
    anim = slide.animation or DEFAULT_ANIMATION.get(slide.kind, "fade")
    out: list[Beat] = []

    def mk(narr, reveal):
        out.append(Beat(index=0, slide_id=slide.id, kind=slide.kind,
                        narration=(narr or "").strip(), reveal=reveal,
                        slide=slide, style=style))

    if slide.kind == "points" and anim == "sequential" and slide.points:
        for i, p in enumerate(slide.points):
            mk(_point_narr(p), {"visible": i + 1, "active": i})
    elif slide.kind in ("points", "statement") and anim == "spotlight" and slide.points:
        for i, p in enumerate(slide.points):
            mk(_point_narr(p), {"active": i})
    elif slide.kind == "table" and anim == "cards_sequential" and (slide.table or {}).get("cards"):
        for i, c in enumerate(slide.table["cards"]):
            mk(c.get("narration", ""), {"visible": i + 1})
    elif slide.kind == "diagram" and anim == "walkthrough" and (slide.diagram or {}).get("steps"):
        cumulative: list[str] = []
        for st in slide.diagram["steps"]:
            for r in st.get("reveal", []):
                if r not in cumulative:
                    cumulative.append(r)
            mk(st.get("narration", ""), {"nodes": list(cumulative), "focus": st.get("focus")})
    else:
        # together / whole / fade / default -> a single, fully-revealed beat
        narr = slide.narration_full
        if not narr and slide.points:
            narr = " ".join(_point_narr(p) for p in slide.points)
        mk(narr, None)

    if not out:                                   # safety: never zero beats
        mk(slide.narration_full or slide.headline, None)
    for b in out:                                 # never silent
        b.narration = b.narration or slide.headline
    out[0].is_slide_start = True
    out[-1].is_slide_end = True
    return out


def expand_slides_to_beats(slides, style: dict) -> list[Beat]:
    beats: list[Beat] = []
    for slide in slides:
        beats.extend(_slide_beats(slide, style))
    for i, b in enumerate(beats):
        b.index = i
    return beats
