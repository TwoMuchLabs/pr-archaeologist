<!-- Paste into your repository's AGENTS.md, CLAUDE.md or equivalent agent instructions. -->

## Check the history before you edit

This repository keeps a generated history index, `CODEBASE_ARCHAEOLOGY.md`, refreshed nightly by
pr-archaeologist.

- Before changing a file, run `pr-archaeologist explain <path>` (add `--json` for structured
  output, or `--function <name>` for one function). If the command is unavailable, open
  `CODEBASE_ARCHAEOLOGY.md` and search for the heading `### \`<path>\``.
- If the file is marked **fragile** or **caution**, read its "Past pitfalls" first. Do not undo a
  recorded fix, reintroduce a reverted approach or contradict a linked ADR without saying so
  explicitly in your summary and PR description.
- If it lists "Usually changes with", open those files as well and check whether they need the
  same change.
- Never edit `CODEBASE_ARCHAEOLOGY.md` or `archaeology-graph.html` by hand; they are regenerated.
