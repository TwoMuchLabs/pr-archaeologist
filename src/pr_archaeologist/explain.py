"""`pr-archaeologist explain <path>`: a concise timeline of why a module exists and what bit people."""

from __future__ import annotations

import difflib
import os
from typing import Dict, List, Optional, Tuple

MARK = {"fix": "✖", "security": "▲", "revert": "↺", "design": "■", "perf": "»", "feature": "+"}
COLORS = {"red": "31", "yellow": "33", "green": "32", "dim": "2", "bold": "1", "cyan": "36"}
LEVEL_COLOR = {"fragile": "red", "caution": "yellow", "stable": "green"}


class Painter:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        return f"\033[{';'.join(COLORS[s] for s in styles)}m{text}\033[0m"


def _candidates(query: str, repo: str, cwd: str) -> List[str]:
    """Ways to read what the user typed as a repo-relative path, most likely first."""
    candidates = [query.replace("\\", "/").lstrip("./") if not os.path.isabs(query) else query]
    absolute = query if os.path.isabs(query) else os.path.join(cwd, query)
    rel = os.path.relpath(os.path.realpath(absolute), os.path.realpath(repo)).replace(os.sep, "/")
    if not rel.startswith(".."):
        candidates.insert(0, rel)
    return candidates


def _directory(files: Dict[str, Dict], candidates: List[str]) -> Optional[List[Dict]]:
    """Live files under the first candidate that is a directory ("." or "" means all of them)."""
    for cand in candidates:
        prefix = cand.rstrip("/") + "/"
        inside = [f for p, f in files.items() if p.startswith(prefix) and f["status"] == "live"]
        if cand in (".", "") or inside:
            inside = inside or [f for f in files.values() if f["status"] == "live"]
            return sorted(inside, key=lambda f: (-f["score"], f["path"]))
    return None


def resolve(report: Dict, query: str, repo: str, cwd: str) -> Tuple[str, object]:
    """Map what the user typed to ('file', report) | ('dir', [reports]) | ('missing', suggestions)."""
    files = report["files"]
    candidates = _candidates(query, repo, cwd)
    previous = {old: f["path"] for f in files.values() for old in f["previous"]}
    for cand in candidates:
        cand = cand.rstrip("/")
        if cand in files:
            return "file", files[cand]
        if cand in previous:
            return "file", files[previous[cand]]
    inside = _directory(files, candidates)
    if inside is not None:
        return "dir", inside
    tail = [f for p, f in files.items() if p.endswith("/" + candidates[-1])]
    if len(tail) == 1:
        return "file", tail[0]
    names = list(files) + list(previous)
    return "missing", difflib.get_close_matches(candidates[0], names, n=5, cutoff=0.5)


