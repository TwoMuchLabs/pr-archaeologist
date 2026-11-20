#!/usr/bin/env python3
"""Save what GitHub answers when the tool reads pull requests, so that code can be
checked offline.

    python scripts/record_github.py <repo> <out.json.gz> [--max-prs N]

Reads the history of <repo>, then runs the same pull-request and issue reading that
`dig --prs` does, keeping every answer exactly as GitHub gave it. The file maps each
request path to its JSON answer, or to {"__http_error__": code} when GitHub refused.

`replay(path)` turns such a file back into a fetcher for `prs.update(fetch_json=...)`.
A path that was never recorded raises KeyError: a replay must not guess.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

ERROR = "__http_error__"


def recording(fetch, tape):
    def fetch_and_keep(path):
        try:
            tape[path] = fetch(path)
        except urllib.error.HTTPError as exc:
            tape[path] = {ERROR: exc.code}
            raise
        return tape[path]

    return fetch_and_keep


def replay(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        tape = json.load(fh)

    def fetch(request):
        answer = tape[request]
        if isinstance(answer, dict) and ERROR in answer:
            raise urllib.error.HTTPError(request, answer[ERROR], "recorded", None, None)
        return answer

    return fetch


def main() -> int:
    from pr_archaeologist import cache, history, prs

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("repo")
    parser.add_argument("out")
    parser.add_argument("--max-prs", type=int, default=90)
    args = parser.parse_args()

    state = cache.empty_state()
    history.update(args.repo, state, full=True)
    tape: dict = {}
    result = prs.update(
        args.repo, state, fetch_json=recording(prs.http_fetcher(prs.find_token()), tape), limit=args.max_prs
    )
    slug = prs.remote_slug(args.repo)
    if slug is None or not tape:
        print(f"nothing recorded: {result}", file=sys.stderr)
        return 1
    with gzip.open(args.out, "wt", encoding="utf-8") as fh:
        json.dump(tape, fh, sort_keys=True)
    print(f"{'/'.join(slug)}: {len(tape)} answers kept in {args.out}; {result}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
