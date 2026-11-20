# pr-archaeologist

Reconstruct the historical *why* behind a legacy codebase, and hand it to the next person or coding
agent before they edit a fragile file.

`pr-archaeologist` reads `git log`, `git blame`, commit messages and the Markdown/ADR docs already in
a repository, and maps every file and function to the intent that created it and the bugs that
shaped it. It writes:

- **`archaeology-graph.html`**: a single self-contained interactive knowledge graph (no CDN, no
  build step, works offline and on a phone).
- **`CODEBASE_ARCHAEOLOGY.md`**: an index AI coding agents can read or grep before editing.
- **`pr-archaeologist explain <path>`**: a concise terminal timeline of why a module exists and
  what bit people before.

It has no dependencies beyond Python 3.10+ and the `git` binary, and uses no LLM. It makes no
network calls unless you ask it to read pull requests (`dig --prs`). A nightly run only analyses
commits added since the previous run.

![The interactive graph with payments/retry.py selected](docs/graph.png)

## Quick start

```bash
pipx install git+https://github.com/TwoMuchLabs/pr-archaeologist
# or: pip install git+https://github.com/TwoMuchLabs/pr-archaeologist

cd /path/to/your/repo
pr-archaeologist dig                      # first run reads the full history
pr-archaeologist explain src/billing/retry.py
open archaeology-graph.html
```

Not on PyPI yet, so it installs straight from GitHub.

To try it without a repo of your own, build the bundled demo (a fictional billing service with five
years of history):

```bash
git clone https://github.com/TwoMuchLabs/pr-archaeologist && cd pr-archaeologist
python examples/make_demo_repo.py /tmp/ledgerline
pr-archaeologist dig --repo /tmp/ledgerline
pr-archaeologist explain payments/retry.py --repo /tmp/ledgerline
```

The output of exactly those commands is checked in under
[`examples/sample-output/`](examples/sample-output/).

## What `explain` prints

```text
payments/retry.py
FRAGILE 83/100 · rank 1 of 12 · 10 commits · 5 fixes · 1 revert · 5 authors · 2019-03-18 → 2024-03-18

WHY IT EXISTS
  2019-03-18 78f3385  Add retry wrapper for gateway timeouts — Maya Okafor
      The gateway drops roughly 1 in 100 calls under load.
  ADR-0002: Idempotency keys on every gateway write (Accepted)  docs/adr/0002-idempotency-keys.md

TIMELINE
  2019-03-18 ◆ 78f3385 born     Add retry wrapper for gateway timeouts #12
  2019-06-11 ✖ e3ce84c fix      double charge when a timed-out request had succeeded #31
  2020-09-15 · 6714dc4 other    Cache gateway auth token in retry loop #71  [later reverted]
  2020-09-17 ↺ 2fb2215 revert   Revert "Cache gateway auth token in retry loop (#71)"
  ...

PAST PITFALLS
  ! [reverted] 2020-09-15 6714dc4
      Tried and reverted: Cache gateway auth token in retry loop — The cached token expired
      mid-retry, which caused every attempt after the first to fail with 401 ...

BEFORE YOU EDIT
  - An approach here was already tried and reverted (6714dc4); do not reintroduce it without new
    evidence.
  - Honour ADR-0002, or supersede it explicitly in your PR.
```

(Abridged. Full text: [`explain-payments-retry.txt`](examples/sample-output/explain-payments-retry.txt).)

Useful variants:

| Command | What you get |
|---|---|
| `explain <file> --function NAME` | Origin, last change and every fix that touched one function |
| `explain <file> --json` | The full structured record, for agents and scripts |
| `explain <file> --all` | Every commit, not only the landmarks |
| `explain <directory>` | The directory's files ranked by fragility |
| `explain <old/path.py>` | Works for renamed and deleted files too |

## Commands

```text
pr-archaeologist dig      [--repo PATH] [--full] [--index PATH] [--graph PATH] [--json PATH]
                          [--prs | --no-prs] [--max-prs N]
                          [--ignore GLOB]... [--no-blame] [--max-commits N]
                          [--max-blame-files N] [--max-graph-files N] [--quiet]
pr-archaeologist explain  <path> [--repo PATH] [--function NAME] [--json] [--all]
                          [--no-refresh] [--no-color]
pr-archaeologist status   [--repo PATH]
```

