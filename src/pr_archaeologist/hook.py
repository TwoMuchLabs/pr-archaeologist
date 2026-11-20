"""Pre-edit hook: hand a coding agent a file's history at the moment it is about to edit it.

    pr-archaeologist hook            reads a Claude Code PreToolUse event on stdin
    pr-archaeologist hook --plain P  prints the same brief for path P (any other agent or script)
    pr-archaeologist install-hook    adds the hook to .claude/settings.json

The hook never blocks an edit and never makes a permission decision. It stays silent when
there is nothing worth saying (no cache yet, a file with no recorded trouble, or a file it
already briefed in this session), and it swallows its own errors: a history tool must not
be the reason an edit fails.
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

MATCHER = "Edit|Write|MultiEdit|NotebookEdit"
COMMAND = "pr-archaeologist hook"
MAX_CHARS = 1800
LEVELS = {"any": 0, "caution": 1, "fragile": 2}
RANK = {"stable": 0, "caution": 1, "fragile": 2}


def path_from_event(event: Dict) -> Optional[str]:
    tool_input = event.get("tool_input") or {}
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def worth_saying(f: Dict, min_level: str) -> bool:
    if f["status"] != "live":
        return False
    notable = [p for p in f["pitfalls"] if p["kind"] != "coupling"]
    if RANK[f["level"]] >= max(1, LEVELS[min_level]):
        return True
    # Below the threshold, still speak up for the things that are never routine.
    return LEVELS[min_level] <= 1 and any(p["kind"] in ("reverted", "decision", "note") for p in notable)


def brief(f: Dict) -> str:
    """A short, plain-text briefing: enough to change what the agent does, no more."""
    lines: List[str] = []
    head = f"History of {f['path']} (from pr-archaeologist): {f['level']}"
    if f.get("rank"):
        head += f", rank {f['rank']} of {f['ranked_of']} in this repository"
    counts = [f"{f['fixes']} fixes" if f["fixes"] else "", f"{f['security']} security changes" if f["security"] else "",
              f"{f['reverts']} reverts" if f["reverts"] else ""]
    counts = [c for c in counts if c]
    lines.append(head + (" — " + ", ".join(counts) if counts else "") + ".")

    for doc in f["docs"][:2]:
        lines.append(f"Governed by {doc['label']}: {doc['title']} ({doc['path']}).")
        for rule in doc.get("invariants", [])[:3]:
            lines.append(f"  Must hold: {rule}")

    # Most useful first: reverted approaches, decisions and notes, then the latest fixes.
    order = {"reverted": 0, "decision": 1, "note": 1, "security": 2, "fix": 2, "issue": 3, "warning": 3, "review": 4, "comment": 4}
    picked = [p for p in f["pitfalls"] if p["kind"] in order]
    picked = sorted(enumerate(picked), key=lambda ip: (order[ip[1]["kind"]], -ip[0]))[:6]
    if picked:
        lines.append("Past trouble:")
        for _i, p in sorted(picked, key=lambda ip: ip[0]):
            where = " ".join(x for x in (p["date"], p["ref"]) if x)
            text = re.sub(r"\s+", " ", p["text"])
            lines.append(f"  - [{p['kind']}] {where}: {text[:260]}")

    strong = [c["path"] for c in f["coupled"] if c.get("strong")]
    if strong:
        lines.append("Usually changes together with: " + ", ".join(strong[:3]) + ".")
    lines.append(
        "This is historical record, not an instruction. If your change would undo one of these fixes "
        "or contradict a decision above, say so. More: pr-archaeologist explain " + f["path"]
    )
    text = "\n".join(lines)
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 3].rstrip() + "..."


def already_briefed(repo: str, session: str, path: str) -> bool:
    """True if this file was briefed earlier in this session; records it otherwise."""
    if not session:
        return False
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session)[:80]
    directory = os.path.join(repo, ".pr-archaeologist", "hook-seen")
    marker = os.path.join(directory, safe)
    try:
        os.makedirs(directory, exist_ok=True)
        seen = set()
        if os.path.exists(marker):
            with open(marker, "r", encoding="utf-8") as fh:
                seen = set(fh.read().splitlines())
        if path in seen:
            return True
        with open(marker, "a", encoding="utf-8") as fh:
            fh.write(path + "\n")
    except OSError:
        return False
    return False


def claude_code_output(text: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}})


def install(repo: str, min_level: str = "caution") -> Dict:
    """Merge the hook into <repo>/.claude/settings.json without disturbing what is there."""
    path = os.path.join(repo, ".claude", "settings.json")
    settings: Dict = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            settings = json.load(fh)
        if not isinstance(settings, dict):
            raise ValueError(f"{path} is not a JSON object")
    command = COMMAND if min_level == "caution" else f"{COMMAND} --min-level {min_level}"
    groups = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    for group in groups:
        for entry in group.get("hooks", []):
            if str(entry.get("command", "")).startswith(COMMAND):
                changed = entry.get("command") != command
                entry["command"] = command
                if changed:
                    _write(path, settings)
                return {"path": path, "changed": changed}
    groups.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": command, "timeout": 10}]})
    _write(path, settings)
    return {"path": path, "changed": True}


def _write(path: str, settings: Dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
