#!/usr/bin/env python3
"""Does a change to the code change what the tool says? Find out on real repositories.

    python scripts/behaviour_check.py <base-ref> <repo> [<repo> ...]

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

Not covered: `dig --prs` (it needs the network), `install-hook` writing to
disk, coloured terminal output, and anything a repository in the sample does
not contain. A clean result is evidence, not proof.
"""

from __future__ import annotations

import contextlib
import filecmp
import gzip
import io
import json
import os
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


def snapshot(repo, out):
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
    if len(argv) >= 3 and argv[0] == "snapshot":
        print(snapshot(os.path.abspath(argv[1]), os.path.abspath(argv[2])))
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
        command = [sys.executable, os.path.abspath(__file__), "snapshot", copy, os.path.join(work, label, name)]
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
