#!/usr/bin/env python3
"""Does a change to the code change what the tool says? Find out on real repositories.

    python scripts/behaviour_check.py [--quick] <base-ref> <repo> [<repo> ...]

Runs the tool from <base-ref> and from the working tree over a copy of each
repository, saves everything a user could see, and compares the two. Use it
before pushing a refactor: "no behaviour change" should be a measurement.

What one snapshot covers:
  - `dig` at an older commit, then again at HEAD (the incremental path);
    the index, the graph, the full model as JSON, and the decoded cache
  - `explain` for files (plain, --all, --json), for functions, for
    directories, and for a path that does not exist
  - `hook --plain` at each level, the Claude Code hook event (twice, to cover
    "already briefed"), `install-hook --print`, and `status`
  - `dig --full --no-blame --max-commits` with a small graph

  - the same model with invented pull requests and issues attached, so the
    code that uses them runs offline: the report, the graph, the index, and
    every file, function and directory explanation, plain and coloured

--quick skips the per-file command-line calls (the slow part) and keeps the
`dig` runs and the invented-pull-request model: use it while working, and the
full check before pushing.

Not covered: fetching from GitHub (`dig --prs` itself), `install-hook` writing
to disk, and anything a repository in the sample does not contain. A clean
result is evidence, not proof.
"""

from __future__ import annotations

import contextlib
import filecmp
import gzip
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

STEP_BACK = 25  # commits between the first dig and the incremental one
TIMING = re.compile(r"\b\d+\.\d+s\b")


def run_cli(argv, stdin=""):
    """Call the tool in-process; return what it printed and its exit code."""
    from pr_archaeologist import cli

    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as exc:  # argparse errors
                code = exc.code
    finally:
        sys.stdin = old_stdin
    return f"exit {code}\n--- stdout\n{out.getvalue()}--- stderr\n{TIMING.sub('<time>', err.getvalue())}"


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True).stdout.strip()


