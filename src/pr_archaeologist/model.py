"""Turn the raw cached facts into the knowledge model every output is rendered from.

The model answers, per file: why does it exist, what decisions shaped it, what broke
before, what tends to change with it, and how careful should you be. It is rebuilt
from the cache on every run (cheap, pure Python), so scoring or wording changes never
require re-reading git.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections import Counter
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .classify import strip_conventional

DEFAULT_IGNORES = [
    "*.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "go.sum", "Cargo.lock",
    "*.min.js", "*.min.css", "*.map", "*.snap",
    "node_modules/*", "vendor/*", "dist/*", "build/*", "target/*", ".venv/*",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.pdf", "*.svg", "*.woff", "*.woff2", "*.ttf",
    "CHANGELOG*", "CHANGES", "CHANGES.*", "HISTORY.*", "NEWS", "NEWS.*", "AUTHORS*", "CONTRIBUTORS*",
    ".pr-archaeologist/*", "CODEBASE_ARCHAEOLOGY.md", "archaeology-graph.html", "archaeology.json",
]
RISKY_KINDS = ("fix", "security", "revert")
LANDMARK_KINDS = ("fix", "security", "revert", "design", "perf")
ADR_MENTION_RE = re.compile(r"\badr[- _#]?0*(\d{1,5})\b", re.I)
COUPLING_NOISE = ("changelog", "changes", "history", "news", "release", "authors", "contributors")
PROSE_EXTENSIONS = {"md", "markdown", "rst", "txt", "adoc", "cff"}
PROSE_NAMES = ("license", "licence", "notice", "copying", "readme", "codeowners")
MAX_DOC_LINKS = 8
FRAGILE, CAUTION = 60, 30


def is_prose(path: str) -> bool:
    """Documentation and legal text: it has a history, but "fragile" is the wrong word for it."""
    base = path.rsplit("/", 1)[-1].lower()
    ext = base.rsplit(".", 1)[-1] if "." in base else ""
    return ext in PROSE_EXTENSIONS or base.split(".", 1)[0].startswith(PROSE_NAMES)


def make_ignore(patterns: Iterable[str]) -> Callable[[str], bool]:
    pats = list(patterns)

    def ignored(path: str) -> bool:
        base = path.rsplit("/", 1)[-1]
        for pat in pats:
            if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, "*/" + pat):
                return True
            if "/" not in pat and fnmatch.fnmatch(base, pat):
                return True
        return False

    return ignored


_PRS: Dict[str, Dict] = {}  # set by build(); pull-request summaries keyed by number
_ISSUES: Dict[str, Dict] = {}  # set by build(); summaries of issues that were closed


def issue_notes(commit: Dict) -> List[Dict]:
    """The problem reports this commit (or its pull request) closed."""
    from .prs import closing_refs  # local import: prs imports nothing from model

    numbers = closing_refs(commit["subject"] + "\n" + commit.get("body", ""))
    for pr_number in commit.get("prs", []):
        numbers += (_PRS.get(str(pr_number)) or {}).get("closes", [])
    notes: List[Dict] = []
    for number in dict.fromkeys(numbers):
        issue = _ISSUES.get(str(number))
        if not issue or issue.get("state") == "missing":
            continue
        notes.append({
            "issue": number, "title": issue.get("title", ""), "author": issue.get("author", ""),
            "lead": issue.get("lead"), "warn": issue.get("warn", []),
            "discussion": issue.get("discussion", []),
        })
    return notes[:3]


def pr_notes(commit: Dict) -> List[Dict]:
    """What the commit's pull request adds: its reasons, warnings and review remarks."""
    notes: List[Dict] = []
    own = set(commit.get("why", [])) | set(commit.get("warn", []))
    if commit.get("lead"):
        own.add(commit["lead"])
    for number in commit.get("prs", []):
        pr = _PRS.get(str(number))
        if not pr or pr.get("state") == "missing":
            continue
        for text in pr.get("why", [])[:2]:
            if text not in own:
                notes.append({"pr": number, "kind": "why", "author": pr.get("author", ""), "text": text})
        if not pr.get("why") and pr.get("lead") and pr["lead"] not in own:
            # The opening line says what the pull request is, which helps explain why a file
            # exists but is not a reason a fix was needed.
            notes.append({"pr": number, "kind": "lead", "author": pr.get("author", ""), "text": pr["lead"]})
        for text in pr.get("warn", []):
            if text not in own:
                notes.append({"pr": number, "kind": "warn", "author": pr.get("author", ""), "text": text})
        for item in pr.get("discussion", []):
            notes.append({"pr": number, "kind": "review", "author": item["author"], "text": item["text"]})
    return notes[:5]


