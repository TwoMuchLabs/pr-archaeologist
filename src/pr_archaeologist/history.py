"""git log parser and incremental history builder.

One streaming `git log` gives us, per commit: metadata, the full message, per-file
status (added / modified / deleted / renamed) and per-file line counts. Commits are
applied oldest-first so renames can be followed forward: a file's history is always
keyed by the path it has *today*.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Iterator, List, Optional

from . import cache
from .classify import PR_MERGE_RE, classify
from .gitio import GitError, git, head_sha, is_ancestor

RS, US = "\x1e", "\x1f"
LOG_FORMAT = f"%x1e%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%b%x1f"
NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t(.*)$", re.S)
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@ ?(.*)$")
BODY_LIMIT = 1500
COCHANGE_MAX_FILES = 25   # bigger commits are sweeping changes, not evidence of coupling
SYMBOL_HINT_MAX_FILES = 40
SYMBOL_KINDS = ("fix", "security", "revert", "perf")

# Languages git ships hunk-header patterns for. Supplied through core.attributesFile so
# `@@ ... @@ def charge(...)` names the enclosing function even without a .gitattributes.
BUILTIN_DIFF_DRIVERS = {
    "python": ["py", "pyi"], "golang": ["go"], "rust": ["rs"], "java": ["java"],
    "kotlin": ["kt", "kts"], "ruby": ["rb"], "php": ["php"], "csharp": ["cs"],
    "cpp": ["c", "h", "cc", "cpp", "hpp", "cxx"], "perl": ["pl", "pm"],
    "elixir": ["ex", "exs"], "bash": ["sh", "bash"], "objc": ["m", "mm"],
    "css": ["css", "scss"], "markdown": ["md"],
}

CTX_KEYWORD_RE = re.compile(
    r"\b(?:def|func|function|fn|fun|sub|class|struct|interface|impl|trait|module|enum)\s+"
    r"(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)"
)
CTX_CALLISH_RE = re.compile(r"([A-Za-z_]\w*)\s*\(")
CTX_STOPWORDS = {"if", "for", "while", "switch", "return", "catch", "elif", "with", "when"}


def _stream_records(repo: str, args: List[str]) -> Iterator[str]:
    """Yield one raw record per commit without holding the whole log in memory."""
    with subprocess.Popen(
        ["git", "-c", "core.quotepath=off", *args], cwd=repo,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as proc:
        assert proc.stdout is not None and proc.stderr is not None
        sep = RS.encode()
        buf = b""
        seen = False  # everything before the first separator is preamble, not a record
        while True:
            chunk = proc.stdout.read(1 << 20)
            if not chunk:
                break
            buf += chunk
            parts = buf.split(sep)
            buf = parts.pop()
            for part in parts:
                if not seen:
                    seen = True
                    continue
                yield part.decode("utf-8", "replace")
        err = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait() != 0:
            raise GitError(f"git log failed: {err.strip()}")
        if seen and buf:
            yield buf.decode("utf-8", "replace")


def utc_iso(stamp: str) -> str:
    """Normalise git's ISO date to UTC so that dates from different time zones sort correctly."""
    try:
        when = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return stamp.strip()
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def parse_record(record: str) -> Optional[Dict]:
    """Parse one `--raw --numstat -z` log record into a commit dict (without derived fields)."""
    parts = record.split(US)
    if len(parts) < 7:
        return None
    sha, parents, author, email, date, subject, body = parts[:7]
    tokens = US.join(parts[7:]).split("\0")
    files: Dict[str, Dict] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i].lstrip("\n")
        i += 1
        if not tok:
            continue
        if tok.startswith(":"):
            status = tok.rsplit(" ", 1)[-1][:1]
            if status in ("R", "C") and i + 1 < len(tokens):
                old, new = tokens[i], tokens[i + 1]
                i += 2
            elif i < len(tokens):
                old, new = None, tokens[i]
                i += 1
            else:
                break
            entry = files.setdefault(new, {"a": 0, "d": 0, "s": status, "old": old})
            entry["s"], entry["old"] = status, old
            continue
        m = NUMSTAT_RE.match(tok)
        if not m:
            continue
        added, deleted, path = m.groups()
        if path == "" and i + 1 < len(tokens):  # rename: counts, then old\0new
            path = tokens[i + 1]
            i += 2
        entry = files.setdefault(path, {"a": 0, "d": 0, "s": "M", "old": None})
        entry["a"] = 0 if added == "-" else int(added)   # "-" marks a binary file
        entry["d"] = 0 if deleted == "-" else int(deleted)
    return {
        "sha": sha.strip(),
        "parents": parents.split(),
        "author": author,
        "email": email,
        "date": utc_iso(date),
        "subject": subject.strip(),
        "body": body.strip(),
        "files": [[p, f["a"], f["d"], f["s"], f["old"]] for p, f in files.items()],
    }


