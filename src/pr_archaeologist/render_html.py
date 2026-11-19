"""Static HTML knowledge graph: one file, no network requests, no build step.

The page is the template in graph_template.html with the graph payload inlined as JSON.
Commit messages are untrusted text, so the payload is embedded in a non-executing
<script type="application/json"> block with `<`, `>` and `&` escaped, and the page's
JavaScript HTML-escapes every value before it reaches the DOM.
"""

from __future__ import annotations

import html
import json
import os
from typing import Dict

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "graph_template.html")


def embed_json(payload: Dict) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (
        text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    )


def render(graph: Dict) -> str:
    with open(TEMPLATE, "r", encoding="utf-8") as fh:
        template = fh.read()
    title = html.escape(f"{graph['meta']['repo']} — codebase archaeology")
    return template.replace("__TITLE__", title).replace("__ARCHAEOLOGY_DATA__", embed_json(graph))