def brief(commit: Dict) -> Dict:
    return {
        "pr_notes": pr_notes(commit),
        "issue_notes": issue_notes(commit),
        "sha": commit["sha"],
        "short": commit["sha"][:7],
        "date": commit["date"][:10],
        "author": commit["author"],
        "kind": commit["kind"],
        "tags": commit.get("tags", []),
        "subject": commit["subject"],
        "title": strip_conventional(commit["subject"]),
        "why": commit.get("why", []),
        "lead": commit.get("lead"),
        "warn": commit.get("warn", []),
        "prs": commit.get("prs", []),
        "issues": commit.get("issues", []),
        "tickets": commit.get("tickets", []),
        "reverts": commit.get("reverts"),
        "reverted_by": commit.get("reverted_by"),
    }


def level_for(score: int) -> str:
    return "fragile" if score >= FRAGILE else "caution" if score >= CAUTION else "stable"


def _score(fixes: int, reverts: int, security: int, hotfixes: int, commits: int,
           authors: int, tripwires: int, couplings: int) -> int:
    """0-100 fragility. Fixes and reverts dominate; churn and head-count only nudge (log scale)."""
    fixes_eff = min(fixes, 6) + math.sqrt(max(0, fixes - 6))
    weight = (
        3.0 * fixes_eff + 6.0 * min(reverts, 4) + 5.0 * min(security, 3) + 2.0 * min(hotfixes, 3)
        + 2.0 * math.log1p(commits) + 2.0 * math.log1p(max(0, authors - 2))
        + 1.5 * min(tripwires, 4) + 2.0 * min(couplings, 2)
    )
    return int(round(100 * (1 - math.exp(-weight / 18.0))))


def _link_docs(state: Dict, live: Dict[str, Dict]) -> Dict[str, List[str]]:
    """doc path -> current file paths it refers to (directly, or via commits citing the ADR)."""
    by_previous = {old: path for path, rec in state["files"].items() for old in rec["previous"]}
    by_base: Dict[str, List[str]] = {}
    for path in live:
        by_base.setdefault(path.rsplit("/", 1)[-1], []).append(path)
    live_paths = list(live)

    def resolve(token: str) -> Optional[str]:
        token = token.lstrip("./")
        if token in live:
            return token
        if token in by_previous and by_previous[token] in live:
            return by_previous[token]
        if "/" in token:
            hits = [p for p in live_paths if p.endswith("/" + token)]
            return hits[0] if len(hits) == 1 else None
        hits = by_base.get(token, [])
        return hits[0] if len(hits) == 1 else None

    adr_by_number = {
        int(doc["number"]): path for path, doc in state["docs"].items()
        if doc["kind"] == "adr" and doc.get("number")
    }
    links: Dict[str, List[str]] = {}
    for path, doc in state["docs"].items():
        found = []
        for token in doc.get("paths", []):
            hit = resolve(token)
            if hit and hit != path and hit not in found:
                found.append(hit)
        # A README that names twenty files is an index, not an explanation of any of them.
        links[path] = found if (doc["kind"] == "adr" or len(found) <= MAX_DOC_LINKS) else []
    # Commits that cite "ADR-0003" tie that ADR to the files they changed.
    if adr_by_number:
        for commit in state["commits"].values():
            numbers = {int(n) for n in ADR_MENTION_RE.findall(commit["subject"] + "\n" + commit["body"])}
            hits = [adr_by_number[n] for n in numbers if n in adr_by_number]
            if not hits or len(commit["files"]) > 15:
                continue
            for doc_path in hits:
                for entry in commit["files"]:
                    target = entry[0] if entry[0] in live else by_previous.get(entry[0])
                    if target in live and target != doc_path and target not in links[doc_path]:
                        links[doc_path].append(target)
    return links