def _new_file() -> Dict:
    return {"commits": [], "born": None, "deleted": None, "previous": []}


def _move_cochange(co: Dict, old: str, new: str) -> None:
    moved = co.pop(old, None)
    if not moved:
        return
    target = co.setdefault(new, {})
    target.pop(old, None)
    for other, n in moved.items():
        if other == new:
            continue
        target[other] = target.get(other, 0) + n
        partner = co.get(other)
        if partner is not None:
            partner[new] = partner.get(new, 0) + partner.pop(old, 0)


def apply_commit(state: Dict, commit: Dict) -> None:
    """Fold one commit (oldest-first order) into the file histories and coupling counts."""
    sha = commit["sha"]
    files, co = state["files"], state["cochange"]
    touched: List[str] = []
    for path, _added, _deleted, status, old in commit["files"]:
        if status == "R" and old and old != path:
            rec = files.pop(old, None) or _new_file()
            if old not in rec["previous"]:
                rec["previous"].append(old)
            existing = files.get(path)
            if existing:  # the new name was used before: merge both pasts
                rec["commits"] = existing["commits"] + rec["commits"]
                rec["previous"] = existing["previous"] + rec["previous"]
                rec["born"] = existing["born"] or rec["born"]
            files[path] = rec
            _move_cochange(co, old, path)
        rec = files.setdefault(path, _new_file())
        rec["commits"].append(sha)
        if rec["born"] is None:
            rec["born"] = sha
        if status == "D":
            rec["deleted"] = sha
        elif rec["deleted"]:
            rec["deleted"] = None  # resurrected
        touched.append(path)
    if 2 <= len(touched) <= COCHANGE_MAX_FILES:
        for a in touched:
            row = co.setdefault(a, {})
            for b in touched:
                if a != b:
                    row[b] = row.get(b, 0) + 1


def _attributes_file(repo: str) -> str:
    directory = os.path.join(repo, cache.CACHE_DIR)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "gitattributes")
    lines = [f"*.{ext} diff={driver}" for driver, exts in sorted(BUILTIN_DIFF_DRIVERS.items()) for ext in exts]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def symbol_from_context(ctx: str) -> Optional[str]:
    """Pull a function/class name out of a hunk header's trailing context text."""
    m = CTX_KEYWORD_RE.search(ctx)
    if m:
        return m.group(1)
    for name in CTX_CALLISH_RE.findall(ctx):
        if name not in CTX_STOPWORDS:
            return name
    return None


def symbols_touched(repo: str, sha: str, attributes: str) -> Dict[str, List[str]]:
    """Which named functions did this commit change? Read from zero-context hunk headers."""
    out = git(
        repo, "show", "-U0", "-M", "--no-color", "--format=", sha,
        check=False, config={"core.attributesFile": attributes},
    )
    result: Dict[str, List[str]] = {}
    path = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            target = line[4:].rstrip("\t")
            path = target[2:] if target.startswith("b/") else None
        elif line.startswith("@@") and path:
            m = HUNK_RE.match(line)
            name = symbol_from_context(m.group(1)) if m and m.group(1) else None
            if name and name not in result.setdefault(path, []):
                result[path].append(name)
    return {p: names for p, names in result.items() if names}


def _link_merge(repo: str, state: Dict, commit: Dict) -> None:
    """A `Merge pull request #N` commit names the PR for every commit it brought in."""
    m = PR_MERGE_RE.match(commit["subject"])
    if not m or len(commit["parents"]) < 2:
        return
    number = int(m.group(1))
    title = next((ln.strip() for ln in commit["body"].splitlines() if ln.strip()), "")
    p1, p2 = commit["parents"][0], commit["parents"][1]
    members = git(repo, "rev-list", "--no-merges", "--max-count=300", f"{p1}..{p2}", check=False).split()
    for sha in members:
        rec = state["commits"].get(sha)
        if rec is None:
            continue
        if number not in rec["prs"]:
            rec["prs"] = sorted(rec["prs"] + [number])
        if title and not rec.get("pr_title"):
            rec["pr_title"] = title


