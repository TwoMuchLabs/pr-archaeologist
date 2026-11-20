# How pr-archaeologist works

```text
git log --raw --numstat ─┐
git show -U0 (fixes)     ├─► cache.json.gz ─► model ─┬─► CODEBASE_ARCHAEOLOGY.md
git blame --incremental ─┤   (incremental)           ├─► archaeology-graph.html
*.md / ADRs ─────────────┘                           └─► explain <path>
```

The cache holds facts; the model is recomputed from it on every run. Changing a scoring weight or
a sentence in the output never requires re-reading git.

## 1. History (`history.py`)

One streaming command provides everything per commit:

```text
git log --reverse --topo-order -M --raw --numstat -z --no-abbrev --format=<fields>
```

- `--raw` gives each file's status (added, modified, deleted, renamed); `--numstat` gives line
  counts; `-z` makes paths with spaces or unicode unambiguous.
- Commits are applied oldest first. On a rename the file's whole record moves to the new path, so
  `payments/ledger.py` carries the history it had as `payments/books.py`.
- `This reverts commit <sha>` becomes a two-way link between the revert and its target.
- `Merge pull request #N` commits label the commits they brought in with that PR number; squash
  merges are read from the `(#N)` suffix.
- For fix, security, revert and perf commits only, `git show -U0` is read for hunk headers
  (`@@ ... @@ def charge_with_retry(...)`) to learn which functions were touched. git's built-in
  language patterns are enabled through a private attributes file, so this works without a
  `.gitattributes` in the repository.
- Commits that touch only pr-archaeologist's own outputs are skipped.

Incremental runs read `cached_head..HEAD`. If `cached_head` is not an ancestor of `HEAD`, the
history is rebuilt.

## 2. Reading messages (`classify.py`)

| Signal | Source |
|---|---|
| Kind | Conventional Commit type, else keyword patterns on the subject |
| PRs, issues, tickets | `(#12)`, `Merge pull request #12`, `Fixes #30`, `PAY-812` |
| Why | Body sentences with causal language; for fixes and designs, the first sentence as a fallback |
| Warnings | Body sentences with "do not", "never", "must", "workaround", "note:" |

Trailers such as `Signed-off-by` are dropped.

## 3. Blame and functions (`blame.py`, `symbols.py`)

`git blame --incremental -w -M HEAD -- <file>` yields (line range, commit) groups. A
`.git-blame-ignore-revs` file is honoured if present. Symbols come from Python's `ast`, or from
declaration regexes plus brace or indentation matching for other languages. Each function then has:

- the oldest commit still owning one of its lines (its origin),
- the newest (last touched),
- every risky commit among its line owners, merged with the hunk-header hits from step 1.

Comments containing `HACK`, `WARNING`, `do not change`, `order matters` and similar are kept as
tripwires with the commit that wrote them. Files are blamed in parallel and re-blamed only when
their blob or latest commit changes.

## 4. Docs and ADRs (`docs.py`)

Tracked and untracked-but-not-ignored Markdown files are parsed for title, status, date and the
Context / Decision / Consequences sections. A file counts as an ADR if it lives under `adr/`,
`decisions/` or `rfcs/`, is named like `0007-something.md`, or has that section shape. Docs are
tied to code by the paths they mention (including a file's former names) and by commits that cite
the ADR number.

## 4b. Pull requests (`prs.py`, optional)

`dig --prs` collects the pull-request numbers found in step 1 and reads each one from the GitHub
REST API with the standard library: the pull request, then its issue comments, review comments
and reviews when it has any. The body is stripped of HTML comments, checklists, tables, images and
code blocks, then split into sentences the same way commit bodies are. Kept: up to three reasons
and two warnings from the description, and up to four reviewer remarks. A reviewer remark is kept
only if it states a hazard or a rule about the code ("never", "otherwise", "race", "deprecate",
"backwards compatible", "breaks"); questions, replies, quotes and first-person hedging are
conversation and are dropped. Also dropped: bots, and anything about the pull request's own
paperwork. A number that turns out to be an issue is remembered as missing
so it is not asked for again. Results live in the cache under `prs`, keyed by number, and survive
a history rebuild.

Issues come next. An issue is wanted if a commit message, or the title or body of a pull request
that commit belongs to, names it after "fix", "close" or "resolve". Each is read once (plus its
comments, if any), reduced to its title, opening sentence, one warning and up to two hazard
remarks, and cached under `issues`. A number that turns out to be a pull request is skipped.

In the model, the issue closed by a fix, security change or revert is listed among the file's
pitfalls as "Reported: <title> — <opening sentence>", once per file. The issue closed by the commit
that created a file joins "why it exists" as "Asked for in #N".

A pull request's reasons and review remarks attach only where the commit fixed or undid
something, and each is shown once per file. A warning, whether from a commit message or its
pull request, attaches to a file only when the commit touched six files or fewer, the file carries
a quarter or more of the changed lines, or the warning names the file.

## 5. The model (`model.py`)

Per file: intent (birth commit, later redesigns, linked decisions), full timeline, pitfalls,
co-change partners, function histories, a fragility score and a rank among the repository's scored
files:

```text
weight = 3·fixes* + 6·reverts + 5·security + 2·hotfixes
       + 2·ln(1+commits) + 2·ln(1+max(0, authors−2))
       + 1.5·warning_comments + 2·strong_couplings
score  = 100 · (1 − e^(−weight/18))          fragile ≥ 60, caution ≥ 30
```

`fixes*` grows linearly to six fixes and by square root after that; the other counts are capped.

What does not count:

- the commit that created the file, even if it was a fix;
- a risky commit that changed two lines or fewer of this file while touching more than three files;
- release commits (`Release v1.2.0`, `Prepare v0.3.0`), which bundle earlier work;
- churn and coupling on their own: with no fix, revert, security change or warning comment, the
  score stops just below caution;
- documentation and licence files (`.md`, `.rst`, `.txt`, `.cff`, `LICENSE`), which are not scored.

Co-change counts ignore commits touching more than 25 files, and changelog-like files. Two files
are listed as changing together when they share at least two commits, 30% of this file's commits
and a Jaccard overlap of 0.25; the coupling is "strong" (scored, drawn in the graph, listed under
hidden couplings) at three commits, 50% and an overlap of 0.4.

For a function, a risky commit is listed only if it came after the commit that wrote the
function's oldest surviving lines.

## 6. Outputs

- **Markdown**: every notable file gets a `### \`path\`` heading so an agent can grep for it. Quiet
  files collapse into one table; deleted files go to a graveyard.
- **HTML**: `graph_template.html` plus the graph payload inlined as JSON. The simulation, rendering,
  search, filters and time slider are plain JavaScript on a canvas. Commit text is untrusted, so the
  payload sits in a non-executing JSON block with `<`, `>` and `&` escaped, and every value is
  HTML-escaped before it reaches the DOM. The graph shows the 250 highest-ranked files and up to
  400 landmark commits by default (`--max-graph-files`).