def _doc_ref(path: str, doc: Dict) -> Dict:
    label = f"ADR-{doc['number']}" if doc.get("number") else ("ADR" if doc["kind"] == "adr" else "Doc")
    return {
        "path": path, "kind": doc["kind"], "label": label, "title": doc["title"],
        "status": doc.get("status"), "date": doc.get("date"), "context": doc.get("context", ""),
        "decision": doc.get("decision", ""), "consequences": doc.get("consequences", ""),
        "invariants": doc.get("invariants", []), "traps": doc.get("traps", []),
    }


def _ordered_commits(rec: Dict, commits_all: Dict[str, Dict]) -> List[Dict]:
    """This file's commits, oldest first, each once."""
    seen, commits = set(), []
    for sha in rec["commits"]:
        if sha in commits_all and sha not in seen:
            seen.add(sha)
            commits.append(commits_all[sha])
    order = {sha: i for i, sha in enumerate(rec["commits"])}
    commits.sort(key=lambda c: (c["date"], order[c["sha"]]))
    return commits


def _change_note(entry: Optional[List], first: bool) -> Optional[str]:
    note = None
    if entry and entry[3] == "R" and entry[4]:
        note = f"renamed from {entry[4]}"
    elif entry and entry[3] == "D":
        note = "deleted"
    return "born" if first and not note else note


def _timeline_event(commit: Dict, entry: Optional[List], first: bool) -> Dict:
    event = brief(commit)
    event["note"] = _change_note(entry, first)
    event["delta"] = [entry[1], entry[2]] if entry else [0, 0]
    # A fix does not count against this file when the file was only created by it, or
    # was barely touched in a commit that mostly changed other files (a version bump
    # riding along with a real fix).
    changed = sum(event["delta"])
    # Does a remark in this commit's message (or its pull request) belong to this file?
    # Yes for a small commit, or when this file carries a real share of the change; a
    # sweeping commit's warnings are not repeated on every file it brushed.
    total = sum(e[1] + e[2] for e in commit["files"]) or 1
    event["local"] = len(commit["files"]) <= 6 or changed / total >= 0.25
    event["incidental"] = bool(
        commit["kind"] in RISKY_KINDS
        and (first or (len(commit["files"]) > 3 and changed <= 2))
    )
    return event


def _timeline(commits: List[Dict], names: set) -> Tuple[List[Dict], int]:
    """One event per commit, and the total lines added and removed in this file."""
    churn = 0
    timeline = []
    for idx, commit in enumerate(commits):
        entry = next((e for e in commit["files"] if e[0] in names), None)
        if entry:
            churn += entry[1] + entry[2]
        timeline.append(_timeline_event(commit, entry, idx == 0))
    return timeline, churn


def _coupling(path: str, state: Dict, ignored: Callable[[str], bool], commit_count: int) -> List[Dict]:
    """Co-change: what else moves when this file moves. Jaccard overlap keeps out the files
    that change with everything (a README touched by half of all commits is not coupling)."""
    coupled: List[Dict] = []
    total = max(1, commit_count)
    for other, n in sorted(state["cochange"].get(path, {}).items(), key=lambda kv: (-kv[1], kv[0])):
        other_rec = state["files"].get(other)
        if not other_rec or other_rec["deleted"] or ignored(other):
            continue
        if other.rsplit("/", 1)[-1].lower().startswith(COUPLING_NOISE):
            continue  # everything "changes with" the changelog
        ratio = n / total
        other_total = len(set(other_rec["commits"]))
        overlap = n / max(1, total + other_total - n)
        if n >= 2 and ratio >= 0.3 and overlap >= 0.25:
            coupled.append({"path": other, "count": n, "of": total, "ratio": round(ratio, 2),
                            "overlap": round(overlap, 2),
                            "strong": n >= 3 and ratio >= 0.5 and overlap >= 0.4})
        if len(coupled) >= 6:
            break
    return coupled


def _counted(n: int, word: str, plural: str = "s") -> str:
    return f"{n} {word}{plural if n != 1 else ''}"