def _wrap(text: str, indent: str, width: int = 96) -> List[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(indent) + len(line) + 1 + len(word) > width:
            lines.append(indent + line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(indent + line)
    return lines


def _advice(f: Dict) -> List[str]:
    tips = []
    reverted = [p for p in f["pitfalls"] if p["kind"] == "reverted"]
    if reverted:
        last = reverted[-1]
        tips.append(f"An approach here was already tried and reverted ({last['ref']}); do not reintroduce it without new evidence.")
    adrs = [d for d in f["docs"] if d["kind"] == "adr"]
    if adrs:
        tips.append(f"Honour {', '.join(d['label'] for d in adrs[:3])}, or supersede it explicitly in your PR.")
    strong = [c for c in f["coupled"] if c["strong"]]
    if strong:
        tips.append(f"Open {', '.join(c['path'] for c in strong[:3])} as well: they change together.")
    fixes = [p for p in f["pitfalls"] if p["kind"] in ("fix", "security") and not p["ref"].startswith(("PR #", "#"))]
    if fixes:
        tips.append(f"Keep the behaviour from the last fix intact ({fixes[-1]['ref']}: {fixes[-1]['text'].split(' — ')[0]}).")
    if f["level"] == "stable" and not tips and not f.get("prose"):
        tips.append("No recorded fixes, reverts or decisions: ordinary care applies.")
    return tips


def _counted(n: int, word: str, plural: str = "s") -> str:
    return f"{n} {word}{plural if n != 1 else ''}"


def _header(f: Dict, paint: Painter) -> List[str]:
    """Path, level badge and counts; then former names and how a deleted file went."""
    out = [paint(f["path"], "bold") + (paint("  (deleted)", "dim") if f["status"] == "deleted" else "")]
    stats = [f"{f['commits']} commits"]
    if f["fixes"]:
        stats.append(_counted(f["fixes"], "fix", "es"))
    if f["security"]:
        stats.append(_counted(f["security"], "security change"))
    if f["reverts"]:
        stats.append(_counted(f["reverts"], "revert"))
    stats.append(_counted(f["author_count"], "author"))
    stats.append(f"{f['first']} → {f['last']}")
    badge = paint(f"{f['level'].upper()} {f['score']}/100", LEVEL_COLOR[f["level"]], "bold")
    if f.get("prose"):
        badge = paint("DOCUMENT · not scored", "dim")
    elif f.get("rank") and f["status"] == "live":
        badge += paint(f" · rank {f['rank']} of {f['ranked_of']}", "dim")
    out.append(f"{badge} · " + " · ".join(stats))
    if f["previous"]:
        out.append(paint("formerly " + ", ".join(f["previous"]), "dim"))
    if f["status"] == "deleted" and f["deleted"]:
        gone = f["deleted"]
        out.append(f"Removed {gone['date']} {gone['short']}: {gone['title']}")
        for why in ([gone["lead"]] if gone.get("lead") else gone["why"][:1]):
            out += _wrap(why, "    ")
    return out


def _why_section(f: Dict, paint: Painter) -> List[str]:
    out = ["", paint("WHY IT EXISTS", "cyan", "bold")]
    for item in f["intent"]:
        out.append(f"  {item['date']} {paint(item['ref'], 'dim')}  {item['text']} — {item['author']}")
        for why in item["why"]:
            out += _wrap(why, "      ")
    for doc in f["docs"]:
        status = f" ({doc['status']})" if doc.get("status") else ""
        out.append(f"  {doc['label']}: {doc['title']}{status}  {paint(doc['path'], 'dim')}")
        if doc.get("decision"):
            out += _wrap("Decision: " + doc["decision"], "      ")
        for rule in doc.get("invariants", []):
            out += _wrap("Must hold: " + rule, "      ")
    if not f["intent"] and not f["docs"]:
        out.append("  (no history recorded)")
    return out


def _timeline_row(e: Dict, paint: Painter) -> str:
    mark = "◆" if e["note"] == "born" else MARK.get(e["kind"], "·")
    label = e["kind"]
    extra = paint("  [later reverted]", "red") if e["reverted_by"] else ""
    if e["note"] == "born":
        label = "born"
    elif e["note"] == "deleted":
        label = "deleted"
    elif e["note"]:
        label = "renamed"
        extra = paint(f"  [{e['note']}]", "dim") + extra
    refs = "".join(f" #{n}" for n in e["prs"])
    tone = ("red",) if e["kind"] in ("fix", "security", "revert") else ()
    return f"  {e['date']} {paint(mark, *tone)} {paint(e['short'], 'dim')} {label:<8} {e['title']}{refs}{extra}"


def _timeline_section(f: Dict, paint: Painter, show_all: bool, limit: int) -> List[str]:
    events = f["timeline"]
    landmark = [
        e for i, e in enumerate(events)
        if show_all or i == 0 or e["note"] or e["kind"] in MARK and e["kind"] != "feature"
        or e["reverted_by"] or "design" in e["tags"]
    ]
    hidden = len(events) - len(landmark)
    shown = landmark if show_all else landmark[:1] + landmark[1:][-(limit - 1):]
    skipped = len(landmark) - len(shown)
    title = "TIMELINE"
    if hidden or skipped:
        title += f"  ({_counted(hidden + skipped, 'routine or older change')} hidden, --all to show)"
    return ["", paint(title, "cyan", "bold")] + [_timeline_row(e, paint) for e in shown]


def _pitfall_section(f: Dict, paint: Painter, show_all: bool) -> List[str]:
    if not f["pitfalls"]:
        return []
    out = ["", paint("PAST PITFALLS", "cyan", "bold")]
    pits = f["pitfalls"] if show_all else f["pitfalls"][-8:]
    if len(pits) < len(f["pitfalls"]):
        out.append(paint(f"  ({len(f['pitfalls']) - len(pits)} older hidden, --all to show)", "dim"))
    for p in pits:
        head = f"  {paint('!', 'red', 'bold')} [{p['kind']}] " + " ".join(x for x in (p["date"], p["ref"]) if x)
        out.append(head)
        out += _wrap(p["text"], "      ")
    return out


def _coupled_section(f: Dict, paint: Painter) -> List[str]:
    if not f["coupled"]:
        return []
    rows = [f"  {c['path']}  ({c['count']} of {c['of']} commits)" for c in f["coupled"]]
    return ["", paint("USUALLY CHANGES WITH", "cyan", "bold")] + rows


def _functions_section(f: Dict, paint: Painter, show_all: bool) -> List[str]:
    risky = [s for s in f["symbols"] if s["fixes"] and not s["container"]]
    if not risky:
        return []
    out = ["", paint("FUNCTIONS WITH A HISTORY", "cyan", "bold")]
    for s in risky[: (None if show_all else 8)]:
        out.append(f"  {paint(s['name'], 'bold')}  L{s['start']}–{s['end']}  ·  {_counted(len(s['fixes']), 'risky change')}")
        for e in s["fixes"][-3:]:
            out.append(f"      {e['date']} {paint(e['short'], 'dim')} {e['title']}")
    return out


def _advice_section(f: Dict, paint: Painter) -> List[str]:
    tips = _advice(f)
    if not tips or f["status"] != "live":
        return []
    out = ["", paint("BEFORE YOU EDIT", "cyan", "bold")]
    for tip in tips:
        lines = _wrap(tip, "    ")
        lines[0] = "  -" + lines[0][3:]
        out += lines
    return out


def render_file(f: Dict, color: bool = False, show_all: bool = False, limit: int = 12) -> str:
    paint = Painter(color)
    out = (
        _header(f, paint) + _why_section(f, paint) + _timeline_section(f, paint, show_all, limit)
        + _pitfall_section(f, paint, show_all) + _coupled_section(f, paint)
        + _functions_section(f, paint, show_all) + _advice_section(f, paint)
    )
    return "\n".join(out) + "\n"


def render_function(f: Dict, name: str, color: bool = False) -> Optional[str]:
    paint = Painter(color)
    matches = [s for s in f["symbols"] if s["name"] == name or s["name"].rsplit(".", 1)[-1] == name]
    if not matches:
        return None
    out: List[str] = []
    for s in matches:
        out.append(paint(f"{f['path']}::{s['name']}", "bold") + f"  ({s['kind']}, L{s['start']}–{s['end']})")
        o, last = s["origin"], s["last"]
        out.append(f"  Oldest surviving lines: {o['date']} {o['short']} {o['title']} — {o['author']}")
        for why in o["why"][:2]:
            out += _wrap(why, "      ")
        if last["sha"] != o["sha"]:
            out.append(f"  Last touched:           {last['date']} {last['short']} {last['title']} — {last['author']}")
        if s["fixes"]:
            out.append("  Fixes and reverts that touched it:")
            for e in s["fixes"]:
                out.append(f"    {e['date']} {e['short']} [{e['kind']}] {e['title']}")
                for why in e["why"][:1]:
                    out += _wrap(why, "        ")
        else:
            out.append("  No recorded fixes or reverts.")
        out.append("")
    return "\n".join(out)


def render_dir(query: str, files: List[Dict], color: bool = False, limit: int = 20) -> str:
    paint = Painter(color)
    out = [paint(f"{query.rstrip('/')}/  ({len(files)} files)", "bold"), ""]
    for f in files[:limit]:
        badge = paint(f"{f['level']:<8}{f['score']:>3}", LEVEL_COLOR[f["level"]])
        purpose = f["intent"][0]["text"] if f["intent"] else ""
        out.append(f"  {badge}  {f['path']}")
        out.append(paint(f"               {f['commits']} commits, {f['fixes']} fixes · {purpose}", "dim"))
    if len(files) > limit:
        out.append(f"  ... {len(files) - limit} more")
    out.append("\nRun `pr-archaeologist explain <file>` for a file's full story.")
    return "\n".join(out) + "\n"
