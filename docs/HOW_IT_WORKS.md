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

## 5. The model (`model.py`)

Per file: intent (birth commit, later redesigns, linked decisions), full timeline, pitfalls,
co-change partners, function histories and a fragility score:

```text
weight = 3·fixes* + 6·reverts + 5·security + 2·hotfixes
       + 2·ln(1+commits) + 2·ln(1+max(0, authors−2))
       + 1.5·warning_comments + 2·strong_couplings
score  = 100 · (1 − e^(−weight/18))          fragile ≥ 60, caution ≥ 30
```

`fixes*` grows linearly to six fixes and by square root after that; the other counts are capped.
Co-change counts ignore commits touching more than 25 files, and changelog-like files.

## 6. Outputs

- **Markdown**: every notable file gets a `### \`path\`` heading so an agent can grep for it. Quiet
  files collapse into one table; deleted files go to a graveyard.
- **HTML**: `graph_template.html` plus the graph payload inlined as JSON. The simulation, rendering,
  search, filters and time slider are plain JavaScript on a canvas. Commit text is untrusted, so the
  payload sits in a non-executing JSON block with `<`, `>` and `&` escaped, and every value is
  HTML-escaped before it reaches the DOM. The graph shows the 250 highest-ranked files and up to
  400 landmark commits by default (`--max-graph-files`).