def _reasons(fixes: List, reverts: List, security: List, hotfixes: List, authors: Counter,
             strong: List[Dict], tripwires: List) -> List[str]:
    counts = (
        (len(fixes), "bug fix", "es"), (len(reverts), "revert", "s"),
        (len(security), "security change", "s"), (len(hotfixes), "hotfix", "es"),
    )
    reasons = [_counted(n, word, plural) for n, word, plural in counts if n]
    if len(authors) > 2:
        reasons.append(f"{len(authors)} authors")
    if strong:
        reasons.append(f"tightly coupled to {strong[0]['path']}")
    if tripwires:
        reasons.append(_counted(len(tripwires), "warning comment"))
    return reasons


def _intent(timeline: List[Dict]) -> List[Dict]:
    """The birth commit, the redesigns, and any decision record that names this file."""
    intent = []
    if timeline:
        born = timeline[0]
        story = [born["lead"]] if born.get("lead") else []
        story += [w for w in born["why"] if w not in story]
        story += [f"{n['text']} (PR #{n['pr']})" for n in born["pr_notes"] if n["kind"] in ("why", "lead")]
        story += [f"Asked for in #{n['issue']}: {n['title']}" for n in born["issue_notes"]]
        intent.append({"source": "commit", "ref": born["short"], "date": born["date"],
                       "author": born["author"], "text": born["title"], "why": story[:2]})
    for event in timeline[1:]:
        if event["kind"] == "design" or "design" in event["tags"]:
            intent.append({"source": "commit", "ref": event["short"], "date": event["date"],
                           "author": event["author"], "text": event["title"], "why": event["why"][:1]})
    return intent[:1] + intent[1:][-3:]


def _undone_or_fixed(event: Dict, commits_all: Dict[str, Dict]) -> List[Dict]:
    """The pitfall an event is in itself: something tried and reverted, or a fix."""
    if event["reverted_by"]:
        by = commits_all.get(event["reverted_by"])
        reason = (by["why"][0] if by and by.get("why") else "")
        return [{
            "kind": "reverted", "date": event["date"], "ref": event["short"],
            "text": f"Tried and reverted: {event['title']}" + (f" — {reason}" if reason else ""),
        }]
    if event["kind"] in ("fix", "security") or (event["kind"] == "revert" and not event["reverts"]):
        why = event["why"][0] if event["why"] else ""
        return [{
            "kind": event["kind"], "date": event["date"], "ref": event["short"],
            "text": event["title"] + (f" — {why}" if why else ""),
        }]
    return []


def _issue_pitfalls(event: Dict, said: set) -> List[Dict]:
    """The issue a fix closed is the problem report: what went wrong, as it was seen."""
    out = []
    for note in event["issue_notes"]:
        if ("issue", note["issue"]) in said:
            continue
        said.add(("issue", note["issue"]))
        text = "Reported: " + note["title"]
        if note["lead"] and note["lead"].lower() != note["title"].lower():
            text += " — " + note["lead"]
        out.append({"kind": "issue", "date": event["date"], "ref": f"#{note['issue']}", "text": text})
        for extra in note["warn"] + [f"{d['author']}: {d['text']}" for d in note["discussion"]]:
            out.append({"kind": "issue", "date": event["date"], "ref": f"#{note['issue']}", "text": extra})
    return out


def _pr_pitfalls(event: Dict, risky: bool, belongs: Callable[[str], bool], said: set) -> List[Dict]:
    """A pull request's warnings always matter; its reasons and review remarks only for
    the changes that fixed or undid something."""
    out = []
    for note in event["pr_notes"]:
        if (note["pr"], note["text"]) in said or not belongs(note["text"]):
            continue
        if note["kind"] == "warn" or (risky and note["kind"] in ("review", "why")):
            said.add((note["pr"], note["text"]))
            label = "review" if note["kind"] == "review" else "warning" if note["kind"] == "warn" else event["kind"]
            who = f"{note['author']}: " if note["kind"] == "review" and note["author"] else ""
            out.append({"kind": label, "date": event["date"], "ref": f"PR #{note['pr']}",
                        "text": who + note["text"]})
    return out


