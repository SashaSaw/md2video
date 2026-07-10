"""Render each Scene to a 1920x1080 PNG using headless Chromium (Playwright).

Why a browser instead of a dedicated diagram tool: Mermaid is a JS library, so
rendering it in a real browser is the highest-fidelity option and it gives us
prose, tables, and diagrams through one styling path. Each scene becomes one
HTML page; we screenshot the viewport. Content that would overflow 1080p is
scaled down to fit.

Requires:  pip install playwright markdown  &&  playwright install chromium
"""

from __future__ import annotations

import base64
import html
import os
from pathlib import Path

W, H = 1920, 1080

# Mermaid is pinned and loaded from a CDN. For a fully offline build, vendor
# mermaid.min.js locally and point the <script src> at file://.
MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0; width: %(W)dpx; height: %(H)dpx;
  background: #faf9f5; color: #1f1e1c;
  font-family: -apple-system, "Segoe UI", Inter, system-ui, sans-serif;
  display: flex; align-items: center; justify-content: center;
}
#stage { width: 1640px; max-height: 920px; transform-origin: center center; }
h1.kicker {
  font-size: 30px; font-weight: 600; color: #b5532a; letter-spacing: .3px;
  margin: 0 0 28px; text-transform: none;
}
.content { font-size: 34px; line-height: 1.5; }
.content p { margin: 0 0 20px; }
.content h3 { font-size: 36px; margin: 8px 0 16px; color: #2c2b28; }
.content ul, .content ol { margin: 0 0 18px; padding-left: 38px; }
.content li { margin: 0 0 12px; }
.content code {
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  background: #efede6; padding: 2px 8px; border-radius: 6px; font-size: .85em;
}
.content strong { color: #111; }
table { border-collapse: collapse; width: 100%%; font-size: 28px; }
th, td { border: 1px solid #d9d6cc; padding: 14px 20px; text-align: left; vertical-align: top; }
th { background: #efece3; font-weight: 600; }
tr:nth-child(even) td { background: #f4f2ec; }
.mermaid { display: flex; justify-content: center; }
.mermaid svg { max-width: 1640px; max-height: 900px; height: auto; }
"""  % {"W": W, "H": H}


def _md_to_html(text: str) -> str:
    import markdown as md_lib
    return md_lib.markdown(text, extensions=["fenced_code", "tables"])


def _page_html(scene, include_mermaid: bool) -> str:
    if scene.kind == "diagram":
        inner = f'<pre class="mermaid">{html.escape(scene.mermaid)}</pre>'
    elif scene.kind == "table":
        inner = f'<div class="content">{_md_to_html(scene.table_md)}</div>'
    else:
        inner = f'<div class="content">{_md_to_html(scene.body)}</div>'

    kicker = html.escape(scene.title)
    mermaid_script = ""
    if include_mermaid and scene.kind == "diagram":
        mermaid_script = (
            f'<script src="{MERMAID_CDN}"></script>'
            '<script>mermaid.initialize({startOnLoad:true,theme:"neutral",'
            'flowchart:{useMaxWidth:true},'
            'themeVariables:{fontFamily:"Inter, system-ui, sans-serif",fontSize:"22px"}});'
            '</script>'
        )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{_CSS}</style></head><body>"
        f"<div id='stage'><h1 class='kicker'>{kicker}</h1>{inner}</div>"
        f"{mermaid_script}</body></html>"
    )


_FIT_JS = """() => {
  const stage = document.getElementById('stage');
  const r = stage.getBoundingClientRect();
  const sx = (1760) / r.width;
  const sy = (1000) / r.height;
  const s = Math.min(1, sx, sy);
  if (s < 1) stage.style.transform = 'scale(' + s + ')';
}"""


def render_scenes(scenes, out_dir: str, progress=None) -> None:
    from playwright.sync_api import sync_playwright
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    total = len(scenes)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H},
                                device_scale_factor=1)
        for i, s in enumerate(scenes):
            page.set_content(_page_html(s, include_mermaid=True),
                             wait_until="networkidle")
            if s.kind == "diagram":
                # Wait for mermaid to swap the <pre> for an <svg>.
                try:
                    page.wait_for_selector(".mermaid svg", timeout=15000)
                except Exception:
                    pass
            page.evaluate(_FIT_JS)
            page.wait_for_timeout(150)
            path = out / f"scene_{s.index:03d}.png"
            page.screenshot(path=str(path))
            s.image_path = str(path)
            if progress:
                progress(i + 1, total)
        browser.close()


# =========================================================================== #
# Storyboard renderer — style presets + per-kind, video-native templates.
# =========================================================================== #
# Bump when CSS/templates change so cached previews invalidate (see web editor).
RENDERER_VERSION = 2

_THEMES = {
    "dark": dict(bg="#0d0e12", bg2="#171a22", text="#f4f5f9", dim="#a6abbd",
                 faint="#6b7186", line="#2a2f3c", code_bg="#0f1219"),
    "light": dict(bg="#faf9f5", bg2="#ffffff", text="#1c1b19", dim="#5b594f",
                  faint="#9a978c", line="#e6e2d8", code_bg="#f3f1ea"),
}
_PRESETS = {
    "dark_keynote": dict(accent="#7c5cff", accent2="#a78bff"),
    "editorial_light": dict(accent="#b5532a", accent2="#d2693a"),
    "minimal_statement": dict(accent="#e0573e", accent2="#f07a4e"),
}

_SLIDE_CSS = """
* { box-sizing: border-box; margin: 0; }
html,body { width:1920px; height:1080px; }
body {
  background: var(--bg); color: var(--text);
  font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
  display:flex; align-items:center; justify-content:center;
  -webkit-font-smoothing: antialiased;
}
#stage { width: 1920px; height: 1080px; padding: 120px 140px;
  display:flex; flex-direction:column; justify-content:center;
  transform-origin: center center; }
.eyebrow { font-size: 30px; font-weight:700; letter-spacing:.14em;
  text-transform:uppercase; color: var(--accent); margin-bottom: 30px; }
.headline { font-weight:750; line-height:1.04; letter-spacing:-.02em;
  font-size: 96px; color: var(--text); }
.subtitle { font-size: 40px; color: var(--dim); margin-top: 34px; line-height:1.3; }
.em { color: var(--accent); font-weight:800; }
ul.points { list-style:none; margin-top: 64px; display:flex; flex-direction:column; gap: 34px; }
li.pt { display:flex; align-items:flex-start; gap: 28px; font-size: 52px;
  line-height:1.22; color: var(--text); transition: opacity .25s; }
li.pt .marker { flex:0 0 auto; margin-top:.42em; width:18px; height:18px;
  border-radius:50%; background: var(--accent); }
li.pt.hidden { opacity:0; }
li.pt.dim { opacity:.34; }
li.pt.dim .marker { background: var(--faint); }

/* diagram */
.diagram .head { margin-bottom: 48px; }
.headline.sm { font-size: 60px; }
.mermaid { display:flex; justify-content:center; align-items:center; }
.mermaid svg { max-width: 1640px; max-height: 720px; height:auto; }

/* table -> cards */
.cards { display:flex; gap: 32px; margin-top: 64px; flex-wrap:wrap; }
.card { flex:1 1 0; min-width: 320px; background: var(--bg2);
  border:1px solid var(--line); border-radius: 22px; padding: 40px 38px; transition: opacity .25s; }
.card.hidden { opacity:0; } .card.dim { opacity:.34; }
.card .k { font-size: 30px; font-weight:700; letter-spacing:.05em;
  text-transform:uppercase; color: var(--accent); margin-bottom: 20px; }
.card .v { font-size: 44px; line-height:1.2; color: var(--text); }

/* code */
pre.code { margin-top: 56px; background: var(--code-bg); border:1px solid var(--line);
  border-radius: 20px; padding: 44px 48px; font-size: 38px; line-height:1.5;
  font-family: ui-monospace,"SF Mono",Menlo,monospace; color: var(--text); overflow:hidden; }
pre.code .ln { white-space:pre; transition: opacity .25s; }
pre.code .ln.hidden { opacity:0; } pre.code .ln.dim { opacity:.4; }

/* title */
.title .headline { font-size: 116px; }
.title.center { text-align:center; align-items:center; }

/* statement */
.statement { }
.statement .idx { font-size: 52px; font-weight:800; color: var(--accent);
  letter-spacing:.05em; margin-bottom: 30px; }
.statement .big { font-size: 104px; font-weight:780; line-height:1.05; }
.statement .ctx { font-size: 34px; color: var(--dim); margin-top: 34px; }

/* image slide (full-bleed) */
.image-slide { position: fixed; inset: 0; background-size: cover; background-position: center;
  display: flex; flex-direction: column; justify-content: flex-end; }
.image-slide .scrim { position: absolute; inset: 0;
  background: linear-gradient(to top, rgba(0,0,0,.78), rgba(0,0,0,.15) 55%, transparent); }
.image-slide .cap { position: relative; padding: 90px 130px; }
.image-slide .cap .eyebrow { color: #fff; }
.image-slide .cap .headline { color: #fff; }
.image-slide.placeholder { position: static; align-items: center; justify-content: center;
  text-align: center; color: var(--text-faint); background: var(--bg2); border: 2px dashed var(--line); }
.image-slide .ph-sub { font-size: 28px; margin-top: 12px; }

/* preset tweaks */
.preset-editorial_light .eyebrow { border-left: 6px solid var(--accent); padding-left: 22px; }
.preset-minimal_statement #stage { align-items:center; text-align:center; }
.preset-minimal_statement .headline { font-size: 120px; }
.preset-minimal_statement ul.points { align-items:center; }
.preset-minimal_statement li.pt { font-size: 60px; }
.preset-dark_keynote body, .theme-dark.preset-dark_keynote body { }
"""


def _vars(preset: str, theme: str) -> str:
    t = _THEMES.get(theme, _THEMES["dark"])
    p = _PRESETS.get(preset, _PRESETS["dark_keynote"])
    bg2grad = ""
    if preset == "dark_keynote" and theme == "dark":
        bg2grad = (f"body{{background:radial-gradient(1400px 700px at 78% -12%,"
                   f"{p['accent']}22,transparent 60%),{t['bg']};}}")
    return (f":root{{--accent:{p['accent']};--accent2:{p['accent2']};"
            f"--bg:{t['bg']};--bg2:{t['bg2']};--text:{t['text']};--dim:{t['dim']};"
            f"--faint:{t['faint']};--line:{t['line']};--code-bg:{t['code_bg']};}}{bg2grad}")


def _emphasize(text: str, emphasis) -> str:
    safe = html.escape(text or "")
    for e in emphasis or []:
        if not e:
            continue
        es = html.escape(e)
        safe = safe.replace(es, f'<span class="em">{es}</span>', 1)
    return safe


def _eyebrow(text: str) -> str:
    return f'<div class="eyebrow">{html.escape(text)}</div>' if text else ""


def _points_inner(slide, reveal) -> str:
    visible = reveal.get("visible") if reveal else None
    active = reveal.get("active") if reveal else None
    lis = []
    for i, p in enumerate(slide.points):
        cls = "pt"
        if visible is not None and i >= visible:
            cls += " hidden"
        elif active is not None and i != active:
            cls += " dim"
        lis.append(f'<li class="{cls}"><span class="marker"></span>'
                   f'<span class="txt">{_emphasize(p.get("text",""), p.get("emphasis"))}</span></li>')
    return (f'<div class="points-slide">{_eyebrow(slide.kicker)}'
            f'<h1 class="headline">{html.escape(slide.headline)}</h1>'
            f'<ul class="points">{"".join(lis)}</ul></div>')


def _statement_inner(slide, reveal) -> str:
    active = (reveal or {}).get("active", 0)
    pts = slide.points or [{"text": slide.headline}]
    active = max(0, min(active, len(pts) - 1))
    idx = f'<div class="idx">{active+1:02d}</div>'
    ctx = f'<div class="ctx">{html.escape(slide.headline)}</div>' if slide.headline else ""
    return (f'<div class="statement">{idx}'
            f'<div class="big">{_emphasize(pts[active].get("text",""), pts[active].get("emphasis"))}</div>{ctx}</div>')


def _title_inner(slide) -> str:
    sub = slide.points[0]["text"] if slide.points else ""
    center = "center"
    return (f'<div class="title {center}">{_eyebrow(slide.kicker)}'
            f'<h1 class="headline">{html.escape(slide.headline)}</h1>'
            + (f'<p class="subtitle">{html.escape(sub)}</p>' if sub else "") + "</div>")


def _table_inner(slide, reveal) -> str:
    cards = (slide.table or {}).get("cards", [])
    visible = reveal.get("visible") if reveal else None
    cs = []
    for i, c in enumerate(cards):
        cls = "card" + (" hidden" if visible is not None and i >= visible else "")
        cs.append(f'<div class="{cls}"><div class="k">{html.escape(c.get("label",""))}</div>'
                  f'<div class="v">{html.escape(c.get("value",""))}</div></div>')
    return (f'<div class="table-slide"><h1 class="headline sm">{html.escape(slide.headline)}</h1>'
            f'<div class="cards">{"".join(cs)}</div></div>')


def _code_inner(slide, reveal) -> str:
    lines = (slide.code or {}).get("lines", [])
    visible = reveal.get("visible") if reveal else None
    rows = []
    for i, ln in enumerate(lines):
        cls = "ln" + (" hidden" if visible is not None and i >= visible else "")
        rows.append(f'<span class="{cls}">{html.escape(ln)}</span>')
    body = "\n".join(rows)
    return (f'<div class="code-slide"><h1 class="headline sm">{html.escape(slide.headline)}</h1>'
            f'<pre class="code"><code>{body}</code></pre></div>')


def _diagram_inner(slide) -> str:
    mer = (slide.diagram or {}).get("mermaid", "")
    return (f'<div class="diagram"><div class="head">{_eyebrow(slide.kicker)}'
            f'<h1 class="headline sm">{html.escape(slide.headline)}</h1></div>'
            f'<pre class="mermaid">{html.escape(mer)}</pre></div>')


def _image_inner(slide) -> str:
    img = slide.image or {}
    p = img.get("path") or ""
    uri = None
    if p and os.path.isabs(p) and os.path.exists(p):
        try:
            with open(p, "rb") as f:
                uri = "data:image/png;base64," + base64.b64encode(f.read()).decode()
        except Exception:
            uri = None
    if not uri:
        return ('<div class="image-slide placeholder"><div>'
                f'<div class="headline sm">{html.escape(slide.headline or "Image")}</div>'
                '<div class="ph-sub">image not generated yet</div></div></div>')
    cap = ""
    if slide.headline or slide.kicker:
        cap = (f'<div class="scrim"></div><div class="cap">{_eyebrow(slide.kicker)}'
               f'<h1 class="headline">{html.escape(slide.headline)}</h1></div>')
    return f'<div class="image-slide" style="background-image:url({uri})">{cap}</div>'


def _gif_ready(slide) -> bool:
    g = getattr(slide, "gif", None) or {}
    p = g.get("path") or ""
    return bool(p and os.path.isabs(p) and os.path.exists(p))


def _gif_inner(slide) -> str:
    """Static render of a gif slide (first frame as background) — for previews/stills.
    In the final video the gif is composited live by ffmpeg (see _gif_overlay_html)."""
    g = slide.gif or {}
    uri = None
    if _gif_ready(slide):
        try:
            with open(g["path"], "rb") as f:
                uri = "data:image/gif;base64," + base64.b64encode(f.read()).decode()
        except Exception:
            uri = None
    if not uri:
        q = (g.get("query") or "").strip()
        sub = f'“{html.escape(q)}” — gif not fetched yet' if q else "no gif yet"
        return ('<div class="image-slide placeholder"><div>'
                f'<div class="headline sm">{html.escape(slide.headline or "GIF")}</div>'
                f'<div class="ph-sub">{sub}</div></div></div>')
    cap = ""
    if slide.headline or slide.kicker:
        cap = (f'<div class="scrim"></div><div class="cap">{_eyebrow(slide.kicker)}'
               f'<h1 class="headline">{html.escape(slide.headline)}</h1></div>')
    return f'<div class="image-slide" style="background-image:url({uri})">{cap}</div>'


def _gif_overlay_html(slide, style: dict) -> str:
    """A transparent page holding only the caption chrome (scrim + headline).
    Screenshot with omit_background=True, then ffmpeg overlays it on the looping gif."""
    preset = style.get("preset", "dark_keynote")
    theme = style.get("theme", "dark")
    cap = (f'<div class="scrim"></div><div class="cap">{_eyebrow(slide.kicker)}'
           f'<h1 class="headline">{html.escape(slide.headline)}</h1></div>')
    return ("<!doctype html><html><head><meta charset='utf-8'><style>"
            "html,body{background:transparent !important;margin:0}"
            f"{_vars(preset, theme)}{_SLIDE_CSS}</style></head>"
            f"<body class='preset-{preset} theme-{theme}'>"
            f"<div class='image-slide'>{cap}</div></body></html>")


def _slide_inner(slide, reveal=None) -> str:
    k = slide.kind
    if k == "image":
        return _image_inner(slide)
    if k == "gif":
        return _gif_inner(slide)
    if k == "title":
        return _title_inner(slide)
    if k == "statement":
        return _statement_inner(slide, reveal)
    if k == "table":
        return _table_inner(slide, reveal)
    if k == "code":
        return _code_inner(slide, reveal)
    if k == "diagram":
        return _diagram_inner(slide)
    return _points_inner(slide, reveal)


def _slide_page_html(slide, style: dict, reveal=None) -> str:
    preset = style.get("preset", "dark_keynote")
    theme = style.get("theme", "dark")
    mermaid_script = ""
    if slide.kind == "diagram":
        mtheme = "dark" if theme == "dark" else "neutral"
        mermaid_script = (
            f'<script src="{MERMAID_CDN}"></script>'
            '<script>mermaid.initialize({startOnLoad:true,theme:"' + mtheme + '",'
            'flowchart:{useMaxWidth:true},'
            'themeVariables:{fontFamily:"-apple-system, system-ui, sans-serif",fontSize:"22px"}});'
            '</script>')
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<style>{_vars(preset, theme)}{_SLIDE_CSS}</style></head>"
            f"<body class='preset-{preset} theme-{theme}'>"
            f"<div id='stage'>{_slide_inner(slide, reveal)}</div>{mermaid_script}</body></html>")


def _shoot(page, html_str: str, is_diagram: bool, out_path: str) -> None:
    page.set_content(html_str, wait_until="networkidle")
    if is_diagram:
        try:
            page.wait_for_selector(".mermaid svg", timeout=15000)
        except Exception:
            pass
    page.evaluate(_FIT_JS)
    page.wait_for_timeout(120)
    page.screenshot(path=out_path)


def render_slide_preview(slide, style: dict, out_path: str) -> str:
    """Render one slide (all content visible) to a PNG — used by the web editor."""
    from playwright.sync_api import sync_playwright
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        _shoot(page, _slide_page_html(slide, style), slide.kind == "diagram", out_path)
        browser.close()
    return out_path


# Hide everything except revealed nodes (matched by label, then id) and the
# edges whose endpoints are both revealed; accent the focus node. Robust to
# Mermaid's id scheme: nodes match on label text first.
_DIAGRAM_REVEAL_JS = """(d) => {
  const svg = document.querySelector('.mermaid svg'); if (!svg) return;
  const vis = new Set(d.visible || []);
  const label2id = {};
  for (const k in (d.id2label || {})) label2id[(d.id2label[k] || '').trim()] = k;
  const accent = getComputedStyle(document.body).getPropertyValue('--accent') || '#7c5cff';
  svg.querySelectorAll('g.node').forEach(g => {
    const t = (g.textContent || '').trim();
    let id = label2id[t];
    if (!id) { const m = (g.id || '').match(/flowchart-(.+?)-\\d+/); if (m) id = m[1]; }
    const on = id && vis.has(id);
    g.style.transition = 'opacity .2s';
    g.style.opacity = on ? '1' : '0.07';
    if (on && id === d.focus) {
      const sh = g.querySelector('rect,polygon,circle,path,ellipse');
      if (sh) { sh.style.stroke = accent; sh.style.strokeWidth = '3px'; }
    }
  });
  const paths = [...svg.querySelectorAll('g.edgePaths > path')];
  const labels = [...svg.querySelectorAll('g.edgeLabels .edgeLabel')];
  (d.edges || []).forEach((e, i) => {
    const on = vis.has(e[0]) && vis.has(e[1]);
    if (paths[i]) paths[i].style.opacity = on ? '1' : '0.07';
    if (labels[i]) labels[i].style.opacity = on ? '1' : '0.07';
  });
}"""


def _diagram_reveal_payload(beat):
    ir = (getattr(beat.slide, "diagram", None) or {}).get("ir") or {}
    return {
        "visible": (beat.reveal or {}).get("nodes", []),
        "focus": (beat.reveal or {}).get("focus"),
        "id2label": {n["id"]: n["label"] for n in ir.get("nodes", [])},
        "edges": [[e["from"], e["to"]] for e in ir.get("edges", [])],
    }


def render_beats(beats, out_dir: str, progress=None) -> None:
    """Render each Beat to a PNG at its reveal state; sets beat.image_path."""
    from playwright.sync_api import sync_playwright
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    total = len(beats)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        for i, b in enumerate(beats):
            # gif slides: don't flatten to a still — keep the source gif for ffmpeg
            # to loop, and render only the caption chrome to a transparent overlay.
            if b.kind == "gif" and _gif_ready(b.slide):
                b.gif_path = (b.slide.gif or {}).get("path") or ""
                if b.slide.headline or b.slide.kicker:
                    opath = str(out / f"beat_{b.index:03d}_overlay.png")
                    page.set_content(_gif_overlay_html(b.slide, b.style),
                                     wait_until="networkidle")
                    page.wait_for_timeout(80)
                    page.screenshot(path=opath, omit_background=True)
                    b.overlay_path = opath
                if progress:
                    progress(i + 1, total)
                continue
            path = str(out / f"beat_{b.index:03d}.png")
            page.set_content(_slide_page_html(b.slide, b.style, b.reveal),
                             wait_until="networkidle")
            if b.kind == "diagram":
                try:
                    page.wait_for_selector(".mermaid svg", timeout=15000)
                except Exception:
                    pass
                if b.reveal and b.reveal.get("nodes") is not None:
                    page.evaluate(_DIAGRAM_REVEAL_JS, _diagram_reveal_payload(b))
            page.evaluate(_FIT_JS)
            page.wait_for_timeout(120)
            page.screenshot(path=path)
            b.image_path = path
            if progress:
                progress(i + 1, total)
        browser.close()


def render_slides_static(slides, style: dict, out_dir: str, progress=None) -> list[str]:
    """Render each slide (fully revealed) to a still — for inspection."""
    from playwright.sync_api import sync_playwright
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        for i, s in enumerate(slides):
            path = str(out / f"{s.id}.png")
            _shoot(page, _slide_page_html(s, style), s.kind == "diagram", path)
            paths.append(path)
            if progress:
                progress(i + 1, len(slides))
        browser.close()
    return paths
