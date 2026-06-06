"""Render each Scene to a 1920x1080 PNG using headless Chromium (Playwright).

Why a browser instead of a dedicated diagram tool: Mermaid is a JS library, so
rendering it in a real browser is the highest-fidelity option and it gives us
prose, tables, and diagrams through one styling path. Each scene becomes one
HTML page; we screenshot the viewport. Content that would overflow 1080p is
scaled down to fit.

Requires:  pip install playwright markdown  &&  playwright install chromium
"""

from __future__ import annotations

import html
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
