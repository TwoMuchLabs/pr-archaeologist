"""Heuristic, LLM-free reading of commit messages.

Turns a subject + body into: a kind (fix / revert / security / perf / design /
feature / refactor / docs / test / chore / other), references (PRs, issues,
tickets), the sentences that explain *why*, and the sentences that warn.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

CONVENTIONAL = re.compile(r"^(?P<type>[A-Za-z]+)(\([^)]*\))?(?P<bang>!)?:\s+(?P<desc>.+)$")

KIND_BY_TYPE = {
    "fix": "fix", "bugfix": "fix", "hotfix": "fix", "bug": "fix",
    "feat": "feature", "feature": "feature", "add": "feature",
    "perf": "perf",
    "refactor": "refactor", "cleanup": "refactor",
    "revert": "revert",
    "docs": "docs", "doc": "docs",
    "test": "test", "tests": "test",
    "chore": "chore", "build": "chore", "ci": "chore", "style": "chore", "deps": "chore",
    "security": "security", "sec": "security",
}

FIX_RE = re.compile(
    r"\b(fix(es|ed|ing)?|close[sd]? .*\bfindings?|bug(fix)?|hotfix|regression|crash(es|ed|ing)?|race( condition)?|"
    r"deadlock|leak(s|ing)?|off[- ]by[- ]one|overflow|null ?pointer|npe|broken|"
    r"incorrect(ly)?|wrong(ly)?|workaround|double[- ](charge|count|free)|corrupt(s|ed|ion)?)\b",
    re.I,
)
# "fix typo", "fix mypy", "fix docs link": real commits, but not evidence that the code was broken.
TRIVIAL_FIX_RE = re.compile(
    r"\bfix(es|ed|ing)?:?\s+(a |an |the |some |more |up )?(typos?|typing|types?|type[- ](errors?|hints?|annotations?|checks?)|"
    r"mypy|pyright|lint\w*|flake8|ruff|pylint|eslint|docs?|docstrings?|documentation|whitespace|indentation|"
    r"format\w*|style|spelling|grammar|ci|tests?|changelog|changes|links?|comments?|imports?|pre-commit|"
    r"merge conflicts?|readme|dev|build|wording|example)\b",
    re.I,
)
TRIVIAL_FIXED_RE = re.compile(r"\b(typos?|spelling|whitespace|formatting|lint|docs?|grammar)\s+fix(es|ed)?\b", re.I)
FIXES_REF_RE = re.compile(r"\bfix(es|ed)?\s+(#\d+|[A-Z][A-Z0-9]+-\d+)", re.I)
# The bare word "security" is not enough ("add security policy" is paperwork, not a fix).
SECURITY_RE = re.compile(
    r"\b(cve-\d{4}-\d+|vulnerab\w+|xss|csrf|sql injection|injection|sanitiz\w+|"
    r"privilege escalation|bypass(es|ed)?|harden(s|ed|ing)?|spoof\w*|red[- ]team\w*|"
    r"fail[- ]opens?|least[- ]privilege|"
    r"security (fix(es)?|issue|bug|hole|patch|advisory|review|audit|finding(s)?))\b",
    re.I,
)
RELEASE_RE = re.compile(r"^\s*(release|prepare|bump( version)?( to)?|version)\s+v?\d+(\.\d+)+|^\s*v?\d+\.\d+(\.\d+)?\s*$", re.I)
DESIGN_RE = re.compile(
    r"\b(introduc(e|es|ed|ing)|initial (commit|implementation|version|import)|redesign\w*|"
    r"rewrit(e|es|ten)|re-?architect\w*|migrat(e|es|ed|ion)|replac(e|es|ed) .+ with|"
    r"switch(es|ed)? (to|from)|adopt(s|ed)?|deprecat\w+|split .+ into|consolidat\w+|"
    r"extract(s|ed)?|adr[- ]?\d+)\b",
    re.I,
)
PERF_RE = re.compile(r"\b(perf(ormance)?|optimi[sz]\w+|speed(s|ed)? up|faster|latency|n\+1|throughput)\b", re.I)
REFACTOR_RE = re.compile(r"\b(refactor\w*|clean ?up|renam(e|es|ed)|reorgani[sz]\w+|simplif\w+|tidy)\b", re.I)
DOCS_RE = re.compile(
    r"^(docs?|documentation|readme|changelog|typos?)\b|\b(fix|correct)\w* (a |the |some )?typos?\b|"
    r"\b(update|fix|improve|add|clarify|rewrite|expand)\w* (the )?(docs?|documentation|readme|changelog|comments?)\b",
    re.I,
)
TEST_RE = re.compile(r"\b(tests?|coverage)\b", re.I)

REVERT_SHA_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})", re.I)
REVERT_SUBJECT_RE = re.compile(r'^Revert\s+"(.+)"\s*$')

PR_SQUASH_RE = re.compile(r"\(#(\d+)\)\s*$")
PR_MERGE_RE = re.compile(r"^Merge pull request #(\d+)")
MR_RE = re.compile(r"See merge request\s+\S*!(\d+)")
ISSUE_RE = re.compile(r"(?<![\w/])#(\d+)\b")
TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
NOT_TICKETS = ("CVE-", "UTF-", "ISO-", "SHA-", "RFC-", "ADR-", "HTTP-", "TLS-", "MD-", "X-")

WHY_RE = re.compile(
    r"\b(because|so that|in order to|to (avoid|prevent|stop|keep|ensure|make sure)|otherwise|"
    r"caused by|root cause|the (problem|issue|bug) (is|was)|turns out|due to|"
    r"this (ensures|prevents|means|avoids|fixes|lets)|instead of|we (decided|chose|need|now)|"
    r"trade-?off|since (the|we|it|this)|which (caused|meant|led)|rather than)\b",
    re.I,
)
WARN_RE = re.compile(
    r"(\b(do not|don't|never|must not|must|always|careful|beware|caveat|gotcha|hack|fragile|"
    r"subtle|important|workaround|temporary|until)\b|\b(note|warning|nb)\s*:)",
    re.I,
)
TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z-]+:\s", re.I)
TRAILER_KEYS = (
    "signed-off-by", "co-authored-by", "change-id", "reviewed-by", "acked-by",
    "tested-by", "cc", "reviewed-on", "bug", "claude-session",
)


def strip_conventional(subject: str) -> str:
    m = CONVENTIONAL.match(subject)
    text = m.group("desc") if m and m.group("type").lower() in KIND_BY_TYPE else subject
    text = PR_SQUASH_RE.sub("", text).strip()
    return text if len(text) <= 140 else text[:137].rstrip() + "..."


def _sentences(body: str) -> List[str]:
    """Split a commit body into sentence-ish units, dropping trailers and noise.

    A bullet is its own unit (with its wrapped continuation lines); prose paragraphs are
    split on sentence ends.
    """
    units: List[str] = []
    for para in re.split(r"\n\s*\n", body):
        prose: List[str] = []
        bullet: Optional[str] = None

        def flush() -> None:
            nonlocal bullet
            if bullet is not None:
                units.append(bullet)
                bullet = None
            if prose:
                text = " ".join(prose)
                units.extend(re.split(r"(?<=[.!?])\s+(?=[A-Z`\"'])", text))
                prose.clear()

        for line in para.splitlines():
            line = line.strip()
            if not line:
                continue
            key = line.split(":", 1)[0].lower()
            if TRAILER_RE.match(line) and key in TRAILER_KEYS:
                continue
            m = re.match(r"^(?:[-*+•]|\d+[.)])\s+(.*)$", line)
            if m:
                flush()
                bullet = m.group(1)
            elif bullet is not None:
                bullet += " " + line
            else:
                prose.append(line)
        flush()
    out: List[str] = []
    for sent in units:
        sent = sent.strip()
        if len(sent) >= 12 and not sent.endswith(":"):
            out.append(sent if len(sent) <= 260 else sent[:257].rstrip() + "...")
    return out


def classify(subject: str, body: str) -> Dict:
    """Return the derived reading of one commit message."""
    full = subject + "\n" + body
    tags: List[str] = []
    kind: Optional[str] = None

    reverts = None
    m = REVERT_SHA_RE.search(body)
    if m:
        reverts = m.group(1)
    if reverts or REVERT_SUBJECT_RE.match(subject) or subject.lower().startswith("revert:"):
        kind = "revert"

    conv = CONVENTIONAL.match(subject)
    conv_kind = KIND_BY_TYPE.get(conv.group("type").lower()) if conv else None
    if conv and conv.group("bang"):
        tags.append("breaking")
    if "BREAKING CHANGE" in body:
        tags.append("breaking")

    trivial = TRIVIAL_FIX_RE.search(subject) or TRIVIAL_FIXED_RE.search(subject)
    if kind is None and trivial and not FIXES_REF_RE.search(full):
        kind = "chore"
    if kind is None and RELEASE_RE.match(subject):
        kind = "release"  # a bundle of earlier work: never evidence that one file was broken
    if kind is None:
        if SECURITY_RE.search(subject) or re.search(r"cve-\d{4}-\d+", full, re.I):
            kind = "security"
        elif conv_kind:
            kind = conv_kind
        elif FIX_RE.search(subject) or FIXES_REF_RE.search(full):
            kind = "fix"
        elif PERF_RE.search(subject):
            kind = "perf"
        elif DESIGN_RE.search(subject):
            kind = "design"
        elif REFACTOR_RE.search(subject):
            kind = "refactor"
        elif TEST_RE.search(subject) and not re.search(r"\b(add|implement|support)\b", subject, re.I):
            kind = "test"
        elif DOCS_RE.search(subject):
            kind = "docs"
        elif re.match(r"^(add|implement|support|allow|enable|create)\b", subject, re.I):
            kind = "feature"
        else:
            kind = "other"

    if kind in ("feature", "refactor", "other") and DESIGN_RE.search(subject):
        tags.append("design")
    if re.search(r"\bhotfix\b|\burgent\b", subject, re.I) or re.search(r"^\s*hotfix\b", body, re.I | re.M):
        tags.append("hotfix")

    prs = set(int(x) for x in PR_SQUASH_RE.findall(subject))
    prs.update(int(x) for x in PR_MERGE_RE.findall(subject))
    prs.update(int(x) for x in MR_RE.findall(body))
    issues = set(int(x) for x in ISSUE_RE.findall(full)) - prs
    tickets = sorted({t for t in TICKET_RE.findall(full) if not t.startswith(NOT_TICKETS)})

    why: List[str] = []
    warn: List[str] = []
    for sent in _sentences(body):
        if WARN_RE.search(sent) and len(warn) < 2:
            warn.append(sent)
        elif WHY_RE.search(sent) and len(why) < 3:
            why.append(sent)
    # A fix with no explicit "because" still usually explains itself in its first sentence.
    if not why and kind in ("fix", "security", "revert", "design"):
        first = [s for s in _sentences(body) if s not in warn][:1]
        why = first

    lead = next((s for s in _sentences(body) if s not in warn), None)

    return {
        "kind": kind,
        "lead": lead,
        "tags": sorted(set(tags)),
        "prs": sorted(prs),
        "issues": sorted(issues),
        "tickets": tickets,
        "why": why,
        "warn": warn,
        "reverts": reverts,
    }
