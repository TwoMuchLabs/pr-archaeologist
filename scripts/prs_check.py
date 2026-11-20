#!/usr/bin/env python3
"""Does a change to prs.py change what it asks GitHub or what it keeps? Replay recorded answers to find out.

    python scripts/prs_check.py [--quick] <base-ref> <recording.json.gz> <repo> [<recording> <repo> ...]

<recording> comes from scripts/record_github.py (the `recordings` branch has two: flask.json.gz
and gin.json.gz, with the commits they were made at in .github/workflows/record-github.yml).
<repo> is a clone of that repository checked out at the recorded commit.

For each pair, prs.update() from <base-ref> and from the working tree are run side by side on:
  - the recorded answers as they are, with several limits, and again once everything is cached
  - the same through the built-in fetcher, and from a repository with no GitHub remote
  - caches with entries dropped, reopened, marked missing, or saved before issues were read
  - a failure of every kind (404, 410, 401, 403, 429, 5xx, no network, bad JSON, wrong shape)
    injected at request after request
  - answers bent into each shape the code tests for
and summarise() / summarise_issue() are compared on every recorded thread, whole and damaged.
Both versions must return the same result, leave the same cache, make the same requests in the
same order and report the same progress. A request that was never recorded is answered 404.
"""
import copy, gzip, importlib, json, os, random, subprocess, sys, tempfile, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
argv = [a for a in sys.argv[1:] if a != "--quick"]
QUICK = len(argv) != len(sys.argv) - 1
if len(argv) < 3 or len(argv) % 2 == 0:
    sys.exit(__doc__)
old_src = tempfile.mkdtemp(prefix="prs-check-")
archive = subprocess.run(["git", "-C", os.path.dirname(HERE), "archive", argv[0], "src"], check=True, capture_output=True).stdout
subprocess.run(["tar", "-x", "-C", old_src], input=archive, check=True)
sys.path.insert(0, os.path.join(old_src, "src")); old = importlib.import_module("pr_archaeologist.prs")
for m in [k for k in sys.modules if k.startswith("pr_archaeologist")]: del sys.modules[m]
sys.path[0] = os.path.join(HERE, "..", "src"); new = importlib.import_module("pr_archaeologist.prs")
from pr_archaeologist import cache, history
assert old.__file__ != new.__file__
NO_REMOTE = tempfile.mkdtemp(prefix="prs-check-norepo-")
subprocess.run(["git", "init", "-q", NO_REMOTE], check=True)
ERR = "__http_error__"
FAILS = [("http", c) for c in (404, 410, 401, 403, 429, 500, 502)] + [("url", 0), ("os", 0), ("value", 0), ("list", 0), ("none", 0), ("empty", 0)]

def fetcher(tape, calls, fail_at=None, fail=None):
    def fetch(path):
        calls.append(path)
        if fail_at is not None and len(calls) - 1 == fail_at:
            kind, code = fail
            if kind == "http": raise urllib.error.HTTPError(path, code, "injected", None, None)
            if kind == "url": raise urllib.error.URLError("injected")
            if kind == "os": raise OSError("injected")
            if kind == "value": raise ValueError("injected")
            return {"list": [], "none": None, "empty": {}}[kind]
        if path not in tape: raise urllib.error.HTTPError(path, 404, "never recorded", None, None)
        answer = tape[path]
        if isinstance(answer, dict) and ERR in answer: raise urllib.error.HTTPError(path, answer[ERR], "recorded", None, None)
        return copy.deepcopy(answer)
    return fetch

def run(mod, repo, state, tape, limit, fail_at=None, fail=None, via_http=False):
    state = copy.deepcopy(state); calls, said = [], []
    fetch = fetcher(tape, calls, fail_at, fail)
    if via_http:
        mod.http_fetcher = lambda token: fetch; mod.find_token = lambda: "t"
        out = mod.update(repo, state, limit=limit, progress=said.append)
    else:
        out = mod.update(repo, state, fetch_json=fetch, limit=limit, progress=said.append)
    return out, state.get("prs"), state.get("issues"), calls, said, state

total = bad = 0; seen = {}
def check(label, *args, **kw):
    global total, bad
    a, b = run(old, *args, **kw), run(new, *args, **kw)
    total += 1
    for key in ("skipped",): seen[a[0][key][:28]] = seen.get(a[0][key][:28], 0) + 1
    if a[:5] != b[:5] or a[5] != b[5]:
        bad += 1
        if bad <= 5: print("MISMATCH", label, a[0], b[0], len(a[3]), len(b[3]))
    return a