`explain` catches up on new commits by itself before answering, so it is never stale; pass
`--no-refresh` to answer strictly from the cache.

## Pull requests (optional)

Commit messages say what changed. The pull request usually says why, and the review says what
nearly went wrong.

```bash
pr-archaeologist dig --prs      # remembered: later runs keep reading new pull requests
pr-archaeologist dig --no-prs   # turn it off again
```

With `--prs`, the tool looks up the pull requests that commits refer to (`(#12)`,
`Merge pull request #12`) on GitHub and adds, per file: the pull request's stated reasons, its
warnings, and remarks from human reviewers that explain or warn. Template scaffolding
(checklists, HTML comments, code blocks), bot comments and paperwork remarks ("please rebase",
"not signed off") are dropped. A pull request with several commits says its piece once per file.

- Works for repositories whose `origin` is on github.com. It uses `GH_TOKEN` or `GITHUB_TOKEN`
  if set, else the `gh` CLI's login, else anonymous access (public repositories, low rate limit).
- Each pull request is read once and cached; open ones are re-read until they close. At most 300
  new pull requests are read per run (`--max-prs`), newest first, so a large backlog fills in over
  several nights.
- If GitHub cannot be reached, the run carries on without them.
- `explain` never touches the network; it shows what `dig` has cached.

Tried on Reversibility Classes (8 pull requests, about 7 seconds). That repository has no human
review comments, so the review-remark path is covered by unit tests only.

## Incremental caching

State lives in `.pr-archaeologist/cache.json.gz` inside the analysed repo (the directory ignores
itself in git). Three independent keys keep nightly runs cheap:

| Stage | Cache key | Re-done when |
|---|---|---|
| History (`git log`) | The commit the cache was built up to | New commits exist: only `cached..HEAD` is read |
| Blame and function mapping | Per file: blob sha + latest commit touching it | That file changed |
| Markdown / ADR parsing | Per file: content hash | That doc was edited |
| Pull requests (`--prs`) | Pull-request number | Not seen before, or still open |

If the cached commit is no longer an ancestor of `HEAD` (force-push, rebase), the history is rebuilt
automatically. `--full` forces a rebuild. The test suite asserts that an incremental run produces
byte-identical output to a full one.

Outputs are deterministic for a given `HEAD` and are only rewritten when their content changes.
Commits that touch nothing but the generated files are skipped, so a nightly job that commits its
own output settles instead of producing a new diff every night.

On Flask's repository (about 3,800 non-merge commits) a first run took roughly 7 seconds in a
cloud dev container and a no-change rerun under 1 second.

## Overnight agent workflow

`agent/nightly.sh` is the cron entry point: incremental dig, optional agent pass, optional commit.

```cron
17 2 * * * ARCHAEOLOGIST_PULL=1 ARCHAEOLOGIST_COMMIT=1 /opt/pr-archaeologist/agent/nightly.sh /srv/repos/my-service
```

- **Cron**: [`examples/crontab.example`](examples/crontab.example)
- **GitHub Actions**: [`examples/github-actions/nightly-archaeology.yml`](examples/github-actions/nightly-archaeology.yml)
  restores the cache, runs `dig`, commits the refreshed files and uploads the graph as an artifact.
  This repository runs the same workflow on itself: the first run built and committed the index,
  the second restored the cache, found nothing new and committed nothing.
- **Agent pass (optional)**: set `ARCHAEOLOGIST_AGENT_CMD` to any agent CLI that takes a prompt as
  its last argument. The script hands it [`agent/nightly-prompt.md`](agent/nightly-prompt.md), which
  asks the agent to write short, commit-cited notes for the most fragile files under
  `docs/archaeology/`. Because those notes name the files they explain, the next `dig` links them
  into the index and graph, lists their "Invariants" under the file and their "Known traps" among
  its pitfalls. The deterministic core never depends on this.
