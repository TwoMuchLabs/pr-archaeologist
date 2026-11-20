"""Command line interface.

    pr-archaeologist dig                 analyse (incrementally) and write the graph + index
    pr-archaeologist explain <path>      why this file exists and what went wrong before
    pr-archaeologist status              what the cache knows
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional

from . import __version__, blame, cache, docs, explain as explain_mod, history, model, prs, render_html, render_md
from .gitio import GitError, git, head_sha, repo_root

DEFAULT_INDEX = "CODEBASE_ARCHAEOLOGY.md"
DEFAULT_GRAPH = "archaeology-graph.html"


def _write_if_changed(path: str, content: str) -> bool:
    """Leave the file (and its mtime) alone when nothing changed: keeps nightly diffs empty."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            if fh.read() == content:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return True


def _rel(repo: str, path: str) -> str:
    return os.path.relpath(os.path.join(repo, path), repo).replace(os.sep, "/")


def _ignore_patterns(state: Dict) -> List[str]:
    config = state.get("config", {})
    return list(config.get("ignore", [])) + list(config.get("outputs", []))


def refresh(repo: str, state: Dict, args, say) -> Dict:
    """Bring history, blame and docs up to date; returns what each stage did."""
    config = dict(state.get("config", {}))
    if getattr(args, "ignore", None):
        config["ignore"] = sorted(set(config.get("ignore", [])) | set(args.ignore))
    outputs = {DEFAULT_INDEX, DEFAULT_GRAPH, *config.get("outputs", [])}
    for attr in ("index", "graph", "json"):
        if getattr(args, attr, None):
            outputs.add(_rel(repo, getattr(args, attr)))
    config["outputs"] = sorted(outputs)

    result = {"history": history.update(
        repo, state, full=getattr(args, "full", False),
        max_commits=getattr(args, "max_commits", None), progress=say,
        generated=config["outputs"],
    )}
    state["config"] = config  # a full rebuild resets the state dict
    ignored = model.make_ignore(model.DEFAULT_IGNORES + _ignore_patterns(state))
    if getattr(args, "no_blame", False):
        result["blame"] = {"blamed": 0, "cached": len(state["blame"])}
    else:
        result["blame"] = blame.update(
            repo, state, ignored, max_files=getattr(args, "max_blame_files", 2000), progress=say
        )
    result["docs"] = docs.update(repo, state, ignored, skip=config["outputs"])
    # Pull requests are opt-in (the only network access) and the choice is remembered.
    if getattr(args, "prs", None) is not None:
        config["prs"] = bool(args.prs)
    if config.get("prs") and hasattr(args, "max_prs"):  # only `dig` goes to the network
        result["prs"] = prs.update(repo, state, limit=getattr(args, "max_prs", 300), progress=say)
    return result


def _report(repo: str, state: Dict) -> Dict:
    last = state["commits"].get(state.get("last_commit") or "")
    head_date = last["date"] if last else ""
    return model.build(state, os.path.basename(repo.rstrip("/")), head_date, _ignore_patterns(state))


def cmd_dig(args) -> int:
    repo = repo_root(args.repo)
    say = (lambda _m: None) if args.quiet else (lambda m: print(m, file=sys.stderr))
    started = time.time()
    state = cache.load(repo)
    if args.full:
        remembered = state.get("config", {})
        state = cache.empty_state()
        state["config"] = remembered
    done = refresh(repo, state, args, say)
    cache.save(repo, state)

    report = _report(repo, state)
    index_path = os.path.join(repo, args.index)
    graph_path = os.path.join(repo, args.graph)
    wrote = []
    if _write_if_changed(index_path, render_md.render(report)):
        wrote.append(args.index)
    payload = model.graph(report, state, max_files=args.max_graph_files)
    if _write_if_changed(graph_path, render_html.render(payload)):
        wrote.append(args.graph)
    if args.json:
        if _write_if_changed(os.path.join(repo, args.json), json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False) + "\n"):
            wrote.append(args.json)

    hist, meta = done["history"], report["meta"]
    counts = {lvl: sum(1 for f in report["files"].values() if f["status"] == "live" and f["level"] == lvl)
              for lvl in ("fragile", "caution", "stable")}
    say(
        f"pr-archaeologist: {hist['mode']} run, {hist['new_commits']} new commit(s) "
        f"({meta['commits']} total), {done['blame']['blamed']} file(s) blamed "
        f"({done['blame']['cached']} cached), {done['docs']['parsed']} doc(s) parsed, "
        f"{time.time() - started:.1f}s"
    )
    if "prs" in done:
        got = done["prs"]
        note = f" ({got['skipped']})" if got["skipped"] else ""
        say(f"  pull requests: {got['fetched']} read, {got['cached']} cached{note}")
        if got["issues"] or got["issues_cached"]:
            say(f"  issues they closed: {got['issues']} read, {got['issues_cached']} cached")
    say(f"  files: {counts['fragile']} fragile, {counts['caution']} caution, {counts['stable']} stable")
    say("  wrote: " + (", ".join(wrote) if wrote else "nothing (outputs already up to date)"))
    return 0