def _event_pitfalls(event: Dict, stem: str, commits_all: Dict[str, Dict], said: set) -> List[Dict]:
    def belongs(text: str) -> bool:
        return event["local"] or (len(stem) >= 4 and stem in text.lower())

    out = _undone_or_fixed(event, commits_all)
    out += [{"kind": "warning", "date": event["date"], "ref": event["short"], "text": warning}
            for warning in event["warn"] if belongs(warning)]
    risky = event["kind"] in RISKY_KINDS or bool(event["reverted_by"])
    if risky:
        out += _issue_pitfalls(event, said)
    return out + _pr_pitfalls(event, risky, belongs, said)


def _standing_pitfalls(tripwires: List[Dict], strong: List[Dict], file_docs: List[Dict],
                       commits_all: Dict[str, Dict]) -> List[Dict]:
    """Hazards that are true of the file now: warning comments, coupling, decision records."""
    out = []
    for wire in tripwires:
        owner = commits_all.get(wire.get("sha") or "")
        out.append({
            "kind": "comment", "date": owner["date"][:10] if owner else "",
            "ref": owner["sha"][:7] if owner else "", "text": f"L{wire['line']}: {wire['text']}",
        })
    for link in strong:
        out.append({
            "kind": "coupling", "date": "", "ref": "",
            "text": f"Changes together with {link['path']} ({link['count']} of {link['of']} commits) — check it when editing this file.",
        })
    for doc in file_docs:
        for trap in doc.get("traps", []):
            out.append({"kind": "note", "date": doc.get("date") or "", "ref": doc["path"], "text": trap})
        if doc["kind"] == "adr" and doc.get("consequences"):
            out.append({"kind": "decision", "date": doc.get("date") or "", "ref": doc["label"],
                        "text": f"{doc['title']}: {doc['consequences']}"})
    return out


def _fixes_by_symbol(timeline: List[Dict], commits: List[Dict], names: set) -> Dict[str, List[Dict]]:
    """Fixes that ever touched a function by name (from the hunk headers of the commit)."""
    hint_fixes: Dict[str, List[Dict]] = {}
    for event, commit in zip(timeline, commits):
        if commit["kind"] not in RISKY_KINDS and commit["kind"] != "perf":
            continue
        for hinted_path, sym_names in commit.get("symbols", {}).items():
            if hinted_path in names:
                for sym in sym_names:
                    hint_fixes.setdefault(sym, []).append(event)
    return hint_fixes


def _symbol_report(sym: Dict, all_symbols: List[Dict], commits_all: Dict[str, Dict],
                   hint_fixes: Dict[str, List[Dict]]) -> Optional[Dict]:
    """Who owns the surviving lines of one function, and the fixes made to it."""
    owners = [commits_all[s] for s in sym["commits"] if s in commits_all]
    if not owners:
        return None
    owners.sort(key=lambda c: c["date"])
    short_name = sym["name"].rsplit(".", 1)[-1]
    # A fix that *wrote* the function is its origin, not a fix to it.
    origin_sha = owners[0]["sha"]
    risky = {c["sha"]: brief(c) for c in owners if c["kind"] in RISKY_KINDS and c["sha"] != origin_sha}
    for event in hint_fixes.get(short_name, []):
        if event["sha"] != origin_sha and event["date"] >= owners[0]["date"][:10]:
            risky.setdefault(event["sha"], event)
    history = sorted(risky.values(), key=lambda e: e["date"])
    container = any(o["name"].startswith(sym["name"] + ".") for o in all_symbols)
    return {
        "name": sym["name"], "kind": sym["kind"], "start": sym["start"], "end": sym["end"],
        "container": container,  # a class whose methods are listed separately
        "origin": brief(owners[0]), "last": brief(owners[-1]),
        "fixes": [{k: e[k] for k in ("short", "date", "kind", "title", "why")} for e in history],
    }


def _tally(timeline: List[Dict]) -> Dict[str, List[Dict]]:
    """The events that count against a file, by what they were."""
    counted = [e for e in timeline if not e["incidental"]]
    return {
        "counted": counted,
        "fixes": [e for e in counted if e["kind"] == "fix"],
        "security": [e for e in counted if e["kind"] == "security"],
        "reverts": [e for e in counted if e["kind"] == "revert"],
        "hotfixes": [e for e in counted if "hotfix" in e["tags"]],
    }