for recording, repo in zip(argv[1::2], argv[2::2]):
    name = os.path.basename(recording).split(".")[0]
    tape = json.load(gzip.open(recording, "rt"))
    base = cache.empty_state(); history.update(repo, base, full=True)
    base = {"commits": base["commits"]}
    rng = random.Random(name)
    full = check(f"{name}/full", repo, base, tape, 90)
    print(name, "full replay:", full[0], "requests:", len(full[3]))
    done = full[5]
    for limit in (0, 1, 7, 40, 300):
        check(f"{name}/limit{limit}", repo, base, tape, limit)
    check(f"{name}/again", repo, done, tape, 90)
    check(f"{name}/again-0", repo, done, tape, 0)
    check(f"{name}/via-http", repo, base, tape, 90, via_http=True)
    check(f"{name}/no-remote", NO_REMOTE, base, tape, 90, via_http=True)
    check(f"{name}/no-remote-nothing-wanted", NO_REMOTE, {"commits": {}}, tape, 90, via_http=True)
    check(f"{name}/no-remote-all-cached", NO_REMOTE, done, tape, 90, via_http=True)
    read = {"commits": {k: c for k, c in base["commits"].items() if c.get("prs") and all(str(n) in done["prs"] for n in c["prs"])}, "prs": copy.deepcopy(done["prs"])}
    assert old.wanted_issues(read) and not [n for c in read["commits"].values() for n in c["prs"] if str(n) not in read["prs"]]
    check(f"{name}/no-remote-only-issues-wanted", NO_REMOTE, read, tape, 90, via_http=True)
    check(f"{name}/only-issues-wanted", repo, read, tape, 90)
    check(f"{name}/no-remote-but-fetcher", NO_REMOTE, base, tape, 5)
    # cached entries in every state the code distinguishes
    for trial in range(40):
        state = copy.deepcopy(done)
        for table in ("prs", "issues"):
            for key in rng.sample(sorted(state[table]), min(len(state[table]), 12)):
                what = rng.choice(["drop", "open", "noclose", "missing", "closed"])
                if what == "drop": del state[table][key]
                elif what == "open": state[table][key]["state"] = "open"
                elif what == "noclose": state[table][key].pop("closes", None)
                elif what == "missing": state[table][key] = {"number": int(key), "state": "missing"}
                else: state[table][key]["state"] = "closed"
        check(f"{name}/cached{trial}", repo, state, tape, rng.choice([90, 90, 5, 300]))
    # a failure of every kind at every request of a full run, and at random requests of partial runs
    n = len(full[3])
    for at in list(range(0, min(n, 12 if QUICK else 45))) + rng.sample(range(n), 12 if QUICK else 60) + [n - 1]:
        for fail in FAILS:
            check(f"{name}/fail{at}{fail}", repo, base, tape, 90, fail_at=at, fail=fail)
    # recorded answers bent into the shapes the code checks for
    for trial in range(60):
        bent = copy.deepcopy(tape)
        for path in rng.sample(sorted(bent), 25):
            what = rng.choice(["404", "410", "isPR", "nocomments", "many", "nobody", "notdict", "null"])
            if what in ("404", "410"): bent[path] = {ERR: int(what)}
            elif isinstance(bent[path], dict):
                if what == "isPR": bent[path]["pull_request"] = {"url": "x"}
                elif what == "nocomments": bent[path]["comments"] = 0; bent[path]["review_comments"] = 0
                elif what == "many": bent[path]["comments"] = 3; bent[path]["review_comments"] = 2
                elif what == "notdict": bent[path] = [1]
                elif what == "null": bent[path]["body"] = None; bent[path]["user"] = None; bent[path]["title"] = None
            elif isinstance(bent[path], list) and what == "nobody":
                for item in bent[path]: item["body"] = None
        check(f"{name}/bent{trial}", repo, base, bent, 90)
    # summarise() and summarise_issue() on every recorded thread, whole and damaged
    pulls = {p: v for p, v in tape.items() if isinstance(v, dict) and ERR not in v and "?" not in p}
    lists = [v for p, v in tape.items() if isinstance(v, list)]
    everything = [c for l in lists for c in l]
    for path, item in sorted(pulls.items())[:: 5 if QUICK else 1]:
        threads = [[], everything[:40], rng.sample(everything, min(len(everything), 30))] + [l for l in rng.sample(lists, 4)]
        for comments in threads:
            for variant in range(3):
                it, cs = copy.deepcopy(item), copy.deepcopy(comments)
                if variant == 1:
                    it["body"] = (it.get("body") or "")[: rng.randrange(200)]; it["user"] = None; it["merged_at"] = None
                    for c in cs[::3]: c["user"] = rng.choice([None, {"login": "dependabot[bot]"}, {"login": "x", "type": "Bot"}, {"login": ""}])
                if variant == 2:
                    it = {"number": it.get("number")}; cs = [{"body": c.get("body")} for c in cs]
                for fn in ("summarise", "summarise_issue"):
                    total += 1
                    if getattr(old, fn)(copy.deepcopy(it), copy.deepcopy(cs)) != getattr(new, fn)(it, cs):
                        bad += 1
                        if bad <= 5: print("MISMATCH", fn, path, variant)
print(f"{total} comparisons; mismatches: {bad}; how runs ended: {seen}")
sys.exit(1 if bad else 0)