- **Daytime agents**: paste [`agent/AGENTS-snippet.md`](agent/AGENTS-snippet.md) into your
  `AGENTS.md` / `CLAUDE.md` so coding agents run `explain` before editing.

## How it decides

Everything is heuristic and inspectable; [`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md) has the detail.

- **Commit kind** (fix, revert, security, perf, design, feature, refactor, release, docs, test,
  chore) comes from Conventional Commit prefixes and keyword patterns in the subject. "Fix typo"
  and "fix mypy" are deliberately not counted as bug fixes; "harden", "red team" and "fail-open"
  count as security changes; the bare word "security" does not.
- **A fix only counts against a file it really changed.** The commit that created a file, a
  release commit, and a commit that touched one or two lines of a file while mostly changing
  others (a version bump riding along) are shown in the timeline but not scored.
- **Why and warnings** are sentences from the commit body containing causal language ("because",
  "root cause", "so that") or imperative warnings ("do not", "never", "must").
- **Reverts** are linked to the commit they undo and surface as "tried and reverted".
- **Renames** are followed, so history is keyed by a file's current path. Deleted files are kept in
  a graveyard with the reason they were removed.
- **Function mapping** combines blame (which commits own the surviving lines) with hunk headers of
  fix commits (which functions a fix touched, even if those lines were rewritten since).
- **ADRs and docs** are linked to code by the paths they mention and by commits that cite them
  ("ADR-0002").
- **Hidden coupling** is co-change: files that keep being modified in the same commits, measured
  by overlap so that a README touched by half of all commits is not "coupled" to everything.
- **Rank** orders the scored files within the repository (1 = most fragile). On an old project
  where many files cross the threshold, read the rank, not the label.
- **Fragility score** (0–100) weighs fixes, reverts, security changes and hotfixes heavily, and
  churn, author count, warning comments and coupling lightly. Fragile ≥ 60, caution ≥ 30. Churn
  alone never reaches caution. Documentation and licence files are not scored.

These rules were tightened after the first run on a repository whose history we know
([Reversibility Classes](https://github.com/TwoMuchLabs/reversibility-classes), 46 commits). The
first pass ranked its README as the most fragile file and labelled 44 of 83 files fragile or
caution; after the fixes the top three are the scanner, its command line and the gate, which is
where that project's review and red-team findings actually landed.

## Limits worth knowing

- It reads what was written down. A repository of `wip` and `fix stuff` commits yields a thin story.
- Fragility thresholds are absolute. A decade-old, heavily patched project will show many fragile
  files (about one in eight on Flask); rank order is more meaningful there than the label.
- Commit kinds are keyword rules. They have been checked by hand against one repository's history,
  not measured for accuracy.
- Symbol extraction uses Python's `ast` for Python and regex plus brace matching for other
  languages. It is approximate outside Python.
- Pull requests are read only from github.com, and only those a commit message names. Issues are
  not read.
- Quoted commit and pull-request text is untrusted. The index tells agents to treat it as record,
  not instruction, and the graph escapes it, but a reader should keep that in mind.
- Only Markdown docs are parsed (not reStructuredText or AsciiDoc).
- The cache keeps one record per commit, so very large histories produce large caches;
  `--max-commits` bounds the first run.
- The agent pass is half proven. Notes written to the prompt's format are picked up and shown
  (tried on Reversibility Classes), but `nightly.sh` has not yet driven a signed-in agent CLI end
  to end.

## Development

```bash
python -m unittest discover -s tests -v     # stdlib only; builds throwaway git repos
```

Layout:

```text
src/pr_archaeologist/
  history.py     git log parser, rename following, incremental update
  classify.py    commit message reading
  blame.py       git blame parser, warning-comment detection
  symbols.py     function and class extraction
  docs.py        Markdown / ADR reader
  cache.py       on-disk state
  model.py       the knowledge model, scoring and graph payload
  render_md.py   CODEBASE_ARCHAEOLOGY.md
  render_html.py + graph_template.html   the static graph
  explain.py     terminal output
  cli.py
agent/           nightly script, agent prompt, AGENTS.md snippet
examples/        demo repo builder, sample output, cron and CI examples
```

## License

MIT