def sample_files(report):
    """Every file in a small repository; the busiest plus a spread in a large one."""
    files = sorted(report["files"].values(), key=lambda f: (-f["score"], f["path"]))
    picked = files if len(files) <= 40 else files[:16] + files[16 :: max(1, (len(files) - 16) // 12)]
    return [f["path"] for f in picked]


REMARKS = (
    "Never call {s} twice because it double counts.",
    "The {s} module must stay in sync with the cache.",
    "This was needed since the old path dropped events.",
    "Careful: retries here are not idempotent.",
    "Reported as a crash on empty input in {s}.",
    "We chose a queue rather than a lock to avoid stalls.",
)


def attach_invented_prs(state, seed):
    """Give the cached history pull requests and issues in the shape prs.py stores them."""
    from pr_archaeologist import prs

    rng = random.Random(seed)
    stems = sorted({p.rsplit("/", 1)[-1].rsplit(".", 1)[0] for p in state["files"]}) or ["core"]
    people = ["ana", "bo", "cy", "dependabot[bot]"]

    def remark():
        return rng.choice(REMARKS).format(s=rng.choice(stems))

    def remarks(most):
        return [remark() for _ in range(rng.randint(0, most))]

    def thread(most):
        return [{"author": rng.choice(people), "text": remark(), "own": rng.random() < 0.3} for _ in range(rng.randint(0, most))]

    issue_numbers = set()
    for index, sha in enumerate(sorted(state["commits"])):
        commit = state["commits"][sha]
        if not commit.get("prs") and index % 3 == 0:
            commit["prs"] = [90000 + index]
        issue_numbers.update(prs.closing_refs(commit["subject"] + "\n" + commit.get("body", "")))
    state["prs"] = {}
    for number in sorted({n for c in state["commits"].values() for n in c.get("prs", [])}):
        if rng.random() < 0.1:
            state["prs"][str(number)] = {"number": number, "state": "missing"}
            continue
        closes = [number + 500000] if rng.random() < 0.5 else []
        issue_numbers.update(closes)
        state["prs"][str(number)] = {
            "number": number, "title": remark(), "author": rng.choice(people), "url": "", "state": "merged",
            "lead": remark() if rng.random() < 0.7 else None, "why": remarks(3), "warn": remarks(2),
            "discussion": thread(2), "closes": closes,
        }
    state["issues"] = {}
    for number in sorted(issue_numbers):
        title = remark()
        lead = rng.choice([None, title, remark()])
        state["issues"][str(number)] = {
            "number": number, "title": title, "author": rng.choice(people), "url": "",
            "state": rng.choice(["closed", "closed", "open", "missing"]), "lead": lead, "warn": remarks(1), "discussion": thread(2),
        }


def model_snapshot(repo, out):
    """Everything the model and the renderers produce, with invented pull requests attached."""
    from pr_archaeologist import cache, cli, explain, hook, model, render_html, render_md

    state = cache.load(repo)
    attach_invented_prs(state, seed=os.path.basename(repo))
    report = cli._report(repo, state)
    payload = model.graph(report, state, max_files=60)

    def dump(name, text):
        with open(os.path.join(out, "invented-prs-" + name), "w", encoding="utf-8") as fh:
            fh.write(text)

    dump("report.json", json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False))
    dump("graph.json", json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False))
    dump("index.md", render_md.render(report))
    dump("graph.html", render_html.render(payload))
    files = [report["files"][p] for p in sorted(report["files"])][:600]
    for color in (False, True):
        for show_all in (False, True):
            dump(f"explain-color{int(color)}-all{int(show_all)}.txt",
                 "\n=====\n".join(explain.render_file(f, color=color, show_all=show_all) for f in files))
        functions = [explain.render_function(f, s["name"], color=color) or "(none)" for f in files for s in f["symbols"][:3]]
        dump(f"functions-color{int(color)}.txt", "\n=====\n".join(functions))
        directories = sorted({p.split("/")[0] for p in report["files"] if "/" in p})
        listings = [explain.render_dir(d, [f for f in files if f["path"].startswith(d + "/")], color=color) for d in directories]
        dump(f"directories-color{int(color)}.txt", "\n=====\n".join(listings))
    briefs = [f"{f['path']} {[hook.worth_saying(f, lvl) for lvl in ('any', 'caution', 'fragile')]}\n{hook.brief(f)}" for f in files]
    dump("hook-briefs.txt", "\n=====\n".join(briefs))


