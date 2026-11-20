"""Optional: read pull-request descriptions and review discussion from GitHub.

Commit messages say what changed; the pull request usually says why, and the review
says what nearly went wrong. This module fetches the pull requests that commits refer
to (`(#12)`, `Merge pull request #12`) and keeps the sentences worth showing.

It is the only part of pr-archaeologist that touches the network, it is off unless
`dig --prs` is used, and it uses nothing beyond the standard library. Each pull request
is fetched once and cached; open ones are re-read until they close.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from .classify import WARN_RE, WHY_RE, _sentences
from .gitio import git

API = "https://api.github.com"
REMOTE_RE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.I)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s")
NOISE_LINE_RE = re.compile(r"^\s*(!\[|<img|<details|</details|<summary|\|[-:| ]+\|\s*$|```)", re.I)
BOT_RE = re.compile(r"(\[bot\]$|^dependabot|^renovate|^github-actions|^codecov|-bot$)", re.I)
# Remarks about the pull request's own paperwork, not about the code.
PROCESS_RE = re.compile(
    r"\b(sign(ed)?[- ]off|signoff|dco|rebas(e|ed|ing)|merge conflicts?|squash\w*|lgtm|nit\b|"
    r"this (pr|pull request)|(ci|check|build|pipeline) (is|was|will|should|fails?|failed|passes|passed)|"
    r"approv(e|ed|al)|force[- ]push\w*|open(ed|ing)? an? (issue|pr|pull request)|merged (in)?to|"
    r"access token|changelog|release notes|documentation update|reference this change|"
    r"wrong branch|target(s|ed|ing)? (main|master|the))",
    re.I,
)
# Review threads are conversation. Only keep a remark that states a hazard or a rule about
# the code; questions, replies, quotes and first-person hedging are dropped.
REMARK_SIGNAL_RE = re.compile(
    r"\b(never|always|must(?![-\w])|otherwise|will (break|fail|crash|leak)|breaks?|broke|race|unsafe|"
    r"thread[- ]?safe\w*|deadlock|regression|backwards?[- ]?(in)?compat\w*|deprecat\w*|"
    r"security|vulnerab\w+|edge case|careful|subtle|side effect|data loss|corrupt\w*|invariant)\b",
    re.I,
)
REMARK_CHATTER_RE = re.compile(
    r"^\s*(>|@\w)|\?\s*$|"
    r"\b(i (don't|do not|didn't|would|wouldn't|will|won't|can|can't|could|think|guess|believe|agree|want|"
    r"wonder|suppose|feel|prefer|am|was|have|had)|i'm|i've|i'll|i'd|imo|imho|thanks|thank you|"
    r"you (do not|don't) need|let me know|for the future|sorry|please)\b",
    re.I,
)
MAX_DISCUSSION = 4

FetchJson = Callable[[str], object]


def remote_slug(repo: str) -> Optional[Tuple[str, str]]:
    """(owner, name) of the GitHub repository behind `origin`, or None."""
    url = git(repo, "remote", "get-url", "origin", check=False).strip()
    m = REMOTE_RE.search(url)
    return (m.group(1), m.group(2)) if m else None


def find_token() -> Optional[str]:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.decode().strip() or None if out.returncode == 0 else None


def http_fetcher(token: Optional[str]) -> FetchJson:
    def fetch(path: str) -> object:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "pr-archaeologist"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(API + path, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    return fetch


def clean_body(text: str) -> str:
    """Drop template scaffolding: HTML comments, checklists, images, tables, code fences."""
    text = HTML_COMMENT_RE.sub("", text or "")
    kept, in_code = [], False
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or CHECKBOX_RE.match(line) or NOISE_LINE_RE.match(line):
            continue
        if line.lstrip().startswith("#"):
            kept.append("")  # a heading separates paragraphs but is not itself a reason
            continue
        kept.append(re.sub(r"\*\*|__|`", "", line))
    return "\n".join(kept).strip()


def _pick(body: str) -> Tuple[List[str], List[str], Optional[str]]:
    why, warn = [], []
    sentences = [s for s in _sentences(clean_body(body)) if not PROCESS_RE.search(s)]
    for sent in sentences:
        if WARN_RE.search(sent) and len(warn) < 2:
            warn.append(sent)
        elif WHY_RE.search(sent) and len(why) < 3:
            why.append(sent)
    lead = next((s for s in sentences if s not in warn), None)
    return why, warn, lead


def summarise(pr: Dict, comments: List[Dict]) -> Dict:
    """Reduce one pull request and its discussion to what the outputs show."""
    why, warn, lead = _pick(pr.get("body") or "")
    author = (pr.get("user") or {}).get("login", "")
    discussion = []
    for item in comments:
        who = (item.get("user") or {}).get("login", "")
        kind = (item.get("user") or {}).get("type", "")
        if not who or kind == "Bot" or BOT_RE.search(who):
            continue
        for sent in _sentences(clean_body(item.get("body") or "")):
            if PROCESS_RE.search(sent) or REMARK_CHATTER_RE.search(sent) or len(sent) < 40:
                continue
            if REMARK_SIGNAL_RE.search(sent) and len(discussion) < MAX_DISCUSSION:
                discussion.append({"author": who, "text": sent, "own": who == author})
    return {
        "number": pr.get("number"),
        "title": (pr.get("title") or "").strip(),
        "author": author,
        "url": pr.get("html_url", ""),
        "state": "merged" if pr.get("merged_at") else pr.get("state", ""),
        "lead": lead,
        "why": why,
        "warn": warn,
        "discussion": discussion,
    }


def update(
    repo: str,
    state: Dict,
    fetch_json: Optional[FetchJson] = None,
    limit: int = 300,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict:
    """Fetch the pull requests commits refer to that are not cached yet. Never raises."""
    say = progress or (lambda _m: None)
    cached = state.setdefault("prs", {})
    wanted: Dict[int, str] = {}
    for commit in state["commits"].values():
        for number in commit.get("prs", []):
            if commit["date"] > wanted.get(number, ""):
                wanted[number] = commit["date"]
    todo = [
        n for n, _date in sorted(wanted.items(), key=lambda kv: kv[1], reverse=True)
        if str(n) not in cached or cached[str(n)].get("state") == "open"
    ][:limit]
    result = {"fetched": 0, "cached": len(wanted) - len(todo), "failed": 0, "skipped": ""}
    if not todo:
        return result
    slug = remote_slug(repo)
    if slug is None and fetch_json is None:
        result["skipped"] = "origin is not a GitHub repository"
        return result
    owner, name = slug or ("", "")
    fetch = fetch_json or http_fetcher(find_token())
    base = f"/repos/{owner}/{name}"
    say(f"  reading {len(todo)} pull request(s) from GitHub")
    for number in todo:
        try:
            pr = fetch(f"{base}/pulls/{number}")
            comments: List[Dict] = []
            if isinstance(pr, dict):
                if pr.get("comments"):
                    comments += list(fetch(f"{base}/issues/{number}/comments?per_page=100") or [])
                if pr.get("review_comments"):
                    comments += list(fetch(f"{base}/pulls/{number}/comments?per_page=100") or [])
                comments += [r for r in list(fetch(f"{base}/pulls/{number}/reviews?per_page=100") or []) if r.get("body")]
                cached[str(number)] = summarise(pr, comments)
                result["fetched"] += 1
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # "#12" was an issue, or the PR lives in another repository
                cached[str(number)] = {"number": number, "state": "missing"}
                continue
            result["failed"] += 1
            if exc.code in (401, 403, 429):  # no access or rate limited: stop, try again next run
                result["skipped"] = f"GitHub answered {exc.code}; set GH_TOKEN or try again later"
                break
        except (urllib.error.URLError, OSError, ValueError) as exc:
            result["failed"] += 1
            result["skipped"] = f"could not reach GitHub ({exc.__class__.__name__})"
            break
    return result