def _file_score(tally: Dict[str, List[Dict]], commits: int, authors: int, tripwires: List, strong: List, prose: bool) -> int:
    fixes, reverts, security, hotfixes = (tally[k] for k in ("fixes", "reverts", "security", "hotfixes"))
    score = _score(len(fixes), len(reverts), len(security), len(hotfixes), commits, authors, len(tripwires), len(strong))
    if not (fixes or reverts or security or hotfixes or tripwires):
        score = min(score, CAUTION - 1)  # churn and coupling alone never make a file "caution"
    return 0 if prose else score  # documentation is not scored; its history is still shown


def _pitfalls(path: str, counted: List[Dict], tripwires: List[Dict], strong: List[Dict], file_docs: List[Dict],
              commits_all: Dict[str, Dict]) -> List[Dict]:
    """Everything a future editor should know went wrong or is easy to get wrong."""
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    said: set = set()  # a pull request with five commits should say its piece once per file
    pitfalls = [p for event in counted for p in _event_pitfalls(event, stem, commits_all, said)]
    return pitfalls + _standing_pitfalls(tripwires, strong, file_docs, commits_all)


def _symbols(blame: Dict, hint_fixes: Dict[str, List[Dict]], commits_all: Dict[str, Dict]) -> List[Dict]:
    all_symbols = blame.get("symbols", [])
    reports = (_symbol_report(sym, all_symbols, commits_all, hint_fixes) for sym in all_symbols)
    return [report for report in reports if report]


def _file_report(path: str, rec: Dict, state: Dict, ignored: Callable[[str], bool],
                 file_docs: List[Dict]) -> Dict:
    commits_all = state["commits"]
    names = {path, *rec["previous"]}
    commits = _ordered_commits(rec, commits_all)
    timeline, churn = _timeline(commits, names)
    tally = _tally(timeline)
    fixes, reverts, security, hotfixes = (tally[k] for k in ("fixes", "reverts", "security", "hotfixes"))
    authors = Counter(e["author"] for e in timeline)
    blame = state["blame"].get(path, {})
    tripwires = blame.get("tripwires", [])
    coupled = _coupling(path, state, ignored, len(commits))
    strong = [c for c in coupled if c["strong"]]
    prose = is_prose(path)
    score = _file_score(tally, len(commits), len(authors), tripwires, strong, prose)
    pitfalls = _pitfalls(path, tally["counted"], tripwires, strong, file_docs, commits_all)
    symbols = _symbols(blame, _fixes_by_symbol(timeline, commits, names), commits_all)

    deleted = commits_all.get(rec["deleted"] or "")
    return {
        "path": path,
        "status": "deleted" if rec["deleted"] else "live",
        "deleted": brief(deleted) if deleted else None,
        "previous": rec["previous"],
        "commits": len(commits),
        "fixes": len(fixes),
        "reverts": len(reverts),
        "security": len(security),
        "churn": churn,
        "lines": blame.get("lines"),
        "authors": [name for name, _n in authors.most_common(4)],
        "author_count": len(authors),
        "first": timeline[0]["date"] if timeline else "",
        "last": timeline[-1]["date"] if timeline else "",
        "score": score,
        "level": level_for(score),
        "rank": None,
        "ranked_of": None,
        "prose": prose,
        "reasons": _reasons(fixes, reverts, security, hotfixes, authors, strong, tripwires),
        "intent": _intent(timeline),
        "docs": file_docs,
        "timeline": timeline,
        "pitfalls": pitfalls,
        "coupled": coupled,
        "symbols": symbols,
    }


