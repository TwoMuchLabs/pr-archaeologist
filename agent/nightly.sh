#!/usr/bin/env bash
# Nightly archaeology run. Safe to call from cron: incremental, idempotent, quiet when idle.
#
#   agent/nightly.sh /path/to/repo
#
# Environment:
#   ARCHAEOLOGIST_PULL=1         fast-forward the checkout before analysing
#   ARCHAEOLOGIST_AGENT_CMD=...  optional agent CLI that takes a prompt as its last argument
#                                (for example: "claude -p"). Runs agent/nightly-prompt.md after
#                                the dig so the agent can write cited notes for fragile files.
#   ARCHAEOLOGIST_COMMIT=1       commit the refreshed index, graph and notes if they changed
#   ARCHAEOLOGIST_PUSH=1         push that commit
set -euo pipefail

REPO="$(cd "${1:-$PWD}" && pwd)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

if [ "${ARCHAEOLOGIST_PULL:-0}" = "1" ]; then
  git pull --ff-only --quiet
fi

pr-archaeologist dig --repo "$REPO"

if [ -n "${ARCHAEOLOGIST_AGENT_CMD:-}" ]; then
  # The agent only writes Markdown under docs/archaeology/. Those notes name the files they
  # explain, so the second dig links them into the index and the graph automatically.
  # The command is evaluated by the shell, so quote arguments inside it as you would on a
  # command line; the prompt is passed as one final argument.
  PROMPT="$(cat "$HERE/nightly-prompt.md")" bash -c "$ARCHAEOLOGIST_AGENT_CMD \"\$PROMPT\""
  pr-archaeologist dig --repo "$REPO" --quiet
fi

if [ "${ARCHAEOLOGIST_COMMIT:-0}" = "1" ]; then
  git add CODEBASE_ARCHAEOLOGY.md archaeology-graph.html
  [ -d docs/archaeology ] && git add docs/archaeology
  if ! git diff --cached --quiet; then
    git commit --quiet -m "chore(archaeology): refresh codebase archaeology"
    [ "${ARCHAEOLOGIST_PUSH:-0}" = "1" ] && git push --quiet
  fi
fi
