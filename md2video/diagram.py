"""Deterministically parse a Mermaid flowchart into a small graph IR.

We parse the *source* (preserving the original node IDs) so the renderer can
target Mermaid's SVG by those IDs — stable, and without altering the diagram.
Only flowcharts (`flowchart`/`graph`) are handled; anything else returns None
and the caller shows the diagram whole.
"""

from __future__ import annotations

import re

# Edge connectors, longest first so "-->" wins over "--".
_EDGE = re.compile(r"(<-->|-\.->|-\.-|==>|===|-->|---|->|--)(?:\|([^|]*)\|)?")
_NODE = re.compile(r'^([A-Za-z0-9_]+)\s*(?:[\[\(\{>]+\s*"?(.*?)"?\s*[\]\)\}]+)?$')


def is_flowchart(src: str) -> bool:
    head = (src or "").strip().splitlines()
    return bool(head) and re.match(r"^(flowchart|graph)\b", head[0].strip()) is not None


def parse_mermaid(src: str) -> dict | None:
    """Return {'nodes':[{id,label}], 'edges':[{id,from,to,label}]} or None."""
    if not is_flowchart(src):
        return None
    lines = [l.strip() for l in src.splitlines() if l.strip()]
    lines = lines[1:]  # drop the `flowchart LR` header

    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def node(token: str):
        token = token.strip()
        m = _NODE.match(token)
        if not m:
            return None
        nid = m.group(1)
        label = (m.group(2) or "").strip() or nid
        if nid not in nodes:
            nodes[nid] = {"id": nid, "label": label}
        elif m.group(2):
            nodes[nid]["label"] = label
        return nid

    for line in lines:
        if line.startswith(("subgraph", "end", "%%", "classDef", "class ", "style ", "linkStyle")):
            continue
        parts, labels, last = [], [], 0
        for mm in _EDGE.finditer(line):
            parts.append(line[last:mm.start()])
            labels.append((mm.group(2) or "").strip())
            last = mm.end()
        parts.append(line[last:])
        if len(parts) < 2:
            node(parts[0])
            continue
        ids = [node(p) for p in parts]
        for i, lab in enumerate(labels):
            a, b = ids[i], ids[i + 1]
            if a and b:
                edges.append({"id": f"{a}__{b}", "from": a, "to": b, "label": lab})

    if not nodes:
        return None
    return {"nodes": list(nodes.values()), "edges": edges}