def build(state: Dict, repo_name: str, head_date: str, ignore_patterns: Optional[List[str]] = None) -> Dict:
    global _PRS, _ISSUES
    _PRS = state.get("prs", {})
    _ISSUES = state.get("issues", {})
    ignored = make_ignore(DEFAULT_IGNORES + list(ignore_patterns or []))
    tracked = {p: r for p, r in state["files"].items() if not ignored(p)}
    live = {p: r for p, r in tracked.items() if not r["deleted"]}

    links = _link_docs(state, live)
    docs_for_file: Dict[str, List[Dict]] = {}
    docs = {}
    for doc_path, doc in sorted(state["docs"].items()):
        ref = _doc_ref(doc_path, doc)
        ref["files"] = sorted(links.get(doc_path, []))
        if doc["kind"] != "adr" and not ref["files"]:
            continue  # a Markdown file that explains no code is not archaeology
        docs[doc_path] = ref
        for target in ref["files"]:
            docs_for_file.setdefault(target, []).append({k: v for k, v in ref.items() if k != "files"})

    files = {
        path: _file_report(path, rec, state, ignored, docs_for_file.get(path, []))
        for path, rec in sorted(tracked.items())
    }
    # Rank scored files against each other: on an old repository the order says more than
    # the label does.
    scored = sorted(
        (f for f in files.values() if f["status"] == "live" and not f["prose"]),
        key=lambda f: (-f["score"], -f["commits"], f["path"]),
    )
    for position, f in enumerate(scored, start=1):
        f["rank"], f["ranked_of"] = position, len(scored)
    kinds = Counter(c["kind"] for c in state["commits"].values())
    dates = sorted(c["date"] for c in state["commits"].values())
    return {
        "meta": {
            "repo": repo_name,
            "head": state.get("last_commit") or state["head"],
            "head_date": head_date[:10],
            "commits": len(state["commits"]),
            "merges": state.get("merges", 0),
            "first_commit": dates[0][:10] if dates else "",
            "files_live": sum(1 for f in files.values() if f["status"] == "live"),
            "files_deleted": sum(1 for f in files.values() if f["status"] == "deleted"),
            "kinds": dict(sorted(kinds.items())),
            "truncated": bool(state.get("truncated")),
            "prs_read": sum(1 for p in state.get("prs", {}).values() if p.get("state") != "missing"),
            "issues_read": sum(1 for p in state.get("issues", {}).values() if p.get("state") != "missing"),
            "thresholds": {"fragile": FRAGILE, "caution": CAUTION},
        },
        "files": files,
        "docs": docs,
    }


def ranked(report: Dict, live_only: bool = True) -> List[Dict]:
    files = [f for f in report["files"].values() if f["status"] == "live" or not live_only]
    return sorted(files, key=lambda f: (-f["score"], -f["commits"], f["path"]))


def _landmark_events(chosen: List[Dict], max_commits: int) -> List[Dict]:
    """Landmark commits: the ones a newcomer would want explained. Risky ones are kept first."""
    landmark: Dict[str, Dict] = {}
    for f in chosen:
        for i, event in enumerate(f["timeline"]):
            if i == 0 or event["kind"] in LANDMARK_KINDS or event["reverted_by"] or "design" in event["tags"]:
                landmark.setdefault(event["sha"], event)
    keep = sorted(
        landmark.values(),
        key=lambda e: (e["kind"] not in RISKY_KINDS and not e["reverted_by"], e["date"]),
    )
    risky = [e for e in keep if e["kind"] in RISKY_KINDS or e["reverted_by"]]
    rest = [e for e in keep if not (e["kind"] in RISKY_KINDS or e["reverted_by"])]
    return (risky[-max_commits:] + rest[-max(0, max_commits - len(risky)):])[:max_commits]


def _commit_node(event: Dict, commit: Dict) -> Dict:
    return {
        "id": "c:" + event["sha"], "type": "commit", "label": event["short"], "date": event["date"],
        "kind": "reverted" if event["reverted_by"] else event["kind"], "title": event["title"],
        "author": event["author"], "why": event["why"], "warn": event["warn"],
        "prs": event["prs"], "issues": event["issues"], "tickets": event["tickets"],
        "body": commit["body"][:600], "pr_notes": event["pr_notes"],
        "issue_notes": [{"issue": n["issue"], "title": n["title"], "lead": n["lead"]} for n in event["issue_notes"]],
    }


def _commit_edges(event: Dict, commit: Dict, chosen_paths: set, by_previous: Dict[str, str], keep_shas: set) -> List[Dict]:
    """The drawn files a commit touched (under their current names), and the commit it reverts."""
    targets = set()
    for entry in commit["files"]:
        target = entry[0] if entry[0] in chosen_paths else by_previous.get(entry[0])
        if target in chosen_paths:
            targets.add(target)
    edges = [{"s": "c:" + event["sha"], "t": "f:" + target, "type": "touch"} for target in sorted(targets)]
    if commit.get("reverts") and commit["reverts"] in keep_shas:
        edges.append({"s": "c:" + event["sha"], "t": "c:" + commit["reverts"], "type": "revert"})
    return edges


