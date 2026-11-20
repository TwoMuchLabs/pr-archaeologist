"""Local Markdown and ADR reader.

Finds decision records and design notes in the repo, extracts their status and
Context / Decision / Consequences sections, and records which paths, commits and PRs
they mention so the model can tie a document to the code it explains.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Callable, Dict, List, Optional, Tuple

from .gitio import git

MAX_DOC_BYTES = 300 * 1024
ADR_PATH_RE = re.compile(r"(^|/)(adrs?|decisions?|architecture[-_]decisions?|rfcs?|design[-_]docs?)/", re.I)
ADR_NAME_RE = re.compile(r"^(adr[-_]?)?\d{3,5}[-_].+\.(md|markdown)$", re.I)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
STATUS_LINE_RE = re.compile(r"^\**\s*status\s*\**\s*[:：]\s*\**\s*(.+?)\**\s*$", re.I)
DATE_LINE_RE = re.compile(r"^\**\s*date\s*\**\s*[:：]\s*\**\s*(\d{4}-\d{2}(?:-\d{2})?)", re.I)
ANY_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
PATHISH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.\w{1,8}|[\w-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|kt|rb|php|cs|c|h|cpp|sql|sh|yml|yaml|toml))\b")
SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\b")
PR_RE = re.compile(r"(?:#|pull/|PR[ -]?)(\d{1,6})\b")
WANTED = {
    "context": ("context", "background", "problem", "motivation", "why"),
    "decision": ("decision", "resolution", "solution", "what we decided"),
    "consequences": ("consequence", "trade-off", "tradeoff", "implication", "risks", "drawback"),
    # Sections used by the notes the overnight agent writes (agent/nightly-prompt.md).
    "invariants": ("invariant", "must stay true", "must hold"),
    "traps": ("known trap", "trap", "pitfall", "gotcha"),
}
BULLET_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.*)$")


def _bullets(lines: List[str], limit: int = 8) -> List[str]:
    """Bullet items of a section, with wrapped continuation lines folded back in."""
    items: List[str] = []
    for line in lines:
        m = BULLET_RE.match(line)
        if m:
            items.append(m.group(1))
        elif items:
            items[-1] += " " + line
        else:
            items.append(line)
    return [_clip(item, 260) for item in items[:limit]]


def _clip(text: str, limit: int = 420) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


FRONTMATTER_KEY_RE = re.compile(r"^(title|status|date)\s*:\s*(.+)$", re.I)


def _frontmatter(lines: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """A leading `---` block: the title, status and date it sets, and the lines after it.

    An unclosed block swallows the whole document, as before.
    """
    if not lines or lines[0].strip() != "---":
        return {}, lines
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), len(lines))
    meta: Dict[str, str] = {}
    for line in lines[:close]:
        m = FRONTMATTER_KEY_RE.match(line.rstrip())
        if m:
            key, value = m.group(1).lower(), m.group(2).strip().strip("\"'")
            meta[key] = value[:10] if key == "date" else value
    return meta, lines[close + 1:]


def _section_of(heading: str) -> Optional[str]:
    """Which of the sections we keep a heading opens, if any."""
    low = heading.lower()
    if low.startswith("status"):
        return "status"
    return next(
        (key for key, words in WANTED.items() if any(low.startswith(w) or f" {w}" in low for w in words)),
        None,
    )


def _scan_body(lines: List[str], meta: Dict[str, str]) -> Tuple[Optional[str], Optional[str], Optional[str], Dict[str, List[str]]]:
    """Walk the document outside code fences: title, status, date and the text of each kept section."""
    title, status, date = meta.get("title"), meta.get("status"), meta.get("date")
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    in_code = False
    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = HEADING_RE.match(line)
        if m:
            heading = m.group(2).strip()
            if title is None and len(m.group(1)) == 1:
                title = heading
            current = _section_of(heading)
            continue
        if status is None:
            m = STATUS_LINE_RE.match(line.strip())
            if m:
                status = m.group(1)
                continue
        if date is None:
            m = DATE_LINE_RE.match(line.strip())
            if m:
                date = m.group(1)
                continue
        if current and line.strip():
            sections.setdefault(current, []).append(line.strip())
    return title, status, date, sections


def _is_adr(path: str, name: str, sections: Dict[str, List[str]], status: Optional[str]) -> bool:
    has_shape = "decision" in sections and ("context" in sections or status)
    if name.lower() in ("readme.md", "index.md", "template.md") and not has_shape:
        return False
    return bool(ADR_PATH_RE.search(path) or ADR_NAME_RE.match(name) or has_shape)


def parse(path: str, text: str) -> Dict:
    lines = text.splitlines()
    meta, body = _frontmatter(lines)
    title, status, date, sections = _scan_body(body, meta)

    if status is None and sections.get("status"):
        status = sections["status"][0]
    if status:
        status = re.sub(r"[*_`]", "", status).strip().split(".")[0][:40]
    if date is None:
        m = ANY_DATE_RE.search("\n".join(lines[:15]))
        date = m.group(1) if m else None

    name = path.rsplit("/", 1)[-1]
    is_adr = _is_adr(path, name, sections, status)
    number = re.match(r"^(?:adr[-_]?)?(\d{3,5})", name, re.I)
    if title and is_adr:
        title = re.sub(r"^(?:adr[-_ ]?)?\d{1,5}[.:)\-]\s+", "", title, flags=re.I)

    return {
        "title": title or name,
        "kind": "adr" if is_adr else "doc",
        "number": number.group(1) if (is_adr and number) else None,
        "status": status,
        "date": date,
        "context": _clip(" ".join(sections.get("context", []))),
        "decision": _clip(" ".join(sections.get("decision", []))),
        "consequences": _clip(" ".join(sections.get("consequences", []))),
        "invariants": _bullets(sections.get("invariants", [])),
        "traps": _bullets(sections.get("traps", [])),
        "paths": sorted(set(PATHISH_RE.findall(text)))[:200],
        "shas": sorted(set(SHA_RE.findall(text)))[:100],
        "prs": sorted({int(n) for n in PR_RE.findall(text)})[:100],
    }


def update(repo: str, state: Dict, ignored: Callable[[str], bool], skip: List[str]) -> Dict:
    """Refresh state['docs']; only files whose content hash changed are re-parsed."""
    listing = git(
        repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
        "--", "*.md", "*.markdown", "*.MD", check=False,
    )
    paths = sorted({p for p in listing.split("\0") if p})
    skip_set = set(skip)
    docs = state["docs"]
    seen, parsed = set(), 0
    for path in paths:
        if path in skip_set or ignored(path):
            continue
        full = os.path.join(repo, path)
        try:
            if os.path.getsize(full) > MAX_DOC_BYTES:
                continue
            with open(full, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        digest = hashlib.sha1(raw).hexdigest()
        seen.add(path)
        if docs.get(path, {}).get("hash") == digest:
            continue
        record = parse(path, raw.decode("utf-8", "replace"))
        record["hash"] = digest
        docs[path] = record
        parsed += 1
    for stale in [p for p in docs if p not in seen]:
        del docs[stale]
    return {"parsed": parsed, "cached": len(seen) - parsed}