def snapshot(repo, out, quick=False):
    os.makedirs(out, exist_ok=True)
    counter = [0]

    def save(label, text):
        counter[0] += 1
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:90]
        with open(os.path.join(out, f"{counter[0]:04d}-{name}.txt"), "w", encoding="utf-8") as fh:
            fh.write(text.replace(repo, "<repo>"))  # the two runs use different copies

    def keep_outputs(stage):
        for name in ("CODEBASE_ARCHAEOLOGY.md", "archaeology-graph.html", "model.json"):
            path = os.path.join(repo, name)
            if os.path.exists(path):
                shutil.copy(path, os.path.join(out, f"{stage}-{name}"))
        with gzip.open(os.path.join(repo, ".pr-archaeologist", "cache.json.gz"), "rt", encoding="utf-8") as fh:
            state = json.load(fh)
        with open(os.path.join(out, f"{stage}-cache.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)

    head = git(repo, "rev-parse", "HEAD")
    commits = int(git(repo, "rev-list", "--count", "HEAD") or 0)
    older = git(repo, "rev-parse", "--verify", "--quiet", f"HEAD~{min(STEP_BACK, commits // 2)}") if commits > 1 else ""
    if older:
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--detach", older], check=True)
        save("dig-older", run_cli(["dig", "--repo", repo, "--json", "model.json"]))
        keep_outputs("older")
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--detach", head], check=True)
    save("dig-head", run_cli(["dig", "--repo", repo, "--json", "model.json"]))
    keep_outputs("head")

    with open(os.path.join(repo, "model.json"), encoding="utf-8") as fh:
        report = json.load(fh)
    model_snapshot(repo, out)
    if quick:
        return counter[0]
    os.chdir(repo)
    paths = sample_files(report)
    for path in paths:
        save(f"explain {path}", run_cli(["explain", path, "--no-refresh"]))
        save(f"explain-all {path}", run_cli(["explain", path, "--no-refresh", "--all"]))
        save(f"explain-json {path}", run_cli(["explain", path, "--no-refresh", "--json"]))
        for level in ("any", "caution", "fragile"):
            save(f"hook-{level} {path}", run_cli(["hook", "--plain", path, "--min-level", level]))
        for symbol in report["files"][path].get("symbols", [])[:2]:
            for flag in ([], ["--json"]):
                save(f"function {path} {symbol['name']}", run_cli(["explain", path, "--no-refresh", "-f", symbol["name"], *flag]))
    for directory in sorted({p.split("/")[0] for p in report["files"] if "/" in p}) + ["."]:
        save(f"explain-dir {directory}", run_cli(["explain", directory, "--no-refresh"]))
        save(f"explain-dir-json {directory}", run_cli(["explain", directory, "--no-refresh", "--json"]))
    save("explain-missing", run_cli(["explain", "no/such/file.py", "--no-refresh"]))
    save("explain-missing-function", run_cli(["explain", paths[0], "--no-refresh", "-f", "no_such_function"]))
    for path in paths[:6] * 2:  # the second pass must stay silent
        event = json.dumps({"hook_event_name": "PreToolUse", "session_id": "check", "cwd": repo, "tool_input": {"file_path": path}})
        save(f"hook-event {path}", run_cli(["hook", "--min-level", "any"], stdin=event))
    save("hook-junk", run_cli(["hook"], stdin="not json"))
    save("install-hook-print", run_cli(["install-hook", "--print", "--min-level", "fragile"]))
    save("status", run_cli(["status"]))
    save("dig-small", run_cli(["dig", "--full", "--no-blame", "--max-commits", "300", "--max-graph-files", "40", "--json", "model.json"]))
    keep_outputs("small")
    return counter[0]


def compare(a, b):
    """Names of outputs that differ between two snapshot directories."""
    cmp = filecmp.dircmp(a, b)
    different = list(cmp.left_only) + list(cmp.right_only)
    _same, changed, errors = filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)
    return sorted(different + changed + errors), len(cmp.common_files)


def main(argv):
    quick = "--quick" in argv
    argv = [a for a in argv if a != "--quick"]
    if len(argv) >= 3 and argv[0] == "snapshot":
        print(snapshot(os.path.abspath(argv[1]), os.path.abspath(argv[2]), quick))
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    base_ref, repos = argv[0], argv[1:]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    work = tempfile.mkdtemp(prefix="behaviour-check-")
    base_src = os.path.join(work, "src-at-base")
    os.makedirs(base_src)
    archive = subprocess.run(["git", "-C", here, "archive", base_ref, "src"], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", base_src], input=archive, check=True)
    sources = {"base": os.path.join(base_src, "src"), "work": os.path.join(here, "src")}

    def take(label, source):
        name = os.path.basename(os.path.abspath(source))
        copy = os.path.join(work, "copy-" + label, name)
        shutil.copytree(source, copy, symlinks=True)
        shutil.rmtree(os.path.join(copy, ".pr-archaeologist"), ignore_errors=True)  # start without a cache
        env = dict(os.environ, PYTHONPATH=sources[label], NO_COLOR="1")
        command = [sys.executable, os.path.abspath(__file__), "snapshot", copy, os.path.join(work, label, name)] + ["--quick"] * quick
        return subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    failed = False
    for source in repos:
        name = os.path.basename(os.path.abspath(source))
        for label, proc in [(label, take(label, source)) for label in sources]:  # both sides at once
            errors = proc.communicate()[1]
            if proc.returncode:
                print(f"{name}: the {label} run crashed\n{errors[-2000:]}")
                return 1
        differing, total = compare(os.path.join(work, "base", name), os.path.join(work, "work", name))
        failed |= bool(differing)
        print(f"{name}: {total} outputs compared, " + (f"{len(differing)} DIFFER" if differing else "all identical"))
        for item in differing[:20]:
            print(f"  {item}")
    if failed:
        print(f"Kept for inspection: {work}")
        return 1
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