def cmd_explain(args) -> int:
    repo = repo_root(args.repo)
    state = cache.load(repo)
    head = head_sha(repo)
    if not args.no_refresh and state.get("head") != head:
        note = "building the cache (first run)" if not state.get("head") else "catching up on new commits"
        print(f"pr-archaeologist: {note}...", file=sys.stderr)
        refresh(repo, state, args, lambda _m: None)
        cache.save(repo, state)
    elif not state.get("head"):
        print("pr-archaeologist: no cache yet; run `pr-archaeologist dig` first.", file=sys.stderr)
        return 2

    report = _report(repo, state)
    kind, found = explain_mod.resolve(report, args.path, repo, os.getcwd())
    color = sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR")
    if kind == "missing":
        print(f"pr-archaeologist: no history found for '{args.path}'.", file=sys.stderr)
        if found:
            print("Did you mean:\n  " + "\n  ".join(found), file=sys.stderr)
        return 2
    if kind == "dir":
        if args.json:
            print(json.dumps([{k: f[k] for k in ("path", "score", "level", "commits", "fixes", "reasons")} for f in found], indent=1))
        else:
            sys.stdout.write(explain_mod.render_dir(args.path, found, color))
        return 0
    if args.function:
        text = explain_mod.render_function(found, args.function, color)
        if text is None:
            names = ", ".join(s["name"] for s in found["symbols"][:30]) or "(none detected)"
            print(f"pr-archaeologist: no function '{args.function}' in {found['path']}. Known: {names}", file=sys.stderr)
            return 2
        if args.json:
            match = [s for s in found["symbols"] if s["name"] == args.function or s["name"].rsplit(".", 1)[-1] == args.function]
            print(json.dumps(match, indent=1, ensure_ascii=False))
        else:
            sys.stdout.write(text)
        return 0
    if args.json:
        print(json.dumps(found, indent=1, ensure_ascii=False))
    else:
        sys.stdout.write(explain_mod.render_file(found, color=color, show_all=args.all))
    return 0


def cmd_status(args) -> int:
    repo = repo_root(args.repo)
    state = cache.load(repo)
    head = head_sha(repo)
    if not state.get("head"):
        print("No cache yet. Run `pr-archaeologist dig`.")
        return 0
    behind = 0
    if state["head"] != head:
        out = git(repo, "rev-list", "--count", f"{state['head']}..{head}", check=False).strip()
        behind = int(out) if out.isdigit() else -1
    size = os.path.getsize(cache.cache_path(repo)) / 1024
    print(f"repo:      {repo}")
    print(f"cache:     {cache.cache_path(repo)} ({size:.0f} KiB)")
    print(f"analysed:  {len(state['commits'])} commits up to {state['head'][:7]}")
    print(f"blame:     {len(state['blame'])} files cached")
    print(f"docs:      {len(state['docs'])} Markdown files cached")
    print("pending:   " + ("up to date" if behind == 0 else "history rewritten; next run rebuilds" if behind < 0 else f"{behind} new commit(s)"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr-archaeologist",
        description="Reconstruct the historical 'why' behind a codebase from git history, blame and ADRs.",
    )
    parser.add_argument("--version", action="version", version=f"pr-archaeologist {__version__}")
    sub = parser.add_subparsers(dest="command")

    dig = sub.add_parser("dig", help="analyse the repo (incrementally) and write the graph and index")
    dig.add_argument("--repo", default=".", help="path inside the repository (default: current directory)")
    dig.add_argument("--full", action="store_true", help="ignore the cache and rebuild from the first commit")
    dig.add_argument("--index", default=DEFAULT_INDEX, help=f"Markdown index path, relative to the repo root (default: {DEFAULT_INDEX})")
    dig.add_argument("--graph", default=DEFAULT_GRAPH, help=f"HTML graph path, relative to the repo root (default: {DEFAULT_GRAPH})")
    dig.add_argument("--json", default=None, metavar="PATH", help="also write the full model as JSON")
    dig.add_argument("--ignore", action="append", default=[], metavar="GLOB", help="extra path glob to leave out (repeatable, remembered)")
    dig.add_argument("--no-blame", action="store_true", help="skip git blame (faster; no function-level mapping)")
    dig.add_argument("--prs", dest="prs", action="store_true", default=None,
                     help="also read the pull requests commits refer to, from GitHub (needs network; remembered)")
    dig.add_argument("--no-prs", dest="prs", action="store_false", help="stop reading pull requests")
    dig.add_argument("--max-prs", type=int, default=300, help="read at most N new pull requests per run (default: 300)")
    dig.add_argument("--max-commits", type=int, default=None, help="on a full build, only read the latest N commits")
    dig.add_argument("--max-blame-files", type=int, default=2000, help="blame at most N files, busiest first (default: 2000)")
    dig.add_argument("--max-graph-files", type=int, default=250, help="file nodes in the HTML graph (default: 250)")
    dig.add_argument("--quiet", "-q", action="store_true", help="no progress output")
    dig.set_defaults(func=cmd_dig)

    exp = sub.add_parser("explain", help="print why a file exists and what went wrong before")
    exp.add_argument("path", help="file or directory, relative to the current directory or the repo root")
    exp.add_argument("--repo", default=".", help="path inside the repository (default: current directory)")
    exp.add_argument("--function", "-f", default=None, metavar="NAME", help="explain one function or class in the file")
    exp.add_argument("--json", action="store_true", help="structured output for agents")
    exp.add_argument("--all", action="store_true", help="show every commit, not just landmarks")
    exp.add_argument("--no-refresh", action="store_true", help="answer from the cache even if it is behind HEAD")
    exp.add_argument("--no-color", action="store_true", help="plain text output")
    exp.set_defaults(func=cmd_explain)

    status = sub.add_parser("status", help="show what the cache covers")
    status.add_argument("--repo", default=".")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except GitError as exc:
        print(f"pr-archaeologist: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