def update(
    repo: str,
    state: Dict,
    full: bool = False,
    max_commits: Optional[int] = None,
    progress: Optional[Callable[[str], None]] = None,
    generated: Optional[List[str]] = None,
) -> Dict:
    """Bring `state` up to the repo's HEAD. Returns a small summary of what was done.

    Incremental when the cached head is still an ancestor of HEAD; otherwise (first run,
    force-push, rebased history, `full=True`) the history is rebuilt from scratch.
    """
    say = progress or (lambda _msg: None)
    head = head_sha(repo)
    if head is None:
        raise GitError("repository has no commits yet")

    mode = "incremental"
    prior = state.get("head")
    if full or not prior:
        mode = "full"
    elif prior == head:
        return {"mode": "noop", "new_commits": 0, "head": head}
    elif not is_ancestor(repo, prior, head):
        mode = "full"
        say("cached head is no longer an ancestor of HEAD (history rewritten); rebuilding")

    if mode == "full":
        keep = {key: state.get(key, {}) for key in ("blame", "docs", "prs", "issues")}
        state.clear()
        state.update(cache.empty_state())
        state.update(keep)  # still valid: keyed by content or by pull-request number
        rev = head
    else:
        rev = f"{prior}..{head}"

    args = [
        "log", "--reverse", "--topo-order", "-M", "--raw", "--numstat", "-z",
        "--no-abbrev", f"--format={LOG_FORMAT}",
    ]
    if mode == "full" and max_commits:
        args.append(f"--max-count={max_commits}")
        state["truncated"] = True
    args.append(rev)

    own_outputs = set(generated or [])
    new_shas: List[str] = []
    merges: List[Dict] = []
    for record in _stream_records(repo, args):
        commit = parse_record(record)
        if commit is None:
            continue
        if len(commit["parents"]) > 1:
            state["merges"] = state.get("merges", 0) + 1
            merges.append(commit)
            continue
        if commit["files"] and all(entry[0] in own_outputs for entry in commit["files"]):
            # The nightly job committing our own output is not history worth analysing, and
            # counting it would make every run change the index it just committed.
            continue
        derived = classify(commit["subject"], commit["body"])
        commit.update(derived)
        commit["reverted_by"] = None
        commit["pr_title"] = commit["subject"] if derived["prs"] else None
        commit["symbols"] = {}
        if len(commit["body"]) > BODY_LIMIT:
            commit["body"] = commit["body"][:BODY_LIMIT].rstrip() + "\n[...]"
        state["commits"][commit["sha"]] = commit
        apply_commit(state, commit)
        new_shas.append(commit["sha"])
        state["last_commit"] = commit["sha"]
        if len(new_shas) % 2000 == 0:
            say(f"  parsed {len(new_shas)} commits")

    # Resolve "This reverts commit <sha>" into edges both ways.
    for sha in new_shas:
        commit = state["commits"][sha]
        target = commit.get("reverts")
        if not target:
            continue
        full_sha = target if target in state["commits"] else next(
            (s for s in state["commits"] if s.startswith(target)), None
        )
        commit["reverts"] = full_sha
        if full_sha:
            state["commits"][full_sha]["reverted_by"] = sha

    for merge in merges:
        _link_merge(repo, state, merge)

    # Function-level attribution for the commits that matter most (fixes and friends).
    wanted = [
        s for s in new_shas
        if state["commits"][s]["kind"] in SYMBOL_KINDS
        and 0 < len(state["commits"][s]["files"]) <= SYMBOL_HINT_MAX_FILES
    ]
    if wanted:
        attributes = _attributes_file(repo)
        with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2))) as pool:
            for sha, hints in zip(wanted, pool.map(lambda s: symbols_touched(repo, s, attributes), wanted)):
                state["commits"][sha]["symbols"] = hints

    state["head"] = head
    return {"mode": mode, "new_commits": len(new_shas), "head": head}
