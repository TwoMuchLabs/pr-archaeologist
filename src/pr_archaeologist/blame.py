"""git blame parser: which commits own which lines, functions and warning comments today.

Blame is the expensive part of a run, so results are cached per file and only recomputed
when the file's content (blob sha) or its latest commit changed since the last run. Both
are needed: a change that is later reverted restores the old blob but not the old blame.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

from . import symbols as symbols_mod
from .gitio import git

GROUP_RE = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+) (\d+)$", re.M)
MAX_BLOB_BYTES = 512 * 1024
TRIPWIRE_RE = re.compile(
    r"(?:#|//|/\*|\*|--|;|<!--)\s*(.*(?:\b(?:HACK|XXX|FIXME|WARNING|WORKAROUND|IMPORTANT|CAUTION|DANGER)\b|"
    r"\bNB:|(?i:\bdo not (?:change|remove|touch|reorder|delete|edit|call|move)\b|"
    r"\bdon't (?:change|remove|touch|reorder|delete|edit|call|move)\b|\border matters\b|"
    r"\bmust (?:not|stay|remain|be kept|run before|run after)\b)).*)$"
)


def parse_incremental(output: str) -> List[Tuple[int, int, str]]:
    """Parse `git blame --incremental` into (final_start_line, line_count, sha) groups."""
    return [(int(m.group(3)), int(m.group(4)), m.group(1)) for m in GROUP_RE.finditer(output)]


def blame_file(repo: str, path: str, blob: str, ignore_revs: Optional[str]) -> Dict:
    args = ["blame", "--incremental", "-w", "-M"]
    if ignore_revs:
        args += ["--ignore-revs-file", ignore_revs]
    args += ["HEAD", "--", path]
    groups = parse_incremental(git(repo, *args, check=False))
    text = git(repo, "cat-file", "blob", blob, check=False)
    lines = text.splitlines()
    owner: List[Optional[str]] = [None] * (len(lines) + 2)
    totals: Dict[str, int] = {}
    for start, count, sha in groups:
        totals[sha] = totals.get(sha, 0) + count
        for ln in range(start, min(start + count, len(owner))):
            owner[ln] = sha

    syms = []
    for sym in symbols_mod.extract(path, text):
        counts: Dict[str, int] = {}
        for ln in range(sym["start"], min(sym["end"], len(lines)) + 1):
            sha = owner[ln]
            if sha:
                counts[sha] = counts.get(sha, 0) + 1
        sym["commits"] = counts
        syms.append(sym)

    tripwires = []
    for idx, line in enumerate(lines, start=1):
        if len(tripwires) >= 12:
            break
        m = TRIPWIRE_RE.search(line)
        if m and len(line) < 300:
            tripwires.append({"line": idx, "text": m.group(1).strip(" */-"), "sha": owner[idx]})

    return {"blob": blob, "lines": len(lines), "commits": totals, "symbols": syms, "tripwires": tripwires}


def head_blobs(repo: str) -> Dict[str, Tuple[str, int]]:
    """path -> (blob sha, size) for every regular file at HEAD."""
    out = git(repo, "ls-tree", "-r", "-l", "-z", "HEAD")
    blobs: Dict[str, Tuple[str, int]] = {}
    for entry in out.split("\0"):
        if "\t" not in entry:
            continue
        meta, path = entry.split("\t", 1)
        parts = meta.split()
        if len(parts) < 4 or parts[1] != "blob" or parts[0] == "120000":
            continue
        try:
            blobs[path] = (parts[2], int(parts[3]))
        except ValueError:
            continue
    return blobs


def _candidates(blobs: Dict, state: Dict, ignored: Callable[[str], bool], max_files: int) -> List[str]:
    """Code files worth blaming, within the budget."""
    candidates = [
        p for p, (_sha, size) in blobs.items()
        if symbols_mod.is_code(p) and size <= MAX_BLOB_BYTES and not ignored(p)
    ]
    if len(candidates) > max_files:
        # Budget blame for the files with the most history: that is where the stories are.
        weight = lambda p: len(state["files"].get(p, {}).get("commits", ()))
        candidates = sorted(candidates, key=lambda p: (-weight(p), p))[:max_files]
    return candidates


def update(
    repo: str,
    state: Dict,
    ignored: Callable[[str], bool],
    max_files: int = 2000,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict:
    """Refresh state['blame'] for files whose blob changed. Returns counts for the run log."""
    say = progress or (lambda _msg: None)
    blobs = head_blobs(repo)
    candidates = _candidates(blobs, state, ignored, max_files)
    wanted = set(candidates)

    blame = state["blame"]
    for stale in [p for p in blame if p not in wanted]:
        del blame[stale]

    def key(path: str) -> str:
        touched = state["files"].get(path, {}).get("commits") or [""]
        return f"{blobs[path][0]}:{touched[-1]}"

    todo = sorted(p for p in candidates if blame.get(p, {}).get("key") != key(p))

    ignore_revs = os.path.join(repo, ".git-blame-ignore-revs")
    ignore_revs = ignore_revs if os.path.exists(ignore_revs) else None
    if todo:
        say(f"  blaming {len(todo)} changed file(s), {len(candidates) - len(todo)} cached")
        with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2))) as pool:
            results = pool.map(lambda p: blame_file(repo, p, blobs[p][0], ignore_revs), todo)
            for path, result in zip(todo, results):
                result["key"] = key(path)
                blame[path] = result
    return {"blamed": len(todo), "cached": len(candidates) - len(todo)}