def _doc_nodes(report: Dict, chosen_paths: set) -> Tuple[List[Dict], List[Dict]]:
    nodes, edges = [], []
    for path, doc in report["docs"].items():
        targets = [t for t in doc["files"] if t in chosen_paths]
        if not targets and doc["kind"] != "adr":
            continue
        nodes.append({
            "id": "d:" + path, "type": "doc", "label": doc["label"] if doc["kind"] == "adr" else path.rsplit("/", 1)[-1],
            "path": path, "kind": doc["kind"], "title": doc["title"], "status": doc["status"],
            "date": doc["date"] or "", "context": doc["context"], "decision": doc["decision"],
            "consequences": doc["consequences"],
        })
        edges += [{"s": "d:" + path, "t": "f:" + target, "type": "explains"} for target in targets]
    return nodes, edges


def _coupling_edges(chosen: List[Dict], chosen_paths: set) -> List[Dict]:
    edges = []
    seen_pairs = set()
    for f in chosen:
        for link in f["coupled"]:
            if link["path"] not in chosen_paths or not link["strong"]:
                continue  # only draw couplings strong enough to act on
            pair = tuple(sorted((f["path"], link["path"])))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            edges.append({"s": "f:" + pair[0], "t": "f:" + pair[1], "type": "coupled", "w": link["count"]})
    return edges


def _file_detail(f: Dict) -> Dict:
    """What the graph's side panel shows for one file."""
    return {
        "path": f["path"], "score": f["score"], "level": f["level"], "reasons": f["reasons"],
        "rank": f["rank"], "ranked_of": f["ranked_of"],
        "commits": f["commits"], "fixes": f["fixes"], "reverts": f["reverts"],
        "authors": f["authors"], "first": f["first"], "last": f["last"], "previous": f["previous"],
        "intent": f["intent"], "pitfalls": f["pitfalls"][-14:], "coupled": f["coupled"],
        "docs": [{k: d[k] for k in ("path", "label", "title", "status")} for d in f["docs"]],
        "timeline": [
            {k: e[k] for k in ("short", "date", "kind", "title", "author", "note", "reverted_by")}
            for e in f["timeline"][-40:]
        ],
        "symbols": [
            {"name": s["name"], "start": s["start"], "end": s["end"], "origin": s["origin"]["title"],
             "origin_date": s["origin"]["date"], "fixes": s["fixes"][-4:]}
            for s in [x for x in f["symbols"] if x["fixes"] and not x["container"]][:25]
        ],
    }


def graph(report: Dict, state: Dict, max_files: int = 250, max_commits: int = 400) -> Dict:
    """Node/edge payload for the HTML graph: files, landmark commits, decision records."""
    # Decision records get their own diamond nodes, and empty files (bare __init__.py) carry
    # no story, so neither is drawn as a file circle.
    chosen = [
        f for f in ranked(report)
        if f["path"] not in report["docs"] and f["lines"] != 0
    ][:max_files]
    chosen_paths = {f["path"] for f in chosen}
    nodes = [{
        "id": "f:" + f["path"], "type": "file", "label": f["path"].rsplit("/", 1)[-1],
        "path": f["path"], "date": f["first"], "score": f["score"], "level": f["level"],
        "size": f["commits"],
    } for f in chosen]
    edges: List[Dict] = []

    keep = _landmark_events(chosen, max_commits)
    keep_shas = {e["sha"] for e in keep}
    by_previous = {old: p for p, rec in state["files"].items() for old in rec["previous"]}
    for event in sorted(keep, key=lambda e: e["date"]):
        commit = state["commits"][event["sha"]]
        nodes.append(_commit_node(event, commit))
        edges += _commit_edges(event, commit, chosen_paths, by_previous, keep_shas)

    doc_nodes, doc_edges = _doc_nodes(report, chosen_paths)
    nodes += doc_nodes
    edges += doc_edges + _coupling_edges(chosen, chosen_paths)
    return {"meta": report["meta"], "nodes": nodes, "edges": edges, "files": {f["path"]: _file_detail(f) for f in chosen}}
